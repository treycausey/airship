#!/usr/bin/env python3
"""Fake `varlock` for the e2e suite.

Real airship.py's notify_ready() invokes `varlock run --path <dir> --filter
PUSHOVER_TOKEN,PUSHOVER_USER -- sh -c ...` to read Pushover credentials from
airship's own `.env.local` and forward them to curl. This fake NEVER reads
any `.env.local` (real or fake) and NEVER execs the wrapped command — it
just records that it was called (argv only, never file contents) to
AIRSHIP_TEST_FORBIDDEN_MARKER and exits non-zero. This is what makes it safe
to point the e2e suite at a real checkout with a real `.env.local`: this
fake intercepts the call before any credential ever gets read, so a real
Pushover push can never be sent by a test.
"""

from __future__ import annotations

import json
import os
import sys

MARKER = os.environ.get("AIRSHIP_TEST_FORBIDDEN_MARKER")


def main(argv: list[str]) -> int:
    if MARKER:
        with open(MARKER, "a") as f:
            f.write(json.dumps({"bin": "varlock", "argv": argv}) + "\n")
    sys.stderr.write(
        "fake varlock: refusing to run (e2e isolation) — "
        "see AIRSHIP_TEST_FORBIDDEN_MARKER\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
