"""Join closer allocation/execution and independent validation in the existing Brain.

An accepted assessment is stage-quality evidence, never source completion or a
durable merge. Missing prices, usage and opportunity cost stay unknown.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

TASKS = {
    "closer_interpret",
    "closer_exception",
    "closer_repair",
    "closer_correction_classification",
}
VALIDATORS = {"closer_disposition.integrate_ordinary", "closer_dispatcher.response_binding"}
EFFICIENCY_SCHEMA = "closer-efficiency-summary/v1"
_DIRECT_BRANCHES = {
    "deterministic_merge",
    "deterministic_close_source",
    "deterministic_post_merge_verify",
    "deterministic_complete",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def record_stage(
    result: dict[str, Any],
    *,
    task_type: str,
    target: str,
    head: str | None,
    agent: str,
    profile_id: str,
    basis: Any,
    capacity_context: Any = None,
    elapsed_seconds: float | None = None,
    validation: dict[str, Any] | None = None,
    structurally_invalid: bool = False,
    parent_run_id: str | None = None,
    writer: Callable[..., Any] | None = None,
) -> None:
    """Best-effort telemetry; this never changes an action/acceptance decision."""
    run_id = result.get("run_id")
    if task_type not in TASKS or not isinstance(run_id, str) or not run_id:
        return
    import execution_profiles
    import feedback

    try:
        if writer is None:
            with feedback._conn(timeout=0.2) as conn:
                row = conn.execute(
                    "SELECT agent,task_type,routing_metadata FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
            if row is None or row[0] != agent or row[1] != task_type:
                return
            metadata = json.loads(row[2] or "{}")
            if (
                metadata.get("selected_profile_id") != profile_id
                or str(metadata.get("subject_id", "")).casefold() != target.casefold()
            ):
                return
        profile = execution_profiles.get_profile(profile_id)
        if profile["agent"] != agent:
            return
        accepted: bool | None = None
        validator = "closer_disposition.integrate_ordinary"
        if validation is not None:
            status = validation.get("status")
            if (
                status in {"accepted", "ordinary_nonpass"}
                and type(result.get("exit")) is int
                and result["exit"] == 0
            ):
                accepted = True
            elif structurally_invalid and result.get("exit") == 0:
                accepted = False
                validator = "closer_dispatcher.response_binding"
        if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
            accepted = None
        digest = hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()
        raw_usage = result.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        elapsed = _number(
            elapsed_seconds
            if elapsed_seconds is not None
            else result.get("_closer_elapsed_seconds")
        )
        payload = {
            "task_category": task_type,
            "target": target,
            "head_sha": head,
            "basis_digest": digest,
            "agent": agent,
            "requested_profile_id": profile_id,
            "requested_model": profile["requested_model"],
            "requested_reasoning_effort": profile["reasoning_effort"],
            "served_model": result.get("served_model"),
            "served_model_status": result.get("served_model_status", "unknown"),
            "capacity_context": capacity_context,
            "attempts": 1,
            "elapsed_seconds": elapsed,
            "measured_usage": {
                "tokens_in": _number(usage.get("input_tokens", result.get("tokens_in"))),
                "tokens_out": _number(usage.get("output_tokens", result.get("tokens_out"))),
            },
            "observed_cost_usd": _number(result.get("cost_usd")),
            "actual_marginal_dollars": None,
            "opportunity_cost": None,
            "capacity_pool_ids": profile["capacity_pool_ids"],
            "capacity_ledger_run_id": run_id,
            "parent_run_id": parent_run_id,
            "repair_review_overhead_seconds": (
                elapsed if task_type in {"closer_exception", "closer_repair"} else None
            ),
            "accepted": accepted,
            "validator": validator,
            "independent_validation": validation is not None,
            "validation_status": (validation or {}).get("status"),
            "guarded_operation_success": None,
            "source_completion": False,
        }
        phase = "verification" if validation is not None else "execution"
        event_id = (
            "closer-stage:"
            + hashlib.sha256(
                json.dumps([run_id, phase, task_type, digest], separators=(",", ":")).encode()
            ).hexdigest()
        )
        (writer or feedback.record_completion_event)(
            run_id,
            event_type="closer_stage",
            phase=phase,
            producer=validator if validation is not None else "closer_dispatcher.worker",
            status=("pass" if accepted is True else "fail" if accepted is False else "unknown"),
            payload={"closer_stage": payload},
            event_id=event_id,
        )
    except Exception:
        # A telemetry outage cannot authorize an action, replace a validation,
        # or turn missing evidence into a success.
        return


def _retained_json(value: Any) -> str | None:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None


def _identity_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _head(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _measurement(values: Iterable[float | None], total: int) -> dict[str, Any]:
    known = [number for value in values if (number := _number(value)) is not None]
    try:
        aggregate = math.fsum(known) if known else None
    except OverflowError:
        aggregate = None
    return {
        "total_or_null": aggregate,
        "measured_runs": len(known),
        "total_runs": total,
        "coverage": len(known) / total if total else None,
        "status": (
            "unknown" if aggregate is None else "complete" if len(known) == total else "partial"
        ),
    }


def _branch_latency(
    row: Mapping[str, Any], receipt_finished: float, *, native: bool = False
) -> float | None:
    """Use retained completed stage timings, or the legacy duration metrics."""
    timings = row.get("stage_results")
    durations: list[float | None]
    if native or timings is not None:
        if not isinstance(timings, Mapping) or not timings:
            return None
        durations = []
        for timing in timings.values():
            if not isinstance(timing, Mapping):
                return None
            started, finished = _number(timing.get("started_at")), _number(
                timing.get("finished_at")
            )
            if (
                timing.get("status") != "completed"
                or not _identity_text(timing.get("work_key"))
                or started is None
                or finished is None
                or not started <= finished <= receipt_finished
            ):
                return None
            durations.append(finished - started)
    else:
        metrics = row.get("metrics", {})
        seconds = metrics.get("stage_seconds", {}) if isinstance(metrics, Mapping) else {}
        if not isinstance(seconds, Mapping) or not seconds:
            return None
        durations = [_number(value) for value in seconds.values()]
        if any(value is None for value in durations):
            return None
    try:
        return _number(math.fsum(value for value in durations if value is not None))
    except OverflowError:
        return None


def _completed_verifier_request(
    row: Mapping[str, Any], target: str, head: Any, frontier: Any
) -> bool:
    """Count retained, authenticated POST progress, never its downstream result.

    The producer's resource proof already checked the original durable operation.
    This pure read-side join checks its exact receipt and causal events again;
    pending callback metadata alone is insufficient and grants no authority.
    """
    resource = row.get("resource_evidence")
    proof = resource.get("accepted_verifier_request") if isinstance(resource, Mapping) else None
    results = row.get("results")
    act = results.get("act") if isinstance(results, Mapping) else None
    if (
        not isinstance(proof, Mapping)
        or not isinstance(act, Mapping)
        or not isinstance(frontier, Mapping)
    ):
        return False
    from closer_frontier_policy import validate_frontier

    try:
        frontier = validate_frontier(frontier, targets=[target])
    except (TypeError, ValueError):
        return False
    queue = act.get("queue_receipt")
    evidence = act.get("pending_evidence")
    if not isinstance(queue, Mapping) or not isinstance(evidence, Mapping):
        return False
    raw = json.dumps(queue, sort_keys=True, separators=(",", ":")).encode()
    operation = proof.get("operation_id")
    if not (
        row.get("status") == "held"
        and row.get("deferred_reason") == "accepted_verifier_request"
        and act.get("status") == "pending"
        and act.get("pending_kind") == "external_event"
        and proof.get("schema") == "closer-accepted-verifier-wait/v1"
        and proof.get("target") == target
        and proof.get("head") == head
        and _head(head)
        and _head(proof.get("merge"))
        and _identity_text(operation)
        and proof.get("receipt_operation_id") == operation
        and proof.get("operation_frontier") == frontier.get("frontier_id")
        and _identity_text(proof.get("operation_frontier"))
        and frontier.get("targets") == [target]
        and frontier["bindings"][target]["revision"] == row.get("revision")
        and _identity_text(proof.get("observed_revision"))
        and isinstance(proof.get("event_id"), str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["event_id"])
        and all(
            evidence.get(key) == proof.get(key)
            for key in ("target", "head", "merge", "event_id", "operation_id", "observed_revision")
        )
        and proof.get("queue_receipt_sha256") == hashlib.sha256(raw).hexdigest()
        and row.get("queue_receipt") == queue
        and queue.get("valid") is True
        and queue.get("stop_valid") is True
    ):
        return False
    accounting = queue.get("accounting")
    events = queue.get("events")
    if not isinstance(accounting, list) or not isinstance(events, list):
        return False
    matches = [
        entry
        for entry in accounting
        if isinstance(entry, Mapping) and entry.get("target") == target
    ]
    if len(matches) != 1:
        return False
    entry = matches[0]
    return bool(
        entry.get("operation_id") == operation
        and entry.get("operation_status") == "completed"
        and entry.get("status") == "deferred"
        # Persisting a completed POST before observation can immediately expose
        # its result-wait. Eligibility to rerun is not whether that POST ran.
        and (
            (entry.get("eligible") is True and entry.get("reason") == "pass_limit")
            or (
                entry.get("eligible") is False
                and entry.get("reason")
                in {
                    "same verification request already accepted",
                    "collect terminal or unattributed verifier evidence; do not rerun",
                }
            )
        )
        and entry.get("action") == "collect_evidence"
        and entry.get("revision") == proof.get("observed_revision")
        and any(
            isinstance(event, Mapping)
            and event.get("kind") == "operation_submitted"
            and event.get("target") == target
            and event.get("operation_id") == operation
            and event.get("action") == "post_merge_verify"
            for event in events
        )
        and any(
            isinstance(event, Mapping)
            and event.get("kind") == "action_advanced"
            and event.get("target") == target
            and event.get("operation_id", operation) == operation
            and event.get("revision") == proof.get("observed_revision")
            and event.get("action") == "post_merge_verify"
            for event in events
        )
    )


def summarize_efficiency(
    receipts: Iterable[Mapping[str, Any]],
    *,
    stage_events: Iterable[Mapping[str, Any]] = (),
    holds: Iterable[Mapping[str, Any]] = (),
    independent_outcomes: Iterable[Mapping[str, Any]] = (),
    now: float,
    current_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    max_age_seconds: float = 86400,
    aged_after_seconds: float = 3600,
) -> dict[str, Any]:
    """Summarize already-retained evidence without reads, writes or authority.

    Legacy run_id and native round_id/run_ids receipts share pipeline-run/target/
    revision branch identities, not hypothetical model
    calls. Only an observed direct ACT branch with no retained reasoning run is
    a zero-reasoning branch; capacity, qualification and ordinary waits are not
    call avoidance. Model IDs, latency and cost are separately deduplicated.

    Optional independent outcomes use schema ``closer-independent-outcome/v1``:
    run_id, target, head_sha, observed_at, independent=True, an evidence_ref with
    sha256 and path/url, and explicit boolean false_acceptance/rework labels.
    Stage accepted/PASS flags never create those labels or source completion.
    Without current_bindings, historical head freshness remains unverified.
    """
    clock = _number(now)
    age_limit = _number(max_age_seconds)
    hold_age = _number(aged_after_seconds)
    if clock is None or age_limit is None or hold_age is None:
        raise ValueError("efficiency summary needs finite nonnegative time limits")
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    def recent(stamp: Any) -> bool:
        observed = _number(stamp)
        return observed is not None and 0 <= clock - observed <= age_limit

    def current(target: str, revision: str, head: Any) -> bool:
        if current_bindings is None:
            return True
        binding = current_bindings.get(target)
        return bool(
            isinstance(binding, Mapping)
            and binding.get("revision") == revision
            and binding.get("head") == head
        )

    # Conflicting copies of the same receipt are ambiguous, not extra work.
    retained: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    conflicts: set[tuple[str, str]] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            reject("invalid_receipt")
            continue
        raw = _retained_json(receipt)
        native = "round_id" in receipt or "run_ids" in receipt
        run = receipt.get("round_id") if native else receipt.get("run_id")
        if (
            raw is None
            or receipt.get("schema") != "closer-pipeline-receipt/v1"
            or not _identity_text(run)
            or not isinstance(run, str)
            or not isinstance(receipt.get("targets"), Mapping)
        ):
            reject("invalid_receipt")
            continue
        if native:
            run_ids = receipt.get("run_ids")
            if (
                not isinstance(run_ids, Mapping)
                or set(run_ids) != set(receipt["targets"])
                or any(not _identity_text(value) for value in run_ids.values())
                or ("run_id" in receipt and receipt["run_id"] != run)
            ):
                reject("invalid_native_run_bindings")
                continue
        if not recent(receipt.get("finished_at")):
            reject("stale_receipt")
            continue
        identity = ("round" if native else "run", run)
        fingerprint = (
            _retained_json({"run_ids": receipt["run_ids"], "targets": receipt["targets"]})
            if native
            else raw
        )
        assert fingerprint is not None  # The entire receipt was already serializable.
        if identity in retained and retained[identity][0] != fingerprint:
            conflicts.add(identity)
        retained[identity] = (fingerprint, receipt)
    for receipt_identity in conflicts:
        retained.pop(receipt_identity, None)
        reject("conflicting_receipt")

    branch_rows: dict[
        tuple[str, str, str], tuple[str, Mapping[str, Any], Any, float, bool, Any]
    ] = {}
    branch_conflicts: set[tuple[str, str, str]] = set()
    for _, receipt in retained.values():
        for target, row in receipt["targets"].items():
            if not isinstance(row, Mapping) or not isinstance(target, str):
                reject("invalid_branch")
                continue
            results = row.get("results", {})
            prep = results.get("prepare", {}) if isinstance(results, Mapping) else {}
            snapshot = prep.get("snapshot", {}) if isinstance(prep, Mapping) else {}
            revision = row.get("revision")
            head = snapshot.get("head") if isinstance(snapshot, Mapping) else None
            run = receipt["run_ids"][target] if "run_ids" in receipt else receipt["run_id"]
            binding = current_bindings.get(target) if current_bindings is not None else None
            if (
                not _identity_text(revision)
                or not isinstance(revision, str)
                or not isinstance(snapshot, Mapping)
                or snapshot.get("target") != target
                or prep.get("revision") != revision
                or snapshot.get("revision", revision) != revision
                or (head is not None and not _head(head))
                or ("run_id" in row and row["run_id"] != run)
                or (
                    isinstance(binding, Mapping)
                    and "run_id" in binding
                    and binding["run_id"] != run
                )
                or not current(target, revision, head)
            ):
                reject("wrong_or_stale_branch_identity")
                continue
            branch_key = (run, target, revision)
            metrics = row.get("metrics", {})
            fingerprint = _retained_json(
                {
                    "head": head,
                    "results": results,
                    "legacy_stage_seconds": (
                        metrics.get("stage_seconds") if isinstance(metrics, Mapping) else None
                    ),
                    "stage_results": row.get("stage_results"),
                    "work_key": row.get("work_key"),
                    "stage": row.get("stage"),
                    "stage_status": row.get("stage_status"),
                    "resource_evidence": row.get("resource_evidence"),
                    "finite_frontier": receipt.get("finite_frontier"),
                }
            )
            assert fingerprint is not None
            if branch_key in branch_rows and branch_rows[branch_key][0] != fingerprint:
                branch_conflicts.add(branch_key)
            branch_rows[branch_key] = (
                fingerprint,
                row,
                head,
                float(receipt["finished_at"]),
                "run_ids" in receipt,
                receipt.get("finite_frontier"),
            )
    for branch_key in branch_conflicts:
        branch_rows.pop(branch_key, None)
        reject("conflicting_branch")

    models: dict[str, tuple[str, Any]] = {}
    model_conflicts: set[str] = set()
    branches: list[tuple[str, set[str], bool, float | None]] = []
    for (_run, target, _revision), (
        _,
        row,
        head,
        finished,
        native,
        frontier,
    ) in branch_rows.items():
        results = row["results"]
        ids: set[str] = set()
        for stage_name in ("interpret", "exception", "act"):
            result = results.get(stage_name, {})
            if not isinstance(result, Mapping):
                continue
            nested = result.get("result", {})
            candidates = [result.get("actual"), result.get("worker")]
            correction = (
                result.get("correction") if stage_name in {"interpret", "exception"} else None
            )
            if isinstance(correction, Mapping):
                candidates.append(correction.get("actual"))
            if isinstance(nested, Mapping):
                candidates.append(nested.get("worker"))
            for actual in candidates:
                rid = actual.get("run_id") if isinstance(actual, Mapping) else None
                if _identity_text(rid) and isinstance(rid, str):
                    ids.add(rid)
                    identity = (target, head)
                    if rid in models and models[rid] != identity:
                        model_conflicts.add(rid)
                    models[rid] = identity
        interpreted = results.get("interpret", {})
        classification = (
            interpreted.get("classification", "unknown")
            if isinstance(interpreted, Mapping)
            else "unknown"
        )
        act = results.get("act")
        is_direct = bool(
            classification in _DIRECT_BRANCHES
            and isinstance(act, Mapping)
            and (
                act.get("status") in {"completed", "progress"}
                or (
                    native
                    and classification == "deterministic_post_merge_verify"
                    and _completed_verifier_request(row, target, head, frontier)
                )
            )
        )
        elapsed = _branch_latency(row, finished, native=native)
        if (native or "stage_results" in row) and elapsed is None:
            reject("invalid_or_incomplete_stage_timing")
        branches.append((str(classification), ids, is_direct, elapsed))
    for run in model_conflicts:
        models.pop(run, None)
        reject("conflicting_model_identity")

    direct = assisted = waits = 0
    branch_latency: dict[str, list[float | None]] = {}
    for classification, ids, is_direct, elapsed in branches:
        if ids.intersection(model_conflicts):
            reject("ambiguous_branch_model_identity")
            continue
        if ids:
            assisted += 1
        elif is_direct:
            direct += 1
        else:
            waits += 1
        branch_latency.setdefault(classification, []).append(elapsed)

    # Execution and verification phases often repeat the same cost/latency.
    # Count the model run once; conflicting measurements stay unknown.
    measurements: dict[str, dict[str, set[float]]] = {}
    for event in stage_events:
        if not isinstance(event, Mapping):
            reject("invalid_stage_event")
            continue
        if (
            _retained_json(event) is None
            or event.get("event_type", "closer_stage") != "closer_stage"
        ):
            reject("invalid_stage_event")
            continue
        if "ts" in event and not recent(event["ts"]):
            reject("stale_stage_event")
            continue
        payload = event.get("payload")
        if payload is None and isinstance(event.get("payload_json"), str):
            try:
                payload = json.loads(event["payload_json"])
            except (ValueError, RecursionError):
                payload = None
        stage = payload.get("closer_stage") if isinstance(payload, Mapping) else None
        run = event.get("run_id")
        if (
            not isinstance(stage, Mapping)
            or _retained_json(stage) is None
            or not isinstance(run, str)
            or run not in models
            or models[run] != (stage.get("target"), stage.get("head_sha"))
            or stage.get("capacity_ledger_run_id") != run
            or stage.get("task_category") not in TASKS
        ):
            reject("unbound_stage_event")
            continue
        values = measurements.setdefault(run, {"latency": set(), "cost": set()})
        for key, field in (("latency", "elapsed_seconds"), ("cost", "observed_cost_usd")):
            value = _number(stage.get(field))
            if value is not None:
                values[key].add(value)

    def observation(run: str, key: str) -> float | None:
        values = measurements.get(run, {}).get(key, set())
        return next(iter(values)) if len(values) == 1 else None

    labels: dict[str, dict[str, set[bool]]] = {}
    for outcome in independent_outcomes:
        if not isinstance(outcome, Mapping):
            reject("unbound_independent_outcome")
            continue
        run = outcome.get("run_id")
        proof = outcome.get("evidence_ref")
        if (
            _retained_json(outcome) is None
            or outcome.get("schema") != "closer-independent-outcome/v1"
            or outcome.get("independent") is not True
            or not isinstance(run, str)
            or run not in models
            or not _head(outcome.get("head_sha"))
            or models[run] != (outcome.get("target"), outcome.get("head_sha"))
            or not recent(outcome.get("observed_at"))
            or not isinstance(proof, Mapping)
            or re.fullmatch(r"[0-9a-f]{64}", str(proof.get("sha256", ""))) is None
            or not _identity_text(proof.get("path") or proof.get("url"))
        ):
            reject("unbound_independent_outcome")
            continue
        label_values = labels.setdefault(run, {"false_acceptance": set(), "rework": set()})
        for key in label_values:
            if type(outcome.get(key)) is bool:
                label_values[key].add(outcome[key])

    outcomes: dict[str, Any] = {
        "measured_runs": sum(
            any(len(values) == 1 for values in row.values()) for row in labels.values()
        )
    }
    for key in ("false_acceptance", "rework"):
        known = [next(iter(row[key])) for row in labels.values() if len(row[key]) == 1]
        outcomes[key + "_measured_runs"] = len(known)
        outcomes[key + "_count"] = sum(known) if known else None
        outcomes[key + "_rate"] = sum(known) / len(known) if known else None

    hold_summary: dict[str, Any] = {
        "active": 0,
        "due": 0,
        "escalated": 0,
        "unowned": 0,
        "aged": 0,
        "oldest_age_seconds": None,
    }
    seen_holds: dict[str, str] = {}
    valid_holds: dict[str, Mapping[str, Any]] = {}
    conflicting_holds: set[str] = set()
    for hold in holds:
        if not isinstance(hold, Mapping):
            reject("invalid_or_stale_hold")
            continue
        raw = _retained_json(hold)
        hid, target, revision = hold.get("hold_id"), hold.get("target"), hold.get("revision")
        created, due = _number(hold.get("created_at")), _number(hold.get("next_check_at"))
        if (
            raw is None
            or hold.get("schema") != "closer-hold-followup/v1"
            or not isinstance(hid, str)
            or not _identity_text(hid)
            or not isinstance(target, str)
            or not isinstance(revision, str)
            or not _identity_text(hold.get("run_id"))
            or created is None
            or created > clock
            or due is None
            or hold.get("status")
            not in {"waiting", "checking", "released", "resolved", "consumed", "escalated"}
            or (
                current_bindings is not None
                and (
                    not isinstance(current_bindings.get(target), Mapping)
                    or current_bindings[target].get("revision") != revision
                )
            )
        ):
            reject("invalid_or_stale_hold")
            continue
        if hid in seen_holds and seen_holds[hid] != raw:
            conflicting_holds.add(hid)
            valid_holds.pop(hid, None)
            reject("conflicting_hold")
            continue
        seen_holds[hid] = raw
        if hid not in conflicting_holds:
            valid_holds[hid] = hold
    for hold in valid_holds.values():
        status = hold["status"]
        if status in {"released", "resolved", "consumed"}:
            continue
        active = status in {"waiting", "checking"}
        age = clock - float(hold["created_at"])
        hold_summary["active"] += int(active)
        hold_summary["due"] += int(active and hold["next_check_at"] <= clock)
        hold_summary["escalated"] += int(status == "escalated")
        hold_summary["unowned"] += int(not _identity_text(hold.get("owner")))
        hold_summary["aged"] += int(age >= hold_age)
        hold_summary["oldest_age_seconds"] = max(hold_summary["oldest_age_seconds"] or 0, age)

    return {
        "schema": EFFICIENCY_SCHEMA,
        "freshness": (
            "current_bindings_checked"
            if current_bindings is not None
            else "historical_freshness_unverified"
        ),
        "observed_branches": {
            "denominator": direct + assisted,
            "deterministic_zero_reasoning": direct,
            "with_retained_model_runs": assisted,
            "excluded_waits": waits,
        },
        "model_runs": {
            "ids": sorted(models),
            "count": len(models),
            "bindings": {
                run: {"target": target, "head_sha": head}
                for run, (target, head) in sorted(models.items())
                if _head(head)
            },
            "stage_event_coverage": len(measurements) / len(models) if models else None,
        },
        "observed_latency_seconds": _measurement(
            (observation(run, "latency") for run in models), len(models)
        ),
        "observed_cost_usd": _measurement(
            (observation(run, "cost") for run in models), len(models)
        ),
        "branch_stage_latency_seconds": {
            key: _measurement(values, len(values)) for key, values in sorted(branch_latency.items())
        },
        "independent_outcomes": outcomes,
        "holds": hold_summary,
        "rejected": rejected,
        "counterfactual_savings_usd": None,
        "authority": False,
    }


def read_stage_events(
    run_bindings: Mapping[str, Mapping[str, Any]],
    *,
    db_path: str | Path | None = None,
    max_runs: int = 128,
    max_events: int = 1024,
    max_payload_bytes: int = 32768,
    timeout_seconds: float = 0.2,
) -> dict[str, Any]:
    """Bounded read-only lookup of existing Brain events for retained model runs.

    Call with the pure summary's ``model_runs.bindings``. This never calls
    feedback._conn, creates/migrates a database, scans other run history, reads
    outcomes or interprets stage accepted/PASS as independent source evidence.
    Returned events are narrow projections of accepted closer_stage payloads,
    bound to persisted run/task/profile/agent/target and the supplied exact head.
    """
    report: dict[str, Any] = {
        "events": [],
        "status": "unavailable",
        "requested_runs": len(run_bindings),
        "returned_events": 0,
        "rejected_events": 0,
        "truncated": False,
        "authority": False,
    }
    if (
        type(max_runs) is not int
        or not 1 <= max_runs <= 512
        or type(max_events) is not int
        or not 1 <= max_events <= 4096
        or type(max_payload_bytes) is not int
        or not 1 <= max_payload_bytes <= 131072
        or _number(timeout_seconds) is None
        or not 0 < timeout_seconds <= 1
    ):
        return {**report, "reason": "invalid_lookup_limits"}
    if len(run_bindings) > max_runs:
        return {**report, "reason": "run_limit_exceeded", "truncated": True}
    if any(
        not _identity_text(run)
        or len(run) > 512
        or not isinstance(binding, Mapping)
        or not _identity_text(binding.get("target"))
        or not _head(binding.get("head_sha"))
        for run, binding in run_bindings.items()
    ):
        return {**report, "reason": "invalid_retained_run_binding"}
    if not run_bindings:
        return {**report, "status": "empty", "reason": "no_bound_model_runs"}
    import feedback

    connection: sqlite3.Connection | None = None
    try:
        selected_db = Path(db_path) if db_path is not None else feedback.resolve_db_path()
        connection = sqlite3.connect(
            selected_db.expanduser().resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=timeout_seconds,
        )
        steps = 0

        def read_budget() -> int:
            nonlocal steps
            steps += 1
            return int(steps > 1000)

        connection.set_progress_handler(read_budget, 1000)
        placeholders = ",".join("?" for _ in run_bindings)
        rows = connection.execute(
            "SELECT e.event_id,e.run_id,e.phase,e.producer,e.updated_ts,e.content_hash,"
            "CASE WHEN length(CAST(e.payload_json AS BLOB))<=? THEN e.payload_json ELSE NULL END,"
            "r.agent,r.task_type,"
            "CASE WHEN length(CAST(r.routing_metadata AS BLOB))<=? THEN r.routing_metadata ELSE NULL END "
            "FROM completion_events e JOIN runs r ON r.run_id=e.run_id "
            "WHERE e.schema_version=? AND e.event_type='closer_stage' AND e.validation_status='accepted' "
            "AND e.phase IN ('execution','verification') AND e.run_id IN (" + placeholders + ") "
            "ORDER BY e.updated_ts,e.event_id LIMIT ?",
            (
                max_payload_bytes,
                max_payload_bytes,
                feedback.COMPLETION_EVENT_SCHEMA_VERSION,
                *sorted(run_bindings),
                max_events + 1,
            ),
        ).fetchall()
        report["truncated"] = len(rows) > max_events
        for (
            event_id,
            run,
            phase,
            producer,
            stamp,
            content_hash,
            raw,
            agent,
            task,
            metadata_raw,
        ) in rows[:max_events]:
            try:
                payload = json.loads(raw) if isinstance(raw, str) else None
                metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else None
                stage = payload.get("closer_stage") if isinstance(payload, Mapping) else None
                if (
                    not isinstance(stage, Mapping)
                    or _retained_json(payload) is None
                    or not isinstance(metadata, Mapping)
                    or content_hash
                    != "sha256:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
                    or run not in run_bindings
                    or task not in TASKS
                    or stage.get("task_category") != task
                    or stage.get("agent") != agent
                    or not _identity_text(stage.get("requested_profile_id"))
                    or stage.get("requested_profile_id") != metadata.get("selected_profile_id")
                    or stage.get("target") != run_bindings[run]["target"]
                    or stage.get("head_sha") != run_bindings[run]["head_sha"]
                    or str(metadata.get("subject_id", "")).casefold()
                    != str(stage.get("target")).casefold()
                    or stage.get("capacity_ledger_run_id") != run
                    or _number(stamp) is None
                    or (phase == "execution" and producer != "closer_dispatcher.worker")
                    or (phase == "verification" and producer not in VALIDATORS)
                    or any(
                        stage.get(field) is not None and _number(stage.get(field)) is None
                        for field in ("elapsed_seconds", "observed_cost_usd")
                    )
                ):
                    report["rejected_events"] += 1
                    continue
                fields = (
                    "task_category",
                    "target",
                    "head_sha",
                    "agent",
                    "requested_profile_id",
                    "capacity_ledger_run_id",
                    "elapsed_seconds",
                    "observed_cost_usd",
                )
                report["events"].append(
                    {
                        "event_id": event_id,
                        "run_id": run,
                        "event_type": "closer_stage",
                        "phase": phase,
                        "producer": producer,
                        "ts": stamp,
                        "payload": {"closer_stage": {key: stage.get(key) for key in fields}},
                    }
                )
            except (TypeError, ValueError, RecursionError):
                report["rejected_events"] += 1
        report["returned_events"] = len(report["events"])
        report["status"] = (
            "partial" if report["truncated"] or report["rejected_events"] else "complete"
        )
        return report
    except (OSError, ValueError, sqlite3.Error):
        return {**report, "events": [], "returned_events": 0, "reason": "brain_read_unavailable"}
    finally:
        if connection is not None:
            connection.close()


def selftest() -> None:
    assert _number(None) is None and _number(True) is None
    assert _number(float("nan")) is None and _number(0) == 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--selftest"]:
        selftest()
        print("closer_stage_feedback selftest passed")
    else:
        raise SystemExit("use --selftest; events are emitted by the native dispatcher")
