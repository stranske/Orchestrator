#!/usr/bin/env python3
"""Run #1154's named production mutations, one node and restoration at a time.

Use the selected repository Python, otherwise /opt/anaconda3/bin/python3.
Default: all 76 cases. --case 01 reruns just one pair.
Receipts include exact source diffs, commands, assertions, and raw pytest output.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_MODULES = [
    "tests/test_issue1154_ranked_coverage.py",
    "tests/test_issue1154_tick_boundaries.py",
]
CASES = Path(__file__).with_name("mutation_cases_1154.json")


def collect_declared_nodes() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *TEST_MODULES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError("pytest collection failed\n" + proc.stdout + proc.stderr)
    return [line.strip() for line in proc.stdout.splitlines() if line.startswith("tests/")]


def mutated_source(case: dict, original: bytes) -> bytes:
    text = original.decode()
    function = next(
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == case["function"]
    )
    lines = text.splitlines(keepends=True)
    start, end = function.lineno - 1, function.end_lineno
    scope = "".join(lines[start:end])
    if scope.count(case["before"]) != 1 or case["before"] == case["after"]:
        raise ValueError(f"case {case['id']}: mutation must match exactly once")
    scope = scope.replace(case["before"], case["after"], 1)
    mutated = "".join(lines[:start]) + scope + "".join(lines[end:])
    ast.parse(mutated)  # Never count syntax/import failures as killed mutations.
    return mutated.encode()


def run_node(case: dict, directory: Path, phase: str) -> dict:
    report = directory / f"{phase}.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        case["node_id"],
        "--tb=long",
        f"--junitxml={report}",
    ]
    env = dict(os.environ)
    # Each process reads fresh source; old timestamp/size-valid pyc cannot hide a mutation.
    env["PYTHONPYCACHEPREFIX"] = str(directory / f"cache-{phase}")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=55
        )
        result = {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {"timeout": True, "command": command, "error": str(exc)}
    result["seconds"] = round(time.monotonic() - started, 3)
    if report.exists():
        tree = ET.parse(report)
        result["testcases"] = [
            {
                "name": node.get("name"),
                "failures": [
                    dict(message=f.get("message"), text=f.text or "")
                    for f in node.findall("failure")
                ],
                "errors": len(node.findall("error")),
                "skipped": len(node.findall("skipped")),
            }
            for node in tree.findall(".//testcase")
        ]
    return result


def expected_result(case: dict, result: dict, *, broken: bool) -> bool:
    nodes = result.get("testcases", [])
    if result.get("returncode") != (1 if broken else 0) or len(nodes) != 1:
        return False
    node = nodes[0]
    if node["name"] != case["node_id"].split("::")[-1] or node["errors"] or node["skipped"]:
        return False
    failures = node["failures"]
    test_module = case["node_id"].split("::")[0]
    if not broken:
        return failures == []
    text = failures[0]["text"]
    loc = f"{test_module}:{case['assertion_line']}:"
    failed_at = f"{test_module}:{case['assertion_line']}:" in text
    failure_kind = (
        "AssertionError" in text
        or failures[0]["message"].startswith("Failed:")
        or "AssertionError" in failures[0]["message"]
    )
    return (
        len(failures) == 1
        and failure_kind
        and case["intended_assertion"] in text
        and failed_at
        and "ImportError" not in text
        and "ModuleNotFoundError" not in text
    )


def run_pair(case: dict) -> dict:
    path = ROOT / case["path"]
    original = path.read_bytes()
    mutated = mutated_source(case, original)
    receipt = {
        **case,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "mutation_diff": "".join(
            difflib.unified_diff(
                original.decode().splitlines(keepends=True),
                mutated.decode().splitlines(keepends=True),
                fromfile=case["path"],
                tofile=case["path"],
            )
        ),
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="issue1154-") as temp:
        try:
            path.write_bytes(mutated)
            receipt["broken"] = run_node(case, Path(temp), "broken")
        finally:
            path.write_bytes(original)
            receipt["source_restored"] = path.read_bytes() == original
            # Even on an unexpected red result, run the restoration check before stopping.
            receipt["restored"] = run_node(case, Path(temp), "restored")
    receipt["pair_seconds"] = round(time.monotonic() - started, 3)
    receipt["passed"] = (
        receipt["source_restored"]
        and receipt["pair_seconds"] < 120
        and expected_result(case, receipt["broken"], broken=True)
        and expected_result(case, receipt["restored"], broken=False)
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[f"{i:02}" for i in range(1, 77)])
    parser.add_argument("--output", type=Path, default=ROOT / "tests/evidence_1154/results.json")
    args = parser.parse_args()
    cases = json.loads(CASES.read_text())
    declared = collect_declared_nodes()
    if len(cases) != 76 or [case["node_id"] for case in cases] != declared:
        raise ValueError("the case map must cover exactly all 76 test nodes")
    # Validate every patch before any source mutation or subprocess.
    for case in cases:
        mutated_source(case, (ROOT / case["path"]).read_bytes())
    results = {
        "check_date": "2026-11-05",
        "python": sys.executable,
        "inputs_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                *(ROOT / module for module in TEST_MODULES),
                Path(__file__).resolve(),
                CASES,
            )
        },
        "pairs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if args.case and case["id"] != args.case:
            continue
        receipt = run_pair(case)
        results["pairs"].append(receipt)
        args.output.write_text(json.dumps(results, indent=2) + "\n")
        count = sum(pair["passed"] for pair in results["pairs"])
        print(
            f"Outcome 1 = {count}/{len(results['pairs'])}; case {case['id']}: "
            f"{'PASS' if receipt['passed'] else 'STOP'} ({receipt['pair_seconds']}s)",
            flush=True,
        )
        if not receipt["passed"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
