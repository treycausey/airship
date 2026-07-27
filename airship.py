#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["segno"]
# ///
# file-length: accept — single-file uv script by design.
"""airship — install a finished iOS .ipa onto your iPhone over the air via Tailscale.

Usage:
    ./airship.py [app.ipa] [--stay]

With no argument, serves the newest .ipa found under the current directory.

No cable, no shared WiFi. Both the Mac and the iPhone must be on the same
Tailscale tailnet, with HTTPS certs enabled. The .ipa must be ad-hoc/development
signed with the iPhone's UDID in its provisioning profile.

See docs/2026-06-24-airship-design.md for the full design.
"""

from __future__ import annotations

import argparse
import datetime
import http.server
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from html import escape
from pathlib import Path
from urllib.parse import quote

PREFERRED_PORT = 4190  # airship's allocated dev-server block
# The HTTPS port Tailscale Serve publishes on. 443 is the tailnet default and
# the right answer almost always — but it is a SHARED origin, and a service
# worker registered there by any other project will serve its own cached shell
# in place of the install page (this happened: a PWA on 443 swallowed the OTA
# landing page). A different port is a different origin, which sidesteps that
# entirely. See --https-port.
DEFAULT_HTTPS_PORT = 443
IDLE_EXIT_GRACE = 45.0  # seconds of quiet after the IPA download before auto-exit
SKIP_DIRS = {"node_modules"}  # pruned (with dotdirs) during no-arg .ipa discovery

# Ownership marker: lets the next run positively identify this run's processes
# (previous airship, or an orphaned serve child after a crash) without guessing.
INSTANCE_FILE = Path(tempfile.gettempdir()) / "airship-instance.json"

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


def _printable(s: str) -> str:
    """Strip control characters before echoing externally-sourced text (IPA
    metadata, HTTP request lines) to the terminal — ANSI/OSC sequences could
    otherwise rewrite what the user sees."""
    return re.sub(r"[\x00-\x1f\x7f]", "�", s)


def warn(msg: str) -> None:
    print(f"\033[33m⚠ {msg}\033[0m", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"\n  \033[32m✓ {msg}\033[0m")


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
        try:
            info = plistlib.loads(zf.read(info_name))
        except plistlib.InvalidFileException as exc:
            raise AirshipError(f"Could not parse {info_name}: {exc}") from exc

    if not isinstance(info, dict):
        raise AirshipError(
            f"{info_name} is a plist {type(info).__name__}, not the expected "
            "dictionary — is this a real iOS app archive?"
        )
    bundle_id = info.get("CFBundleIdentifier")
    if not bundle_id:
        raise AirshipError("Info.plist is missing CFBundleIdentifier.")

    # Manifest bundle-version is the build number (CFBundleVersion); fall back to
    # the marketing version.
    version = (
        info.get("CFBundleVersion") or info.get("CFBundleShortVersionString") or "0"
    )
    app_stem = Path(app_dir).name.removesuffix(".app")
    title = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app_stem

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

    expiry = plist.get("ExpirationDate")
    if isinstance(expiry, datetime.datetime):
        now = datetime.datetime.now(tz=expiry.tzinfo)
        if expiry < now:
            warn(f"Provisioning profile EXPIRED on {expiry:%Y-%m-%d}.")

    devices = plist.get("ProvisionedDevices")
    if not devices:
        # In-house/enterprise profiles provision every device — Apple supports
        # OTA install for those, so only a device-less, non-enterprise profile
        # (an App Store profile) is a problem.
        if not plist.get("ProvisionsAllDevices"):
            warn(
                "Provisioning profile has no ProvisionedDevices and is not an "
                "enterprise (ProvisionsAllDevices) profile — this looks like "
                "an App Store profile and cannot install OTA on a registered "
                "device."
            )
        return

    known = _known_device_udids()
    if known and not known & set(devices):
        warn(
            f"None of the {len(known)} device(s) this Mac knows about is in "
            f"the provisioning profile's {len(devices)} provisioned "
            "device(s) — install will likely fail."
        )


def _decode_cms_plist(raw: bytes) -> dict | None:
    """embedded.mobileprovision is a CMS-signed blob wrapping an XML plist.
    Returns None (callers skip, advisory-only) unless the blob decodes to the
    expected dictionary."""
    try:
        out = subprocess.run(
            ["security", "cms", "-D", "-i", "/dev/stdin"],
            input=raw,
            capture_output=True,
            check=True,
            timeout=10,
        )
        plist = plistlib.loads(out.stdout)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        plistlib.InvalidFileException,
    ):
        return None
    return plist if isinstance(plist, dict) else None


# Real iOS-family hardware carries a modern 8-16 UDID (00008120-000A11BB…)
# or a legacy 40-hex one; Macs and simulators carry standard 5-group UUIDs,
# which neither pattern matches.
_UDID_PATTERNS = (r"\b[0-9A-F]{8}-[0-9A-F]{16}\b", r"\b[0-9a-fA-F]{40}\b")


def parse_devicectl_udids(data: dict) -> frozenset[str]:
    """iOS-platform device UDIDs from `devicectl list devices` JSON."""
    udids: set[str] = set()
    for dev in (data.get("result") or {}).get("devices") or []:
        hw = (dev or {}).get("hardwareProperties") or {}
        if hw.get("platform") == "iOS" and hw.get("udid"):
            udids.add(str(hw["udid"]))
    return frozenset(udids)


def parse_xctrace_udids(text: str) -> frozenset[str]:
    """Device UDIDs from `xctrace list devices` text output (everything above
    the simulators section; the UDID patterns skip Macs and simulators).
    Unlike devicectl's JSON, the text carries no platform field, so non-iOS
    hardware (an Apple TV, a Watch) is included — acceptable looseness for an
    advisory fallback that only runs when devicectl is unusable."""
    head = text.split("== Simulators ==", 1)[0]
    found: set[str] = set()
    for pat in _UDID_PATTERNS:
        found.update(re.findall(pat, head))
    return frozenset(found)


def _known_device_udids() -> frozenset[str]:
    """UDIDs of iPhones/iPads this Mac has paired with, even if currently
    offline — the airship scenario is exactly a phone that is NOT cabled.
    Best-effort and purely advisory: CoreDevice's registry via `devicectl`
    (structured JSON, filterable to iOS), falling back to `xctrace` text.
    Empty when Xcode tooling is unavailable."""
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "devices.json"
        try:
            subprocess.run(
                ["xcrun", "devicectl", "list", "devices", "--timeout", "5",
                 "--json-output", str(out_path), "--quiet"],
                capture_output=True, timeout=15, check=False,
            )
            # A parseable registry is authoritative, even when empty — don't
            # ask xctrace's looser text output for a second opinion. But only
            # a real registry counts: an error envelope (or any JSON without
            # "result") must fall back, not read as "no devices".
            # TypeError/AttributeError: advisory code must survive any JSON
            # shape devicectl emits, not just the object we expect.
            data = json.loads(out_path.read_text())
            if not isinstance(data, dict) or "result" not in data:
                raise ValueError("no result envelope")
            return parse_devicectl_udids(data)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError,
                ValueError, TypeError, AttributeError):
            pass  # devicectl unavailable or unusable — fall back to xctrace
    try:
        out = subprocess.run(
            ["xcrun", "xctrace", "list", "devices"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return parse_xctrace_udids(out.stdout)


def find_newest_ipa(root: Path) -> Path:
    """Newest .ipa under root, skipping dot-directories and node_modules."""
    root = root.resolve()
    if root in (Path.home(), Path(root.anchor)):
        raise AirshipError(
            f"Refusing to hunt for .ipa files under {root} — run from a project "
            "directory or pass a path explicitly."
        )
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
        ]
        candidates += [Path(dirpath) / f for f in filenames if f.endswith(".ipa")]
    if not candidates:
        raise AirshipError(f"No .ipa found under {root} — pass a path explicitly.")
    return max(candidates, key=lambda p: p.stat().st_mtime)


# --------------------------------------------------------------------------- #
# Tailscale
# --------------------------------------------------------------------------- #


def tailscale_base_url(https_port: int = DEFAULT_HTTPS_PORT) -> str:
    """Return https://<node>.<tailnet>.ts.net for `https_port`, trailing dot
    stripped. A non-443 port is appended, since that is a distinct origin."""
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
            "`tailscale status` failed — is this node up and logged in? "
            "(try `tailscale up`)\n" + exc.stderr.strip()
        ) from exc

    dns_name = json.loads(out.stdout).get("Self", {}).get("DNSName", "")
    host = dns_name.rstrip(".")  # FQDN comes back with a trailing dot
    if not host:
        raise AirshipError("Could not determine this node's Tailscale DNS name.")
    if https_port != DEFAULT_HTTPS_PORT:
        return f"https://{host}:{https_port}"
    return f"https://{host}"


def tailscale_self_ips() -> frozenset[str]:
    """This node's own Tailscale IPs (v4 and v6)."""
    try:
        out = subprocess.run(
            ["tailscale", "ip"], capture_output=True, text=True, check=True
        )
        return frozenset(line.strip() for line in out.stdout.splitlines() if line.strip())
    except subprocess.CalledProcessError:
        return frozenset()


def tailscale_ip() -> str:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip().splitlines()[0]
    except (subprocess.CalledProcessError, IndexError):
        return "(unknown)"


def serve_status() -> dict:
    """Parsed `tailscale serve status --json`. FAILS CLOSED: callers decide
    whether `/` is safe to take based on this answer, so an unreadable status
    raises instead of reading as "nothing is mapped"."""
    try:
        out = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout) or {}
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = exc.stderr.strip() if hasattr(exc, "stderr") and exc.stderr else exc
        raise AirshipError(
            "Could not read `tailscale serve status` — refusing to guess "
            f"whether `/` is free.\n{detail}"
        ) from exc


def inspect_serve_root(
    cfg: dict, https_port: int = DEFAULT_HTTPS_PORT
) -> tuple[str, str] | None:
    """Find an existing Serve handler on `/` of `https_port` (the only port
    airship will use) in a `serve status --json` dict.

    Returns (scope, target): scope is "background" (persistent `--bg` config,
    top-level Web key) or "foreground" (a live `tailscale serve` session under
    the Foreground key); target is the proxy URL or the raw handler. None when
    `/` is free.
    """

    suffix = f":{https_port}"

    def root_target(web: dict) -> str | None:
        for host, host_cfg in (web or {}).items():
            if not host.endswith(suffix):
                continue  # mappings on other HTTPS ports never conflict with us
            handler = ((host_cfg or {}).get("Handlers") or {}).get("/")
            if handler:
                return handler.get("Proxy") or json.dumps(handler)
        return None

    target = root_target(cfg.get("Web") or {})
    if target:
        return ("background", target)
    for session in (cfg.get("Foreground") or {}).values():
        target = root_target((session or {}).get("Web") or {})
        if target:
            return ("foreground", target)
    return None


def proxy_port(target: str) -> int | None:
    m = re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost):(\d+)/?", target)
    return int(m.group(1)) if m else None


def _pid_command(pid: int) -> str | None:
    """Command line of a live process, or None if it is gone."""
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,  # nonzero simply means the pid is gone
    )
    return out.stdout.strip() or None


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _wait_until(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def read_instance() -> dict:
    try:
        return json.loads(INSTANCE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_instance(serve_pid: int) -> None:
    INSTANCE_FILE.write_text(
        json.dumps({"pid": os.getpid(), "serve_pid": serve_pid})
    )


def ensure_serve_root_free(https_port: int = DEFAULT_HTTPS_PORT) -> None:
    """Make `/` on :`https_port` available before we serve.

    airship's own leftovers are identified positively via the instance file
    written on every run: a previous airship still running is SIGTERMed and
    taken over; an orphaned `tailscale serve` child from a crashed run is
    killed (its foreground session dies with it). Anything else holding `/`
    belongs to someone else and is never touched — airship refuses with
    instructions instead.
    """
    inst = read_instance()
    pid, serve_pid = inst.get("pid"), inst.get("serve_pid")
    if pid and "airship" in (_pid_command(pid) or ""):
        warn(f"Previous airship still running (pid {pid}) — taking over.")
        _terminate_pid(pid)
        if not _wait_until(lambda: _pid_command(pid) is None, timeout=15):
            raise AirshipError(
                f"Previous airship (pid {pid}) did not exit within 15s — "
                "kill it manually and rerun."
            )
    elif serve_pid and "tailscale" in (_pid_command(serve_pid) or ""):
        warn(
            f"Cleaning up orphaned `tailscale serve` (pid {serve_pid}) "
            "left by a crashed airship."
        )
        _terminate_pid(serve_pid)
        _wait_until(lambda: _pid_command(serve_pid) is None, timeout=10)
    INSTANCE_FILE.unlink(missing_ok=True)

    # Whatever still maps `/` is not ours. Give teardown a moment, then refuse.
    if _wait_until(
        lambda: inspect_serve_root(serve_status(), https_port) is None, timeout=5
    ):
        return
    found = inspect_serve_root(serve_status(), https_port)
    if found is None:
        return  # the conflict freed itself between the last poll and now
    scope, target = found
    raise AirshipError(
        f"Tailscale Serve already maps `/` on :{https_port} to {target} ({scope}). "
        "airship will not overwrite another service — if that mapping is stale, "
        f"clear it with `tailscale serve --https={https_port} off`; otherwise "
        f"stop the service that owns it, or pick a free port with "
        f"`--https-port <n>`."
    )


class ServeChild:
    """The foreground `tailscale serve` process plus its combined output.

    Output goes to an unlinked temp file rather than a pipe: nothing reads
    the child's output while airship serves (possibly for a long time), and
    a chatty child on a full pipe buffer would block forever."""

    def __init__(self, argv: list[str]) -> None:
        # No context manager (SIM115): the log must outlive this scope — it
        # lives as long as the child; the OS reclaims the unlinked file after.
        # errors="replace": output() runs on failure paths, where a stray
        # non-UTF-8 byte must not mask the real error with a decode crash.
        self.log = tempfile.TemporaryFile(  # noqa: SIM115
            mode="w+", encoding="utf-8", errors="replace"
        )
        self.proc = subprocess.Popen(
            argv, stdout=self.log, stderr=subprocess.STDOUT
        )

    def output(self) -> str:
        self.log.seek(0)
        return self.log.read().strip()


def start_serve(port: int, https_port: int = DEFAULT_HTTPS_PORT) -> ServeChild:
    """Run foreground `tailscale serve` and wait until `/` actually maps to our
    port (or fail with the serve CLI's own output)."""
    argv = ["tailscale", "serve"]
    if https_port != DEFAULT_HTTPS_PORT:
        argv.append(f"--https={https_port}")
    argv.append(str(port))
    child = ServeChild(argv)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child.proc.poll() is not None:
                raise AirshipError(
                    f"`{' '.join(argv)}` exited immediately:\n{child.output()}"
                )
            try:
                found = inspect_serve_root(serve_status(), https_port)
            except AirshipError:
                # Transient status failure mid-poll: keep polling — the
                # deadline is the fail-closed backstop.
                found = None
            if found and proxy_port(found[1]) == port:
                return child
            time.sleep(0.5)
        raise AirshipError("Timed out waiting for Tailscale Serve to map `/`.")
    except BaseException:  # incl. Ctrl-C — never orphan the serve child
        if child.proc.poll() is None:
            child.proc.terminate()
            try:
                child.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.proc.kill()
        child.log.close()
        raise


def probe_landing(url: str) -> None:
    """Confirm the HTTPS landing page answers before you walk to the phone.
    Uses curl (always present on macOS, trusts system roots — uv-managed
    Pythons may not have the system CA bundle)."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "15", url],
            capture_output=True, text=True, timeout=20, check=False,
        )
        code = out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        code = ""
    if code == "200":
        print(f"  Reachability: \033[32m✓\033[0m {url} answers from this Mac.")
    else:
        warn(
            f"Could not verify {url} from this Mac (HTTP {code or 'no response'}). "
            "If the phone can't load it either, check HTTPS certs and "
            "`tailscale serve status`."
        )


# --------------------------------------------------------------------------- #
# Artifact staging
# --------------------------------------------------------------------------- #


def build_manifest(base_url: str, meta: dict[str, str]) -> bytes:
    """Generate the Apple OTA manifest as XML plist (no string templates)."""
    manifest = {
        "items": [
            {
                "assets": [
                    {"kind": "software-package", "url": f"{base_url}/{IPA_NAME}"}
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
    return "itms-services://?action=download-manifest&url=" + quote(
        manifest_url, safe=""
    )


def build_index_html(base_url: str, meta: dict[str, str]) -> str:
    """Landing page. `&` in the href is HTML-escaped as `&amp;`."""
    href = itms_url(base_url).replace("&", "&amp;")
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
    # APFS clone: instant like a hardlink, but an immutable snapshot — a
    # rebuild of the source .ipa cannot corrupt an in-flight download.
    clone = subprocess.run(
        ["cp", "-c", str(ipa_path), str(staging / IPA_NAME)],
        capture_output=True,
        check=False,  # returncode is inspected: nonzero → non-APFS fallback
    )
    if clone.returncode != 0:
        shutil.copy2(ipa_path, staging / IPA_NAME)  # non-APFS fallback
    (staging / MANIFEST_NAME).write_bytes(build_manifest(base_url, meta))
    (staging / INDEX_NAME).write_text(build_index_html(base_url, meta))
    return staging


# --------------------------------------------------------------------------- #
# Local HTTP server
# --------------------------------------------------------------------------- #


class ServerState:
    """Shared between HTTP handler threads and the main wait loop."""

    def __init__(self, self_ips: frozenset[str] = frozenset()) -> None:
        self.self_ips = self_ips  # this node's IPs, to ignore local test fetches
        self.lock = threading.Lock()
        self.in_flight = 0
        self.last_request = time.monotonic()
        self.ipa_downloaded_at: float | None = None

    def note_request(self) -> None:
        self.last_request = time.monotonic()

    def note_ipa_downloaded(self) -> None:
        first = self.ipa_downloaded_at is None
        self.ipa_downloaded_at = time.monotonic()
        if first:
            ok(
                "IPA downloaded — the install is now running on the phone. "
                "Ctrl-C is safe from here."
            )

    def should_exit(self, now: float) -> bool:
        if self.ipa_downloaded_at is None or self.in_flight > 0:
            return False
        # Idle is measured from whichever happened last: the download finishing
        # (not starting) or any later request.
        return now - max(self.last_request, self.ipa_downloaded_at) >= IDLE_EXIT_GRACE


def make_handler(
    root: Path, state: ServerState
) -> type[http.server.SimpleHTTPRequestHandler]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def send_response(self, code, message=None):
            self._status = code
            super().send_response(code, message)

        def do_GET(self):
            with state.lock:
                state.in_flight += 1
            try:
                super().do_GET()
                # Reached only when the full body streamed without error.
                if self._is_phone_ipa_download():
                    state.note_ipa_downloaded()
            finally:
                with state.lock:
                    state.in_flight -= 1

        def _is_phone_ipa_download(self) -> bool:
            """A 200, fully-streamed GET of the IPA from another tailnet device.
            Tailscale Serve stamps X-Forwarded-For with the requester's tailnet
            IP; local verification fetches carry this node's own IP (or none)."""
            if self.path.split("?", 1)[0] != f"/{IPA_NAME}":
                return False
            if getattr(self, "_status", None) != 200:
                return False  # 304 / 404 responses send no body
            peer = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return bool(peer) and peer not in state.self_ips

        def log_request(self, code="-", size="-"):
            state.note_request()  # every handled request resets the idle clock
            super().log_request(code, size)

        def guess_type(self, path):
            for suffix, ctype in CONTENT_TYPES.items():
                if path.endswith(suffix):
                    return ctype
            return super().guess_type(path)

        def log_message(self, fmt, *args):
            sys.stderr.write("  [http] " + _printable(fmt % args) + "\n")

    return Handler


def start_server(
    root: Path, state: ServerState
) -> tuple[http.server.ThreadingHTTPServer, int]:
    """Bind 127.0.0.1 (Serve proxies from localhost; no LAN exposure needed).
    Prefer PREFERRED_PORT, fall back to an OS-assigned free port."""
    handler = make_handler(root, state)
    for port in (PREFERRED_PORT, 0):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            if port == 0:
                raise
            continue
    actual_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, actual_port


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(
    ipa_path: Path, stay: bool = False, https_port: int = DEFAULT_HTTPS_PORT
) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so cleanup runs
    meta = read_ipa_metadata(ipa_path)
    warn_on_signing(ipa_path, meta["embedded_profile"])

    base_url = tailscale_base_url(https_port)
    ensure_serve_root_free(https_port)

    staging: Path | None = None
    server: http.server.ThreadingHTTPServer | None = None
    serve: ServeChild | None = None
    try:
        staging = stage_artifacts(ipa_path, base_url, meta)
        state = ServerState(self_ips=tailscale_self_ips())
        server, port = start_server(staging, state)
        serve = start_serve(port, https_port)
        write_instance(serve.proc.pid)

        _print_handoff(meta, base_url, port)
        probe_landing(f"{base_url}/")
        if stay:
            print("\n  Serving — press Ctrl-C when the install finishes.")
        else:
            print(
                "\n  Serving — exits by itself once the phone has downloaded "
                f"the app and {int(IDLE_EXIT_GRACE)}s pass (or press Ctrl-C)."
            )

        while True:
            time.sleep(0.5)
            if serve.proc.poll() is not None:
                raise AirshipError(
                    f"tailscale serve exited unexpectedly.\n{serve.output()}"
                )
            if not stay and state.should_exit(time.monotonic()):
                ok("Done — cleaning up. Rerun airship if you need the link again.")
                break
    except KeyboardInterrupt:
        print("\n  Stopping.")
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # don't let ^C abort cleanup
        if serve is not None and serve.proc.poll() is None:
            serve.proc.terminate()
            try:
                serve.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                serve.proc.kill()
        if serve is not None:
            serve.log.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        INSTANCE_FILE.unlink(missing_ok=True)
    return 0


def _print_handoff(meta: dict[str, str], base_url: str, port: int) -> None:
    import segno

    landing = f"{base_url}/"
    title, bundle_id, version = (
        _printable(meta[k]) for k in ("title", "bundle_id", "version")
    )
    print()
    print(f"  \033[1m{title}\033[0m  {bundle_id}  v{version}")
    print()
    print("  Open this on your iPhone (Safari), then tap Install:")
    print(f"  \033[36m{landing}\033[0m")
    print()
    segno.make(landing, error="m").terminal(compact=True)
    print()
    print(f"  Tailscale IP: {tailscale_ip()}  (local server on 127.0.0.1:{port})")
    print(f"  itms link (debug): {itms_url(base_url)}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # banner must show through pipes/tee
    parser = argparse.ArgumentParser(
        description="Install an iOS .ipa onto your iPhone over the air via Tailscale."
    )
    parser.add_argument(
        "ipa",
        nargs="?",
        type=Path,
        help="Path to the .ipa (default: newest .ipa under the current directory)",
    )
    parser.add_argument(
        "--stay",
        action="store_true",
        help="Keep serving until Ctrl-C instead of auto-exiting after the install",
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=DEFAULT_HTTPS_PORT,
        metavar="N",
        help=(
            f"Tailscale Serve HTTPS port (default {DEFAULT_HTTPS_PORT}). Use a "
            "different port when something already owns `/` on the default, or "
            "when a service worker from another project has claimed that origin "
            "— a different port is a different origin, so it cannot intercept "
            "the install page."
        ),
    )
    args = parser.parse_args(argv)

    try:
        ipa = args.ipa
        if ipa is None:
            ipa = find_newest_ipa(Path.cwd())
            print(f"  Using newest .ipa: {ipa}")
        return run(ipa, stay=args.stay, https_port=args.https_port)
    except KeyboardInterrupt:
        print("\n  Stopping.")
        return 130
    except AirshipError as exc:
        print(f"\033[31m✗ {exc}\033[0m", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
