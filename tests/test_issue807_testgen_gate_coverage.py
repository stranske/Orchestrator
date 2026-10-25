"""Hermetic regression coverage for issue 807's first ranked target."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import testgen_gate as gate


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Ensure a gate regression cannot inherit or create live runtime state."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_command_report_bounds_success_timeout_and_spawn_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="x" * 1300, stderr="warn"),
    )
    success = gate.command_report("check", ["safe", "command"], tmp_path, 7)
    assert success["ok"] is True
    assert success["stdout_tail"] == "x" * gate.TAIL_STDOUT

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"out", stderr=b"err")
        ),
    )
    timed_out = gate.command_report("check", ["safe"], tmp_path, 7)
    assert timed_out["exit_code"] == 124
    assert timed_out["stdout_tail"] == "out"
    assert timed_out["stderr_tail"] == "err\n[timed out after 7s]"

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("blocked"))
    )
    failed = gate.command_report("check", ["safe"], tmp_path, 7)
    assert failed["exit_code"] is None
    assert failed["stderr_tail"] == "blocked"


def test_coverage_check_distinguishes_data_failure_from_a_completed_empty_measurement(
    monkeypatch, tmp_path
):
    run = {"ok": True, "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
    no_data = {"ok": False, "exit_code": 1, "stdout_tail": "No data to report.", "stderr_tail": ""}
    reports = iter([run, no_data])
    monkeypatch.setattr(gate, "command_report", lambda *args, **kwargs: next(reports))
    empty = gate.coverage_check("candidate", tmp_path, ["pkg"], ["tests"], tmp_path, 4)
    assert empty["ok"] is True
    assert empty["coverage"] == {"covered_lines": 0, "num_statements": 0, "percent_covered": 0.0}

    failed_run = {"ok": False, "exit_code": 5, "stdout_tail": "", "stderr_tail": "nothing"}
    monkeypatch.setattr(gate, "command_report", lambda *args, **kwargs: failed_run)
    failed = gate.coverage_check("candidate", tmp_path, ["pkg"], ["tests"], tmp_path, 4)
    assert failed["ok"] is False
    assert failed["error"] == "nothing"
    assert failed["exit"]["measured"] is False


def test_measurement_and_hollow_probes_name_unmeasured_inputs(monkeypatch, tmp_path):
    good = {
        "ok": True,
        "coverage": {"num_statements": 3},
        "measured_files": ["pkg/module.py"],
        "never_imported": [],
    }
    measured = gate.coverage_measurement(["pkg"], good, good)
    assert measured["measured"] is True
    assert measured["statements"] == 3

    blind = gate.coverage_measurement(["missing"], good, good)
    assert blind["measured"] is False
    assert "no measured file belongs" in blind["reasons"][0]

    no_base = gate.hollow_check(tmp_path, None, ["tests"], "tests/test_new.py", 3)
    assert no_base["measured"] is False
    assert "no --base-ref" in no_base["reason"]

    probe_calls = []

    def fake_probe(argv, **kwargs):
        probe_calls.append(argv)
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "node_analysis": {"counts": {"nodes": 2}},
                    "hollow_nodes": [],
                    "node_verdict": "PASS",
                }
            )
        )

    monkeypatch.setattr(gate.subprocess, "run", fake_probe)
    graded = gate.hollow_check(tmp_path, "HEAD~1", ["tests"], "tests/test_new.py", 3)
    assert len(probe_calls) == 1
    argv = probe_calls[0]
    assert argv[argv.index("--base-ref") + 1] == "HEAD~1"
    assert argv[argv.index("--test-path") + 1] == "tests/test_new.py"
    assert graded == {
        "measured": True,
        "reason": None,
        "remedy": None,
        "hollow_nodes": [],
        "counts": {"nodes": 2},
        "node_verdict": "PASS",
    }


def test_run_gate_rejects_invalid_inputs_before_starting_any_command(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(gate, "command_report", lambda *args, **kwargs: started.append(args))
    assert gate.run_gate(tmp_path, ["pkg"], ["tests"], ["tests"], runs=0) == {
        "ok": False,
        "error": "--runs must be >= 1",
    }
    assert gate.run_gate(tmp_path / "missing", ["pkg"], ["tests"], ["tests"])["error"].startswith(
        "repo not found:"
    )
    assert gate.run_gate(tmp_path, [], ["tests"], ["tests"]) == {
        "ok": False,
        "error": "at least one --source is required",
    }
    assert started == []


def test_run_gate_assembles_a_measured_passing_result_without_running_real_commands(
    monkeypatch, tmp_path
):
    run = {"ok": True, "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
    baseline = {
        "ok": True,
        "coverage": {"covered_lines": 5},
        "run": run,
        "json": run,
        "error": None,
    }
    candidate = {**baseline, "coverage": {"covered_lines": 8}}
    monkeypatch.setattr(gate, "command_report", lambda *args, **kwargs: run)
    checks = iter([baseline, candidate])
    monkeypatch.setattr(gate, "coverage_check", lambda *args, **kwargs: next(checks))
    monkeypatch.setattr(
        gate, "reliability_check", lambda *args, **kwargs: {"all_passed": True, "runs_requested": 1}
    )
    monkeypatch.setattr(gate, "coverage_measurement", lambda *args: {"measured": True})
    monkeypatch.setattr(
        gate,
        "hollow_check",
        lambda *args: {"measured": True, "hollow_nodes": [], "counts": {"nodes": 1}},
    )

    result = gate.run_gate(
        tmp_path, ["pkg"], ["baseline"], ["candidate"], runs=1, base_ref="HEAD~1"
    )
    assert result["ok"] is True
    assert result["coverage_delta"] == {
        "covered_lines": 3,
        "min_required": 1,
        "baseline_covered_lines": 5,
        "candidate_covered_lines": 8,
    }
    assert result["could_not_measure"] == []


def test_main_passes_shell_split_arguments_to_the_gate(monkeypatch, capsys):
    captured = {}

    def fake_run_gate(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    assert (
        gate.main(
            [
                "--source",
                "pkg",
                "--baseline-pytest-args",
                "tests -k 'not slow'",
                "--candidate-pytest-args",
                "tests/test_candidate.py",
                "--reliability-pytest-args",
                "tests -k fast",
                "--runs",
                "2",
                "--min-covered-lines-delta",
                "3",
                "--timeout",
                "9",
                "--base-ref",
                "main",
                "--test-path",
                "tests/test_candidate.py",
            ]
        )
        == 0
    )
    assert captured["args"] == (
        ".",
        ["pkg"],
        ["tests", "-k", "not slow"],
        ["tests/test_candidate.py"],
    )
    assert captured["kwargs"] == {
        "reliability_pytest_args": ["tests", "-k", "fast"],
        "runs": 2,
        "min_covered_lines_delta": 3,
        "timeout": 9,
        "base_ref": "main",
        "test_path": "tests/test_candidate.py",
    }
    assert json.loads(capsys.readouterr().out) == {"ok": True}
