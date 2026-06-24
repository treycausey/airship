"""Tests for airship. Run with: uv run pytest

The Gambatte-fixture.ipa fixture is built from a real Tauri iOS build's
binary Info.plist and real embedded.mobileprovision (real-world data, not
synthetic), with a placeholder app binary to keep it small.
"""

from __future__ import annotations

import plistlib
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import airship  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "Gambatte-fixture.ipa"
BASE_URL = "https://example-mac.tail1234.ts.net"


# --------------------------------------------------------------------------- #
# Metadata extraction (real-world IPA)
# --------------------------------------------------------------------------- #


def test_reads_metadata_from_real_ipa():
    meta = airship.read_ipa_metadata(FIXTURE)
    assert meta["bundle_id"] == "com.gambatte.app"
    assert meta["version"] == "0.1.0"  # CFBundleVersion
    # No CFBundleDisplayName in this build → falls back to the .app stem.
    assert meta["title"] == "Gambatte"
    assert meta["embedded_profile"] == "Payload/Gambatte.app/embedded.mobileprovision"


def test_missing_ipa_raises_clear_error(tmp_path):
    with pytest.raises(airship.AirshipError, match="not found"):
        airship.read_ipa_metadata(tmp_path / "nope.ipa")


def test_non_zip_raises_clear_error(tmp_path):
    bogus = tmp_path / "fake.ipa"
    bogus.write_text("not a zip")
    with pytest.raises(airship.AirshipError, match="valid .ipa"):
        airship.read_ipa_metadata(bogus)


def test_zip_without_app_bundle_raises(tmp_path):
    empty = tmp_path / "empty.ipa"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("README.txt", "no app here")
    with pytest.raises(airship.AirshipError, match="No Payload"):
        airship.read_ipa_metadata(empty)


def test_missing_bundle_id_raises(tmp_path):
    bad = tmp_path / "bad.ipa"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("Payload/Foo.app/Info.plist", plistlib.dumps({"CFBundleName": "Foo"}))
    with pytest.raises(airship.AirshipError, match="CFBundleIdentifier"):
        airship.read_ipa_metadata(bad)


def test_version_falls_back_to_short_version(tmp_path):
    ipa = tmp_path / "v.ipa"
    info = {"CFBundleIdentifier": "a.b.c", "CFBundleShortVersionString": "9.9"}
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("Payload/Foo.app/Info.plist", plistlib.dumps(info))
    meta = airship.read_ipa_metadata(ipa)
    assert meta["version"] == "9.9"


# --------------------------------------------------------------------------- #
# Manifest + itms link + landing page
# --------------------------------------------------------------------------- #


def test_manifest_is_valid_xml_plist_with_correct_fields():
    meta = airship.read_ipa_metadata(FIXTURE)
    raw = airship.build_manifest(BASE_URL, meta)
    parsed = plistlib.loads(raw)
    item = parsed["items"][0]
    assert item["assets"][0]["kind"] == "software-package"
    assert item["assets"][0]["url"] == f"{BASE_URL}/app.ipa"
    md = item["metadata"]
    assert md["bundle-identifier"] == "com.gambatte.app"
    assert md["bundle-version"] == "0.1.0"
    assert md["kind"] == "software"
    assert md["title"] == "Gambatte"


def test_manifest_is_emitted_as_xml():
    meta = airship.read_ipa_metadata(FIXTURE)
    raw = airship.build_manifest(BASE_URL, meta)
    assert raw.lstrip().startswith(b"<?xml")


def test_itms_url_percent_encodes_nested_manifest_url():
    url = airship.itms_url(BASE_URL)
    assert url.startswith("itms-services://?action=download-manifest&url=")
    # the nested https URL must be percent-encoded (no raw :// in the tail)
    tail = url.split("url=", 1)[1]
    assert "%3A%2F%2F" in tail
    assert "://" not in tail


def test_index_html_escapes_ampersand_in_href():
    meta = airship.read_ipa_metadata(FIXTURE)
    html = airship.build_index_html(BASE_URL, meta)
    assert "download-manifest&amp;url=" in html
    assert "Gambatte" in html
    assert "com.gambatte.app" in html


# --------------------------------------------------------------------------- #
# Tailscale DNS name handling
# --------------------------------------------------------------------------- #


def test_base_url_strips_trailing_dot(monkeypatch):
    import subprocess

    class FakeRun:
        stdout = '{"Self": {"DNSName": "example-mac.tail1234.ts.net."}}'

    monkeypatch.setattr(airship.shutil, "which", lambda _: "/usr/local/bin/tailscale")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeRun())
    url = airship.tailscale_base_url()
    assert url == "https://example-mac.tail1234.ts.net"
    assert ".ts.net." not in url


def test_base_url_errors_when_tailscale_absent(monkeypatch):
    monkeypatch.setattr(airship.shutil, "which", lambda _: None)
    with pytest.raises(airship.AirshipError, match="tailscale"):
        airship.tailscale_base_url()


# --------------------------------------------------------------------------- #
# Staging + content types + port fallback
# --------------------------------------------------------------------------- #


def test_stage_artifacts_writes_url_safe_names():
    meta = airship.read_ipa_metadata(FIXTURE)
    staging = airship.stage_artifacts(FIXTURE, BASE_URL, meta)
    try:
        assert (staging / "app.ipa").exists()
        assert (staging / "manifest.plist").exists()
        assert (staging / "index.html").exists()
        # manifest parses and points at the staged ipa name
        parsed = plistlib.loads((staging / "manifest.plist").read_bytes())
        assert parsed["items"][0]["assets"][0]["url"].endswith("/app.ipa")
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)


def test_handler_sets_correct_content_types(tmp_path):
    handler_cls = airship.make_handler(tmp_path)
    # guess_type is a method; check the suffix mapping it relies on
    assert airship.CONTENT_TYPES[".ipa"] == "application/octet-stream"
    assert airship.CONTENT_TYPES[".plist"] == "text/xml"
    # the handler class is constructed rooted at tmp_path
    assert handler_cls is not None


def test_server_falls_back_when_preferred_port_busy(monkeypatch):
    import socket

    # Occupy the preferred port so airship must fall back.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", airship.PREFERRED_PORT))
    blocker.listen(1)
    try:
        server, port = airship.start_server(Path("/tmp"))
        try:
            assert port != airship.PREFERRED_PORT
            assert port > 0
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.shutdown()
    finally:
        blocker.close()
