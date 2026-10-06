import json
from pathlib import Path

import pytest

import redirect_plan
import roles


@pytest.mark.parametrize("valid", [True, False])
def test_a_decompose_verdict_attaches_the_roles_plan_or_falls_back_with_a_reason(
    monkeypatch, valid
):
    roles.reset_role_invocation_counts()
    monkeypatch.setenv("ORCH_ROLE_SHADOW", "1")
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "0")
    calls = []
    fixture = Path(__file__).parent / "rail_exercises/redirect-plan/arm-a/fixtures/epic-valid.json"
    proposal = json.loads(fixture.read_text())
    if not valid:
        proposal["subtasks"][0]["dependencies"] = ["unknown"]

    def decompose(**kwargs):
        calls.append(kwargs)
        return {"proposal": proposal, "role_run_id": "role:decomposer:test:1", "errors": []}

    monkeypatch.setattr(roles, "run_decomposer_agent", decompose)
    monkeypatch.setattr(roles.feedback, "record_role_selector_event", lambda *a, **kw: None)
    rep = redirect_plan.plan(
        {"target": "o/r#1", "policy_decision": {"action": "decompose"}}, next_agent="codex"
    )
    assert len(calls) == 1 and calls[0]["dispatch"] is True
    assert "backend" not in calls[0], "role backend must remain router-chosen"
    assert rep["dry_run_only"] is True
    if valid:
        assert rep["decomposition"]["proposal"] == proposal
        assert json.dumps(proposal, indent=2) in rep["prompt_text"]
        assert rep["decomposition"]["role_run_id"] == "role:decomposer:test:1"
    else:
        assert rep["decomposition"]["proposal"] is None
        assert "unknown subtask" in rep["prompt_text"]
        assert "split the work into 2-3" in rep["prompt_text"]


def test_shadow_gate_and_cycle_cap_preserve_deterministic_fallback(monkeypatch):
    roles.reset_role_invocation_counts()
    monkeypatch.delenv("ORCH_ROLE_SHADOW", raising=False)
    calls = []
    monkeypatch.setattr(
        roles, "run_decomposer_agent", lambda **kw: calls.append(kw) or {"proposal": None}
    )
    rep = redirect_plan.plan(
        {"target": "o/r#1", "policy_decision": {"action": "decompose"}}, next_agent="codex"
    )
    assert calls[0]["dispatch"] is False
    assert "shadow_gate_disabled" in rep["prompt_text"]
    monkeypatch.setenv("ORCH_ROLE_SHADOW", "1")
    monkeypatch.setattr(roles.feedback, "record_role_selector_event", lambda *a, **kw: None)
    roles._ROLE_INVOCATION_COUNTS["decomposer"] = roles._role_cap(None, "decomposer")
    rep = redirect_plan.plan(
        {"target": "o/r#1", "policy_decision": {"action": "decompose"}}, next_agent="codex"
    )
    assert calls[-1]["dispatch"] is False
    assert "per_cycle_invocation_cap" in rep["prompt_text"]
    roles.reset_role_invocation_counts()


def test_decomposer_exception_preserves_deterministic_fallback(monkeypatch):
    roles.reset_role_invocation_counts()
    monkeypatch.setenv("ORCH_ROLE_SHADOW", "1")
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "0")
    monkeypatch.setattr(roles.feedback, "record_role_selector_event", lambda *a, **kw: None)

    def unavailable(**kwargs):
        raise RuntimeError("role unavailable")

    monkeypatch.setattr(roles, "run_decomposer_agent", unavailable)
    try:
        rep = redirect_plan.plan(
            {"target": "o/r#1", "policy_decision": {"action": "decompose"}}, next_agent="codex"
        )
        assert rep["decomposition"]["proposal"] is None
        assert "decomposer unavailable" in rep["prompt_text"]
        assert "split the work into 2-3" in rep["prompt_text"]
    finally:
        roles.reset_role_invocation_counts()
