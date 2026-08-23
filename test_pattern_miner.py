from __future__ import annotations

import hashlib
import json

import pytest

from completion_event_adapter import (
    EnvelopeError,
    _canonical_target,
    OutOfScopeError,
    SUBJECTLESS_PRODUCERS,
    adapt_completion_event_envelope,
)
import pattern_miner
from pattern_miner import MAX_REPORTED_REJECTIONS, PHASES, PatternMiner, main
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


def test_one_unusable_event_is_skipped_without_aborting_the_batch():
    """One identity-less event must not destroy the run's other episodes.

    The adapter appended ``invalid_normalized_spec_hash`` and then derived
    identity from the value it had just rejected. research_subjects raised a bare
    ValueError, which is not an EnvelopeError, so it escaped the per-event skip in
    ``_assemble`` and aborted the whole run -- turning one absent field into a
    total, silent outage.
    """
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    # A verification event from a gate that terminated with gate_status
    # "missing_spec" genuinely has no spec: it is a faithful record that none was
    # materialized. Such an event must be skipped, never given a fabricated hash.
    unusable = completion_episode(9)[0]
    unusable["identity"]["normalized_spec_hash"] = None

    result = PatternMiner().mine([unusable, *events], now=200)

    # The three good episodes survive the one bad event and still emit.
    assert len(result.candidates) == 1
    assert result.status["accepted_event_count"] == 21
    assert result.status["rejected_event_count"] == 1
    reasons = {reason for rejection in result.rejections for reason in rejection.reasons}
    assert "invalid_normalized_spec_hash" in reasons
    # The skip is counted per stable code, not merely logged.
    assert result.status["rejected_event_reasons"]["invalid_normalized_spec_hash"] == 1
    assert result.status["rejected_event_reasons"]["identity_derivation_failed"] == 1


def test_corpus_wide_identity_fault_reports_counts_and_bounds_detail():
    """Every event unusable is a clean, legible zero -- not a crash, not a flood."""
    events = []
    for index in range(1, 61):
        for row in completion_episode(index):
            row["identity"]["normalized_spec_hash"] = None
            events.append(row)

    result = PatternMiner().mine(events, now=200)

    assert result.status["raw_event_count"] == 420
    assert result.status["accepted_event_count"] == 0
    assert result.status["rejected_event_count"] == 420
    assert not result.candidates
    # The cause is named once, with its full total, beside the zero it explains.
    assert result.status["rejected_event_reasons"]["invalid_normalized_spec_hash"] == 420
    # Per-event detail is capped, and the cap declares itself.
    payload = result.to_dict(include_candidates=False)
    assert len(payload["rejections"]) == MAX_REPORTED_REJECTIONS
    assert payload["rejections_truncated"]["total"] == 420
    assert payload["rejected_event_reasons"]["invalid_normalized_spec_hash"] == 420


def test_valid_spec_hash_still_derives_identity_and_is_not_skipped():
    """Guards the revert half of the break/revert demonstration.

    If the fix above ever degrades into skipping every event, this fails: a
    well-formed corpus must still be accepted and still derive real identity.
    """
    events = [row for index in range(1, 4) for row in completion_episode(index)]
    result = PatternMiner().mine(events, now=200)
    assert result.status["accepted_event_count"] == 21
    assert result.status["rejected_event_count"] == 0
    assert result.status["rejected_event_reasons"] == {}
    assert len(result.candidates) == 1


def _strip_identity(row: dict) -> dict:
    """Make a row look like ordinary production delivery: no research design set at all."""
    row = json.loads(json.dumps(row))
    row["identity"].update({
        "normalized_spec_hash": None, "base_sha": None, "subject_id": None,
        "family_id": None, "observation_id": None, "attempt_id": None,
        "attempt_resolution": "unresolved", "subject_arms": [], "subject_profiles": [],
        "resolved_provider": None, "resolved_model": None, "profile_id": None,
        "arm_id": None, "experiment_id": None,
    })
    row["event"]["attempt_id"] = None
    return row


def test_production_delivery_is_excluded_not_failed():
    """~70% of the stream has no research subject. Counting that as failure hid a real fault."""
    row = _strip_identity(completion_episode(1)[0])
    row["event"]["producer"] = "keepalive"
    with pytest.raises(OutOfScopeError) as caught:
        adapt_completion_event_envelope(row)
    assert any(r.startswith("no_research_subject:keepalive") for r in caught.value.reasons)

    result = PatternMiner().mine([row], now=200)
    assert result.status["excluded_event_count"] == 1
    assert result.status["rejected_event_count"] == 0, "an exclusion must not read as a defect"
    assert result.status["accepted_event_count"] == 0
    assert result.status["excluded_producers"] == {"keepalive": 1}


def test_research_claiming_event_without_identity_stays_a_rejection():
    """THE ANTI-RELAXATION GUARD. An event that claims research must identify itself.

    If this ever becomes an exclusion, the accepted count can be moved by dropping identity
    instead of supplying it -- which is exactly how mined candidates come to rest on identities
    that distinguish nothing.
    """
    row = _strip_identity(completion_episode(1)[0])
    row["event"]["producer"] = "orchestrator_local"
    row["identity"]["experiment_id"] = "tick-1-owner-repo-1"
    with pytest.raises(EnvelopeError) as caught:
        adapt_completion_event_envelope(row)
    assert not isinstance(caught.value, OutOfScopeError), "research context must not be excused"
    result = PatternMiner().mine([row], now=200)
    assert result.status["rejected_event_count"] == 1
    assert result.status["excluded_event_count"] == 0


def test_declared_subjectless_producer_carrying_research_is_flagged():
    """The declaration is load-bearing: a contract change must be said, not silently admitted."""
    row = _strip_identity(completion_episode(1)[0])
    row["event"]["producer"] = "keepalive"
    row["identity"]["experiment_id"] = "tick-9-owner-repo-9"
    with pytest.raises(EnvelopeError) as caught:
        adapt_completion_event_envelope(row)
    assert "subjectless_producer_carries_experiment" in caught.value.reasons


def test_contract_violation_is_a_defect_even_out_of_scope():
    """A bad payload is a defect regardless of scope; scope must not launder it."""
    row = _strip_identity(completion_episode(1)[0])
    row["event"]["producer"] = "keepalive"
    row["event"]["payload"] = {"result": {"status": "ok"}, "not_allowlisted_field": 1}
    with pytest.raises(EnvelopeError) as caught:
        adapt_completion_event_envelope(row)
    assert not isinstance(caught.value, OutOfScopeError)
    assert any(r.startswith("payload_field_not_allowlisted") for r in caught.value.reasons)


def test_mining_health_distinguishes_ran_from_mined():
    """`orchestrate.sh` read only the exit code, so accepting 0 of 1784 looked healthy."""
    empty = PatternMiner().mine([], now=200).status["mining_health"]
    assert empty["state"] == "no_input" and empty["actionable"] is True

    scoped = [_strip_identity(r) for r in completion_episode(2)]
    for r in scoped:
        r["event"]["producer"] = "keepalive"
    out = PatternMiner().mine(scoped, now=200).status["mining_health"]
    assert out["state"] == "all_out_of_scope", out
    assert out["actionable"] is False, "a clean production-only stream is not a fault"
    assert out["summary"].startswith("accepted 0 /"), out

    good = [row for index in range(1, 4) for row in completion_episode(index)]
    mining = PatternMiner().mine(good, now=200).status["mining_health"]
    assert mining["state"] == "mining" and mining["candidate_count"] >= 1

    bad = completion_episode(5)[0]
    bad["identity"]["subject_id"] = "subject:forged"
    rejecting = PatternMiner().mine([*good, bad], now=200).status["mining_health"]
    assert rejecting["state"] == "rejecting" and rejecting["actionable"] is True


def test_rejecting_health_names_its_blockers_and_never_implies_a_false_drain():
    """`203 rejected as malformed` said something was wrong and nothing about what to fix.

    On the live corpus FOUR reason codes each hit 203 of 203 events. Naming only the
    alphabetically-first one would invite fixing it and expecting the queue to drain, when in
    fact it would not move by a single event. So a tie at the top must be reported as a tie.
    """
    bad = completion_episode(5)[0]
    bad["identity"]["subject_id"] = "subject:forged"
    health = PatternMiner().mine([bad], now=200).status["mining_health"]
    assert health["state"] == "rejecting"
    reasons = PatternMiner().mine([bad], now=200).status["rejected_event_reasons"]
    assert reasons, "a rejecting run with no reason codes cannot be diagnosed"

    # The blocker is NAMED in the operator-visible detail, not only in the JSON.
    assert health["top_blocker"] in reasons, health
    assert health["top_blocker"] in health["detail"], health["detail"]
    assert str(health["top_blocker_count"]) in health["detail"], health["detail"]

    # A tie is reported as a tie: every code sharing the top count is listed, and the phrasing
    # says how many independent fixes stand in the way.
    top = max(reasons.values())
    tied = sorted(code for code, count in reasons.items() if count == top)
    assert health["top_blockers"] == tied[:4], (health["top_blockers"], tied)
    if len(tied) > 1:
        assert f"{len(health['top_blockers'])} blockers" in health["detail"], health["detail"]
        for code in health["top_blockers"]:
            assert code in health["detail"], (code, health["detail"])

    # DELIBERATE BREAK -> REVERT. Drop the reason counts the caller passes in, which is exactly
    # what the pre-fix code did, and the detail line loses the diagnosis while still claiming
    # the run is actionable -- actionable with nothing named is the silence this repo was built
    # to prevent.
    blind = pattern_miner._mining_health(1, 0, 1, 0, 0, 0, None)
    assert blind["top_blocker"] is None and blind["top_blockers"] == []
    assert "blocker" not in blind["detail"], blind["detail"]
    assert blind["actionable"] is True, "the break keeps the alarm and loses the cause"
    # REVERTED: pass the counts and the cause comes back.
    seeing = pattern_miner._mining_health(1, 0, 1, 0, 0, 0, reasons)
    assert seeing["top_blocker"] in seeing["detail"], seeing["detail"]


def test_domain_research_mines_without_a_base_sha_but_a_repo_subject_still_cannot():
    """`record_domain_research` passes `base_sha=None` on purpose -- a pricing study is not cut
    from a commit -- so requiring it unconditionally made the whole domain namespace unminable.

    The exemption is scoped to that ONE closed namespace, and this test pins both halves: a domain
    subject mines with no base_sha, and a repo-scoped subject with no base_sha is still rejected.
    Without the second half this would be a relaxation of the identity contract rather than
    recognition that one component does not apply.
    """
    import research_subjects

    domain = research_subjects.domain_target("model-tier-pricing")

    def _domain_rows(index):
        rows = completion_episode(index, target=domain, repository=domain)
        for row in rows:
            row["identity"]["base_sha"] = ""
            ident = research_subjects.subject_identity_from_hash(
                domain, "testgen", row["identity"]["normalized_spec_hash"], None,
                row["identity"]["subject_arms"], row["identity"]["subject_profiles"],
            )
            row["identity"]["subject_id"] = ident["subject_id"]
            row["identity"]["family_id"] = ident["subject_family_id"]
            row["identity"]["observation_id"] = research_subjects.completion_observation_id(
                ident["subject_id"], f"run-{index}", row["identity"]["attempt_id"]
            )
        return rows

    rows = [r for i in (11, 12, 13) for r in _domain_rows(i)]
    status = PatternMiner().mine(rows, now=200).status
    assert "missing_base_sha" not in (status["rejected_event_reasons"] or {}), status[
        "rejected_event_reasons"
    ]
    assert status["mining_health"]["state"] == "mining", status["mining_health"]
    assert status["complete_episode_count"] == 3, status["mining_health"]

    # THE OTHER HALF: a repo-scoped subject with no base_sha is still malformed. All 203 of the
    # live rejections are repo-scoped, so the exemption above clears none of them.
    repo_rows = completion_episode(21, target="owner/repo#21")
    for row in repo_rows:
        row["identity"]["base_sha"] = ""
    repo_status = PatternMiner().mine(repo_rows, now=200).status
    assert "missing_base_sha" in (repo_status["rejected_event_reasons"] or {}), repo_status[
        "rejected_event_reasons"
    ]


def test_a_subjectless_producer_can_graduate_without_a_code_edit():
    """The declaration must not latch: it once rejected the very evidence that would revise it.

    `subjectless_producer_carries_experiment` originally fired on ANY experiment_id, so a producer
    declared subjectless could never demonstrate otherwise -- a human had to edit a Python dict,
    and the only signal prompting that edit was the rejection itself. `roles` is the live case: it
    is declared to have "no delivering arm", which is empirically false for the `redirect` role
    (8 target/role cells across 2-3 agents each, 14 accepted / 211 counterfactual role edges).

    The drain that works while the gate is shut is a SELF-CONSISTENT identity, and both halves are
    pinned here: a coherent one graduates, a forged one does not.
    """
    producer = sorted(SUBJECTLESS_PRODUCERS)[0]

    # GRADUATES: identity derives from the event's own contract, so it went through the registered
    # path. The producer declaration does not veto real evidence.
    good = [row for index in (31, 32, 33) for row in completion_episode(index)]
    for row in good:
        row["event"]["producer"] = producer
        row["identity"]["experiment_id"] = "round:some-audit:2026-08-22"
    status = PatternMiner().mine(good, now=200).status
    reasons = status["rejected_event_reasons"] or {}
    assert "subjectless_producer_carries_experiment" not in reasons, reasons
    assert status["mining_health"]["state"] == "mining", status["mining_health"]

    # DOES NOT GRADUATE: an experiment_id with a subject_id that does not derive from the event's
    # own contract is a borrowed or forged identity, and is still called out by name.
    forged = completion_episode(34)
    for row in forged:
        row["event"]["producer"] = producer
        row["identity"]["experiment_id"] = "round:borrowed:2026-08-22"
        row["identity"]["subject_id"] = "subject:0000000000000000000000ff"
    forged_reasons = PatternMiner().mine(forged, now=200).status["rejected_event_reasons"] or {}
    assert "subjectless_producer_carries_experiment" in forged_reasons, forged_reasons


def test_a_resolved_model_alone_does_not_make_a_production_event_a_defect():
    """Regression: worker provenance landing on production runs must not create 111 fake defects.

    `presents_identity` once counted `subject_profiles`, which was safe only while profiles
    appeared exclusively on research runs. The moment `resolved_model` started resolving on
    ordinary offloads, 111 production events with no subject, no arms and no experiment flipped
    from correctly EXCLUDED to reported as malformed — purely for naming the model that served
    them. One root cause wearing 111 hats, hiding the real rejections behind it.

    A profile is an execution detail. The design set is the arm set.
    """
    rows = [_strip_identity(row) for row in completion_episode(41)]
    for row in rows:
        row["event"]["producer"] = "orchestrator_local"
        row["identity"]["subject_profiles"] = ["codex-5.6-terra-high"]
        row["identity"]["resolved_provider"] = "openai"
        row["identity"]["resolved_model"] = "gpt-5.6-terra"
    status = PatternMiner().mine(rows, now=200).status
    assert status["rejected_event_count"] == 0, status["rejected_event_reasons"]
    assert status["excluded_event_count"] == len(rows), status["mining_health"]
    assert status["mining_health"]["state"] == "all_out_of_scope", status["mining_health"]

    # A REAL research claim still gets judged as one: an arm set is the design set, so an
    # incomplete claim beside it is a defect and not a scope question.
    claiming = [_strip_identity(row) for row in completion_episode(42)]
    for row in claiming:
        row["event"]["producer"] = "orchestrator_local"
        row["identity"]["subject_arms"] = ["codex", "cursor"]
    claim_status = PatternMiner().mine(claiming, now=200).status
    assert claim_status["rejected_event_count"] == len(claiming), claim_status["mining_health"]

    # AND the profile-set check stays a real check where a subject DOES exist: a registration gap
    # between the subject's declared profiles and the one that actually ran is still named.
    mismatched = completion_episode(43)
    for row in mismatched:
        row["identity"]["subject_profiles"] = ["codex:some-other-profile"]
        row["identity"]["profile_id"] = "codex-5.6-terra-high"
    reasons = PatternMiner().mine(mismatched, now=200).status["rejected_event_reasons"] or {}
    assert "selected_profile_not_in_subject_set" in reasons, reasons


def test_rejections_report_how_many_could_ever_be_repaired():
    """`184 malformed` reads as work somebody should do. All 184 were history.

    `missing_joined_attempt_id` means the event carries no attempt row to join, and the export
    builds that join FROM the attempt row -- so a run that finished without one can never gain it.
    Verified on the live population: 25 UX-panel subjects, 0 with a base commit, 0 execution
    attempts, every run before provenance existed. A blocked count without its drainable twin is
    the same silence this repo keeps rediscovering.
    """
    unrepairable = completion_episode(51)
    for row in unrepairable:
        row["identity"]["attempt_id"] = ""          # no attempt row was ever written
        row["identity"]["attempt_resolution"] = "unresolved"
    health = PatternMiner().mine(unrepairable, now=200).status["mining_health"]
    assert health["state"] == "rejecting", health
    assert health["unrecoverable_rejections"] == health["rejected_event_count"], health
    assert health["drainable_rejections"] == 0, health
    assert "can never be repaired" in health["detail"], health["detail"]
    assert "0 drainable" in health["detail"], health["detail"]

    # THE OTHER HALF: a rejection that DOES have an attempt row is drainable, and must not be
    # written off as history -- otherwise this becomes a way to make a real defect count vanish.
    repairable = completion_episode(52)
    for row in repairable:
        row["identity"]["subject_id"] = "subject:0000000000000000000000ff"
    health2 = PatternMiner().mine(repairable, now=200).status["mining_health"]
    assert health2["state"] == "rejecting", health2
    assert health2["unrecoverable_rejections"] == 0, health2
    assert health2["drainable_rejections"] == health2["rejected_event_count"], health2
    assert "can never be repaired" not in health2["detail"], health2["detail"]


def test_every_declared_subjectless_producer_has_a_stated_reason():
    """A declaration without a reason is an assertion nobody can audit later."""
    for producer, reason in SUBJECTLESS_PRODUCERS.items():
        assert producer == producer.lower(), producer
        assert len(reason) > 20, f"{producer} needs a real justification, got {reason!r}"


def test_issue_scoped_targets_are_unchanged():
    """KEEPALIVE GUARD. Extending the grammar must not move `owner/repo#N` by one character."""
    assert _canonical_target("stranske/trip-planner#12") == (
        "stranske/trip-planner#12", "stranske/trip-planner")
    # Numeric normalisation is part of the old contract and must survive.
    assert _canonical_target("stranske/trip-planner#007") == (
        "stranske/trip-planner#7", "stranske/trip-planner")
    assert _canonical_target("Stranske/Trip-Planner#3") == (
        "stranske/trip-planner#3", "stranske/trip-planner")


def test_research_scopes_without_an_issue_now_parse():
    """Most research is not done on a GitHub issue; requiring one rejected all of it."""
    assert _canonical_target("stranske/trip-planner") == (
        "stranske/trip-planner", "stranske/trip-planner")
    assert _canonical_target("domain/luminar-editing") == (
        "domain/luminar-editing", "domain/luminar-editing")
    assert _canonical_target("local/Reader") == ("local/reader", "local/reader")
    assert _canonical_target("JobSearch.2026") == ("jobsearch.2026", "jobsearch.2026")


def test_transport_noise_and_sentinels_are_still_rejected():
    """Widening scope must not admit things that name no scope at all."""
    for bad in ("offload:/private/tmp", "triage:20-items",
                "stranske/trip-planner [ux_review]", "unknown", "none", "-", "", "   "):
        assert _canonical_target(bad) is None, bad


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

