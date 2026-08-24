#!/usr/bin/env python3
"""Read-only consumer-sync drift classifier and shadow promotion dashboard."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any

import capabilities
import capability_compiler
from runner_effect_bridge import record_runner_effect, runner_outputs_to_effect

PLAN_SCHEMA = "workflows.consumer-sync-plan/v1"
HANDOFF_SCHEMA = "workflows.consumer-sync-shadow-handoff/v1"
SHADOW_RESULT_SCHEMA = "orchestrator.consumer-sync-shadow-result/v1"
CAPABILITY_ID = "capability:reference-sync-hygiene-test-gate"
ALLOWED_ACTIONS = {"create", "update", "remove", "skip", "no_change"}
ALLOWED_REASONS = {
    "manifest_skip",
    "target_missing",
    "create_only_existing",
    "content_matches",
    "content_differs",
    "obsolete_target_present",
    "obsolete_target_absent",
}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,255}$")
ENTRY_FIELDS = {
    "section",
    "source",
    "resolved_source",
    "target",
    "description",
    "sync_mode",
    "is_directory",
    "skip_repos",
    "skip_reasons",
    "overwrite_repos",
    "template_sync",
    "delivery",
    # Added by the producer (Workflows scripts/sync_manifest_compiler.py: ManifestEntry.requires,
    # carried in both plan_record() and effect_core). Unlike the artifact member list, this set
    # stays EXACT on purpose: effect_fingerprint is verified over ENTRY_FIELDS minus
    # {effect_fingerprint, description}, so tolerating an unknown field would mean hashing a shape
    # nobody validated — and the fingerprint would mismatch anyway. Pinning it is what makes the
    # identity check mean something; the cost is that a producer field addition must land here too.
    "requires",
    "content_sha256",
    "effect_fingerprint",
}
REMOVAL_FIELDS = {"target", "description", "effect_fingerprint"}


class ConsumerSyncShadowError(ValueError):
    def __init__(self, reasons: list[str]):
        self.reasons = tuple(dict.fromkeys(reasons))
        super().__init__("; ".join(self.reasons))


def _stable_hash(namespace: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(namespace.encode() + b"\0" + encoded).hexdigest()


def _safe_path(value: Any) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or text.startswith("./"):
        return None
    return str(path)


def validate_shadow_handoff(raw: Any, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Validate the Workflows producer envelope without granting authority."""
    fields = {
        "schema",
        "version",
        "handoff_id",
        "capability_id",
        "plan_schema",
        "plan_id",
        "manifest_sha256",
        "entry_count",
        "removal_count",
        "plan_filename",
        "run_ref",
        "supervision_mode",
        "write_authority",
        "promotion_allowed",
        "effect_allowlist",
        "kill_switch",
        "consumer",
    }
    reasons: list[str] = []
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ConsumerSyncShadowError(["invalid_shadow_handoff_fields"])
    if raw.get("schema") != HANDOFF_SCHEMA or raw.get("version") != 1:
        reasons.append("unsupported_shadow_handoff_schema")
    if raw.get("capability_id") != CAPABILITY_ID:
        reasons.append("shadow_handoff_capability_mismatch")
    if raw.get("plan_schema") != PLAN_SCHEMA or raw.get("plan_id") != plan.get("plan_id"):
        reasons.append("shadow_handoff_plan_mismatch")
    if raw.get("manifest_sha256") != plan.get("manifest_sha256"):
        reasons.append("shadow_handoff_manifest_mismatch")
    if raw.get("entry_count") != len(plan.get("entries") or []) or raw.get("removal_count") != len(
        plan.get("removals") or []
    ):
        reasons.append("shadow_handoff_count_mismatch")
    if raw.get("plan_filename") != "consumer-sync-plan.json":
        reasons.append("shadow_handoff_plan_filename_mismatch")
    run_ref = str(raw.get("run_ref") or "")
    if not RUN_REF_RE.fullmatch(run_ref):
        reasons.append("invalid_shadow_handoff_run_ref")
    if any(
        marker in run_ref.lower() for marker in ("token", "secret", "password", "api-key", "apikey")
    ):
        reasons.append("secret_like_shadow_handoff_run_ref")
    if raw.get("supervision_mode") != "shadow":
        reasons.append("shadow_handoff_supervision_mismatch")
    if raw.get("write_authority") is not False or raw.get("promotion_allowed") is not False:
        reasons.append("shadow_handoff_requests_authority")
    if raw.get("effect_allowlist") != [
        "create",
        "update",
        "remove",
        "skip",
        "no_change",
    ]:
        reasons.append("shadow_handoff_effect_allowlist_mismatch")
    if raw.get("kill_switch") != "ORCH_REFERENCE_WORKFLOW_DISABLED=1":
        reasons.append("shadow_handoff_kill_switch_mismatch")
    if raw.get("consumer") != "Orchestrator/consumer_sync_shadow.py":
        reasons.append("shadow_handoff_consumer_mismatch")
    core = {key: raw[key] for key in sorted(fields - {"handoff_id"})}
    if raw.get("handoff_id") != _stable_hash("consumer-sync-shadow-handoff", core):
        reasons.append("shadow_handoff_identity_mismatch")
    if reasons:
        raise ConsumerSyncShadowError(reasons)
    return dict(raw)


def validate_consumer_sync_plan(raw: Any) -> dict[str, Any]:
    """Validate the typed Workflows plan; reject prose and executable fields."""
    reasons: list[str] = []
    if not isinstance(raw, dict):
        raise ConsumerSyncShadowError(["plan_not_object"])
    allowed = {"schema", "version", "plan_id", "manifest_sha256", "entries", "removals"}
    if set(raw) != allowed:
        reasons.append("invalid_plan_fields")
    if raw.get("schema") != PLAN_SCHEMA or raw.get("version") != 1:
        reasons.append("unsupported_plan_schema")
    if not SHA256_RE.fullmatch(str(raw.get("manifest_sha256") or "")):
        reasons.append("invalid_manifest_sha256")
    entries_raw = raw.get("entries")
    removals_raw = raw.get("removals")
    if not isinstance(entries_raw, list):
        reasons.append("entries_not_array")
        entries_raw = []
    if not isinstance(removals_raw, list):
        reasons.append("removals_not_array")
        removals_raw = []
    entries: list[dict[str, Any]] = []
    target_owners: dict[str, str] = {}
    for index, entry in enumerate(entries_raw):
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            # Name the drift. This used to emit 225 identical `invalid_entry_fields:N` strings for
            # one added producer field, which said nothing about WHICH field moved.
            detail = ""
            if isinstance(entry, dict):
                unexpected = sorted(set(entry) - ENTRY_FIELDS)
                missing = sorted(ENTRY_FIELDS - set(entry))
                detail = f":unexpected={unexpected}:missing={missing}"
            reasons.append(f"invalid_entry_fields:{index}{detail}")
            continue
        normalized = dict(entry)
        for field in ("source", "resolved_source", "target"):
            path = _safe_path(entry.get(field))
            if path is None:
                reasons.append(f"unsafe_{field}:{index}")
            else:
                normalized[field] = path
        if not isinstance(entry.get("section"), str) or not entry["section"].strip():
            reasons.append(f"invalid_section:{index}")
        if entry.get("sync_mode") not in (None, "create_only"):
            reasons.append(f"unsupported_sync_mode:{index}")
        if not isinstance(entry.get("is_directory"), bool):
            reasons.append(f"invalid_is_directory:{index}")
        for field in ("skip_repos", "overwrite_repos", "requires"):
            values = entry.get(field)
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                reasons.append(f"invalid_{field}:{index}")
        # `requires` names other manifest TARGETS. It never becomes a filesystem path in this
        # consumer, but it is hashed into the effect identity, so it is checked, not normalised —
        # rewriting it here would desynchronise the fingerprint from the producer's.
        if isinstance(entry.get("requires"), list) and any(
            _safe_path(dep) is None for dep in entry["requires"]
        ):
            reasons.append(f"unsafe_requires:{index}")
        skip_reasons = entry.get("skip_reasons")
        if (
            not isinstance(skip_reasons, dict)
            or set(skip_reasons) != set(entry.get("skip_repos") or [])
            or any(not isinstance(value, str) for value in skip_reasons.values())
        ):
            reasons.append(f"invalid_skip_reasons:{index}")
        if entry.get("template_sync") not in (None, "exact"):
            reasons.append(f"unsupported_template_sync:{index}")
        if entry.get("delivery") != "copy":
            reasons.append(f"unsupported_delivery:{index}")
        for field in ("content_sha256", "effect_fingerprint"):
            if not SHA256_RE.fullmatch(str(entry.get(field) or "")):
                reasons.append(f"invalid_{field}:{index}")
        effect_fields = ENTRY_FIELDS - {
            "effect_fingerprint",
            "description",
        }
        expected_effect = _stable_hash(
            "consumer-sync-source-effect",
            {key: normalized.get(key) for key in sorted(effect_fields)},
        )
        if entry.get("effect_fingerprint") != expected_effect:
            reasons.append(f"entry_effect_identity_mismatch:{index}")
        target = str(normalized.get("target") or "")
        if target in target_owners:
            reasons.append(f"duplicate_target:{target}")
        target_owners[target] = f"entry:{index}"
        entries.append(normalized)
    removals: list[dict[str, str]] = []
    for index, removal in enumerate(removals_raw):
        if not isinstance(removal, dict) or set(removal) != REMOVAL_FIELDS:
            reasons.append(f"invalid_removal_fields:{index}")
            continue
        removal_target = _safe_path(removal.get("target"))
        if removal_target is None:
            reasons.append(f"unsafe_removal_target:{index}")
            continue
        fingerprint = str(removal.get("effect_fingerprint") or "")
        if not SHA256_RE.fullmatch(fingerprint):
            reasons.append(f"invalid_removal_effect_fingerprint:{index}")
        if fingerprint != _stable_hash("consumer-sync-removal-effect", {"target": removal_target}):
            reasons.append(f"removal_effect_identity_mismatch:{index}")
        if removal_target in target_owners:
            reasons.append(f"duplicate_target:{removal_target}")
        target_owners[removal_target] = f"removal:{index}"
        description = str(removal.get("description") or "").strip()
        if not description:
            reasons.append(f"invalid_removal_description:{index}")
        removals.append(
            {
                "target": removal_target,
                "description": description,
                "effect_fingerprint": fingerprint,
            }
        )
    core = {
        "schema": PLAN_SCHEMA,
        "version": 1,
        "manifest_sha256": raw.get("manifest_sha256"),
        "entries": entries,
        "removals": removals,
    }
    expected_plan_id = _stable_hash("consumer-sync-plan", core)
    if raw.get("plan_id") != expected_plan_id:
        reasons.append("plan_identity_mismatch")
    if reasons:
        raise ConsumerSyncShadowError(reasons)
    return {**core, "plan_id": expected_plan_id}


def classify_shadow_drift(
    plan: dict[str, Any], *, repository: str, observed_targets: dict[str, str]
) -> dict[str, Any]:
    """Classify proposed effects without reading or writing a consumer repo."""
    if os.environ.get("ORCH_REFERENCE_WORKFLOW_DISABLED") == "1":
        raise ConsumerSyncShadowError(["consumer_sync_shadow_kill_switch_active"])
    normalized = validate_consumer_sync_plan(plan)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ConsumerSyncShadowError(["invalid_repository"])
    if not isinstance(observed_targets, dict) or any(
        _safe_path(path) is None or not isinstance(value, str)
        for path, value in observed_targets.items()
    ):
        raise ConsumerSyncShadowError(["invalid_observed_targets"])
    proposals: list[dict[str, Any]] = []
    for entry in normalized["entries"]:
        target = entry["target"]
        observed = observed_targets.get(target)
        if repository in entry["skip_repos"]:
            action, reason = "skip", "manifest_skip"
        elif observed is None:
            action, reason = "create", "target_missing"
        elif entry["sync_mode"] == "create_only" and repository not in entry["overwrite_repos"]:
            action, reason = "skip", "create_only_existing"
        elif observed == entry["content_sha256"]:
            action, reason = "no_change", "content_matches"
        else:
            action, reason = "update", "content_differs"
        proposal_core = {
            "target": target,
            "action": action,
            "reason": reason,
            "source_effect_fingerprint": entry["effect_fingerprint"],
            "observed_sha256": observed,
            "desired_sha256": entry["content_sha256"],
        }
        proposals.append(
            {
                **proposal_core,
                "effect_fingerprint": _stable_hash("consumer-sync-effect", proposal_core),
            }
        )
    for removal in normalized["removals"]:
        target = removal["target"]
        action = "remove" if target in observed_targets else "no_change"
        proposal_core = {
            "target": target,
            "action": action,
            "reason": "obsolete_target_present" if action == "remove" else "obsolete_target_absent",
            "source_effect_fingerprint": removal["effect_fingerprint"],
            "observed_sha256": observed_targets.get(target),
            "desired_sha256": None,
        }
        proposals.append(
            {
                **proposal_core,
                "effect_fingerprint": _stable_hash("consumer-sync-effect", proposal_core),
            }
        )
    if any(row["action"] not in ALLOWED_ACTIONS for row in proposals):
        raise ConsumerSyncShadowError(["unallowlisted_shadow_action"])
    result_core = {
        "schema": SHADOW_RESULT_SCHEMA,
        "version": 1,
        "capability_id": CAPABILITY_ID,
        "plan_id": normalized["plan_id"],
        "repository": repository.lower(),
        "mode": "shadow_read_only",
        "side_effects_performed": [],
        "proposals": proposals,
    }
    return {
        **result_core,
        "result_id": _stable_hash("consumer-sync-shadow-result", result_core),
    }


def validate_shadow_result(result: Any) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(result, dict):
        raise ConsumerSyncShadowError(["result_not_object"])
    expected_keys = {
        "schema",
        "version",
        "capability_id",
        "plan_id",
        "repository",
        "mode",
        "side_effects_performed",
        "proposals",
        "result_id",
    }
    if set(result) != expected_keys:
        reasons.append("invalid_result_fields")
    if result.get("schema") != SHADOW_RESULT_SCHEMA or result.get("version") != 1:
        reasons.append("unsupported_result_schema")
    if result.get("capability_id") != CAPABILITY_ID:
        reasons.append("result_capability_mismatch")
    if not isinstance(result.get("plan_id"), str) or not SHA256_RE.fullmatch(result["plan_id"]):
        reasons.append("invalid_result_plan_id")
    repo = result.get("repository")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        reasons.append("invalid_result_repository")
    elif repo != repo.lower():
        reasons.append("result_repository_not_lowercase")
    if result.get("mode") != "shadow_read_only":
        reasons.append("invalid_result_mode")
    if result.get("side_effects_performed") != []:
        reasons.append("result_side_effects_not_empty")
    proposals = result.get("proposals")
    if not isinstance(proposals, list):
        reasons.append("proposals_not_array")
    else:
        proposal_keys = {
            "target",
            "action",
            "reason",
            "source_effect_fingerprint",
            "observed_sha256",
            "desired_sha256",
            "effect_fingerprint",
        }
        seen_targets: set[str] = set()
        for index, prop in enumerate(proposals):
            if not isinstance(prop, dict) or set(prop) != proposal_keys:
                reasons.append(f"invalid_proposal_fields:{index}")
                continue
            if prop.get("action") not in ALLOWED_ACTIONS:
                reasons.append(f"unallowlisted_shadow_action:{index}")
            if prop.get("reason") not in ALLOWED_REASONS:
                reasons.append(f"invalid_proposal_reason:{index}")
            target = _safe_path(prop.get("target"))
            if target is None:
                reasons.append(f"unsafe_proposal_target:{index}")
            elif target in seen_targets:
                reasons.append(f"duplicate_proposal_target:{target}")
            else:
                seen_targets.add(target)
            for field in ("source_effect_fingerprint", "effect_fingerprint"):
                val = prop.get(field)
                if not isinstance(val, str) or not SHA256_RE.fullmatch(val):
                    reasons.append(f"invalid_proposal_{field}:{index}")
            if prop.get("observed_sha256") is not None:
                val = prop.get("observed_sha256")
                if not isinstance(val, str) or not SHA256_RE.fullmatch(val):
                    reasons.append(f"invalid_proposal_observed_sha256:{index}")
            if prop.get("desired_sha256") is not None:
                val = prop.get("desired_sha256")
                if not isinstance(val, str) or not SHA256_RE.fullmatch(val):
                    reasons.append(f"invalid_proposal_desired_sha256:{index}")

            # Recalculate effect fingerprint
            proposal_core = {
                "target": prop["target"],
                "action": prop["action"],
                "reason": prop["reason"],
                "source_effect_fingerprint": prop["source_effect_fingerprint"],
                "observed_sha256": prop["observed_sha256"],
                "desired_sha256": prop["desired_sha256"],
            }
            if prop.get("effect_fingerprint") != _stable_hash(
                "consumer-sync-effect", proposal_core
            ):
                reasons.append(f"proposal_effect_identity_mismatch:{index}")

    if reasons:
        raise ConsumerSyncShadowError(reasons)

    # Validate result_id
    result_core = {
        "schema": SHADOW_RESULT_SCHEMA,
        "version": 1,
        "capability_id": CAPABILITY_ID,
        "plan_id": result["plan_id"],
        "repository": repo,
        "mode": "shadow_read_only",
        "side_effects_performed": [],
        "proposals": proposals,
    }
    if result.get("result_id") != _stable_hash("consumer-sync-shadow-result", result_core):
        raise ConsumerSyncShadowError(["result_identity_mismatch"])
    return result


def record_shadow_result(
    result: dict[str, Any],
    *,
    ledger_path: Path = capabilities.REG,
    timestamp: int | None = None,
    supervision_mode: str = "shadow",
    evidence_artifact_ref: str | None = None,
    event_ref: str | None = None,
    phase_id: str | None = None,
) -> dict[str, Any]:
    if supervision_mode not in {"shadow", "human-on-exception"}:
        raise ConsumerSyncShadowError(["invalid_supervision_mode"])
    if phase_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", phase_id):
        raise ConsumerSyncShadowError(["invalid_phase_id"])
    if supervision_mode == "human-on-exception" and not str(phase_id or "").startswith("importer-"):
        raise ConsumerSyncShadowError(["invalid_supervision_mode_without_importer_phase"])

    # Fully validate result shape/proposals/repository/result_id
    validate_shadow_result(result)

    if CAPABILITY_ID not in capabilities.load(ledger_path, create=False):
        capability_compiler.run_reference_workflow(ledger_path=ledger_path)
    fingerprint = _stable_hash(
        "consumer-sync-shadow-effects",
        [row["effect_fingerprint"] for row in result["proposals"]],
    )
    if evidence_artifact_ref is None:
        suffix = result["result_id"].split(":", 1)[1]
        if phase_id:
            evidence_artifact_ref = f"consumer-sync-{supervision_mode}-{phase_id}:{suffix}"
        else:
            evidence_artifact_ref = f"consumer-sync-{supervision_mode}:{suffix}"

    actual_event_ref = event_ref
    if actual_event_ref is None:
        if phase_id:
            actual_event_ref = f"{result['result_id']}:{phase_id}"
        else:
            actual_event_ref = result["result_id"]

    # Validate refs contain phase_id if phase_id is passed
    if phase_id:
        if phase_id not in evidence_artifact_ref:
            raise ConsumerSyncShadowError(["evidence_artifact_ref_missing_phase"])
        if phase_id not in actual_event_ref:
            raise ConsumerSyncShadowError(["event_ref_missing_phase"])

    outputs = {
        "capability-id": CAPABILITY_ID,
        "effect-fingerprint": fingerprint,
        "evidence-artifact-ref": evidence_artifact_ref,
        "supervision-mode": supervision_mode,
        "capability-evidence-status": "accepted",
        "terminal-disposition": "no-change",
    }
    # SUBJECT IDENTITY. The validated `repository` on the result is the consumer repo this rail
    # acted on, and it used to be DROPPED here: the receipt kept only fingerprints and refs, so
    # `_causal_readiness`, which promotes on "distinct durable subjects", had nothing to count and
    # fell back to the PR target — under which three PRs from a single pilot would have posed as
    # three INDEPENDENT durable subjects. Carrying the repo through is what makes the promotion gate
    # honest rather than merely closed. Optional in the effect schema (v2), so a result without a
    # repository still validates instead of silently dropping evidence.
    subject = str(result.get("repository") or "").strip().lower()
    if subject:
        outputs["subject-id"] = subject
    try:
        runner_outputs_to_effect(outputs)
    except ValueError as exc:
        raise ConsumerSyncShadowError([str(exc)]) from exc
    if not RUN_REF_RE.fullmatch(str(actual_event_ref)):
        raise ConsumerSyncShadowError(["invalid_shadow_event_ref"])
    return record_runner_effect(
        outputs,
        event_ref=actual_event_ref,
        ledger_path=ledger_path,
        timestamp=timestamp,
    )


def promotion_dashboard(
    *, ledger_path: Path = capabilities.REG, now: int | None = None
) -> dict[str, Any]:
    current = int(time.time()) if now is None else int(now)
    cap = capabilities.load(ledger_path, create=False).get(CAPABILITY_ID)
    if cap is None:
        return {
            "schema": "orchestrator.consumer-sync-shadow-dashboard/v1",
            "status": "no-data",
            "capability_id": CAPABILITY_ID,
            "promotion_ready": False,
            "promotion_blockers": ["capability_not_registered"],
        }
    events = cap.get("event_history") or []
    outcomes = [
        event
        for event in events
        if event.get("type") == "outcome" and str(event.get("ref") or "").startswith("effect:")
    ]
    harms = [
        event
        for event in events
        if event.get("type") == "failure"
        and str(event.get("ref") or "").startswith("counterexample:")
    ]
    distinct_effects = {event.get("ref") for event in [*outcomes, *harms]}
    supervision_avoided = sum(
        (event.get("metadata") or {}).get("supervision_mode")
        in {"unattended", "human-on-exception"}
        for event in outcomes
    )
    blockers: list[str] = []
    if len(distinct_effects) < 5:
        blockers.append("minimum_distinct_effects_not_met")
    total = len(outcomes) + len(harms)
    if total and len(harms) / total > 0.1:
        blockers.append("harm_ratio_exceeds_limit")
    probes = cap.get("activation_evidence") or {}
    if not (probes.get("rollback_probe") or {}).get("passed"):
        blockers.append("rollback_probe_missing")
    if int(cap.get("expiry") or 0) <= current:
        blockers.append("capability_expired")
    if not cap.get("kill_switch"):
        blockers.append("kill_switch_missing")
    if cap.get("status") != "shadow":
        blockers.append("not_in_shadow_state")
    if supervision_avoided == 0:
        blockers.append("no_reduced_supervision_evidence")
    return {
        "schema": "orchestrator.consumer-sync-shadow-dashboard/v1",
        "status": "healthy-shadow" if distinct_effects else "no-data",
        "capability_id": CAPABILITY_ID,
        "distinct_effect_count": len(distinct_effects),
        "successful_outcome_count": len(outcomes),
        "harm_count": len(harms),
        "supervision_avoided_count": supervision_avoided,
        "promotion_ready": not blockers,
        "promotion_blockers": blockers,
        "counterexamples": [event.get("ref") for event in harms],
        "expires_at": cap.get("expiry"),
        "kill_switch": cap.get("kill_switch"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    classify = subparsers.add_parser("classify")
    classify.add_argument("--plan", type=Path, required=True)
    classify.add_argument("--repository", required=True)
    classify.add_argument("--observed", type=Path, required=True)
    classify.add_argument("--handoff", type=Path)
    classify.add_argument("--record", action="store_true")
    classify.add_argument("--ledger", type=Path, default=capabilities.REG)
    dashboard = subparsers.add_parser("dashboard")
    dashboard.add_argument("--ledger", type=Path, default=capabilities.REG)
    args = parser.parse_args(argv)
    if args.command == "dashboard":
        print(json.dumps(promotion_dashboard(ledger_path=args.ledger), indent=2, sort_keys=True))
        return 0
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    plan = validate_consumer_sync_plan(plan)
    if args.handoff:
        handoff = json.loads(args.handoff.read_text(encoding="utf-8"))
        validate_shadow_handoff(handoff, plan=plan)
    observed = json.loads(args.observed.read_text(encoding="utf-8"))
    result = classify_shadow_drift(plan, repository=args.repository, observed_targets=observed)
    output = {"result": result}
    if args.record:
        output["receipt"] = record_shadow_result(result, ledger_path=args.ledger)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
