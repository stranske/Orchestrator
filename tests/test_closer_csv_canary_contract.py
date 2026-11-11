"""Corrected CSV canary passes; partial revision 0507d28 fails the contract.

Use the suite interpreter in a virtual environment or when Anaconda is
unavailable. Otherwise, use ``/opt/anaconda3/bin/python3``. The partial
``csv_export.py`` is the blob
``5e64e302ce90da9a3145f99412d20bf24beaef31`` from
``0507d28acf68d6bf21c68138d18d56cd01968827``. A checkout with git confirms the
pin against that revision. The scratch copy is the only place that blob is
executed. Observed result: exit status 1 and
``AssertionError: CSV contract is not implemented``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
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
    """Prefer the active virtual environment, then the local Anaconda runtime."""
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix and Path(conda_prefix).resolve() == Path(sys.prefix).resolve():
        return sys.executable
    if sys.prefix == sys.base_prefix and ANACONDA.is_file() and os.access(ANACONDA, os.X_OK):
        return str(ANACONDA)
    return sys.executable


def partial_csv_export():
    """Return the partial revision's csv_export.py, checked against git when that commit is local.

    Always verify the embedded source's Git blob hash, including in depth-1
    checkouts and exec mirrors. When the commit is local, also check its source.
    """
    source = PARTIAL_CSV_EXPORT.encode("utf-8")
    header = f"blob {len(source)}\0".encode("ascii")
    assert hashlib.sha1(header + source).hexdigest() == PARTIAL_BLOB, "partial fixture pin changed"
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


def run_contract(script, export_path=None):
    args = [contract_interpreter(), str(script)]
    if export_path is not None:
        args.append(str(export_path))
    env = os.environ.copy()
    env.pop("PYTHONOPTIMIZE", None)
    return subprocess.run(
        args,
        cwd=script.parent,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_contract_interpreter_preserves_selected_conda_environment(monkeypatch, tmp_path):
    fallback = tmp_path / "anaconda-python"
    fallback.write_text("", encoding="utf-8")
    fallback.chmod(0o755)
    selected = tmp_path / "selected-conda"
    monkeypatch.setattr(sys, "prefix", str(selected))
    monkeypatch.setattr(sys, "base_prefix", str(selected))
    monkeypatch.setattr(sys, "executable", str(selected / "bin/python"))
    monkeypatch.setattr(sys.modules[__name__], "ANACONDA", fallback)
    monkeypatch.setenv("CONDA_PREFIX", str(selected / "."))

    assert contract_interpreter() == sys.executable

    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path / "other-conda"))
    assert contract_interpreter() == str(fallback)
    monkeypatch.delenv("CONDA_PREFIX")
    assert contract_interpreter() == str(fallback)


def test_contract_rejects_wrong_csv_scores_with_parent_optimization(monkeypatch, tmp_path):
    scratch = tmp_path / "wrong scores"
    shutil.copytree(FIXTURE, scratch)
    exporter = scratch / "csv_export.py"
    source = exporter.read_text(encoding="utf-8")
    source = source.replace('row["score"]])', '"wrong score"])')
    exporter.write_text(source, encoding="utf-8")
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")

    result = run_contract(scratch / SCRIPT_NAME)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "contract assertion failed" in result.stderr
    assert "wrong score" in result.stderr


def test_corrected_contract_exits_zero_and_partial_revision_raises(tmp_path):
    """Exit 0 on the corrected fixture; exit 1 with AssertionError on 0507d28."""
    corrected = run_contract(FIXTURE / SCRIPT_NAME)
    assert corrected.returncode == 0, corrected.stderr
    assert corrected.stdout.strip() == "contract ok"
    assert corrected.stderr == ""

    scratch = tmp_path / "partial canary"
    shutil.copytree(FIXTURE, scratch)
    partial_export = scratch / "csv_export.py"
    partial_export.write_text(partial_csv_export(), encoding="utf-8", newline="")
    for script, export_path in (
        (scratch / SCRIPT_NAME, None),
        (FIXTURE / SCRIPT_NAME, partial_export),
    ):
        partial = run_contract(script, export_path)
        assert partial.returncode == 1, partial.stderr
        assert partial.stdout == ""
        assert "AssertionError: CSV contract is not implemented" in partial.stderr
        assert "contract assertion failed" in partial.stderr
        assert "NotImplementedError: Declared incomplete CSV canary fixture" in partial.stderr

    # Selecting the historical module must not replace the corrected sibling.
    corrected = run_contract(scratch / SCRIPT_NAME, FIXTURE / "csv_export.py")
    assert corrected.returncode == 0, corrected.stderr
    assert corrected.stdout.strip() == "contract ok"
    assert corrected.stderr == ""


def test_contract_entry_point_requires_explicit_evidence_opt_in(monkeypatch, capsys, tmp_path):
    """Default and unsupported commands never start subprocess or network evidence reads."""
    spec = importlib.util.spec_from_file_location("csv_contract_entry", FIXTURE / SCRIPT_NAME)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    def unexpected_evidence(*args, **kwargs):
        raise AssertionError("evidence collection requires --chain-evidence")

    monkeypatch.setattr(verifier.subprocess, "Popen", unexpected_evidence)
    monkeypatch.setattr(verifier.urllib.request, "urlopen", unexpected_evidence)
    monkeypatch.setattr(verifier, "_report_chain_evidence", unexpected_evidence)

    assert verifier.main([]) == 0
    output = capsys.readouterr()
    assert output.out == "contract ok\n"
    assert output.err == ""

    for args in (["--legacy-chain-evidence"], ["--legacy"], ["--unknown"]):
        assert verifier.main(args) == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert output.err == "unsupported arguments\n"

    # This acceptance runner has no native custody. The current reader must
    # report that gap, without falling back to contract success or legacy reads.
    state = tmp_path / "missing-native-custody"
    monkeypatch.setenv("CLOSER_CORE_STATE", str(state))
    original_path = list(sys.path)
    assert verifier.main(["--chain-evidence"]) == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["completion"] == "incomplete"
    assert str(state / "completions" / "stranske_orchestrator_988_989.json") in report["reason"]
    assert output.err == ""
    assert not state.exists(), "the reader must not create native custody"
    assert sys.path == original_path

    calls = []
    monkeypatch.setattr(verifier, "_report_chain_evidence", lambda: calls.append(True) or 2)
    assert verifier.main(["--chain-evidence", "--legacy"]) == 2
    assert calls == [True]
