"""Tests for airship. Run with: uv run pytest

The Gambatte-fixture.ipa fixture carries a real Tauri iOS build's binary
Info.plist (real-world binary-plist parsing coverage) and a placeholder app
binary to keep it small. Its embedded.mobileprovision is synthetic and
unsigned: a real profile is CMS-signed and embeds the developer's identity
and device UDIDs, which do not belong in a public repo.
"""

from __future__ import annotations

import datetime
import os
import plistlib
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import airship

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


def test_non_dict_info_plist_raises_clear_error(tmp_path):
    # A valid plist whose top level is an array must be a clean AirshipError,
    # not an AttributeError traceback.
    bad = tmp_path / "array.ipa"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("Payload/Foo.app/Info.plist", plistlib.dumps(["not", "a", "dict"]))
    with pytest.raises(airship.AirshipError, match="dictionary"):
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
    handler_cls = airship.make_handler(tmp_path, airship.ServerState())
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
        server, port = airship.start_server(Path("/tmp"), airship.ServerState())
        try:
            assert port != airship.PREFERRED_PORT
            assert port > 0
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.shutdown()
    finally:
        blocker.close()


# --------------------------------------------------------------------------- #
# Staging: snapshot semantics (clone/copy, never a shared inode)
# --------------------------------------------------------------------------- #


@pytest.fixture
def staged():
    meta = airship.read_ipa_metadata(FIXTURE)
    staging = airship.stage_artifacts(FIXTURE, BASE_URL, meta)
    yield staging
    shutil.rmtree(staging, ignore_errors=True)


def test_stage_snapshots_ipa_without_sharing_inode(staged):
    # A rebuild of the source .ipa must not be able to corrupt what we serve:
    # the staged copy is a clone/copy (own inode), not a hardlink.
    src, dst = FIXTURE, staged / "app.ipa"
    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_ino != src.stat().st_ino


# --------------------------------------------------------------------------- #
# Serve-root inspection (Web + Foreground, scoped to :443)
# --------------------------------------------------------------------------- #


def test_inspect_serve_root_empty_config():
    assert airship.inspect_serve_root({}) is None
    assert airship.inspect_serve_root({"Web": {}}) is None


def test_inspect_serve_root_finds_background_mapping():
    cfg = {
        "Web": {
            "host.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:59999"}}}
        }
    }
    assert airship.inspect_serve_root(cfg) == ("background", "http://127.0.0.1:59999")


def test_inspect_serve_root_finds_foreground_session():
    # Shape captured from `tailscale serve status --json` (v1.98.8) while a
    # foreground `tailscale serve <port>` was running.
    cfg = {
        "Foreground": {
            "212a6bcdde35ed99": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "host.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:58888"}}
                    }
                },
            }
        }
    }
    assert airship.inspect_serve_root(cfg) == ("foreground", "http://127.0.0.1:58888")


def test_inspect_serve_root_ignores_non_root_handlers():
    cfg = {
        "Web": {
            "host.ts.net:443": {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:1"}}}
        }
    }
    assert airship.inspect_serve_root(cfg) is None


def test_inspect_serve_root_ignores_other_ports_by_default():
    # A mapping on another HTTPS port never conflicts with airship on its
    # default port, and must not block or be touched.
    cfg = {
        "Web": {
            "host.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}
        }
    }
    assert airship.inspect_serve_root(cfg) is None


def test_proxy_port_parsing():
    assert airship.proxy_port("http://127.0.0.1:4190") == 4190
    assert airship.proxy_port("http://localhost:3000") == 3000
    assert airship.proxy_port("/some/static/path") is None


# --------------------------------------------------------------------------- #
# Instance-file ownership and takeover / recovery branches
# --------------------------------------------------------------------------- #


@pytest.fixture
def instance_file(monkeypatch, tmp_path):
    path = tmp_path / "airship-instance.json"
    monkeypatch.setattr(airship, "INSTANCE_FILE", path)
    # Collapse the wait loops: evaluate the predicate once, no sleeping.
    monkeypatch.setattr(
        airship, "_wait_until", lambda pred, timeout, interval=0.5: pred()
    )
    return path


def test_instance_file_round_trip(instance_file):
    airship.write_instance(4243)
    assert airship.read_instance() == {"pid": os.getpid(), "serve_pid": 4243}


def test_ensure_root_free_when_nothing_running(instance_file, monkeypatch):
    monkeypatch.setattr(airship, "serve_status", dict)
    airship.ensure_serve_root_free()  # must not raise


def test_ensure_root_refuses_foreign_service(instance_file, monkeypatch):
    cfg = {
        "Web": {
            "h.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}
        }
    }
    monkeypatch.setattr(airship, "serve_status", lambda: cfg)
    killed = []
    monkeypatch.setattr(airship, "_terminate_pid", killed.append)
    with pytest.raises(airship.AirshipError, match="not overwrite"):
        airship.ensure_serve_root_free()
    assert killed == []  # never touches processes it cannot prove are airship's


def test_ensure_root_takes_over_previous_airship(instance_file, monkeypatch):
    instance_file.write_text('{"pid": 4242, "serve_pid": 4243}')
    alive = {4242: "python /Users/me/dev/airship/airship.py app.ipa"}
    monkeypatch.setattr(airship, "_pid_command", lambda pid: alive.get(pid))
    killed = []
    monkeypatch.setattr(
        airship, "_terminate_pid", lambda pid: (killed.append(pid), alive.pop(pid, None))
    )
    monkeypatch.setattr(airship, "serve_status", dict)
    airship.ensure_serve_root_free()
    assert killed == [4242]
    assert not instance_file.exists()


def test_ensure_root_kills_orphaned_serve_child(instance_file, monkeypatch):
    instance_file.write_text('{"pid": 4242, "serve_pid": 4243}')
    alive = {4243: "tailscale serve 4190"}  # airship pid gone, serve child orphaned
    monkeypatch.setattr(airship, "_pid_command", lambda pid: alive.get(pid))
    killed = []
    monkeypatch.setattr(
        airship, "_terminate_pid", lambda pid: (killed.append(pid), alive.pop(pid, None))
    )
    monkeypatch.setattr(airship, "serve_status", dict)
    airship.ensure_serve_root_free()
    assert killed == [4243]
    assert not instance_file.exists()


def test_ensure_root_returns_cleanly_when_mapping_frees_after_wait(
    instance_file, monkeypatch
):
    # The conflict present during the wait can vanish before the re-inspect
    # that names it; that must read as "root is free", not a crash.
    cfg = {
        "Web": {
            "h.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}
        }
    }
    statuses = iter([cfg])
    monkeypatch.setattr(airship, "serve_status", lambda: next(statuses, {}))
    airship.ensure_serve_root_free()  # must not raise


def test_ensure_root_ignores_stale_instance_with_reused_pids(instance_file, monkeypatch):
    instance_file.write_text('{"pid": 1, "serve_pid": 2}')
    # Both pids now belong to unrelated processes.
    monkeypatch.setattr(airship, "_pid_command", lambda pid: "/sbin/launchd")
    killed = []
    monkeypatch.setattr(airship, "_terminate_pid", killed.append)
    monkeypatch.setattr(airship, "serve_status", dict)
    airship.ensure_serve_root_free()
    assert killed == []
    assert not instance_file.exists()


# --------------------------------------------------------------------------- #
# No-arg IPA discovery
# --------------------------------------------------------------------------- #


def test_find_newest_ipa_picks_most_recent(tmp_path):
    old = tmp_path / "build" / "old.ipa"
    new = tmp_path / "new.ipa"
    old.parent.mkdir()
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert airship.find_newest_ipa(tmp_path) == new


def test_find_newest_ipa_skips_hidden_and_node_modules(tmp_path):
    hidden = tmp_path / ".git" / "x.ipa"
    nm = tmp_path / "node_modules" / "y.ipa"
    real = tmp_path / "app.ipa"
    hidden.parent.mkdir()
    nm.parent.mkdir()
    hidden.write_bytes(b"h")
    nm.write_bytes(b"n")
    real.write_bytes(b"r")
    os.utime(real, (1, 1))  # oldest, but the only eligible one
    assert airship.find_newest_ipa(tmp_path) == real


def test_find_newest_ipa_errors_when_none(tmp_path):
    with pytest.raises(airship.AirshipError, match="No .ipa"):
        airship.find_newest_ipa(tmp_path)


# --------------------------------------------------------------------------- #
# Download tracking, landing page at /, and idle auto-exit
# --------------------------------------------------------------------------- #

SELF_IP = "100.64.0.5"
PHONE_IP = "100.64.0.7"


def _get(port: int, path: str, **headers) -> bytes:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def test_server_marks_download_only_for_remote_full_fetch(staged):
    state = airship.ServerState(self_ips=frozenset({SELF_IP}))
    server, port = airship.start_server(staged, state)
    try:
        assert b"Install" in _get(port, "/")  # index.html served at /
        # Local fetches (no X-Forwarded-For, or our own IP via the proxy) are
        # verification traffic and must not arm the auto-exit timer.
        _get(port, "/app.ipa")
        _get(port, "/app.ipa", **{"X-Forwarded-For": SELF_IP})
        assert state.ipa_downloaded_at is None
        # A 304 conditional response sends no body and must not count.
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(
                port,
                "/app.ipa",
                **{
                    "X-Forwarded-For": PHONE_IP,
                    "If-Modified-Since": "Fri, 01 Jan 2038 00:00:00 GMT",
                },
            )
        assert exc.value.code == 304
        assert state.ipa_downloaded_at is None
        # A full 200 fetch from another tailnet device is the phone.
        _get(port, "/app.ipa", **{"X-Forwarded-For": PHONE_IP})
        assert state.ipa_downloaded_at is not None
    finally:
        server.shutdown()


def test_should_exit_semantics():
    state = airship.ServerState()
    state.last_request = 100.0
    assert state.should_exit(1e9) is False  # never downloaded → never auto-exit
    # Idle is measured from download completion, even when the transfer took
    # longer than the grace period (last_request stamps at request start).
    state.ipa_downloaded_at = 200.0
    assert state.should_exit(200.0 + airship.IDLE_EXIT_GRACE - 1) is False
    assert state.should_exit(200.0 + airship.IDLE_EXIT_GRACE + 1) is True
    # Never exit while a transfer is in flight, no matter how idle.
    state.in_flight = 1
    assert state.should_exit(1e9) is False
    state.in_flight = 0
    # A later request resets the idle clock.
    state.last_request = 300.0
    assert state.should_exit(300.0 + airship.IDLE_EXIT_GRACE - 1) is False


def test_find_newest_ipa_refuses_home_and_root():
    with pytest.raises(airship.AirshipError, match="project directory"):
        airship.find_newest_ipa(Path.home())
    with pytest.raises(airship.AirshipError, match="project directory"):
        airship.find_newest_ipa(Path("/"))


# --- --https-port: a different port is a different origin ----------------- #


def test_inspect_serve_root_matches_the_requested_port():
    # The same config that is invisible on 443 IS the conflict on 8444.
    cfg = {
        "Web": {
            "host.ts.net:8444": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}
        }
    }
    assert airship.inspect_serve_root(cfg) is None
    assert airship.inspect_serve_root(cfg, 8444) == (
        "background",
        "http://127.0.0.1:3000",
    )


def test_inspect_serve_root_foreground_honours_the_port():
    cfg = {
        "Foreground": {
            "abc": {
                "Web": {
                    "host.ts.net:8444": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:4190"}}
                    }
                }
            }
        }
    }
    assert airship.inspect_serve_root(cfg, 8444) == (
        "foreground",
        "http://127.0.0.1:4190",
    )
    assert airship.inspect_serve_root(cfg) is None


def _fake_dns(monkeypatch):
    import subprocess

    class FakeRun:
        stdout = '{"Self": {"DNSName": "example-mac.tail1234.ts.net."}}'

    monkeypatch.setattr(airship.shutil, "which", lambda _: "/usr/local/bin/tailscale")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeRun())


def test_base_url_omits_the_default_port(monkeypatch):
    _fake_dns(monkeypatch)
    assert airship.tailscale_base_url() == "https://example-mac.tail1234.ts.net"


def test_base_url_appends_a_non_default_port(monkeypatch):
    # The port has to reach the URL, or the manifest and landing page would
    # advertise an origin nothing is listening on.
    _fake_dns(monkeypatch)
    assert (
        airship.tailscale_base_url(8444)
        == "https://example-mac.tail1234.ts.net:8444"
    )


# --------------------------------------------------------------------------- #
# Signing preflight (synthetic fixture profile; CMS decode bypassed)
# --------------------------------------------------------------------------- #

PROFILE_MEMBER = "Payload/Gambatte.app/embedded.mobileprovision"
KNOWN_IPHONE = "00008120-000A11BB22CC33DD"  # in the fixture's ProvisionedDevices


@pytest.fixture
def warnings(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(airship, "warn", seen.append)
    return seen


def _fixture_profile() -> dict:
    with zipfile.ZipFile(FIXTURE) as zf:
        return plistlib.loads(zf.read(PROFILE_MEMBER))


def _preflight(monkeypatch, profile: dict, known: frozenset[str]) -> None:
    monkeypatch.setattr(airship, "_decode_cms_plist", lambda raw: profile)
    monkeypatch.setattr(airship, "_known_device_udids", lambda: known)
    airship.warn_on_signing(FIXTURE, PROFILE_MEMBER)


def test_signing_warns_on_expired_profile(warnings, monkeypatch):
    profile = _fixture_profile()
    profile["ExpirationDate"] = datetime.datetime(
        2020, 1, 1, tzinfo=datetime.timezone.utc
    )
    _preflight(monkeypatch, profile, frozenset())
    assert any("EXPIRED" in w for w in warnings)


def test_signing_warns_when_profile_has_no_devices(warnings, monkeypatch):
    profile = _fixture_profile()
    del profile["ProvisionedDevices"]
    _preflight(monkeypatch, profile, frozenset({KNOWN_IPHONE}))
    assert any("ProvisionedDevices" in w for w in warnings)


def test_signing_warns_when_no_known_device_is_provisioned(warnings, monkeypatch):
    _preflight(
        monkeypatch, _fixture_profile(), frozenset({"00008888-DEADBEEF00000001"})
    )
    assert any("install will likely fail" in w for w in warnings)


def test_signing_quiet_when_a_known_device_is_provisioned(warnings, monkeypatch):
    _preflight(monkeypatch, _fixture_profile(), frozenset({KNOWN_IPHONE}))
    assert warnings == []


def test_signing_quiet_for_enterprise_profile(warnings, monkeypatch):
    # ProvisionsAllDevices (enterprise in-house) profiles have no
    # ProvisionedDevices yet Apple supports OTA install for them — no warning.
    profile = _fixture_profile()
    del profile["ProvisionedDevices"]
    profile["ProvisionsAllDevices"] = True
    _preflight(monkeypatch, profile, frozenset({KNOWN_IPHONE}))
    assert warnings == []


def test_signing_quiet_when_no_devices_are_known(warnings, monkeypatch):
    # No discoverable devices → the UDID check is skipped, not a false alarm.
    _preflight(monkeypatch, _fixture_profile(), frozenset())
    assert warnings == []


def test_decode_cms_plist_rejects_unsigned_input():
    # The fixture's profile is intentionally NOT CMS-signed; the strict
    # decoder must return None for it rather than crash or half-parse.
    with zipfile.ZipFile(FIXTURE) as zf:
        raw = zf.read(PROFILE_MEMBER)
    assert airship._decode_cms_plist(raw) is None


# --------------------------------------------------------------------------- #
# Known-device UDID discovery (devicectl JSON primary, xctrace text fallback)
# --------------------------------------------------------------------------- #

# Shape captured from `xcrun devicectl list devices --json-output` (Xcode 26)
# on a real Mac; names and identifiers replaced.
DEVICECTL_JSON = {
    "result": {
        "devices": [
            {
                "deviceProperties": {"name": "Living Room"},
                "hardwareProperties": {
                    "udid": "00008110-000E44FF55AA66EE",
                    "platform": "tvOS",
                    "deviceType": "appleTV",
                },
            },
            {
                "deviceProperties": {"name": "My iPhone"},
                "hardwareProperties": {
                    "udid": KNOWN_IPHONE,
                    "platform": "iOS",
                    "deviceType": "iPhone",
                },
            },
            {
                "deviceProperties": {"name": "My iPad"},
                "hardwareProperties": {
                    "udid": "00008132-000B77CC88DD99EE",
                    "platform": "iOS",
                    "deviceType": "iPad",
                },
            },
            {"deviceProperties": {"name": "half-paired, no hardware info"}},
        ]
    }
}


def test_devicectl_udids_keeps_ios_devices_only():
    udids = airship.parse_devicectl_udids(DEVICECTL_JSON)
    assert udids == {KNOWN_IPHONE, "00008132-000B77CC88DD99EE"}


def test_devicectl_udids_tolerates_empty_and_malformed():
    assert airship.parse_devicectl_udids({}) == frozenset()
    assert airship.parse_devicectl_udids({"result": {}}) == frozenset()
    assert airship.parse_devicectl_udids({"result": {"devices": [{}]}}) == frozenset()


# Shape captured from `xcrun xctrace list devices` (Xcode 26) on a real Mac;
# names and identifiers replaced. Macs and simulators carry standard 5-group
# UUIDs; iOS-family hardware carries 8-16 UDIDs (legacy devices: 40 hex).
XCTRACE_OUTPUT = """\
== Devices ==
My Mac Studio (BAA35997-5DC4-573D-8E83-C2385813B50A)

== Devices Offline ==
Living Room (26.4) (00008110-000E44FF55AA66EE)
My iPhone (27.0) (00008120-000A11BB22CC33DD)
Old iPhone (12.5) (abcdef0123456789abcdef0123456789abcdef01)

== Simulators ==
iPhone 17 Simulator (26.0) (E83FD08E-32A4-401D-939C-771FDCD32A1A)
"""


def test_xctrace_udids_finds_hardware_and_skips_macs_and_simulators():
    assert airship.parse_xctrace_udids(XCTRACE_OUTPUT) == {
        "00008110-000E44FF55AA66EE",
        KNOWN_IPHONE,
        "abcdef0123456789abcdef0123456789abcdef01",
    }


def test_xctrace_udids_empty_output():
    assert airship.parse_xctrace_udids("") == frozenset()


def test_known_udids_trusts_an_empty_devicectl_registry(monkeypatch, tmp_path):
    # devicectl answering "no iOS devices" is authoritative: no xctrace
    # second opinion (which would cost another subprocess and could smuggle
    # in non-iOS hardware).
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        Path(argv[argv.index("--json-output") + 1]).write_text(
            '{"result": {"devices": []}}'
        )
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(airship.subprocess, "run", fake_run)
    assert airship._known_device_udids() == frozenset()
    assert all("xctrace" not in argv for argv in calls)


def test_known_udids_falls_back_to_xctrace_when_devicectl_unusable(monkeypatch):
    def fake_run(argv, **kwargs):
        if "devicectl" in argv:
            raise FileNotFoundError("xcrun")  # e.g. no Xcode CLT devicectl
        return type("R", (), {"stdout": XCTRACE_OUTPUT})()

    monkeypatch.setattr(airship.subprocess, "run", fake_run)
    assert KNOWN_IPHONE in airship._known_device_udids()


def test_known_udids_treats_error_envelope_as_unusable(monkeypatch):
    # devicectl writing parseable JSON WITHOUT a result envelope (an error
    # report) must fall back to xctrace, not read as "zero devices".
    def fake_run(argv, **kwargs):
        if "devicectl" in argv:
            Path(argv[argv.index("--json-output") + 1]).write_text(
                '{"error": {"code": 1}}'
            )
            return type("R", (), {"stdout": ""})()
        return type("R", (), {"stdout": XCTRACE_OUTPUT})()

    monkeypatch.setattr(airship.subprocess, "run", fake_run)
    assert KNOWN_IPHONE in airship._known_device_udids()


# --------------------------------------------------------------------------- #
# serve_status fails closed
# --------------------------------------------------------------------------- #


def test_serve_status_raises_when_status_unreadable(monkeypatch):
    # An unreadable status must never read as "nothing is mapped" — that is
    # the answer that authorizes taking over `/`.
    import subprocess

    def fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="tailscaled hiccup")

    monkeypatch.setattr(airship.subprocess, "run", fake_run)
    with pytest.raises(airship.AirshipError, match="refusing to guess"):
        airship.serve_status()


def test_start_serve_passes_https_flag_only_when_non_default(monkeypatch):
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeProc()

    monkeypatch.setattr(airship.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        airship,
        "inspect_serve_root",
        lambda cfg, port=443: ("foreground", "http://127.0.0.1:4190"),
    )
    monkeypatch.setattr(airship, "serve_status", dict)

    airship.start_serve(4190)
    assert calls[-1] == ["tailscale", "serve", "4190"]

    airship.start_serve(4190, 8444)
    assert calls[-1] == ["tailscale", "serve", "--https=8444", "4190"]
