"""Additional coverage for capability_recurrence_check.py uncovered check functions.

This module exercises the _check_* predicate functions that were not covered by the
initial test suite, focusing on the pure error paths and missing dependency handling.
"""

from __future__ import annotations

import pytest

import capability_recurrence_check as recurrence


@pytest.fixture(autouse=True)
def _hermetic_paths(tmp_path):
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setenv("ORCH_STATE_DIR", str(tmp_path))
        scoped.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
        scoped.setattr(recurrence.capabilities, "REG", tmp_path / "capabilities.json")
        yield


def test_check_rail_exercise_cadence_missing_module(monkeypatch):
    """_check_rail_exercise_cadence handles missing module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_rail(name, *a, **kw):
        if name == "rail_exercise":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_rail)
    result = recurrence._check_rail_exercise_cadence()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_route_weights_export_missing_module(monkeypatch):
    """_check_route_weights_export handles missing module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_route_weights(name, *a, **kw):
        if name == "route_weights_export":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_route_weights)
    result = recurrence._check_route_weights_export()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_reference_sync_gate_missing_modules(monkeypatch):
    """_check_reference_sync_gate handles missing modules gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_all(name, *a, **kw):
        if name in ("capabilities", "capability_outcome_bridge", "feedback"):
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_all)
    result = recurrence._check_reference_sync_gate()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_synthesis_gate_missing_module(monkeypatch):
    """_check_synthesis_gate handles missing capabilities module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_caps(name, *a, **kw):
        if name == "capabilities":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_caps)
    result = recurrence._check_synthesis_gate()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_model_trial_gate_missing_module(monkeypatch):
    """_check_model_trial_gate handles missing capabilities module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_caps(name, *a, **kw):
        if name == "capabilities":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_caps)
    result = recurrence._check_model_trial_gate()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_thompson_missing_router(monkeypatch):
    """_check_thompson handles missing router module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_router(name, *a, **kw):
        if name == "router":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_router)
    result = recurrence._check_thompson()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_thompson_missing_exploration_review(monkeypatch):
    """_check_thompson handles missing exploration_review module gracefully (no error key)."""
    import builtins

    real_import = builtins.__import__

    def block_exploration(name, *a, **kw):
        if name == "exploration_review":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    # First, mock router._exploration_mode to return epsilon-greedy
    import types

    mock_router = types.ModuleType("router")
    mock_router._exploration_mode = lambda: "epsilon-greedy"

    monkeypatch.setattr(builtins, "__import__", block_exploration)

    # Need to make sure router is available but exploration_review is blocked
    def conditional_import(name, *a, **kw):
        if name == "router":
            return mock_router
        if name == "exploration_review":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", conditional_import)
    result = recurrence._check_thompson()
    assert result["fires"] is False
    assert "review_unavailable" in result["detail"]
    assert "error" not in result["detail"]


def test_check_adjudicator_missing_feedback(monkeypatch):
    """_check_adjudicator handles missing feedback module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_feedback(name, *a, **kw):
        if name == "feedback":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_feedback)
    monkeypatch.setattr(recurrence, "_predicate_flag", lambda flag: {"fires": True, "detail": {}})
    result = recurrence._check_adjudicator()
    assert result["fires"] is False
    # When feedback is missing, the count defaults to 0
    assert result["detail"]["recorded_disagreements"] == 0


def test_check_keepalive_escalation_missing_module(monkeypatch):
    """_check_keepalive_escalation handles missing keepalive_supervisor module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_ks(name, *a, **kw):
        if name == "keepalive_supervisor":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_ks)
    result = recurrence._check_keepalive_escalation()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_gemini_isolation_missing_module(monkeypatch):
    """_check_gemini_isolation handles missing adapters module gracefully."""
    import builtins

    real_import = builtins.__import__

    def block_adapters(name, *a, **kw):
        if name == "adapters":
            raise RuntimeError("unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", block_adapters)
    result = recurrence._check_gemini_isolation()
    assert result["fires"] is False
    assert result["detail"]["error"] == "unavailable"


def test_check_value_chain_monitor_exercises_recurrence_fixture(monkeypatch):
    """_check_value_chain_monitor calls the module's recurrence_fixture."""
    import builtins
    import types

    real_import = builtins.__import__
    mock_module = types.ModuleType("value_chain_monitor")
    call_count = [0]

    def recurrence_fixture():
        call_count[0] += 1
        return True

    mock_module.recurrence_fixture = recurrence_fixture

    def conditional_import(name, *a, **kw):
        if name == "value_chain_monitor":
            return mock_module
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", conditional_import)
    result = recurrence._check_value_chain_monitor()
    assert result["fires"] is True
    assert call_count[0] == 1
    assert result["detail"]["proof"] == "independent demand and earliest-break fixture"


def test_classify_tick_env_covers_all_branches():
    """_classify_tick_env handles all outcome branches: ok, nonzero_exit, empty_output, no_orch_keys."""
    # OK path: exports exist
    resolved, rec = recurrence._classify_tick_env(0, "ORCH_A=1\nORCH_B=2\n", "")
    assert resolved == {"ORCH_A": "1", "ORCH_B": "2"}
    assert rec["reason"] == "ok"

    # Non-zero exit
    resolved, rec = recurrence._classify_tick_env(1, "some output\n", "error")
    assert resolved == {}
    assert rec["reason"] == "nonzero_exit"

    # Empty output
    resolved, rec = recurrence._classify_tick_env(0, "", "")
    assert resolved == {}
    assert rec["reason"] == "empty_output"

    # No ORCH_ keys
    resolved, rec = recurrence._classify_tick_env(0, "OTHER=1\n", "")
    assert resolved == {}
    assert rec["reason"] == "no_orch_keys"


def test_classify_tick_env_records_stderr_tail():
    """_classify_tick_env captures stderr tail for debugging."""
    resolved, rec = recurrence._classify_tick_env(1, "output", "line1\nline2\nline3\n")
    assert rec["reason"] == "nonzero_exit"
    assert rec["stderr_tail"] == ["line2", "line3"]


def test_classify_tick_env_empty_stderr():
    """_classify_tick_env handles empty stderr without error."""
    resolved, rec = recurrence._classify_tick_env(1, "output", "")
    assert rec["reason"] == "nonzero_exit"
    assert "stderr_tail" not in rec


def test_tick_env_failure_message_all_outcomes():
    """tick_env_failure_message covers all three outcome classes: defect, script_error, environment."""
    # Defect outcomes
    defect_no_keys = recurrence.tick_env_failure_message(
        {
            "reason": "no_orch_keys",
            "outcome": "defect",
            "attempts": [{"returncode": 0, "stdout_lines": 3, "keys": 0, "detail": "test"}],
        }
    )
    assert "DEFECT (tree)" in defect_no_keys

    defect_missing = recurrence.tick_env_failure_message(
        {
            "reason": "script_missing",
            "outcome": "defect",
            "attempts": [{"detail": "missing"}],
        }
    )
    assert "DEFECT (tree)" in defect_missing

    # Script error
    script_err = recurrence.tick_env_failure_message(
        {
            "reason": "nonzero_exit",
            "outcome": "script_error",
            "attempts": [{"returncode": 2}],
        }
    )
    assert "PROLOGUE ABORTED" in script_err

    # Environment
    env_err = recurrence.tick_env_failure_message(
        {
            "reason": "timeout",
            "outcome": "environment",
            "attempts": [{"detail": "slow"}],
        }
    )
    assert "ENVIRONMENT, not the tree" in env_err
