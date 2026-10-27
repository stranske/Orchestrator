"""Join an accepted correlated request to an authenticated terminal observation.

Correlation is inert: it never changes semantic event identity, verdict, retry
permission, or readiness. Callers supply their ordinary fresh native observation;
normal successor admission still performs fresh PREPARE and guarded ACT.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard

import closer_queue as queue
import verification_events
import verifier_evidence
from closer_frontier_policy import action_identity, validate_frontier

SCHEMA = "closer-verifier-terminal-followup/v1"


def _number(value: Any) -> TypeGuard[int | float]:
    return type(value) in {int, float} and math.isfinite(value)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def native_binding_digest(value):
    # Existing Core native-binding protocol uses sorted JSON DEFAULT separators,
    # distinct from compact semantic event/action digests.
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_requirements(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Original authored requirements; no comment/update timestamps or verdicts."""
    sources = []
    for source in observation.get("sources") or []:
        if not isinstance(source, Mapping):
            raise ValueError("malformed original source requirements")
        sources.append(
            {key: source.get(key) for key in ("target", "kind", "title", "body", "state")}
        )
    return {
        "schema": "closer-verification-source-requirements/v1",
        **{
            key: observation.get(key)
            for key in (
                "target",
                "pr_target",
                "kind",
                "state",
                "head",
                "base",
                "merge",
                "body",
                "title",
                "source_inventory",
            )
        },
        "pr_target": observation.get("pr_target", observation.get("target")),
        "sources": sorted(sources, key=_digest),
    }


def substantive_witness(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Follow-up facts only; normal frontier revision/ACT guards stay unchanged."""
    facts = {
        key: observation.get(key)
        for key in (
            "target",
            "pr_target",
            "kind",
            "state",
            "head",
            "head_branch",
            "head_repository",
            "base",
            "merge",
            "merged_at",
            "body",
            "title",
            "draft",
            "source_inventory",
            "criteria",
            "active_threads",
            "checks",
            "pending_check_run_ids",
            "pending_check_head",
            "mergeable",
            "action",
        )
    }
    facts["labels"] = sorted(observation.get("labels") or [])
    sources = []
    for source in observation.get("sources") or []:
        if not isinstance(source, Mapping):
            raise ValueError("malformed substantive source witness")
        row = {key: source.get(key) for key in ("target", "kind", "title", "body", "state")}
        row["labels"] = sorted(source.get("labels") or [])
        provenance = source.get("provenance") or {}
        row["provenance"] = {
            key: provenance.get(key) for key in ("kind", "via", "url", "body_sha256")
        }
        sources.append(row)
    facts["sources"] = sorted(sources, key=_digest)
    facts["threads"] = sorted(
        [
            {
                "id": thread.get("id"),
                "isResolved": thread.get("isResolved"),
                "isOutdated": thread.get("isOutdated"),
                "comments": [
                    comment.get("body")
                    for comment in (thread.get("comments") or {}).get("nodes", [])
                ],
            }
            for thread in observation.get("threads") or []
        ],
        key=_digest,
    )
    binding = observation.get("operation_binding") or {}
    facts["routing"] = {
        key: binding.get(key) for key in ("pr_target", "source_targets", "disposition_digest")
    }
    return facts


def terminal_proof(
    state_path: str | Path,
    target: str,
    accepted: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Authenticate the ORIGINAL sent input and CURRENT normal terminal result.

    No request is submitted here. Missing old correlation is not reconstructed
    from head, timestamps, request-content hashes, or unrelated terminal results.
    """
    current = time.time() if now is None else now
    try:
        stamp = observation.get("observed_at")
        if (
            observation.get("target") != target
            or observation.get("_local_cached")
            or observation.get("state") != "MERGED"
            or observation.get("eligible") is not True
            or observation.get("action") in {None, "post_merge_verify", "initiate_verify", "wait"}
            or not _number(stamp)
            or not 0 <= current - stamp <= 120
        ):
            return None
        basis = accepted["event_id"]
        if not isinstance(basis, str) or not verification_events.HEX256.fullmatch(basis):
            return None
        path = Path(state_path)
        if path.resolve() != path or path.is_symlink():
            return None
        document = queue._read_state(path)
        namespace = accepted["operation_frontier"]
        bucket = document["frontiers"][namespace]
        frontier = validate_frontier(bucket["policy"])
        operation = bucket["items"][target]["operation"]
        binding, result = operation["binding"], operation["result"]
        acknowledgment = result.get("verification_post_accepted") or {}
        if not (
            operation.get("id") == accepted["operation_id"]
            and operation.get("status") == "completed"
            and operation.get("action") == "post_merge_verify"
            and operation.get("finite_frontier") == frontier
            and operation.get("revision") == frontier["bindings"][target]["revision"]
            and action_identity(target, operation["revision"], operation["action"], binding)
            == frontier["bindings"][target]["action_id"]
            and result.get("verification_request_basis") == basis
            and acknowledgment.get("mutation_kind") == "workflow_dispatch"
            and acknowledgment.get("request_basis") == basis
            # Added only after the actual POST carrying this field succeeds.
            and acknowledgment.get("correlation_input") == basis
            and all(
                binding.get(key) == accepted.get(key) == observation.get(key)
                for key in ("head", "merge")
            )
            and all(
                record.get(key) in (None, False)
                for record in (operation, result)
                for key in ("outcome_uncertain", "no_effect", "advisory_only")
            )
        ):
            return None
        core = result.get("core_request_correlation")
        native = core.get("native_binding") if isinstance(core, Mapping) else None
        if not (
            isinstance(core, Mapping)
            and core.get("schema") == "closer-core-request-correlation/v1"
            and core.get("operation_id") == operation["id"]
            and isinstance(core.get("core_operation_key"), str)
            and core["core_operation_key"]
            and isinstance(core.get("core_target_fence_key"), str)
            and core["core_target_fence_key"]
            and isinstance(core.get("core_package_digest"), str)
            and verification_events.HEX256.fullmatch(core["core_package_digest"])
            and isinstance(native, Mapping)
            and native_binding_digest(native) == core.get("native_binding_sha256")
            and native.get("queue_operation_id") == operation["id"]
            and native.get("pr_target") == binding.get("pr_target", target)
            and all(native.get(key) == binding.get(key) for key in ("head", "merge"))
            and native.get("verification_mutation") == acknowledgment
            and native.get("verification_source_requirements") == source_requirements(observation)
        ):
            return None
        marker = observation.get("verifier")
        subject = binding.get("pr_target", target)
        repo, number = subject.split("#")
        if not (
            isinstance(marker, dict)
            # This producer requests compare; event identity alone omits mode.
            and isinstance(marker.get("assessment_identity"), Mapping)
            and marker["assessment_identity"].get("canonical") is True
            and marker["assessment_identity"].get("mode") == "compare"
            and all(
                isinstance(marker["assessment_identity"].get(key), str)
                for key in ("provider", "model", "model2")
            )
            and marker.get("_request_correlation_unusable") is not True
            and verifier_evidence.decision_matches_identity(
                marker, repo, int(number), accepted["head"], accepted["merge"]
            )
            and verification_events.valid_event(
                marker.get("verification_event"),
                repo,
                int(number),
                accepted["head"],
                accepted["merge"],
            )
        ):
            return None
        correlation: dict[str, Any] = {"kind": "terminal_basis", "request_basis": basis}
        if marker.get("verification_request_basis") != basis:
            aliases = marker.get("_request_correlations") or []
            matching = [
                alias
                for alias in aliases
                if isinstance(alias, Mapping)
                and isinstance(alias.get("record"), Mapping)
                and alias["record"].get("verification_request_basis") == basis
                and alias["record"].get("verification_event") == marker["verification_event"]
                and str(alias["record"].get("result_run_id")) == str(marker["run_id"])
                and str(alias["record"].get("result_run_attempt")) == str(marker["run_attempt"])
                and alias.get("sha256") == verification_events.content_hash(alias["record"])
            ]
            if not matching:
                return None
            correlation = {"kind": "authenticated_alias", **dict(matching[0])}
        # Require the normal current lifecycle selection to identify this result;
        # a stale terminal beside a newer pending/conflicting event cannot release.
        lifecycle = observation.get("verifier_lifecycle")
        if not isinstance(lifecycle, Mapping) or any(
            lifecycle.get(key) != marker.get(key)
            for key in ("schema", "verification_event", "run_id", "run_attempt", "verdict")
        ):
            return None
        return {
            "schema": SCHEMA,
            "target": target,
            "request_basis": basis,
            "operation_id": operation["id"],
            "operation_frontier": namespace,
            "operation_revision": operation["revision"],
            "operation_binding_sha256": _digest(binding),
            "head": accepted["head"],
            "merge": accepted["merge"],
            "verification_event": dict(marker["verification_event"]),
            "run_id": str(marker["run_id"]),
            "run_attempt": str(marker["run_attempt"]),
            "terminal_marker_sha256": _digest(
                {
                    key: value
                    for key, value in marker.items()
                    if key
                    not in {
                        "_lifecycle_history",
                        "_request_correlations",
                        "_request_correlation_unusable",
                    }
                }
            ),
            "substantive_witness": substantive_witness(observation),
            "correlation": correlation,
            "authority": False,
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
