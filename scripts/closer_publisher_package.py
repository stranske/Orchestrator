#!/usr/bin/env python3
"""Create or verify a narrow create-only closer publisher closure package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    verify.add_argument("--descriptor", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        print(json.dumps(closure.validate(json.loads(args.descriptor.read_text())), sort_keys=True))
        return 0
    args.packages_root.mkdir(parents=True, exist_ok=True)
    staging = args.packages_root / ".staging"
    staging.mkdir(exist_ok=True)
    seed = "|".join(
        map(
            str,
            (
                args.source_root.resolve(),
                args.controller_data_root.resolve(),
                args.source_snapshot_receipt.resolve(),
                args.source_capture_tool.resolve(),
            ),
        )
    )
    temporary = staging / hashlib.sha256(seed.encode()).hexdigest()
    descriptor = closure.capture(
        args.source_root,
        temporary,
        controller_data_root=args.controller_data_root,
        source_snapshot_receipt=args.source_snapshot_receipt,
        source_capture_tool=args.source_capture_tool,
    )
    package_id = hashlib.sha256(
        json.dumps(
            {k: v for k, v in descriptor.items() if k != "root"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    final = args.packages_root / package_id
    if final.exists():
        raise SystemExit("content-addressed package already exists")
    os.replace(temporary, final)
    descriptor["root"] = str(final.resolve())
    (final / "publisher-closure.json").write_text(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n"
    )
    print(json.dumps(closure.validate(descriptor), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
