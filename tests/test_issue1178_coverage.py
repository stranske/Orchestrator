"""Tests for issue #1178 - covering uncovered lines in rail_exercise.py.

Hermetic tests for uncovered statements and branches identified in issue #1178.
Check date: 2026-11-05. Outcome target: all retained tests pass and each detects its
targeted mutation. Command: ``python3 -m pytest -q --no-cov tests/test_issue1178_coverage.py``.
Stop rule: 3 attempts or 90 minutes, then report the first failing step.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import rail_exercise


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from live runtime state."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    handoff = tmp_path / "handoff"
    state.mkdir()
    runtime.mkdir()
    handoff.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(handoff))
    monkeypatch.delenv("ORCH_FEEDBACK_DB", raising=False)
    monkeypatch.delenv("ORCH_CAPABILITIES_PATH", raising=False)

    started: list[tuple[tuple, dict]] = []

    def forbid(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("Tests must not start external processes")

    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    yield
    assert started == [], f"Subprocess was started: {started}"


@pytest.fixture
def patched_exec_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep expansion and execution from building an external flat-mirror view."""
    root = tmp_path / "exec-root"
    root.mkdir()
    monkeypatch.setattr(rail_exercise, "_EXEC_ROOT", root)
    return root


class TestRailExerciseContracts:
    """Cover lines 70-72 and 73 in rail_exercise.contracts()."""

    def test_contracts_with_invalid_json(self, tmp_path: Path) -> None:
        contract_dir = tmp_path / "test_contract"
        contract_dir.mkdir()
        contract_file = contract_dir / "contract.json"
        contract_file.write_text("not valid json", encoding="utf-8")

        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file]):
            result = rail_exercise.contracts()
            assert len(result) == 1
            assert result[0][0] == contract_file
            assert "_load_error" in result[0][1]
            assert result[0][1]["capability_id"] == "test_contract"
            assert (
                "JSON" in result[0][1]["_load_error"] or "Expecting" in result[0][1]["_load_error"]
            )

    def test_contracts_with_oserror(self, tmp_path: Path) -> None:
        contract_file = tmp_path / "nonexistent" / "contract.json"

        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file]):
            result = rail_exercise.contracts()
            assert len(result) == 1
            assert result[0][0] == contract_file
            assert "_load_error" in result[0][1]
            assert result[0][1]["capability_id"] == "nonexistent"

    def test_contracts_only_filter(self, tmp_path: Path) -> None:
        contract_dir = tmp_path / "test_contract"
        contract_dir.mkdir()
        contract_file = contract_dir / "contract.json"
        contract_file.write_text('{"capability_id": "match", "run": "true"}', encoding="utf-8")

        contract_dir2 = tmp_path / "test_contract2"
        contract_dir2.mkdir()
        contract_file2 = contract_dir2 / "contract.json"
        contract_file2.write_text('{"capability_id": "nomatch", "run": "true"}', encoding="utf-8")

        with patch.object(
            rail_exercise, "contract_paths", return_value=[contract_file, contract_file2]
        ):
            result = rail_exercise.contracts(only="match")
            assert len(result) == 1
            assert result[0][1]["capability_id"] == "match"

    def test_contracts_valid_json(self, tmp_path: Path) -> None:
        contract_dir = tmp_path / "test_contract"
        contract_dir.mkdir()
        contract_file = contract_dir / "contract.json"
        contract_file.write_text('{"capability_id": "test", "run": "true"}', encoding="utf-8")

        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file]):
            result = rail_exercise.contracts()
            assert len(result) == 1
            assert result[0][0] == contract_file
            assert result[0][1]["capability_id"] == "test"
            assert "_load_error" not in result[0][1]

    def test_contracts_empty_payload(self) -> None:
        with patch.object(rail_exercise, "contract_paths", return_value=[]):
            assert rail_exercise.contracts() == []


class TestRailExerciseContractPaths:
    """Cover lines 117-118 in rail_exercise.contract_paths()."""

    def test_contract_paths_with_nonexistent_root(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nonexistent"
        with patch.object(rail_exercise, "CONTRACT_ROOT", nonexistent):
            assert rail_exercise.contract_paths() == []

    def test_contract_paths_with_existing_root(self, tmp_path: Path) -> None:
        root = tmp_path / "contracts"
        nested = root / "outer" / "inner"
        nested.mkdir(parents=True)
        contract_file = nested / "contract.json"
        contract_file.write_text('{"capability_id": "nested"}', encoding="utf-8")
        with patch.object(rail_exercise, "CONTRACT_ROOT", root):
            discovered = rail_exercise.contract_paths()
            assert discovered == [contract_file]


class TestRailExerciseExpand:
    """Cover lines 117-118 and 125 in rail_exercise._expand()."""

    def test_expand_with_invalid_base64(self, tmp_path: Path, patched_exec_root: Path) -> None:
        command = "python3 -c \"import base64; base64.b64decode('a')\""
        contract_dir = tmp_path / "test"
        contract_dir.mkdir()
        fixture_dir = contract_dir / "fixtures"
        fixture_dir.mkdir()

        result = rail_exercise._expand(command, contract_dir, fixture_dir)
        assert result == command

    def test_expand_with_valid_base64_but_no_replacement(
        self, tmp_path: Path, patched_exec_root: Path
    ) -> None:
        payload = "print('no fixture path rewrite here')"
        canonical = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        # Noncanonical extra padding still decodes to the same payload.
        encoded = canonical + "="
        command = f"python3 -c \"import base64; base64.b64decode('{encoded}')\""
        contract_dir = tmp_path / "test"
        contract_dir.mkdir()
        fixture_dir = contract_dir / "fixtures"
        fixture_dir.mkdir()

        result = rail_exercise._expand(command, contract_dir, fixture_dir)
        assert result == command
        assert str(patched_exec_root) not in command


class TestRailExerciseRun:
    """Cover lines 149-150 in rail_exercise._run()."""

    def test_run_with_timeout(self, tmp_path: Path, patched_exec_root: Path) -> None:
        import subprocess as sp

        contract_dir = tmp_path / "contract"
        contract_dir.mkdir()
        fixture_dir = tmp_path / "fixtures"
        fixture_dir.mkdir()

        with patch("subprocess.run", side_effect=sp.TimeoutExpired("test", 1)) as mocked:
            result = rail_exercise._run(
                "echo test", contract_dir=contract_dir, fixture_dir=fixture_dir, env={}
            )
            assert mocked.call_args.kwargs["cwd"] == patched_exec_root
            assert len(result) == 1
            assert result[0]["rc"] == 124
            assert result[0]["output"] == "timeout after 600 seconds"

    def test_run_normal(self, tmp_path: Path, patched_exec_root: Path) -> None:
        from subprocess import CompletedProcess

        contract_dir = tmp_path / "contract"
        contract_dir.mkdir()
        fixture_dir = tmp_path / "fixtures"
        fixture_dir.mkdir()

        mock_result = CompletedProcess(args=[], returncode=0, stdout="success", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mocked:
            result = rail_exercise._run(
                "echo test", contract_dir=contract_dir, fixture_dir=fixture_dir, env={}
            )
            assert mocked.call_args.kwargs["cwd"] == patched_exec_root
            assert len(result) == 1
            assert result[0]["rc"] == 0
            assert result[0]["output"] == "success"


class TestRailExerciseBreakOk:
    """Cover line 173 in rail_exercise._break_ok()."""

    def test_break_ok_expected_marker_present(self) -> None:
        case = {"expected": "MARKER"}
        result = [{"output": "some output MARKER more output", "rc": 0}]
        check: list[dict] = []

        ok, reason = rail_exercise._break_ok(case, result, check)
        assert ok is True
        assert reason == "expected marker present"


class TestRailExerciseRunContract:
    """Cover lines 190 and 197 in rail_exercise.run_contract()."""

    def test_run_contract_with_load_error(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.json"
        contract = {"_load_error": "test error", "capability_id": "ignored"}

        result = rail_exercise.run_contract(path, contract)
        assert result == {
            "capability_id": "ignored",
            "contract": str(path),
            "status": "skip",
            "reason": "test error",
        }

    def test_run_contract_with_skip_reason(self, tmp_path: Path) -> None:
        path = tmp_path / "contract.json"
        contract = {"capability_id": "test", "skip_reason": "test skip"}

        result = rail_exercise.run_contract(path, contract)
        assert result == {
            "capability_id": "test",
            "contract": str(path),
            "status": "skip",
            "reason": "test skip",
        }


class TestRailExerciseReport:
    """Cover lines 320, 329, 343-344 in rail_exercise.report()."""

    @staticmethod
    def _passing_row(path: Path) -> dict:
        return {
            "capability_id": "cap-x",
            "contract": str(path),
            "status": "pass",
            "reason": "ok",
        }

    def test_report_record_fails_when_rail_phase_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import capability_advisor as ca
        import capability_propensity as cp

        path = tmp_path / "cap" / "contract.json"
        row = self._passing_row(path)
        propensity: list[str] = []
        monkeypatch.setattr(rail_exercise, "run_contract", lambda _p, _c: row)
        monkeypatch.setattr(
            rail_exercise, "contracts", lambda only="": [(path, {"capability_id": "cap-x"})]
        )
        monkeypatch.setattr(ca, "surfaces_binding", lambda ids: {"cap-x": []})
        monkeypatch.setattr(
            cp, "record_trigger", lambda *a, **k: propensity.append("trigger") or True
        )
        monkeypatch.setattr(
            cp, "record_usefulness", lambda *a, **k: propensity.append("usefulness") or True
        )

        result = rail_exercise.report(record=True)
        assert result["recording"] is True
        assert result["contracts"][0]["record"] == (
            "record failed: expected one rail-exercise phase, found []"
        )
        assert propensity == []

    def test_report_record_fails_when_advisor_returns_no_experiment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import capability_advisor as ca
        import capability_propensity as cp

        path = tmp_path / "cap" / "contract.json"
        row = self._passing_row(path)
        propensity: list[str] = []
        monkeypatch.setattr(rail_exercise, "run_contract", lambda _p, _c: row)
        monkeypatch.setattr(
            rail_exercise, "contracts", lambda only="": [(path, {"capability_id": "cap-x"})]
        )
        monkeypatch.setattr(
            ca, "surfaces_binding", lambda ids: {"cap-x": ["rail-exercise:fixture"]}
        )
        monkeypatch.setattr(ca, "advise", lambda *a, **k: {"experiment_id": ""})
        monkeypatch.setattr(
            cp, "record_trigger", lambda *a, **k: propensity.append("trigger") or True
        )
        monkeypatch.setattr(
            cp, "record_usefulness", lambda *a, **k: propensity.append("usefulness") or True
        )

        result = rail_exercise.report(record=True)
        assert (
            result["contracts"][0]["record"] == "record failed: advisor returned no experiment id"
        )
        assert propensity == []

    def test_report_record_surfaces_advisor_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import capability_advisor as ca
        import capability_propensity as cp

        path = tmp_path / "cap" / "contract.json"
        row = self._passing_row(path)
        propensity: list[str] = []

        def broken_advise(*_args, **_kwargs):
            raise RuntimeError("advisor error")

        monkeypatch.setattr(rail_exercise, "run_contract", lambda _p, _c: row)
        monkeypatch.setattr(
            rail_exercise, "contracts", lambda only="": [(path, {"capability_id": "cap-x"})]
        )
        monkeypatch.setattr(
            ca, "surfaces_binding", lambda ids: {"cap-x": ["rail-exercise:fixture"]}
        )
        monkeypatch.setattr(ca, "advise", broken_advise)
        monkeypatch.setattr(
            cp, "record_trigger", lambda *a, **k: propensity.append("trigger") or True
        )
        monkeypatch.setattr(
            cp, "record_usefulness", lambda *a, **k: propensity.append("usefulness") or True
        )

        result = rail_exercise.report(record=True)
        assert result["contracts"][0]["record"] == "record failed: advisor error"
        assert propensity == []


class TestRailExerciseMain:
    """Cover lines 528-529, 546-547 in rail_exercise.main()."""

    @staticmethod
    def _one_contract_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        path = tmp_path / "cap" / "contract.json"
        monkeypatch.setattr(
            rail_exercise,
            "contracts",
            lambda only="": [(path, {"capability_id": "cap", "skip_reason": "hermetic"})],
        )

    def test_main_with_heartbeat_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[tuple[str, str]] = []

        def heartbeat(capability: str, event: str, **_kwargs: object) -> None:
            calls.append((capability, event))
            if event == "invocation":
                raise Exception("invocation heartbeat failed")

        self._one_contract_main(monkeypatch, tmp_path)
        monkeypatch.setattr(rail_exercise.capabilities, "daily_heartbeat", heartbeat)
        monkeypatch.setattr(sys, "argv", ["rail_exercise.py"])

        assert rail_exercise.main() == 0
        err = capsys.readouterr().err
        assert err == "rail_exercise: capability heartbeat failed: invocation heartbeat failed\n"
        assert calls == [
            ("rail-exercise-cadence", "invocation"),
            ("rail-exercise-cadence", "success"),
        ]

    def test_main_with_second_heartbeat_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[tuple[str, str]] = []

        def heartbeat(capability: str, event: str, **_kwargs: object) -> None:
            calls.append((capability, event))
            if event in {"success", "failure"}:
                raise Exception("verdict heartbeat failed")

        self._one_contract_main(monkeypatch, tmp_path)
        monkeypatch.setattr(rail_exercise.capabilities, "daily_heartbeat", heartbeat)
        monkeypatch.setattr(sys, "argv", ["rail_exercise.py"])

        assert rail_exercise.main() == 0
        err = capsys.readouterr().err
        assert err == "rail_exercise: capability heartbeat failed: verdict heartbeat failed\n"
        assert calls == [
            ("rail-exercise-cadence", "invocation"),
            ("rail-exercise-cadence", "success"),
        ]


def test_selftest_names_an_absent_committed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(command, **_kwargs):
        text = command if isinstance(command, str) else " ".join(command)
        return _Result(0 if text.strip() == "true" else 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    missing = tmp_path / "no-committed-tree"
    monkeypatch.setattr(rail_exercise, "CONTRACT_ROOT", missing)
    rail_exercise.selftest()
    assert f"committed tree absent at {missing} — not checked" in capsys.readouterr().out
