#!/usr/bin/env python3
"""Replay the original five #771 tests in a supplied isolated historical checkout.

Requires pytest in the selected interpreter. Never operates on this script's checkout.
An incomplete replay leaves diagnostic artifacts but never a success receipt.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HEAD = "18ad97a054dab7f6039dd42c8131bee04267748b"
TARGET = "src/feedback.py"
TEST_MODULE = "tests/test_feedback_registry_queries.py"
BLOBS = {
    TARGET: "5cdb301107b1e8167b18578b0842917c14a5dd97",
    TEST_MODULE: "e4f448e592e65b9625f393c783d6fae447e0b924",
}
# Each anchor is unique in the original source. The last mutation reproduces
# the double-counting bug fixed by PR #772, beyond merely breaking validation.
MUTATIONS = {
    "test_run_queries_keep_pending_and_mode_scopes_distinct": (
        "def latest_run_id_for_target(target: str, mode: str | None = None) -> str | None:\n",
        "    return None\n",
    ),
    "test_snapshot_json_exports_initialized_tables_to_the_requested_path": (
        "def snapshot_json(path=None) -> dict:\n",
        "    return {}\n",
    ),
    "test_evidence_registry_normalizes_citations_and_retires_unused_rows": (
        "def prune_dead_evidence(min_influence: int = 1) -> list[str]:\n",
        "    return []\n",
    ),
    "test_evidence_proposals_and_approval_require_recurrence_then_reactivate": (
        "        eligible = recurrence >= min_recurrence\n",
        "        eligible = True\n",
    ),
    "test_role_selector_metrics_preserve_no_match_and_rejection_signals": (
        '            for key in ("matched", "disagreement"):\n',
        '            for key in ("matched", "invoked", "disagreement"):\n',
    ),
}


def blob_hash(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def mutate(source: str, test_id: str) -> str:
    anchor, replacement = MUTATIONS[test_id]
    if source.count(anchor) != 1:
        raise ValueError(f"Expected exactly one mutation anchor for {test_id}")
    if anchor.startswith("def "):
        replacement = anchor + replacement
    return source.replace(anchor, replacement, 1)


def check_checkout(checkout: Path) -> None:
    if checkout == Path(__file__).resolve().parents[1]:
        raise ValueError("Supply an isolated historical checkout, separate from this repository")
    for args, expected in ((["rev-parse", "HEAD"], HEAD), (["status", "--porcelain"], "")):
        result = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip() != expected:
            raise ValueError(f"Historical checkout failed preflight: git {' '.join(args)}")
    for relative, expected in BLOBS.items():
        if blob_hash((checkout / relative).read_bytes()) != expected:
            raise ValueError(f"Historical blob mismatch: {relative}")


def verify_result(xml: Path, expected_ids: list[str], failed: bool) -> None:
    cases = ET.parse(xml).getroot().findall(".//testcase")
    if sorted(case.attrib["name"] for case in cases) != sorted(expected_ids):
        raise ValueError("Pytest did not execute exactly the requested historical test IDs")
    for case in cases:
        if case.find("error") is not None or case.find("skipped") is not None:
            raise ValueError("Pytest setup errors and skips are not mutation evidence")
        if (case.find("failure") is not None) != failed:
            raise ValueError("Pytest outcome does not match the expected mutation result")


def run_pytest(
    checkout: Path,
    output: Path,
    label: str,
    test_ids: list[str],
    python: str,
    failed: bool = False,
) -> dict:
    xml = output / f"{label}.xml"
    nodes = [f"{TEST_MODULE}::{test_id}" for test_id in test_ids]
    if test_ids == list(MUTATIONS):
        nodes = [TEST_MODULE]
    command = [python, "-m", "pytest", "-q", *nodes, "-m", "not slow", f"--junitxml={xml}"]
    # Retain complete combined output, even when an interrupted run cannot be evidence.
    # A fresh prefix prevents stale bytecode after same-size, same-timestamp edits.
    with tempfile.TemporaryDirectory(prefix="issue771-bytecode-") as cache, (
        output / f"{label}.log"
    ).open("wb") as log:
        result = subprocess.run(
            command,
            cwd=checkout,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            env={**os.environ, "PYTHONPYCACHEPREFIX": cache},
        )
    record = {"command": command, "exit_status": result.returncode}
    (output / f"{label}.json").write_text(json.dumps(record, indent=2) + "\n")
    if result.returncode != (1 if failed else 0):
        raise ValueError(f"Unexpected pytest exit status for {label}: {result.returncode}")
    verify_result(xml, test_ids, failed)
    return record


def capture(checkout: Path, output: Path, python: str = sys.executable) -> None:
    checkout, output = checkout.resolve(), output.resolve()
    if checkout == output or checkout in output.parents:
        raise ValueError("Evidence output must be outside the historical checkout")
    check_checkout(checkout)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use an empty evidence directory; prior receipts must not be overwritten")
    target = checkout / TARGET
    original = target.read_bytes()
    source = original.decode("utf-8")
    receipt = {"head": HEAD, "checkout": str(checkout), "blobs": BLOBS, "mutations": {}}
    receipt["baseline"] = run_pytest(checkout, output, "original-five", list(MUTATIONS), python)
    for test_id in MUTATIONS:
        changed = mutate(source, test_id)
        patch = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                changed.splitlines(keepends=True),
                fromfile=f"a/{TARGET}",
                tofile=f"b/{TARGET}",
            )
        )
        (output / f"{test_id}.patch").write_text(patch)
        try:
            target.write_bytes(changed.encode("utf-8"))
            failure = run_pytest(checkout, output, f"{test_id}-failure", [test_id], python, True)
        finally:
            target.write_bytes(original)
        restored = run_pytest(checkout, output, f"{test_id}-restored", [test_id], python)
        if target.read_bytes() != original:
            raise ValueError("Restored source differs from the historical blob")
        receipt["mutations"][test_id] = {"failure": failure, "restored": restored}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    capture(args.checkout, args.output, args.python)


if __name__ == "__main__":
    main()
