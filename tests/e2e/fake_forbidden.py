#!/usr/bin/env python3
"""Fake `xcrun` / `security` for the e2e suite.

airship.py's signing preflight (warn_on_signing / _known_device_udids) can
shell out to `security cms -D` (decode embedded.mobileprovision) and `xcrun
devicectl` / `xcrun xctrace` (list paired devices). None of these should ever
run against the e2e fixture .ipa (it carries no embedded.mobileprovision), so
this fake just records the call to AIRSHIP_TEST_FORBIDDEN_MARKER and fails —
belt and suspenders in case a future code path starts calling them.

Installed twice under two names (xcrun, security); argv[0]'s basename picks
which one gets recorded.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MARKER = os.environ.get("AIRSHIP_TEST_FORBIDDEN_MARKER")


def main(argv: list[str]) -> int:
    bin_name = Path(sys.argv[0]).name
    if MARKER:
        with open(MARKER, "a") as f:
            f.write(json.dumps({"bin": bin_name, "argv": argv}) + "\n")
    sys.stderr.write(f"fake {bin_name}: refusing to run (e2e isolation)\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
