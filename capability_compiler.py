#!/usr/bin/env python3
"""Compile evidence-backed workflow and skill candidates into inert artifacts.

The compiler emits a shadow/dry-run plan only.  It has no arbitrary command or
apply surface: every step and rollback must name a typed deterministic entrypoint
from ``ENTRYPOINTS``.  Activation remains governed by ``capabilities.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import capabilities
import cross_repo_lane
import epic_lane
import feedback
import repo_knowledge
import roles
from capability_ir import CapabilityIR, Lifecycle, SourceOccurrence, canonical_json, stable_hash

WORKFLOW_SOURCE_SCHEMA = "orchestrator.workflow-rail-source"
WORKFLOW_PLAN_SCHEMA = "orchestrator.workflow-rail-plan"
WORKFLOW_RESULT_SCHEMA = "orchestrator.workflow-rail-shadow-result"
WORKFLOW_VERSION = 1

SKILL_SOURCE_SCHEMA = "orchestrator.skill-capability-source"
SKILL_MANIFEST_SCHEMA = "orchestrator.skill-capability-manifest"
SKILL_DECISION_SCHEMA = "orchestrator.skill-capability-compiler-decision"
SKILL_VERSION = 1
MAX_SKILL_RESOURCE_BYTES = 1024 * 1024
MAX_SKILL_INSTRUCTIONS = 24
MAX_SKILL_TRIGGERS = 12
SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SECRET_RE = re.compile(
    r"(?:bearer\s+[a-z0-9._~+/-]{8,}|(?:gh[opurs]_|sk-|api[_-]?key)[a-z0-9._-]{8,}|"
    r"(?:token|secret|password|credential)\s*[:=]\s*[^\s]{6,})",
    re.IGNORECASE,
)
RESOURCE_SUFFIXES = {
    "scripts": {".sh", ".py", ".js", ".ts"},
    "references": {".md", ".txt", ".json", ".yaml", ".yml"},
    "assets": {".tmpl", ".j2", ".svg", ".png"},
}
EVIDENCE_CONTRACT_SOURCE_SCHEMA = "orchestrator.evidence-contract-source"
EVIDENCE_CONTRACT_PLAN_SCHEMA = "orchestrator.evidence-contract-shadow-plan"
EVIDENCE_CAPTURE_SCHEMA = "orchestrator.named-test-evidence-capture"
EVIDENCE_CONTRACT_VERSION = 1
EVIDENCE_CONTRACT_TTL_SECONDS = 30 * 86400

ROLE_SOURCE_SCHEMA = "orchestrator.role-capability-source"
ROLE_MANIFEST_SCHEMA = "orchestrator.generated-role-manifest"
ROLE_DECISION_SCHEMA = "orchestrator.role-capability-compiler-decision"
ROLE_VERSION = 1
ROLE_TTL_SECONDS = 90 * 86400
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
ROLE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
ROLE_VALUE_TYPES = {"string", "integer", "boolean", "string_list"}
ROLE_FORBIDDEN_AUTHORITY = re.compile(
    r"\b(?:apply|claim|close|commit|delete|deploy|dispatch|kill|label|merge|mutate|push|"
    r"release|reset|revert|write)\b",
    re.IGNORECASE,
)
ROLE_IDENTITY_KEYS = {"agent", "backend", "model", "profile", "provider"}

PLAYBOOK_SOURCE_SCHEMA = "orchestrator.repo-playbook-source"
PLAYBOOK_MANIFEST_SCHEMA = "orchestrator.repo-playbook-manifest"
PLAYBOOK_DECISION_SCHEMA = "orchestrator.repo-playbook-compiler-decision"
PLAYBOOK_VERSION = 1
PLAYBOOK_TTL_SECONDS = 90 * 86400
PLAYBOOK_SECTIONS = frozenset(repo_knowledge.PLAYBOOK_SECTIONS)
PLAYBOOK_PROMPT_INJECTION = re.compile(
    r"\b(?:ignore (?:all |any )?(?:previous|prior) instructions|system prompt|developer message|"
    r"jailbreak|reveal (?:a |the )?(?:secret|token|credential)|execute arbitrary|bypass safety)\b",
    re.IGNORECASE,
)

EVIDENCE_CAPTURE_HOOKS: dict[str, dict[str, Any]] = {
    "local_verify.named_test_capture": {
        "producer": "local_verify",
        "inputs": {
            "named_test_id": "string",
            "status": "string",
            "result_hash": "artifact_ref",
            "deliberate_break_status": "string",
            "duration_ms": "integer",
        },
    },
    "runtime_ac.named_test_capture": {
        "producer": "runtime_ac",
        "inputs": {
            "named_test_id": "string",
            "status": "string",
            "result_hash": "artifact_ref",
            "deliberate_break_status": "string",
            "duration_ms": "integer",
        },
    },
}

_SAFE_TEST_ID = re.compile(r"^(?:pytest|cli-smoke|runtime-ac|local-verify):[A-Za-z0-9_./:\-]+$")
_FORBIDDEN_EVIDENCE_KEYS = {
    "command",
    "cmd",
    "raw_output",
    "stdout",
    "stderr",
    "stdout_tail",
    "stderr_tail",
    "secret",
    "secrets",
    "token",
    "prompt",
}

VALUE_TYPES = {"string", "path", "repository", "ref", "boolean", "integer", "artifact_ref"}
CONDITION_IDS = {
    "kill_switch_off",
    "not_expired",
    "repository_exists",
    "remote_reachable",
    "remote_synced",
    "workspace_root_clean",
    "named_test_gate_passed",
    "consumer_sync_plan_valid",
    "consumer_sync_shadow_classified",
}

# These identifiers correspond to deterministic local skills/modules.  They are
# data contracts, never shell templates.
ENTRYPOINTS: dict[str, dict[str, Any]] = {
    "git_remote_sync.sync": {
        "inputs": {"repository": "repository", "remote": "string", "branch": "ref"},
        "outputs": {"before_sha": "ref", "after_sha": "ref", "status": "string"},
        "side_effect_policy": "guarded_remote",
        "rollbacks": {"git_remote_sync.restore_ref"},
    },
    "git_remote_sync.restore_ref": {
        "inputs": {"repository": "repository", "ref": "ref"},
        "outputs": {"status": "string"},
        "side_effect_policy": "guarded_remote",
        "rollbacks": set(),
    },
    "code_workspace_hygiene.audit": {
        "inputs": {"workspace_root": "path"},
        "outputs": {"clean": "boolean", "finding_ref": "artifact_ref"},
        "side_effect_policy": "read_only",
        "rollbacks": {"workflow.noop"},
    },
    "local_verify.named_test_gate": {
        "inputs": {"workspace_root": "path", "gate_id": "string"},
        "outputs": {"passed": "boolean", "result_ref": "artifact_ref"},
        "side_effect_policy": "read_only",
        "rollbacks": {"workflow.noop"},
    },
    "workflow.noop": {
        "inputs": {},
        "outputs": {"status": "string"},
        "side_effect_policy": "read_only",
        "rollbacks": set(),
    },
    "consumer_sync_shadow.classify": {
        "inputs": {"plan_ref": "artifact_ref", "repository": "repository"},
        "outputs": {"result_ref": "artifact_ref", "proposal_count": "integer"},
        "side_effect_policy": "read_only",
        "rollbacks": {"workflow.noop"},
    },
}


class WorkflowCompileError(ValueError):
    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(dict.fromkeys(str(reason) for reason in reasons))
        super().__init__("; ".join(self.reasons))


def workflow_step_idempotency_key(
    capability_id: str, step_id: str, version: int, entrypoint: str, inputs: dict[str, Any]
) -> str:
    digest = stable_hash(
        "workflow-step-idempotency",
        {
            "capability_id": capability_id,
            "step_id": step_id,
            "version": version,
            "entrypoint": entrypoint,
            "inputs": inputs,
        },
    ).split(":", 1)[1]
    return f"workflow-step:{digest}"


def _typed_values(
    values: Any, contract: dict[str, str], path: str
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(values, dict):
        return {}, [f"{path} must be an object"]
    unknown = sorted(set(values) - set(contract))
    missing = sorted(set(contract) - set(values))
    if unknown:
        errors.append(f"{path} has unsupported inputs: {unknown}")
    if missing:
        errors.append(f"{path} is missing inputs: {missing}")
    typed: dict[str, Any] = {}
    for name, type_name in sorted(contract.items()):
        value = values.get(name)
        if type_name == "integer" and not isinstance(value, int):
            errors.append(f"{path}.{name} must be an integer")
        elif type_name == "boolean" and not isinstance(value, bool):
            errors.append(f"{path}.{name} must be a boolean")
        elif type_name not in {"integer", "boolean"} and not isinstance(value, str):
            errors.append(f"{path}.{name} must be a string")
        elif isinstance(value, str) and not value.strip():
            errors.append(f"{path}.{name} must be non-empty")
        typed[name] = {"type": type_name, "value": value}
    return typed, errors


def _conditions(value: Any, path: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], [f"{path} must be a list"]
    errors: list[str] = []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"check", "expected"}:
            errors.append(f"{path}[{index}] must contain only check and expected")
            continue
        check = str(item.get("check") or "")
        if check not in CONDITION_IDS:
            if any(word in check.lower() for word in ("judge", "judgment", "decide", "review")):
                errors.append(f"ambiguous judgment: {path}[{index}]")
            else:
                errors.append(f"unsupported condition: {check}")
            continue
        expected = item.get("expected")
        if not isinstance(expected, (bool, int, str)):
            errors.append(f"{path}[{index}].expected must be a scalar")
            continue
        result.append({"check": check, "expected": expected})
    return result, errors


def _has_dependency_path(
    by_id: dict[str, dict[str, Any]], start: str, target: str, seen: set[str] | None = None
) -> bool:
    if start == target:
        return True
    if start not in by_id:
        return False
    visited = set(seen or ())
    if start in visited:
        return False
    visited.add(start)
    return any(
        _has_dependency_path(by_id, dependency, target, visited)
        for dependency in by_id[start].get("depends_on") or []
    )


def compile_workflow_rail(source: dict[str, Any]) -> dict[str, Any]:
    """Compile one strict source object into a non-executable workflow plan."""
    errors: list[str] = []
    if not isinstance(source, dict):
        raise WorkflowCompileError(["workflow source must be an object"])
    source_fields = {
        "schema",
        "version",
        "capability_id",
        "source_ir_ref",
        "selector",
        "steps",
        "barriers",
        "expires_at",
        "kill_switch",
    }
    unknown_source_fields = sorted(set(source) - source_fields)
    if unknown_source_fields:
        errors.append(f"workflow source has unsupported fields: {unknown_source_fields}")
    if source.get("schema") != WORKFLOW_SOURCE_SCHEMA or source.get("version") != WORKFLOW_VERSION:
        errors.append("unsupported workflow source schema")
    capability_id = str(source.get("capability_id") or "")
    if not capability_id.startswith("capability:"):
        errors.append("invalid capability_id")
    if not str(source.get("source_ir_ref") or "").startswith("capability-ir:"):
        errors.append("workflow source requires a capability IR reference")
    selector = source.get("selector")
    allowed_selector_fields = {"task_type", "repository", "capability_id"}
    if not isinstance(selector, dict) or set(selector) != {"field", "operator", "value"}:
        errors.append("selector must contain field, operator, and value")
    elif selector.get("field") not in allowed_selector_fields or selector.get("operator") not in {
        "equals",
        "in",
    }:
        errors.append("ambiguous judgment: selector")
    elif selector.get("operator") == "equals" and not isinstance(selector.get("value"), str):
        errors.append("selector equals value must be a string")
    elif selector.get("operator") == "in" and not isinstance(selector.get("value"), list):
        errors.append("selector in value must be a list")

    steps = source.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowCompileError([*errors, "workflow requires steps"])
    if any(step.get("requires_judgment") for step in steps if isinstance(step, dict)):
        errors.append("ambiguous judgment: workflow step")
    dag_errors = epic_lane.validate_dependency_dag(steps)
    errors.extend(dag_errors)

    keys = [str(step.get("idempotency_key") or "") for step in steps if isinstance(step, dict)]
    if len(keys) != len(set(keys)):
        errors.append("duplicate idempotency key")

    compiled_by_id: dict[str, dict[str, Any]] = {}
    allowed_step_fields = {
        "id",
        "version",
        "entrypoint",
        "inputs",
        "depends_on",
        "idempotency_key",
        "preconditions",
        "postconditions",
        "retry",
        "timeout_seconds",
        "side_effect_policy",
        "rollback",
        "requires_judgment",
    }
    for index, step in enumerate(steps):
        path = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{path} must be an object")
            continue
        unknown = sorted(set(step) - allowed_step_fields)
        if unknown:
            errors.append(f"{path} has unsupported fields: {unknown}")
        step_id = str(step.get("id") or "").strip()
        version = step.get("version")
        entrypoint = str(step.get("entrypoint") or "")
        if not isinstance(version, int) or version < 1:
            errors.append(f"{path}.version must be a positive integer")
        spec = ENTRYPOINTS.get(entrypoint)
        if spec is None:
            errors.append(f"unallowlisted command: {entrypoint}")
            continue
        typed_inputs, input_errors = _typed_values(
            step.get("inputs"), spec["inputs"], f"{path}.inputs"
        )
        errors.extend(input_errors)
        if step.get("side_effect_policy") != spec["side_effect_policy"]:
            errors.append(f"side-effect policy mismatch: {step_id}")
        preconditions, condition_errors = _conditions(
            step.get("preconditions"), f"{path}.preconditions"
        )
        errors.extend(condition_errors)
        postconditions, condition_errors = _conditions(
            step.get("postconditions"), f"{path}.postconditions"
        )
        errors.extend(condition_errors)
        retry = step.get("retry")
        if not isinstance(retry, dict) or set(retry) != {"max_attempts", "backoff_seconds"}:
            errors.append(f"{path}.retry must contain max_attempts and backoff_seconds")
        elif not isinstance(retry["max_attempts"], int) or not 1 <= retry["max_attempts"] <= 5:
            errors.append(f"{path}.retry.max_attempts must be between 1 and 5")
        elif (
            not isinstance(retry["backoff_seconds"], int)
            or not 0 <= retry["backoff_seconds"] <= 300
        ):
            errors.append(f"{path}.retry.backoff_seconds must be between 0 and 300")
        timeout = step.get("timeout_seconds")
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            errors.append(f"{path}.timeout_seconds must be between 1 and 3600")
        rollback = step.get("rollback")
        if not isinstance(rollback, dict):
            errors.append(f"missing rollback: {step_id}")
            rollback = {}
        rollback_entrypoint = str(rollback.get("entrypoint") or "")
        rollback_spec = ENTRYPOINTS.get(rollback_entrypoint)
        if rollback_entrypoint not in spec["rollbacks"] or rollback_spec is None:
            errors.append(f"invalid rollback: {step_id}")
            typed_rollback_inputs = {}
        else:
            typed_rollback_inputs, rollback_errors = _typed_values(
                rollback.get("inputs"), rollback_spec["inputs"], f"{path}.rollback.inputs"
            )
            errors.extend(rollback_errors)
        expected_key = workflow_step_idempotency_key(
            capability_id, step_id, int(version or 0), entrypoint, step.get("inputs") or {}
        )
        if step.get("idempotency_key") != expected_key:
            errors.append(f"non-deterministic idempotency key: {step_id}")
        compiled_by_id[step_id] = {
            "id": step_id,
            "version": version,
            "entrypoint": entrypoint,
            "inputs": typed_inputs,
            "outputs": {
                name: {"type": type_name} for name, type_name in sorted(spec["outputs"].items())
            },
            "depends_on": sorted(step.get("depends_on") or []),
            "idempotency_key": step.get("idempotency_key"),
            "preconditions": preconditions,
            "postconditions": postconditions,
            "retry": retry,
            "timeout_seconds": timeout,
            "side_effect_policy": spec["side_effect_policy"],
            "rollback": {"entrypoint": rollback_entrypoint, "inputs": typed_rollback_inputs},
        }

    barriers = source.get("barriers")
    if not isinstance(barriers, list):
        errors.append("barriers must be a list")
        barriers = []
    errors.extend(cross_repo_lane.validate_deterministic_barriers(list(compiled_by_id), barriers))
    for barrier in barriers:
        if not isinstance(barrier, dict):
            continue
        if barrier.get("condition_id") not in CONDITION_IDS:
            errors.append(f"unsupported barrier condition: {barrier.get('condition_id')}")
        before, after = str(barrier.get("before") or ""), str(barrier.get("after") or "")
        if (
            before in compiled_by_id
            and after in compiled_by_id
            and not _has_dependency_path(compiled_by_id, before, after)
        ):
            errors.append(f"barrier is not represented in DAG: {barrier.get('id')}")
    expiry = source.get("expires_at")
    if not isinstance(expiry, int) or expiry <= 0:
        errors.append("expires_at must be a positive integer")
    kill_switch = str(source.get("kill_switch") or "")
    if not kill_switch:
        errors.append("kill_switch is required")
    if errors:
        raise WorkflowCompileError(errors)

    order = epic_lane.dependency_order(steps)
    compiled_steps = [
        {**compiled_by_id[step_id], "ordinal": index} for index, step_id in enumerate(order, 1)
    ]
    normalized_barriers = sorted(
        (
            {key: barrier[key] for key in ("id", "after", "before", "condition_id")}
            for barrier in barriers
        ),
        key=lambda item: item["id"],
    )
    plan_core = {
        "schema": WORKFLOW_PLAN_SCHEMA,
        "version": WORKFLOW_VERSION,
        "capability_id": capability_id,
        "source_ir_ref": source.get("source_ir_ref"),
        "selector": selector,
        "steps": compiled_steps,
        "barriers": normalized_barriers,
        "rollback_order": [
            {"step_id": step["id"], **step["rollback"]} for step in reversed(compiled_steps)
        ],
        "execution_policy": {
            "executable": False,
            "mode": "shadow_dry_run",
            "allow_arbitrary_shell": False,
            "side_effects_permitted": False,
        },
        "lifecycle": {
            "state": "shadow",
            "expires_at": expiry,
            "kill_switch": kill_switch,
            "rollback": "reverse_topological_allowlisted_rollback",
        },
    }
    return {**plan_core, "plan_id": stable_hash("workflow-rail-plan", plan_core)}


def compile_workflow_candidate(source: dict[str, Any]) -> dict[str, Any]:
    """Return a compiler decision, retaining rejected actions as inert proposals."""
    try:
        plan = compile_workflow_rail(source)
    except WorkflowCompileError as exc:
        core = {
            "schema": "orchestrator.workflow-rail-compiler-decision",
            "version": WORKFLOW_VERSION,
            "status": "proposal",
            "executable": False,
            "rejection_reasons": list(exc.reasons),
            "capability_id": (
                str(source.get("capability_id") or "") if isinstance(source, dict) else ""
            ),
        }
        return {**core, "decision_id": stable_hash("workflow-rail-compiler-decision", core)}
    return {
        "schema": "orchestrator.workflow-rail-compiler-decision",
        "version": WORKFLOW_VERSION,
        "status": "compiled_shadow",
        "executable": False,
        "rejection_reasons": [],
        "capability_id": plan["capability_id"],
        "plan": plan,
        "decision_id": stable_hash("workflow-rail-compiler-decision", plan["plan_id"]),
    }


def dry_run_workflow_rail(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("schema") != WORKFLOW_PLAN_SCHEMA or plan.get("version") != WORKFLOW_VERSION:
        raise ValueError("unsupported workflow rail plan")
    rows = [
        {
            "step_id": step["id"],
            "status": "would_run",
            "idempotency_key": step["idempotency_key"],
            "side_effect_recorded": False,
        }
        for step in plan["steps"]
    ]
    core = {
        "schema": WORKFLOW_RESULT_SCHEMA,
        "version": WORKFLOW_VERSION,
        "plan_id": plan["plan_id"],
        "capability_id": plan["capability_id"],
        "status": "shadow_pass",
        "steps": rows,
        "side_effects": [],
        "consumed": False,
    }
    return {**core, "result_id": stable_hash("workflow-rail-result", core)}


def consume_workflow_output(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("schema") != WORKFLOW_RESULT_SCHEMA or result.get("side_effects") != []:
        raise ValueError("invalid shadow workflow output")
    if any(row.get("status") != "would_run" for row in result.get("steps") or []):
        raise ValueError("incomplete shadow workflow output")
    return {
        "consumer": "capability_compiler.reference_workflow_consumer",
        "result_id": result["result_id"],
        "receipt_id": stable_hash("workflow-rail-consumer", result["result_id"]),
        "consumed": True,
    }


def _register_shadow(plan: dict[str, Any], ledger_path: Path) -> None:
    capability_id = plan["capability_id"]
    existing = capabilities.load(ledger_path, create=False)
    if capability_id in existing:
        cap = existing[capability_id]
        prior_plan_id = (cap.get("activation_evidence") or {}).get("workflow_plan_id")
        if prior_plan_id != plan["plan_id"]:
            if cap.get("status") != "shadow":
                raise ValueError("non-shadow capability cannot adopt a different workflow plan")
            evidence = cap.setdefault("activation_evidence", {})
            prior_ids = list(evidence.get("workflow_plan_previous_ids") or [])
            if prior_plan_id and prior_plan_id not in prior_ids:
                prior_ids.append(prior_plan_id)
            evidence["workflow_plan_previous_ids"] = prior_ids
            evidence["workflow_plan_id"] = plan["plan_id"]
            cap["matcher"] = plan["selector"]
            cap["rollback"] = {"steps": plan["rollback_order"]}
            cap.setdefault("event_history", []).append(
                {
                    "timestamp": int(time.time()),
                    "type": "workflow_plan_extended",
                    "from": prior_plan_id,
                    "to": plan["plan_id"],
                    "reason": "typed consumer-sync shadow step added",
                }
            )
            capabilities.validate_capability(cap)
            capabilities.save(existing, ledger_path)
        return
    capabilities.register(
        capability_id,
        {
            "status": "shadow",
            "owner": "orchestrator",
            "matcher": plan["selector"],
            "entrypoint": "capability_compiler.py:run_reference_workflow",
            "trigger_cadence": "supervised CLI and focused activation probe",
            "flags_defaults": {"mode": "shadow_dry_run"},
            "output_artifact": WORKFLOW_RESULT_SCHEMA,
            "downstream_consumer": "capability_compiler.py:consume_workflow_output",
            "learning_sink": "capabilities lifecycle ledger",
            "activation_evidence": {"workflow_plan_id": plan["plan_id"]},
            "gate_reason": "compiled workflow remains candidate-only in shadow",
            "gate_evidence": "compiler exposes no apply or arbitrary command surface",
            "evidence_threshold": "supervised durable outcomes justify a separate canary decision",
            "activation_deadline": plan["lifecycle"]["expires_at"],
            "expiry": plan["lifecycle"]["expires_at"],
            "next_transition": "retired",
            "kill_switch": plan["lifecycle"]["kill_switch"],
            "rollback": {"steps": plan["rollback_order"]},
        },
        path=ledger_path,
    )


class EvidenceContractCompileError(ValueError):
    """A candidate could not be safely compiled into a shadow contract."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(dict.fromkeys(str(reason) for reason in reasons))
        super().__init__("; ".join(self.reasons))


def _forbidden_evidence_paths(value: Any, path: str = "source") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            child = f"{path}.{key}"
            if (
                key_text in _FORBIDDEN_EVIDENCE_KEYS
                or key_text.endswith("_token")
                or "secret" in key_text
                or "password" in key_text
            ):
                findings.append(f"{child} is forbidden in a bounded evidence contract")
            findings.extend(_forbidden_evidence_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_forbidden_evidence_paths(item, f"{path}[{index}]"))
    return findings


def _canonical_subject_count(candidate: Any) -> tuple[float, list[str]]:
    errors: list[str] = []
    if not isinstance(candidate, dict):
        return 0.0, ["candidate must be an object"]
    subjects = candidate.get("independent_subjects")
    specs = candidate.get("independent_specs")
    if not isinstance(subjects, list) or not all(isinstance(x, str) and x for x in subjects):
        errors.append("candidate.independent_subjects must contain canonical IDs")
        subjects = []
    if not isinstance(specs, list) or not all(isinstance(x, str) and x for x in specs):
        errors.append("candidate.independent_specs must contain canonical hashes")
        specs = []
    effective = candidate.get("effective_subject_count")
    if not isinstance(effective, (int, float)):
        errors.append("candidate.effective_subject_count must be numeric")
        return 0.0, errors
    canonical = min(float(effective), float(len(set(subjects))), float(len(set(specs))))
    if float(effective) != canonical:
        errors.append("effective subject count does not match canonical subject/spec identities")
    return canonical, errors


def build_evidence_contract_source(
    candidate: dict[str, Any],
    *,
    capture_hook: str = "local_verify.named_test_capture",
    named_test_id: str = (
        "pytest:test_evidence_contract_compiler.py::"
        "test_paraphrased_gaps_form_one_distinct_subject_candidate"
    ),
    live_gate_id: str = "local-verify:named-test-deliberate-break",
    now: int | None = None,
) -> dict[str, Any]:
    """Build an inert, inspectable source document for the first shadow."""
    current = int(time.time()) if now is None else int(now)
    candidate_expiry = (candidate.get("lifecycle") or {}).get("expires_at")
    expires_at = min(
        int(candidate_expiry or current + EVIDENCE_CONTRACT_TTL_SECONDS),
        current + EVIDENCE_CONTRACT_TTL_SECONDS,
    )
    return {
        "schema": EVIDENCE_CONTRACT_SOURCE_SCHEMA,
        "version": EVIDENCE_CONTRACT_VERSION,
        "candidate": candidate,
        "capture_hook": capture_hook,
        "named_test_id": named_test_id,
        "live_gate_id": live_gate_id,
        "deliberate_break": {
            "required": True,
            "expected": "named_test_fails",
            "failure_message": "one subject met promotion threshold",
        },
        "capture_contract": {
            "inputs": dict(EVIDENCE_CAPTURE_HOOKS.get(capture_hook, {}).get("inputs", {})),
            "output": {"capture_ref": "artifact_ref", "bounded": "boolean"},
        },
        "evaluation": {
            "citation_field": "cited_evidence_contracts",
            "influence_measures": [
                "later_agreement_delta",
                "later_decisiveness_delta",
                "later_gap_delta",
                "rework",
                "durability",
            ],
            "citation_is_not_influence": True,
        },
        "created_at": current,
        "expires_at": expires_at,
        "rollback": {
            "action": "disable_capture_hook_and_retire_candidate",
            "triggers": ["expired", "no_influence", "recurring_harm"],
        },
    }


def compile_evidence_contract(source: dict[str, Any]) -> dict[str, Any]:
    """Compile a semantic gap candidate to a non-executable shadow plan."""
    if not isinstance(source, dict):
        raise EvidenceContractCompileError(["source must be an object"])
    errors = _forbidden_evidence_paths(source)
    expected_keys = {
        "schema",
        "version",
        "candidate",
        "capture_hook",
        "named_test_id",
        "live_gate_id",
        "deliberate_break",
        "capture_contract",
        "evaluation",
        "created_at",
        "expires_at",
        "rollback",
    }
    unknown = sorted(set(source) - expected_keys)
    missing = sorted(expected_keys - set(source))
    if unknown:
        errors.append(f"source has unsupported fields: {unknown}")
    if missing:
        errors.append(f"source is missing fields: {missing}")
    if source.get("schema") != EVIDENCE_CONTRACT_SOURCE_SCHEMA or source.get("version") != 1:
        errors.append("unsupported evidence contract source schema")

    candidate = source.get("candidate")
    effective, identity_errors = _canonical_subject_count(candidate)
    errors.extend(identity_errors)
    if effective < 3:
        errors.append("candidate requires at least three independent subject/spec identities")
    lifecycle = (candidate or {}).get("lifecycle") if isinstance(candidate, dict) else {}
    if not isinstance(lifecycle, dict) or lifecycle.get("candidate_only") is not True:
        errors.append("candidate must remain candidate-only")
    if isinstance(lifecycle, dict) and lifecycle.get("promotion_allowed") is not False:
        errors.append("candidate may not grant its own promotion")

    hook = source.get("capture_hook")
    if hook not in EVIDENCE_CAPTURE_HOOKS:
        errors.append("capture hook is not allowlisted")
    named_test_id = source.get("named_test_id")
    live_gate_id = source.get("live_gate_id")
    if not isinstance(named_test_id, str) or not _SAFE_TEST_ID.fullmatch(named_test_id):
        errors.append("named_test_id is not a bounded named test identifier")
    if not isinstance(live_gate_id, str) or not _SAFE_TEST_ID.fullmatch(live_gate_id):
        errors.append("live_gate_id is not a bounded named gate identifier")
    deliberate = source.get("deliberate_break")
    if not isinstance(deliberate, dict) or deliberate.get("required") is not True:
        errors.append("a deliberate-break check is required")
    elif deliberate.get("expected") != "named_test_fails":
        errors.append("deliberate-break must expect the named test to fail")

    expected_capture = EVIDENCE_CAPTURE_HOOKS.get(str(hook), {}).get("inputs")
    capture = source.get("capture_contract")
    if not isinstance(capture, dict) or capture.get("inputs") != expected_capture:
        errors.append("capture contract inputs do not match the allowlisted typed hook")
    elif capture.get("output") != {"capture_ref": "artifact_ref", "bounded": "boolean"}:
        errors.append("capture contract output is not bounded")
    evaluation = source.get("evaluation")
    required_measures = {
        "later_agreement_delta",
        "later_decisiveness_delta",
        "later_gap_delta",
        "rework",
        "durability",
    }
    if (
        not isinstance(evaluation, dict)
        or not required_measures.issubset(set(evaluation.get("influence_measures") or []))
        or evaluation.get("citation_is_not_influence") is not True
    ):
        errors.append("evaluation must measure outcome influence separately from citations")
    created_at = source.get("created_at")
    expires_at = source.get("expires_at")
    if (
        not isinstance(created_at, int)
        or not isinstance(expires_at, int)
        or expires_at <= created_at
    ):
        errors.append("contract requires a future integer expiry")
    rollback = source.get("rollback")
    if (
        not isinstance(rollback, dict)
        or rollback.get("action") != "disable_capture_hook_and_retire_candidate"
    ):
        errors.append("contract requires an explicit disable-and-retire rollback")
    if errors:
        raise EvidenceContractCompileError(errors)

    material = {key: source[key] for key in sorted(source)}
    plan_id = stable_hash("evidence-contract-shadow-plan", material)
    return {
        "schema": EVIDENCE_CONTRACT_PLAN_SCHEMA,
        "version": EVIDENCE_CONTRACT_VERSION,
        "plan_id": plan_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_name": candidate["name"],
        "effective_subject_count": effective,
        "capture_hook": hook,
        "named_test_id": named_test_id,
        "live_gate_id": live_gate_id,
        "deliberate_break": deliberate,
        "capture_contract": capture,
        "evaluation": evaluation,
        "created_at": created_at,
        "expires_at": expires_at,
        "rollback": rollback,
        "lifecycle": {
            "state": "compiled_shadow_candidate",
            "candidate_only": True,
            "executable": False,
            "promotion_allowed": False,
        },
    }


def compile_first_shadow_contract(
    candidate: dict[str, Any], *, now: int | None = None
) -> dict[str, Any]:
    """Compile the first named-test/smoke/deliberate-break candidate."""
    if candidate.get("name") != "named_test_smoke_deliberate_break":
        raise EvidenceContractCompileError(
            ["first shadow only supports the named-test evidence cluster"]
        )
    return compile_evidence_contract(build_evidence_contract_source(candidate, now=now))


def capture_named_test_evidence(plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Capture only typed status and a digest; never command text or raw output."""
    if plan.get("schema") != EVIDENCE_CONTRACT_PLAN_SCHEMA:
        raise ValueError("unsupported evidence contract plan")
    if plan.get("capture_hook") not in EVIDENCE_CAPTURE_HOOKS:
        raise ValueError("capture hook is not allowlisted")
    forbidden = _forbidden_evidence_paths(result, "result")
    if forbidden:
        raise ValueError("; ".join(forbidden))
    expected = set(EVIDENCE_CAPTURE_HOOKS[plan["capture_hook"]]["inputs"])
    if not isinstance(result, dict) or set(result) != expected:
        raise ValueError(f"capture result must contain exactly {sorted(expected)}")
    if result.get("named_test_id") != plan.get("named_test_id"):
        raise ValueError("capture named_test_id does not match the plan")
    if result.get("status") not in {"PASS", "FAIL", "ERROR"}:
        raise ValueError("invalid named test status")
    if result.get("deliberate_break_status") not in {"PASS", "FAIL", "NOT_RUN"}:
        raise ValueError("invalid deliberate-break status")
    if not isinstance(result.get("duration_ms"), int) or result["duration_ms"] < 0:
        raise ValueError("duration_ms must be a non-negative integer")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(result.get("result_hash") or "")):
        raise ValueError("result_hash must be a sha256 artifact reference")
    payload = {key: result[key] for key in sorted(result)}
    return {
        "schema": EVIDENCE_CAPTURE_SCHEMA,
        "version": 1,
        "capture_ref": stable_hash("named-test-evidence-capture", payload),
        "contract_plan_id": plan["plan_id"],
        "producer": EVIDENCE_CAPTURE_HOOKS[plan["capture_hook"]]["producer"],
        "evidence": payload,
        "bounded": True,
    }


def evaluator_prompt_fragment(plan: dict[str, Any]) -> str:
    """Prompt contract for citing captures without equating citation with influence."""
    return (
        "Optional bounded evidence contract " + plan["plan_id"] + ": inspect only captures "
        f"from {plan['capture_hook']} for named test {plan['named_test_id']} and live gate "
        f"{plan['live_gate_id']}. If it materially affects the verdict, return its plan ID in "
        "cited_evidence_contracts. A citation alone is not influence; later agreement, "
        "decisiveness, evidence gaps, rework, and durability are measured separately."
    )


def measure_contract_influence(
    plan: dict[str, Any], uses: Sequence[dict[str, Any]], *, now: int | None = None
) -> dict[str, Any]:
    """Measure later outcomes, retain counterexamples, and apply expiry/rollback."""
    current = int(time.time()) if now is None else int(now)
    citations = sum(bool(row.get("cited")) for row in uses)
    influential = 0
    harms = 0
    counterexamples = []
    for row in uses:
        agreement = float(row.get("later_agreement_delta") or 0)
        decisive = float(row.get("later_decisiveness_delta") or 0)
        gap_delta = float(row.get("later_gap_delta") or 0)
        durability = str(row.get("durability") or "unknown")
        rework = bool(row.get("rework"))
        harm = (
            rework
            or agreement < 0
            or decisive < 0
            or gap_delta > 0
            or durability
            in {
                "reverted",
                "abandoned",
                "regressed",
            }
        )
        improved = agreement > 0 or decisive > 0 or gap_delta < 0 or durability == "durable"
        if improved and not harm:
            influential += 1
        if harm:
            harms += 1
            counterexamples.append(dict(row))
    expired = current >= int(plan["expires_at"])
    retirement_reason = None
    if expired and influential == 0:
        retirement_reason = "expired_without_influence"
    elif harms >= 2 and harms > influential:
        retirement_reason = "recurring_harm"
    retired = retirement_reason is not None
    return {
        "plan_id": plan["plan_id"],
        "citation_count": citations,
        "influential_use_count": influential,
        "harm_count": harms,
        "counterexamples": counterexamples,
        "state": "retired" if retired else "shadow_monitoring",
        "retirement_reason": retirement_reason,
        "rollback": {
            "performed": retired,
            "capture_hook_enabled": not retired,
            "action": plan["rollback"]["action"] if retired else None,
        },
    }


def evidence_contract_issue_body(plan: dict[str, Any]) -> str:
    """Render an agent-ready issue body from the compiled, still-inert plan."""
    return f"""## Problem
Evaluators lack consistent bounded proof from named tests and deliberate-break checks.

## Scope
- Integrate `capability_compiler.py`, `evidence_schema.py`, `local_verify.py`, and `runtime_ac.py`.
- Keep contract `{plan['plan_id']}` candidate-only and non-executable.

## Acceptance criteria
- [ ] Ensure only allowlisted typed captures from `{plan['capture_hook']}` are accepted.
- [ ] Ensure evaluator prompts can cite the contract while influence is measured from later outcomes.
- [ ] Ensure expiry or recurring harm disables the hook and retires the candidate.

## Verification
Run `pytest -q test_evidence_contract_compiler.py::test_paraphrased_gaps_form_one_distinct_subject_candidate`.
Run the deliberate-break fixture with one subject and confirm `AssertionError: one subject met promotion threshold`.

## Non-goals
Do not activate the contract, run arbitrary commands, or retain raw output or secrets.
"""


def validate_contract_issue_format(plan: dict[str, Any]) -> dict[str, Any]:
    import issue_quality

    features = issue_quality.extract_issue_features(evidence_contract_issue_body(plan))
    if not features["has_acceptance_criteria"] or not features["has_test_instructions"]:
        raise ValueError("generated evidence-contract issue is not agent-ready")
    return features


def _probe_once(capability_id: str, probe: str, ref: str, ledger_path: Path) -> None:
    cap = capabilities.load(ledger_path, create=False)[capability_id]
    existing = (cap.get("activation_evidence") or {}).get(probe) or {}
    if existing.get("passed") and existing.get("ref") == ref:
        return
    capabilities.record_probe(capability_id, probe, passed=True, ref=ref, path=ledger_path)


def run_reference_workflow(
    *,
    ledger_path: Path,
    consumer: Callable[[dict[str, Any]], dict[str, Any]] = consume_workflow_output,
) -> dict[str, Any]:
    """Real shadow caller: compile, dry-run, consume, and ledger the reference rail."""
    plan = compile_workflow_rail(reference_workflow_source())
    _register_shadow(plan, ledger_path)
    capability_id = plan["capability_id"]
    event_prefix = plan["plan_id"]
    capabilities.heartbeat(
        capability_id,
        "match",
        ref=event_prefix,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:match",
    )
    capabilities.heartbeat(
        capability_id,
        "invocation",
        ref=event_prefix,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:invocation",
    )
    try:
        result = dry_run_workflow_rail(plan)
        capabilities.heartbeat(
            capability_id,
            "output",
            ref=result["result_id"],
            path=ledger_path,
            idempotency_key=f"{event_prefix}:output",
        )
        receipt = consumer(result)
        capabilities.heartbeat(
            capability_id,
            "consumer",
            ref=receipt["receipt_id"],
            path=ledger_path,
            idempotency_key=f"{event_prefix}:consumer",
        )
        capabilities.heartbeat(
            capability_id,
            "success",
            ref=receipt["receipt_id"],
            path=ledger_path,
            idempotency_key=f"{event_prefix}:success",
        )
        capabilities.heartbeat(
            capability_id,
            "outcome",
            ref=f"shadow:{receipt['receipt_id']}",
            path=ledger_path,
            idempotency_key=f"{event_prefix}:outcome",
        )
        _probe_once(capability_id, "producer_probe", result["result_id"], ledger_path)
        _probe_once(capability_id, "consumer_probe", receipt["receipt_id"], ledger_path)
        _probe_once(capability_id, "outcome_probe", f"shadow:{receipt['receipt_id']}", ledger_path)
        _probe_once(
            capability_id,
            "rollback_probe",
            stable_hash("workflow-rollback", plan["rollback_order"]),
            ledger_path,
        )
        return {"plan": plan, "result": result, "consumer_receipt": receipt}
    except Exception as exc:
        error_ref = stable_hash(
            "workflow-shadow-failure",
            {"plan_id": plan["plan_id"], "error_type": type(exc).__name__},
        )
        capabilities.heartbeat(
            capability_id,
            "failure",
            ref=error_ref,
            path=ledger_path,
            idempotency_key=f"{event_prefix}:failure:{error_ref}",
        )
        raise


def reference_workflow_source() -> dict[str, Any]:
    capability_id = "capability:reference-sync-hygiene-test-gate"

    def step(
        step_id: str, entrypoint: str, inputs: dict[str, Any], depends_on: list[str], **rest: Any
    ) -> dict[str, Any]:
        version = 1
        return {
            "id": step_id,
            "version": version,
            "entrypoint": entrypoint,
            "inputs": inputs,
            "depends_on": depends_on,
            "idempotency_key": workflow_step_idempotency_key(
                capability_id, step_id, version, entrypoint, inputs
            ),
            **rest,
        }

    return {
        "schema": WORKFLOW_SOURCE_SCHEMA,
        "version": WORKFLOW_VERSION,
        "capability_id": capability_id,
        "source_ir_ref": "capability-ir:reference-durable-pattern",
        "selector": {
            "field": "task_type",
            "operator": "in",
            "value": ["maintenance", "consumer_sync_drift"],
        },
        "steps": [
            step(
                "sync",
                "git_remote_sync.sync",
                {"repository": "owner/repo", "remote": "origin", "branch": "main"},
                [],
                preconditions=[
                    {"check": "kill_switch_off", "expected": True},
                    {"check": "remote_reachable", "expected": True},
                ],
                postconditions=[{"check": "remote_synced", "expected": True}],
                retry={"max_attempts": 3, "backoff_seconds": 5},
                timeout_seconds=120,
                side_effect_policy="guarded_remote",
                rollback={
                    "entrypoint": "git_remote_sync.restore_ref",
                    "inputs": {"repository": "owner/repo", "ref": "pre-sync"},
                },
            ),
            step(
                "consumer-drift",
                "consumer_sync_shadow.classify",
                {"plan_ref": "artifact:consumer-sync-plan", "repository": "owner/repo"},
                ["sync"],
                preconditions=[{"check": "consumer_sync_plan_valid", "expected": True}],
                postconditions=[{"check": "consumer_sync_shadow_classified", "expected": True}],
                retry={"max_attempts": 1, "backoff_seconds": 0},
                timeout_seconds=60,
                side_effect_policy="read_only",
                rollback={"entrypoint": "workflow.noop", "inputs": {}},
            ),
            step(
                "hygiene",
                "code_workspace_hygiene.audit",
                {"workspace_root": "/workspace"},
                ["consumer-drift"],
                preconditions=[{"check": "remote_synced", "expected": True}],
                postconditions=[{"check": "workspace_root_clean", "expected": True}],
                retry={"max_attempts": 1, "backoff_seconds": 0},
                timeout_seconds=60,
                side_effect_policy="read_only",
                rollback={"entrypoint": "workflow.noop", "inputs": {}},
            ),
            step(
                "test-gate",
                "local_verify.named_test_gate",
                {"workspace_root": "/workspace", "gate_id": "focused-tests"},
                ["hygiene"],
                preconditions=[{"check": "workspace_root_clean", "expected": True}],
                postconditions=[{"check": "named_test_gate_passed", "expected": True}],
                retry={"max_attempts": 2, "backoff_seconds": 2},
                timeout_seconds=300,
                side_effect_policy="read_only",
                rollback={"entrypoint": "workflow.noop", "inputs": {}},
            ),
        ],
        "barriers": [
            {
                "id": "sync-before-consumer-drift",
                "after": "sync",
                "before": "consumer-drift",
                "condition_id": "remote_synced",
            },
            {
                "id": "consumer-drift-before-hygiene",
                "after": "consumer-drift",
                "before": "hygiene",
                "condition_id": "consumer_sync_shadow_classified",
            },
            {
                "id": "sync-before-hygiene",
                "after": "sync",
                "before": "hygiene",
                "condition_id": "remote_synced",
            },
            {
                "id": "hygiene-before-tests",
                "after": "hygiene",
                "before": "test-gate",
                "condition_id": "workspace_root_clean",
            },
        ],
        "expires_at": 1893456000,
        "kill_switch": "ORCH_REFERENCE_WORKFLOW_DISABLED=1",
    }


class SkillCompileError(ValueError):
    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(dict.fromkeys(str(reason) for reason in reasons))
        super().__init__("; ".join(self.reasons))


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _bounded_string_list(value: Any, field: str, *, limit: int) -> tuple[list[str], list[str]]:
    if not isinstance(value, list) or not value:
        return [], [f"{field} must be a non-empty list"]
    errors: list[str] = []
    result: list[str] = []
    if len(value) > limit:
        errors.append(f"{field} exceeds item limit")
    for index, item in enumerate(value[:limit]):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field}[{index}] must be a non-empty string")
            continue
        text = " ".join(item.strip().split())
        if len(text) > 512:
            errors.append(f"{field}[{index}] exceeds length limit")
            continue
        result.append(text)
    return result, errors


def _skill_directories(roots: Sequence[Path]) -> list[Path]:
    found: set[Path] = set()
    for raw in roots:
        root = Path(raw)
        if (root / "SKILL.md").is_file():
            found.add(root.resolve())
        elif root.is_dir():
            for skill_md in root.glob("*/SKILL.md"):
                found.add(skill_md.parent.resolve())
    return sorted(found)


def _skill_frontmatter_name(skill_md: Path) -> str | None:
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def _installed_skill_evidence(roots: Sequence[Path]) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    fingerprints: set[str] = set()
    for skill_dir in _skill_directories(roots):
        name = _skill_frontmatter_name(skill_dir / "SKILL.md")
        if name:
            names.add(name)
        manifest_path = skill_dir / ".orchestrator" / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("source_fingerprint"):
                fingerprints.add(str(manifest["source_fingerprint"]))
    return names, fingerprints


def _safe_resource(raw: Any, index: int) -> tuple[dict[str, Any] | None, list[str]]:
    path = f"resources[{index}]"
    if not isinstance(raw, dict) or set(raw) != {"kind", "source_path", "target", "content_hash"}:
        return None, [f"{path} must contain kind, source_path, target, and content_hash"]
    errors: list[str] = []
    kind = str(raw.get("kind") or "")
    if kind not in RESOURCE_SUFFIXES:
        errors.append(f"{path}.kind is unsupported")
    source = Path(str(raw.get("source_path") or "")).expanduser()
    target_text = str(raw.get("target") or "")
    target = Path(target_text)
    if SECRET_RE.search(target_text):
        errors.append(f"secret-bearing resource target: {index}")
    if target.is_absolute() or ".." in target.parts or not target.parts or target.parts[0] != kind:
        errors.append(f"{path}.target must stay under {kind}/")
    if source.suffix.lower() not in RESOURCE_SUFFIXES.get(kind, set()):
        errors.append(f"{path} has unsupported resource type")
    if not source.is_file():
        errors.append(f"{path}.source_path does not exist")
        return None, errors
    size = source.stat().st_size
    if size > MAX_SKILL_RESOURCE_BYTES:
        errors.append(f"{path} exceeds size limit")
    content = source.read_bytes()
    text = content.decode("utf-8", errors="ignore")
    if SECRET_RE.search(text):
        errors.append(f"secret-bearing resource: {target_text}")
    actual_hash = _sha256_bytes(content)
    if raw.get("content_hash") != actual_hash:
        errors.append(f"resource content hash mismatch: {target_text}")
    if errors:
        return None, errors
    return {
        "kind": kind,
        "source_path": source,
        "target": target.as_posix(),
        "content_hash": actual_hash,
        "source_resource_id": stable_hash(
            "skill-resource-source", {"filename": source.name, "content_hash": actual_hash}
        ),
    }, []


def _render_skill_markdown(source: dict[str, Any], resources: list[dict[str, Any]]) -> str:
    description = _render_skill_description(source)
    title = " ".join(part.capitalize() for part in str(source["name"]).split("-"))
    lines = [
        "---",
        f"name: {source['name']}",
        "description: " + json.dumps(description, ensure_ascii=True),
        "---",
        "",
        f"# {title}",
        "",
        "## Workflow",
        "",
        *[f"{index}. {instruction}" for index, instruction in enumerate(source["instructions"], 1)],
    ]
    if resources:
        lines.extend(["", "## Resources", ""])
        for resource in resources:
            lines.append(
                f"- Use [{Path(resource['target']).name}]({resource['target']}) for the reusable "
                f"{resource['kind'][:-1]} step."
            )
    lines.extend(["", "## Safety boundaries", ""])
    lines.extend(f"- {item}" for item in source["safety_boundaries"])
    lines.extend(["", "## Expected artifacts", ""])
    lines.extend(f"- `{item}`" for item in source["expected_artifacts"])
    lines.extend(
        [
            "",
            "## Validation",
            "",
            f"Run `{source['validation_command']}` from this skill directory.",
            "",
            "## Candidate lifecycle",
            "",
            "Keep this package shadow-only until activation evidence supports a separate canary decision.",
            f"Expiry: `{source['expires_at']}`.",
            f"Predecessor: `{source.get('predecessor') or 'none'}`.",
            f"Rollback: `{source['rollback']['action']}`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_skill_description(source: dict[str, Any]) -> str:
    description = str(source["description"]).strip()
    trigger_clause = "; ".join(source["triggers"])
    return (
        f"{description} Use when Codex needs to {trigger_clause}."
        if trigger_clause
        else description
    )


def _package_file_hashes(skill_dir: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or ".orchestrator" in path.relative_to(skill_dir).parts:
            continue
        rows[path.relative_to(skill_dir).as_posix()] = _sha256_file(path)
    return rows


def validate_skill_package(skill_dir: Path) -> dict[str, Any]:
    """Validate the generated subset of the local skill-creator contract."""
    skill_dir = Path(skill_dir)
    manifest_path = skill_dir / ".orchestrator" / "manifest.json"
    if not manifest_path.is_file():
        raise AssertionError("skill manifest does not exist")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SKILL_MANIFEST_SCHEMA or manifest.get("version") != SKILL_VERSION:
        raise AssertionError("invalid skill manifest")
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise AssertionError("SKILL.md does not exist")
    content = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        raise AssertionError("invalid SKILL.md frontmatter")
    keys = [line.split(":", 1)[0].strip() for line in match.group(1).splitlines() if ":" in line]
    if keys != ["name", "description"]:
        raise AssertionError("SKILL.md frontmatter must contain only name and description")
    if _skill_frontmatter_name(skill_md) != manifest.get("name"):
        raise AssertionError("skill name does not match manifest")
    for resource in manifest.get("resources") or []:
        target = skill_dir / resource["target"]
        if not target.is_file():
            raise AssertionError("skill resource does not exist")
        if _sha256_file(target) != resource["content_hash"]:
            raise AssertionError("skill resource hash mismatch")
        if f"({resource['target']})" not in content:
            raise AssertionError("skill resource is not referenced by SKILL.md")
    hashes = _package_file_hashes(skill_dir)
    if stable_hash("skill-package-content", hashes) != manifest.get("content_hash"):
        raise AssertionError("skill package content hash mismatch")
    if SECRET_RE.search(content):
        raise AssertionError("secret detected in generated skill")
    return manifest


def compile_skill_package(
    source: dict[str, Any],
    *,
    output_root: Path,
    installed_skill_roots: Sequence[Path] = (),
    existing_capability_fingerprints: Sequence[str] = (),
) -> dict[str, Any]:
    """Emit one versioned shadow skill package without installing or activating it."""
    errors: list[str] = []
    if not isinstance(source, dict):
        raise SkillCompileError(["skill source must be an object"])
    allowed_fields = {
        "schema",
        "version",
        "capability_id",
        "source_ir_ref",
        "source_fingerprint",
        "procedure_class",
        "reuse_scope",
        "name",
        "description",
        "triggers",
        "instructions",
        "resources",
        "safety_boundaries",
        "expected_artifacts",
        "validation_command",
        "expires_at",
        "predecessor",
        "rollback",
    }
    unknown = sorted(set(source) - allowed_fields)
    if unknown:
        errors.append(f"skill source has unsupported fields: {unknown}")
    if source.get("schema") != SKILL_SOURCE_SCHEMA or source.get("version") != SKILL_VERSION:
        errors.append("unsupported skill source schema")
    capability_id = str(source.get("capability_id") or "")
    if not capability_id.startswith("capability:"):
        errors.append("invalid capability_id")
    if not str(source.get("source_ir_ref") or "").startswith("capability-ir:"):
        errors.append("skill source requires a capability IR reference")
    source_fingerprint = str(source.get("source_fingerprint") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", source_fingerprint):
        errors.append("invalid source fingerprint")
    if source.get("procedure_class") == "deterministic_gate":
        errors.append("deterministic gate: route to acceptance_gate")
    if source.get("reuse_scope") == "repo_only":
        errors.append("repo-only procedure: route to playbook")
    if source.get("procedure_class") != "reusable_procedure":
        errors.append("skill source is not a reusable procedure")
    name = str(source.get("name") or "")
    if not SKILL_NAME_RE.fullmatch(name) or len(name) > 64 or "--" in name:
        errors.append("invalid skill name")
    description = str(source.get("description") or "").strip()
    if not description or len(description) > 700 or "<" in description or ">" in description:
        errors.append("invalid skill description")
    triggers, list_errors = _bounded_string_list(
        source.get("triggers"), "triggers", limit=MAX_SKILL_TRIGGERS
    )
    errors.extend(list_errors)
    instructions, list_errors = _bounded_string_list(
        source.get("instructions"), "instructions", limit=MAX_SKILL_INSTRUCTIONS
    )
    errors.extend(list_errors)
    safety, list_errors = _bounded_string_list(
        source.get("safety_boundaries"), "safety_boundaries", limit=12
    )
    errors.extend(list_errors)
    artifacts, list_errors = _bounded_string_list(
        source.get("expected_artifacts"), "expected_artifacts", limit=12
    )
    errors.extend(list_errors)
    validation_command = str(source.get("validation_command") or "")
    if not re.fullmatch(
        r"(?:bash|python3) (?:scripts|references)/[A-Za-z0-9_.-]+(?: --help)?", validation_command
    ):
        errors.append("validation command is not allowlisted")
    expires_at = source.get("expires_at")
    if not isinstance(expires_at, int) or expires_at <= 0:
        errors.append("expires_at must be a positive integer")
    rollback = source.get("rollback")
    if not isinstance(rollback, dict) or set(rollback) != {"action", "reason"}:
        errors.append("skill rollback must contain action and reason")
    elif rollback.get("action") != "retire_shadow_package":
        errors.append("unsupported skill rollback action")
    if SECRET_RE.search(
        canonical_json({key: value for key, value in source.items() if key != "resources"})
    ):
        errors.append("secret-bearing skill source")

    installed_names, installed_fingerprints = _installed_skill_evidence(installed_skill_roots)
    if name in installed_names:
        errors.append(f"duplicate installed skill: {name}")
    if source_fingerprint in set(existing_capability_fingerprints) | installed_fingerprints:
        errors.append("duplicate capability fingerprint")

    raw_resources = source.get("resources")
    if not isinstance(raw_resources, list) or not raw_resources:
        errors.append("skill requires at least one reused resource")
        raw_resources = []
    resources: list[dict[str, Any]] = []
    targets: set[str] = set()
    for index, raw in enumerate(raw_resources):
        resource, resource_errors = _safe_resource(raw, index)
        errors.extend(resource_errors)
        if resource:
            if resource["target"] in targets:
                errors.append(f"duplicate skill resource target: {resource['target']}")
            targets.add(resource["target"])
            resources.append(resource)
    normalized_description_source = {**source, "description": description, "triggers": triggers}
    if len(_render_skill_description(normalized_description_source)) > 1024:
        errors.append("rendered skill description exceeds length limit")
    command_parts = validation_command.split()
    if len(command_parts) >= 2 and command_parts[1] not in targets:
        errors.append("validation command does not reference a bundled resource")
    if errors:
        raise SkillCompileError(errors)

    normalized = {
        **source,
        "description": description,
        "triggers": triggers,
        "instructions": instructions,
        "safety_boundaries": safety,
        "expected_artifacts": artifacts,
    }
    skill_markdown = _render_skill_markdown(normalized, resources)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / name
    if destination.exists():
        raise SkillCompileError([f"refusing to overwrite skill package: {name}"])
    staging_root = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=output_root))
    staging = staging_root / name
    try:
        staging.mkdir()
        (staging / "SKILL.md").write_text(skill_markdown, encoding="utf-8")
        manifest_resources: list[dict[str, Any]] = []
        for resource in resources:
            target = staging / resource["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resource["source_path"], target)
            manifest_resources.append(
                {
                    key: resource[key]
                    for key in ("kind", "target", "content_hash", "source_resource_id")
                }
            )
        file_hashes = _package_file_hashes(staging)
        content_hash = stable_hash("skill-package-content", file_hashes)
        manifest = {
            "schema": SKILL_MANIFEST_SCHEMA,
            "version": SKILL_VERSION,
            "capability_id": capability_id,
            "source_ir_ref": source["source_ir_ref"],
            "source_fingerprint": source_fingerprint,
            "name": name,
            "content_hash": content_hash,
            "resources": manifest_resources,
            "expected_artifacts": artifacts,
            "validation_command": validation_command,
            "lifecycle": {
                "state": "shadow",
                "expires_at": expires_at,
                "predecessor": source.get("predecessor"),
                "rollback": rollback,
                "globally_installed": False,
            },
        }
        manifest_dir = staging / ".orchestrator"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        validate_skill_package(staging)
        os.replace(staging, destination)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return {"package_path": str(destination), "manifest": validate_skill_package(destination)}


def compile_skill_candidate(source: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Compile or return a bounded non-skill routing decision."""
    try:
        package = compile_skill_package(source, **kwargs)
    except SkillCompileError as exc:
        reasons = list(exc.reasons)
        target = None
        if any(reason.startswith("deterministic gate:") for reason in reasons):
            target = "acceptance_gate"
        elif any(reason.startswith("repo-only procedure:") for reason in reasons):
            target = "playbook"
        elif any(reason.startswith("duplicate") for reason in reasons):
            target = "existing_skill"
        core = {
            "schema": SKILL_DECISION_SCHEMA,
            "version": SKILL_VERSION,
            "status": "routed" if target else "rejected",
            "target": target,
            "executable": False,
            "rejection_reasons": reasons,
            "capability_id": (
                str(source.get("capability_id") or "") if isinstance(source, dict) else ""
            ),
        }
        return {**core, "decision_id": stable_hash("skill-compiler-decision", core)}
    return {
        "schema": SKILL_DECISION_SCHEMA,
        "version": SKILL_VERSION,
        "status": "compiled_shadow",
        "target": "skill",
        "executable": False,
        "rejection_reasons": [],
        **package,
    }


def _register_skill_shadow(manifest: dict[str, Any], ledger_path: Path) -> None:
    capability_id = manifest["capability_id"]
    existing = capabilities.load(ledger_path, create=False)
    if capability_id in existing:
        if (existing[capability_id].get("activation_evidence") or {}).get(
            "skill_content_hash"
        ) != manifest["content_hash"]:
            raise ValueError("capability already registered with a different skill package")
        return
    capabilities.register(
        capability_id,
        {
            "status": "shadow",
            "owner": "orchestrator",
            "matcher": {"kind": "skill_trigger", "name": manifest["name"]},
            "entrypoint": "capability_compiler.py:shadow_invoke_skill_package",
            "trigger_cadence": "naturally matching supervised shadow tasks",
            "flags_defaults": {"globally_installed": False, "shadow_only": True},
            "output_artifact": SKILL_MANIFEST_SCHEMA,
            "downstream_consumer": "feedback.py:record_skill_invocation",
            "learning_sink": "feedback completion events and influence edges",
            "activation_evidence": {"skill_content_hash": manifest["content_hash"]},
            "gate_reason": "generated skill remains shadow-only",
            "gate_evidence": "package is outside installed skill roots and invocation is observational",
            "evidence_threshold": "multiple accepted durable shadow uses justify a separate canary decision",
            "activation_deadline": manifest["lifecycle"]["expires_at"],
            "expiry": manifest["lifecycle"]["expires_at"],
            "next_transition": "retired",
            "kill_switch": "retire generated skill capability and discard its shadow package",
            "rollback": manifest["lifecycle"]["rollback"],
            "predecessor": manifest["lifecycle"].get("predecessor"),
        },
        path=ledger_path,
    )


def shadow_invoke_skill_package(
    skill_dir: Path,
    *,
    task_ref: str,
    influenced_run_ids: Sequence[str],
    artifact_refs: Sequence[dict[str, Any]],
    ledger_path: Path,
    accepted: bool = True,
    downstream_outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one observational skill use without changing baseline task behavior."""
    manifest = validate_skill_package(Path(skill_dir))
    _register_skill_shadow(manifest, ledger_path)
    capability_id = manifest["capability_id"]
    match_ref = stable_hash(
        "skill-shadow-match", {"content_hash": manifest["content_hash"], "task_ref": task_ref}
    )
    event_prefix = f"{manifest['content_hash']}:{match_ref}"
    capabilities.heartbeat(
        capability_id,
        "match",
        ref=match_ref,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:match",
    )
    capabilities.heartbeat(
        capability_id,
        "invocation",
        ref=match_ref,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:invocation",
    )
    package_artifact = {
        "artifact_id": f"skill-package:{manifest['name']}:{manifest['content_hash'][-12:]}",
        "kind": "skill-package",
        "content_hash": manifest["content_hash"],
        "ref_class": "shadow",
    }
    all_artifacts = [package_artifact, *list(artifact_refs)]
    invocation_digest = stable_hash(
        "skill-shadow-invocation",
        {
            "skill": manifest["name"],
            "version": manifest["content_hash"],
            "task_ref": task_ref,
            "runs": sorted(influenced_run_ids),
            "artifacts": all_artifacts,
            "accepted": accepted,
        },
    )
    invocation_id = "skill-invocation:" + invocation_digest.split(":", 1)[1][:24]
    invocation = feedback.record_skill_invocation(
        manifest["name"],
        manifest["content_hash"],
        phase="execution",
        artifacts=all_artifacts,
        influenced_run_ids=list(influenced_run_ids),
        result="succeeded" if accepted else "failed",
        accepted=accepted,
        invocation_id=invocation_id,
        acceptance_gate_id="generated-skill-shadow",
    )
    capabilities.heartbeat(
        capability_id,
        "output",
        ref=manifest["content_hash"],
        path=ledger_path,
        idempotency_key=f"{event_prefix}:output",
    )
    capabilities.heartbeat(
        capability_id,
        "consumer",
        ref=invocation["event_id"],
        path=ledger_path,
        idempotency_key=f"{event_prefix}:consumer",
    )
    outcome_ref = None
    if downstream_outcome is not None:
        for run_id in influenced_run_ids:
            feedback.record_outcome(str(run_id), **downstream_outcome)
        outcome_ref = stable_hash(
            "skill-shadow-outcome",
            {"runs": sorted(influenced_run_ids), "outcome": downstream_outcome},
        )
        if accepted:
            capabilities.heartbeat(
                capability_id,
                "success",
                ref=outcome_ref,
                path=ledger_path,
                idempotency_key=f"{event_prefix}:success",
            )
            capabilities.heartbeat(
                capability_id,
                "outcome",
                ref=outcome_ref,
                path=ledger_path,
                idempotency_key=f"{event_prefix}:outcome",
            )
    _probe_once(capability_id, "producer_probe", manifest["content_hash"], ledger_path)
    _probe_once(capability_id, "consumer_probe", invocation["event_id"], ledger_path)
    if accepted and outcome_ref:
        _probe_once(capability_id, "outcome_probe", outcome_ref, ledger_path)
    _probe_once(
        capability_id,
        "rollback_probe",
        stable_hash("skill-shadow-rollback", manifest["lifecycle"]["rollback"]),
        ledger_path,
    )
    return {
        "manifest": manifest,
        "invocation": invocation,
        "accepted": bool(accepted),
        "outcome_ref": outcome_ref,
        "baseline_changed": False,
    }


def reference_skill_source() -> dict[str, Any]:
    resource = (
        Path.home()
        / ".codex"
        / "skills"
        / "code-workspace-hygiene"
        / "scripts"
        / "audit_code_root.sh"
    )
    return {
        "schema": SKILL_SOURCE_SCHEMA,
        "version": SKILL_VERSION,
        "capability_id": "capability:audit-handoff-evidence",
        "source_ir_ref": "capability-ir:audit-handoff-evidence",
        "source_fingerprint": stable_hash(
            "skill-source-pattern", {"procedure": "audit-handoff-evidence", "version": 1}
        ),
        "procedure_class": "reusable_procedure",
        "reuse_scope": "shared",
        "name": "audit-handoff-evidence",
        "description": "Prepare bounded workspace-audit evidence and a concise completion handoff.",
        "triggers": [
            "audit workspace hygiene after completing delegated work",
            "prepare evidence for an audit or workflow handoff",
        ],
        "instructions": [
            "Run the bundled workspace audit script in report-only mode for the task workspace.",
            "Classify reported entries without deleting, moving, or archiving anything.",
            "Summarize the audit result and attach it to the completed-work handoff.",
            "Record only bounded artifact IDs and content hashes in completion lineage.",
        ],
        "resources": [
            {
                "kind": "scripts",
                "source_path": str(resource),
                "target": "scripts/audit_code_root.sh",
                "content_hash": _sha256_file(resource),
            }
        ],
        "safety_boundaries": [
            "Keep the bundled audit script in report-only mode.",
            "Do not delete, archive, reset, or modify workspace contents.",
            "Do not include raw prompts, credentials, user documents, or command output in lineage.",
        ],
        "expected_artifacts": ["workspace-audit-report", "audit-handoff-summary"],
        "validation_command": "bash scripts/audit_code_root.sh --help",
        "expires_at": 1893456000,
        "predecessor": "capability:completion-event-lineage",
        "rollback": {
            "action": "retire_shadow_package",
            "reason": "shadow evidence expires, regresses, or duplicates an installed skill",
        },
    }


class RoleCompileError(ValueError):
    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(dict.fromkeys(str(reason) for reason in reasons))
        super().__init__("; ".join(self.reasons))


def _role_contract(candidate: CapabilityIR) -> dict[str, Any]:
    contract = candidate.graph.get("role_contract")
    if not isinstance(contract, dict):
        raise RoleCompileError(["candidate lacks a typed role contract"])
    return contract


def _validate_role_schema(value: Any, *, label: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not isinstance(value, dict) or not value:
        return {}, [f"generated role lacks {label}"]
    errors: list[str] = []
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw in sorted(value.items()):
        if not isinstance(name, str) or not ROLE_FIELD_RE.fullmatch(name):
            errors.append(f"invalid {label} field: {name}")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{label}.{name} must be an object")
            continue
        allowed = {"type", "required", "enum", "max_length", "max_items"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            errors.append(f"{label}.{name} has unsupported fields: {unknown}")
        type_name = raw.get("type")
        if type_name not in ROLE_VALUE_TYPES:
            errors.append(f"{label}.{name} has unsupported type")
        if raw.get("required") is not True:
            errors.append(f"{label}.{name} must be required")
        spec: dict[str, Any] = {"type": type_name, "required": True}
        if "enum" in raw:
            enum = raw.get("enum")
            if (
                type_name != "string"
                or not isinstance(enum, list)
                or not enum
                or len(enum) > 12
                or not all(isinstance(item, str) and item.strip() for item in enum)
            ):
                errors.append(f"{label}.{name} has invalid enum")
            else:
                spec["enum"] = list(enum)
        if "max_length" in raw:
            bound = raw.get("max_length")
            if type_name != "string" or not isinstance(bound, int) or not 1 <= bound <= 4096:
                errors.append(f"{label}.{name} has invalid max_length")
            else:
                spec["max_length"] = bound
        if "max_items" in raw:
            bound = raw.get("max_items")
            if type_name != "string_list" or not isinstance(bound, int) or not 1 <= bound <= 24:
                errors.append(f"{label}.{name} has invalid max_items")
            else:
                spec["max_items"] = bound
        normalized[name] = spec
    return normalized, errors


def _forbidden_role_identity(value: Any, path: str = "role_contract") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_norm = str(key).lower().replace("_id", "")
            if key_norm in ROLE_IDENTITY_KEYS:
                errors.append(f"generated role hard-codes execution identity at {path}.{key}")
            errors.extend(_forbidden_role_identity(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_forbidden_role_identity(item, f"{path}[{index}]"))
    return errors


def _validate_generated_role_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Assert the exact generated-role contract, including the break-test message."""
    if not isinstance(manifest, dict):
        raise AssertionError("invalid generated role manifest")
    if "output_schema" not in manifest:
        raise AssertionError("generated role lacks output schema")
    required = {
        "schema",
        "version",
        "manifest_id",
        "capability_id",
        "source_ir_ref",
        "source_fingerprint",
        "name",
        "description",
        "authority",
        "route_as",
        "input_schema",
        "output_schema",
        "selector",
        "capacity_policy",
        "prompt_protocol",
        "prompt_hash",
        "lifecycle",
        "shadow_only",
        "profile_agnostic",
        "generated_at",
    }
    if set(manifest) != required:
        raise AssertionError("generated role manifest has unsupported or missing fields")
    if manifest["schema"] != ROLE_MANIFEST_SCHEMA or manifest["version"] != ROLE_VERSION:
        raise AssertionError("unsupported generated role manifest")
    if manifest["shadow_only"] is not True or manifest["profile_agnostic"] is not True:
        raise AssertionError("generated role is not shadow-only and profile-agnostic")
    input_schema, input_errors = _validate_role_schema(
        manifest["input_schema"], label="input schema"
    )
    output_schema, output_errors = _validate_role_schema(
        manifest["output_schema"], label="output schema"
    )
    if (
        input_errors
        or output_errors
        or input_schema != manifest["input_schema"]
        or output_schema != manifest["output_schema"]
    ):
        raise AssertionError("generated role has invalid strict schemas")
    expected_hash = stable_hash("generated-role-prompt-protocol", manifest["prompt_protocol"])
    if manifest["prompt_hash"] != expected_hash:
        raise AssertionError("generated role prompt hash mismatch")
    core = {key: value for key, value in manifest.items() if key != "manifest_id"}
    if manifest["manifest_id"] != stable_hash("generated-role-manifest", core):
        raise AssertionError("generated role manifest hash mismatch")
    roles.role_from_generated_manifest(manifest)
    return manifest


def compile_role_capability(
    raw_candidate: CapabilityIR | dict[str, Any], *, now: int | None = None
) -> dict[str, Any]:
    """Compile eligible judgment IR into a typed, inert shadow-role manifest."""
    candidate = (
        raw_candidate
        if isinstance(raw_candidate, CapabilityIR)
        else CapabilityIR.from_dict(raw_candidate)
    )
    candidate.validate()
    current = int(time.time()) if now is None else int(now)
    errors: list[str] = []
    if candidate.kind_proposal != "role":
        errors.append("candidate is not a role judgment pattern")
    contract = _role_contract(candidate)
    allowed_contract_fields = {
        "schema",
        "version",
        "name",
        "description",
        "authority",
        "route_as",
        "input_schema",
        "output_schema",
        "selector",
        "capacity_policy",
        "prompt_protocol",
        "expires_at",
        "kill_switch",
        "rollback",
    }
    unknown = sorted(set(contract) - allowed_contract_fields)
    if unknown:
        errors.append(f"role contract has unsupported fields: {unknown}")
    if contract.get("schema") != ROLE_SOURCE_SCHEMA or contract.get("version") != ROLE_VERSION:
        errors.append("unsupported role source schema")
    if (
        candidate.graph.get("decision_mode") != "judgment"
        or candidate.graph.get("requires_judgment") is not True
    ):
        errors.append("deterministic pattern: route to workflow")
    durable_subjects = {
        item.subject_id
        for item in candidate.source_occurrences
        if item.verification_ref and item.outcome_ref and item.durability_ref
    }
    if len(durable_subjects) < 3 or durable_subjects != set(candidate.independent_subjects):
        errors.append("role requires three independent durable judgment subjects")
    if float(candidate.telemetry.get("effective_subject_count") or 0) < 3:
        errors.append("role requires three effective independent subjects")
    if candidate.counterexamples:
        negative_ratio = float(candidate.telemetry.get("negative_ratio") or 0)
        if negative_ratio > 0.2:
            errors.append("role judgment evidence has excessive counterexamples")
    name = str(contract.get("name") or "")
    if not ROLE_NAME_RE.fullmatch(name):
        errors.append("invalid generated role name")
    if name in roles.ROLE_REGISTRY or name in roles.GENERATED_ROLE_REGISTRY:
        errors.append(f"duplicate existing role: {name}")
    description = str(contract.get("description") or "").strip()
    if not 20 <= len(description) <= 500:
        errors.append("generated role description is unbounded or empty")
    authority = str(contract.get("authority") or "").strip()
    if not 20 <= len(authority) <= 500 or ROLE_FORBIDDEN_AUTHORITY.search(authority):
        errors.append("destructive or unbounded role authority")
    if ROLE_FORBIDDEN_AUTHORITY.search(
        canonical_json(
            {
                "description": contract.get("description"),
                "input_schema": contract.get("input_schema"),
                "output_schema": contract.get("output_schema"),
                "prompt_protocol": contract.get("prompt_protocol"),
            }
        )
    ):
        errors.append("destructive generated role contract")
    route_as = str(contract.get("route_as") or "")
    if route_as not in roles.router.ROUTE_TABLE:
        errors.append("unsupported role routing prior")
    input_schema, input_errors = _validate_role_schema(
        contract.get("input_schema"), label="input schema"
    )
    output_schema, output_errors = _validate_role_schema(
        contract.get("output_schema"), label="output schema"
    )
    errors.extend(input_errors + output_errors)
    selector = contract.get("selector")
    if not isinstance(selector, dict) or set(selector) != {
        "field",
        "operator",
        "value",
        "max_matches_per_cycle",
    }:
        errors.append("generated role selector must be exact and bounded")
    else:
        if selector.get("field") not in input_schema:
            errors.append("generated role selector field is outside input schema")
        if selector.get("operator") not in {"equals", "in"}:
            errors.append("generated role selector has unsupported operator")
        value = selector.get("value")
        if selector.get("operator") == "equals" and not isinstance(value, str):
            errors.append("generated role selector equals value must be a string")
        if selector.get("operator") == "in" and (
            not isinstance(value, list)
            or not 1 <= len(value) <= 8
            or not all(isinstance(item, str) for item in value)
        ):
            errors.append("generated role selector values are unbounded")
        if selector.get("max_matches_per_cycle") != 1:
            errors.append("generated role selector must allow one match per cycle")
    capacity = contract.get("capacity_policy")
    expected_capacity = {
        "selection": "router_capacity_and_learned_weights",
        "reserve_policy": "preserve",
        "max_invocations_per_cycle": 1,
    }
    if capacity != expected_capacity:
        errors.append("generated role capacity policy is not bounded")
    protocol = contract.get("prompt_protocol")
    if not isinstance(protocol, dict) or set(protocol) != {
        "version",
        "purpose",
        "instructions",
        "max_context_chars",
        "output_format",
    }:
        errors.append("generated role prompt protocol is incomplete")
    else:
        if protocol.get("version") != 1 or protocol.get("output_format") != "strict_json":
            errors.append("generated role prompt protocol is unsupported")
        if (
            not isinstance(protocol.get("purpose"), str)
            or not 20 <= len(protocol["purpose"]) <= 500
        ):
            errors.append("generated role prompt purpose is unbounded")
        instructions = protocol.get("instructions")
        if (
            not isinstance(instructions, list)
            or not 1 <= len(instructions) <= 12
            or not all(isinstance(item, str) and 5 <= len(item) <= 500 for item in instructions)
        ):
            errors.append("generated role prompt instructions are unbounded")
        if (
            not isinstance(protocol.get("max_context_chars"), int)
            or not 256 <= protocol["max_context_chars"] <= 8192
        ):
            errors.append("generated role prompt context bound is invalid")
    expires_at = contract.get("expires_at")
    if not isinstance(expires_at, int) or not current < expires_at <= current + ROLE_TTL_SECONDS:
        errors.append("generated role requires a bounded future expiry")
    kill_switch = contract.get("kill_switch")
    expected_env = "ORCH_GENERATED_ROLE_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_DISABLED"
    if kill_switch != {"env": expected_env, "disabled_value": "1"}:
        errors.append("generated role kill switch is invalid")
    rollback = contract.get("rollback")
    if (
        not isinstance(rollback, dict)
        or set(rollback) != {"action", "predecessor", "reason"}
        or rollback.get("action") != "retire_generated_role_and_restore_predecessor"
        or not candidate.predecessor
        or rollback.get("predecessor") != candidate.predecessor
    ):
        errors.append("generated role lacks predecessor rollback")
    errors.extend(_forbidden_role_identity(contract))
    if SECRET_RE.search(canonical_json(contract)):
        errors.append("secret-bearing role candidate")
    if errors:
        raise RoleCompileError(errors)
    normalized_protocol = {
        "version": 1,
        "purpose": protocol["purpose"].strip(),
        "instructions": [item.strip() for item in protocol["instructions"]],
        "max_context_chars": protocol["max_context_chars"],
        "output_format": "strict_json",
    }
    lifecycle = {
        "state": "shadow",
        "expires_at": expires_at,
        "kill_switch": kill_switch,
        "predecessor": candidate.predecessor,
        "rollback": rollback,
    }
    core = {
        "schema": ROLE_MANIFEST_SCHEMA,
        "version": ROLE_VERSION,
        "capability_id": candidate.capability_id,
        "source_ir_ref": candidate.capability_id,
        "source_fingerprint": candidate.fingerprint,
        "name": name,
        "description": description,
        "authority": authority,
        "route_as": route_as,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "selector": dict(selector),
        "capacity_policy": dict(capacity),
        "prompt_protocol": normalized_protocol,
        "prompt_hash": stable_hash("generated-role-prompt-protocol", normalized_protocol),
        "lifecycle": lifecycle,
        "shadow_only": True,
        "profile_agnostic": True,
        "generated_at": current,
    }
    manifest = {**core, "manifest_id": stable_hash("generated-role-manifest", core)}
    _validate_generated_role_manifest(manifest)
    return {"manifest": manifest, "role": roles.role_from_generated_manifest(manifest)}


def compile_role_candidate(
    raw_candidate: CapabilityIR | dict[str, Any], *, now: int | None = None
) -> dict[str, Any]:
    """Compile a role or return an inert, typed routing/rejection decision."""
    try:
        compiled = compile_role_capability(raw_candidate, now=now)
    except RoleCompileError as exc:
        reasons = list(exc.reasons)
        deterministic = "deterministic pattern: route to workflow" in reasons
        core = {
            "schema": ROLE_DECISION_SCHEMA,
            "version": ROLE_VERSION,
            "status": "routed" if deterministic else "rejected",
            "target": "workflow" if deterministic else None,
            "executable": False,
            "rejection_reasons": reasons,
        }
        return {**core, "decision_id": stable_hash("role-compiler-decision", core)}
    manifest = compiled["manifest"]
    return {
        "schema": ROLE_DECISION_SCHEMA,
        "version": ROLE_VERSION,
        "status": "compiled_shadow",
        "target": "role",
        "executable": False,
        "manifest": manifest,
        "role": compiled["role"],
    }


def reference_role_candidate(*, now: int | None = None) -> CapabilityIR:
    """Return an offline judgment fixture for compiler selftests and examples."""
    current = int(time.time()) if now is None else int(now)
    predecessor = "role-adjudicator"
    name = "evidence-gap-prioritizer-reference"
    contract = {
        "schema": ROLE_SOURCE_SCHEMA,
        "version": ROLE_VERSION,
        "name": name,
        "description": "Compare bounded evidence gaps and recommend one investigation priority.",
        "authority": "Advisory only: compare supplied evidence and recommend one bounded investigation priority.",
        "route_as": "review",
        "input_schema": {
            "task_type": {"type": "string", "required": True, "enum": ["review"], "max_length": 32},
            "evidence_refs": {"type": "string_list", "required": True, "max_items": 8},
        },
        "output_schema": {
            "decision": {
                "type": "string",
                "required": True,
                "enum": ["inspect", "defer"],
                "max_length": 32,
            },
            "rationale": {"type": "string", "required": True, "max_length": 800},
        },
        "selector": {
            "field": "task_type",
            "operator": "equals",
            "value": "review",
            "max_matches_per_cycle": 1,
        },
        "capacity_policy": {
            "selection": "router_capacity_and_learned_weights",
            "reserve_policy": "preserve",
            "max_invocations_per_cycle": 1,
        },
        "prompt_protocol": {
            "version": 1,
            "purpose": "Prioritize one supplied evidence gap using bounded judgment.",
            "instructions": [
                "Use only supplied evidence references and return one advisory recommendation."
            ],
            "max_context_chars": 2048,
            "output_format": "strict_json",
        },
        "expires_at": current + 30 * 86400,
        "kill_switch": {
            "env": "ORCH_GENERATED_ROLE_EVIDENCE_GAP_PRIORITIZER_REFERENCE_DISABLED",
            "disabled_value": "1",
        },
        "rollback": {
            "action": "retire_generated_role_and_restore_predecessor",
            "predecessor": predecessor,
            "reason": "shadow evidence expired or regressed",
        },
    }
    occurrences = tuple(
        SourceOccurrence(
            event_id=f"reference-role-event-{index}",
            event_refs=tuple(f"reference-role-event-{index}-{phase}" for phase in range(7)),
            occurred_at=current - index,
            subject_id=f"reference-role-subject:{index}",
            observation_id=f"reference-role-observation:{index}",
            family_id=None,
            canonical_target=f"owner/repo#{index}",
            repository=f"owner/repo-{index}",
            task_type="review",
            normalized_spec_hash=stable_hash("reference-role-spec", index),
            base_sha=f"base-{index}",
            profile_id=f"profile-{index}",
            arm_id=f"arm-{index}",
            attempt_id=f"attempt-{index}",
            verification_ref=stable_hash("reference-role-verification", index),
            outcome_ref=stable_hash("reference-role-outcome", index),
            durability_ref=stable_hash("reference-role-durability", index),
        )
        for index in range(1, 4)
    )
    return CapabilityIR(
        capability_id="capability:evidence-gap-prioritizer-reference",
        fingerprint=stable_hash("reference-role-candidate", name),
        semantic_fingerprint=stable_hash("reference-role-semantic", name),
        output_contract_fingerprint=stable_hash("reference-role-output", contract["output_schema"]),
        kind_proposal="role",
        owner_proposal="orchestrator",
        source_occurrences=occurrences,
        counterexamples=(),
        independent_subjects=tuple(item.subject_id for item in occurrences),
        independent_repositories=tuple(item.repository for item in occurrences),
        selector=contract["selector"],
        graph={
            "phase_order": [
                "trigger",
                "decision",
                "execution",
                "artifact",
                "verification",
                "outcome",
                "durability",
            ],
            "decision_mode": "judgment",
            "requires_judgment": True,
            "role_contract": contract,
        },
        artifact_refs=(),
        gates={"durable_result_required": True},
        telemetry={
            "distinct_subject_count": 3,
            "effective_subject_count": 3.0,
            "negative_ratio": 0.0,
        },
        lifecycle=Lifecycle(expires_at=current + 30 * 86400),
        predecessor=predecessor,
    )


class PlaybookCompileError(ValueError):
    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(dict.fromkeys(str(reason) for reason in reasons))
        super().__init__("; ".join(self.reasons))


def _playbook_contract(candidate: CapabilityIR) -> dict[str, Any]:
    contract = candidate.graph.get("playbook_contract")
    if not isinstance(contract, dict):
        raise PlaybookCompileError(["candidate lacks a typed playbook contract"])
    return contract


def validate_playbook_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "version",
        "manifest_id",
        "rule_id",
        "capability_id",
        "source_ir_ref",
        "source_fingerprint",
        "repo",
        "section",
        "text",
        "content_hash",
        "selector",
        "current_refs",
        "negative_examples",
        "evidence_refs",
        "risk_level",
        "optional_workflows_bundle",
        "lifecycle",
        "generated_at",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise AssertionError("invalid playbook manifest shape")
    if manifest["schema"] != PLAYBOOK_MANIFEST_SCHEMA or manifest["version"] != PLAYBOOK_VERSION:
        raise AssertionError("unsupported playbook manifest")
    if manifest["section"] not in PLAYBOOK_SECTIONS:
        raise AssertionError("invalid playbook manifest section")
    if manifest["risk_level"] != "low_reversible":
        raise AssertionError("playbook canary is not low risk")
    lifecycle = manifest["lifecycle"]
    if lifecycle.get("state") != "canary" or not lifecycle.get("predecessor"):
        raise AssertionError("invalid playbook canary lifecycle")
    expected_hash = stable_hash(
        "repo-playbook-rule",
        {
            "repo": manifest["repo"],
            "section": manifest["section"],
            "text": manifest["text"],
            "selector": manifest["selector"],
            "current_refs": manifest["current_refs"],
        },
    )
    if manifest["content_hash"] != expected_hash:
        raise AssertionError("playbook content hash mismatch")
    core = {key: value for key, value in manifest.items() if key != "manifest_id"}
    if manifest["manifest_id"] != stable_hash("repo-playbook-manifest", core):
        raise AssertionError("playbook manifest hash mismatch")
    return manifest


def compile_playbook_capability(
    raw_candidate: CapabilityIR | dict[str, Any],
    *,
    repo_root: Path,
    registry_path: Path = repo_knowledge.REG,
    now: int | None = None,
) -> dict[str, Any]:
    """Compile durable repo-specific IR into a reversible low-risk canary rule."""
    candidate = (
        raw_candidate
        if isinstance(raw_candidate, CapabilityIR)
        else CapabilityIR.from_dict(raw_candidate)
    )
    candidate.validate()
    contract = _playbook_contract(candidate)
    current = int(time.time()) if now is None else int(now)
    errors: list[str] = []
    allowed = {
        "schema",
        "version",
        "repo",
        "section",
        "text",
        "content_hash",
        "selector",
        "current_refs",
        "negative_examples",
        "risk_level",
        "expires_at",
        "rollback",
        "optional_workflows_bundle",
    }
    unknown = sorted(set(contract) - allowed)
    if unknown:
        errors.append(f"playbook contract has unsupported fields: {unknown}")
    if (
        contract.get("schema") != PLAYBOOK_SOURCE_SCHEMA
        or contract.get("version") != PLAYBOOK_VERSION
    ):
        errors.append("unsupported playbook source schema")
    if candidate.kind_proposal != "playbook" or candidate.owner_proposal != "repo":
        errors.append("candidate is not a repo-owned playbook pattern")
    repo = str(contract.get("repo") or "").strip()
    source_repos = {item.repository for item in candidate.source_occurrences}
    if not repo or source_repos != {repo} or set(candidate.independent_repositories) != {repo}:
        errors.append("cross-repo-generic playbook candidate")
    durable_subjects = {
        item.subject_id
        for item in candidate.source_occurrences
        if item.verification_ref and item.outcome_ref and item.durability_ref
    }
    if len(durable_subjects) < 3 or durable_subjects != set(candidate.independent_subjects):
        errors.append("insufficient durable repo evidence")
    if float(candidate.telemetry.get("effective_subject_count") or 0) < 3:
        errors.append("insufficient durable repo evidence")
    section = str(contract.get("section") or "")
    if section not in PLAYBOOK_SECTIONS:
        errors.append("invalid playbook section")
    text_value = " ".join(str(contract.get("text") or "").split())
    if not 20 <= len(text_value) <= 600:
        errors.append("playbook rule text is empty or unbounded")
    if PLAYBOOK_PROMPT_INJECTION.search(text_value) or SECRET_RE.search(text_value):
        errors.append("prompt-injection-like playbook candidate")
    selector = contract.get("selector")
    if not isinstance(selector, dict) or set(selector) != {"repo", "task_types", "lanes"}:
        errors.append("playbook selector must be exact")
        selector = {}
    else:
        task_types = selector.get("task_types")
        lanes = selector.get("lanes")
        if selector.get("repo") != repo:
            errors.append("playbook selector repo mismatch")
        if (
            not isinstance(task_types, list)
            or not 1 <= len(task_types) <= 6
            or any(item not in roles.router.ROUTE_TABLE for item in task_types)
        ):
            errors.append("playbook selector task types are unsupported")
        if (
            not isinstance(lanes, list)
            or not 1 <= len(lanes) <= 2
            or any(item not in {"opener", "closer"} for item in lanes)
        ):
            errors.append("playbook selector lanes are unsupported")
    refs_result = repo_knowledge.validate_current_refs(
        Path(repo_root), contract.get("current_refs")
    )
    if not refs_result["valid"]:
        errors.extend(refs_result["errors"] or ["stale current path"])
    current_refs = refs_result["refs"]
    if current_refs and not any(
        ref["path"] in text_value or (ref.get("symbol") and ref["symbol"] in text_value)
        for ref in current_refs
    ):
        errors.append("cross-repo-generic playbook candidate")
    negatives = contract.get("negative_examples")
    if not isinstance(negatives, list) or not 1 <= len(negatives) <= 8:
        errors.append("playbook requires bounded negative examples")
        negatives = []
    else:
        for index, item in enumerate(negatives):
            if not isinstance(item, dict) or set(item) != {"text", "evidence_ref"}:
                errors.append(f"negative_examples[{index}] is invalid")
            elif (
                not 10 <= len(str(item.get("text") or "")) <= 400
                or not str(item.get("evidence_ref") or "").strip()
            ):
                errors.append(f"negative_examples[{index}] is invalid")
    risk_level = str(contract.get("risk_level") or "")
    if risk_level != "low_reversible":
        errors.append("policy choice requires owner question")
    expires_at = contract.get("expires_at")
    if (
        not isinstance(expires_at, int)
        or not current < expires_at <= current + PLAYBOOK_TTL_SECONDS
    ):
        errors.append("playbook requires a bounded future expiry")
    rollback = contract.get("rollback")
    if (
        not isinstance(rollback, dict)
        or set(rollback) != {"action", "predecessor", "reason"}
        or rollback.get("action") != "remove_managed_rule_and_restore_predecessor"
        or not candidate.predecessor
        or rollback.get("predecessor") != candidate.predecessor
    ):
        errors.append("playbook lacks predecessor rollback")
    expected_hash = stable_hash(
        "repo-playbook-rule",
        {
            "repo": repo,
            "section": section,
            "text": text_value,
            "selector": selector,
            "current_refs": current_refs,
        },
    )
    if contract.get("content_hash") != expected_hash:
        errors.append("playbook content hash mismatch")
    if text_value and repo:
        duplicate = repo_knowledge.managed_rule_duplicate(
            repo, text_value, expected_hash, path=registry_path
        )
        if duplicate:
            errors.append(duplicate["reason"])
    if errors:
        raise PlaybookCompileError(errors)
    evidence_refs = sorted(
        {
            ref
            for item in candidate.source_occurrences
            for ref in (item.verification_ref, item.outcome_ref, item.durability_ref)
            if ref
        }
    )
    lifecycle = {
        "state": "canary",
        "expires_at": expires_at,
        "predecessor": candidate.predecessor,
        "rollback": rollback,
    }
    rule_id = "playbook:" + expected_hash.split(":", 1)[1][:24]
    core = {
        "schema": PLAYBOOK_MANIFEST_SCHEMA,
        "version": PLAYBOOK_VERSION,
        "rule_id": rule_id,
        "capability_id": candidate.capability_id,
        "source_ir_ref": candidate.capability_id,
        "source_fingerprint": candidate.fingerprint,
        "repo": repo,
        "section": section,
        "text": text_value,
        "content_hash": expected_hash,
        "selector": selector,
        "current_refs": current_refs,
        "negative_examples": negatives,
        "evidence_refs": evidence_refs,
        "risk_level": risk_level,
        "optional_workflows_bundle": bool(contract.get("optional_workflows_bundle")),
        "lifecycle": lifecycle,
        "generated_at": current,
    }
    manifest = {**core, "manifest_id": stable_hash("repo-playbook-manifest", core)}
    validate_playbook_manifest(manifest)
    return manifest


def compile_playbook_candidate(
    raw_candidate: CapabilityIR | dict[str, Any],
    *,
    repo_root: Path,
    registry_path: Path = repo_knowledge.REG,
    now: int | None = None,
    record_owner_question: bool = False,
) -> dict[str, Any]:
    """Compile, auto-expire weak evidence, or emit a non-blocking policy question."""
    try:
        manifest = compile_playbook_capability(
            raw_candidate, repo_root=repo_root, registry_path=registry_path, now=now
        )
    except PlaybookCompileError as exc:
        reasons = list(exc.reasons)
        needs_owner = "policy choice requires owner question" in reasons
        weak = "insufficient durable repo evidence" in reasons
        status = "owner_question" if needs_owner else "expired" if weak else "rejected"
        core: dict[str, Any] = {
            "schema": PLAYBOOK_DECISION_SCHEMA,
            "version": PLAYBOOK_VERSION,
            "status": status,
            "target": "owner_question" if needs_owner else None,
            "executable": False,
            "rejection_reasons": reasons,
        }
        if needs_owner:
            try:
                candidate = (
                    raw_candidate
                    if isinstance(raw_candidate, CapabilityIR)
                    else CapabilityIR.from_dict(raw_candidate)
                )
                repo = str(_playbook_contract(candidate).get("repo") or "")
            except Exception:
                repo = ""
            question = {
                "question": "Should this higher-risk repo policy be added to the managed playbook?",
                "default_action": "leave the candidate unexported and let it expire",
                "repo": repo,
                "options": ["keep unexported", "approve a supervised canary"],
                "expires_days": 7.0,
            }
            core["owner_question"] = question
            if record_owner_question:
                core["owner_question_result"] = feedback.record_owner_question(**question)
        return {**core, "decision_id": stable_hash("repo-playbook-decision", core)}
    return {
        "schema": PLAYBOOK_DECISION_SCHEMA,
        "version": PLAYBOOK_VERSION,
        "status": "compiled_canary",
        "target": "playbook",
        "executable": False,
        "manifest": manifest,
    }


def _register_playbook_capability(manifest: dict[str, Any], ledger_path: Path) -> None:
    capability_id = manifest["capability_id"]
    existing = capabilities.load(ledger_path, create=False) if ledger_path.exists() else {}
    if capability_id in existing:
        old_hash = (existing[capability_id].get("activation_evidence") or {}).get(
            "playbook_manifest_hash"
        )
        if old_hash != manifest["manifest_id"]:
            raise ValueError("capability already registered with a different playbook rule")
        return
    capabilities.register(
        capability_id,
        {
            "status": "canary",
            "owner": manifest["repo"],
            "matcher": manifest["selector"],
            "entrypoint": "capability_compiler.py:record_playbook_invocation",
            "trigger_cadence": "matching dispatcher prompt context",
            "flags_defaults": {"managed_block_only": True, "low_risk_canary": True},
            "output_artifact": "hashed managed AGENTS.md playbook rule",
            "downstream_consumer": "repo_knowledge.py:append_context and dispatcher",
            "learning_sink": "feedback completion events and capability influence edges",
            "activation_evidence": {"playbook_manifest_hash": manifest["manifest_id"]},
            "gate_reason": "low-risk playbook rule is in a reversible canary",
            "gate_evidence": "current path/symbol validation and managed-block export passed",
            "evidence_threshold": "linked accepted uses remain durable without negative counterexamples",
            "activation_deadline": manifest["lifecycle"]["expires_at"],
            "expiry": manifest["lifecycle"]["expires_at"],
            "next_transition": "retired",
            "kill_switch": "remove exactly this managed rule",
            "rollback": manifest["lifecycle"]["rollback"],
            "predecessor": manifest["lifecycle"]["predecessor"],
        },
        ledger_path,
    )


def export_playbook_canary(
    manifest: dict[str, Any],
    *,
    repo_root: Path,
    registry_path: Path,
    ledger_path: Path,
    workflows_bundle_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Export through the managed block and optional portable bundle boundary."""
    validate_playbook_manifest(manifest)
    refs = repo_knowledge.validate_current_refs(repo_root, manifest["current_refs"])
    if not refs["valid"]:
        raise PlaybookCompileError(refs["errors"])
    registry = repo_knowledge.install_managed_rule(manifest, path=registry_path, apply=apply)
    agents = repo_knowledge.update_agents_md(
        repo_root, repo=manifest["repo"], path=registry_path, apply=apply
    )
    bundle = None
    if workflows_bundle_path is not None and manifest["optional_workflows_bundle"]:
        bundle = repo_knowledge.update_capability_bundle(
            workflows_bundle_path, manifest, apply=apply
        )
    if apply:
        _register_playbook_capability(manifest, ledger_path)
    return {
        "manifest": manifest,
        "registry": registry,
        "agents_md": agents,
        "bundle": bundle,
        "block_status": repo_knowledge.validate_agents_md_export(
            repo_root, repo=manifest["repo"], path=registry_path
        ),
    }


def _playbook_selector_matches(
    manifest: dict[str, Any], *, repo: str, task_type: str, lane: str
) -> bool:
    selector = manifest["selector"]
    return (
        repo == selector["repo"]
        and task_type in selector["task_types"]
        and lane in selector["lanes"]
    )


def record_playbook_invocation(
    manifest: dict[str, Any],
    *,
    target_run_id: str,
    repo: str,
    task_type: str,
    lane: str,
    accepted: bool,
    ledger_path: Path,
    now: int | None = None,
) -> dict[str, Any]:
    """Record match, injection, acceptance, influence, and joined outcome."""
    validate_playbook_manifest(manifest)
    _register_playbook_capability(manifest, ledger_path)
    current = int(time.time()) if now is None else int(now)
    matched = _playbook_selector_matches(manifest, repo=repo, task_type=task_type, lane=lane)
    if not matched or current >= int(manifest["lifecycle"]["expires_at"]):
        return {"matched": matched, "injected": False, "accepted": False, "events": []}
    invocation_id = (
        "playbook-invocation:"
        + stable_hash(
            "playbook-invocation",
            {"rule_id": manifest["rule_id"], "target_run_id": target_run_id, "ts": current},
        ).split(":", 1)[1][:24]
    )
    capability_id = manifest["capability_id"]
    common = {
        "capability_ids": [capability_id],
        "result": {"version_hash": manifest["content_hash"]},
    }
    match_event = feedback.record_completion_event(
        invocation_id,
        event_type="workflow",
        phase="trigger",
        producer="orchestrator",
        status="recorded",
        payload={
            **common,
            "result": {**common["result"], "matched": True, "status": "matched"},
        },
        timestamp=current,
    )
    injection_event = feedback.record_completion_event(
        invocation_id,
        event_type="workflow",
        phase="decision",
        producer="orchestrator",
        status="recorded",
        payload={
            **common,
            "result": {
                **common["result"],
                "matched": True,
                "invoked": True,
                "action_id": "playbook-injection",
                "status": "injected",
            },
        },
        timestamp=current,
    )
    acceptance_event = feedback.record_completion_event(
        invocation_id,
        event_type="workflow",
        phase="artifact",
        producer="orchestrator",
        status="succeeded" if accepted else "failed",
        payload={
            "capability_ids": [capability_id],
            "result_hashes": [manifest["content_hash"]],
            "result": {
                "accepted": bool(accepted),
                "status": "accepted" if accepted else "rejected",
            },
        },
        timestamp=current,
    )
    edge = feedback.record_influence_edge(
        target_run_id=target_run_id,
        influence_type="capability",
        influence_id=manifest["rule_id"],
        accepted=accepted,
        source_event_id=acceptance_event["event_id"],
        acceptance_gate_id="repo-playbook-low-risk-canary",
        metadata={"version_hash": manifest["content_hash"]},
    )
    event_prefix = stable_hash("playbook-capability-event", invocation_id)
    for event_type, ref in (
        ("match", match_event["event_id"]),
        ("invocation", injection_event["event_id"]),
        ("output", manifest["content_hash"]),
        ("consumer", acceptance_event["event_id"]),
    ):
        capabilities.heartbeat(
            capability_id,
            event_type,
            ref=ref,
            path=ledger_path,
            idempotency_key=f"{event_prefix}:{event_type}",
        )
    outcome = None
    with feedback._conn() as conn:
        outcome = conn.execute(
            "SELECT adjudicated_verdict,merged,durability FROM outcomes WHERE run_id=?",
            (target_run_id,),
        ).fetchone()
        edge_row = conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,merged,durability FROM influence_edges WHERE edge_id=?",
            (edge["edge_id"],),
        ).fetchone()
    outcome_ref = None
    if accepted and outcome:
        outcome_ref = stable_hash(
            "playbook-downstream-outcome",
            {"target_run_id": target_run_id, "outcome": list(outcome)},
        )
        capabilities.heartbeat(
            capability_id,
            "success",
            ref=outcome_ref,
            path=ledger_path,
            idempotency_key=f"{event_prefix}:success",
        )
        capabilities.heartbeat(
            capability_id,
            "outcome",
            ref=outcome_ref,
            path=ledger_path,
            idempotency_key=f"{event_prefix}:outcome",
        )
    for probe, ref in (
        ("producer_probe", manifest["content_hash"]),
        ("consumer_probe", acceptance_event["event_id"]),
        ("rollback_probe", manifest["lifecycle"]["predecessor"]),
    ):
        capabilities.record_probe(capability_id, probe, passed=True, ref=ref, path=ledger_path)
    if outcome_ref:
        capabilities.record_probe(
            capability_id, "outcome_probe", passed=True, ref=outcome_ref, path=ledger_path
        )
    return {
        "matched": True,
        "injected": True,
        "accepted": bool(accepted),
        "invocation_id": invocation_id,
        "events": [match_event, injection_event, acceptance_event],
        "edge": tuple(edge_row) if edge_row else None,
        "outcome_ref": outcome_ref,
    }


def rollback_playbook_canary(
    manifest: dict[str, Any],
    *,
    repo_root: Path,
    registry_path: Path,
    ledger_path: Path,
    workflows_bundle_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove exactly this generated rule and retire its capability."""
    validate_playbook_manifest(manifest)
    removal = repo_knowledge.remove_managed_rule(
        manifest["rule_id"], repo=manifest["repo"], path=registry_path, apply=apply
    )
    agents = repo_knowledge.update_agents_md(
        repo_root, repo=manifest["repo"], path=registry_path, apply=apply
    )
    bundle = None
    if workflows_bundle_path is not None and manifest["optional_workflows_bundle"]:
        bundle = repo_knowledge.update_capability_bundle(
            workflows_bundle_path, manifest, remove=True, apply=apply
        )
    if apply and ledger_path.exists():
        caps = capabilities.load(ledger_path, create=False)
        if (
            manifest["capability_id"] in caps
            and caps[manifest["capability_id"]]["status"] != "retired"
        ):
            capabilities.transition(
                manifest["capability_id"],
                "retired",
                reason="managed playbook rule rolled back to predecessor",
                evidence_refs=[manifest["lifecycle"]["predecessor"]],
                path=ledger_path,
            )
    return {
        "removal": removal,
        "agents_md": agents,
        "bundle": bundle,
        "predecessor": manifest["lifecycle"]["predecessor"],
    }


def _selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-compiler-") as tmp:
        root = Path(tmp)
        first = run_reference_workflow(ledger_path=root / "capabilities.json")
        second = run_reference_workflow(ledger_path=root / "capabilities.json")
        assert first == second
        assert first["result"]["side_effects"] == []
        package = compile_skill_package(
            reference_skill_source(), output_root=root / "skill-candidates"
        )
        manifest = validate_skill_package(Path(package["package_path"]))
        assert manifest["lifecycle"]["state"] == "shadow"
        assert manifest["lifecycle"]["globally_installed"] is False
        role_compiled = compile_role_capability(reference_role_candidate())
        role_manifest = _validate_generated_role_manifest(role_compiled["manifest"])
        assert (
            role_compiled["role"].validate(
                {"decision": "inspect", "rationale": "Inspect the named evidence gap."}
            )
            == []
        )
        assert role_manifest["shadow_only"] and role_manifest["profile_agnostic"]
        deterministic = reference_role_candidate().to_dict()
        deterministic["graph"]["decision_mode"] = "deterministic"
        deterministic["graph"]["requires_judgment"] = False
        decision = compile_role_candidate(deterministic)
        assert decision["target"] == "workflow" and decision["executable"] is False
        playbook_root = root / "playbook-repo"
        (playbook_root / "docs").mkdir(parents=True)
        (playbook_root / "docs" / "RULES.md").write_text(
            "registry_symbol is required.\n", encoding="utf-8"
        )
        (playbook_root / "AGENTS.md").write_text(
            "# User instructions\n\nPreserve this.\n", encoding="utf-8"
        )
        playbook_registry = root / "repo-knowledge.json"
        playbook_registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repos": {
                        "owner/reference": {
                            "summary": "Reference repo.",
                            "definition_of_done": [],
                            "gotchas": [],
                            "validation": [],
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raw_playbook = reference_role_candidate().to_dict()
        raw_playbook["capability_id"] = "capability:reference-repo-playbook"
        raw_playbook["kind_proposal"] = "playbook"
        raw_playbook["owner_proposal"] = "repo"
        raw_playbook["independent_repositories"] = ["owner/reference"]
        for occurrence in raw_playbook["source_occurrences"]:
            occurrence["repository"] = "owner/reference"
        selector = {"repo": "owner/reference", "task_types": ["implement"], "lanes": ["opener"]}
        current_refs = [{"path": "docs/RULES.md", "symbol": "registry_symbol"}]
        rule_text = "When changing `docs/RULES.md`, retain the `registry_symbol` and run the narrow validation."
        content_hash = stable_hash(
            "repo-playbook-rule",
            {
                "repo": "owner/reference",
                "section": "validation",
                "text": rule_text,
                "selector": selector,
                "current_refs": current_refs,
            },
        )
        raw_playbook["selector"] = selector
        raw_playbook["graph"] = {
            "phase_order": [
                "trigger",
                "decision",
                "execution",
                "artifact",
                "verification",
                "outcome",
                "durability",
            ],
            "playbook_contract": {
                "schema": PLAYBOOK_SOURCE_SCHEMA,
                "version": PLAYBOOK_VERSION,
                "repo": "owner/reference",
                "section": "validation",
                "text": rule_text,
                "content_hash": content_hash,
                "selector": selector,
                "current_refs": current_refs,
                "negative_examples": [
                    {
                        "text": "A prior change omitted the registry symbol and required repair.",
                        "evidence_ref": stable_hash("reference-playbook-negative", "one"),
                    }
                ],
                "risk_level": "low_reversible",
                "expires_at": int(time.time()) + 86400,
                "rollback": {
                    "action": "remove_managed_rule_and_restore_predecessor",
                    "predecessor": "role-adjudicator",
                    "reason": "canary regressed",
                },
                "optional_workflows_bundle": False,
            },
        }
        playbook_manifest = compile_playbook_capability(
            raw_playbook, repo_root=playbook_root, registry_path=playbook_registry
        )
        exported = export_playbook_canary(
            playbook_manifest,
            repo_root=playbook_root,
            registry_path=playbook_registry,
            ledger_path=root / "playbook-capabilities.json",
            apply=True,
        )
        assert exported["block_status"]["status"] == "current"
        assert "Preserve this." in (playbook_root / "AGENTS.md").read_text()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("reference", "selftest"))
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args(argv)
    if args.command == "selftest":
        _selftest()
        print("capability_compiler.py selftest: OK")
        return 0
    if not args.ledger:
        parser.error("reference requires --ledger")
    print(json.dumps(run_reference_workflow(ledger_path=args.ledger), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
