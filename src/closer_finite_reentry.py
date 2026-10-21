"""Exact, explicit re-entry for proven unstarted finite Closer requests.

This repairs scheduling state only. Normal dispatcher admission, interpretation,
queue claims and effect guards still run; historical state is never rewritten.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import time
from pathlib import Path

import closer_pipeline as pipeline
import closer_resources as resources
from closer_frontier_policy import validate_frontier

SCHEMA = "closer-finite-reentry/v1"


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _physical(path):
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError("aliased_path")
    return path


def _read(path):
    path = _physical(path)
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("oversized_record")
    try:
        value = json.loads(raw)
    except (RecursionError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_or_overdeep_json_record") from exc
    if not isinstance(value, dict):
        raise ValueError("record_must_be_object")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        child, depth = pending.pop()
        nodes += 1
        if depth > 128 or nodes > 100_000:
            raise ValueError("record_structure_bound")
        if isinstance(child, dict):
            pending.extend((item, depth + 1) for item in child.values())
        elif isinstance(child, list):
            pending.extend((item, depth + 1) for item in child)
    return value, hashlib.sha256(raw).hexdigest()


def _entry(state, frontier):
    entries = state.get("frontiers")
    if not isinstance(entries, dict):
        raise ValueError("malformed_finite_frontiers")
    entry = entries.get(frontier["frontier_id"])
    if not isinstance(entry, dict) or not isinstance(entry.get("items"), dict):
        raise ValueError("malformed_finite_entry")
    return entry


def _inner_boundary(path, target):
    inner = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    if not inner.exists():
        _physical(inner)
        return {"path": str(inner), "sha256": None}
    doc, sha = _read(inner)
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != "closer-queue/v1"
        or not isinstance(doc.get("items", {}), dict)
    ):
        raise ValueError("unreadable_inner_queue")

    # A pre-callback refusal must have no effect history at all, not merely an
    # absent current operation. Completed/foreign operations require real reconciliation.
    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key
                    in {
                        "operation",
                        "operation_history",
                        "provider_outcome_uncertain",
                        "recovery_barrier",
                        "action_history",
                    }
                    and child
                ):
                    raise ValueError("inner_operation_requires_reconciliation")
                if key == "status" and child is not None and not isinstance(child, str):
                    raise ValueError("malformed_inner_status")
                if (
                    key == "status"
                    and isinstance(child, str)
                    and child
                    in {
                        "unknown",
                        "uncertain",
                        "running",
                        "submitted",
                        "completed",
                        "complete",
                    }
                ):
                    raise ValueError("inner_outcome_requires_reconciliation")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(doc)
    return {"path": str(inner), "sha256": sha}


def _scope_proof(root, path, target, entry, row, *, caller_path):
    expected_binding = {
        "state_path": str(path),
        "pipeline_run_id": entry["run_id"],
        "revision": row.get("revision"),
        "identity": row.get("identity", target),
        "work_key": row.get("work_key"),
    }
    candidates = []
    retained = row.get("scope_refusal")
    if retained is not None and (
        not isinstance(retained, dict) or not isinstance(retained.get("path"), str)
    ):
        raise ValueError("malformed_scope_refusal_reference")
    paths = (
        [Path(retained["path"])]
        if isinstance(retained, dict) and retained.get("path")
        else list((root / "scope-admissions").glob("*/*.json"))
    )
    if len(paths) > 4096:
        raise ValueError("scope_proof_inventory_bound")
    for candidate in paths:
        candidate = _physical(candidate)
        if not candidate.is_relative_to(root / "scope-admissions"):
            raise ValueError("scope_proof_outside_admission_inventory")
        proof, sha = _read(candidate)
        if isinstance(retained, dict) and sha != retained.get("sha256"):
            raise ValueError("scope_proof_digest_changed")
        binding = proof.get("binding")
        # Only this exact request can contribute a proof. An unrelated record's
        # obsolete/aliased workspace must not poison the current target's plan.
        if (
            proof.get("schema") != "closer-scoped-admission/v1"
            or proof.get("target") != target
            or proof.get("stage") != row.get("stage")
            or not isinstance(binding, dict)
            or {key: value for key, value in binding.items() if key != "state_path"}
            != {key: value for key, value in expected_binding.items() if key != "state_path"}
        ):
            continue
        normalized_binding = None
        if isinstance(binding, dict) and isinstance(binding.get("state_path"), str):
            producer_path = Path(binding["state_path"])
            # A relative producer path needs the same explicit caller context.
            # Never guess an unavailable original working directory.
            known_context = producer_path.is_absolute() or (
                not Path(caller_path).is_absolute() and producer_path == Path(caller_path)
            )
            if known_context and _physical(producer_path) == path:
                normalized_binding = {**binding, "state_path": str(path)}
        if (
            proof.get("schema") == "closer-scoped-admission/v1"
            and proof.get("target") == target
            and proof.get("stage") == row.get("stage")
            and normalized_binding == expected_binding
            and proof.get("admitted") is False
            and proof.get("callback_started") is False
            and type(proof.get("observed_at")) in {int, float}
            and type(row.get("stage_started_at")) in {int, float}
            and type(row.get("stage_deadline")) in {int, float}
            and row.get("stage_started_at", float("inf"))
            <= proof["observed_at"]
            <= row.get("stage_deadline", float("-inf"))
            and isinstance(proof.get("reason"), str)
            and proof["reason"]
            # Match the finite coordinator's retained error format exactly;
            # the full reason remains bound by the admission proof digest.
            and row.get("error") == "ScopeBlocked: " + proof["reason"][:200]
            and proof.get("input_sha256")
            == _digest(
                {
                    k: row.get("results", {})[k]
                    for k in pipeline.STAGES
                    if k in row.get("results", {})
                }
            )
        ):
            candidates.append({"path": str(candidate), "sha256": sha})
    if len(candidates) != 1:
        raise ValueError("missing_or_ambiguous_exact_pre_callback_proof")
    return candidates[0]


def plan(state_path, *, frontier, target, event):
    """Read-only plan. A trigger is explicit evidence, never new action authority."""
    path = _physical(state_path)
    root = _physical(path.parent.parent.parent)
    if (
        path.name != "state.json"
        or path.parent.parent.name != "targets"
        or not root.name.endswith(".scoped")
    ):
        raise ValueError("finite_reentry_requires_target_shard")
    frontier = validate_frontier(frontier)
    if target not in frontier["targets"] or path.parent != resources.shard_dir(root, target):
        raise ValueError("target_shard_identity_mismatch")
    if not isinstance(event, str) or not event.strip() or len(event) > 512:
        raise ValueError("explicit_bounded_retry_event_required")
    state, _ = _read(path)
    lease, lease_sha = _read(path.parent / "lease.json")
    if (
        lease.get("target") != resources.canonical_target(target)
        or lease.get("finite_frontier") != frontier
        or type(lease.get("epoch")) is not int
        or lease["epoch"] < 1
        or not isinstance(lease.get("owner"), str)
        or not lease["owner"]
    ):
        raise ValueError("finite_lease_identity_changed")
    entry = _entry(state, frontier)
    if (
        not isinstance(entry, dict)
        or entry.get("finite_frontier") != frontier
        or entry.get("schema") != "closer-pipeline-frontier/v2"
        or entry.get("run_id")
        != pipeline._digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[:32]
    ):
        raise ValueError("finite_frontier_identity_changed")
    row = entry.get("items", {}).get(target)
    if (
        not isinstance(row, dict)
        or row.get("recovery_barrier") is not None
        or row.get("provider_outcome_uncertain") not in (None, False)
    ):
        raise ValueError("unresolved_provider_or_recovery_boundary")
    if not isinstance(row.get("status"), str) or row["status"] not in {"held", "unknown"}:
        raise ValueError("row_not_retryable")
    results = row.get("results", {})
    if not isinstance(results, dict) or any(
        key in results and not isinstance(results[key], dict) for key in pipeline.STAGES
    ):
        raise ValueError("malformed_callback_results")
    if any(
        result.get("status") is not None and not isinstance(result.get("status"), str)
        for result in results.values()
        if isinstance(result, dict)
    ):
        raise ValueError("malformed_callback_status")
    history = row.get("reentry_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError("malformed_reentry_history")
    if row.get("error") is not None and not isinstance(row["error"], str):
        raise ValueError("malformed_request_error")
    if any(
        isinstance(results.get(k), dict)
        and (
            results[k].get("outcome_uncertain")
            or results[k].get("status") in {"unknown", "uncertain", "running", "submitted"}
        )
        for k in pipeline.STAGES
    ):
        raise ValueError("unresolved_callback_result")
    stage, proof, transport = row.get("stage"), None, None
    if not isinstance(stage, str):
        raise ValueError("malformed_request_stage")
    kind = None
    if (
        row.get("status") == "held"
        and stage == "interpret"
        and results.get("interpret", {}).get("classification") == "assessment_transport_unavailable"
    ):
        import adapters

        problem = adapters.data_only_problem("claude", "assess")
        if problem:
            raise ValueError("assessment_transport_still_unqualified:" + problem)
        transport = {
            "path": adapters._data_only_claude_binary(),
            "sha256": adapters.DATA_ONLY_CLAUDE_SHA256,
        }
        kind = "qualified_assessment_transport"
    elif (
        row.get("status") == "held"
        and stage == "prepare"
        and row.get("error") == "ResourceBlocked: no io pool slot before the deadline"
    ):
        kind = "unstarted_prepare_pool"
    elif (row.get("error") or "").startswith("ScopeBlocked: "):
        proof = _scope_proof(root, path, target, entry, row, caller_path=state_path)
        kind = "pre_callback_scope_refusal"
    else:
        raise ValueError("no_supported_unstarted_boundary")
    if stage not in {"prepare", "interpret", "act"}:
        raise ValueError("unsupported_retry_stage")
    inner = _inner_boundary(path, target)
    trigger_fingerprint = _digest(
        {
            "kind": kind,
            "stage": stage,
            "transport": transport,
            "retained_results": results,
            "scope_reason": row.get("error") if proof else None,
        }
    )
    if any(
        item.get("trigger_fingerprint") == trigger_fingerprint
        for item in row.get("reentry_history", [])
    ):
        raise ValueError("unchanged_boundary_already_retried")
    result = {
        "schema": SCHEMA,
        "state_path": str(path),
        "target": target,
        "frontier": frontier,
        "event": event,
        "row_sha256": _digest(row),
        "kind": kind,
        "stage": stage,
        "scope_proof": proof,
        "transport": transport,
        "lease_sha256": lease_sha,
        "trigger_fingerprint": trigger_fingerprint,
        "inner_boundary": inner,
        "acceptance_authority": False,
        "global_stop_authority": False,
    }
    return {**result, "plan_sha256": _digest(result)}


@contextlib.contextmanager
def _exclusive(path):
    path = _physical(path)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise resources.ResourceBlocked("active_finite_writer") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def apply(state_path, *, frontier, target, event, expected_plan_sha256):
    """CAS one target under existing target/run/inventory/state ownership locks."""
    path = _physical(state_path)
    frontier = validate_frontier(frontier)
    root = _physical(path.parent.parent.parent)
    key = _digest([frontier, target, event])
    audit = root / "reentry-audit" / (key + ".json")
    with _exclusive(path.with_name(path.name + ".runlock")), _exclusive(path.parent / "lease.lock"):
        with resources.inventory_lock(root, exclusive=True, deadline=time.time() + 10):
            with pipeline._locked(path, timeout_seconds=10):
                if audit.exists():
                    record, _ = _read(audit)
                    if not isinstance(record.get("plan"), dict):
                        raise ValueError("malformed_reentry_audit_plan")
                    if (
                        not isinstance(record.get("before"), dict)
                        or not isinstance(record.get("after"), dict)
                        or _digest(record["after"]) != record.get("after_sha256")
                    ):
                        raise ValueError("malformed_reentry_audit_rows")
                    if record.get("plan", {}).get("plan_sha256") != expected_plan_sha256:
                        raise ValueError("retry_event_already_used_for_another_plan")
                    state, _ = _read(path)
                    row = _entry(state, frontier)["items"][target]
                    if record.get("status") == "prepared":
                        if _digest(row) == _digest(record.get("before")):
                            fresh = plan(state_path, frontier=frontier, target=target, event=event)
                            if fresh["plan_sha256"] != expected_plan_sha256:
                                raise ValueError("prepared_reentry_inputs_changed")
                            state["frontiers"][frontier["frontier_id"]]["items"][target] = record[
                                "after"
                            ]
                            pipeline._save_snapshot(path, state)
                        elif _digest(row) != record.get("after_sha256"):
                            raise ValueError("prepared_reentry_state_uncertain")
                        record["status"] = "applied"
                        resources._atomic_write(audit, record)
                    elif record.get("status") != "applied":
                        raise ValueError("invalid_reentry_audit")
                    return {"status": "applied", "idempotent": True, "audit": str(audit)}
                proposal = plan(state_path, frontier=frontier, target=target, event=event)
                if proposal["plan_sha256"] != expected_plan_sha256:
                    raise ValueError("reentry_plan_changed")
                state, _ = _read(path)
                entry = state["frontiers"][frontier["frontier_id"]]
                before = copy.deepcopy(entry["items"][target])
                row = entry["items"][target]
                history = row.setdefault("reentry_history", [])
                prior_request = {
                    key: value for key, value in before.items() if key != "reentry_history"
                }
                history.append(
                    {
                        "event": event,
                        "plan_sha256": expected_plan_sha256,
                        "trigger_fingerprint": proposal["trigger_fingerprint"],
                        "previous": copy.deepcopy(prior_request),
                        "at": time.time(),
                    }
                )
                if len(history) > 32:
                    raise ValueError("reentry_history_bound")
                row.pop("error", None)
                row.pop("deferred_next_trigger", None)
                row.pop("scope_refusal", None)
                if proposal["stage"] == "act":
                    # No effect occurred, but the real historical scope is still
                    # unavailable. Reconciliation cannot turn that into readiness.
                    row.update(
                        status="held",
                        stage_status="completed",
                        deferred_reason="scope_quarantined",
                        deferred_next_trigger="reconcile_scope_before_retry",
                    )
                    row["resource_evidence"] = {
                        "scope_refusal": proposal["scope_proof"],
                        "confirmed_not_started": True,
                    }
                else:
                    # Keep a known no-effect boundary until the normal target
                    # admission and new lease succeed. A bare pending row would
                    # correctly be censused as an abandoned unresolved owner.
                    row.update(status="held", stage_status="queued")
                    row["reentry_ready"] = {"event": event, "plan_sha256": expected_plan_sha256}
                    if proposal["kind"] == "qualified_assessment_transport":
                        row.get("results", {}).pop("interpret", None)
                record = {
                    "schema": SCHEMA,
                    "plan": proposal,
                    "before": before,
                    "after": copy.deepcopy(row),
                    "after_sha256": _digest(row),
                    "status": "prepared",
                    "at": time.time(),
                }
                audit.parent.mkdir(parents=True, exist_ok=True)
                resources._atomic_write(audit, record)
                pipeline._save_snapshot(path, state)
                record["status"] = "applied"
                resources._atomic_write(audit, record)
                return {
                    "status": "applied",
                    "idempotent": False,
                    "audit": str(audit),
                    "stage": row["stage"],
                    "row_status": row["status"],
                }


def ready(state_path, frontier, target, row):
    """Validate the exact audited hold before its production lease consumes it."""
    marker = row.get("reentry_ready")
    if (
        not isinstance(marker, dict)
        or row.get("status") != "held"
        or row.get("stage_status") != "queued"
    ):
        raise ValueError("invalid_reentry_ready_boundary")
    path = _physical(state_path)
    root = path.parent.parent.parent
    # The resource fence registry deliberately uses the lease's caller-form
    # pathname; physical paths remain mandatory for filesystem/audit checks.
    identity = resources.fence_identity(Path(state_path))
    fence = resources.fence_for(Path(state_path))
    if identity is None or fence is None:
        raise ValueError("reentry_requires_live_production_lease")
    fence()
    lease, _ = _read(path.parent / "lease.json")
    if lease.get("finite_frontier") != frontier or identity != {
        "target": resources.canonical_target(target),
        "owner": lease.get("owner"),
        "epoch": lease.get("epoch"),
    }:
        raise ValueError("reentry_live_lease_identity_changed")
    key = _digest([frontier, target, marker.get("event")])
    audit, _ = _read(root / "reentry-audit" / (key + ".json"))
    proposal = audit.get("plan", {})
    if not isinstance(proposal, dict):
        raise ValueError("malformed_reentry_audit_plan")
    if (
        audit.get("schema") != SCHEMA
        or audit.get("status") != "applied"
        or proposal.get("frontier") != frontier
        or proposal.get("target") != target
        or proposal.get("state_path") != str(path)
        or proposal.get("plan_sha256") != marker.get("plan_sha256")
        or audit.get("after_sha256") != _digest(row)
        or not isinstance(row.get("stage"), str)
        or row["stage"] not in {"prepare", "interpret"}
    ):
        raise ValueError("reentry_audit_does_not_bind_ready_row")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="exact finite target shard state.json")
    parser.add_argument("--frontier", required=True, help="frozen finite-frontier/v2 JSON file")
    parser.add_argument("--target", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_plan_sha256:
        parser.error("--apply requires --expected-plan-sha256")
    try:
        frontier, _ = _read(args.frontier)
        common = {"frontier": frontier, "target": args.target, "event": args.event}
        result = (
            apply(args.state, **common, expected_plan_sha256=args.expected_plan_sha256)
            if args.apply
            else plan(args.state, **common)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, resources.ResourceBlocked) as exc:
        print(json.dumps({"status": "held", "reason": str(exc), "acceptance_authority": False}))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
