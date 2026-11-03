#!/usr/bin/env python3
"""Per-node break-and-revert runner for stranske/Orchestrator#834 test modules.

Discovers pytest node IDs, applies one temporary production mutation per node,
requires a targeted test failure, restores the file in ``finally``, and exits
nonzero when any node is unmapped or incomplete.

Usage: python3 tests/mutation_runner_829.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from mutation_map_829 import MUTATIONS_BY_TEST_NAME  # noqa: E402

TEST_MODULES = [
    "tests/test_consumer_sync_artifact_ingest_coverage_gaps.py",
    "tests/test_redirect_shadow_helpers_cli_coverage.py",
    "tests/test_redirect_shadow_historical_coverage.py",
]

TIMEOUT = 120
NODE_LINE = re.compile(r"^(tests/[^\s]+::[^\s]+)")


@dataclass
class NodeRecord:
    node_id: str
    status: str
    mutation_target: str = ""
    apply_ok: bool = False
    green_before: bool = False
    red_exit: int | None = None
    failed_node: str = ""
    failure_kind: str = ""
    failure_excerpt: str = ""
    restore_ok: bool = False
    green_after: bool = False
    diagnostics: str = ""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_node_ids() -> list[str]:
    env = _isolated_env(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *TEST_MODULES],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "pytest collection failed\n"
            + proc.stdout
            + proc.stderr
        )
    nodes: list[str] = []
    for line in proc.stdout.splitlines():
        m = NODE_LINE.match(line.strip())
        if m:
            nodes.append(m.group(1))
    if not nodes:
        raise RuntimeError("collection returned zero node IDs")
    return nodes


def _isolated_env(cwd: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["ORCH_STATE_DIR"] = str(cwd / ".mutation_state")
    env["ORCH_LOCAL_RUNTIME"] = str(cwd / ".mutation_runtime")
    env.pop("CI", None)
    return env


def copy_isolated(tmp: Path) -> Path:
    iso = tmp / "iso"
    iso.mkdir(parents=True, exist_ok=True)
    for name in ("src", "tests"):
        src = REPO_ROOT / name
        shutil.copytree(src, iso / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for extra in ("pyproject.toml",):
        p = REPO_ROOT / extra
        if p.is_file():
            shutil.copy2(p, iso / extra)
    return iso


def run_pytest_node(cwd: Path, node_id: str) -> tuple[int, str]:
    env = _isolated_env(cwd)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            node_id,
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    return proc.returncode, proc.stdout + proc.stderr


def classify_failure(node_id: str, output: str) -> tuple[str, str, str]:
    """Return (failed_node, failure_kind, excerpt)."""
    failed_node = ""
    for line in output.splitlines():
        if line.startswith("FAILED "):
            failed_node = line.split("FAILED ", 1)[1].split(" - ", 1)[0].strip()
            break
        if line.startswith("ERROR "):
            failed_node = line.split("ERROR ", 1)[1].split(" - ", 1)[0].strip()
            break
    if not failed_node and "=== FAILURES ===" in output:
        for line in output.splitlines():
            if line.startswith(node_id):
                failed_node = node_id
                break
    kind = "none"
    if "AssertionError" in output:
        kind = "AssertionError"
    elif "Failed:" in output or "assert " in output:
        kind = "assertion"
    elif failed_node:
        kind = "FAILED"
    excerpt_lines = [
        ln
        for ln in output.splitlines()
        if "AssertionError" in ln or ln.strip().startswith("E   assert") or "FAILED" in ln
    ]
    excerpt = "\n".join(excerpt_lines[:8])[:1200]
    return failed_node, kind, excerpt


def apply_mutation(path: Path, old: str, new: str) -> tuple[bool, str]:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        return False, f"needle count {count} (expected 1)"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True, ""


def test_name_from_node(node_id: str) -> str:
    return node_id.rsplit("::", 1)[-1]


def run_node(node_id: str, tmp: Path) -> NodeRecord:
    name = test_name_from_node(node_id)
    spec = MUTATIONS_BY_TEST_NAME.get(name)
    record = NodeRecord(node_id=node_id, status="UNMAPPED")
    if spec is None:
        record.diagnostics = f"no mutation mapping for {name}"
        return record

    iso = copy_isolated(tmp)
    target = iso / spec["target"]
    record.mutation_target = spec["target"]
    orig_bytes = target.read_bytes()
    orig_sha = sha256_file(target)

    try:
        code, out = run_pytest_node(iso, node_id)
        record.green_before = code == 0
        if not record.green_before:
            record.status = "GREEN_BEFORE_FAILED"
            record.diagnostics = out[-1500:]
            return record

        ok, why = apply_mutation(target, spec["old"], spec["new"])
        record.apply_ok = ok
        if not ok:
            record.status = "APPLY_FAILED"
            record.diagnostics = why
            return record

        code, out = run_pytest_node(iso, node_id)
        record.red_exit = code
        failed_node, kind, excerpt = classify_failure(node_id, out)
        record.failed_node = failed_node
        record.failure_kind = kind
        record.failure_excerpt = excerpt
        record.diagnostics = out[-2000:]

        if code == 0:
            record.status = "NOT_DETECTED"
            return record
        if failed_node and failed_node != node_id:
            record.status = "WRONG_NODE_FAILED"
            return record
        if kind == "none":
            record.status = "NO_ASSERTION_FAILURE"
            return record

        target.write_bytes(orig_bytes)
        record.restore_ok = sha256_file(target) == orig_sha
        if not record.restore_ok:
            record.status = "RESTORE_VERIFY_FAILED"
            return record

        code, out = run_pytest_node(iso, node_id)
        record.green_after = code == 0
        if not record.green_after:
            record.status = "GREEN_AFTER_FAILED"
            record.diagnostics = out[-1500:]
            return record

        record.status = "OK"
        return record
    finally:
        if target.exists():
            target.write_bytes(orig_bytes)
        shutil.rmtree(iso, ignore_errors=True)


def main() -> int:
    print("mutation_runner_829: discover nodes and run break-and-revert\n")
    try:
        nodes = collect_node_ids()
    except RuntimeError as exc:
        print(f"COLLECTION_ERROR: {exc}")
        return 2

    print(f"collected {len(nodes)} node(s)\n")
    missing = [n for n in nodes if test_name_from_node(n) not in MUTATIONS_BY_TEST_NAME]
    if missing:
        print("MISSING_MAPPINGS:")
        for n in missing:
            print(f"  {n}")
        return 2
    if len(MUTATIONS_BY_TEST_NAME) != len(nodes):
        extra = set(MUTATIONS_BY_TEST_NAME) - {test_name_from_node(n) for n in nodes}
        if extra:
            print("STALE_MAPPINGS (not in collection):")
            for name in sorted(extra):
                print(f"  {name}")
            return 2

    records: list[NodeRecord] = []
    with tempfile.TemporaryDirectory(prefix="mut829_") as tmpdir:
        tmp = Path(tmpdir)
        for node_id in nodes:
            print(node_id)
            rec = run_node(node_id, tmp)
            records.append(rec)
            print(f"  {rec.status}")
            if rec.status != "OK":
                print(f"  detail: {rec.failure_kind or rec.diagnostics[:200]}")

    summary = {
        "collected": len(nodes),
        "nodes": [asdict(r) for r in records],
        "counts": {},
    }
    for rec in records:
        summary["counts"][rec.status] = summary["counts"].get(rec.status, 0) + 1

    out_path = REPO_ROOT / "tests" / "mutation_evidence_829.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    print(json.dumps(summary["counts"], indent=2))

    if any(r.status != "OK" for r in records):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
