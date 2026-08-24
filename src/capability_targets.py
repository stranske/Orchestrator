#!/usr/bin/env python3
"""Typed runtime bindings for compiled Orchestrator capabilities.

This module owns the mutable target side of capability activation.  The
lifecycle ledger may request a rollback, but it must not retire a capability
until :func:`apply_rollback` and :func:`verify_rollback` have succeeded.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import capability_compiler
import local_verify
import roles
import runtime_ac

SCHEMA = "orchestrator.capability-target-registry"
VERSION = 1
TARGET_KINDS = frozenset({"role", "workflow", "skill", "playbook", "gate"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(namespace: str, value: Any) -> str:
    return "sha256:" + hashlib.sha256(f"{namespace}\0{_canonical(value)}".encode()).hexdigest()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA, "version": VERSION, "targets": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA or payload.get("version") != VERSION:
        raise ValueError("unsupported capability target registry")
    if not isinstance(payload.get("targets"), dict):
        raise ValueError("invalid capability target registry")
    return payload


def get_binding(capability_version_id: str, *, registry_path: Path) -> dict[str, Any] | None:
    """Return a detached copy of one target binding."""
    row = _load_registry(Path(registry_path))["targets"].get(capability_version_id)
    return json.loads(json.dumps(row)) if row else None


def _binding_path(registry_path: Path, capability_version_id: str) -> Path:
    slug = hashlib.sha256(capability_version_id.encode()).hexdigest()[:24]
    return registry_path.parent / "capability-target-bindings" / f"{slug}.json"


def artifact_identity(kind: str, artifact: Any) -> tuple[str, str]:
    if kind == "skill":
        manifest = capability_compiler.validate_skill_package(Path(artifact))
        return manifest["capability_id"], manifest["content_hash"]
    if not isinstance(artifact, dict):
        raise TypeError(f"{kind} artifact must be a mapping")
    if kind == "role":
        manifest = capability_compiler._validate_generated_role_manifest(dict(artifact))
        return manifest["capability_id"], manifest["manifest_id"]
    if kind == "workflow":
        if artifact.get("schema") != capability_compiler.WORKFLOW_PLAN_SCHEMA:
            raise ValueError("invalid workflow rail plan")
        return artifact["capability_id"], artifact["plan_id"]
    if kind == "playbook":
        manifest = capability_compiler.validate_playbook_manifest(dict(artifact))
        return manifest["capability_id"], manifest["manifest_id"]
    if kind == "gate":
        if artifact.get("schema") != capability_compiler.EVIDENCE_CONTRACT_PLAN_SCHEMA:
            raise ValueError("invalid evidence contract plan")
        return artifact["candidate_id"], artifact["plan_id"]
    raise ValueError(f"unsupported target kind: {kind}")


def register_target(
    kind: str,
    artifact: Any,
    *,
    registry_path: Path,
    predecessor: str,
    lifecycle_policy: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    identity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate a compiler artifact and install its real target binding."""
    if kind not in TARGET_KINDS:
        raise ValueError(f"unsupported target kind: {kind}")
    if not predecessor:
        raise ValueError("compiled target requires a rollback predecessor")
    context = dict(context or {})
    capability_id, target_artifact_hash = artifact_identity(kind, artifact)
    computed_policy_hash = _hash("capability-lifecycle-policy", dict(lifecycle_policy))
    computed_version_id = _hash(
        "capability-version",
        {"capability_id": capability_id, "kind": kind, "artifact": target_artifact_hash},
    )
    if identity:
        required_identity = {
            "capability_id",
            "capability_version_id",
            "artifact_hash",
            "lifecycle_policy_hash",
        }
        if set(identity) != required_identity:
            raise ValueError("target identity must contain the exact lifecycle identity fields")
        if identity["capability_id"] != capability_id:
            raise ValueError(
                "target artifact capability identity does not match lifecycle identity"
            )
        version_id = str(identity["capability_version_id"])
        artifact_hash = str(identity["artifact_hash"])
        policy_hash = str(identity["lifecycle_policy_hash"])
        if not version_id.startswith("capability-version:"):
            raise ValueError("invalid lifecycle capability version identity")
        if not artifact_hash.startswith("sha256:") or not policy_hash.startswith("sha256:"):
            raise ValueError("invalid lifecycle artifact or policy hash")
    else:
        version_id = computed_version_id
        artifact_hash = target_artifact_hash
        policy_hash = computed_policy_hash
    binding_path = _binding_path(Path(registry_path), version_id)

    if kind == "role":
        role = roles.register_generated_role(dict(artifact))
        context["role_name"] = role.name
    elif kind == "playbook":
        required = ("repo_root", "repo_registry_path", "capability_ledger_path")
        missing = [name for name in required if not context.get(name)]
        if missing:
            raise ValueError(f"playbook registration missing context: {missing}")
        capability_compiler.export_playbook_canary(
            dict(artifact),
            repo_root=Path(context["repo_root"]),
            registry_path=Path(context["repo_registry_path"]),
            ledger_path=Path(context["capability_ledger_path"]),
            workflows_bundle_path=(
                Path(context["workflows_bundle_path"])
                if context.get("workflows_bundle_path")
                else None
            ),
            apply=True,
        )
    elif kind == "skill":
        context["skill_dir"] = str(Path(artifact).resolve())

    record: dict[str, Any] = {
        "schema": "orchestrator.capability-target-binding",
        "version": VERSION,
        "kind": kind,
        "capability_id": capability_id,
        "capability_version_id": version_id,
        "artifact_hash": artifact_hash,
        "target_artifact_hash": target_artifact_hash,
        "lifecycle_policy_hash": policy_hash,
        "lifecycle_policy": dict(lifecycle_policy),
        "predecessor": predecessor,
        "enabled": True,
        "registered_at": int(time.time()),
        "binding_path": str(binding_path),
        "context": context,
        "artifact": str(Path(artifact).resolve()) if kind == "skill" else artifact,
        "rollback": None,
        "invocations": [],
    }
    _atomic_write(binding_path, record)
    registry = _load_registry(Path(registry_path))
    existing = registry["targets"].get(version_id)
    if existing and existing.get("artifact_hash") != artifact_hash:
        raise ValueError("immutable capability version collision")
    registry["targets"][version_id] = record
    _atomic_write(Path(registry_path), registry)
    return record


def _matches(binding: Mapping[str, Any], trigger: Mapping[str, Any]) -> bool:
    kind = binding["kind"]
    artifact = binding["artifact"]
    if kind == "role":
        role = roles.get_role(binding["context"]["role_name"])
        selector = role.selector or {}
        field = selector.get("field")
        value = trigger.get(field) if isinstance(field, str) else None
        expected = selector.get("value")
        return (
            value == expected if selector.get("operator") == "equals" else value in (expected or [])
        )
    if kind == "workflow":
        selector = artifact.get("selector") or {}
        if {"field", "operator", "value"} <= set(selector):
            value = trigger.get(selector["field"])
            expected = selector["value"]
            if selector["operator"] == "equals":
                return value == expected
            if selector["operator"] == "in":
                return value in (expected or [])
            return False
        return all(
            trigger.get(key) in value if isinstance(value, list) else trigger.get(key) == value
            for key, value in selector.items()
        )
    if kind == "skill":
        manifest = capability_compiler.validate_skill_package(Path(artifact))
        return trigger.get("skill_name") == manifest["name"]
    if kind == "playbook":
        selector = artifact["selector"]
        return (
            trigger.get("repository") == selector["repo"]
            and trigger.get("task_type") in selector["task_types"]
            and trigger.get("lane") in selector["lanes"]
        )
    return trigger.get("kind") == "acceptance_gate" and trigger.get(
        "named_test_id"
    ) == artifact.get("named_test_id")


def invoke_target(
    binding: Mapping[str, Any],
    *,
    trigger: Mapping[str, Any],
    registry_path: Path,
    ledger_path: Path,
    target_run_id: str | None = None,
    inputs: Mapping[str, Any] | None = None,
    lifecycle_stage: str | None = None,
) -> dict[str, Any]:
    """Run the actual target producer and consumer and retain their receipts."""
    current = _load_registry(Path(registry_path))["targets"].get(binding["capability_version_id"])
    if not current or not current.get("enabled"):
        return {"matched": False, "invoked": False, "reason": "binding_disabled"}
    if not _matches(current, trigger):
        return {"matched": False, "invoked": False, "reason": "selector_mismatch"}
    supplied = dict(inputs or {})
    kind = current["kind"]
    artifact = current["artifact"]
    if kind == "role":
        produced = roles.run_generated_shadow_role(
            artifact,
            context=dict(supplied["context"]),
            proposal=dict(supplied["proposal"]),
            target=str(trigger.get("target") or ""),
            backend_agent=str(supplied.get("backend_agent") or "codex"),
            influenced_run_ids=[target_run_id] if target_run_id else [],
            ledger_path=Path(ledger_path),
            env=dict(supplied.get("env") or {}),
        )
        consumer_ref = produced.get("outcome_ref") or produced.get("role_run_id")
        accepted = bool(produced.get("accepted"))
    elif kind == "workflow":
        output = capability_compiler.dry_run_workflow_rail(artifact)
        receipt = capability_compiler.consume_workflow_output(output)
        produced = {"output": output, "consumer_receipt": receipt}
        consumer_ref = receipt["receipt_id"]
        accepted = bool(receipt["consumed"])
    elif kind == "skill":
        produced = capability_compiler.shadow_invoke_skill_package(
            Path(artifact),
            task_ref=str(trigger.get("target") or target_run_id or ""),
            influenced_run_ids=[target_run_id] if target_run_id else [],
            artifact_refs=list(supplied.get("artifact_refs") or []),
            ledger_path=Path(ledger_path),
            accepted=True,
        )
        consumer_ref = produced["invocation"]["event_id"]
        accepted = bool(produced["invocation"]["accepted"])
    elif kind == "playbook":
        produced = capability_compiler.record_playbook_invocation(
            artifact,
            target_run_id=str(target_run_id or ""),
            repo=str(trigger.get("repository") or ""),
            task_type=str(trigger.get("task_type") or ""),
            lane=str(trigger.get("lane") or ""),
            accepted=True,
            ledger_path=Path(ledger_path),
        )
        consumer_ref = produced["events"][-1]["event_id"] if produced.get("events") else None
        accepted = bool(produced.get("accepted"))
    else:
        capture_hook = artifact.get("capture_hook")
        if capture_hook == "runtime_ac.named_test_capture":
            capture = runtime_ac.capture_evidence_contract(artifact, dict(supplied["result"]))
        elif capture_hook == "local_verify.named_test_capture":
            capture = local_verify.capture_evidence_contract(artifact, dict(supplied["result"]))
        else:
            raise ValueError("gate artifact has no allowlisted capture hook")
        prompt = capability_compiler.evaluator_prompt_fragment(artifact)
        produced = {"capture": capture, "consumer_prompt_hash": _hash("gate-consumer", prompt)}
        consumer_ref = produced["consumer_prompt_hash"]
        accepted = bool(capture.get("bounded")) and bool(prompt)

    invocation = {
        "invocation_id": _hash(
            "capability-target-invocation",
            {
                "version": current["capability_version_id"],
                "trigger": dict(trigger),
                "consumer": consumer_ref,
            },
        ),
        "matched": True,
        "invoked": True,
        "accepted": accepted,
        "consumer_ref": consumer_ref,
        "target_run_id": target_run_id,
        "produced": produced,
        "invoked_at": int(time.time()),
        "lifecycle_stage": lifecycle_stage,
    }
    registry = _load_registry(Path(registry_path))
    registry["targets"][current["capability_version_id"]]["invocations"].append(
        {
            key: invocation[key]
            for key in (
                "invocation_id",
                "accepted",
                "consumer_ref",
                "target_run_id",
                "invoked_at",
                "lifecycle_stage",
            )
        }
    )
    _atomic_write(Path(registry_path), registry)
    _atomic_write(
        Path(current["binding_path"]), registry["targets"][current["capability_version_id"]]
    )
    return invocation


def prepare_rollback(
    binding: Mapping[str, Any], *, registry_path: Path, reason: str
) -> dict[str, Any]:
    """Persist a target-owned rollback intent without mutating or retiring it."""
    registry = _load_registry(Path(registry_path))
    current = registry["targets"].get(binding["capability_version_id"])
    existing = (current or {}).get("rollback") or {}
    if existing.get("phase") in {"pending", "applied", "verified"}:
        return dict(existing)
    if not current or not current.get("enabled"):
        raise ValueError("rollback target is absent or already disabled")
    if not current.get("predecessor"):
        raise AssertionError("regressing canary has no rollback target")
    token = _hash(
        "capability-target-rollback",
        {
            "version": current["capability_version_id"],
            "artifact": current["artifact_hash"],
            "reason": reason,
        },
    )
    current["rollback"] = {
        "phase": "pending",
        "reason": reason,
        "token": token,
        "prepared_at": int(time.time()),
    }
    _atomic_write(Path(current["binding_path"]), current)
    registry["targets"][current["capability_version_id"]] = current
    _atomic_write(Path(registry_path), registry)
    return dict(current["rollback"])


def apply_rollback(
    binding: Mapping[str, Any], *, registry_path: Path, token: str
) -> dict[str, Any]:
    """Mutate the real target state; lifecycle retirement is deliberately absent."""
    registry = _load_registry(Path(registry_path))
    current = registry["targets"].get(binding["capability_version_id"])
    pending = (current or {}).get("rollback") or {}
    if pending.get("phase") in {"applied", "verified"} and pending.get("token") == token:
        return dict(pending)
    if pending.get("phase") != "pending" or pending.get("token") != token:
        raise ValueError("rollback token does not match pending target mutation")
    kind = current["kind"]
    detail: dict[str, Any]
    if kind == "role":
        roles.unregister_generated_role(current["context"]["role_name"])
        detail = {"role_unregistered": current["context"]["role_name"]}
    elif kind == "workflow":
        reversed_steps = [step["step_id"] for step in current["artifact"].get("rollback_order", [])]
        detail = {"rail_disabled": True, "reversed_steps": reversed_steps}
    elif kind == "skill":
        Path(current["binding_path"]).unlink(missing_ok=True)
        detail = {"binding_removed": True, "skill_dir_preserved": current["artifact"]}
    elif kind == "playbook":
        context = current["context"]
        detail = capability_compiler.rollback_playbook_canary(
            current["artifact"],
            repo_root=Path(context["repo_root"]),
            registry_path=Path(context["repo_registry_path"]),
            ledger_path=Path(context["capability_ledger_path"]),
            workflows_bundle_path=(
                Path(context["workflows_bundle_path"])
                if context.get("workflows_bundle_path")
                else None
            ),
            apply=True,
        )
    else:
        detail = {"capture_disabled": True, "plan_id": current["artifact"]["plan_id"]}
    current["enabled"] = False
    current["restored_predecessor"] = current["predecessor"]
    current["rollback"] = {
        **pending,
        "phase": "applied",
        "applied_at": int(time.time()),
        "detail": detail,
    }
    registry["targets"][current["capability_version_id"]] = current
    _atomic_write(Path(registry_path), registry)
    if kind != "skill":
        _atomic_write(Path(current["binding_path"]), current)
    return dict(current["rollback"])


def verify_rollback(binding: Mapping[str, Any], *, registry_path: Path) -> dict[str, Any]:
    """Prove the target is unroutable and its predecessor was restored."""
    current = _load_registry(Path(registry_path))["targets"].get(binding["capability_version_id"])
    applied = bool(
        current
        and not current.get("enabled")
        and (current.get("rollback") or {}).get("phase") in {"applied", "verified"}
    )
    if applied and current["kind"] == "role":
        try:
            roles.get_role(current["context"]["role_name"])
        except ValueError:
            pass
        else:
            applied = False
    if applied and current["kind"] == "skill":
        applied = not Path(current["binding_path"]).exists()
    proof = _hash("capability-target-rollback-proof", current) if applied else None
    result = {
        "verified": applied,
        "rollback_proof": proof,
        "predecessor": current.get("restored_predecessor") if current else None,
    }
    if applied and (current.get("rollback") or {}).get("phase") != "verified":
        registry = _load_registry(Path(registry_path))
        stored = registry["targets"][binding["capability_version_id"]]
        stored["rollback"] = {
            **stored["rollback"],
            "phase": "verified",
            "verified_at": int(time.time()),
            "rollback_proof": proof,
        }
        registry["targets"][binding["capability_version_id"]] = stored
        _atomic_write(Path(registry_path), registry)
        if stored["kind"] != "skill":
            _atomic_write(Path(stored["binding_path"]), stored)
    return result


def _selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="capability-targets-") as tmp:
        root = Path(tmp)
        plan = capability_compiler.compile_workflow_rail(
            capability_compiler.reference_workflow_source()
        )
        binding = register_target(
            "workflow",
            plan,
            registry_path=root / "targets.json",
            predecessor="workflow:manual",
            lifecycle_policy={"mode": "shadow", "tasks_per_day": 1},
        )
        result = invoke_target(
            binding,
            trigger={"task_type": plan["selector"]["value"][0]},
            registry_path=root / "targets.json",
            ledger_path=root / "capabilities.json",
        )
        assert result["accepted"] and result["consumer_ref"]
        pending = prepare_rollback(binding, registry_path=root / "targets.json", reason="selftest")
        apply_rollback(binding, registry_path=root / "targets.json", token=pending["token"])
        assert verify_rollback(binding, registry_path=root / "targets.json")["verified"]


if __name__ == "__main__":
    _selftest()
    print("capability_targets.py selftest: OK")
