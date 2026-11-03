"""Versioned policy helpers for finite Closer work frontiers.

This policy removes only the aggregate elapsed-work cutoff for a *new*, frozen
target set.  It does not remove per-request timeouts, provider capacity checks,
concurrency bounds, cancellation, lease ownership, or UNKNOWN handling.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

POLICY = "finite-frontier/v2"
MAX_REQUEST_TIMEOUT_SECONDS = 120.0
TERMINAL_STATES = frozenset({"completed", "held", "paused", "cancelled", "capacity_blocked"})
FRONTIER_STATUSES = frozenset({"pending", *TERMINAL_STATES})


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def action_identity(
    target: str, revision: str, action: str, operation_binding: Mapping[str, Any]
) -> str:
    """Fingerprint the exact queue action and its mutation binding for one revision."""
    if (
        not isinstance(target, str)
        or not target
        or not isinstance(revision, str)
        or not revision
        or not isinstance(action, str)
        or not action
        or not isinstance(operation_binding, Mapping)
    ):
        raise ValueError("finite action identity needs target, revision, action, and binding")
    return _digest(
        {
            "target": target,
            "revision": revision,
            "action": action,
            "operation_binding": operation_binding,
        }
    )


def immutable_targets(targets: Any) -> tuple[str, ...]:
    """Capture one finite target set; never infer or append a later assignment."""
    if (
        not isinstance(targets, (list, tuple))
        or not targets
        or any(
            not isinstance(target, str) or not target or target != target.strip()
            for target in targets
        )
    ):
        raise ValueError("finite frontier requires explicit nonempty target identities")
    if len(set(targets)) != len(targets):
        raise ValueError("finite frontier cannot repeat target identities")
    import closer_resources

    canonical = [closer_resources.canonical_target(target) for target in targets]
    if len(set(canonical)) != len(canonical):
        raise ValueError("finite frontier cannot repeat canonical target identities")
    return tuple(targets)


def _validated_bindings(targets: Sequence[str], bindings: Any) -> dict[str, dict[str, str]]:
    if not isinstance(bindings, Mapping) or set(bindings) != set(targets):
        raise ValueError("finite frontier must bind every selected target exactly once")
    result: dict[str, dict[str, str]] = {}
    for target in targets:
        binding = bindings[target]
        if not isinstance(binding, Mapping):
            raise ValueError(f"finite frontier binding missing for {target!r}")
        revision = binding.get("revision")
        action_id = binding.get("action_id")
        if (
            not isinstance(revision, str)
            or not revision
            or revision != revision.strip()
            or not isinstance(action_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", action_id)
        ):
            raise ValueError(f"finite frontier revision/action identity invalid for {target!r}")
        result[target] = {"revision": revision, "action_id": action_id}
    return result


def make_frontier(
    targets: Sequence[str], *, owner: str, frontier_id: str, bindings: Mapping[str, Any]
) -> dict[str, Any]:
    """Create a canonical immutable frontier descriptor for a new campaign."""
    selected = immutable_targets(targets)
    if not isinstance(owner, str) or not owner or owner != owner.strip():
        raise ValueError("finite frontier owner is required")
    if not isinstance(frontier_id, str) or not frontier_id or frontier_id != frontier_id.strip():
        raise ValueError("finite frontier id is required")
    target_list = list(selected)
    frozen_bindings = _validated_bindings(selected, bindings)
    return {
        "schema": POLICY,
        "frontier_id": frontier_id,
        "owner": owner,
        "targets": target_list,
        "targets_sha256": _digest(target_list),
        "bindings": frozen_bindings,
        "bindings_sha256": _digest(frozen_bindings),
    }


def make_frontier_from_observations(
    observations: Mapping[str, Mapping[str, Any]], *, owner: str, frontier_id: str
) -> dict[str, Any]:
    """Freeze an existing queue observation set, including its exact action binding.

    Callers must supply the already-produced normalized source observations; this
    function performs no provider calls and never adds targets after freezing.
    """
    if not isinstance(observations, Mapping) or not observations:
        raise ValueError("finite frontier needs an explicit source-observation mapping")
    targets = list(observations)
    bindings: dict[str, dict[str, str]] = {}
    for target in targets:
        observed = observations[target]
        if not isinstance(observed, Mapping) or observed.get("target", target) != target:
            raise ValueError("finite frontier source observation target differs")
        action = observed.get("action")
        if (
            not isinstance(action, str)
            or not action
            or action in {"complete", "wait"}
            or observed.get("eligible") is not True
        ):
            raise ValueError("finite frontier source observation is not actionable")
        revision = observed.get("revision")
        if not isinstance(revision, str) or not revision or revision != revision.strip():
            raise ValueError("finite frontier source observation has no exact revision")
        operation_binding = observed.get("operation_binding")
        if not isinstance(operation_binding, Mapping):
            raise ValueError("finite frontier source observation has no operation binding")
        bindings[target] = {
            "revision": revision,
            "action_id": action_identity(target, revision, action, operation_binding),
        }
    return make_frontier(targets, owner=owner, frontier_id=frontier_id, bindings=bindings)


def validate_frontier(value: Any, *, targets: Sequence[str] | None = None) -> dict[str, Any]:
    """Validate and copy a descriptor; reject target-set drift on every resume."""
    if not isinstance(value, Mapping) or value.get("schema") != POLICY:
        raise ValueError("finite frontier policy/version missing")
    selected = immutable_targets(value.get("targets"))
    expected_hash = _digest(list(selected))
    if value.get("targets_sha256") != expected_hash:
        raise ValueError("finite frontier target digest differs")
    bindings = _validated_bindings(selected, value.get("bindings"))
    if value.get("bindings_sha256") != _digest(bindings):
        raise ValueError("finite frontier revision/action binding digest differs")
    if (
        not isinstance(value.get("owner"), str)
        or not value["owner"]
        or value["owner"] != value["owner"].strip()
    ):
        raise ValueError("finite frontier owner missing")
    if (
        not isinstance(value.get("frontier_id"), str)
        or not value["frontier_id"]
        or value["frontier_id"] != value["frontier_id"].strip()
    ):
        raise ValueError("finite frontier id missing")
    if targets is not None and tuple(immutable_targets(targets)) != selected:
        raise ValueError("finite frontier target set changed")
    return {
        "schema": POLICY,
        "frontier_id": value["frontier_id"],
        "owner": value["owner"],
        "targets": list(selected),
        "targets_sha256": expected_hash,
        "bindings": bindings,
        "bindings_sha256": _digest(bindings),
    }


def validate_item_states(frontier: Mapping[str, Any], states: Mapping[str, str]) -> dict[str, str]:
    """Validate a durable status map without allowing unselected target entries."""
    checked = validate_frontier(frontier)
    if not isinstance(states, Mapping) or set(states) != set(checked["targets"]):
        raise ValueError("finite frontier state must cover exactly its immutable targets")
    result: dict[str, str] = {}
    for target in checked["targets"]:
        status = states[target]
        if status not in FRONTIER_STATUSES:
            raise ValueError(f"unsupported finite frontier state for {target!r}")
        result[target] = status
    return result


def request_window(started_at: Any, timeout_seconds: Any) -> dict[str, float]:
    """Build a finite timeout for one request; never use it as a round deadline."""
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(started_at)
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > MAX_REQUEST_TIMEOUT_SECONDS
    ):
        raise ValueError("request window needs a finite timeout in (0, 120] seconds")
    deadline = float(started_at + timeout_seconds)
    if not math.isfinite(deadline):
        raise ValueError("request deadline must be finite")
    return {"started_at": float(started_at), "deadline": deadline}


def request_is_live(window: Mapping[str, Any], now: float) -> bool:
    """Return whether this exact request's original window remains live."""
    deadline = window.get("deadline") if isinstance(window, Mapping) else None
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(now)
    ):
        return False
    return float(now) < float(deadline)


def can_start_new_request(status: str, *, cancelled: bool, paused: bool, capacity_ok: bool) -> bool:
    """Apply stop gates before a new request; never reinterpret UNKNOWN as pending."""
    if cancelled or paused or not capacity_ok or status != "pending":
        return False
    return True
