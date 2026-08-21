#!/usr/bin/env python3
"""Versioned, storage-independent completion-event adapter for pattern mining.

The feedback-table query/join intentionally lives outside this module.  Issue 7
may produce the envelope below from ``completion_events`` joined to ``runs`` and
``execution_attempts``; the miner consumes only this contract::

    {
      "schema": "orchestrator.completion-event-envelope", "version": 1,
      "event": {
        "event_id": "evt-1", "schema_version": 1, "run_id": "run-1",
        "attempt_id": null, "event_type": "completion",
        "phase": "trigger", "producer": "dispatcher",
        "status": "complete", "validation_status": "accepted",
        "payload": {}, "content_hash": "sha256:...", "redaction_count": 0,
        "created_ts": 100, "updated_ts": 100
      },
      "identity": {
        "subject_id": "subject:...", "observation_id": "observation:...",
        "family_id": "family:...", "attempt_id": "worker-attempt-1",
        "attempt_resolution": "resolved",
        "canonical_target": "owner/repo#1", "repository": "owner/repo",
        "task_type": "implement", "normalized_spec_hash": "sha256:...",
        "base_sha": "abc123", "profile_id": "codex:gpt-5.6",
        "arm_id": "worker", "resolved_provider": "openai",
        "resolved_model": "gpt-5.6", "subject_arms": ["worker"],
        "subject_profiles": {"worker": "codex:gpt-5.6"}
      }
    }

``payload_json`` may be supplied instead of decoded ``payload``. Run-level rows
may have a null event ``attempt_id``; the joined identity must select one worker
attempt. If more than one successful worker attempt is possible, the join emits
``attempt_resolution=ambiguous`` and the adapter rejects it. No raw prompt
or secret field is accepted.  A seven-row episode uses the phases trigger,
decision, execution, artifact, verification, outcome, and durability.

Identity fields are not caller assertions. ``subject_id`` reuses the canonical
research-subject identity over target, task type, spec hash, base SHA, and the
full arm/profile sets. ``observation_id`` binds that subject to run/attempt.
Selected arm/profile and resolved provider/model remain attempt provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

import research_subjects


ENVELOPE_SCHEMA = "orchestrator.completion-event-envelope"
ENVELOPE_VERSION = 1
EVENT_SCHEMA_VERSION = 1
PHASES = (
    "trigger",
    "decision",
    "execution",
    "artifact",
    "verification",
    "outcome",
    "durability",
)
PAYLOAD_ALLOWLIST = {
    "adjudication_id",
    "acceptance_gate_ids",
    "artifact_refs",
    "capability_ids",
    "capability_effects",
    "changed_path_classes",
    "command_ids",
    "delivery",
    "durability",
    "panel_ids",
    "result",
    "result_hashes",
    "retry_sequence",
    "role_ids",
    "root_cause_ids",
    "skill_ids",
    "test_ids",
    "verification",
    "workflow_ids",
}
RAW_PROMPT_KEYS = {"prompt", "prompt_text", "raw_prompt", "system_prompt", "user_prompt"}
SECRET_KEYS = {"access_token", "api_key", "password", "private_key", "secret", "token"}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TARGET_RE = re.compile(r"^([^/\s]+)/([^#\s]+)#(\d+)$")
RESULT_ALLOWLIST = {
    "action_id",
    "backend_run_id",
    "decision_source_id",
    "influence_id",
    "influence_type",
    "influenced_run_id",
    "notes_hash",
    "outcome_verdict",
    "operation_role",
    "proposal_hash",
    "status",
    "trace_key_hash",
    "version_hash",
}
ARTIFACT_REF_REQUIRED_FIELDS = {"artifact_id", "kind", "content_hash"}
ARTIFACT_REF_OPTIONAL_FIELDS = {"ref_class"}
CAPABILITY_ID_RE = re.compile(r"^capability:[a-z0-9][a-z0-9-]{2,127}$")
EVIDENCE_ARTIFACT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,255}$")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CAPABILITY_EFFECT_SCHEMA = "workflows.agent-runner-capability-effect/v2"
# v1 stays valid forever: existing runner outputs in flight must keep validating, so the version
# bump is ADDITIVE. v2 adds the optional `subject_id` — the consumer repository a compiled-workflow
# rail acted on. Without it the subject was DROPPED at this boundary, and a promotion gate that
# counts "distinct durable subjects" had nothing to count: `_causal_readiness` fell back to the
# PR target, which would have let three PRs from one pilot pose as three independent subjects.
CAPABILITY_EFFECT_SCHEMA_V1 = "workflows.agent-runner-capability-effect/v1"
CAPABILITY_EFFECT_SCHEMAS = (CAPABILITY_EFFECT_SCHEMA_V1, CAPABILITY_EFFECT_SCHEMA)
CAPABILITY_EFFECT_OPTIONAL_FIELDS = {"subject_id"}
CAPABILITY_EFFECT_FIELDS = {
    "schema",
    "capability_id",
    "effect_fingerprint",
    "evidence_artifact_ref",
    "supervision_mode",
    "evidence_status",
    "terminal_disposition",
}
SUPERVISION_MODES = {
    "shadow",
    "human-reviewed",
    "human-on-exception",
    "unattended",
}
CAPABILITY_EVIDENCE_STATUSES = {"accepted", "rejected", "not-evaluated"}
TERMINAL_DISPOSITIONS = {
    "success",
    "failure",
    "no-change",
    "blocked",
    "cancelled",
}


class EnvelopeError(ValueError):
    def __init__(self, event_id: str, reasons: Iterable[str]):
        self.event_id = event_id
        self.reasons = tuple(dict.fromkeys(reasons))
        super().__init__(", ".join(self.reasons))


@dataclass(frozen=True)
class CompletionIdentity:
    subject_id: str
    observation_id: str
    family_id: str | None
    attempt_id: str
    canonical_target: str
    repository: str
    task_type: str
    normalized_spec_hash: str
    base_sha: str
    profile_id: str
    arm_id: str
    resolved_provider: str
    resolved_model: str


@dataclass(frozen=True)
class CompletionEvent:
    event_id: str
    run_id: str
    attempt_id: str
    event_type: str
    phase: str
    producer: str
    status: str
    validation_status: str
    payload: dict[str, Any]
    content_hash: str
    redaction_count: int
    occurred_at: int
    created_at: int
    identity: CompletionIdentity


def validate_capability_effect_record(
    raw: Any, *, expected_capability_ids: Iterable[str] = ()
) -> dict[str, str]:
    """Validate one typed runner effect without accepting prose or credentials."""
    if not isinstance(raw, dict):
        raise ValueError("invalid_capability_effect_fields")
    present = set(raw)
    # Required fields exactly; optional fields may be present or absent, nothing else is tolerated.
    if not (CAPABILITY_EFFECT_FIELDS <= present
            and present <= CAPABILITY_EFFECT_FIELDS | CAPABILITY_EFFECT_OPTIONAL_FIELDS):
        raise ValueError("invalid_capability_effect_fields")
    keys = CAPABILITY_EFFECT_FIELDS | (present & CAPABILITY_EFFECT_OPTIONAL_FIELDS)
    record = {key: _string(raw.get(key)) for key in keys}
    record = {key: value.lower() if key != "evidence_artifact_ref" else value for key, value in record.items()}
    if record["schema"] not in CAPABILITY_EFFECT_SCHEMAS:
        raise ValueError("unsupported_capability_effect_schema")
    # An empty optional is the same as absent — never a subject called "".
    if not record.get("subject_id"):
        record.pop("subject_id", None)
    # A subject must be a real repository, so "distinct durable subjects" cannot be gamed by prose.
    if "subject_id" in record and not REPOSITORY_RE.fullmatch(record["subject_id"]):
        raise ValueError("invalid_capability_effect_subject_id")
    if not CAPABILITY_ID_RE.fullmatch(record["capability_id"]):
        raise ValueError("invalid_capability_effect_id")
    expected = {_string(value).lower() for value in expected_capability_ids}
    if expected and record["capability_id"] not in expected:
        raise ValueError("capability_effect_not_linked_to_payload")
    if not SHA256_RE.fullmatch(record["effect_fingerprint"]):
        raise ValueError("invalid_capability_effect_fingerprint")
    artifact_ref = record["evidence_artifact_ref"]
    if not EVIDENCE_ARTIFACT_REF_RE.fullmatch(artifact_ref):
        raise ValueError("invalid_capability_effect_artifact_ref")
    lowered_ref = artifact_ref.lower()
    if any(marker in lowered_ref for marker in ("token", "secret", "password", "api-key", "apikey")):
        raise ValueError("secret_like_capability_effect_artifact_ref")
    if record["supervision_mode"] not in SUPERVISION_MODES:
        raise ValueError("unsupported_capability_effect_supervision_mode")
    if record["evidence_status"] not in CAPABILITY_EVIDENCE_STATUSES:
        raise ValueError("unsupported_capability_effect_evidence_status")
    if record["terminal_disposition"] not in TERMINAL_DISPOSITIONS:
        raise ValueError("unsupported_capability_effect_terminal_disposition")
    return record


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).strip().lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _canonical_target(value: Any) -> tuple[str, str] | None:
    match = TARGET_RE.fullmatch(_string(value))
    if not match:
        return None
    owner, repo, issue = match.groups()
    repository = f"{owner.lower()}/{repo.lower()}"
    return f"{repository}#{int(issue)}", repository


def derive_completion_identity_ids(
    *,
    canonical_target: str,
    task_type: str,
    normalized_spec_hash: str,
    base_sha: str,
    run_id: str,
    attempt_id: str,
    subject_arms: list[str],
    subject_profiles: dict[str, Any] | list[Any],
) -> dict[str, str]:
    """Derive the canonical family/subject/observation identity contract."""
    identity = research_subjects.subject_identity_from_hash(
        canonical_target,
        task_type,
        normalized_spec_hash,
        base_sha,
        subject_arms,
        subject_profiles,
    )
    return {
        "family_id": identity["subject_family_id"],
        "subject_id": identity["subject_id"],
        "observation_id": research_subjects.completion_observation_id(
            identity["subject_id"], run_id, attempt_id
        ),
    }


def adapt_completion_event_envelope(raw: dict[str, Any]) -> CompletionEvent:
    """Validate one joined v1 envelope and return a typed event.

    Every failure is expressed as a stable machine rejection code so an
    operator can distinguish missing upstream joins from weak pattern evidence.
    """
    event_raw = raw.get("event") if isinstance(raw, dict) else None
    event_id = _string((event_raw or {}).get("event_id")) or "<unknown>"
    reasons: list[str] = []
    if not isinstance(raw, dict) or raw.get("schema") != ENVELOPE_SCHEMA:
        reasons.append("unsupported_completion_event_envelope_schema")
    if not isinstance(raw, dict) or raw.get("version") != ENVELOPE_VERSION:
        reasons.append("unsupported_completion_event_envelope_version")
    if not isinstance(event_raw, dict):
        raise EnvelopeError(event_id, [*reasons, "missing_event_record"])
    identity_raw = raw.get("identity")
    if not isinstance(identity_raw, dict):
        raise EnvelopeError(event_id, [*reasons, "missing_joined_identity"])

    if event_raw.get("schema_version") != EVENT_SCHEMA_VERSION:
        reasons.append("unsupported_completion_event_schema_version")
    for field in (
        "event_id",
        "run_id",
        "event_type",
        "phase",
        "producer",
        "status",
        "validation_status",
        "content_hash",
        "created_ts",
        "updated_ts",
    ):
        if event_raw.get(field) in (None, ""):
            reasons.append(f"missing_event_{field}")
    if event_raw.get("content_hash") and not SHA256_RE.fullmatch(
        _string(event_raw.get("content_hash")).lower()
    ):
        reasons.append("invalid_completion_event_content_hash")
    phase = _string(event_raw.get("phase")).lower()
    if phase not in PHASES:
        reasons.append("unsupported_completion_event_phase")
    if _string(event_raw.get("validation_status")).lower() != "accepted":
        reasons.append("completion_event_not_validated")
    try:
        redaction_count = int(event_raw.get("redaction_count") or 0)
    except (TypeError, ValueError):
        redaction_count = -1
    if redaction_count != 0:
        reasons.append("completion_event_contains_redactions")

    payload = event_raw.get("payload")
    if payload is None and "payload_json" in event_raw:
        try:
            payload = json.loads(event_raw["payload_json"])
        except (TypeError, json.JSONDecodeError):
            reasons.append("invalid_payload_json")
    if not isinstance(payload, dict):
        reasons.append("completion_event_payload_not_object")
        payload = {}
    unexpected = sorted(set(payload) - PAYLOAD_ALLOWLIST)
    if unexpected:
        reasons.append("payload_field_not_allowlisted:" + ",".join(unexpected))
    keys = set(_walk_keys(payload))
    if keys & RAW_PROMPT_KEYS:
        reasons.append("raw_prompt_field_present")
    if keys & SECRET_KEYS:
        reasons.append("secret_field_present")
    result = payload.get("result")
    if result is not None:
        if not isinstance(result, dict):
            reasons.append("result_metadata_not_object")
        else:
            unexpected_result = sorted(set(result) - RESULT_ALLOWLIST)
            if unexpected_result:
                reasons.append(
                    "result_field_not_allowlisted:" + ",".join(unexpected_result)
                )
    artifact_refs = payload.get("artifact_refs")
    if artifact_refs is not None:
        if not isinstance(artifact_refs, list):
            reasons.append("artifact_refs_not_array")
        else:
            for index, artifact in enumerate(artifact_refs):
                if not isinstance(artifact, dict):
                    reasons.append(f"invalid_artifact_ref:{index}")
                    continue
                if not set(artifact) <= (
                    ARTIFACT_REF_REQUIRED_FIELDS | ARTIFACT_REF_OPTIONAL_FIELDS
                ):
                    reasons.append(f"invalid_artifact_ref_fields:{index}")
                if not all(
                    _string(artifact.get(field))
                    for field in ARTIFACT_REF_REQUIRED_FIELDS
                ):
                    reasons.append(f"incomplete_artifact_ref:{index}")
                if not SHA256_RE.fullmatch(
                    _string(artifact.get("content_hash")).lower()
                ):
                    reasons.append(f"invalid_artifact_content_hash:{index}")
    capability_effects = payload.get("capability_effects")
    if capability_effects is not None:
        if not isinstance(capability_effects, list):
            reasons.append("capability_effects_not_array")
        else:
            capability_ids = payload.get("capability_ids")
            linked_ids = capability_ids if isinstance(capability_ids, list) else []
            for index, effect in enumerate(capability_effects):
                try:
                    validate_capability_effect_record(
                        effect, expected_capability_ids=linked_ids
                    )
                except ValueError as exc:
                    reasons.append(f"{exc}:{index}")
    if _string(event_raw.get("event_type")).lower() in {
        "close_note",
        "merge_note",
        "terminal_note",
    }:
        reasons.append("generic_terminal_note_only")

    target = _canonical_target(identity_raw.get("canonical_target"))
    if not target:
        reasons.append("invalid_canonical_target")
        canonical_target, derived_repo = "", ""
    else:
        canonical_target, derived_repo = target
    repository = _string(identity_raw.get("repository")).lower()
    if repository != derived_repo:
        reasons.append("repository_target_mismatch")
    task_type = _string(identity_raw.get("task_type")).lower()
    if not task_type:
        reasons.append("missing_task_type")
    spec_hash = _string(identity_raw.get("normalized_spec_hash")).lower()
    if not SHA256_RE.fullmatch(spec_hash):
        reasons.append("invalid_normalized_spec_hash")
    base_sha = _string(identity_raw.get("base_sha")).lower()
    if not base_sha:
        reasons.append("missing_base_sha")
    profile_id = _string(identity_raw.get("profile_id")).lower()
    arm_id = _string(identity_raw.get("arm_id")).lower()
    provider = _string(identity_raw.get("resolved_provider")).lower()
    model = _string(identity_raw.get("resolved_model")).lower()
    if not (profile_id or arm_id) or not provider or not model:
        reasons.append("unresolved_model_provenance")
    supplied_subject_id = _string(identity_raw.get("subject_id"))
    supplied_observation_id = _string(identity_raw.get("observation_id"))
    supplied_family_id = _string(identity_raw.get("family_id")) or None
    joined_attempt_id = _string(identity_raw.get("attempt_id"))
    attempt_resolution = _string(identity_raw.get("attempt_resolution")).lower()
    subject_arms_raw = identity_raw.get("subject_arms")
    if not isinstance(subject_arms_raw, list):
        reasons.append("invalid_subject_arm_set")
        subject_arms: list[str] = []
    else:
        subject_arms = sorted(
            {_string(value).lower() for value in subject_arms_raw if _string(value)}
        )
    subject_profiles = identity_raw.get("subject_profiles")
    if not isinstance(subject_profiles, (dict, list)):
        reasons.append("invalid_subject_profile_set")
        subject_profiles = {}
    if not subject_arms and not subject_profiles:
        reasons.append("missing_subject_design_set")
    if arm_id and subject_arms and arm_id not in subject_arms:
        reasons.append("selected_arm_not_in_subject_set")
    if isinstance(subject_profiles, list) and profile_id:
        profile_set = {_string(value).lower() for value in subject_profiles}
        if profile_id not in profile_set:
            reasons.append("selected_profile_not_in_subject_set")
    if not supplied_subject_id:
        reasons.append("missing_subject_id")
    if not supplied_observation_id:
        reasons.append("missing_observation_id")
    expected_ids = derive_completion_identity_ids(
        canonical_target=canonical_target,
        task_type=task_type,
        normalized_spec_hash=spec_hash,
        base_sha=base_sha,
        run_id=_string(event_raw.get("run_id")),
        attempt_id=joined_attempt_id,
        subject_arms=subject_arms,
        subject_profiles=subject_profiles,
    )
    if supplied_subject_id and supplied_subject_id != expected_ids["subject_id"]:
        reasons.append("subject_identity_mismatch")
    if (
        supplied_observation_id
        and supplied_observation_id != expected_ids["observation_id"]
    ):
        reasons.append("observation_identity_mismatch")
    if supplied_family_id and supplied_family_id != expected_ids["family_id"]:
        reasons.append("family_identity_mismatch")
    if attempt_resolution == "ambiguous":
        reasons.append("ambiguous_multiple_successful_attempts")
    elif attempt_resolution != "resolved":
        reasons.append("worker_attempt_not_resolved")
    if not joined_attempt_id:
        reasons.append("missing_joined_attempt_id")
    raw_attempt_id = _string(event_raw.get("attempt_id"))
    if raw_attempt_id and joined_attempt_id and raw_attempt_id != joined_attempt_id:
        reasons.append("event_attempt_join_mismatch")
    if reasons:
        raise EnvelopeError(event_id, reasons)

    return CompletionEvent(
        event_id=event_id,
        run_id=_string(event_raw["run_id"]),
        attempt_id=joined_attempt_id,
        event_type=_string(event_raw["event_type"]).lower(),
        phase=phase,
        producer=_string(event_raw["producer"]).lower(),
        status=_string(event_raw["status"]).lower(),
        validation_status="accepted",
        payload=payload,
        content_hash=_string(event_raw["content_hash"]).lower(),
        redaction_count=0,
        occurred_at=int(event_raw["updated_ts"] or event_raw["created_ts"]),
        created_at=int(event_raw["created_ts"]),
        identity=CompletionIdentity(
            subject_id=expected_ids["subject_id"],
            observation_id=expected_ids["observation_id"],
            family_id=expected_ids["family_id"],
            attempt_id=joined_attempt_id,
            canonical_target=canonical_target,
            repository=repository,
            task_type=task_type,
            normalized_spec_hash=spec_hash,
            base_sha=base_sha,
            profile_id=profile_id,
            arm_id=arm_id,
            resolved_provider=provider,
            resolved_model=model,
        ),
    )
