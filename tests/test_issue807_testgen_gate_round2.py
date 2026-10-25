"""Hermetic coverage for the remaining issue-807 `testgen_gate` gaps."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import testgen_gate as gate


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_summary_parser_and_failure_cause_edge_cases():
    # A `=`-framed line with no count tokens is skipped (and alone never parses).
    assert gate._pytest_summary_counts("==== not a summary ====")["parsed"] is False
    text = "===== 2 passed, 1 failed in 0.1s =====\n==== trailing decoy ===="
    counts = gate._pytest_summary_counts(text)
    assert counts["parsed"] is True
    assert counts["passed"] == 2
    assert counts["failed"] == 1

    # Unknown exit code and a coverage-gate false red.
    unknown = gate.pytest_exit_meaning(99)
    assert unknown["measured"] is False
    assert unknown["meaning"] == "unknown"

    cov = (
        "FAIL Required test coverage of 25.0% not reached. Total coverage: 8.23%\n"
        "===== 1 passed, 92 deselected in 3.36s ====="
    )
    cause = gate.pytest_failure_cause(1, cov)
    assert cause["test_failure"] is False
    assert "coverage threshold" in cause["cause"]


def test_coverage_helpers_and_empty_source_guards(tmp_path):
    with pytest.raises(ValueError, match="at least one source"):
        gate.coverage_run_cmd([], tmp_path / "data", ["tests"])

    dotted = gate.coverage_sources(["pkg/mod.py", "plain"])
    assert dotted == ["pkg.mod", "plain"]

    assert gate.measured_files({"files": {"./a.py": {}, "b\\c.py": {}}}) == ["a.py", "b/c.py"]
    assert gate._source_matches_file("", "pkg/mod.py") is False
    assert gate._source_matches_file("pkg/mod.py", "pkg/mod.py") is True
    assert gate._source_matches_file("pkg.mod", "other/pkg/mod.py") is True

    bad_json = tmp_path / "broken.json"
    bad_json.write_text("{not-json", encoding="utf-8")
    totals, files, err = gate.read_coverage_json(bad_json)
    assert totals is None and files == [] and err

    good_json = tmp_path / "good.json"
    report = {
        "totals": {"covered_lines": 3, "num_statements": 5, "percent_covered": 60.0},
        "files": {
            "pkg/a.py": {"summary": {"covered_lines": 2, "num_statements": 4}},
            "pkg/b.py": {"summary": {"covered_lines": 1, "num_statements": 1}},
        },
    }
    good_json.write_text(json.dumps(report), encoding="utf-8")
    totals, files, err = gate.read_coverage_json(good_json)
    assert err is None
    assert totals["covered_lines"] == 3
    assert files == ["pkg/a.py", "pkg/b.py"]

    summed = gate.coverage_totals({"files": report["files"]})
    assert summed == {"covered_lines": 3, "num_statements": 5, "percent_covered": 60.0}


def test_command_report_timeout_without_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial", stderr=None)
        ),
    )
    timed = gate.command_report("check", ["safe"], tmp_path, 3)
    assert timed["exit_code"] == 124
    assert timed["stderr_tail"] == "timed out after 3s"
    assert timed["stdout_tail"] == "partial"


def test_coverage_check_json_success_and_non_empty_data_failure(monkeypatch, tmp_path):
    json_commands = []
    read_paths = []

    def fake_command(name, argv, cwd, timeout):
        if name.endswith("_coverage_run"):
            return {"ok": True, "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
        if name.endswith("_coverage_json"):
            json_commands.append(argv)
            return {"ok": True, "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
        raise AssertionError(name)

    def fake_read(path):
        read_paths.append(path)
        return (
            {"covered_lines": 4, "num_statements": 10, "percent_covered": 40.0},
            ["pkg/mod.py"],
            None,
        )

    monkeypatch.setattr(gate, "command_report", fake_command)
    monkeypatch.setattr(gate, "read_coverage_json", fake_read)
    ok = gate.coverage_check("candidate", tmp_path, ["pkg"], ["tests"], tmp_path, 4)
    assert ok["ok"] is True
    assert ok["coverage"]["covered_lines"] == 4
    assert ok["measured_files"] == ["pkg/mod.py"]
    assert read_paths == [tmp_path / "candidate.json"]
    assert len(json_commands) == 1
    argv = json_commands[0]
    assert argv[argv.index("-o") + 1] == str(read_paths[0])

    def failing_json(name, argv, cwd, timeout):
        if name.endswith("_coverage_run"):
            return {"ok": True, "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
        return {
            "ok": False,
            "exit_code": 1,
            "stdout_tail": "parse error",
            "stderr_tail": "coverage json exploded",
        }

    monkeypatch.setattr(gate, "command_report", failing_json)
    monkeypatch.setattr(
        gate,
        "read_coverage_json",
        lambda path: (_ for _ in ()).throw(AssertionError("should not read")),
    )
    failed = gate.coverage_check("candidate", tmp_path, ["pkg"], ["tests"], tmp_path, 4)
    assert failed["ok"] is False
    assert failed["error"] == "coverage json exploded"


def test_reliability_and_measurement_zero_and_never_imported(monkeypatch, tmp_path):
    calls = []

    def fake_command(name, argv, cwd, timeout):
        calls.append(name)
        return {"ok": name.endswith("_1"), "exit_code": 0 if name.endswith("_1") else 1}

    monkeypatch.setattr(gate, "command_report", fake_command)
    reliability = gate.reliability_check(tmp_path, ["tests"], runs=2, timeout=3, temp_dir=tmp_path)
    assert reliability["runs_requested"] == 2
    assert reliability["all_passed"] is False
    assert len(calls) == 2

    side = {
        "ok": True,
        "coverage": {"num_statements": 0},
        "measured_files": ["other.py"],
        "never_imported": ["pkg.missing"],
    }
    measured = gate.coverage_measurement(["pkg"], side, side)
    assert measured["measured"] is False
    assert any("0 statements" in r for r in measured["reasons"])
    assert any("never imported" in r for r in measured["reasons"])
    assert any("importable MODULE" in r for r in measured["reasons"])


def test_hollow_check_failure_modes(monkeypatch, tmp_path):
    missing = tmp_path / "no-local-verify.py"
    monkeypatch.setattr(gate, "LOCAL_VERIFY", missing)
    assert gate.hollow_check(tmp_path, "HEAD~1", ["tests"], None, 3)["reason"].startswith(
        "local_verify.py is not beside"
    )

    monkeypatch.setattr(gate, "LOCAL_VERIFY", tmp_path / "local_verify.py")
    (tmp_path / "local_verify.py").write_text("# stub\n", encoding="utf-8")

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], 3)),
    )
    assert "exceeded" in gate.hollow_check(tmp_path, "HEAD~1", ["tests"], None, 3)["reason"]

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="no json here", stderr=""),
    )
    assert "emitted no JSON" in gate.hollow_check(tmp_path, "HEAD~1", ["tests"], None, 3)["reason"]

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{not-json", stderr=""),
    )
    assert "unparseable JSON" in gate.hollow_check(tmp_path, "HEAD~1", ["tests"], None, 3)["reason"]

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"node_analysis": {"counts": {}}, "node_verdict": "EMPTY"}),
            stderr="",
        ),
    )
    empty = gate.hollow_check(tmp_path, "HEAD~1", ["tests"], None, 3)
    assert empty["measured"] is False
    assert empty["reason"] == "EMPTY"


def test_hollow_and_verdict_rows_name_blind_and_hollow_cases():
    blind = gate._hollow_check_row(
        {
            "measured": False,
            "reason": "missing base",
            "remedy": "pass --base-ref",
            "hollow_nodes": [],
        }
    )
    assert blind["ok"] is False and blind["could_not_measure"] is True
    assert "COULD NOT MEASURE HOLLOWNESS" in blind["detail"]

    hollow = gate._hollow_check_row(
        {"measured": True, "hollow_nodes": ["tests/test_a.py::t"], "counts": {"nodes": 1}}
    )
    assert hollow["ok"] is False
    assert "pass against a broken base" in hollow["detail"]

    clean = gate._hollow_check_row({"measured": True, "hollow_nodes": [], "counts": {"nodes": 2}})
    assert clean["ok"] is True
    assert "every candidate node discriminates" in clean["detail"]

    collect = {"ok": False, "exit": gate.pytest_exit_meaning(5), "run": {"exit_code": 5}}
    baseline = {"ok": True, "exit": gate.pytest_exit_meaning(0)}
    candidate = {"ok": True, "exit": gate.pytest_exit_meaning(0)}
    reliability = {"all_passed": True, "runs_requested": 1}
    measurement = {"measured": False, "reasons": ["no measured file belongs to --source 'pkg'"]}
    checks = gate.verdict_checks(
        collect,
        baseline,
        candidate,
        reliability,
        covered_delta=0,
        min_covered_lines_delta=1,
        measurement=measurement,
        hollow={"measured": False, "reason": "no probe", "remedy": "", "hollow_nodes": []},
    )
    by_name = {c["name"]: c for c in checks}
    assert by_name["collect_import_ok"]["could_not_measure"] is True
    assert by_name["coverage_delta"]["ok"] is False
    assert by_name["coverage_delta"]["could_not_measure"] is True


def test_main_requires_live_mode_arguments():
    with pytest.raises(SystemExit) as exc:
        gate.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        gate.main(["--source", "pkg"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        gate.main(["--source", "pkg", "--baseline-pytest-args", "tests"])
    assert exc.value.code == 2
    # Selftest path is exercised without spawning agents.
    assert gate.main(["--selftest"]) == 0
