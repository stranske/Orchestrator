"""Hermetic coverage for feedback's evidence registry and run-query boundaries."""

from __future__ import annotations

import json

import pytest

import feedback


@pytest.fixture(autouse=True)
def isolated_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback.time, "time", lambda: 1_700_000_000)


def test_run_queries_keep_pending_and_mode_scopes_distinct():
    feedback.record_run("local-old", "owner/repo#1", "implement", "codex", mode="local", ts=10)
    feedback.record_run("remote-new", "owner/repo#1", "implement", "claude", mode="remote", ts=20)
    feedback.record_run("other", "owner/repo#2", "review", "codex", mode="local", ts=30)
    feedback.record_outcome("local-old", durability="durable")

    assert feedback.runs_needing_outcome() == [
        {"run_id": "remote-new", "target": "owner/repo#1"},
        {"run_id": "other", "target": "owner/repo#2"},
    ]
    assert feedback.runs_needing_outcome("local") == [{"run_id": "other", "target": "owner/repo#2"}]
    assert feedback.latest_run_id_for_target("owner/repo#1") == "remote-new"
    assert feedback.latest_run_id_for_target("owner/repo#1", "local") == "local-old"
    assert feedback.latest_run_id_for_target("missing") is None
    assert feedback.runs_for_target("owner/repo#1") == [
        {"run_id": "remote-new", "source": "orchestrator_remote", "ts": 20, "has_outcome": False},
        {"run_id": "local-old", "source": "orchestrator_local", "ts": 10, "has_outcome": True},
    ]


def test_snapshot_json_exports_initialized_tables_to_the_requested_path(tmp_path):
    feedback.record_run("snapshot", "owner/repo#3", "implement", "codex")

    output = tmp_path / "nested" / "snapshot.json"
    result = feedback.snapshot_json(output)

    exported = json.loads(output.read_text())
    assert result["path"] == str(output)
    assert result["rows"]["runs"] == 1
    assert exported["runs"][0]["run_id"] == "snapshot"
    assert {"completion_events", "evidence_types", "routing_decisions_v2"} <= exported.keys()


def test_evidence_registry_normalizes_citations_and_retires_unused_rows():
    assert feedback.normalize_evidence_type_citations(None) == []
    assert feedback.normalize_evidence_type_citations(
        [{"name": "  Test evidence  "}, {"type": "test   evidence"}, {}, "Other"]
    ) == ["Test evidence", "Other"]

    feedback.record_evidence_type("Test evidence", "verified by a focused test")
    feedback.record_evidence_type("Unused")
    assert feedback.record_evidence_type_citations(
        ["Test evidence", "missing", "test evidence"]
    ) == ["Test evidence"]
    feedback.bump_evidence_influence("Test evidence")

    assert feedback.prune_dead_evidence() == ["Unused"]
    assert feedback.active_evidence_types() == [
        {
            "name": "Test evidence",
            "added_ts": 1_700_000_000,
            "influence": 2,
            "rationale": "verified by a focused test",
        }
    ]


def test_evidence_proposals_and_approval_require_recurrence_then_reactivate():
    with pytest.raises(ValueError, match="name"):
        feedback.approve_evidence_type("", "gap")
    with pytest.raises(ValueError, match="gap"):
        feedback.approve_evidence_type("Evidence", "")

    feedback.record_evidence_gap("pr-1", "reviewer", "missing provenance")
    preview = feedback.approve_evidence_type("Provenance", "missing provenance", min_recurrence=2)
    assert preview["eligible"] is False
    assert preview["preview"] is True
    blocked = feedback.approve_evidence_type(
        "Provenance", "missing provenance", min_recurrence=2, apply=True
    )
    assert blocked["applied"] is False
    assert "recurrence 1 < min_recurrence 2" == blocked["blocked_reason"]

    feedback.record_evidence_gap("pr-2", "reviewer", "missing provenance")
    feedback.record_evidence_gap("pr-3", "reviewer", "missing provenance")
    assert feedback.propose_evidence_changes(min_recurrence=3) == [
        {
            "gap": "missing provenance",
            "recurrence": 3,
            "proposal": "add evidence type for: missing provenance",
        }
    ]
    approved = feedback.approve_evidence_type(
        "Provenance", "missing provenance", rationale="required for replay", apply=True
    )
    assert approved["applied"] is True
    assert approved["gaps_marked"] == 3
    assert approved["evidence_type_status"] == "active"
    assert feedback.propose_evidence_changes() == []

    assert feedback.prune_dead_evidence() == ["Provenance"]
    feedback.record_evidence_gap("pr-4", "reviewer", "missing provenance")
    feedback.record_evidence_gap("pr-5", "reviewer", "missing provenance")
    feedback.record_evidence_gap("pr-6", "reviewer", "missing provenance")
    reactivated = feedback.approve_evidence_type(
        "Provenance", "missing provenance", rationale="renewed", apply=True
    )
    assert reactivated["evidence_type_status"] == "reactivated"
    assert feedback.active_evidence_types()[0]["rationale"] == "renewed"


def test_role_selector_metrics_preserve_no_match_and_rejection_signals():
    with pytest.raises(ValueError, match="role_name"):
        feedback.record_role_selector_event("", "invoked", reason="missing")
    with pytest.raises(ValueError, match="invalid role selector"):
        feedback.record_role_selector_event("redirect", "unknown", reason="bad")

    feedback.record_run(
        "role-redirect", "owner/repo#4", "role:redirect", "codex", role_name="redirect"
    )
    feedback.record_role_selector_event(
        "redirect", "no_matching_work", reason="queue empty", target="owner/repo"
    )
    feedback.record_role_selector_event(
        "redirect",
        "matched_not_invoked",
        reason="capacity",
        target="owner/repo#4",
        matched=True,
    )
    feedback.record_role_selector_event(
        "redirect",
        "invoked",
        reason="dispatch",
        target="owner/repo#4",
        matched=True,
        invoked=True,
        accepted=True,
        disagreement=True,
    )
    feedback.record_role_selector_event(
        "redirect",
        "invoked",
        reason="declined",
        target="owner/repo#5",
        matched=True,
        invoked=True,
        accepted=False,
    )

    redirect = feedback.role_activation_metrics()["roles"]["redirect"]
    assert redirect == {
        "matched": 3,
        "invoked": 2,
        "accepted": 1,
        "rejected": 1,
        "disagreement": 1,
        "no_matching_work": 1,
        "matched_not_invoked": 1,
        "role_runs": 1,
        "linked": 0,
        "durable": 0,
        "evidence_readiness": "collecting",
        "profile_fit": "insufficient_role_runs",
    }


def test_model_epoch_cost_telemetry_and_review_rounds_are_isolated(tmp_path):
    epochs = tmp_path / "epochs.json"
    epochs.write_text(json.dumps({"epochs": {"codex": {"since": "2026-01-02T00:00:00Z"}}}))
    assert feedback.model_epoch_floor("codex", 1, epochs) == 1_767_312_000
    assert feedback.model_epoch_floor("missing", 7, epochs) == 7
    epochs.write_text("not json")
    assert feedback.model_epoch_floor("codex", 7, epochs) == 7

    feedback.record_run("complete", "owner/repo#7", "implement", "codex", ts=10)
    feedback.record_run("partial", "owner/repo#8", "implement", "codex", ts=11)
    feedback.record_cost("complete", cost_usd=2, tokens_in=1000, tokens_out=1000, source="ccusage")
    feedback.record_cost("partial", cost_usd=1, tokens_in=1000, tokens_out=1000, source="ledger")
    with feedback._conn() as conn:
        telemetry = feedback._cost_telemetry(conn, ["codex", "missing"], 0)
    assert telemetry["codex"]["valid"] is True
    assert telemetry["codex"]["complete_rows"] == 1
    assert telemetry["missing"]["coverage"] is None
    assert feedback._cost_coverage_note(telemetry["codex"]) == "cost_cov=50%"

    feedback.record_review_round("complete", "owner/repo#7", 1, "PASS", "codex", "abc")
    assert feedback.review_round_rows() == [("complete", "owner/repo#7", 1, "PASS", "codex", 1)]


def test_learning_population_and_cost_telemetry_reject_incomplete_or_implausible_data(monkeypatch):
    monkeypatch.setenv("ORCH_RELEARN_FLEET_ROWS", "0")
    assert feedback._learning_population() == "experimental"
    assert "='experimental'" in feedback._learning_assignment_sql()
    monkeypatch.setenv("ORCH_RELEARN_FLEET_ROWS", "1")
    assert feedback._learning_population() == "fleet"
    assert "'assigned'" in feedback._learning_assignment_sql()

    feedback.record_run("thin", "owner/repo#10", "implement", "cursor", ts=10)
    feedback.record_cost("thin", cost_usd=1, tokens_in=1, tokens_out=1, source="ccusage")
    feedback.record_run("partial", "owner/repo#11", "implement", "claude", ts=11)
    feedback.record_cost("partial", cost_usd=1, tokens_in=1000, tokens_out=1000, source="ledger")
    with feedback._conn() as conn:
        telemetry = feedback._cost_telemetry(conn, ["cursor", "claude"], 0)
    assert telemetry["cursor"]["valid"] is False
    assert "implausible" in telemetry["cursor"]["reason"]
    assert telemetry["claude"]["valid"] is False
    assert "no complete-source" in telemetry["claude"]["reason"]


def test_owner_questions_and_work_item_subject_preserve_scope_and_expiry():
    assert feedback.work_item_subject(" owner/repo#9 ") == "owner/repo#9"
    assert feedback.work_item_subject("owner/repo") is None
    with pytest.raises(ValueError):
        feedback.record_owner_question("", "continue")

    first = feedback.record_owner_question("Ship?", "hold", repo="owner/repo", expires_days=0)
    assert first["deduped"] is False
    assert feedback.record_owner_question("Ship?", "hold", repo="owner/repo")["deduped"] is True
    other = feedback.record_owner_question("Ship?", "proceed", repo="other/repo", expires_days=0)
    assert other["deduped"] is False
    assert feedback.open_owner_questions()[0]["question"] == "Ship?"
    assert feedback.expire_owner_questions(1_700_000_001) == 2
    assert feedback.owner_decisions_for(repo="owner/repo") == [
        {"question": "Ship?", "decision": "hold", "source": "default_ratified"}
    ]
    assert feedback.owner_decisions_for(repo="other/repo") == [
        {"question": "Ship?", "decision": "proceed", "source": "default_ratified"}
    ]
