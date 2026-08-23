#!/usr/bin/env python3
"""Bridge typed Workflows runner outputs into the existing capability ledger.

The bridge is intentionally storage-thin: completion events remain the durable
evidence plane and ``capabilities.py`` remains the lifecycle authority. Free-form
runner text is never inspected, and invalid or rejected evidence cannot mutate
capability state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import capabilities
from completion_event_adapter import (
    CAPABILITY_EFFECT_SCHEMA,
    validate_capability_effect_record,
)

RUNNER_OUTPUT_KEYS = {
    "capability_id": ("capability-id", "capability_id"),
    "effect_fingerprint": ("effect-fingerprint", "effect_fingerprint"),
    "evidence_artifact_ref": ("evidence-artifact-ref", "evidence_artifact_ref"),
    "supervision_mode": ("supervision-mode", "supervision_mode"),
    "evidence_status": (
        "capability-evidence-status",
        "capability_evidence_status",
        "evidence_status",
    ),
    "terminal_disposition": ("terminal-disposition", "terminal_disposition"),
}
# Optional, all-or-none does NOT apply: a runner that never learned about subjects still validates.
RUNNER_OPTIONAL_OUTPUT_KEYS = {"subject_id": ("subject-id", "subject_id")}


class RunnerEffectError(ValueError):
    """Stable rejection code for runner effect evidence."""


def _value(outputs: dict[str, Any], names: tuple[str, ...]) -> str:
    present = [str(outputs[name] or "").strip() for name in names if name in outputs]
    nonempty = [value for value in present if value]
    if len(set(nonempty)) > 1:
        raise RunnerEffectError("conflicting_runner_effect_aliases")
    return nonempty[0] if nonempty else ""


def runner_outputs_to_effect(outputs: dict[str, Any]) -> dict[str, str] | None:
    """Normalize one all-or-none Workflows output record."""
    if not isinstance(outputs, dict):
        raise RunnerEffectError("runner_outputs_not_object")
    values = {field: _value(outputs, aliases) for field, aliases in RUNNER_OUTPUT_KEYS.items()}
    if not any(values.values()):
        return None
    missing = sorted(field for field, value in values.items() if not value)
    if missing:
        raise RunnerEffectError("partial_runner_effect:" + ",".join(missing))
    raw = {"schema": CAPABILITY_EFFECT_SCHEMA, **values}
    # Optional fields are excluded from the all-or-none check above on purpose: a missing subject
    # must NOT reject an otherwise complete effect, or adding subject identity would have silently
    # thrown away every existing runner's evidence.
    for field, aliases in RUNNER_OPTIONAL_OUTPUT_KEYS.items():
        optional = _value(outputs, aliases)
        if optional:
            raw[field] = optional
    try:
        return validate_capability_effect_record(
            raw, expected_capability_ids=[values["capability_id"]]
        )
    except ValueError as exc:
        raise RunnerEffectError(str(exc)) from exc


def _stable_hash(namespace: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(namespace.encode() + b"\0" + encoded).hexdigest()


def record_runner_effect(
    outputs: dict[str, Any],
    *,
    event_ref: str,
    ledger_path: Path = capabilities.REG,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Record accepted typed evidence idempotently against an existing capability."""
    effect = runner_outputs_to_effect(outputs)
    if effect is None:
        return {
            "schema": "orchestrator.runner-effect-receipt/v1",
            "status": "absent",
            "mutated": False,
            "receipt_id": _stable_hash("runner-effect-absent", event_ref),
        }
    capability_id = effect["capability_id"]
    ledger = capabilities.load(ledger_path, create=False)
    if capability_id not in ledger:
        raise RunnerEffectError("unknown_capability_id")
    receipt_core = {
        "schema": "orchestrator.runner-effect-receipt/v1",
        "capability_id": capability_id,
        "event_ref": str(event_ref),
        "effect_fingerprint": effect["effect_fingerprint"],
        "evidence_artifact_ref": effect["evidence_artifact_ref"],
        "supervision_mode": effect["supervision_mode"],
        "evidence_status": effect["evidence_status"],
        "terminal_disposition": effect["terminal_disposition"],
    }
    receipt_id = _stable_hash("runner-effect-receipt", receipt_core)
    if effect["evidence_status"] != "accepted":
        return {
            **receipt_core,
            "status": "rejected",
            "mutated": False,
            "receipt_id": receipt_id,
        }

    metadata = {
        "effect_fingerprint": effect["effect_fingerprint"],
        "evidence_artifact_ref": effect["evidence_artifact_ref"],
        "supervision_mode": effect["supervision_mode"],
        "terminal_disposition": effect["terminal_disposition"],
        "event_ref_hash": _stable_hash("runner-effect-event-ref", event_ref),
    }
    if effect.get("subject_id"):
        metadata["subject_id"] = effect["subject_id"]
    output_added = capabilities.heartbeat(
        capability_id,
        "output",
        ref=effect["evidence_artifact_ref"],
        metadata=metadata,
        timestamp=timestamp,
        path=ledger_path,
        idempotency_key=f"{receipt_id}:output",
    )
    counterexample = effect["terminal_disposition"] in {
        "failure",
        "blocked",
        "cancelled",
    }
    if counterexample:
        terminal_added = capabilities.heartbeat(
            capability_id,
            "failure",
            ref="counterexample:" + effect["effect_fingerprint"],
            metadata=metadata,
            timestamp=timestamp,
            path=ledger_path,
            idempotency_key=f"{receipt_id}:terminal",
        )
    else:
        terminal_added = capabilities.heartbeat(
            capability_id,
            "outcome",
            ref="effect:" + effect["effect_fingerprint"],
            metadata=metadata,
            timestamp=timestamp,
            path=ledger_path,
            idempotency_key=f"{receipt_id}:terminal",
        )
    return {
        **receipt_core,
        "status": "accepted",
        "mutated": bool(output_added or terminal_added),
        "counterexample": counterexample,
        "receipt_id": receipt_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-output-json", type=Path, required=True)
    parser.add_argument("--event-ref", required=True)
    parser.add_argument("--ledger", type=Path, default=capabilities.REG)
    args = parser.parse_args(argv)
    outputs = json.loads(args.runner_output_json.read_text(encoding="utf-8"))
    try:
        receipt = record_runner_effect(outputs, event_ref=args.event_ref, ledger_path=args.ledger)
    except RunnerEffectError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
