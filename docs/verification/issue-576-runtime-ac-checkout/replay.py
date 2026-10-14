"""Replay the issue's production-call-site break in an isolated development checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    args.out.mkdir(parents=True, exist_ok=False)
    source = root / "src/keepalive_outcomes.py"
    original = source.read_bytes()
    needle = b"worktree=checkout,"
    assert original.count(needle) == 1
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_runtime_ac_shadow.py::test_ingest_executes_shadow_spec_on_exact_checkout",
        "-q",
        "-o",
        "addopts=",
    ]

    def run(name):
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        log = (result.stdout + result.stderr).replace(str(root), "<checkout>")
        (args.out / f"{name}.log").write_text(log)
        return result.returncode, log

    before, _ = run("corrected-green")
    assert before == 0
    base_source = subprocess.check_output(
        ["git", "show", "eef7c2c0:src/keepalive_outcomes.py"], cwd=root
    )
    try:
        source.write_bytes(base_source)
        baseline, baseline_log = run("original-red")
        assert baseline == 1 and "1 failed" in baseline_log and "len(observed)" in baseline_log
    finally:
        source.write_bytes(original)
    try:
        source.write_bytes(original.replace(needle, b'worktree=pr.get("_worktree"),'))
        broken_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        red, red_log = run("broken-red")
        assert red == 1 and "1 failed" in red_log and "len(observed)" in red_log
    finally:
        source.write_bytes(original)
    restored, _ = run("restored-green")
    assert source.read_bytes() == original and restored == 0
    manifest = {
        "command": ["python3", *command[1:]],
        "exit_codes": {
            "original": baseline,
            "corrected": before,
            "broken": red,
            "restored": restored,
        },
        "original_source_sha256": hashlib.sha256(base_source).hexdigest(),
        "broken_source_sha256": broken_hash,
        "restoration_byte_identical": True,
        "inputs": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for path in [
                "src/keepalive_outcomes.py",
                "src/runtime_ac_gate.py",
                "src/provision.py",
                "src/adversarial.py",
                "tests/test_runtime_ac_shadow.py",
            ]
        },
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
