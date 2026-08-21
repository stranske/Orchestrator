#!/usr/bin/env python3
"""Production coordinator for compiled capability targets and causal lifecycle.

The lifecycle ledger owns identity, routing, evidence, and retirement. Target
adapters own mutable bindings. Rollback is deliberately two phase: persist the
ledger intent, mutate and verify the target outside the ledger lock, then retire.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

import capabilities
import capability_compiler
import capability_targets
import feedback


DEFAULT_TARGET_REGISTRY = Path(
    os.environ.get(
        "ORCH_CAPABILITY_TARGET_REGISTRY",
        Path.home() / ".codex" / "orchestrator" / "capability-targets.json",
    )
)

SAFE_CANARY_RISKS = {"low", "low_reversible", "moderate_reversible"}
SAFE_CANARY_SIDE_EFFECTS = {
    "read_only", "advisory", "managed_reversible_write", "reversible_write"
}

TARGET_CONTRACTS = {
    "role": (
        "roles.py:run_generated_shadow_role",
        "validated generated-role proposal",
        "dispatcher accepted-role lineage",
    ),
    "workflow": (
        "capability_compiler.py:dry_run_workflow_rail",
        "typed workflow rail result",
        "consume_workflow_output",
    ),
    "skill": (
        "capability_compiler.py:shadow_invoke_skill_package",
        "validated skill-package result",
        "dispatcher accepted-skill lineage",
    ),
    "playbook": (
        "capability_compiler.py:record_playbook_invocation",
        "managed playbook rule",
        "repo_knowledge.append_context",
    ),
    "gate": (
        "runtime_ac.py:capture_evidence_contract",
        "typed acceptance-gate evidence",
        "evaluator_prompt_fragment",
    ),
}


def _artifact_payload(kind: str, artifact: Any) -> dict[str, Any]:
    if kind == "skill":
        return capability_compiler.validate_skill_package(Path(artifact))
    if not isinstance(artifact, dict):
        raise TypeError(f"{kind} compiler artifact must be a mapping")
    return json.loads(json.dumps(artifact))


def _record(
    capability_id: str,
    kind: str,
    *,
    predecessor: str,
    matcher: Mapping[str, Any],
    expiry: int,
    compatibility_evidence: Mapping[str, Any] | None = None,
    status: str = "generated",
) -> dict[str, Any]:
    entrypoint, output, consumer = TARGET_CONTRACTS[kind]
    return {
        "status": status,
        "owner": "orchestrator",
        "matcher": dict(matcher),
        "entrypoint": entrypoint,
        "trigger_cadence": "naturally matching production tasks",
        "output_artifact": output,
        "downstream_consumer": consumer,
        "learning_sink": "feedback capability-version causal joins",
        "activation_evidence": dict(compatibility_evidence or {}),
        "activation_deadline": expiry,
        "expiry": expiry,
        "next_transition": "validated" if status == "generated" else None,
        "kill_switch": f"ORCH_CAPABILITY_{kind.upper()}_ENABLED=0",
        "rollback": {"transition": "retired", "predecessor": predecessor},
        "predecessor": predecessor,
    }


def _unsafe_policy(policy: Mapping[str, Any]) -> bool:
    return (
        str(policy.get("risk_level") or "low") not in SAFE_CANARY_RISKS
        or str(policy.get("side_effect_policy") or "read_only")
        not in SAFE_CANARY_SIDE_EFFECTS
    )


def register_compiled_target(
    kind: str,
    artifact: Any,
    *,
    lifecycle_policy: Mapping[str, Any],
    matcher: Mapping[str, Any],
    predecessor: str,
    ledger_path: Path,
    target_registry_path: Path,
    target_context: Mapping[str, Any] | None = None,
    expiry: int | None = None,
) -> dict[str, Any]:
    """Register one compiler artifact through the ledger and its real target."""
    if kind not in TARGET_CONTRACTS:
        raise ValueError(f"unsupported target kind: {kind}")
    capability_id, _ = capability_targets.artifact_identity(kind, artifact)
    payload = _artifact_payload(kind, artifact)
    policy = dict(lifecycle_policy)
    expires_at = int(expiry or policy.get("expires_at") or (time.time() + 30 * 86400))
    identity = capabilities.compiled_version_identity(
        capability_id, artifact=payload, lifecycle_policy=policy
    )
    compatibility_evidence: dict[str, Any] = {}
    if kind == "workflow":
        compatibility_evidence["workflow_plan_id"] = payload["plan_id"]
    elif kind == "skill":
        compatibility_evidence["skill_content_hash"] = payload["content_hash"]
    elif kind == "role":
        compatibility_evidence["role_manifest_hash"] = payload["manifest_id"]
    elif kind == "playbook":
        compatibility_evidence["playbook_manifest_hash"] = payload["manifest_id"]
    cap = capabilities.register_compiled_version(
        capability_id,
        target_kind=kind,
        artifact=payload,
        lifecycle_policy=policy,
        record=_record(
            capability_id,
            kind,
            predecessor=predecessor,
            matcher=matcher,
            expiry=expires_at,
            compatibility_evidence=compatibility_evidence,
        ),
        path=Path(ledger_path),
    )
    if _unsafe_policy(policy):
        question = feedback.record_owner_question(
            f"Allow a supervised canary for high-risk compiled {kind} {capability_id}?",
            "keep_shadow_unexported",
            target=capability_id,
            options=["keep shadow unexported", "approve bounded supervised canary"],
            expires_days=float(policy.get("owner_question_expires_days") or 2.0),
        )
        cap = capabilities.attach_owner_question(
            capability_id, question, path=Path(ledger_path)
        )
        return {"status": "owner_question", "capability": cap, "binding": None}

    try:
        binding = capability_targets.register_target(
            kind,
            artifact,
            registry_path=Path(target_registry_path),
            predecessor=predecessor,
            lifecycle_policy=policy,
            context=target_context,
            identity={key: identity[key] for key in (
                "capability_id", "capability_version_id", "artifact_hash",
                "lifecycle_policy_hash",
            )},
        )
    except Exception:
        capabilities.transition(
            capability_id,
            "retired",
            reason="target registration failed before wiring",
            evidence_refs=[identity["artifact_hash"]],
            path=Path(ledger_path),
        )
        raise
    capabilities.transition(
        capability_id,
        "validated",
        reason="compiler artifact validated by typed target registrar",
        evidence_refs=[binding["target_artifact_hash"]],
        path=Path(ledger_path),
    )
    capabilities.transition(
        capability_id,
        "wired",
        reason="real target binding installed",
        evidence_refs=[binding["binding_path"]],
        path=Path(ledger_path),
    )
    return {
        "status": "wired",
        "capability": capabilities.load(Path(ledger_path), create=False)[capability_id],
        "binding": binding,
    }


def invoke_compiled_target(
    capability_id: str,
    *,
    trigger: Mapping[str, Any],
    target_run_id: str,
    ledger_path: Path,
    target_registry_path: Path,
    inputs: Mapping[str, Any] | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Invoke a matching real producer/consumer and write its version-exact edge."""
    now = int(timestamp or time.time())
    cap = capabilities.load(Path(ledger_path), create=False).get(capability_id)
    if not cap:
        raise ValueError(f"unknown capability: {capability_id}")
    if cap["status"] not in {"wired", "shadow", "exercised", "canary", "active"}:
        return {"matched": False, "invoked": False, "reason": f"status:{cap['status']}"}
    binding = capability_targets.get_binding(
        cap["capability_version_id"], registry_path=Path(target_registry_path)
    )
    if not binding:
        raise ValueError("lifecycle version has no target binding")
    if cap["status"] == "canary":
        limit = int((cap.get("lifecycle_policy") or {}).get("tasks_per_day") or 1)
        recent = sum(
            1 for row in binding.get("invocations") or []
            if int(row.get("invoked_at") or 0) >= now - 86400
            and row.get("lifecycle_stage") == "canary"
        )
        if recent >= limit:
            return {"matched": True, "invoked": False, "reason": "canary_quota_exhausted"}
    result = capability_targets.invoke_target(
        binding,
        trigger=dict(trigger),
        registry_path=Path(target_registry_path),
        ledger_path=Path(ledger_path),
        target_run_id=target_run_id,
        inputs=inputs,
        lifecycle_stage=cap["status"],
    )
    if result.get("invoked") and result.get("accepted"):
        edge = feedback.record_capability_consumption(
            capability_id=capability_id,
            capability_version_id=cap["capability_version_id"],
            source_run_id=f"capability-producer:{result['invocation_id']}",
            target_run_id=target_run_id,
            accepted=True,
            producer=f"capability_lifecycle:{cap['status']}",
        )
        result["causal_edge"] = edge
    return result


def start_canary(
    capability_id: str, *, ledger_path: Path, evidence_ref: str
) -> dict[str, Any]:
    cap = capabilities.load(Path(ledger_path), create=False)[capability_id]
    if cap["status"] != "exercised":
        raise ValueError("only an exercised capability can enter canary")
    capabilities.transition(
        capability_id,
        "canary",
        reason="bounded canary authorized by joined shadow evidence",
        evidence_refs=[evidence_ref],
        path=Path(ledger_path),
    )
    return capabilities.load(Path(ledger_path), create=False)[capability_id]


def reconcile_capability(
    capability_id: str,
    *,
    ledger_path: Path,
    target_registry_path: Path,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Reconcile evidence, then execute any target rollback outside ledger lock."""
    result = capabilities.reconcile_causal_lifecycle(
        capability_id, path=Path(ledger_path), timestamp=timestamp
    )
    pending = result.get("rollback_pending")
    if not pending:
        return result
    binding = capability_targets.get_binding(
        result["capability_version_id"], registry_path=Path(target_registry_path)
    )
    if not binding:
        return {**result, "rollback_status": "target_binding_missing_retryable"}
    prepared = capability_targets.prepare_rollback(
        binding,
        registry_path=Path(target_registry_path),
        reason=str(pending["reason"]),
    )
    capability_targets.apply_rollback(
        binding,
        registry_path=Path(target_registry_path),
        token=prepared["token"],
    )
    proof = capability_targets.verify_rollback(
        binding, registry_path=Path(target_registry_path)
    )
    if not proof["verified"]:
        return {**result, "rollback_status": "verification_failed_retryable"}
    retired = capabilities.complete_verified_rollback(
        capability_id,
        rollback_proof=proof["rollback_proof"],
        predecessor=str(proof["predecessor"]),
        path=Path(ledger_path),
        timestamp=timestamp,
    )
    return {
        **result,
        "status": retired["status"],
        "rollback_pending": None,
        "rollback_status": "verified_retired",
        "rollback_proof": proof["rollback_proof"],
    }


def reconcile_all(
    *,
    ledger_path: Path = capabilities.REG,
    target_registry_path: Path = DEFAULT_TARGET_REGISTRY,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Cadence entrypoint for continuous learning and verified target rollback."""
    records = capabilities.load(Path(ledger_path), create=False)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for capability_id, cap in sorted(records.items()):
        if not cap.get("capability_version_id"):
            continue
        try:
            rows.append(
                reconcile_capability(
                    capability_id,
                    ledger_path=Path(ledger_path),
                    target_registry_path=Path(target_registry_path),
                    timestamp=timestamp,
                )
            )
        except (AssertionError, OSError, ValueError) as exc:
            errors.append({"capability_id": capability_id, "error": str(exc)})
    return {"reconciled": rows, "errors": errors, "valid": not errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reconcile = sub.add_parser("reconcile-all")
    reconcile.add_argument("--ledger", type=Path, default=capabilities.REG)
    reconcile.add_argument("--target-registry", type=Path, default=DEFAULT_TARGET_REGISTRY)
    args = parser.parse_args(argv)
    result = reconcile_all(
        ledger_path=args.ledger, target_registry_path=args.target_registry
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
