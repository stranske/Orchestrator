"""Discriminating regression coverage for issue #916's ranked targets.

All filesystem state is pytest-owned and every external boundary is replaced
before invocation.  These are behavior tests, rather than calls to a module's
selftest, so removing the branch they name makes an assertion fail.
"""

from __future__ import annotations

from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import completion_event_adapter as adapter
import keepalive_evidence
import mcp_server


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def _effect(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": adapter.CAPABILITY_EFFECT_SCHEMA,
        "capability_id": "capability:round-916",
        "effect_fingerprint": "sha256:" + "a" * 64,
        "evidence_artifact_ref": "github-pr:owner/repo#916",
        "supervision_mode": next(iter(adapter.SUPERVISION_MODES)),
        "evidence_status": next(iter(adapter.CAPABILITY_EVIDENCE_STATUSES)),
        "terminal_disposition": next(iter(adapter.TERMINAL_DISPOSITIONS)),
    }
    result.update(overrides)
    return result


def _envelope() -> dict[str, object]:
    spec_hash = "b" * 64
    identity = adapter.derive_completion_identity_ids(
        canonical_target="owner/repo#916",
        task_type="implement",
        normalized_spec_hash=spec_hash,
        base_sha="c" * 40,
        run_id="run-916",
        attempt_id="attempt-916",
        subject_arms=["codex"],
        subject_profiles=["codex:stable"],
    )
    return {
        "schema": adapter.ENVELOPE_SCHEMA,
        "version": adapter.ENVELOPE_VERSION,
        "event": {
            "schema_version": adapter.EVENT_SCHEMA_VERSION,
            "event_id": "event-916",
            "run_id": "run-916",
            "attempt_id": "attempt-916",
            "event_type": "completion",
            "phase": "verification",
            "producer": "orchestrator_remote",
            "status": "complete",
            "validation_status": "accepted",
            "payload": {"capability_ids": ["capability:round-916"]},
            "content_hash": "sha256:" + "d" * 64,
            "redaction_count": 0,
            "created_ts": 10,
            "updated_ts": 11,
        },
        "identity": {
            "subject_id": identity["subject_id"],
            "observation_id": identity["observation_id"],
            "family_id": identity["family_id"],
            "attempt_id": "attempt-916",
            "attempt_resolution": "resolved",
            "canonical_target": "owner/repo#916",
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
        ({"schema": "unknown"}, "unsupported_capability_effect_schema"),
        ({"subject_id": "not a repository"}, "invalid_capability_effect_subject_id"),
        ({"capability_id": "wrong"}, "invalid_capability_effect_id"),
        ({"capability_id": "capability:other"}, "capability_effect_not_linked_to_payload"),
        ({"effect_fingerprint": "bad"}, "invalid_capability_effect_fingerprint"),
        ({"evidence_artifact_ref": "not valid!"}, "invalid_capability_effect_artifact_ref"),
    ],
)
def test_effect_validation_rejects_each_typed_boundary(
    change: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        adapter.validate_capability_effect_record(
            _effect(**change), expected_capability_ids=["capability:round-916"]
        )


def test_completion_adapter_accumulates_payload_and_identity_errors() -> None:
    raw = deepcopy(_envelope())
    event = raw["event"]
    identity = raw["identity"]
    assert isinstance(event, dict) and isinstance(identity, dict)
    event.update(
        {
            "schema_version": "old",
            "payload_json": "{",
            "payload": None,
            "attempt_id": "other-attempt",
        }
    )
    identity.update(
        {
            "canonical_target": "not valid!",
            "task_type": "",
            "subject_arms": "not-a-list",
            "subject_profiles": "not-a-list",
            "attempt_id": "",
            "attempt_resolution": "ambiguous",
        }
    )
    with pytest.raises(adapter.EnvelopeError) as excinfo:
        adapter.adapt_completion_event_envelope(raw)
    assert {
        "unsupported_completion_event_schema_version",
        "invalid_payload_json",
        "completion_event_payload_not_object",
        "invalid_canonical_target",
        "missing_task_type",
        "invalid_subject_arm_set",
        "invalid_subject_profile_set",
        "missing_subject_design_set",
        "ambiguous_multiple_successful_attempts",
        "missing_joined_attempt_id",
    } <= set(excinfo.value.reasons)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"artifact_refs": "wrong"}, "artifact_refs_not_array"),
        ({"artifact_refs": [{"extra": True}]}, "invalid_artifact_ref_fields:0"),
        ({"capability_effects": "wrong"}, "capability_effects_not_array"),
        (
            {"capability_effects": [_effect(capability_id="wrong")]},
            "invalid_capability_effect_id:0",
        ),
    ],
)
def test_completion_adapter_rejects_typed_payload_boundaries(
    payload: dict[str, object], reason: str
) -> None:
    raw = deepcopy(_envelope())
    event = raw["event"]
    assert isinstance(event, dict)
    event["payload"] = payload
    with pytest.raises(adapter.EnvelopeError, match=reason):
        adapter.adapt_completion_event_envelope(raw)


def test_keepalive_handles_missing_database_and_cli_modes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError, match="feedback DB not found"):
        keepalive_evidence._readonly_conn(tmp_path / "missing.db")

    monkeypatch.setattr(keepalive_evidence, "_selftest", lambda: None)
    assert keepalive_evidence.main(["--selftest"]) == 0
    with pytest.raises(SystemExit):
        keepalive_evidence.main([])

    monkeypatch.setattr(keepalive_evidence.keepalive_outcomes, "_active_repos", lambda: ["a/b"])
    monkeypatch.setattr(
        keepalive_evidence,
        "evidence_for_repo",
        lambda repo, lookback_days: {"repo": repo, "lookback_days": lookback_days},
    )
    monkeypatch.setattr(keepalive_evidence, "_human_summary", lambda result: result["repo"])
    assert keepalive_evidence.main(["--all-active", "--lookback-days", "4"]) == 0
    assert capsys.readouterr().out.strip() == "a/b"
    assert keepalive_evidence.main(["--all-active", "--json"]) == 0
    assert '"repos"' in capsys.readouterr().out
    assert keepalive_evidence.main(["--repo", "c/d"]) == 0
    assert capsys.readouterr().out.strip() == "c/d"
    assert keepalive_evidence.main(["--repo", "c/d", "--json"]) == 0
    assert '"repo": "c/d"' in capsys.readouterr().out


def test_mcp_server_dispatch_protocol_and_stdio_are_hermetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_read_json_file", lambda _path: None)
    assert mcp_server._call_tool("capacity_status", {}) == {"error": "no capacity snapshot"}
    monkeypatch.setattr(mcp_server, "_fleet_summary", lambda: {"ok": True})
    assert mcp_server._call_tool("fleet_summary", {}) == {"ok": True}
    monkeypatch.setattr(mcp_server.feedback, "current_weights", lambda task: {task: 1})
    assert mcp_server._call_tool("route_weights", {"task_type": "test"})["weights"] == {"test": 1}

    monkeypatch.setattr(mcp_server, "_call_tool", lambda name, args: {"name": name, "args": args})
    assert mcp_server._handle({"id": 1, "method": "initialize"})["result"]["capabilities"] == {
        "tools": {}
    }
    assert mcp_server._handle({"method": "notifications/initialized"}) is None
    assert (
        mcp_server._handle({"id": 2, "method": "tools/list"})["result"]["tools"] == mcp_server.TOOLS
    )
    call = mcp_server._handle(
        {"id": 3, "method": "tools/call", "params": {"name": "x", "arguments": {"a": 1}}}
    )
    assert '"name": "x"' in call["result"]["content"][0]["text"]
    assert mcp_server._handle({"id": 4, "method": "ping"})["result"] == {}
    assert mcp_server._handle({"id": 5, "method": "missing"})["error"]["code"] == -32601

    stdin, stdout = StringIO('{bad json}\n\n{"id": 6, "method": "ping"}\n'), StringIO()
    monkeypatch.setattr(mcp_server.sys, "stdin", stdin)
    monkeypatch.setattr(mcp_server.sys, "stdout", stdout)
    assert mcp_server.serve() == 0
    assert '"id": 6' in stdout.getvalue()


def test_mcp_server_dispatches_mutating_and_advisory_tools_without_real_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advice = SimpleNamespace(
        advise=lambda *args, **kwargs: {"dispatch_ready_count": 2, "args": args, "kwargs": kwargs},
        should_reask=lambda previous, current: previous != current,
        learned_associations=lambda: {"learned": True},
    )
    propensity = SimpleNamespace(
        DECLINE_KIND_DEFAULT="not_now",
        DECLINE_KINDS={"not_now": {"fix": "none"}},
        record_decline=lambda *args, **kwargs: True,
        decline_kind_demotable=lambda kind: kind == "not_now",
    )
    monkeypatch.setitem(__import__("sys").modules, "capability_advisor", advice)
    monkeypatch.setitem(__import__("sys").modules, "capability_propensity", propensity)
    response = mcp_server._call_tool(
        "capability_advice",
        {"task": "test", "repository": "o/r", "previous": {"task": "old"}, "surface": "test"},
    )
    assert response["reask"] is True
    decline = mcp_server._call_tool(
        "capability_decline",
        {"capability_id": "cap", "experiment_id": "exp", "reason": "reason", "surface": "test"},
    )
    assert decline == {
        "recorded": True,
        "capability_id": "cap",
        "experiment_id": "exp",
        "surface": "test",
        "affects_propensity": False,
        "attributable_to_surface": True,
        "kind": "not_now",
        "can_demote_the_binding": True,
        "kind_implies_fix": "none",
        "note": "recorded as a reasoned rejection; the usefulness posterior is untouched because the capability never ran",
        "already_recorded": False,
    }
    assert mcp_server._call_tool("capability_associations", {}) == {"learned": True}
    monkeypatch.setattr(mcp_server.feedback, "expire_owner_questions", lambda: None)
    monkeypatch.setattr(mcp_server.feedback, "open_owner_questions", lambda: ["question"])
    monkeypatch.setattr(mcp_server.feedback, "answer_owner_question", lambda *_args: True)
    monkeypatch.setattr(
        mcp_server.feedback, "record_owner_question", lambda *_args, **_kwargs: {"ok": True}
    )
    monkeypatch.setattr(mcp_server.feedback, "resume_hint", lambda _run: None)
    assert mcp_server._call_tool("owner_questions", {}) == {"open": ["question"]}
    assert mcp_server._call_tool(
        "answer_owner_question", {"question_id": "q", "answer": "yes"}
    ) == {"answered": True}
    assert mcp_server._call_tool("record_owner_question", {"question": "q", "default": "no"}) == {
        "ok": True
    }
    assert mcp_server._call_tool("resume_hint", {"run_id": "missing"}) == {
        "error": "no resume token for missing"
    }
    with pytest.raises(ValueError, match="unknown tool"):
        mcp_server._call_tool("unknown", {})
