"""End-to-end tests: a real airship.py subprocess, serving real HTTPS
(terminated by the fake tailscale TLS proxy in fake_tailscale.py), driven
with httpx. No browser, no real Tailscale, no real certs, no real instance
records, no real Pushover network access — see conftest.py for how each is
faked or isolated.

Run just these with: uv run --with pytest --with segno --with httpx \
    pytest -m e2e tests/
The default `pytest tests/` run excludes them (see pytest.ini).
"""

from __future__ import annotations

import json
import plistlib
import re
import socket
from urllib.parse import quote

import pytest

from conftest import BUNDLE_ID, BUNDLE_VERSION, APP_TITLE

pytestmark = pytest.mark.e2e


def test_install_page_is_served(airship_factory, fixture_ipa):
    inst = airship_factory(fixture_ipa)
    inst.wait_ready()
    resp = inst.client.get(f"{inst.base_url}/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Install" in body
    assert BUNDLE_ID in body
    assert APP_TITLE in body
    # The itms link must point at this server's own manifest, over https (the
    # manifest URL is percent-encoded inside the itms-services link). Built
    # independently here with urllib.parse.quote rather than by calling
    # airship.itms_url() — the point of this assertion is to catch a bug in
    # that function, which calling the same function to compute the expected
    # value could never do.
    manifest_url = f"{inst.base_url}/manifest.plist"
    expected_itms = (
        "itms-services://?action=download-manifest&amp;url="
        + quote(manifest_url, safe="")
    )
    assert expected_itms in body
    # probe_landing() ran a real curl against this real HTTPS endpoint (via
    # fake_curl.py's loopback passthrough) as part of the startup handoff
    # wait_ready() waited for; it must have found the real success path.
    assert re.search(r"Reachability:.*✓", inst.stdout()), (
        f"probe_landing did not report success:\n{inst.stdout()}"
    )


def test_manifest_has_correct_bundle_id_version_and_ipa_url(airship_factory, fixture_ipa):
    inst = airship_factory(fixture_ipa)
    inst.wait_ready()
    resp = inst.client.get(f"{inst.base_url}/manifest.plist")
    assert resp.status_code == 200
    manifest = plistlib.loads(resp.content)
    item = manifest["items"][0]
    md = item["metadata"]
    assert md["bundle-identifier"] == BUNDLE_ID
    assert md["bundle-version"] == BUNDLE_VERSION
    asset_url = item["assets"][0]["url"]
    # Manifest must point back at this same server, over HTTPS.
    assert asset_url == f"{inst.base_url}/app.ipa"
    assert asset_url.startswith("https://")


def test_ipa_downloads_byte_identical(airship_factory, fixture_ipa):
    inst = airship_factory(fixture_ipa)
    inst.wait_ready()
    resp = inst.client.get(f"{inst.base_url}/app.ipa")
    assert resp.status_code == 200
    assert resp.content == fixture_ipa.read_bytes()


def test_free_port_selection_skips_an_already_taken_port(airship_factory, fixture_ipa):
    # Pick a preferred port of our own (via AIRSHIP_PREFERRED_PORT — see
    # airship.py) and hold it open before airship ever tries to bind it, so
    # its local HTTP server is forced onto the OS-assigned fallback port.
    # Using our own port, rather than the real PREFERRED_PORT (4190), keeps
    # this deterministic regardless of whatever else is running on the host
    # (this Mac has real, live airships holding 4190/4443/4444 that must
    # never be touched).
    preferred = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    preferred.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    preferred.bind(("127.0.0.1", 0))
    preferred_port = preferred.getsockname()[1]
    preferred.listen(1)
    try:
        inst = airship_factory(
            fixture_ipa, extra_env={"AIRSHIP_PREFERRED_PORT": str(preferred_port)}
        )
        inst.wait_ready()  # only possible if the fallback port actually served
        match = re.search(r"local server on 127\.0\.0\.1:(\d+)", inst.stdout())
        assert match, f"no 'local server on 127.0.0.1:<port>' line in stdout:\n{inst.stdout()}"
        actual_port = int(match.group(1))
        assert actual_port != preferred_port, (
            "airship bound its own preferred (held-open) port instead of falling back"
        )
        # And the real HTTPS path still works end-to-end on the fallback port.
        resp = inst.client.get(f"{inst.base_url}/app.ipa")
        assert resp.status_code == 200
        assert resp.content == fixture_ipa.read_bytes()
    finally:
        preferred.close()


def test_instance_record_is_written_keyed_by_port_and_cleaned_up_on_shutdown(
    airship_factory, fixture_ipa
):
    inst = airship_factory(fixture_ipa)
    inst.wait_ready()

    assert inst.wait_instance_file(present=True), (
        f"{inst.instance_path} was never written while airship was serving"
    )
    record = json.loads(inst.instance_path.read_text())
    assert isinstance(record.get("pid"), int)
    assert isinstance(record.get("serve_pid"), int)
    # Keyed by port: no other port's record should exist in the same dir.
    other_records = [
        p for p in inst.instance_dir.glob("airship-instance-*.json")
        if p != inst.instance_path
    ]
    assert other_records == []

    code = inst.stop()
    assert code == 0
    assert inst.wait_instance_file(present=False), (
        f"{inst.instance_path} was not removed after clean shutdown"
    )


def test_pushover_never_reaches_the_real_network_even_with_a_real_looking_env_local(
    airship_factory, fixture_ipa, airship_copy_with_dummy_env
):
    """The main checkout (/Users/treycausey/dev/airship) has a REAL
    .env.local with real Pushover credentials. notify_ready() resolves its
    env dir from the running script's own directory, so if this suite ever
    ran airship.py from a copy sitting next to a `.env.local` — real or
    dummy — a real push would fire straight to Trey's phone unless something
    intercepts it upstream of any credential ever being read. This proves
    fake_varlock.py is that interception point, using a DUMMY .env.local
    (never the real one) to trigger the same code path.
    """
    inst = airship_factory(fixture_ipa, airship_path=airship_copy_with_dummy_env)
    inst.wait_ready()  # notify_ready has already run once "Serving —" prints

    calls = inst.forbidden_calls()
    varlock_calls = [c for c in calls if c["bin"] == "varlock"]
    assert len(varlock_calls) == 1, (
        f"expected exactly one intercepted varlock call, got: {calls}"
    )
    # The intercepted call is the pushover notification's — proof it's the
    # right call, without our fake ever touching PUSHOVER_TOKEN/PUSHOVER_USER
    # (it doesn't read the env file at all, real or dummy; see fake_varlock.py).
    assert "PUSHOVER_TOKEN,PUSHOVER_USER" in " ".join(varlock_calls[0]["argv"])
    # Defense in depth: even if varlock had NOT intercepted, fake curl refuses
    # anything that isn't a loopback URL — no call to api.pushover.net either.
    assert not any(c["bin"] == "curl" for c in calls), (
        f"a curl call escaped past the fake varlock: {calls}"
    )
    assert "Push notification failed" in inst.stdout()
    inst.stop()
