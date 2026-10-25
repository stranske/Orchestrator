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
                import closer_verifier_followup

                terminal = closer_verifier_followup.terminal_proof(
                    action_path, target, accepted_proof, observation, now=now()
                )
                if terminal is None:
                    raise ValueError("accepted predecessor lacks current correlated terminal proof")
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
                    causal["accepted_terminal"] = terminal
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


def retain_receipt(state_path, root_path, *, target, frontier, native):
    """Journal an exact produced receipt reference; this grants no native authority."""
    import closer_resources as resources

    descriptor = policy.validate_frontier(frontier, targets=[target])
    round_id = native.get("round_id")
    receipt_path = native.get("receipt_path")
    if receipt_path is None:
        # Unit callers may omit external receipts. Native authentication never
        # accepts this absence as descendant coverage.
        return None
    expected = resources.scoped_root(Path(root_path)) / "rounds" / f"{round_id}.json"
    if Path(receipt_path) != expected or not isinstance(round_id, str):
        raise ValueError("stage receipt path is not its exact owned round")
    receipt, sha = resources.read_round_receipt(expected)
    if not (
        receipt.get("round_id") == round_id
        and receipt.get("finite_frontier") == descriptor
        and receipt.get("assignment") == [target]
        and set(receipt.get("run_ids") or {}) == {target}
        and native.get("receipt_sha256") == sha
    ):
        raise ValueError("stage receipt exact target/frontier binding changed")
    ref = {
        "target": target,
        "receipt_path": str(expected),
        "receipt_sha256": sha,
        "round_id": round_id,
        "run_ids": copy.deepcopy(receipt["run_ids"]),
        "finite_frontier": descriptor,
    }
    path = Path(state_path)
    with pipeline._locked(path, timeout_seconds=1):
        state = pipeline._load(path)
        record = (state.get("stage_successors") or {}).get(descriptor["frontier_id"])
        if not isinstance(record, dict) or record.get("finite_frontier") != descriptor:
            raise ValueError("stage receipt has no exact journaled child")
        refs = record.setdefault("receipt_refs", [])
        if not isinstance(refs, list) or len(refs) >= 32:
            raise ValueError("stage receipt retention bound exceeded")
        prior = [value for value in refs if value.get("round_id") == round_id]
        if prior and prior != [ref]:
            raise ValueError("stage round receipt identity conflicts")
        if not prior:
            refs.append(ref)
            pipeline._save(path, state)
    return ref


def linked_chain(state, root, target, *, max_depth=8):
    """Bounded immutable ancestry only; this is never readiness/effect authority."""
    original = policy.validate_frontier(root)
    current = original
    seen = {current["frontier_id"]}
    chain = []
    for _ in range(max_depth):
        entry = (state.get("frontiers") or {}).get(current["frontier_id"])
        if not isinstance(entry, Mapping) or entry.get("finite_frontier") != current:
            if chain:
                return chain, current, None
            raise ValueError("stage root frontier is missing or changed")
        row = (entry.get("items") or {}).get(target)
        if not isinstance(row, Mapping):
            raise ValueError("stage root target is missing")
        if (
            row.get("status") == "unknown"
            or row.get("stage_status") in {"running", "unknown"}
            or row.get("provider_outcome_uncertain") not in (None, False)
            or row.get("recovery_barrier") is not None
        ):
            return chain, current, entry
        matches = [
            value
            for value in (state.get("stage_successors") or {}).values()
            if isinstance(value, Mapping)
            and (value.get("stage_predecessor") or {}).get("predecessor_frontier_id")
            == current["frontier_id"]
            and (value.get("stage_predecessor") or {}).get("target") == target
        ]
        if not matches:
            return chain, current, entry
        if len(matches) != 1:
            raise ValueError("stage predecessor has conflicting child owners")
        record = matches[0]
        lineage = record.get("stage_predecessor") or {}
        child = policy.validate_frontier(record["finite_frontier"])
        frozen = {
            key: current[key] for key in ("targets", "targets_sha256", "owner", "bindings_sha256")
        }
        if not (
            record.get("schema") == "closer-stage-successor/v1"
            and child["targets"] == [target]
            and target in original["targets"]
            and child["owner"] == original["owner"]
            and lineage.get("predecessor_frontier_sha256") == _digest(current)
            and lineage.get("predecessor_row_sha256") == _digest(row)
            and lineage.get("frozen_cohort") == frozen
        ):
            raise ValueError("stage lineage original cohort or source identity changed")
        keys = (
            (
                "kind",
                "predecessor_frontier_id",
                "check_run_id",
                "pending_head",
                "target",
                "original_wait",
                "fresh_binding",
            )
            if lineage.get("kind") == "ci_no_effect_wait"
            else (
                "predecessor_frontier_id",
                "operation_id",
                "progress_key",
                "target",
                "fresh_binding",
            )
        )
        causal = {key: lineage[key] for key in keys}
        if lineage.get("kind") != "ci_no_effect_wait" and "accepted_request" in lineage:
            causal["accepted_request"] = lineage["accepted_request"]
            causal["accepted_terminal"] = lineage["accepted_terminal"]
        expected = policy.make_frontier(
            [target],
            owner=original["owner"],
            frontier_id="stage-successor-" + _digest(causal),
            bindings={target: causal["fresh_binding"]},
        )
        if child != expected or child["frontier_id"] in seen:
            raise ValueError("stage child descriptor changed or ancestry cycles")
        chain.append({"record": record, "parent": current, "item": row})
        seen.add(child["frontier_id"])
        current = child
    raise ValueError("stage ancestry exceeds bounded traversal")


def active_leaf(state_path, action_path, root, target):
    """Select only exact already-published descendants after real source readback."""
    import closer_ci_followup as ci

    state = pipeline._load(state_path)
    chain, current, entry = linked_chain(state, root, target)
    for hop in chain:
        lineage, old, row = hop["record"]["stage_predecessor"], hop["parent"], hop["item"]
        if lineage.get("kind") == "ci_no_effect_wait":
            wait = ci.original_wait_proof(state_path, action_path, target, old, row)
            terminal = lineage.get("ci_terminal") or {}
            check = terminal.get("terminal_check") or {}
            original = ci.source_snapshot(row)
            if not (
                terminal.get("schema") == ci.SCHEMA
                and terminal.get("original_wait") == wait
                and check.get("id") == wait["check_run_id"]
                and check.get("head_sha") == wait["head"]
                and check.get("status") == "completed"
                and check.get("conclusion") == "success"
                and terminal.get("source_requirements")
                == ci.verifier_followup.source_requirements(original)
            ):
                raise ValueError("stage CI ancestry lacks exact retained no-effect terminal proof")
            continue
        action = row["results"]["act"]
        checked = queue.validate_stop(
            action_path, [target], receipt=action["queue_receipt"], frontier_id=old["frontier_id"]
        )
        if not (checked.get("valid") is True and checked.get("stop_valid") is True):
            raise ValueError("stage ancestry queue receipt is not authoritative")
        document = queue._read_state(action_path)
        accepted = lineage.get("accepted_request")
        if accepted is not None:
            actual = queue.accepted_verifier_wait_proof(
                action_path,
                target,
                frontier=old,
                receipt=action["queue_receipt"],
                evidence=action.get("pending_evidence"),
            )
            if actual != accepted or (lineage.get("accepted_terminal") or {}).get(
                "request_basis"
            ) != accepted.get("event_id"):
                raise ValueError("stage accepted-request ancestry changed")
        namespace = accepted["operation_frontier"] if accepted is not None else old["frontier_id"]
        bucket = document["frontiers"][namespace]
        op_policy = policy.validate_frontier(bucket["policy"])
        item = bucket["items"][target]
        operations = [item.get("operation"), *(item.get("operation_history") or [])]
        matched = [
            op
            for op in operations
            if isinstance(op, Mapping) and op.get("id") == lineage["operation_id"]
        ]
        if len(matched) != 1:
            raise ValueError("stage original operation identity changed")
        operation = matched[0]
        binding, result = operation.get("binding"), operation.get("result") or {}
        if not (
            operation.get("status") == "completed"
            and operation.get("action") in {"merge", "post_merge_verify"}
            and isinstance(binding, Mapping)
            and operation.get("finite_frontier") == op_policy
            and operation.get("revision") == op_policy["bindings"][target]["revision"]
            and policy.action_identity(target, operation["revision"], operation["action"], binding)
            == op_policy["bindings"][target]["action_id"]
            and all(
                record.get(key) is None or record.get(key) is False
                for record in (operation, result)
                for key in ("outcome_uncertain", "no_effect", "advisory_only")
            )
        ):
            raise ValueError("stage ancestry effect is not conclusively completed")
    return {
        "finite_frontier": current,
        "entry": entry,
        "chain": chain,
        "pending_child": chain[-1]["record"] if chain and entry is None else None,
    }


def create_ci_successor(
    state_path,
    action_state_path,
    *,
    predecessor,
    target,
    terminal_read,
    canonical_path,
    canonical_row,
    admission,
    now=time.time,
):
    """Publish one CI no-effect successor in the existing stage journal only."""
    import closer_ci_followup as ci
    import closer_evidence
    import closer_finite_reentry as reentry

    path, action_path, assignment = (
        reentry._physical(state_path),
        reentry._physical(action_state_path),
        reentry._physical(canonical_path),
    )
    old = policy.validate_frontier(predecessor)
    if target not in old["targets"] or path.name != "state.json":
        raise ValueError("CI successor cannot expand its original cohort")
    observation = terminal_read.get("observation")
    if terminal_read.get("status") != "ready" or not isinstance(observation, Mapping):
        raise ValueError("CI successor requires actual terminal read")
    stamp = observation.get("observed_at")
    if type(stamp) not in {int, float} or not math.isfinite(stamp) or not 0 <= now() - stamp <= 120:
        raise ValueError("CI successor observation is stale")
    fresh = policy.make_frontier_from_observations(
        {target: observation}, owner=old["owner"], frontier_id="candidate"
    )
    _admitted(admission)
    canonical_sha = _canonical(assignment, target, canonical_row)
    with path.with_name(path.name + ".runlock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("CI predecessor pipeline owner is still live") from exc
        with pipeline._locked(path, timeout_seconds=1):
            state = pipeline._load(path)
            entry = state["frontiers"].get(old["frontier_id"])
            if not isinstance(entry, Mapping) or entry.get("finite_frontier") != old:
                raise ValueError("CI immutable predecessor changed")
            row = entry["items"][target]
            ci.pending_evidence(row)
            with queue._ledger_lock(action_path, cutoff=now() + 1, now=now) as locked:
                if not locked:
                    raise ValueError("CI original queue remains owned")
                wait = ci.original_wait_proof(path, action_path, target, old, row)
                proof = ci.terminal_proof(
                    action_path,
                    target,
                    old,
                    row,
                    observation,
                    terminal_read["proof"]["terminal_check"],
                    now=now(),
                    wait_proof=wait,
                )
                if proof != terminal_read.get("proof"):
                    raise ValueError("CI terminal read changed before publication")
                links = [
                    value
                    for value in state.setdefault("stage_successors", {}).values()
                    if isinstance(value, Mapping)
                    and (value.get("stage_predecessor") or {}).get("predecessor_frontier_id")
                    == old["frontier_id"]
                    and (value.get("stage_predecessor") or {}).get("target") == target
                ]
                if len(links) > 1:
                    raise ValueError("CI predecessor has multiple linked successors")
                existing = links[0] if links else None
                causal = {
                    "kind": "ci_no_effect_wait",
                    "predecessor_frontier_id": old["frontier_id"],
                    "check_run_id": wait["check_run_id"],
                    "pending_head": wait["head"],
                    "target": target,
                    "original_wait": wait,
                    "fresh_binding": fresh["bindings"][target],
                }
                child = policy.make_frontier(
                    [target],
                    owner=old["owner"],
                    frontier_id="stage-successor-" + _digest(causal),
                    bindings={target: causal["fresh_binding"]},
                )
                if existing is not None:
                    lineage = existing["stage_predecessor"]
                    if any(
                        lineage.get(key) != value
                        for key, value in causal.items()
                        if key != "fresh_binding"
                    ):
                        raise ValueError("CI existing successor identity conflicts")
                    recorded = {key: lineage[key] for key in causal}
                    expected = policy.make_frontier(
                        [target],
                        owner=old["owner"],
                        frontier_id="stage-successor-" + _digest(recorded),
                        bindings={target: recorded["fresh_binding"]},
                    )
                    if existing.get("finite_frontier") != expected:
                        raise ValueError("CI recorded successor descriptor changed")
                    child = expected
                _admitted(admission)
                if _canonical(assignment, target, canonical_row) != canonical_sha:
                    raise ValueError("CI canonical assignment changed before publication")
                if existing is None:
                    raw = action_path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != wait["queue_sha256"]:
                        raise ValueError("CI no-effect queue changed before retention")
                    archive = closer_evidence._retain_bytes(
                        path.parent.parent.parent / "stage-successor-proofs", raw, "json"
                    )
                    if child["frontier_id"] in state["frontiers"]:
                        raise ValueError("CI unlinked runnable child already exists")
                    lineage = {
                        **causal,
                        "ci_terminal": proof,
                        "original_queue": {key: archive[key] for key in ("path", "sha256")},
                        "predecessor_row_sha256": pipeline._digest(row),
                        "predecessor_frontier_sha256": pipeline._digest(old),
                        "frozen_cohort": {
                            key: old[key]
                            for key in ("targets", "targets_sha256", "owner", "bindings_sha256")
                        },
                    }
                    state["stage_successors"][child["frontier_id"]] = {
                        "schema": "closer-stage-successor/v1",
                        "finite_frontier": child,
                        "created_at": now(),
                        "stage_predecessor": lineage,
                    }
                    pipeline._save(path, state)
                return {
                    "finite_frontier": child,
                    "execution_targets": [target],
                    "frontier_target": target,
                    "predecessor": copy.deepcopy(
                        existing["stage_predecessor"] if existing is not None else lineage
                    ),
                    "reused": existing is not None,
                    "acceptance_authority": False,
                    "fresh_binding_drift": child["bindings"][target] != fresh["bindings"][target],
                }
