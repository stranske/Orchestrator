#!/usr/bin/env python3
"""Stable CLI for closer release preflight and verified installation."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from closer_release_preflight import (  # noqa: E402
    DEFAULT_PYTHON,
    DEFAULT_WORKERS,
    PreflightError,
    ReleasePreflight,
    checked_environment,
    error_receipt,
    install_after_preflight,
    selftest,
)


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("mode", choices=("preflight", "install"))
    argument_parser.add_argument("--source", required=True, type=Path)
    argument_parser.add_argument("--expected-source-sha", required=True)
    argument_parser.add_argument("--mirror", required=True, type=Path)
    argument_parser.add_argument("--core", required=True, type=Path)
    argument_parser.add_argument("--expected-core-sha", required=True)
    argument_parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    argument_parser.add_argument("--journal", required=True, type=Path)
    argument_parser.add_argument("--owner", required=True)
    argument_parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    argument_parser.add_argument("--run-id")
    argument_parser.add_argument("--timeout", type=float, default=3600)
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv == ["--selftest"]:
        print(json.dumps(selftest(), sort_keys=True))
        return 0
    args = parser().parse_args(argv)
    config = ReleasePreflight(
        source=args.source,
        expected_source_sha=args.expected_source_sha,
        mirror=args.mirror,
        core=args.core,
        expected_core_sha=args.expected_core_sha,
        python=args.python,
        journal=args.journal,
        owner=args.owner,
        workers=args.workers,
        run_id=args.run_id,
        timeout=args.timeout,
    )
    started = time.monotonic()
    try:
        if args.mode == "preflight":
            _checked, _environment, receipt = checked_environment(config)
            print(json.dumps(receipt, sort_keys=True))
        else:
            receipt, result = install_after_preflight(config)
            installed = result.get("stage") == "installed"
            payload = {"ok": installed, "preflight": receipt, "release": result}
            if not installed:
                payload["error"] = {
                    "code": "release_not_installed",
                    "message": "existing verified release path did not reach installed",
                }
            print(json.dumps(payload, sort_keys=True))
            if not installed:
                return 3
    except PreflightError as exc:
        print(json.dumps(error_receipt(exc, time.monotonic() - started), sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
