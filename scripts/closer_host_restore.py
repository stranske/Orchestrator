#!/usr/bin/env python3
"""CLI entrypoint for the bounded host-only closer wrapper restore."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from closer_host_restore_contract import main

if __name__ == "__main__":
    raise SystemExit(main())
