"""Focused regression coverage for the ranked modules in issue #781.

These tests keep all state in pytest's temporary directory and inject every
external seam; none may start a subprocess, call ``gh``, or use the live
orchestrator ledger.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import cadence_registry
import completion_event_adapter
import feedback
import keepalive_evidence
import research_subjects


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an accidental runtime write fail inside this test's own sandbox."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def _valid_envelope() -> dict:
    target = "Owner/Repo#007"
    spec_hash = "a" * 64
    base_sha = "b" * 40
    run_id = "run-7"
    attempt_id = "attempt-7"
    arms = ["codex"]
    profiles = ["codex:stable"]
    identity = completion_event_adapter.derive_completion_identity_ids(
        canonical_target="owner/repo#7",
        task_type="implement",
        normalized_spec_hash=spec_hash,
        base_sha=base_sha,
        run_id=run_id,
        attempt_id=attempt_id,
        subject_arms=arms,
        subject_profiles=profiles,
    )
    return {
        "schema": completion_event_adapter.ENVELOPE_SCHEMA,
        "version": completion_event_adapter.ENVELOPE_VERSION,
        "event": {
            "schema_version": completion_event_adapter.EVENT_SCHEMA_VERSION,
            "event_id": "event-7",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "event_type": "completion",
            "phase": "verification",
            "producer": "orchestrator_remote",
            "status": "complete",
            "validation_status": "accepted",
            "payload": {
                "capability_ids": ["capability:coverage-round"],
                "artifact_refs": [
                    {
                        "artifact_id": "github-pr:owner/repo#7",
                        "kind": "github-pr",
                        "content_hash": "sha256:" + "c" * 64,
                    }
                ],
            },
            "content_hash": "sha256:" + "d" * 64,
            "redaction_count": 0,
            "created_ts": 100,
            "updated_ts": 101,
        },
        "identity": {
            "subject_id": identity["subject_id"],
            "observation_id": identity["observation_id"],
            "family_id": identity["family_id"],
            "attempt_id": attempt_id,
            "attempt_resolution": "resolved",
            "canonical_target": target,
            "repository": "owner/repo",
            "task_type": "implement",
            "normalized_spec_hash": "sha256:" + spec_hash,
            "base_sha": base_sha,
            "profile_id": "codex:stable",
            "arm_id": "codex",
            "resolved_provider": "openai",
            "resolved_model": "gpt-5.6",
            "subject_arms": arms,
            "subject_profiles": profiles,
        },
    }


def test_completion_adapter_accepts_canonical_identity_and_rejects_secret_payload() -> None:
    accepted = completion_event_adapter.adapt_completion_event_envelope(_valid_envelope())
    assert (accepted.identity.canonical_target, accepted.identity.repository) == (
        "owner/repo#7",
        "owner/repo",
    )
    assert accepted.occurred_at == 101

    rejected = _valid_envelope()
    rejected["event"]["payload"]["result"] = {"token": "must-never-be-admitted"}
    rejected["event"]["payload"]["artifact_refs"] = [{"artifact_id": "only-one-field"}]
    with pytest.raises(completion_event_adapter.EnvelopeError) as excinfo:
        completion_event_adapter.adapt_completion_event_envelope(rejected)
    assert {reason.split(":", 1)[0] for reason in excinfo.value.reasons} >= {
        "secret_field_present",
        "result_field_not_allowlisted",
        "incomplete_artifact_ref",
        "invalid_artifact_content_hash",
    }


def test_keepalive_evidence_dedupes_open_link_and_groups_process_signals() -> None:
    candidate = {"seed": "PR #12 delivery regressed", "evidence": {"pr": 12, "title": "Fix"}}
    keepalive_evidence._dedupe_candidate(
        candidate,
        "owner/repo",
        issue_search_fn=lambda *_args: pytest.fail("search should not run after an open link"),
        linked_issues_fn=lambda *_args: [{"number": "91", "state": "OPEN"}],
    )
    assert candidate["possible_duplicate"] and candidate["dup_issue"] == 91

    calls: list[int] = []
    rows = [
        {"run_id": "one", "pr_number": 4, "work_type": "sync", "durability": "abandoned"},
        {"run_id": "two", "target": "owner/repo#4", "work_type": "sync", "durability": "reverted"},
        {"run_id": "three", "pr_number": 8, "work_type": "docs", "durability": "reverted"},
        {"run_id": "ignored", "pr_number": 9, "work_type": "issue", "durability": "reverted"},
        {
            "run_id": "suppressed",
            "pr_number": 10,
            "work_type": "sync",
            "durability": "abandoned",
            "notes": "process_ignore=duplicate_or_superseded",
        },
    ]
    signals = keepalive_evidence._process_signals(
        "owner/repo", rows, 30, {}, lambda _repo, pr: calls.append(pr) or f"PR {pr}"
    )
    assert [(row["work_type"], row["count"], row["severity"]) for row in signals] == [
        ("docs", 1, "HIGH"),
        ("sync", 1, "MED"),
    ]
    assert calls == [4, 8]


def test_cadence_inspection_reports_retry_and_retirement_without_live_state(tmp_path: Path) -> None:
    row = {
        "key": "retired-test-step",
        "success_stamp": ".success",
        "cadence_days": 0,
        "artifact": "evidence.json",
        "log": "step.log",
        "gate": "test gate",
        "retired": {
            "since": "2026-10-01",
            "reason": "test only",
            "re_enable_env": {"ORCH_TEST": "1"},
        },
    }
    failure = tmp_path / ".fail-retired-test-step"
    failure.write_text("not-a-number")
    os.utime(failure, (900_000, 900_000))
    report = cadence_registry.inspect_cadence(
        tmp_path, now=1_000_000, retry_hours=1, registry=(row,), environ={}
    )
    step = report["steps"][0]
    assert (step["success_status"], step["failure_count"], step["retry_state"]) == (
        "retired",
        1,
        "ready_to_retry",
    )
    live = cadence_registry.inspect_cadence(
        tmp_path, now=1_000_000, retry_hours=1, registry=(row,), environ={"ORCH_TEST": "1"}
    )["steps"][0]
    assert live["success_status"] == "missing"


def test_research_finding_implementation_requires_a_verified_closing_pr_run() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(feedback.SCHEMA)
        feedback._migrate_schema(conn)
        identity = research_subjects.subject_identity(
            "owner/repo#7", "audit", "finding", "base", ["codex"]
        )
        research_subjects.record_subject(identity, lifecycle="active", exp_id="round-7", conn=conn)
        feedback.record_run(
            "arm-run", "owner/repo#7", "audit", "codex", experiment_id="round-7", conn=conn
        )
        research_subjects.record_finding_issue(
            "round-7", "owner/repo#7", arm="codex", identity=identity, conn=conn
        )
        feedback.record_run("pr-run", "owner/repo#44", "implement", "codex", conn=conn)
        with pytest.raises(ValueError, match="closure_source"):
            research_subjects.record_finding_implementation(
                "round-7", "owner/repo#7", "pr-run", closed_by="owner/repo#44", conn=conn
            )
        event_id = research_subjects.record_finding_implementation(
            "round-7",
            "owner/repo#7",
            "pr-run",
            closed_by="owner/repo#44",
            closure_source="github:closedByPullRequestsReferences",
            conn=conn,
        )
        assert event_id.startswith("subject-event:")
    finally:
        conn.close()
