#!/usr/bin/env python3
"""Fake `curl` for the e2e suite.

airship.py shells out to curl in two places: probe_landing() (a real
reachability check against the install page it just served) and, inside
notify_ready()'s wrapped shell command, a POST to api.pushover.net (blocked
upstream by fake_varlock.py before it ever gets this far — this is defense
in depth). This fake tells the two apart by host:

  - a loopback URL (127.0.0.1 / localhost / ::1) is passed straight through
    to the REAL curl, so probe_landing() exercises its actual success path
    against the fake tailscale TLS proxy (see fake_tailscale.py). The real
    curl needs CURL_CA_BUNDLE pointed at the e2e self-signed cert to trust it
    (set by the test fixture; curl honors that env var natively).
  - anything else (api.pushover.net, or any other real host) is refused and
    recorded to AIRSHIP_TEST_FORBIDDEN_MARKER, never touching the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import urlparse

MARKER = os.environ.get("AIRSHIP_TEST_FORBIDDEN_MARKER")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
REAL_CURL_CANDIDATES = ("/usr/bin/curl", "/opt/homebrew/bin/curl", "/usr/local/bin/curl")


def _extract_url(argv: list[str]) -> str | None:
    for arg in reversed(argv):
        if arg.startswith("http://") or arg.startswith("https://"):
            return arg
    return None


def _mark(argv: list[str]) -> None:
    if MARKER:
        with open(MARKER, "a") as f:
            f.write(json.dumps({"bin": "curl", "argv": argv}) + "\n")


def main(argv: list[str]) -> int:
    url = _extract_url(argv)
    host = urlparse(url).hostname if url else None

    if host in LOOPBACK_HOSTS:
        real_curl = next((c for c in REAL_CURL_CANDIDATES if os.path.exists(c)), None)
        if real_curl is None:
            sys.stderr.write("fake curl: no real curl found to proxy a loopback request\n")
            return 1
        return subprocess.call([real_curl, *argv])

    _mark(argv)
    sys.stderr.write(
        f"fake curl: refusing non-loopback request to {url!r} (e2e isolation)\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
