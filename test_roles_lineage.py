from __future__ import annotations

import json
from pathlib import Path

import pytest

import adapters
import dispatcher
import epic_lane
import feedback
import roles


@pytest.fixture()
def isolated_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "feedback" / "roles.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    roles.reset_role_invocation_counts()
    return db


def _proposals() -> dict[str, dict]:
    triage_item = {"target": "owner/repo#1", "lane": "opener", "task_type": "implement"}
    return {
        "redirect": {
            "action": "redirect", "reason": "stale credential", "confidence": "high",
            "corrected_prompt": "Retry the bounded change, validate it, and report evidence.",
            "switch_agent": None,
        },
        "prompt": {
            "task_type": "implement", "summary": "Implement the bounded fix.",
            "scoped_prompt": (
                "Implement owner/repo#1 only. Meet the acceptance criteria, validate with pytest, "
                "then commit, push, and open or update the PR with evidence."
            ),
            "definition_of_done": ["The bounded behavior works"],
            "acceptance_criteria": ["Focused test passes"], "validation": ["pytest -q"],
            "expected_paths": ["src/example.py"], "out_of_scope": ["No unrelated refactor"],
            "risk_flags": ["Preserve compatibility"], "confidence": "high",
        },
        "decomposer": epic_lane._valid_plan(),
        "triage": {
            "summary": "One bounded item is ready.",
            "recommendations": [{
                "target": triage_item["target"], "action": "work_now", "priority": 1,
                "reason": "The item is bounded.", "batch_id": None,
            }],
            "batches": [], "global_risks": [], "confidence": "high",
        },
        "adjudicator": {
            "decision": "reject_blocker", "confidence": "high",
            "rationale": "Persisted runtime evidence contradicts the review claim.",
            "evidence_assessment": [{
                "claim": "runtime evidence is missing", "status": "contradicted",
                "evidence_ref": "runtime-ac:1", "reason": "The persisted check passed.",
            }],
            "ground_truth_refs": ["runtime-ac:1"],
            "recommended_next_step": "Inspect runtime-ac:1 and the review note.",
            "evidence_gaps": [],
        },
    }


def _invoke(role_name: str) -> dict:
    proposal = _proposals()[role_name]
    common = {"backend": "codex", "dispatch": True, "proposal_json": proposal}
    if role_name == "redirect":
        return roles.run_redirect_agent(
            {"target": "owner/repo#1", "state": "stalled", "recommended_action": "inspect"},
            "Focused test passes", **common,
        )
    if role_name == "prompt":
        return roles.run_prompt_agent(
            target="owner/repo#1", goal="Implement the bounded fix", task_type="implement", **common,
        )
    if role_name == "decomposer":
        return roles.run_decomposer_agent(goal="Deliver the bounded epic", target="owner/repo#1", **common)
    if role_name == "triage":
        return roles.run_triage_agent(
            backlog_items=[{"target": "owner/repo#1", "lane": "opener", "task_type": "implement"}],
            **common,
        )
    return roles.run_adjudicator_agent(
        case={
            "case_type": "runtime_ac", "target": "owner/repo#1",
            "disputed_finding": "runtime evidence is missing",
            "ground_truth_evidence": [{"ref": "runtime-ac:1", "status": "PASS"}],
        },
        **common,
    )


@pytest.mark.parametrize("role_name", list(roles.ROLE_REGISTRY))
def test_each_role_valid_proposal_auto_mirrors_downstream_outcome(
    isolated_feedback: Path, role_name: str
) -> None:
    result = _invoke(role_name)
    assert result["proposal"] is not None and not result["errors"], result
    role_run_id = result["role_run_id"]
    assert role_run_id
    downstream = f"work:{role_name}"
    feedback.record_run(
        downstream, "owner/repo#1", "implement", "cursor",
        influenced_by_role_run_ids=[role_run_id],
    )
    feedback.record_outcome(
        downstream, adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    with feedback._conn() as conn:
        mirrored = conn.execute(
            "SELECT adjudicated_verdict,durability FROM outcomes WHERE run_id=?",
            (role_run_id,),
        ).fetchone()
    assert mirrored == ("PASS", "durable")


def test_accepted_role_outcome_auto_links(
    isolated_feedback: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke("prompt")
    role_run_id = result["role_run_id"]
    dispatch = {
        "run_id": "dispatch:accepted-proposal", "agent": "cursor", "mode": "composer",
        "target": "owner/repo#1", "lane": "opener", "task_type": "implement",
        "model": "cursor-auto", "cwd": str(tmp_path), "wrapped": "true",
        "influenced_by_role_run_ids": [role_run_id],
    }

    class FakeProcess:
        pid = 4242

    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(dispatcher.claims, "update_metadata", lambda *args, **kwargs: True)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "ledger.ndjson")
    dispatcher._spawn(dispatch)
    feedback.record_outcome(
        dispatch["run_id"], adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    with feedback._conn() as conn:
        mirrored = conn.execute(
            "SELECT adjudicated_verdict,durability FROM outcomes WHERE run_id=?",
            (role_run_id,),
        ).fetchone()
    assert mirrored == ("PASS", "durable"), "accepted role outcome was not mirrored"


def test_rejected_role_records_disagreement_without_copied_success(
    isolated_feedback: Path,
) -> None:
    invalid = _proposals()["prompt"]
    invalid = {**invalid, "validation": []}
    result = roles.run_prompt_agent(
        target="owner/repo#2", goal="vague", backend="codex", dispatch=True,
        proposal_json=invalid,
    )
    assert result["role_run_id"] and result["proposal"] is None and result["errors"]
    feedback.record_run("work:rejected-role", "owner/repo#2", "implement", "cursor")
    feedback.record_influence_edge(
        target_run_id="work:rejected-role", influence_type="role",
        influence_id=result["role_run_id"], source_run_id=result["role_run_id"],
        accepted=False, metadata={"status": "rejected", "disagreement": True},
    )
    feedback.record_role_selector_event(
        "prompt", "invoked", reason="underspecified", target="owner/repo#2",
        matched=True, invoked=True, accepted=False, disagreement=True,
        role_run_id=result["role_run_id"],
    )
    feedback.record_outcome(
        "work:rejected-role", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    with feedback._conn() as conn:
        edge = conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict FROM influence_edges "
            "WHERE target_run_id='work:rejected-role'"
        ).fetchone()
        copied = conn.execute(
            "SELECT 1 FROM outcomes WHERE run_id=?", (result["role_run_id"],)
        ).fetchone()
    assert edge == (0, 1, None)
    assert copied is None
    metrics = feedback.role_activation_metrics()
    assert metrics["roles"]["prompt"]["rejected"] >= 1
    assert metrics["roles"]["prompt"]["disagreement"] >= 1


def test_selector_distinguishes_no_match_from_gate_capacity_and_cap(
    isolated_feedback: Path,
) -> None:
    no_match = roles.select_role_activation(
        "triage", matched=False, gate_enabled=True, capacity_available=True,
        reason="empty_backlog",
    )
    gate = roles.select_role_activation(
        "prompt", matched=True, gate_enabled=False, capacity_available=True,
        reason="underspecified",
    )
    capacity = roles.select_role_activation(
        "decomposer", matched=True, gate_enabled=True, capacity_available=False,
        reason="epic_lane",
    )
    invoked = roles.select_role_activation(
        "adjudicator", matched=True, gate_enabled=True, capacity_available=True,
        reason="persisted_evidence_disagreement", max_invocations=1,
    )
    capped = roles.select_role_activation(
        "adjudicator", matched=True, gate_enabled=True, capacity_available=True,
        reason="persisted_evidence_disagreement", max_invocations=1,
    )
    assert no_match["selector_status"] == "no_matching_work"
    assert gate["selector_status"] == "matched_not_invoked" and gate["reason"] == "shadow_gate_disabled"
    assert capacity["selector_status"] == "matched_not_invoked" and capacity["reason"] == "no_role_capacity"
    assert invoked["selector_status"] == "invoked"
    assert capped["selector_status"] == "matched_not_invoked" and capped["reason"] == "per_cycle_invocation_cap"


def test_redirect_plan_carries_accepted_role_id(isolated_feedback: Path) -> None:
    result = _invoke("redirect")
    command = result["plan"]["steps"][-1]["commands"][0]
    idx = command.index("--influenced-by-role-run-id")
    assert command[idx + 1] == result["role_run_id"]

