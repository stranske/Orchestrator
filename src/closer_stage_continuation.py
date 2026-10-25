"""Causal finite-stage successors in the existing native pipeline frontier store.

This is admission bookkeeping, not an evaluator. Call only after ordinary fresh
observation, current-runtime qualification and target admission. A successor runs
PREPARE normally; neither predecessor results nor ready/PASS state are copied.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_queue as queue


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _progress(receipt: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    # Same queue-progress identity validated by the finite pipeline ACT boundary.
    return hashlib.sha256(
        json.dumps(
            {
                **{
                    k: row.get(k)
                    for k in ("revision", "action", "operation_id", "operation_status")
                },
                "events": receipt.get("events", [])[-8:],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _canonical(path: Path, target: str, expected: Mapping[str, Any]) -> str:
    raw = path.read_bytes()
    document = json.loads(raw)
    rows = document.get("targets")
    if not isinstance(rows, list):
        raise ValueError("current canonical assignment is missing")
    matched = [row for row in rows if isinstance(row, Mapping) and row.get("target") == target]
    if len(matched) != 1 or dict(matched[0]) != dict(expected):
        raise ValueError("current canonical target binding changed")
    return hashlib.sha256(raw).hexdigest()


def _admitted(admission: Callable[[], Mapping[str, Any]]) -> None:
    gates = admission()
    if not isinstance(gates, Mapping) or not (
        gates.get("paused") is False
        and gates.get("dependency") is None
        and "dependency" in gates
        and gates.get("live_owner") is False
    ):
        raise ValueError("stage successor requires ordinary pause/dependency/ownership admission")


def create_successor(
    state_path: str | Path,
    action_state_path: str | Path,
    *,
    predecessor: Mapping[str, Any],
    target: str,
    observation: Mapping[str, Any],
    canonical_path: str | Path,
    canonical_row: Mapping[str, Any],
    runtime_binding: Mapping[str, Any] | None = None,
    admission: Callable[[], Mapping[str, Any]],
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """CAS a causal descriptor/link into the existing pipeline journal metadata.

    No runnable frontier is published here: normal native pipeline initialization
    creates its frontier and first target row atomically after ordinary admission.

    ``runtime_binding`` records the caller's normally qualified CURRENT runtime;
    historical generation, if absent, stays unknown. Upgrades do not invalidate
    an actually completed effect. Caller must execute only ``execution_targets``
    with ``frontier_target=target`` through the ordinary admitted pipeline.
    """
    path, action_path, assignment = Path(state_path), Path(action_state_path), Path(canonical_path)
    old = policy.validate_frontier(predecessor)
    if target not in old["targets"]:
        raise ValueError("stage successor cannot expand its frozen cohort")
    if runtime_binding is not None and not isinstance(runtime_binding, Mapping):
        raise ValueError("current runtime provenance must be an object or unknown")
    if observation.get("target") != target or observation.get("_local_cached"):
        raise ValueError("stage successor requires a fresh native target observation")
    stamp = observation.get("observed_at")
    if (
        isinstance(stamp, bool)
        or not isinstance(stamp, (int, float))
        or not math.isfinite(stamp)
        or stamp > now()
    ):
        raise ValueError("stage successor observation timestamp is invalid")
    # policy validates eligibility, revision, action and exact operation binding.
    fresh = policy.make_frontier_from_observations(
        {target: observation}, owner=old["owner"], frontier_id="candidate"
    )
    _admitted(admission)
    canonical_sha = _canonical(assignment, target, canonical_row)
    lock_path = path.with_name(path.name + ".runlock")
    with lock_path.open("a+") as run_lock:
        try:
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("predecessor pipeline owner is still live") from exc
        with pipeline._locked(path, timeout_seconds=1):
            state = pipeline._load(path)
            entry = state["frontiers"].get(old["frontier_id"])
            if not isinstance(entry, Mapping) or entry.get("finite_frontier") != old:
                raise ValueError("exact immutable predecessor is missing")
            item = (entry.get("items") or {}).get(target)
            accepted_wait = (
                isinstance(item, Mapping)
                and item.get("deferred_reason") == "accepted_verifier_request"
            )
            if not isinstance(item, Mapping) or not (
                item.get("status") == "held"
                and item.get("stage") == "act"
                and item.get("stage_status") == "completed"
                and (accepted_wait or item.get("deferred_next_trigger") == "new_frontier_required")
            ):
                raise ValueError("predecessor has no verified completed stage transition")
            act = (item.get("results") or {}).get("act")
            if (
                not isinstance(act, Mapping)
                or (
                    not accepted_wait
                    and (
                        act.get("status") != "completed" or act.get("target_complete") is not False
                    )
                )
                or (accepted_wait and act.get("status") != "pending")
            ):
                raise ValueError("predecessor action is not completed partial progress")
            receipt = act.get("queue_receipt")
            if not isinstance(receipt, Mapping):
                raise ValueError("exact predecessor queue receipt is missing")
            queue_sha = hashlib.sha256(action_path.read_bytes()).hexdigest()
            checked = queue.validate_stop(
                action_path, [target], receipt=receipt, frontier_id=old["frontier_id"], now=now
            )
            if checked.get("valid") is not True or checked.get("stop_valid") is not True:
                raise ValueError("predecessor queue receipt is not authoritative")
            accepted_proof = None
            if accepted_wait:
                accepted_proof = queue.accepted_verifier_wait_proof(
                    action_path,
                    target,
                    frontier=old,
                    receipt=receipt,
                    evidence=act.get("pending_evidence"),
                )
                resource = item.get("resource_evidence")
                if (
                    accepted_proof is None
                    or not isinstance(resource, Mapping)
                    or resource.get("accepted_verifier_request") != accepted_proof
                ):
                    raise ValueError("accepted predecessor request proof differs")
            # Match the native finite-progress validator's pipeline->queue order.
            # Public validation releases its queue lock; CAS those exact bytes,
            # then retain the queue lock through successor store publication.
            with queue._ledger_lock(action_path, cutoff=float(now()) + 1, now=now) as locked:
                if not locked or hashlib.sha256(action_path.read_bytes()).hexdigest() != queue_sha:
                    raise ValueError("validated predecessor queue changed during stage admission")
                queue_document = queue._read_state(action_path)
                qstate = queue.state_for_frontier(
                    queue_document, old["frontier_id"], expected_policy=old
                )
                qitem = (qstate.get("items") or {}).get(target) if qstate else None
                accounting = receipt.get("accounting")
                rows = (
                    [r for r in accounting if isinstance(r, Mapping) and r.get("target") == target]
                    if isinstance(accounting, list)
                    else []
                )
                if len(rows) != 1 or not isinstance(qitem, Mapping):
                    raise ValueError("predecessor queue accounting is missing")
                row = rows[0]
                operation_policy = old
                operation_id = row.get("operation_id")
                if accepted_proof is not None:
                    # A duplicate request can wait in a later frontier while
                    # retaining the one accepted POST in its original namespace.
                    original = queue.state_for_frontier(
                        queue_document, accepted_proof["operation_frontier"]
                    )
                    if original is None:
                        raise ValueError("accepted request operation namespace is missing")
                    operation_policy = policy.validate_frontier(
                        queue_document["frontiers"][accepted_proof["operation_frontier"]]["policy"]
                    )
                    qitem = original["items"].get(target)
                    if not isinstance(qitem, Mapping):
                        raise ValueError("accepted request operation namespace is missing")
                    operation_id = accepted_proof["operation_id"]
                operations = [qitem.get("operation"), *(qitem.get("operation_history") or [])]
                completed = []
                for op in operations:
                    if not isinstance(op, Mapping) or op.get("id") != operation_id:
                        continue
                    revision, action, binding = (
                        op.get("revision"),
                        op.get("action"),
                        op.get("binding"),
                    )
                    if (
                        not isinstance(revision, str)
                        or not isinstance(action, str)
                        or not isinstance(binding, Mapping)
                    ):
                        raise ValueError("predecessor operation binding is malformed")
                    identity = policy.action_identity(target, revision, action, binding)
                    if (
                        op.get("status") == "completed"
                        and op.get("finite_frontier") == operation_policy
                        and op.get("revision") == operation_policy["bindings"][target]["revision"]
                        and identity == operation_policy["bindings"][target]["action_id"]
                    ):
                        completed.append(op)
                progress = _progress(receipt, row)
                if not (
                    len(completed) == 1
                    and (accepted_proof is not None or row.get("operation_status") == "completed")
                    and receipt.get("stop_valid") is True
                    and (
                        accepted_proof is not None
                        or (
                            row.get("eligible") is True
                            and (
                                row.get("status") in {"completed", "observed"}
                                or (
                                    row.get("status") == "deferred"
                                    and row.get("reason") == "pass_limit"
                                )
                            )
                            and progress == item.get("progress_key") == act.get("progress_key")
                        )
                    )
                ):
                    raise ValueError("predecessor completed operation/progress identity differs")
                op = completed[0]
                result = op.get("result") or {}
                if op.get("outcome_uncertain") or (
                    isinstance(result, Mapping)
                    and any(
                        result.get(k) is True
                        for k in ("outcome_uncertain", "no_effect", "advisory_only")
                    )
                ):
                    raise ValueError(
                        "uncertain or no-effect operation cannot create a stage successor"
                    )
                completed_at = op.get("completed_at")
                if (
                    isinstance(completed_at, bool)
                    or not isinstance(completed_at, (int, float))
                    or not math.isfinite(completed_at)
                    or stamp < completed_at
                ):
                    raise ValueError(
                        "fresh observation must follow completed predecessor operation"
                    )
                if (
                    observation.get("action") == op.get("action")
                    or fresh["bindings"][target]["action_id"]
                    == old["bindings"][target]["action_id"]
                ):
                    raise ValueError("unchanged action cannot create another stage")
                causal = {
                    "predecessor_frontier_id": old["frontier_id"],
                    "operation_id": op["id"],
                    "progress_key": progress,
                    "target": target,
                    "fresh_binding": fresh["bindings"][target],
                }
                if accepted_proof is not None:
                    causal["accepted_request"] = dict(accepted_proof)
                frontier_id = "stage-successor-" + _digest(causal)
                bindings = {target: fresh["bindings"][target]}
                successor = policy.make_frontier(
                    [target], owner=old["owner"], frontier_id=frontier_id, bindings=bindings
                )
                lineage = {
                    **causal,
                    "predecessor_row_sha256": _digest(item),
                    "predecessor_frontier_sha256": _digest(old),
                    "frozen_cohort": {
                        "targets": old["targets"],
                        "targets_sha256": old["targets_sha256"],
                        "owner": old["owner"],
                        "bindings_sha256": old["bindings_sha256"],
                    },
                    "predecessor_generation": entry.get("execution_generation"),
                    "current_runtime_binding": (
                        dict(runtime_binding) if runtime_binding is not None else None
                    ),
                    "canonical_row": dict(canonical_row),
                    "observation_sha256": _digest(observation),
                }
                successors = state.setdefault("stage_successors", {})
                if not isinstance(successors, dict):
                    raise ValueError("native stage successor metadata is malformed")
                links = []
                for candidate in successors.values():
                    link = (
                        candidate.get("stage_predecessor")
                        if isinstance(candidate, Mapping)
                        else None
                    )
                    if (
                        isinstance(link, Mapping)
                        and link.get("predecessor_frontier_id") == old["frontier_id"]
                        and link.get("target") == target
                    ):
                        links.append(candidate)
                if len(links) > 1:
                    raise ValueError("multiple stage successors already claim this predecessor")
                existing = links[0] if links else successors.get(frontier_id)
                if existing is not None:
                    if (
                        not isinstance(existing, Mapping)
                        or existing.get("schema") != "closer-stage-successor/v1"
                        or not isinstance(existing.get("stage_predecessor"), Mapping)
                    ):
                        raise ValueError("recorded stage successor metadata is malformed")
                    link = existing["stage_predecessor"]
                    recorded = {key: link.get(key) for key in causal}
                    if (
                        any(
                            recorded[key] != causal[key] for key in causal if key != "fresh_binding"
                        )
                        or link.get("predecessor_row_sha256") != lineage["predecessor_row_sha256"]
                        or link.get("predecessor_frontier_sha256")
                        != lineage["predecessor_frontier_sha256"]
                        or link.get("frozen_cohort") != lineage["frozen_cohort"]
                    ):
                        raise ValueError("deterministic stage successor identity conflicts")
                    frozen_bindings = {target: recorded["fresh_binding"]}
                    expected = policy.make_frontier(
                        [target],
                        owner=old["owner"],
                        frontier_id="stage-successor-" + _digest(recorded),
                        bindings=frozen_bindings,
                    )
                    if existing.get("finite_frontier") != expected:
                        raise ValueError("recorded stage successor descriptor differs")
                    # First causal successor wins even after a new projection or
                    # request arrives. Normal PREPARE judges its frozen binding;
                    # never allocate a second namespace to evade its state.
                    successor = expected
                _admitted(admission)
                if _canonical(assignment, target, canonical_row) != canonical_sha:
                    raise ValueError("canonical assignment changed during stage admission")
                if existing is None:
                    # Metadata in the existing journal is not a runnable frontier.
                    # Only normal pipeline initialization may publish a frontier
                    # and its first target row atomically (census requires it).
                    if frontier_id in state["frontiers"]:
                        raise ValueError("unlinked successor frontier already exists")
                    successors[frontier_id] = {
                        "schema": "closer-stage-successor/v1",
                        "finite_frontier": successor,
                        "created_at": now(),
                        "stage_predecessor": lineage,
                    }
                    pipeline._save(path, state)
                return {
                    "finite_frontier": successor,
                    "execution_targets": [target],
                    "frontier_target": target,
                    "predecessor": copy.deepcopy(
                        existing.get("stage_predecessor") if existing else lineage
                    ),
                    "reused": existing is not None,
                    "fresh_binding_drift": successor["bindings"][target]
                    != fresh["bindings"][target],
                    "acceptance_authority": False,
                }
