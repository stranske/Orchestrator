from __future__ import annotations

import hashlib
import json

import pytest

from completion_event_adapter import adapt_completion_event_envelope
from pattern_miner import PHASES, PatternMiner, main
import research_subjects


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def completion_episode(
    index: int,
    *,
    subject_id: str | None = None,
    observation_id: str | None = None,
    target: str | None = None,
    repository: str = "owner/repo",
    entrypoint: str = "dispatcher",
    verification: str = "PASS",
    durability: str = "durable",
    resolved_model: str = "gpt-5.6",
) -> list[dict]:
    """Terra v1 rows plus the narrow joined identity consumed by the miner."""
    target = target or f"{repository}#{index}"
    run_id = f"run-{index}"
    attempt_id = f"attempt-{index}"
    subject_arms = ["worker"]
    subject_profiles = ["codex:gpt-5.6"]
    canonical_identity = research_subjects.subject_identity_from_hash(
        target,
        "testgen",
        _sha("normalized coverage spec"),
        "abc123",
        subject_arms,
        subject_profiles,
    )
    subject_id = subject_id or canonical_identity["subject_id"]
    observation_id = observation_id or research_subjects.completion_observation_id(
        subject_id, run_id, attempt_id
    )
    identity = {
        "subject_id": subject_id,
        "observation_id": observation_id,
        "family_id": canonical_identity["subject_family_id"],
        "attempt_id": attempt_id,
        "attempt_resolution": "resolved",
        "canonical_target": target,
        "repository": repository,
        "task_type": "testgen",
        "normalized_spec_hash": _sha("normalized coverage spec"),
        "base_sha": "abc123",
        "profile_id": "codex:gpt-5.6",
        "arm_id": "worker",
        "resolved_provider": "openai",
        "resolved_model": resolved_model,
        "subject_arms": subject_arms,
        "subject_profiles": subject_profiles,
    }
    payloads = {
        "trigger": {
            "result": {
                "status": "matched",
                "version_hash": _sha("trigger-v1"),
            }
        },
        "decision": {
            "workflow_ids": ["workflow:testgen"],
            "result": {
                "action_id": "action:testgen",
                "decision_source_id": "router:deterministic",
                "status": "selected",
            },
        },
        "execution": {
            "result": {
                "operation_role": "worker",
                "backend_run_id": f"backend-{index}",
                "status": "complete",
                "trace_key_hash": _sha(f"trace-{index}"),
            }
        },
        "artifact": {
            "artifact_refs": [
                {
                    "artifact_id": f"artifact-{index}",
                    "kind": "patch",
                    "content_hash": _sha(f"patch-{index}"),
                    "ref_class": "durable_store",
                }
            ],
            "changed_path_classes": ["tests"],
            "result_hashes": [_sha(f"result-{index}")],
            "result": {
                "status": "accepted",
                "version_hash": _sha("test-patch-contract-v1"),
            },
        },
        "verification": {
            "acceptance_gate_ids": ["gate:testgen"],
            "test_ids": ["pytest:generated"],
            "command_ids": ["cmd:testgen-gate"],
            "result_hashes": [_sha(f"verification-{index}")],
            "verification": {
                "verifier_verdict": verification,
                "adjudicated_verdict": verification,
            },
            "result": {"status": verification.lower()},
        },
        "outcome": {
            "delivery": {
                "pr_number": index,
                "merged": verification == "PASS",
                "ci_status": "success" if verification == "PASS" else "failure",
            },
            "result": {
                "outcome_verdict": "success" if verification == "PASS" else "failure",
                "status": "complete",
            },
        },
        "durability": {"durability": {"status": durability, "checked_ts": 100 + index}},
    }
    rows = []
    for offset, phase in enumerate(PHASES):
        event = {
            "event_id": f"event-{index}-{phase}",
            "schema_version": 1,
            "run_id": run_id,
            # Terra run-level rows may omit this; joined identity binds the attempt.
            "attempt_id": attempt_id if phase in {"execution", "artifact", "verification"} else None,
            "event_type": "completion",
            "phase": phase,
            "producer": (
                entrypoint
                if phase == "execution"
                else "runtime_ac"
                if phase == "verification"
                else "dispatcher"
            ),
            "status": "success" if phase != "durability" else durability,
            "validation_status": "accepted",
            "payload_json": json.dumps(payloads[phase], sort_keys=True),
            "content_hash": _sha(f"event-{index}-{phase}"),
            "redaction_count": 0,
            "created_ts": 10 + index + offset,
            "updated_ts": 10 + index + offset,
        }
        rows.append(
            {
                "schema": "orchestrator.completion-event-envelope",
                "version": 1,
                "event": event,
                "identity": dict(identity),
            }
        )
    return rows


@pytest.fixture
def repeated_subject_events():
    events = []
    for index in range(1, 81):
        events.extend(
            completion_episode(
                index,
                target="owner/repo#1",
            )
        )
    return events


def test_real_feedback_envelope_adapts_run_level_phase():
    raw = completion_episode(1)[0]
    assert raw["event"]["attempt_id"] is None
    event = adapt_completion_event_envelope(raw)
    assert event.attempt_id == "attempt-1"
    assert event.validation_status == "accepted"
    assert event.identity.subject_id.startswith("subject:")


def test_profile_only_normal_work_uses_exporter_subject_contract():
    raw = completion_episode(1)[0]
    identity = raw["identity"]
    identity["arm_id"] = ""
    identity["subject_arms"] = []
    identity["subject_profiles"] = [identity["profile_id"]]
    expected = research_subjects.subject_identity_from_hash(
        identity["canonical_target"],
        identity["task_type"],
        identity["normalized_spec_hash"],
        identity["base_sha"],
        identity["subject_arms"],
        identity["subject_profiles"],
    )
    identity["subject_id"] = expected["subject_id"]
    identity["family_id"] = expected["subject_family_id"]
    identity["observation_id"] = research_subjects.completion_observation_id(
        expected["subject_id"], raw["event"]["run_id"], identity["attempt_id"]
    )
    event = adapt_completion_event_envelope(raw)
    assert event.identity.subject_id == expected["subject_id"]
    assert event.identity.arm_id == ""


def test_three_independent_episodes_emit_one_candidate():
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    result = PatternMiner().mine(events, now=200)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert len(candidate.independent_subjects) == 3
    assert candidate.telemetry["effective_subject_count"] == 3.0
    assert candidate.kind_proposal == "workflow"
    assert candidate.lifecycle.state == "clustered"
    assert tuple(candidate.graph["phase_order"]) == PHASES
    assert len(candidate.graph["edges"]) == 6
    assert all(len(item.event_refs) == 7 for item in candidate.source_occurrences)
    assert result.status["emitted_candidate_count"] == 1


def test_retries_and_same_subject_do_not_inflate_evidence(repeated_subject_events):
    result = PatternMiner().mine(repeated_subject_events, now=200)
    progress = result.status["threshold_progress"][0]
    assert progress["positive_distinct_subjects"] == 1, (
        "repeated subject counted as independent"
    )
    assert not result.candidates
    assert progress["required_positive_distinct_subjects"] == 3


def test_forged_subject_ids_cannot_manufacture_independence():
    events = []
    for index in range(1, 4):
        events.extend(
            completion_episode(
                index,
                target="owner/repo#1",
                subject_id=f"subject:forged-{index}",
            )
        )
    result = PatternMiner().mine(events, now=200)
    assert not result.candidates
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "subject_identity_mismatch" in reasons
    assert "observation_identity_mismatch" in reasons


def test_failed_retry_then_terminal_success_counts_subject_once_and_keeps_audit():
    events = []
    for subject_number in range(1, 4):
        target = f"owner/repo#{subject_number}"
        failed_retry = completion_episode(
            subject_number,
            target=target,
            verification="FAIL",
            durability="reverted",
        )
        failed_artifact = next(
            row for row in failed_retry if row["event"]["phase"] == "artifact"
        )
        failed_payload = json.loads(failed_artifact["event"]["payload_json"])
        failed_payload["changed_path_classes"] = ["discarded-retry-output"]
        failed_artifact["event"]["payload_json"] = json.dumps(failed_payload)
        events.extend(failed_retry)
        events.extend(completion_episode(100 + subject_number, target=target))
    result = PatternMiner().mine(events, now=300)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert len(candidate.independent_subjects) == 3
    assert candidate.telemetry["negative_ratio"] == 0.0
    assert candidate.telemetry["retry_history_episode_count"] == 3
    assert candidate.telemetry["retry_counterexample_count"] == 3
    assert len(candidate.counterexamples) == 3
    assert all(not item.terminal for item in candidate.counterexamples)
    assert {item.audit_class for item in candidate.counterexamples} == {
        "superseded_retry_counterexample"
    }


def test_reversed_phase_timestamps_reject_non_topological_episode():
    events = completion_episode(1)
    trigger = next(row for row in events if row["event"]["phase"] == "trigger")
    trigger["event"]["created_ts"] = 500
    trigger["event"]["updated_ts"] = 500
    result = PatternMiner(min_positive_subjects=1).mine(events, now=600)
    assert not result.candidates
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "non_monotonic_phase_order:trigger->decision" in reasons


def test_generic_prompt_and_unresolved_provenance_are_exact_rejections():
    generic = completion_episode(1)[0]
    generic["event"]["event_type"] = "terminal_note"
    raw_prompt = completion_episode(2)[0]
    raw_prompt["event"]["payload_json"] = json.dumps(
        {"result": {"status": "matched", "raw_prompt": "do secret work"}}
    )
    unresolved = completion_episode(3)[0]
    unresolved["identity"]["resolved_model"] = ""
    result = PatternMiner().mine([generic, raw_prompt, unresolved], now=200)
    assert not result.candidates
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "generic_terminal_note_only" in reasons
    assert "raw_prompt_field_present" in reasons
    assert "unresolved_model_provenance" in reasons


def test_missing_phase_and_ambiguous_attempt_are_named_rejections():
    missing = completion_episode(1)[:-1]
    ambiguous = completion_episode(2)[0]
    ambiguous["identity"]["attempt_resolution"] = "ambiguous"
    result = PatternMiner().mine([*missing, ambiguous], now=200)
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "missing_phase:durability" in reasons
    assert "ambiguous_multiple_successful_attempts" in reasons


def test_negative_evidence_is_retained_but_bounded():
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    events.extend(completion_episode(4, verification="FAIL", durability="reverted"))
    result = PatternMiner().mine(events, now=200)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert len(candidate.counterexamples) == 1
    assert candidate.counterexamples[0].reason == "verification_not_passed"
    assert candidate.telemetry["negative_ratio"] == 0.25


def test_unnamed_verification_and_unaccepted_artifact_do_not_emit():
    events = completion_episode(1)
    verification = next(row for row in events if row["event"]["phase"] == "verification")
    payload = json.loads(verification["event"]["payload_json"])
    payload["acceptance_gate_ids"] = []
    payload["test_ids"] = []
    verification["event"]["payload_json"] = json.dumps(payload)
    artifact = next(row for row in events if row["event"]["phase"] == "artifact")
    artifact["event"]["status"] = "failed"
    result = PatternMiner(min_positive_subjects=1).mine(events, now=200)
    assert not result.candidates
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "unnamed_verification" in reasons


def test_real_merged_artifact_status_is_accepted_with_bounded_ref_and_hash():
    events = completion_episode(1)
    artifact = next(row for row in events if row["event"]["phase"] == "artifact")
    artifact["event"]["status"] = "merged"
    result = PatternMiner(min_positive_subjects=1).mine(events, now=200)
    assert len(result.candidates) == 1
    assert result.candidates[0].artifact_refs == ("artifact-1",)

    missing_ref = completion_episode(2)
    artifact = next(row for row in missing_ref if row["event"]["phase"] == "artifact")
    artifact["event"]["status"] = "merged"
    payload = json.loads(artifact["event"]["payload_json"])
    payload["artifact_refs"] = []
    payload["result_hashes"] = []
    artifact["event"]["payload_json"] = json.dumps(payload)
    rejected = PatternMiner(min_positive_subjects=1).mine(missing_ref, now=200)
    assert not rejected.candidates
    reasons = {reason for item in rejected.rejections for reason in item.reasons}
    assert "artifact_not_accepted" in reasons


def test_same_output_contract_implementations_dedupe_with_alias_tombstones():
    events = []
    for index, entrypoint in enumerate(("a.run", "b.run", "c.run"), start=1):
        events.extend(completion_episode(index, entrypoint=entrypoint))
    result = PatternMiner().mine(events, now=200)
    assert len(result.candidates) == 1
    assert len(result.candidates[0].aliases) == 3
    assert {item.reason for item in result.tombstones} == {"deduplicated_alias"}


def test_candidate_without_new_evidence_expires_to_tombstone():
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    miner = PatternMiner(candidate_ttl_days=1)
    result = miner.mine(events, now=200)
    expiry = result.candidates[0].lifecycle.expires_at
    expired = miner.sweep(now=expiry)
    assert expired.candidates[0].lifecycle.state == "retired"
    assert (
        expired.candidates[0].lifecycle.expiry_reason
        == "no_new_evidence_before_candidate_ttl"
    )
    tombstone = next(
        item for item in expired.tombstones if item.capability_id is not None
    )
    assert tombstone.fingerprint == expired.candidates[0].fingerprint
    assert expired.status["expired_candidate_count"] == 1


def test_status_command_is_machine_readable_and_queue_free(tmp_path, capsys):
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    path = tmp_path / "completion-events.json"
    path.write_text(json.dumps(events))
    status_path = tmp_path / "status.json"
    assert main(
        [
            "status",
            "--events",
            str(path),
            "--now",
            "200",
            "--write",
            str(status_path),
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads(status_path.read_text()) == report
    assert report["schema"] == "orchestrator.pattern-miner-status"
    assert report["accepted_event_count"] == 21
    assert report["distinct_eligible_subjects"] == 3
    assert report["emitted_candidate_count"] == 1
    assert report["human_review_queue_count"] == 0
    assert report["next_actions"] == ["observe_until_ttl_or_compiler_intake"]
    assert report["input_contract"]["version"] == 1
    assert "candidates" not in report


def test_cadence_runner_restores_state_and_persists_expiry_tombstone(
    tmp_path, capsys
):
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    events_path = tmp_path / "completion-events.jsonl"
    events_path.write_text("".join(json.dumps(row) + "\n" for row in events))
    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("")
    state_path = tmp_path / "pattern-miner-state.json"
    status_path = tmp_path / "pattern-miner-status.json"
    inventory_path = tmp_path / "pattern-miner-inventory.json"

    assert main(
        [
            "run",
            "--events",
            str(events_path),
            "--state",
            str(state_path),
            "--status-out",
            str(status_path),
            "--inventory-out",
            str(inventory_path),
            "--ttl-days",
            "1",
            "--now",
            "200",
        ]
    ) == 0
    capsys.readouterr()
    first_state = json.loads(state_path.read_text())
    assert first_state["schema"] == "orchestrator.pattern-miner-state"
    assert first_state["version"] == 1
    assert len(first_state["candidates"]) == 1
    expiry = first_state["candidates"][0]["lifecycle"]["expires_at"]

    assert main(
        [
            "run",
            "--events",
            str(empty_path),
            "--state",
            str(state_path),
            "--status-out",
            str(status_path),
            "--inventory-out",
            str(inventory_path),
            "--ttl-days",
            "1",
            "--now",
            str(expiry),
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    persisted = json.loads(state_path.read_text())
    inventory = json.loads(inventory_path.read_text())
    assert report["state_loaded"] is True
    assert report["expired_candidate_count"] == 1
    assert "candidates" not in json.loads(status_path.read_text())
    assert inventory["candidates"][0]["lifecycle"]["state"] == "retired"
    assert persisted["candidates"][0]["lifecycle"]["state"] == "retired"
    expiry_tombstones = [
        item
        for item in persisted["tombstones"]
        if item["reason"] == "no_new_evidence_before_candidate_ttl"
    ]
    assert len(expiry_tombstones) == 1
    assert (
        expiry_tombstones[0]["fingerprint"]
        == persisted["candidates"][0]["fingerprint"]
    )


def test_result_metadata_schema_is_strict():
    event = completion_episode(1)[0]
    event["event"]["payload_json"] = json.dumps(
        {"result": {"status": "matched", "arbitrary_llm_claim": "active"}}
    )
    result = PatternMiner().mine([event], now=200)
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "result_field_not_allowlisted:arbitrary_llm_claim" in reasons

