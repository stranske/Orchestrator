from __future__ import annotations

import json
from pathlib import Path

import pytest

import adapters
import dispatcher
import feedback
import ledger_reconcile
import research_subjects


@pytest.fixture()
def isolated_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "feedback" / "orchestrator.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    return db


def accepted_role_dispatch_fixture(tmp_path: Path) -> dict:
    return {
        "run_id": "dispatch:accepted-role",
        "agent": "codex",
        "mode": "full",
        "target": "owner/repo#7",
        "lane": "opener",
        "task_type": "implement",
        "model": "gpt-5.6-codex",
        "cwd": str(tmp_path),
        "wrapped": "true",
        "influenced_by_role_run_ids": ["role:accepted"],
        "influenced_by_skill_event_ids": [],
        "influenced_by_workflow_ids": ["orchestrator-dispatch"],
        "capability_ids": ["feedback-store"],
        "acceptance_gate_ids": ["issue-7-ac"],
        "routing_metadata": {"selected_profile_id": "codex-sol"},
    }


def test_accepted_influence_auto_links_outcome(
    isolated_feedback: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feedback.record_role_run(
        "role:accepted", "redirect", "owner/repo#7", "cursor", action="redirect"
    )
    dispatch = accepted_role_dispatch_fixture(tmp_path)

    class FakeProcess:
        pid = 4242

    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(dispatcher.claims, "update_metadata", lambda *args, **kwargs: True)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch-logs")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    dispatcher._spawn(dispatch)
    feedback.record_outcome(
        dispatch["run_id"], adjudicated_verdict="PASS", merged=True, durability="durable"
    )

    with feedback._conn() as conn:
        edge = conn.execute(
            "SELECT outcome_verdict,durability FROM influence_edges "
            "WHERE target_run_id=? AND influence_type='role' AND accepted=1",
            (dispatch["run_id"],),
        ).fetchone()
        role_outcome = conn.execute(
            "SELECT adjudicated_verdict,durability FROM outcomes WHERE run_id='role:accepted'"
        ).fetchone()
    assert edge is not None and edge == (
        "PASS",
        "durable",
    ), "accepted influence has no outcome edge"
    assert role_outcome == ("PASS", "durable")


def test_rejected_role_is_counterfactual_without_mirrored_success(isolated_feedback: Path) -> None:
    feedback.record_role_run("role:rejected", "redirect", "owner/repo#8", "vibe")
    feedback.record_run("work:rejected", "owner/repo#8", "implement", "codex")
    feedback.record_influence_edge(
        target_run_id="work:rejected",
        influence_type="role",
        influence_id="role:rejected",
        source_run_id="role:rejected",
        accepted=False,
    )
    feedback.record_outcome(
        "work:rejected", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    with feedback._conn() as conn:
        edge = conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,durability FROM influence_edges "
            "WHERE target_run_id='work:rejected'"
        ).fetchone()
        mirrored = conn.execute("SELECT 1 FROM outcomes WHERE run_id='role:rejected'").fetchone()
    assert edge == (0, 1, None, None)
    assert mirrored is None


def test_redaction_bounds_and_nested_allowlists(isolated_feedback: Path) -> None:
    redacted = feedback.record_completion_event(
        "redaction-run",
        event_type="completion",
        phase="execution",
        producer="selftest",
        status="succeeded",
        payload={
            "raw_prompt": "do the thing",
            "token": "ghp_supersecret123456789",
            "command_ids": ["pytest"] * 100,
            "artifact_refs": [{"artifact_id": "gate", "raw_path": "/private/source"}],
        },
    )
    assert redacted["validation_status"] == "redacted"
    assert redacted["bytes"] <= feedback.MAX_COMPLETION_EVENT_BYTES
    with feedback._conn() as conn:
        payload_text = conn.execute(
            "SELECT payload_json FROM completion_events WHERE event_id=?",
            (redacted["event_id"],),
        ).fetchone()[0]
    assert "supersecret" not in payload_text and "do the thing" not in payload_text
    assert "redacted_fields" in payload_text

    rejected = feedback.record_completion_event(
        "nested-reject",
        event_type="completion",
        phase="execution",
        producer="selftest",
        payload={"delivery": {"merged": True, "raw_output": "proprietary"}},
    )
    assert rejected["validation_status"] == "rejected"


def test_exact_enums_are_enforced(isolated_feedback: Path) -> None:
    with pytest.raises(ValueError, match="invalid completion event_type"):
        feedback.record_completion_event(
            "bad-type", event_type="made_up", phase="execution", producer="selftest"
        )
    with pytest.raises(ValueError, match="invalid completion phase"):
        feedback.record_completion_event(
            "bad-phase", event_type="completion", phase="invocation", producer="selftest"
        )


def test_edge_rerecord_preserves_propagated_terminal_state(isolated_feedback: Path) -> None:
    feedback.record_run(
        "source-role", "owner/repo#9", "role:redirect", "cursor", role_name="redirect"
    )
    feedback.record_run("target-work", "owner/repo#9", "implement", "codex")
    kwargs = {
        "target_run_id": "target-work",
        "influence_type": "role",
        "influence_id": "source-role",
        "source_run_id": "source-role",
        "accepted": True,
    }
    feedback.record_influence_edge(**kwargs)
    feedback.record_outcome(
        "target-work", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    feedback.record_influence_edge(**kwargs)
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT outcome_verdict,merged,durability,propagated_ts FROM influence_edges"
        ).fetchone()
    assert row[:3] == ("PASS", 1, "durable") and row[3] is not None


def test_completion_episode_shared_envelope(isolated_feedback: Path) -> None:
    feedback.record_run(
        "episode-run",
        "Owner/Repo#10",
        "implement",
        "codex",
        routing_metadata={
            "spec_hash": "a" * 64,
            "base_sha": "b" * 40,
            "profile_id": "codex-sol",
        },
    )
    feedback.record_execution_attempt(
        "episode-run",
        attempt_id="worker:episode-run",
        operation_role="worker",
        profile_id="codex-sol",
        resolved_provider="openai",
        resolved_model="gpt-5.6-codex",
        status="success",
    )
    feedback.record_completion_event(
        "episode-run",
        event_type="completion",
        phase="artifact",
        producer="dispatcher",
        status="merged",
        payload={
            "artifact_refs": [
                {
                    "artifact_id": "github-pr:owner/repo#10",
                    "kind": "github-pr",
                    "content_hash": "9" * 64,
                }
            ],
            "delivery": {"merged": True, "target_id": "Owner/Repo#10", "task_type": "implement"},
        },
    )
    episode = next(
        row for row in feedback.completion_event_episodes() if row["event"]["phase"] == "artifact"
    )
    assert episode["schema"] == "orchestrator.completion-event-envelope"
    assert episode["version"] == 1
    assert episode["event"]["validation_status"] == "accepted"
    assert episode["event"]["payload"]["delivery"]["merged"] is True
    assert episode["identity"]["observation_id"].startswith("sha256:")
    assert episode["identity"]["observation_id"] != episode["event"]["event_id"]
    assert episode["identity"]["normalized_spec_hash"] == "sha256:" + "a" * 64
    assert episode["identity"]["resolved_provider"] == "openai"
    assert episode["identity"]["resolved_model"] == "gpt-5.6-codex"
    assert episode["identity"]["attempt_id"] == "attempt:worker:episode-run"
    assert episode["identity"]["attempt_resolution"] == "resolved"
    assert {"created_ts", "updated_ts"}.issubset(episode["event"])
    assert "created_at" not in episode["event"] and "occurred_at" not in episode["event"]
    assert episode["identity_complete"] and episode["provenance_complete"]
    expected_subject = research_subjects.subject_identity_from_hash(
        "Owner/Repo#10",
        "implement",
        "a" * 64,
        "b" * 40,
        [],
        ["codex-sol"],
    )
    assert episode["identity"]["subject_id"] == expected_subject["subject_id"]
    assert episode["identity"]["family_id"] == expected_subject["subject_family_id"]
    assert episode["identity"]["subject_arms"] == []
    assert episode["identity"]["subject_profiles"] == ["codex-sol"]
    assert episode["identity"]["observation_id"] == research_subjects.completion_observation_id(
        expected_subject["subject_id"], "episode-run", "attempt:worker:episode-run"
    )
    assert {
        "schema_version",
        "event_id",
        "run_id",
        "attempt_id",
        "event_type",
        "phase",
        "producer",
        "status",
        "validation_status",
        "content_hash",
        "redaction_count",
        "created_ts",
        "updated_ts",
        "payload",
    }.issubset(episode["event"])
    assert {
        "run_id",
        "observation_id",
        "subject_id",
        "family_id",
        "canonical_target",
        "repository",
        "task_type",
        "normalized_spec_hash",
        "base_sha",
        "profile_id",
        "arm_id",
        "attempt_id",
        "attempt_resolution",
        "resolved_provider",
        "resolved_model",
        "subject_arms",
        "subject_profiles",
    }.issubset(episode["identity"])


def test_retries_on_one_subject_collapse_to_one_subject(
    isolated_feedback: Path,
) -> None:
    with feedback._conn() as conn:
        research_subjects.ensure_schema(conn)
        identity = research_subjects.subject_identity(
            "Owner/Repo#11",
            "testgen",
            "normalized spec",
            "c" * 40,
            ["codex", "cursor"],
            {"codex": "codex-sol"},
        )
        research_subjects.record_subject(
            identity, lifecycle="active", exp_id="retry-exp", conn=conn
        )
    for ordinal in (1, 2):
        run_id = f"retry-exp:codex:retry-{ordinal}"
        feedback.record_run(
            run_id,
            "Owner/Repo#11",
            "testgen",
            "codex",
            experiment_id="retry-exp",
        )
        feedback.record_execution_attempt(
            run_id,
            attempt_id=f"worker:{run_id}",
            operation_role="worker",
            profile_id="codex-sol",
            resolved_provider="openai",
            resolved_model="gpt-5.6-codex",
            status="success",
        )
        feedback.record_outcome(
            run_id, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
    episodes = [
        row
        for row in feedback.completion_event_episodes()
        if row["identity"]["experiment_id"] == "retry-exp"
    ]
    assert len({row["identity"]["run_id"] for row in episodes}) == 2
    assert {row["identity"]["subject_id"] for row in episodes} == {identity["subject_id"]}
    assert {row["identity"]["family_id"] for row in episodes} == {identity["subject_family_id"]}


def test_evaluator_model_never_resolves_worker_provenance(isolated_feedback: Path) -> None:
    feedback.record_run(
        "evaluator-only",
        "owner/repo#12",
        "implement",
        "codex",
        routing_metadata={
            "spec_hash": "d" * 64,
            "base_sha": "e" * 40,
            "profile_id": "judge-profile",
        },
    )
    feedback.record_execution_attempt(
        "evaluator-only",
        attempt_id="evaluator:evaluator-only",
        operation_role="evaluator",
        profile_id="judge-profile",
        resolved_provider="openai",
        resolved_model="gpt-5.6-codex",
        status="success",
    )
    feedback.record_outcome(
        "evaluator-only", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    episode = feedback.completion_event_episodes()[0]
    assert episode["identity"]["attempt_id"] is None
    assert episode["identity"]["attempt_resolution"] == "unresolved"
    assert episode["identity"]["resolved_model"] is None
    assert episode["identity_complete"]
    assert not episode["provenance_complete"]


def test_skill_protocol_and_health_metrics(isolated_feedback: Path) -> None:
    feedback.record_run("skill-target", "owner/repo#13", "implement", "codex")
    invocation = feedback.record_skill_invocation(
        "repo-audit",
        "skill-version-one",
        artifacts=[{"artifact_id": "audit-findings", "kind": "json", "content_hash": "f" * 64}],
        influenced_run_ids=["skill-target"],
        result="succeeded",
    )
    feedback.record_outcome(
        "skill-target", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    health = feedback.completion_event_health()
    assert invocation["edge_ids"]
    assert health["accepted_influence_linked"] == 1
    assert health["durable"] == 1
    assert health["orphan_edges"] == 0


def test_full_seven_phase_envelope_uses_one_canonical_attempt(isolated_feedback: Path) -> None:
    feedback.record_run(
        "seven-phase",
        "owner/repo#14",
        "implement",
        "codex",
        pr_number=14,
        routing_metadata={
            "normalized_spec_hash": "1" * 64,
            "base_sha": "2" * 40,
            "profile_id": "codex-terra",
        },
    )
    feedback.record_execution_attempt(
        "seven-phase",
        attempt_id="attempt:failed",
        attempt_ordinal=1,
        operation_role="worker",
        profile_id="codex-terra",
        resolved_provider="openai",
        resolved_model="gpt-5.6-codex",
        status="failed",
    )
    feedback.record_execution_attempt(
        "seven-phase",
        attempt_id="attempt:canonical",
        attempt_ordinal=2,
        operation_role="worker",
        profile_id="codex-terra",
        resolved_provider="openai",
        resolved_model="gpt-5.6-codex",
        status="success",
    )
    feedback.record_outcome(
        "seven-phase",
        verifier_verdict="PASS",
        adjudicated_verdict="PASS",
        merged=True,
        ci_status="success",
        durability="durable",
    )
    envelopes = [
        row
        for row in feedback.completion_event_episodes()
        if row["identity"]["run_id"] == "seven-phase"
    ]
    phases = {row["event"]["phase"] for row in envelopes}
    assert {
        "trigger",
        "decision",
        "execution",
        "artifact",
        "verification",
        "outcome",
        "durability",
    }.issubset(phases)
    assert len(envelopes) == len(phases), "exporter emitted duplicate canonical phases"
    assert {row["event"]["attempt_id"] for row in envelopes} == {"attempt:canonical"}
    assert len({row["identity"]["observation_id"] for row in envelopes}) == 1
    assert all(row["identity"]["retry_count"] == 1 for row in envelopes)


def test_attempt_only_does_not_manufacture_productive_artifact(isolated_feedback: Path) -> None:
    feedback.record_run("attempt-only", "owner/repo#15", "implement", "codex")
    feedback.record_execution_attempt(
        "attempt-only",
        attempt_id="attempt:only",
        operation_role="worker",
        profile_id="codex-sol",
        resolved_provider="openai",
        resolved_model="gpt-5.6-codex",
        status="success",
        trace_key="generic-lifecycle-trace",
    )
    envelopes = [
        row
        for row in feedback.completion_event_episodes()
        if row["identity"]["run_id"] == "attempt-only"
    ]
    assert "artifact" not in {row["event"]["phase"] for row in envelopes}


def test_multiple_successful_worker_attempts_export_ambiguous(isolated_feedback: Path) -> None:
    feedback.record_run(
        "ambiguous-worker",
        "owner/repo#16",
        "implement",
        "codex",
        routing_metadata={
            "normalized_spec_hash": "3" * 64,
            "base_sha": "4" * 40,
            "profile_id": "codex-sol",
        },
    )
    for ordinal in (1, 2):
        feedback.record_execution_attempt(
            "ambiguous-worker",
            attempt_id=f"attempt:success-{ordinal}",
            attempt_ordinal=ordinal,
            operation_role="worker",
            profile_id="codex-sol",
            resolved_provider="openai",
            resolved_model="gpt-5.6-codex",
            status="success",
        )
    envelope = feedback.completion_event_episodes()[0]
    assert envelope["identity"]["attempt_resolution"] == "ambiguous"
    assert envelope["identity"]["successful_attempt_count"] == 2
    assert not envelope["provenance_complete"]


def test_named_verifier_wins_over_generic_outcome_verification(isolated_feedback: Path) -> None:
    feedback.record_run("verified-run", "owner/repo#17", "implement", "codex")
    feedback.record_execution_attempt(
        "verified-run",
        attempt_id="attempt:verified",
        operation_role="worker",
        profile_id="codex-sol",
        resolved_provider="openai",
        resolved_model="gpt-5.6-codex",
        status="success",
    )
    feedback.record_outcome("verified-run", verifier_verdict="PASS", durability="durable")
    feedback.record_completion_event(
        "verified-run",
        event_type="verification",
        phase="verification",
        producer="local_verify",
        status="pass",
        payload={
            "acceptance_gate_ids": ["local-deliberate-break"],
            "verification": {
                "verifier_verdict": "PASS",
                "verifier_ids": ["local_verify"],
                "result_hashes": {"result": "5" * 64},
            },
        },
    )
    verification = [
        row
        for row in feedback.completion_event_episodes()
        if row["identity"]["run_id"] == "verified-run" and row["event"]["phase"] == "verification"
    ]
    assert len(verification) == 1
    assert verification[0]["event"]["producer"] == "local_verify"


def test_required_lineage_is_persisted_before_process_start(
    isolated_feedback: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatch = accepted_role_dispatch_fixture(tmp_path)
    popen_called = False

    def fake_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("process must not start")

    def fail_record(*args, **kwargs):
        raise OSError("feedback unavailable")

    monkeypatch.setattr(dispatcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(feedback, "record_run", fail_record)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch-logs")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    with pytest.raises(RuntimeError, match="refusing dispatch without required completion lineage"):
        dispatcher._spawn(dispatch)
    assert not popen_called
    telemetry = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
    assert telemetry[-1]["event"] == "telemetry_error"
    assert "feedback unavailable" not in json.dumps(telemetry[-1])


def test_legacy_telemetry_error_is_reconciled_into_decision(
    isolated_feedback: Path, tmp_path: Path
) -> None:
    ledger = tmp_path / "legacy-ledger.ndjson"
    ledger.write_text(
        json.dumps(
            {
                "ts": 100,
                "agent": "codex",
                "event": "telemetry_error",
                "run_id": "legacy-backfill",
                "target": "owner/repo#18",
                "task_type": "implement",
                "mode": "local",
                "reasoning_level": "full",
                "model": "gpt-5.6-codex",
                "error_hash": "6" * 64,
            }
        )
        + "\n"
    )
    summary = ledger_reconcile.reconcile(ledger)
    assert summary["telemetry_runs_backfilled"] == 1
    with feedback._conn() as conn:
        run = conn.execute(
            "SELECT target,task_type,agent,mode FROM runs WHERE run_id='legacy-backfill'"
        ).fetchone()
        decision = conn.execute(
            "SELECT 1 FROM completion_events WHERE run_id='legacy-backfill' AND phase='decision'"
        ).fetchone()
    assert run == ("owner/repo#18", "implement", "codex", "local")
    assert decision is not None
