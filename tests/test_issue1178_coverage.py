"""Tests for issue #1178 - covering uncovered lines in rail_exercise.py and exploration_evidence_plan.py.

This file contains hermetic tests for uncovered statements and branches identified in issue #1178.
The tests are isolated from live runtime state and do not start external processes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import rail_exercise


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
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
    
    # Prevent subprocess calls
    started = []
    def forbid(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("Tests must not start external processes")
    
    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    yield
    assert started == [], f"Subprocess was started: {started}"


class TestRailExerciseContracts:
    """Cover lines 70-72 and 73 in rail_exercise.contracts()."""

    def test_contracts_with_invalid_json(self, tmp_path):
        """Test contracts() handles invalid JSON gracefully (covers lines 70-72)."""
        contract_dir = tmp_path / "test_contract"
        contract_dir.mkdir()
        contract_file = contract_dir / "contract.json"
        contract_file.write_text("not valid json")
        
        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file]):
            result = rail_exercise.contracts()
            assert len(result) == 1
            assert result[0][0] == contract_file
            assert "_load_error" in result[0][1]
            assert result[0][1]["capability_id"] == "test_contract"

    def test_contracts_with_oserror(self, tmp_path):
        """Test contracts() handles OSError gracefully (covers lines 70-72)."""
        contract_file = tmp_path / "nonexistent" / "contract.json"
        # File doesn't exist, will raise OSError when read
        
        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file]):
            result = rail_exercise.contracts()
            assert len(result) == 1
            assert "_load_error" in result[0][1]

    def test_contracts_only_filter(self, tmp_path):
        """Test contracts() filters by only parameter (covers line 73)."""
        contract_dir = tmp_path / "test_contract"
        contract_dir.mkdir()
        contract_file = contract_dir / "contract.json"
        contract_file.write_text('{"capability_id": "match", "run": "true"}')
        
        contract_dir2 = tmp_path / "test_contract2"
        contract_dir2.mkdir()
        contract_file2 = contract_dir2 / "contract.json"
        contract_file2.write_text('{"capability_id": "nomatch", "run": "true"}')
        
        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file, contract_file2]):
            result = rail_exercise.contracts(only="match")
            assert len(result) == 1
            assert result[0][1]["capability_id"] == "match"

    def test_contracts_valid_json(self, tmp_path):
        """Test contracts() loads valid JSON correctly."""
        contract_dir = tmp_path / "test_contract"
        contract_dir.mkdir()
        contract_file = contract_dir / "contract.json"
        contract_file.write_text('{"capability_id": "test", "run": "true"}')
        
        with patch.object(rail_exercise, "contract_paths", return_value=[contract_file]):
            result = rail_exercise.contracts()
            assert len(result) == 1
            assert result[0][0] == contract_file
            assert result[0][1]["capability_id"] == "test"

    def test_contracts_empty_payload(self):
        """Test contracts() with empty payload."""
        with patch.object(rail_exercise, "contract_paths", return_value=[]):
            result = rail_exercise.contracts()
            assert result == []


class TestRailExerciseContractPaths:
    """Cover lines 117-118 in rail_exercise.contract_paths()."""

    def test_contract_paths_with_nonexistent_root(self, tmp_path):
        """Test contract_paths() with non-existent root (covers lines 117-118)."""
        nonexistent = tmp_path / "nonexistent"
        with patch.object(rail_exercise, "CONTRACT_ROOT", nonexistent):
            result = rail_exercise.contract_paths()
            assert result == []

    def test_contract_paths_with_existing_root(self, tmp_path):
        """Test contract_paths() with existing root."""
        root = tmp_path / "contracts"
        root.mkdir()
        with patch.object(rail_exercise, "CONTRACT_ROOT", root):
            result = rail_exercise.contract_paths()
            assert isinstance(result, list)


class TestRailExerciseExpand:
    """Cover lines 117-118 and 125 in rail_exercise._expand()."""

    def test_expand_with_invalid_base64(self, tmp_path):
        """Test _expand() with invalid base64 (covers lines 117-118)."""
        # Command with invalid base64 encoded content
        command = 'echo "test" && echo "b64:invalid=="'
        contract_dir = tmp_path / "test"
        contract_dir.mkdir()
        fixture_dir = contract_dir / "fixtures"
        fixture_dir.mkdir()
        
        result = rail_exercise._expand(command, contract_dir, fixture_dir)
        # Should return original command when decode fails
        assert result == command

    def test_expand_with_valid_base64_but_no_replacement(self, tmp_path):
        """Test _expand() with valid base64 but no replacement needed (covers line 125)."""
        import base64
        payload = 'echo "test"'
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        command = f'echo "b64:{encoded}"'
        contract_dir = tmp_path / "test"
        contract_dir.mkdir()
        fixture_dir = contract_dir / "fixtures"
        fixture_dir.mkdir()
        
        result = rail_exercise._expand(command, contract_dir, fixture_dir)
        # Should return original command when no replacement pattern found
        assert result == command


class TestRailExerciseRun:
    """Cover lines 149-150 in rail_exercise._run()."""

    def test_run_with_timeout(self, tmp_path):
        """Test _run() handles timeout (covers lines 149-150)."""
        # This is hard to test without actually running subprocess
        # We'll mock subprocess.run to raise TimeoutExpired
        import subprocess as sp
        
        contract_dir = tmp_path / "contract"
        contract_dir.mkdir()
        fixture_dir = tmp_path / "fixtures"
        fixture_dir.mkdir()
        
        with patch("subprocess.run", side_effect=sp.TimeoutExpired("test", 1)):
            result = rail_exercise._run("echo test", contract_dir=contract_dir, fixture_dir=fixture_dir, env={})
            assert len(result) == 1
            assert result[0]["rc"] == 124
            assert "timeout" in result[0]["output"]

    def test_run_normal(self, tmp_path):
        """Test _run() with normal execution."""
        from subprocess import CompletedProcess
        
        contract_dir = tmp_path / "contract"
        contract_dir.mkdir()
        fixture_dir = tmp_path / "fixtures"
        fixture_dir.mkdir()
        
        mock_result = CompletedProcess(args=[], returncode=0, stdout="success", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = rail_exercise._run("echo test", contract_dir=contract_dir, fixture_dir=fixture_dir, env={})
            assert len(result) == 1
            assert result[0]["rc"] == 0


class TestRailExerciseBreakOk:
    """Cover line 173 in rail_exercise._break_ok()."""

    def test_break_ok_expected_marker_present(self):
        """Test _break_ok() with expected marker present (covers line 173)."""
        case = {"expected": "MARKER"}
        result = [{"output": "some output MARKER more output", "rc": 0}]
        check = []
        
        ok, reason = rail_exercise._break_ok(case, result, check)
        assert ok is True
        assert "expected marker present" in reason


class TestRailExerciseRunContract:
    """Cover lines 190 and 197 in rail_exercise.run_contract()."""

    def test_run_contract_with_load_error(self, tmp_path):
        """Test run_contract() with load error (covers line 190)."""
        path = tmp_path / "contract.json"
        contract = {"_load_error": "test error"}
        
        result = rail_exercise.run_contract(path, contract)
        assert result["status"] == "skip"
        assert result["reason"] == "test error"

    def test_run_contract_with_skip_reason(self, tmp_path):
        """Test run_contract() with skip_reason (covers line 197)."""
        path = tmp_path / "contract.json"
        contract = {"capability_id": "test", "skip_reason": "test skip"}
        
        result = rail_exercise.run_contract(path, contract)
        assert result["status"] == "skip"
        assert result["reason"] == "test skip"


class TestRailExerciseReport:
    """Cover lines 320, 329, 343-344 in rail_exercise.report()."""

    def test_report_with_missing_phase(self, tmp_path):
        """Test report() with missing phase (covers line 320)."""
        # Mock contracts to return empty
        with patch.object(rail_exercise, "contracts", return_value=[]):
            with patch("capability_advisor.advise", return_value={}):
                result = rail_exercise.report()
                # The function should handle missing phase gracefully
                assert isinstance(result, dict)

    def test_report_with_no_experiment_id(self, tmp_path):
        """Test report() with no experiment id (covers line 329)."""
        with patch.object(rail_exercise, "contracts", return_value=[]):
            with patch("capability_advisor.advise", return_value={"experiment_id": None}):
                result = rail_exercise.report()
                assert isinstance(result, dict)

    def test_report_exception_in_advisor(self, tmp_path):
        """Test report() with exception in advisor (covers lines 343-344)."""
        with patch.object(rail_exercise, "contracts", return_value=[]):
            with patch("capability_advisor.advise", side_effect=Exception("advisor error")):
                result = rail_exercise.report()
                assert isinstance(result, dict)
                # The result should indicate failure
                assert any("failed" in str(v) for v in result.values())


class TestRailExerciseMain:
    """Cover lines 528-529, 546-547 in rail_exercise.main()."""

    def test_main_with_heartbeat_exception(self, tmp_path, monkeypatch):
        """Test main() with heartbeat exception (covers lines 528-529)."""
        import sys
        
        with patch.object(rail_exercise, "contracts", return_value=[]):
            with patch("capabilities.daily_heartbeat", side_effect=Exception("heartbeat failed")):
                with patch.object(sys, "argv", ["rail_exercise"]):
                    with patch("builtins.print"):
                        # Mock report to return a dict with required keys
                        with patch.object(rail_exercise, "report", return_value={"totals": {"contracts": 1}, "tree": "test"}):
                            result = rail_exercise.main()
                            assert isinstance(result, int)

    def test_main_with_second_heartbeat_exception(self, tmp_path, monkeypatch):
        """Test main() with second heartbeat exception (covers lines 546-547)."""
        import sys
        
        with patch.object(rail_exercise, "contracts", return_value=[]):
            with patch("capabilities.daily_heartbeat", side_effect=Exception("heartbeat failed")):
                with patch.object(sys, "argv", ["rail_exercise", "--record"]):
                    with patch("builtins.print"):
                        with patch.object(rail_exercise, "report", return_value={"totals": {"contracts": 1}, "tree": "test"}):
                            result = rail_exercise.main()
                            assert isinstance(result, int)
