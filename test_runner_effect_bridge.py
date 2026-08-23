from __future__ import annotations

from pathlib import Path

import pytest

import capabilities
from runner_effect_bridge import (
    RunnerEffectError,
    record_runner_effect,
    runner_outputs_to_effect,
)

CAPABILITY_ID = "capability:reference-sync-hygiene-test-gate"


def valid_outputs(**overrides: str) -> dict[str, str]:
    values = {
        "capability-id": CAPABILITY_ID,
        "effect-fingerprint": "sha256:" + "a" * 64,
        "evidence-artifact-ref": "github-actions:owner/repo:123:consumer-sync-plan",
        "supervision-mode": "shadow",
        "capability-evidence-status": "accepted",
        "terminal-disposition": "no-change",
    }
    values.update(overrides)
    return values


def registered_ledger(path: Path) -> None:
    capabilities.register(
        CAPABILITY_ID,
        {
            "status": "shadow",
            "owner": "orchestrator",
            "matcher": {"kind": "typed_evidence", "schema": "consumer-sync-plan/v1"},
            "entrypoint": "runner_effect_bridge.py",
            "trigger_cadence": "typed runner completion evidence",
            "flags_defaults": {"mode": "shadow"},
            "output_artifact": "orchestrator.runner-effect-receipt/v1",
            "downstream_consumer": "capabilities lifecycle ledger",
            "learning_sink": "completion-event lineage",
            "gate_reason": "shadow evidence cannot authorize writes",
            "gate_evidence": "effect validator and no apply entrypoint",
            "evidence_threshold": "durable low-harm outcomes with rollback proof",
            "activation_deadline": 1893456000,
            "expiry": 1893456000,
            "next_transition": "retired",
            "kill_switch": "ORCH_REFERENCE_WORKFLOW_DISABLED=1",
            "rollback": {"steps": []},
        },
        path=path,
    )


def test_absent_evidence_is_backwards_compatible(tmp_path: Path) -> None:
    receipt = record_runner_effect({}, event_ref="run:1", ledger_path=tmp_path / "caps.json")
    assert receipt["status"] == "absent"
    assert receipt["mutated"] is False


def test_partial_spoofed_and_secret_evidence_are_rejected() -> None:
    with pytest.raises(RunnerEffectError, match="partial_runner_effect"):
        runner_outputs_to_effect({"capability-id": CAPABILITY_ID})
    with pytest.raises(RunnerEffectError, match="fingerprint"):
        runner_outputs_to_effect(valid_outputs(**{"effect-fingerprint": "sha256:no"}))
    with pytest.raises(RunnerEffectError, match="secret_like"):
        runner_outputs_to_effect(
            valid_outputs(**{"evidence-artifact-ref": "artifact:secret-token:1"})
        )


def test_unknown_capability_cannot_mutate_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "caps.json"
    capabilities.save({}, ledger)
    with pytest.raises(RunnerEffectError, match="unknown_capability_id"):
        record_runner_effect(valid_outputs(), event_ref="run:1", ledger_path=ledger)
    assert capabilities.load(ledger, create=False) == {}


def test_accepted_effect_is_idempotent_and_links_outcome(tmp_path: Path) -> None:
    ledger = tmp_path / "caps.json"
    registered_ledger(ledger)
    first = record_runner_effect(
        valid_outputs(), event_ref="run:1", ledger_path=ledger, timestamp=100
    )
    second = record_runner_effect(
        valid_outputs(), event_ref="run:1", ledger_path=ledger, timestamp=101
    )
    assert first["mutated"] is True
    assert second["mutated"] is False
    cap = capabilities.load(ledger, create=False)[CAPABILITY_ID]
    assert cap["outcome_links"] == ["effect:" + "sha256:" + "a" * 64]
    effect_events = [
        event
        for event in cap["event_history"]
        if str(event.get("idempotency_key") or "").startswith(first["receipt_id"])
    ]
    assert [event["type"] for event in effect_events] == ["output", "outcome"]


def test_failure_is_retained_as_counterexample_without_success_outcome(tmp_path: Path) -> None:
    ledger = tmp_path / "caps.json"
    registered_ledger(ledger)
    receipt = record_runner_effect(
        valid_outputs(**{"terminal-disposition": "failure"}),
        event_ref="run:2",
        ledger_path=ledger,
        timestamp=100,
    )
    cap = capabilities.load(ledger, create=False)[CAPABILITY_ID]
    assert receipt["counterexample"] is True
    assert cap["outcome_links"] == []
    assert any(event["type"] == "failure" for event in cap["event_history"])


def test_explicitly_rejected_evidence_is_observable_but_does_not_mutate(tmp_path: Path) -> None:
    ledger = tmp_path / "caps.json"
    registered_ledger(ledger)
    before = capabilities.load(ledger, create=False)
    receipt = record_runner_effect(
        valid_outputs(**{"capability-evidence-status": "rejected"}),
        event_ref="run:3",
        ledger_path=ledger,
    )
    assert receipt["status"] == "rejected"
    assert receipt["mutated"] is False
    assert capabilities.load(ledger, create=False) == before
