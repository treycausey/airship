#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["segno"]
# ///
"""airship — install a finished iOS .ipa onto your iPhone over the air via Tailscale.

Usage:
    ./airship.py <app.ipa>
    uv run airship.py <app.ipa>

No cable, no shared WiFi. Both the Mac and the iPhone must be on the same
Tailscale tailnet, with HTTPS certs enabled. The .ipa must be ad-hoc/development
signed with the iPhone's UDID in its provisioning profile.

See docs/2026-06-24-airship-design.md for the full design.
"""

from __future__ import annotations

import argparse
import http.server
import json
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

PREFERRED_PORT = 4190  # airship's allocated dev-server block

# Stable, URL-safe staged filenames (never the original IPA basename).
IPA_NAME = "app.ipa"
MANIFEST_NAME = "manifest.plist"
INDEX_NAME = "index.html"

CONTENT_TYPES = {
    ".ipa": "application/octet-stream",
    ".plist": "text/xml",
    ".html": "text/html; charset=utf-8",
}


class AirshipError(Exception):
    """User-facing error with a clear message (no stack trace shown)."""


# --------------------------------------------------------------------------- #
# IPA inspection
# --------------------------------------------------------------------------- #


def read_ipa_metadata(ipa_path: Path) -> dict[str, str]:
    """Pull bundle id, version, and display name from the IPA's Info.plist."""
    if not ipa_path.exists():
        raise AirshipError(f"IPA not found: {ipa_path}")
    if not zipfile.is_zipfile(ipa_path):
        raise AirshipError(f"Not a valid .ipa (zip) file: {ipa_path}")

    with zipfile.ZipFile(ipa_path) as zf:
        names = zf.namelist()
        # Find the single Payload/<App>.app/Info.plist at the bundle root.
        info_candidates = [
            n
            for n in names
            if n.startswith("Payload/")
            and n.endswith(".app/Info.plist")
            and n.count("/") == 2
        ]
        if not info_candidates:
            raise AirshipError(
                "No Payload/*.app/Info.plist found in the IPA — is this a real "
                "iOS app archive?"
            )
        if len(info_candidates) > 1:
            raise AirshipError(
                f"Expected one app bundle, found {len(info_candidates)}: "
                f"{info_candidates}"
            )

        info_name = info_candidates[0]
        app_dir = info_name[: -len("/Info.plist")]  # Payload/Foo.app
        info = plistlib.loads(zf.read(info_name))

    bundle_id = info.get("CFBundleIdentifier")
    if not bundle_id:
        raise AirshipError("Info.plist is missing CFBundleIdentifier.")

    # Manifest bundle-version is the build number (CFBundleVersion); fall back to
    # the marketing version.
    version = (
        info.get("CFBundleVersion")
        or info.get("CFBundleShortVersionString")
        or "0"
    )
    app_stem = Path(app_dir).name.removesuffix(".app")
    title = (
        info.get("CFBundleDisplayName")
        or info.get("CFBundleName")
        or app_stem
    )

    return {
        "bundle_id": str(bundle_id),
        "version": str(version),
        "title": str(title),
        "embedded_profile": f"{app_dir}/embedded.mobileprovision",
    }


def warn_on_signing(ipa_path: Path, profile_member: str) -> None:
    """Advisory preflight: decode embedded.mobileprovision and warn on the
    common silent 'Unable to Install' causes. Never fails the run."""
    try:
        with zipfile.ZipFile(ipa_path) as zf:
            if profile_member not in zf.namelist():
                warn(
                    "No embedded.mobileprovision in the IPA — if this is an App "
                    "Store build it will not install over the air."
                )
                return
            raw = zf.read(profile_member)
    except (KeyError, zipfile.BadZipFile):
        return  # best-effort only

    plist = _decode_cms_plist(raw)
    if plist is None:
        return

    import datetime

    expiry = plist.get("ExpirationDate")
    if isinstance(expiry, datetime.datetime):
        now = datetime.datetime.now(tz=expiry.tzinfo)
        if expiry < now:
            warn(f"Provisioning profile EXPIRED on {expiry:%Y-%m-%d}.")

    devices = plist.get("ProvisionedDevices")
    if not devices:
        warn(
            "Provisioning profile has no ProvisionedDevices — this looks like an "
            "App Store / enterprise profile and cannot install ad-hoc OTA on a "
            "registered device."
        )
        return

    udid = _iphone_udid()
    if udid and udid not in devices:
        warn(
            f"This iPhone's UDID ({udid}) is not in the provisioning profile's "
            f"{len(devices)} provisioned device(s) — install will likely fail."
        )


def _decode_cms_plist(raw: bytes) -> dict | None:
    """embedded.mobileprovision is a CMS-signed blob wrapping an XML plist."""
    try:
        out = subprocess.run(
            ["security", "cms", "-D", "-i", "/dev/stdin"],
            input=raw,
            capture_output=True,
            check=True,
        )
        return plistlib.loads(out.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, plistlib.InvalidFileException):
        return None


def _iphone_udid() -> str | None:
    """Best-effort: read the connected iPhone's UDID via `iinfo`."""
    try:
        out = subprocess.run(
            ["iinfo"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    # iinfo output format is unknown to us; scan for a 40-hex or 8-16 UUID-ish
    # token. Keep this purely advisory.
    import re

    for pat in (r"\b[0-9a-fA-F]{40}\b", r"\b[0-9A-F]{8}-[0-9A-F]{16}\b"):
        m = re.search(pat, out.stdout)
        if m:
            return m.group(0)
    return None


# --------------------------------------------------------------------------- #
# Tailscale
# --------------------------------------------------------------------------- #


def tailscale_base_url() -> str:
    """Return https://<node>.<tailnet>.ts.net with the trailing dot stripped."""
    if shutil.which("tailscale") is None:
        raise AirshipError("`tailscale` not found on PATH. Is Tailscale installed?")
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AirshipError(
            "`tailscale status` failed — is this node logged in?\n"
            + exc.stderr.strip()
        ) from exc

    dns_name = json.loads(out.stdout).get("Self", {}).get("DNSName", "")
    host = dns_name.rstrip(".")  # FQDN comes back with a trailing dot
    if not host:
        raise AirshipError("Could not determine this node's Tailscale DNS name.")
    return f"https://{host}"


def assert_serve_root_free() -> None:
    """Refuse to clobber an existing Serve mapping on `/`."""
    try:
        out = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return  # no existing config / older CLI — nothing to clobber
    text = out.stdout.strip()
    if not text or text == "null":
        return
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        return
    web = cfg.get("Web") or {}
    for host_cfg in web.values():
        handlers = (host_cfg or {}).get("Handlers") or {}
        if "/" in handlers:
            raise AirshipError(
                "Tailscale Serve already maps `/` to another service. Run "
                "`tailscale serve status` and clear it before using airship "
                "(airship will not overwrite your existing Serve config)."
            )


def tailscale_ip() -> str:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip().splitlines()[0]
    except (subprocess.CalledProcessError, IndexError):
        return "(unknown)"


# --------------------------------------------------------------------------- #
# Artifact staging
# --------------------------------------------------------------------------- #


def build_manifest(base_url: str, meta: dict[str, str]) -> bytes:
    """Generate the Apple OTA manifest as XML plist (no string templates)."""
    manifest = {
        "items": [
            {
                "assets": [
                    {
                        "kind": "software-package",
                        "url": f"{base_url}/{IPA_NAME}",
                    }
                ],
                "metadata": {
                    "bundle-identifier": meta["bundle_id"],
                    "bundle-version": meta["version"],
                    "kind": "software",
                    "title": meta["title"],
                },
            }
        ]
    }
    return plistlib.dumps(manifest, fmt=plistlib.FMT_XML)


def itms_url(base_url: str) -> str:
    manifest_url = f"{base_url}/{MANIFEST_NAME}"
    return (
        "itms-services://?action=download-manifest&url="
        + quote(manifest_url, safe="")
    )


def build_index_html(base_url: str, meta: dict[str, str]) -> str:
    """Landing page. `&` in the href is HTML-escaped as `&amp;`."""
    href = itms_url(base_url).replace("&", "&amp;")
    from html import escape

    title = escape(meta["title"])
    bundle_id = escape(meta["bundle_id"])
    version = escape(meta["version"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Install {title}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         display: grid; place-items: center; min-height: 100vh;
         background: #f5f5f7; color: #1d1d1f; }}
  .card {{ background: #fff; padding: 2.5rem; border-radius: 18px;
          box-shadow: 0 8px 30px rgba(0,0,0,.08); text-align: center;
          max-width: 320px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .meta {{ color: #6e6e73; font-size: .85rem; margin-bottom: 1.75rem; }}
  a.install {{ display: inline-block; background: #0071e3; color: #fff;
              text-decoration: none; padding: .85rem 2rem; border-radius: 980px;
              font-weight: 600; font-size: 1.05rem; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <div class="meta">{bundle_id} &middot; v{version}</div>
    <a class="install" href="{href}">Install</a>
  </div>
</body>
</html>
"""


def stage_artifacts(ipa_path: Path, base_url: str, meta: dict[str, str]) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="airship-"))
    shutil.copy2(ipa_path, staging / IPA_NAME)
    (staging / MANIFEST_NAME).write_bytes(build_manifest(base_url, meta))
    (staging / INDEX_NAME).write_text(build_index_html(base_url, meta))
    return staging


# --------------------------------------------------------------------------- #
# Local HTTP server
# --------------------------------------------------------------------------- #


def make_handler(root: Path) -> type[http.server.SimpleHTTPRequestHandler]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def guess_type(self, path):  # noqa: A003 (matching stdlib signature)
            for suffix, ctype in CONTENT_TYPES.items():
                if path.endswith(suffix):
                    return ctype
            return super().guess_type(path)

        def log_message(self, fmt, *args):
            sys.stderr.write("  [http] " + (fmt % args) + "\n")

    return Handler


def start_server(root: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    """Bind 127.0.0.1 (Serve proxies from localhost; no LAN exposure needed).
    Prefer PREFERRED_PORT, fall back to an OS-assigned free port."""
    handler = make_handler(root)
    for port in (PREFERRED_PORT, 0):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            if port == 0:
                raise
            continue
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def warn(msg: str) -> None:
    print(f"\033[33m⚠ {msg}\033[0m", file=sys.stderr)


def run(ipa_path: Path) -> int:
    meta = read_ipa_metadata(ipa_path)
    warn_on_signing(ipa_path, meta["embedded_profile"])

    base_url = tailscale_base_url()
    assert_serve_root_free()

    staging = stage_artifacts(ipa_path, base_url, meta)
    server, port = start_server(staging)

    serve_proc = subprocess.Popen(["tailscale", "serve", str(port)])

    def cleanup(*_args):
        serve_proc.terminate()
        try:
            serve_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            serve_proc.kill()
        server.shutdown()
        server.server_close()
        shutil.rmtree(staging, ignore_errors=True)

    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (cleanup(), sys.exit(0)))

    try:
        landing = f"{base_url}/{INDEX_NAME}"
        _print_handoff(meta, landing, base_url, port)
        serve_proc.wait()  # block until Serve child exits
    finally:
        cleanup()
    return 0


def _print_handoff(meta: dict[str, str], landing: str, base_url: str, port: int) -> None:
    import segno

    print()
    print(f"  \033[1m{meta['title']}\033[0m  {meta['bundle_id']}  v{meta['version']}")
    print()
    print("  Open this on your iPhone (Safari), then tap Install:")
    print(f"  \033[36m{landing}\033[0m")
    print()
    segno.make(landing, error="m").terminal(compact=True)
    print()
    print(f"  Tailscale IP: {tailscale_ip()}  (local server on 127.0.0.1:{port})")
    print(f"  itms link (debug): {itms_url(base_url)}")
    print()
    print("  Serving… press Ctrl-C when the install finishes.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install an iOS .ipa onto your iPhone over the air via Tailscale."
    )
    parser.add_argument("ipa", type=Path, help="Path to the .ipa to install")
    args = parser.parse_args(argv)

    try:
        return run(args.ipa)
    except AirshipError as exc:
        print(f"\033[31m✗ {exc}\033[0m", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
