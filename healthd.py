#!/usr/bin/env python3
"""Run the ingest server straight from a checkout.

This is what the always-on background job calls, so it must work with nothing
but a system Python: no virtualenv, no pip install, no shell profile.

    /usr/bin/python3 ~/.hermes/plugins/hermes-health/healthd.py serve --host 0.0.0.0 --port 8787
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hermes_health.cli import main  # noqa: E402  (after sys.path fix)

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv
    raise SystemExit(main(argv))
