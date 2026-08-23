from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import capabilities
import execution_profiles
import feedback

NOW = int(time.time())


def _register(ledger: Path, *, minimum: int = 2) -> dict:
    return capabilities.register_compiled_version(
        "capability:causal-fixture",
        target_kind="workflow",
        artifact={"entrypoint": "fixture.py:run", "content": "v1"},
        lifecycle_policy={"min_independent_durable_reuse": minimum},
        record={
            "status": "canary",
            "owner": "orchestrator",
            "matcher": {"repository": "owner/repo", "task_types": ["implement"]},
            "entrypoint": "fixture.py:run",
            "trigger_cadence": "per matching task",
            "output_artifact": "fixture-result.json",
            "downstream_consumer": "dispatcher.py",
            "learning_sink": "feedback capability joins",
            "expiry": NOW + 86400,
            "activation_deadline": NOW + 86400,
            "next_transition": "active",
            "kill_switch": "ORCH_FIXTURE=0",
            "rollback": {"transition": "retired", "predecessor": "baseline"},
            "predecessor": "baseline",
        },
        path=ledger,
    )


def _episode(cap: dict, ordinal: int, profile_id: str, *, durability: str = "durable") -> str:
    run_id = f"work:{ordinal}"
    feedback.record_run(
        run_id,
        f"owner/repo#{ordinal}",
        "implement",
        "codex",
        routing_metadata={"subject_id": f"subject:{ordinal}"},
    )
    attempt_id = feedback.record_execution_attempt(
        run_id,
        attempt_id=f"attempt:profile:{ordinal}",
        operation_role="worker",
        profile_id=profile_id,
        requested_provider="openai",
        requested_model=profile_id,
        status="complete",
        source="selftest",
        completed_ts=NOW + ordinal,
    )
    feedback.record_capability_consumption(
        capability_id=cap["capability_id"],
        capability_version_id=cap["capability_version_id"],
        source_run_id=f"capability-output:{ordinal}",
        target_run_id=run_id,
        accepted=True,
        producer="selftest",
        attempt_id=attempt_id,
    )
    feedback.record_outcome(
        run_id,
        adjudicated_verdict="PASS",
        merged=True,
        durability=durability,
    )
    return attempt_id


def test_additive_capability_migration_preserves_existing_rows(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.executescript(feedback.SCHEMA)
        conn.execute(
            "INSERT INTO completion_events "
            "(event_id,schema_version,run_id,event_type,phase,producer,status,"
            "validation_status,payload_json,content_hash,redaction_count,created_ts,updated_ts) "
            "VALUES ('legacy-event',1,'legacy-run','completion','execution','selftest',"
            "'succeeded','accepted','{}','sha256:legacy',0,1,1)"
        )
        conn.execute(
            "INSERT INTO influence_edges "
            "(edge_id,schema_version,target_run_id,influence_type,influence_id,accepted,"
            "counterfactual,created_ts,metadata_hash) "
            "VALUES ('legacy-edge',1,'legacy-run','capability','legacy',1,0,1,'sha256:legacy')"
        )
        conn.execute("ALTER TABLE completion_events RENAME TO completion_events_new")
        conn.execute(
            "CREATE TABLE completion_events AS SELECT event_id,schema_version,run_id,attempt_id,"
            "event_type,phase,producer,status,validation_status,payload_json,content_hash,"
            "redaction_count,created_ts,updated_ts FROM completion_events_new"
        )
        conn.execute("DROP TABLE completion_events_new")
        conn.execute("ALTER TABLE influence_edges RENAME TO influence_edges_new")
        conn.execute(
            "CREATE TABLE influence_edges AS SELECT edge_id,schema_version,source_event_id,"
            "source_run_id,target_event_id,target_run_id,influence_type,influence_id,accepted,"
            "counterfactual,acceptance_gate_id,outcome_verdict,merged,durability,created_ts,"
            "propagated_ts,metadata_hash FROM influence_edges_new"
        )
        conn.execute("DROP TABLE influence_edges_new")
    with feedback._conn() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(completion_events)")}
        assert {"capability_id", "capability_version_id"} <= columns
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM completion_events WHERE event_id='legacy-event'"
            ).fetchone()[0]
            == 1
        )
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(influence_edges)")}
        assert {"capability_id", "capability_version_id"} <= edge_columns
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM influence_edges WHERE edge_id='legacy-edge'"
            ).fetchone()[0]
            == 1
        )


def test_event_identity_cannot_be_enriched_or_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    event = feedback.record_completion_event(
        "run-1", event_id="fixed", producer="selftest", status="complete"
    )
    assert event["event_id"] == "fixed"
    try:
        feedback.record_completion_event(
            "run-1",
            event_id="fixed",
            producer="selftest",
            status="complete",
            capability_id="capability:x",
            capability_version_id="capability-version:x",
        )
        raise AssertionError("nullable event identity was rewritten")
    except ValueError as exc:
        assert "immutable capability identity changed" in str(exc)


def test_consumption_requires_an_exact_target_event(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    ledger = tmp_path / "capabilities.json"
    cap = _register(ledger)
    with pytest.raises(ValueError, match="target has no completion event"):
        feedback.record_capability_consumption(
            capability_id=cap["capability_id"],
            capability_version_id=cap["capability_version_id"],
            source_run_id="capability-output:missing",
            target_run_id="work:missing",
            accepted=True,
            producer="selftest",
        )


def test_same_version_learns_across_profile_attempts_and_late_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    ledger = tmp_path / "capabilities.json"
    cap = _register(ledger)
    version_id = cap["capability_version_id"]
    first = _episode(cap, 1, "codex-5.6-sol-high")
    second = _episode(cap, 2, "codex-5.6-terra-high")

    promoted = capabilities.reconcile_causal_lifecycle(
        cap["capability_id"], path=ledger, timestamp=NOW + 10
    )
    assert promoted["status"] == "active"
    assert promoted["routing_prior"]["observations"] == 2
    assert promoted["readiness"]["durable_subjects"] == ["subject:1", "subject:2"]
    rows = feedback.capability_causal_evidence(cap["capability_id"], version_id)
    assert {tuple(row["profile_attempt_ids"]) for row in rows} == {(first,), (second,)}

    third = _episode(cap, 3, "codex-5.6-luna-high")
    refreshed = capabilities.reconcile_causal_lifecycle(
        cap["capability_id"], path=ledger, timestamp=NOW + 20
    )
    stored = capabilities.load(ledger, create=False)[cap["capability_id"]]
    assert refreshed["routing_prior"]["observations"] == 3
    assert stored["capability_version_id"] == version_id
    assert stored["routing_prior"]["observations"] == 3
    assert third in {
        attempt
        for row in feedback.capability_causal_evidence(cap["capability_id"], version_id)
        for attempt in row["profile_attempt_ids"]
    }
    transitions = [event for event in stored["event_history"] if event["type"] == "transition"]
    assert transitions[-1]["from"] == "canary" and transitions[-1]["to"] == "active"


def test_regression_is_join_derived_and_withholds_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    ledger = tmp_path / "capabilities.json"
    cap = _register(ledger, minimum=1)
    _episode(cap, 1, "codex-5.6-sol-high")
    capabilities.reconcile_causal_lifecycle(cap["capability_id"], path=ledger, timestamp=NOW + 5)
    feedback.record_outcome("work:1", durability="reworked")
    result = capabilities.reconcile_causal_lifecycle(
        cap["capability_id"], path=ledger, timestamp=NOW + 6
    )
    assert result["rollback_pending"]["evidence_ref"].startswith("edge:")
    records = capabilities.load(ledger, create=False)
    decision = capabilities.capability_routing_decision(
        {"repository": "owner/repo", "task_type": "implement", "lane": "opener"},
        capabilities_by_id=records,
        seed=7,
    )
    assert decision["selected_capability_id"] is None
    assert decision["rejection_reasons"][cap["capability_id"]] == ["rollback_pending"]


def test_profile_decision_attaches_only_real_matching_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    profile = execution_profiles.get_profile("codex-5.6-sol-high")
    envelope = execution_profiles.select_profile(
        "implement", "owner/repo#23", [profile["profile_id"]], rng_seed=4
    )
    feedback.record_profile_decision(envelope)
    feedback.record_run("profile-run", "owner/repo#23", "implement", "codex")
    attempt_id = feedback.record_execution_attempt(
        "profile-run",
        attempt_id="attempt:profile:profile-run",
        operation_role="worker",
        profile_id=profile["profile_id"],
        requested_provider=profile["provider"],
        requested_model=profile["requested_model"],
        status="started",
    )
    assert feedback.attach_profile_attempt_to_decision(envelope["decision_id"], attempt_id) == [
        attempt_id
    ]
    with feedback._conn() as conn:
        stored = conn.execute(
            "SELECT profile_attempt_ids_json FROM routing_decisions_v2 WHERE decision_id=?",
            (envelope["decision_id"],),
        ).fetchone()[0]
    assert json.loads(stored) == [attempt_id]
    other = execution_profiles.get_profile("codex-5.6-terra-high")
    other_attempt = feedback.record_execution_attempt(
        "profile-run",
        attempt_id="attempt:profile:wrong-profile",
        operation_role="worker",
        profile_id=other["profile_id"],
        requested_provider=other["provider"],
        requested_model=other["requested_model"],
        status="started",
    )
    with pytest.raises(ValueError, match="decision/profile attempt mismatch"):
        feedback.attach_profile_attempt_to_decision(envelope["decision_id"], other_attempt)


def test_pending_durability_is_not_a_failure_or_promotion_vote(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    ledger = tmp_path / "capabilities.json"
    cap = _register(ledger, minimum=1)
    run_id = "work:pending"
    feedback.record_run(run_id, "owner/repo#99", "implement", "codex")
    feedback.record_capability_consumption(
        capability_id=cap["capability_id"],
        capability_version_id=cap["capability_version_id"],
        source_run_id="capability-output:pending",
        target_run_id=run_id,
        accepted=True,
        producer="selftest",
    )
    feedback.record_outcome(run_id, adjudicated_verdict="PASS", merged=True)
    result = capabilities.reconcile_causal_lifecycle(
        cap["capability_id"], path=ledger, timestamp=NOW + 30
    )
    assert result["status"] == "canary"
    assert result["routing_prior"]["observations"] == 0
    assert result["readiness"]["failures"] == 0
