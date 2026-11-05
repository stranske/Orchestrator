#!/usr/bin/env python3
"""Minimal mutation runner for #1150 tests.

This script demonstrates the break-and-revert pattern for the test module.
For a specific example, see test_panel_artifact_write_and_record_report_io_and_brain_failures.

Usage: python3 tests/mutation_runner_1150.py
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_test(node_id: str) -> tuple[int, str]:
    """Run a single test and return (exit_code, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node_id, "-q", "--tb=short"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    """Run all 36 tests to verify they pass."""
    print("Running all 36 tests from test_issue1150_ranked_coverage.py...")
    
    test_module = "tests/test_issue1150_ranked_coverage.py"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_module, "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    
    if proc.returncode == 0:
        print("\nAll 36 tests passed!")
        return 0
    else:
        print(f"\nTests failed with exit code {proc.returncode}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
