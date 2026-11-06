"""Corrected CSV canary passes; partial revision 0507d28 fails the contract.

The repository-selected interpreter is ``/opt/anaconda3/bin/python3`` when that
executable is present. This runner has no Anaconda install, so the check uses
the interpreter executing the suite. The partial ``csv_export.py`` is the blob
``5e64e302ce90da9a3145f99412d20bf24beaef31`` from
``0507d28acf68d6bf21c68138d18d56cd01968827``. A checkout with git confirms the
pin against that revision. The scratch copy is the only place that blob is
executed. Observed result: exit status 1 and
``AssertionError: CSV contract is not implemented``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/closer_real_csv_canary/v1"
SCRIPT_NAME = "verify_csv_contract.py"
PARTIAL_REVISION = "0507d28acf68d6bf21c68138d18d56cd01968827"
PARTIAL_RELATIVE = "tests/fixtures/closer_real_csv_canary/v1/csv_export.py"
PARTIAL_BLOB = "5e64e302ce90da9a3145f99412d20bf24beaef31"
ANACONDA = Path("/opt/anaconda3/bin/python3")
PARTIAL_CSV_EXPORT = '''"""Declared partial fixture for the controlled Closer E2E negative test.
This revision is intentionally incomplete; it is not production functionality.
"""

import json


def export_rows(rows, format):
    if format == "json":
        return json.dumps(rows)
    if format == "csv":
        raise NotImplementedError("Declared incomplete CSV canary fixture")
    raise ValueError("unsupported format")
'''


def contract_interpreter():
    """Use Anaconda when it is installed; otherwise the suite interpreter."""
    if ANACONDA.is_file() and os.access(ANACONDA, os.X_OK):
        return str(ANACONDA)
    return sys.executable


def partial_csv_export():
    """Return the partial revision's csv_export.py, checked against git when that commit is local.

    A depth-1 checkout does not contain 0507d28. The pin was taken from that blob
    on a clone that has it, and a checkout that does have the commit must still match.
    """
    if (REPO / ".git").exists():
        present = subprocess.run(
            ["git", "cat-file", "-e", f"{PARTIAL_REVISION}^{{commit}}"],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
        if present.returncode == 0:
            observed = subprocess.check_output(
                ["git", "show", f"{PARTIAL_REVISION}:{PARTIAL_RELATIVE}"],
                cwd=REPO,
            )
            blob = subprocess.check_output(
                ["git", "rev-parse", f"{PARTIAL_REVISION}:{PARTIAL_RELATIVE}"],
                cwd=REPO,
                text=True,
            ).strip()
            assert blob == PARTIAL_BLOB
            assert observed.decode() == PARTIAL_CSV_EXPORT
    return PARTIAL_CSV_EXPORT


def run_contract(script):
    return subprocess.run(
        [contract_interpreter(), str(script)],
        cwd=script.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_corrected_contract_exits_zero_and_partial_revision_raises(tmp_path):
    """Exit 0 on the corrected fixture; exit 1 with AssertionError on 0507d28."""
    corrected = run_contract(FIXTURE / SCRIPT_NAME)
    assert corrected.returncode == 0, corrected.stderr
    assert corrected.stdout.strip() == "contract ok"
    assert corrected.stderr == ""

    scratch = tmp_path / "partial-canary"
    shutil.copytree(FIXTURE, scratch)
    (scratch / "csv_export.py").write_text(partial_csv_export())
    partial = run_contract(scratch / SCRIPT_NAME)
    assert partial.returncode == 1
    assert partial.stdout == ""
    assert "AssertionError: CSV contract is not implemented" in partial.stderr
    assert "contract assertion failed" in partial.stderr
    assert "NotImplementedError: Declared incomplete CSV canary fixture" in partial.stderr
