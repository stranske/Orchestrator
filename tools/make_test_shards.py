#!/usr/bin/env python3
"""Balance deterministic module shards from JUnit timings (never verification counts).

Run after adding/removing test modules:
  python tools/make_test_shards.py junit.xml --shards 6
Untimed modules use the median measured module duration, and are named in the manifest.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def make_plan(junit: pathlib.Path, root: pathlib.Path, count: int) -> dict:
    if count < 1:
        raise ValueError("--shards must be positive")
    seconds: dict[str, float] = defaultdict(float)
    for case in ET.parse(junit).getroot().iter("testcase"):
        # pytest classnames may end in a test class. Match against collected module paths,
        # rather than assuming every classname ends at a module.
        parts = (case.get("classname") or "").split(".")
        for end in range(len(parts), 0, -1):
            module = "/".join(parts[:end]) + ".py"
            if (root / module).is_file():
                seconds[module] += float(case.get("time", "0"))
                break
    if not seconds:
        raise ValueError("JUnit contains no timings matching this checkout")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    modules = sorted(
        {
            line.split("::", 1)[0]
            for line in proc.stdout.splitlines()
            if "::" in line and line.split("::", 1)[0].endswith(".py")
        }
    )
    if len(modules) < count:
        raise ValueError("not enough collected modules for requested shards")
    default = statistics.median(seconds.values())
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for module in sorted(modules, key=lambda name: (-seconds.get(name, default), name)):
        index = min(range(count), key=lambda i: (loads[i], i))
        shards[index].append(module)
        loads[index] += seconds.get(module, default)
    return {
        "timing_source": junit.name,
        "estimated_test_seconds": [round(load, 3) for load in loads],
        "untimed_modules": sorted(set(modules) - seconds.keys()),
        "shards": [sorted(shard) for shard in shards],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=pathlib.Path)
    parser.add_argument("--shards", type=int, default=6)
    parser.add_argument(
        "--output", type=pathlib.Path, default=pathlib.Path("config/test-shards.json")
    )
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    args.output.write_text(json.dumps(make_plan(args.junit, root, args.shards), indent=2) + "\n")


if __name__ == "__main__":
    main()
