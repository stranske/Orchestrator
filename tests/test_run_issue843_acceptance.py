"""Failure-path checks for the scoped runner; no real pytest child or source mutation."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "issue843_acceptance",
    Path(__file__).resolve().parents[1] / "scripts/run_issue843_acceptance.py",
)
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)
NODE = "tests/test_example.py::test_mapped"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(runner, "EVIDENCE_DIR", evidence)
    monkeypatch.setattr(runner, "RECEIPT_JSON", evidence / "mutation-receipt.json")
    monkeypatch.setattr(runner, "REPORT_MD", evidence / "README.md")

    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected subprocess")

    monkeypatch.setattr(runner.subprocess, "run", unexpected)


def mutation(replace="first"):
    return runner.MutationSpec("example", (NODE,), "example.py", "old", "broken", replace)


@pytest.mark.parametrize(
    "replace,expected", [("first", b"broken old\r\n"), ("all", b"broken broken\r\n")]
)
def test_mutate_and_restore_exact_bytes(tmp_path, replace, expected):
    path = tmp_path / "example.py"
    original = b"old old\r\n"
    path.write_bytes(original)
    saved, digest = runner.apply_mutation(path, mutation(replace))
    assert path.read_bytes() == expected
    assert saved == original
    runner.restore_file(path, saved, digest)
    assert path.read_bytes() == original
    assert runner.sha256_bytes(path.read_bytes()) == digest


def test_missing_needle_and_wrong_restore_hash_leave_file_untouched(tmp_path):
    path = tmp_path / "example.py"
    path.write_bytes(b"untouched\r\n")
    with pytest.raises(ValueError, match="needle missing"):
        runner.apply_mutation(path, mutation())
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        runner.restore_file(path, b"old", "wrong")
    assert path.read_bytes() == b"untouched\r\n"


def test_restore_checks_on_disk_bytes(tmp_path, monkeypatch):
    path = tmp_path / "example.py"
    monkeypatch.setattr(Path, "write_bytes", lambda *args: None)
    path.touch()
    with pytest.raises(RuntimeError, match="restore failed"):
        runner.restore_file(path, b"old", runner.sha256_bytes(b"old"))


@pytest.mark.parametrize(
    "code,output,valid",
    [
        (1, f"FAILED {NODE} - AssertionError\n" + "padding\n" * 500, True),
        (1, f"FAILED {NODE}_other - AssertionError\n", False),
        (1, f"FAILED {NODE}[parameter] - AssertionError\n", False),
        (1, f"ERROR {NODE} - setup failed\n", False),
        (1, f"FAILED {NODE} - AssertionError\nERROR other - teardown failed\n", False),
        (1, "ImportError: broken dependency\n", False),
        (2, f"FAILED {NODE} - collection failed\n", False),
        (3, f"FAILED {NODE} - internal error\n", False),
        (4, "ERROR: unknown node\n", False),
        (5, "no tests ran\n", False),
        (0, f"FAILED {NODE} - stale output\n", False),
    ],
)
def test_only_exact_mapped_test_failure_counts(monkeypatch, code, output, valid):
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, code, output, "")
    )
    result = runner.run_pytest("selected-python", (NODE,))
    assert result["failed"] is valid
    if valid:
        assert NODE in result["failed_node_ids"]
        assert f"FAILED {NODE}" not in result["stdout_tail"]
        assert result["failure_excerpt"] == f"FAILED {NODE} - AssertionError"


def test_child_gets_fresh_temporary_paths_before_imports(monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", "/live/state")
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", "/live/runtime")
    roots = []

    def child(cmd, **kwargs):
        env = kwargs["env"]
        state, runtime = Path(env["ORCH_STATE_DIR"]), Path(env["ORCH_LOCAL_RUNTIME"])
        assert state.parent == runtime.parent and state != runtime
        assert state.parent.is_dir()
        assert 0 < kwargs["timeout"] < 120
        assert cmd[0] == "selected-python"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        roots.append(state.parent)
        return subprocess.CompletedProcess(cmd, 0, "1 passed", "")

    monkeypatch.setattr(runner.subprocess, "run", child)
    for _ in range(2):
        runner.run_pytest("selected-python", (NODE,), extra_env={"ORCH_STATE_DIR": "/caller/state"})
    assert roots[0] != roots[1]
    assert all(not path.exists() for path in roots)


@pytest.mark.parametrize("timeout_on", [1, 2])
def test_timeout_propagates_and_restores_source(tmp_path, monkeypatch, timeout_on):
    spec = mutation()
    monkeypatch.setattr(runner, "MUTATIONS", (spec,))
    source = tmp_path / spec.rel_path
    original = b"old\r\n"
    source.write_bytes(original)
    calls = 0
    roots = []

    def child(cmd, **kwargs):
        nonlocal calls
        calls += 1
        roots.append(Path(kwargs["env"]["ORCH_STATE_DIR"]).parent)
        assert source.read_bytes() == (b"broken\r\n" if calls == 1 else original)
        if calls == timeout_on:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 1, f"FAILED {NODE} - AssertionError\n", "")

    monkeypatch.setattr(runner.subprocess, "run", child)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_mutations("selected-python")
    assert source.read_bytes() == original
    assert all(not path.exists() for path in roots)
    assert not runner.EVIDENCE_DIR.exists()


@pytest.mark.parametrize(
    "code,output", [(0, "1 passed"), (1, f"ERROR {NODE} - setup"), (2, "collection error")]
)
def test_invalid_break_restores_source(tmp_path, monkeypatch, code, output):
    spec = mutation()
    monkeypatch.setattr(runner, "MUTATIONS", (spec,))
    source = tmp_path / spec.rel_path
    source.write_bytes(b"old\r\n")
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, code, output, "")
    )
    with pytest.raises(SystemExit, match="mapped test failure"):
        runner.run_mutations("selected-python")
    assert source.read_bytes() == b"old\r\n"


@pytest.mark.parametrize(
    "args", [["--limit", "0"], ["--limit", "-1"], ["--limit", "1", "--write-report"]]
)
def test_bad_arguments_rejected_before_work(monkeypatch, args):
    def unexpected(*a, **kw):
        pytest.fail("invalid arguments reached interpreter selection or execution")

    monkeypatch.setattr(runner, "resolve_python", unexpected)
    monkeypatch.setattr(runner, "run_mutations", unexpected)
    monkeypatch.setattr(runner, "write_report", unexpected)
    with pytest.raises(SystemExit) as exc:
        runner.main(args)
    assert exc.value.code == 2
    assert not runner.EVIDENCE_DIR.exists()


def valid_receipts():
    return [
        {
            "mutation_id": spec.mutation_id,
            "target": spec.rel_path,
            "node_ids": list(spec.node_ids),
            "verdict": "PASS",
            "original_sha256": "same-hash",
            "restored_sha256": "same-hash",
            "break": [
                {"node_id": node, "exit_code": 1, "failed": True, "failed_node_ids": [node]}
                for node in spec.node_ids
            ],
            "restored_pass": [{"node_id": node, "exit_code": 0} for node in spec.node_ids],
        }
        for spec in runner.MUTATIONS
    ]


@pytest.mark.parametrize(
    "damage", ["partial", "duplicate", "missing_break", "setup", "hash", "baseline", "mapping"]
)
def test_invalid_report_preserves_both_evidence_files(monkeypatch, damage):
    receipts = copy.deepcopy(valid_receipts())
    baseline = {"exit_code": 0}
    if damage == "partial":
        receipts.pop()
    elif damage == "duplicate":
        receipts.append(receipts[0])
    elif damage == "missing_break":
        receipts[0]["break"] = []
    elif damage == "setup":
        receipts[0]["break"][0]["failed"] = False
    elif damage == "hash":
        receipts[0]["restored_sha256"] = "changed"
    elif damage == "baseline":
        baseline["exit_code"] = 1
    else:
        monkeypatch.setattr(runner, "MUTATIONS", runner.MUTATIONS[:-1])
    runner.EVIDENCE_DIR.mkdir()
    runner.RECEIPT_JSON.write_bytes(b"prior valid receipt")
    runner.REPORT_MD.write_bytes(b"prior report")
    with pytest.raises(ValueError):
        runner.write_report(
            python="selected-python", baseline=baseline, receipts=receipts, git_head="revision"
        )
    assert runner.RECEIPT_JSON.read_bytes() == b"prior valid receipt"
    assert runner.REPORT_MD.read_bytes() == b"prior report"


def test_complete_report_labels_coverage_limits():
    runner.write_report(
        python="selected-python",
        baseline={"exit_code": 0},
        receipts=valid_receipts(),
        git_head="revision",
    )
    report = runner.REPORT_MD.read_text()
    assert "selected-python scripts/run_issue843_acceptance.py --baseline-only" in report
    assert "local pytest missing-line estimates" in report
    assert "shortfall remain UNVERIFIED" in report
    assert "partial delivery" in report
    assert "merge commit remains authoritative" not in report


def test_selected_interpreter_failure_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STANDARD_PYTHON", str(tmp_path / "missing-python"))
    with pytest.raises(SystemExit, match="selected Python interpreter unavailable"):
        runner.resolve_python(None)
    selected = tmp_path / "selected-python"
    selected.touch(mode=0o700)
    assert runner.resolve_python(str(selected)) == str(selected)
