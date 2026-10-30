#!/usr/bin/env python3
"""Create or verify a narrow create-only closer publisher closure package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import closer_publisher_closure as closure


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    for option in ("source-root", "controller-data-root", "packages-root"):
        create.add_argument("--" + option, type=Path, required=True)
    create.add_argument("--source-snapshot-receipt", type=Path, required=True)
    create.add_argument("--source-capture-tool", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        print(
            json.dumps(
                closure.verify_package_directory(args.package_root.resolve()),
                sort_keys=True,
            )
        )
        return 0
    descriptor = closure.publish_package(
        args.source_root,
        args.packages_root,
        controller_data_root=args.controller_data_root,
        source_snapshot_receipt=args.source_snapshot_receipt,
        source_capture_tool=args.source_capture_tool,
    )
    print(json.dumps(descriptor, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
