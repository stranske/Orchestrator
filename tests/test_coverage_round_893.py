"""Regression coverage for issue #893's ranked validation targets.

The tests use only in-memory data or pytest's temporary runtime.  They never
invoke subprocesses, GitHub, or a live Orchestrator ledger.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import completion_event_adapter as adapter
import keepalive_evidence


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def _effect(**overrides: object) -> dict[str, object]:
    effect: dict[str, object] = {
        "schema": adapter.CAPABILITY_EFFECT_SCHEMA,
        "capability_id": "capability:coverage-round",
        "effect_fingerprint": "sha256:" + "a" * 64,
        "evidence_artifact_ref": "github-pr:owner/repo#893",
        "supervision_mode": next(iter(adapter.SUPERVISION_MODES)),
        "evidence_status": next(iter(adapter.CAPABILITY_EVIDENCE_STATUSES)),
        "terminal_disposition": next(iter(adapter.TERMINAL_DISPOSITIONS)),
    }
    effect.update(overrides)
    return effect


def _envelope() -> dict[str, object]:
    spec_hash = "b" * 64
    identity = adapter.derive_completion_identity_ids(
        canonical_target="owner/repo#893",
        task_type="implement",
        normalized_spec_hash=spec_hash,
        base_sha="c" * 40,
        run_id="run-893",
        attempt_id="attempt-893",
        subject_arms=["codex"],
        subject_profiles=["codex:stable"],
    )
    return {
        "schema": adapter.ENVELOPE_SCHEMA,
        "version": adapter.ENVELOPE_VERSION,
        "event": {
            "schema_version": adapter.EVENT_SCHEMA_VERSION,
            "event_id": "event-893",
            "run_id": "run-893",
            "attempt_id": "attempt-893",
            "event_type": "completion",
            "phase": "verification",
            "producer": "orchestrator_remote",
            "status": "complete",
            "validation_status": "accepted",
            "payload": {"capability_ids": ["capability:coverage-round"]},
            "content_hash": "sha256:" + "d" * 64,
            "redaction_count": 0,
            "created_ts": 10,
            "updated_ts": 11,
        },
        "identity": {
            "subject_id": identity["subject_id"],
            "observation_id": identity["observation_id"],
            "family_id": identity["family_id"],
            "attempt_id": "attempt-893",
            "attempt_resolution": "resolved",
            "canonical_target": "owner/repo#893",
            "repository": "owner/repo",
            "task_type": "implement",
            "normalized_spec_hash": "sha256:" + spec_hash,
            "base_sha": "c" * 40,
            "profile_id": "codex:stable",
            "arm_id": "codex",
            "resolved_provider": "openai",
            "resolved_model": "gpt-5.6",
            "subject_arms": ["codex"],
            "subject_profiles": ["codex:stable"],
        },
    }


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"capability_id": "bad"}, "invalid_capability_effect_id"),
        ({"effect_fingerprint": "not-a-fingerprint"}, "invalid_capability_effect_fingerprint"),
        ({"evidence_artifact_ref": "secret-token"}, "secret_like_capability_effect_artifact_ref"),
        ({"supervision_mode": "unapproved"}, "unsupported_capability_effect_supervision_mode"),
        ({"evidence_status": "unapproved"}, "unsupported_capability_effect_evidence_status"),
        (
            {"terminal_disposition": "unapproved"},
            "unsupported_capability_effect_terminal_disposition",
        ),
    ],
)
def test_capability_effect_validation_rejects_each_typed_contract_violation(
    change: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        adapter.validate_capability_effect_record(
            _effect(**change), expected_capability_ids=["capability:coverage-round"]
        )


def test_adapter_reports_payload_and_effect_contract_violations_together() -> None:
    raw = deepcopy(_envelope())
    event = raw["event"]
    assert isinstance(event, dict)
    event["content_hash"] = "broken"
    event["phase"] = "not-a-phase"
    event["validation_status"] = "pending"
    event["redaction_count"] = "not-an-integer"
    event["payload"] = {
        "unexpected": True,
        "result": "not-a-dict",
        "artifact_refs": [{"artifact_id": "only-id"}, "not-a-dict"],
        "capability_effects": [_effect(capability_id="bad"), "not-a-dict"],
    }
    with pytest.raises(adapter.EnvelopeError) as excinfo:
        adapter.adapt_completion_event_envelope(raw)
    reasons = set(excinfo.value.reasons)
    assert {
        "invalid_completion_event_content_hash",
        "unsupported_completion_event_phase",
        "completion_event_not_validated",
        "completion_event_contains_redactions",
        "payload_field_not_allowlisted:unexpected",
        "result_metadata_not_object",
        "incomplete_artifact_ref:0",
        "invalid_artifact_content_hash:0",
        "invalid_artifact_ref:1",
        "invalid_capability_effect_id:0",
        "invalid_capability_effect_fields:1",
    } <= reasons


def test_adapter_rejects_wrong_envelope_and_missing_join_without_deriving_identity() -> None:
    with pytest.raises(adapter.EnvelopeError) as missing_event:
        adapter.adapt_completion_event_envelope({"schema": "wrong", "version": 0})
    assert set(missing_event.value.reasons) == {
        "unsupported_completion_event_envelope_schema",
        "unsupported_completion_event_envelope_version",
        "missing_event_record",
    }

    raw = _envelope()
    raw.pop("identity")
    with pytest.raises(adapter.EnvelopeError, match="missing_joined_identity"):
        adapter.adapt_completion_event_envelope(raw)


def test_keepalive_json_helpers_handle_success_failure_and_malformed_link_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        returncode = 0
        stdout = '{"title": "A title", "closingIssuesReferences": [{"number": 8}, {"number": 8}, {"number": "bad"}]}'

    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> Result:
        calls.append(args)
        if args[1:3] == ["issue", "list"]:
            result = Result()
            result.stdout = '[{"number": 41, "title": "Existing issue"}]'
            return result
        if "closingIssuesReferences" in args:
            result = Result()
            result.stdout = (
                '{"closingIssuesReferences": [{"number": 8}, {"number": 8}, {"number": "bad"}]}'
            )
            return result
        if "number,state" in args:
            result = Result()
            result.stdout = '{"number": 8, "state": "OPEN"}'
            return result
        return Result()

    monkeypatch.setattr(keepalive_evidence.subprocess, "run", fake_run)
    assert keepalive_evidence._fetch_pr_title("owner/repo", 5) == "A title"
    assert keepalive_evidence._issue_search("owner/repo", "needle") == [
        {"number": 41, "title": "Existing issue"}
    ]
    assert keepalive_evidence._linked_issues("owner/repo", 5) == [{"number": 8, "state": "OPEN"}]
    assert all(call[0] == "gh" for call in calls)

    class BadResult:
        returncode = 1
        stdout = "not-json"

    monkeypatch.setattr(keepalive_evidence.subprocess, "run", lambda *_args, **_kwargs: BadResult())
    assert keepalive_evidence._run_json(["gh", "issue", "list"]) is None

    class MalformedResult:
        returncode = 0
        stdout = "not-json"

    monkeypatch.setattr(
        keepalive_evidence.subprocess, "run", lambda *_args, **_kwargs: MalformedResult()
    )
    assert keepalive_evidence._run_json(["gh", "issue", "list"]) is None


def test_keepalive_evidence_builds_candidates_without_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "run_id": "keepalive:owner/repo#12:codex",
            "target": "owner/repo#12",
            "agent": "codex",
            "pr_number": None,
            "work_type": "issue",
            "durability": "reverted",
        },
        {
            "run_id": "keepalive:owner/repo#13:codex",
            "target": "owner/repo#13",
            "agent": "codex",
            "pr_number": 13,
            "work_type": "sync",
            "durability": "abandoned",
        },
    ]
    titles: list[int] = []
    monkeypatch.setattr(keepalive_evidence, "_repo_rows", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(keepalive_evidence, "_knowledge_candidates", lambda *_args, **_kwargs: [])
    result = keepalive_evidence.evidence_for_repo(
        "owner/repo",
        db_path=Path("not-used.db"),
        title_fetch_fn=lambda _repo, number: titles.append(number) or f"PR {number}",
        issue_search_fn=lambda _repo, _query: [],
        linked_issues_fn=lambda _repo, _number: [],
        knowledge_path=Path("not-used.json"),
    )
    assert result["repo"] == "owner/repo"
    assert titles == [13, 12]
    assert result["candidates"][0]["severity"] == "HIGH"
    assert result["process_signals"][0]["work_type"] == "sync"
