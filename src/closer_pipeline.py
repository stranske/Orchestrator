"""Completion-driven, durable coordinator for bounded closer pipeline work.

The callbacks are adapters, not authority: in particular ``act`` must hand work
to the existing guarded closer queue/claims implementation.  This module owns
only scheduling state and evidence receipts.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SCHEMA = "closer-pipeline/v1"
STAGES = ("prepare", "interpret", "exception", "act", "reconcile")
CEILINGS = {"io": 8, "low": 4, "exception": 2}
MAX_TARGETS = 1024
CALLBACK_RESULT_BYTES = 32 * 1024
SEMANTIC_CACHE_PREFIX = "closer-evidence-semantic/v1:"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _semantic_cache_token(prepare: Any) -> str | None:
    """Accept only the producer's explicit versioned semantic binding."""
    if not isinstance(prepare, Mapping):
        return None
    value = prepare.get("semantic_cache_key")
    if not isinstance(value, str) or not value.startswith(SEMANTIC_CACHE_PREFIX):
        return None
    digest = value[len(SEMANTIC_CACHE_PREFIX) :]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return value


def _valid_worker_capacity(resource: Any, now_value: float) -> bool:
    if not isinstance(resource, Mapping):
        return False
    capacity = resource.get("capacity")
    if not isinstance(capacity, Mapping) or capacity.get("lane") not in CEILINGS:
        return False
    sampled_at = capacity.get("sampled_at")
    if (
        not isinstance(sampled_at, (int, float))
        or isinstance(sampled_at, bool)
        or not math.isfinite(float(sampled_at))
        or not 0 <= now_value - float(sampled_at) <= 1800
    ):
        return False
    active = capacity.get("active")
    limit = capacity.get("limit")
    if type(active) is int and type(limit) is int and limit > 0 and active >= limit:
        return True
    required = capacity.get("required")
    return capacity.get("available") == 0 and type(required) is int and required > 0


def _valid_provider_failure(resource: Any) -> bool:
    if not isinstance(resource, Mapping):
        return False
    failure = resource.get("provider_failure")
    if not isinstance(failure, Mapping):
        return False
    for field in ("profile_id", "agent", "run_id", "category", "evidence_hash"):
        value = failure.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    exit_code = failure.get("exit")
    return type(exit_code) is int and exit_code != 0


def _valid_deadline_evidence(resource: Any, deadline: float, rows: Mapping[str, Any]) -> bool:
    if not isinstance(resource, Mapping) or resource.get("deadline") != deadline:
        return False
    observed = resource.get("observed_at")
    if not isinstance(observed, (int, float)) or isinstance(observed, bool) or observed < deadline:
        return False
    if resource.get("type") == "active_deadline":
        return True
    if resource.get("type") != "round_deadline_exhausted":
        return False
    reported = resource.get("reported_deadline")
    remaining = resource.get("remaining_seconds")
    source = resource.get("source_target")
    return (
        isinstance(reported, (int, float))
        and not isinstance(reported, bool)
        and abs(float(reported) - deadline) <= 0.01
        and isinstance(remaining, (int, float))
        and not isinstance(remaining, bool)
        and math.isfinite(float(remaining))
        and float(remaining) < 2
        and isinstance(source, str)
        and source in rows
    )


def _valid_github_capacity_evidence(resource: Any, now_value: float) -> bool:
    if not isinstance(resource, Mapping):
        return False
    capacity = resource.get("github_capacity")
    if not isinstance(capacity, Mapping):
        return False
    throttle = capacity.get("throttle")
    if not isinstance(throttle, Mapping):
        return False
    observed = capacity.get("observed_at")
    reset_at = capacity.get("reset_at_epoch")
    resource_name = capacity.get("resource")
    reset_in = throttle.get("reset_in_s")
    remaining = throttle.get("remaining")
    limit = throttle.get("limit")
    return (
        throttle.get("state") == "shed"
        and throttle.get("action") == "defer"
        and isinstance(throttle.get("resource"), str)
        and bool(throttle.get("resource"))
        and resource_name == throttle.get("resource")
        and isinstance(throttle.get("reason"), str)
        and bool(throttle.get("reason"))
        and isinstance(throttle.get("source"), str)
        and bool(throttle.get("source"))
        and type(remaining) is int
        and remaining >= 0
        and type(limit) is int
        and limit > 0
        and type(reset_in) is int
        and reset_in > 0
        and isinstance(observed, (int, float))
        and not isinstance(observed, bool)
        and math.isfinite(float(observed))
        and 0 <= now_value - float(observed) <= 1800
        and isinstance(reset_at, (int, float))
        and not isinstance(reset_at, bool)
        and math.isfinite(float(reset_at))
        and abs(float(reset_at) - (float(observed) + reset_in)) <= 1
        and float(reset_at) > now_value
    )


def _bounded(value: Any, *, limit: int = 128 * 1024) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > limit:
        raise ValueError(f"pipeline JSON exceeds {limit} bytes")
    return json.loads(encoded)


def _carry_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Retain only facts needed to resume/reconcile across generations."""
    keys = (
        "status",
        "stage",
        "revision",
        "identity",
        "owner",
        "progress_key",
        "transitions",
        "work_key",
        "metrics",
        "error",
        "deferred_reason",
        "deferred_next_trigger",
        "pending_kind",
        "pending_evidence",
        "resource_evidence",
        "carried",
    )
    return {**{key: item[key] for key in keys if key in item}, "results": {}}


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "runs": {}, "carry": {}, "cache": {}}


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read pipeline state: {exc}") from exc
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != SCHEMA
        or not isinstance(doc.get("runs"), dict)
    ):
        raise ValueError("malformed pipeline state")
    doc.setdefault("carry", {})
    doc.setdefault("cache", {})
    return doc


def _save(path: Path, doc: Mapping[str, Any]) -> None:
    encoded = json.dumps(doc, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 8 * 1024 * 1024:
        raise ValueError("pipeline state exceeds 8 MiB; retain large evidence externally")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _capacity(capacity_fn: Callable[[], Mapping[str, Any]]) -> dict[str, int]:
    report = capacity_fn()
    if not isinstance(report, Mapping):
        raise ValueError("capacity_fn must return an object with fresh io/low/exception capacity")
    result: dict[str, int] = {}
    for lane, ceiling in CEILINGS.items():
        if lane not in report:
            raise ValueError(f"capacity report is missing the {lane} lane; unknown is not zero")
        value = report[lane]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"capacity {lane} must be a non-negative integer")
        result[lane] = min(value, ceiling)
    return result


def run_pipeline(
    state_path: str | Path,
    targets: Iterable[str],
    *,
    prepare: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    interpret: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    act: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    exception: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    reconcile: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    capacity_fn: Callable[[], Mapping[str, Any]],
    deadline: float,
    max_io_workers: int = 4,
    max_low_workers: int = 2,
    max_exception_workers: int = 1,
    round_deadline_exception: type[BaseException] | None = None,
    round_capacity_exception: type[BaseException] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run independent targets with per-stage bounded pools and durable receipts.

    ``deadline`` is an epoch timestamp. Restarting a run preserves its first
    deadline and all completed stage values. A process interruption after an
    action was durably marked ``running`` parks that item as ``uncertain``; the
    action adapter must reconcile it through the existing queue before retry.
    """
    path = Path(state_path)
    target_list = list(targets)
    if len(set(target_list)) != len(target_list) or any(
        not isinstance(t, str) or not t for t in target_list
    ):
        raise ValueError("targets must be unique non-empty strings")
    if len(target_list) > MAX_TARGETS:
        raise ValueError(
            f"at most {MAX_TARGETS} assigned targets fit the bounded coordinator window"
        )
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise ValueError("deadline must be an epoch timestamp")
    bounds = {"io": max_io_workers, "low": max_low_workers, "exception": max_exception_workers}
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in bounds.values()):
        raise ValueError("pool ceilings must be positive integers")
    callbacks = {
        "prepare": (prepare, "io"),
        "interpret": (interpret, "low"),
        "exception": (exception, "exception"),
        "act": (act, "io"),
    }
    if reconcile is not None:
        callbacks["reconcile"] = (reconcile, "io")
    assignment = sorted(target_list)
    run_guard = path.with_name(path.name + ".runlock")
    run_guard.parent.mkdir(parents=True, exist_ok=True)
    run_lock = run_guard.open("a+")
    acquired = False
    while now() < float(deadline):
        try:
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except BlockingIOError:
            time.sleep(min(0.025, max(0.0, float(deadline) - now())))
    if not acquired:
        run_lock.close()
        return {
            "schema": "closer-pipeline-receipt/v1",
            "authority": False,
            "status": "deferred",
            "reason": "coordinator_lock_unavailable",
            "continuation_required": True,
            "finished_at": now(),
            "targets": {},
        }
    with _locked(path):
        try:
            state = _load(path)
        except Exception:
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
            run_lock.close()
            raise
        candidates = [
            (key, value)
            for key, value in state["runs"].items()
            if float(value.get("deadline", 0)) > now()
            and any(
                row.get("status") not in {"completed", "deferred", "failed"}
                for row in value.get("targets", {}).values()
            )
        ]
        if candidates:
            run_id, run = max(candidates, key=lambda pair: float(pair[1].get("created", 0)))
            run["carry_targets"] = sorted(set(run.get("targets", {})) - set(assignment))
            run["assignment"] = assignment
        else:
            if state["runs"]:
                previous = max(state["runs"].values(), key=lambda row: float(row.get("created", 0)))
                previous_assignment = previous.get("assignment", [])
                if float(previous.get("deadline", 0)) <= now():
                    for old_item in previous.get("targets", {}).values():
                        if old_item.get("status") == "queued":
                            old_item.update(
                                status="deferred",
                                deferred_reason="deadline_exhausted",
                                deferred_next_trigger="next_round",
                            )
                        elif old_item.get("status") == "running":
                            if old_item.get("stage") in {"act", "reconcile"}:
                                old_item.update(
                                    status="uncertain",
                                    deferred_next_trigger="reconcile_before_retry",
                                )
                            else:
                                old_item.update(
                                    status="deferred",
                                    deferred_reason="deadline_exhausted",
                                    deferred_next_trigger="next_round",
                                )
                    _save(path, state)
                # Resource proofs authorize stopping only as of the completed prior
                # round. A later cadence may arrive after their freshness window; use
                # the recorded receipt time to validate that historical stop, then
                # carry deferred targets into a new prepare stage for fresh admission.
                summary = previous.get("receipt_summary")
                prior_finished_at = (
                    summary.get("finished_at") if isinstance(summary, Mapping) else None
                )
                prior_stop_now = (
                    (lambda finished=float(prior_finished_at): finished)
                    if isinstance(prior_finished_at, (int, float))
                    and not isinstance(prior_finished_at, bool)
                    and math.isfinite(float(prior_finished_at))
                    and float(prior_finished_at) >= float(previous.get("created", 0))
                    and float(prior_finished_at) <= now()
                    else now
                )
                prior_stop = validate_stop(path, previous_assignment, now=prior_stop_now)
                if not prior_stop.get("valid"):
                    fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
                    run_lock.close()
                    return {
                        "schema": "closer-pipeline-receipt/v1",
                        "authority": False,
                        "status": "blocked",
                        "reason": "prior_run_has_no_valid_stop",
                        "prior_stop": prior_stop,
                        "continuation_required": True,
                        "finished_at": now(),
                        "targets": {},
                    }
            run_id = _digest([assignment, now()])
            run = state["runs"].setdefault(
                run_id,
                {
                    "targets": {},
                    "assignment": assignment,
                    "deadline": float(deadline),
                    "created": now(),
                    "metrics": {"capacity_samples": [], "peak_active": {}},
                },
            )
            latest: dict[str, dict[str, Any]] = {}
            for old_run in sorted(
                state["runs"].values(), key=lambda row: float(row.get("created", 0))
            ):
                for old_target, old_item in old_run.get("targets", {}).items():
                    if old_item.get("status") not in {"completed", "failed", "deferred"}:
                        latest[old_target] = old_item
            for old_target, old_item in state["carry"].items():
                latest[old_target] = old_item
            carried = []
            for old_target, old_item in latest.items():
                item = _bounded(old_item)
                if item.get("status") == "running":
                    item.update(status="uncertain" if item.get("stage") == "act" else "pending")
                if old_target in assignment:
                    if item.get("status") != "uncertain":
                        item.update(status="queued", stage="prepare")
                else:
                    if item.get("status") not in {"uncertain", "pending"}:
                        item.update(status="queued", stage="prepare")
                    item["carried"] = True
                    carried.append(old_target)
                run["targets"][old_target] = item
            run["carry_targets"] = sorted(carried)
            # Keep only a small run history; unresolved targets are additionally
            # copied into the top-level carry ledger before old runs are trimmed.
            state["runs"] = {run_id: run}
            state["carry"] = {}
        # A resumed invocation cannot silently extend its original budget.
        active_deadline = min(float(deadline), float(run["deadline"]))
        for target in target_list:
            item = run["targets"].setdefault(
                target,
                {
                    "status": "queued",
                    "stage": "prepare",
                    "results": {},
                    "metrics": {"waits": 0, "errors": 0},
                },
            )
            # A process can disappear after launching a callback. Its outcome is
            # unknown; never automatically replay an action from that state.
            if item.get("status") == "running":
                item["status"] = (
                    "uncertain" if item.get("stage") in {"act", "reconcile"} else "pending"
                )
                item["recovered_at"] = now()
                if item["status"] == "uncertain":
                    item["deferred_next_trigger"] = "reconcile_before_retry"
            if item.get("status") == "uncertain" and reconcile is not None:
                item.update(status="queued", stage="reconcile")
            if item.get("status") == "pending" and not item.get("carried"):
                item.update(status="queued", stage="prepare")
        for item in run["targets"].values():
            if item.get("status") == "uncertain" and reconcile is not None:
                item.update(status="queued", stage="reconcile")
            elif item.get("status") == "pending":
                item.update(status="queued", stage="prepare")
        _save(path, state)

    executors = {
        lane: ThreadPoolExecutor(max_workers=bounds[lane], thread_name_prefix=f"closer-{lane}")
        for lane in bounds
    }
    futures: dict[Future[Any], tuple[str, str, str, str | None, float]] = {}
    active: dict[str, int] = {lane: 0 for lane in bounds}
    started = now()
    capacity_samples: list[dict[str, Any]] = []
    peak_active: dict[str, int] = {lane: 0 for lane in bounds}
    io_turn = "ready"
    round_budget_event: dict[str, Any] | None = None

    def mutate(fn: Callable[[dict[str, Any], dict[str, Any]], Any]) -> Any:
        with _locked(path):
            doc = _load(path)
            current = doc["runs"][run_id]
            out = fn(current, doc)
            try:
                _save(path, doc)
            except ValueError as exc:
                if "exceeds 8 MiB" not in str(exc):
                    raise
                # Keep an honest durable failure receipt if accumulated callback
                # evidence would exceed the coordinator's state budget. An act
                # whose result cannot be persisted remains uncertain.
                fallback = _load(path)
                fallback_item = fallback["runs"][run_id]["targets"].get(target)
                if not isinstance(fallback_item, dict):
                    raise
                fallback_item["results"] = {}
                fallback_item["metrics"]["errors"] = fallback_item["metrics"].get("errors", 0) + 1
                if stage in {"act", "reconcile"}:
                    fallback_item.update(
                        status="uncertain",
                        deferred_next_trigger="reconcile_before_retry",
                        error="state_budget_prevented_action_receipt",
                    )
                else:
                    fallback_item.update(
                        status="failed",
                        deferred_next_trigger="human_review",
                        error="state_budget_exhausted",
                    )
                if cache_key:
                    fallback.get("cache", {}).pop(cache_key, None)
                _save(path, fallback)
            return out

    try:
        while True:
            if now() >= active_deadline and not futures:
                break
            capacity_error = None
            try:
                cap = _capacity(capacity_fn)
                capacity_samples.append({"at": now(), **cap})
            except Exception as exc:
                capacity_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                cap = {lane: 0 for lane in bounds}
                capacity_samples.append({"at": now(), "error": capacity_error, **cap})
            # Resolve completions immediately and persist before finding more work.
            done = [f for f in futures if f.done()]
            for future in done:
                target, stage, key, cache_key, launched = futures.pop(future)
                lane = callbacks[stage][1]
                active[lane] -= 1
                elapsed = max(0.0, now() - launched)
                try:
                    value = _bounded(future.result(), limit=CALLBACK_RESULT_BYTES)
                    if not isinstance(value, dict):
                        raise ValueError("callback must return an object")
                    error = None
                except Exception as exc:  # recorded as evidence, never acceptance
                    if (
                        stage == "prepare"
                        and round_deadline_exception is not None
                        and isinstance(exc, round_deadline_exception)
                    ):
                        event = {
                            "type": "round_deadline_exhausted",
                            "reported_deadline": getattr(exc, "deadline", None),
                            "remaining_seconds": getattr(exc, "remaining_seconds", None),
                            "source_target": target,
                        }
                        if round_budget_event is None:
                            round_budget_event = event
                        value, error = {"_round_deadline_exhausted": event}, None
                    elif (
                        stage == "prepare"
                        and round_capacity_exception is not None
                        and isinstance(exc, round_capacity_exception)
                    ):
                        throttle = getattr(exc, "throttle", None)
                        resource_name = getattr(exc, "resource", None)
                        observed_at = getattr(exc, "observed_at", None)
                        reset_at = getattr(exc, "reset_at_epoch", None)
                        evidence = {
                            "github_capacity": {
                                "throttle": throttle,
                                "resource": resource_name,
                                "observed_at": observed_at,
                                "reset_at_epoch": reset_at,
                            }
                        }
                        if _valid_github_capacity_evidence(evidence, now()):
                            value, error = {"_round_capacity_deferred": evidence}, None
                        else:
                            value, error = {}, "RoundCapacityDeferred without valid SHED proof"
                    else:
                        value, error = {}, f"{type(exc).__name__}: {str(exc)[:500]}"

                def finish(current: dict[str, Any], doc: dict[str, Any]) -> None:
                    item = current["targets"][target]
                    if item.get("work_key") != key or item.get("status") != "running":
                        item["metrics"]["incorrect_transitions"] = (
                            item["metrics"].get("incorrect_transitions", 0) + 1
                        )
                        return
                    item["metrics"].setdefault("stage_seconds", {})[stage] = elapsed
                    if now() > active_deadline:
                        item["metrics"]["late_seconds"] = max(0.0, now() - active_deadline)
                    if value.get("_not_started_deadline") is True:
                        item.update(
                            status="deferred",
                            deferred_reason="deadline_exhausted",
                            deferred_next_trigger="next_round",
                            resource_evidence={
                                "type": "active_deadline",
                                "deadline": float(run["deadline"]),
                                "observed_at": now(),
                            },
                        )
                        return
                    if value.get("_round_deadline_exhausted"):
                        item.update(
                            status="queued",
                            stage="prepare",
                            error="typed_round_deadline_exhausted",
                            round_budget_event=value["_round_deadline_exhausted"],
                        )
                        item["metrics"]["round_budget_exhaustions"] = (
                            item["metrics"].get("round_budget_exhaustions", 0) + 1
                        )
                        return
                    if value.get("_round_capacity_deferred"):
                        evidence = value["_round_capacity_deferred"]
                        capacity = evidence["github_capacity"]
                        reset_at = float(capacity["reset_at_epoch"])
                        item.update(
                            status="deferred",
                            deferred_reason="github_capacity",
                            deferred_next_trigger=f"retry_after_github_reset:{reset_at:.3f}",
                            resource_evidence=evidence,
                        )
                        return
                    if error:
                        item["metrics"]["errors"] += 1
                        item["error"] = error
                        item["deferred_next_trigger"] = (
                            "reconcile_before_retry"
                            if stage in {"act", "reconcile"}
                            else "human_review"
                        )
                        # An action may have taken effect despite an exception.
                        item["status"] = "uncertain" if stage in {"act", "reconcile"} else "failed"
                        if stage == "act" and reconcile is not None:
                            item.update(status="queued", stage="reconcile")
                            item.pop("deferred_next_trigger", None)
                        else:
                            item["stage"] = stage
                        return
                    try:
                        _bounded({**item.get("results", {}), stage: value}, limit=64 * 1024)
                    except ValueError:
                        doc.get("cache", {}).pop(key, None)
                        item.update(
                            status="failed",
                            error="callback_results_too_large",
                            deferred_next_trigger="human_review",
                        )
                        item["metrics"]["errors"] += 1
                        return
                    item["results"][stage] = value
                    if stage in {"interpret", "exception"} and cache_key:
                        # Keep the newest semantic result per target/stage. The
                        # coordinator state byte limit bounds storage; a small
                        # fixed entry count would churn on queues larger than
                        # the arbitrary cache size and repeat model work.
                        for old_key, entry in list(doc["cache"].items()):
                            if (
                                entry.get("target") == target
                                and entry.get("stage") == stage
                                and old_key != cache_key
                            ):
                                doc["cache"].pop(old_key, None)
                        doc["cache"][cache_key] = {
                            "value": value,
                            "saved_at": now(),
                            "target": target,
                            "stage": stage,
                            "revision": item.get("revision"),
                            "identity": item.get("identity", target),
                        }
                    item.pop("error", None)
                    item["status"] = "queued"
                    if stage == "prepare":
                        prior_revision = item.get("revision")
                        item["revision"] = value.get("revision")
                        item["identity"] = value.get("identity", target)
                        if isinstance(value.get("owner"), str):
                            item["owner"] = value["owner"]
                        if prior_revision != item["revision"]:
                            item["results"].pop("interpret", None)
                            item["results"].pop("exception", None)
                        item["stage"] = "interpret"
                    elif stage == "interpret":
                        # Admission guards may stop before any provider call. Preserve
                        # that exact event dependency instead of escalating it to the
                        # strong-worker lane or advancing to an action.
                        if (
                            value.get("status") == "pending"
                            and value.get("pending_kind")
                            in {"ci", "async_operation", "external_event"}
                            and isinstance(value.get("next_trigger"), str)
                            and isinstance(value.get("pending_evidence"), Mapping)
                        ):
                            evidence_value = value.get("pending_evidence")
                            kind_value = value.get("pending_kind")
                            evidence = (
                                evidence_value if isinstance(evidence_value, Mapping) else None
                            )
                            kind = kind_value if isinstance(kind_value, str) else ""
                            id_fields = {
                                "ci": "check_run_id",
                                "async_operation": "operation_id",
                                "external_event": "event_id",
                            }
                            has_id = id_fields.get(kind)
                            event_id = (
                                evidence.get(has_id) if evidence is not None and has_id else None
                            )
                            if evidence is not None and isinstance(event_id, str) and event_id:
                                # A pending interpretation is a wake-up point,
                                # not a reusable semantic decision. The next
                                # invocation must rerun the admission check.
                                if cache_key:
                                    doc.get("cache", {}).pop(cache_key, None)
                                item.update(
                                    status="pending",
                                    pending_kind=kind,
                                    deferred_next_trigger=value["next_trigger"],
                                    pending_evidence=dict(evidence),
                                )
                                return
                        if (
                            value.get("status") == "deferred"
                            and value.get("reason") == "provider_failure"
                            and isinstance(value.get("next_trigger"), str)
                        ):
                            evidence = value.get("resource_evidence")
                            if isinstance(evidence, Mapping) and _valid_provider_failure(evidence):
                                # This observed dispatcher run failed at transport or
                                # provider admission. It is not substantive ambiguity;
                                # don't invoke the strong lane and don't cache a
                                # failed profile decision across the next round.
                                if cache_key:
                                    doc.get("cache", {}).pop(cache_key, None)
                                item.update(
                                    status="deferred",
                                    deferred_reason="provider_failure",
                                    deferred_next_trigger=value["next_trigger"],
                                    resource_evidence=dict(evidence),
                                )
                            else:
                                item.update(
                                    status="failed",
                                    error="provider_failure_without_valid_run_evidence",
                                    deferred_next_trigger="human_review",
                                )
                            return
                        needs = value.get("substantive_needs")
                        if needs is None:
                            needs = ["unknown_or_unstructured_interpretation"]
                        if not isinstance(needs, list):
                            needs = ["unknown_or_unstructured_interpretation"]
                            item["results"][stage]["substantive_needs"] = needs
                        if needs:
                            item["stage"] = "exception"
                        else:
                            item["stage"] = "act"
                    elif stage == "exception":
                        # Senior output routes work only; it cannot certify completion.
                        if value.get("status") == "deferred":
                            resource = value.get("resource_evidence")
                            reason = value.get("reason")
                            if (
                                reason == "worker_capacity"
                                and isinstance(value.get("next_trigger"), str)
                                and isinstance(resource, Mapping)
                                and _valid_worker_capacity(resource, now())
                            ):
                                item.update(
                                    status="deferred",
                                    deferred_reason=reason,
                                    deferred_next_trigger=value["next_trigger"],
                                    resource_evidence=dict(resource),
                                )
                            else:
                                item.update(
                                    status="failed",
                                    error="exception_deferral_without_valid_capacity_evidence",
                                    deferred_next_trigger="human_review",
                                )
                        else:
                            item["stage"] = "act"
                    elif stage == "reconcile":
                        outcome = value.get("outcome")
                        if outcome == "confirmed_completed":
                            if value.get("target_complete") is True:
                                item["status"] = "completed"
                            elif (
                                isinstance(value.get("progress_key"), str) and value["progress_key"]
                            ):
                                item.update(
                                    status="queued",
                                    stage="prepare",
                                    progress_key=value["progress_key"],
                                )
                            elif value.get("pending_kind") and value.get("next_trigger"):
                                item.update(
                                    status="pending",
                                    pending_kind=value["pending_kind"],
                                    deferred_next_trigger=value["next_trigger"],
                                )
                            else:
                                item.update(
                                    status="failed",
                                    error="reconcile completion needs terminal or continuation proof",
                                )
                        elif outcome == "confirmed_not_started":
                            item.update(status="queued", stage="act")
                        else:
                            item.update(status="uncertain", stage="reconcile")
                    else:
                        status = value.get("status")
                        if status == "completed":
                            if value.get("target_complete") is True:
                                item["status"] = "completed"
                            else:
                                progress = value.get("progress_key")
                                if not isinstance(progress, str) or not progress:
                                    item.update(
                                        status="failed",
                                        error="act requires target_complete or progress_key",
                                        deferred_next_trigger="human_review",
                                    )
                                    return
                                if progress == item.get("progress_key"):
                                    item.update(
                                        status="parked",
                                        deferred_reason="no_progress",
                                        deferred_next_trigger=value.get(
                                            "next_trigger", "fresh_observation"
                                        ),
                                    )
                                else:
                                    item["progress_key"] = progress
                                    item["transitions"] = item.get("transitions", 0) + 1
                                    if item["transitions"] > 12:
                                        item.update(
                                            status="parked",
                                            deferred_reason="transition_limit",
                                            deferred_next_trigger="fresh_round",
                                        )
                                        return
                                    item.update(status="queued", stage="prepare")
                        elif status == "deferred":
                            item.update(
                                status="deferred",
                                deferred_reason=value.get("reason"),
                                deferred_next_trigger=value.get("next_trigger"),
                                resource_evidence=value.get("resource_evidence"),
                            )
                        elif status == "pending":
                            item.update(
                                status="pending",
                                pending_kind=value.get("pending_kind"),
                                deferred_next_trigger=value.get("next_trigger"),
                                pending_evidence=value.get("pending_evidence"),
                                stage="act",
                            )
                        else:
                            item.update(
                                status="failed",
                                error="act returned unsupported or missing status",
                                deferred_next_trigger="human_review",
                            )

                mutate(finish)

            if now() >= active_deadline:
                if futures:
                    wait(tuple(futures), timeout=0.05, return_when=FIRST_COMPLETED)
                    continue
                break
            if round_budget_event is not None:
                if futures:
                    timeout = max(0.0, active_deadline - now())
                    wait(
                        tuple(futures),
                        timeout=None if timeout == 0 else timeout,
                        return_when=FIRST_COMPLETED,
                    )
                    continue
                remaining = active_deadline - now()
                if remaining > 0:
                    time.sleep(remaining)
                break
            # A target can have only one stage in flight. Fair round-robin order
            # rotates by target and stage age so slow early items do not block others.
            with _locked(path):
                state = _load(path)
                current = state["runs"][run_id]
                rows = current["targets"]
                base_order = sorted(rows, key=lambda t: (rows[t].get("last_scheduled", 0), t))
                io_prepare: list[str] = []
                io_ready: list[str] = []
                other_lanes: list[str] = []
                for candidate in base_order:
                    candidate_item = rows[candidate]
                    candidate_stage = candidate_item.get("stage")
                    if candidate_item.get("status") != "queued" or candidate_stage not in callbacks:
                        continue
                    candidate_lane = callbacks[candidate_stage][1]
                    if candidate_lane != "io":
                        other_lanes.append(candidate)
                    elif candidate_stage == "prepare":
                        io_prepare.append(candidate)
                    else:
                        io_ready.append(candidate)
                # IO shares capacity across preparation and guarded act/reconcile
                # work. Alternate classes when both are ready: this lets the
                # first prepared targets advance while the rest of the assignment
                # is still entering the queue, without starving preparation.
                ordered: list[str] = []
                io_slots = max(0, min(cap["io"], bounds["io"]) - active["io"])
                if io_slots and not io_ready:
                    # Keep one IO slot available while eligible cheap/strong
                    # interpretation is in flight. Its result may make an act
                    # actionable immediately; consuming the last lane with a
                    # new prepare would recreate a stage barrier on slow prep.
                    advancing_work = any(
                        item.get("status") in {"queued", "running"}
                        and item.get("stage") in {"interpret", "exception"}
                        and cap[callbacks[item["stage"]][1]] > 0
                        for item in rows.values()
                    )
                    if advancing_work:
                        io_slots -= 1
                while (io_prepare or io_ready) and len(ordered) < io_slots:
                    if io_prepare and io_ready:
                        chosen = io_turn
                        io_turn = "prepare" if chosen == "ready" else "ready"
                    elif io_prepare:
                        chosen = "prepare"
                    else:
                        chosen = "ready"
                    source = io_prepare if chosen == "prepare" else io_ready
                    ordered.append(source.pop(0))
                ordered.extend(other_lanes)
                for target in ordered:
                    if now() >= active_deadline:
                        break
                    item = rows[target]
                    if item.get("status") != "queued":
                        continue
                    stage = item.get("stage")
                    if stage not in callbacks:
                        continue
                    callback, lane = callbacks[stage]
                    if active[lane] >= min(cap[lane], bounds[lane]):
                        item["metrics"]["waits"] += 1
                        continue
                    context = {
                        **item.get("results", {}),
                        "revision": item.get("revision"),
                        "identity": item.get("identity", target),
                        "deadline": active_deadline,
                        "state_path": str(path),
                        "action_state_path": str(
                            path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json"
                        ),
                        "work_key": _digest(
                            [
                                target,
                                stage,
                                item.get("revision"),
                                item.get("identity", target),
                                _digest(
                                    {
                                        name: item.get("results", {}).get(name)
                                        for name in ("prepare", "interpret")
                                        if name == "prepare" or stage == "exception"
                                    }
                                ),
                            ]
                        ),
                    }
                    key = context["work_key"]
                    semantic_token = _semantic_cache_token(item.get("results", {}).get("prepare"))
                    cache_key = None
                    if stage == "interpret" and semantic_token:
                        cache_key = _digest(
                            {
                                "schema": "closer-pipeline-semantic-cache/v1",
                                "target": target,
                                "identity": item.get("identity", target),
                                "stage": stage,
                                "semantic": semantic_token,
                            }
                        )
                    elif stage == "exception" and semantic_token:
                        interpret_result = item.get("results", {}).get("interpret")
                        if isinstance(interpret_result, Mapping):
                            cache_key = _digest(
                                {
                                    "schema": "closer-pipeline-semantic-cache/v1",
                                    "target": target,
                                    "identity": item.get("identity", target),
                                    "stage": stage,
                                    "semantic": semantic_token,
                                    "interpretation": _digest(interpret_result),
                                }
                            )
                    try:
                        safe_context = _bounded(context, limit=256 * 1024)
                    except ValueError:
                        item.update(
                            status="failed",
                            error="callback_context_too_large",
                            deferred_next_trigger="human_review",
                        )
                        item["metrics"]["errors"] += 1
                        _save_state_locked(path, state)
                        continue
                    item.update(status="running", work_key=key, last_scheduled=now())
                    _save_state_locked(path, state)

                    def invoke(cb=callback, name=target, ctx=safe_context):
                        if now() >= active_deadline:
                            return {"_not_started_deadline": True}
                        return cb(name, ctx)

                    cached = state.get("cache", {}).get(cache_key) if cache_key else None
                    if isinstance(cached, Mapping) and isinstance(cached.get("value"), Mapping):
                        future = Future()
                        future.set_result(cached["value"])
                    else:
                        future = executors[lane].submit(invoke)
                    futures[future] = (target, stage, key, cache_key, now())
                    active[lane] += 1
                    peak_active[lane] = max(peak_active[lane], active[lane])
                _save(path, state)
            if not futures:
                # If every queued lane is unavailable, record the fresh capacity
                # sample and yield promptly. Rechecking in a tight loop would
                # rewrite the durable state repeatedly without advancing work.
                with _locked(path):
                    state = _load(path)
                    current_state = state["runs"][run_id]
                    for target, item in current_state["targets"].items():
                        if item.get("status") != "queued" or item.get("stage") not in callbacks:
                            continue
                        lane = callbacks[item["stage"]][1]
                        if capacity_error:
                            item.update(
                                status="failed",
                                error="capacity_unavailable:" + capacity_error,
                                deferred_next_trigger="fresh_capacity_report",
                            )
                        elif cap.get(lane, 0) == 0:
                            item.update(
                                status="deferred",
                                deferred_reason="worker_capacity",
                                deferred_next_trigger="capacity_recheck",
                                resource_evidence={
                                    "capacity": {
                                        "lane": lane,
                                        "active": active[lane],
                                        "limit": 0,
                                        "available": 0,
                                        "required": 1,
                                        "sampled_at": now(),
                                    }
                                },
                            )
                    _save_state_locked(path, state)
                break
            else:
                wait(
                    tuple(futures),
                    timeout=max(0.0, active_deadline - now()),
                    return_when=FIRST_COMPLETED,
                )

        # Mark queued work as budget-exhausted; running mutations stay uncertain.
        def exhaust(current: dict[str, Any], doc: dict[str, Any]) -> None:
            for item in current["targets"].values():
                if item.get("status") == "queued":
                    observed_at = now()
                    event = item.pop("round_budget_event", None) or round_budget_event
                    evidence: dict[str, Any] = {
                        "type": "active_deadline",
                        "deadline": float(run["deadline"]),
                        "observed_at": observed_at,
                    }
                    if isinstance(event, Mapping):
                        evidence = {
                            "type": "round_deadline_exhausted",
                            "deadline": float(run["deadline"]),
                            "reported_deadline": event.get("reported_deadline"),
                            "remaining_seconds": event.get("remaining_seconds"),
                            "source_target": event.get("source_target"),
                            "observed_at": observed_at,
                        }
                    item.update(
                        status="deferred",
                        deferred_reason="deadline_exhausted",
                        deferred_next_trigger="next_round",
                        resource_evidence=evidence,
                    )
                elif item.get("status") == "running":
                    if item.get("stage") in {"act", "reconcile"}:
                        item.update(
                            status="uncertain",
                            deferred_next_trigger="reconcile_before_retry",
                        )
                    else:
                        item.update(
                            status="pending",
                            deferred_next_trigger="next_round",
                        )

        mutate(exhaust)
    finally:
        for executor in executors.values():
            executor.shutdown(wait=True, cancel_futures=False)

    with _locked(path):
        state = _load(path)
        run = state["runs"][run_id]
        run.setdefault("metrics", {}).update(
            capacity_samples=capacity_samples[-100:],
            peak_active=peak_active,
            throughput_per_minute=(
                sum(1 for item in run["targets"].values() if item.get("status") == "completed")
                / max((now() - started) / 60.0, 1 / 60.0)
            ),
        )
        carry = state.setdefault("carry", {})
        for target, item in run["targets"].items():
            if item.get("status") == "completed":
                carry.pop(target, None)
            else:
                carry[target] = _carry_item(item)
        receipt = {
            "schema": "closer-pipeline-receipt/v1",
            "run_id": run_id,
            "deadline": run["deadline"],
            "elapsed_seconds": max(0.0, now() - started),
            "finished_at": now(),
            "overrun_seconds": max(0.0, now() - float(run["deadline"])),
            "active_cutoff": active_deadline,
            "active_overrun_seconds": max(0.0, now() - active_deadline),
            "metrics": _bounded(run["metrics"]),
            "assignment": assignment,
            "carry_targets": run.get("carry_targets", []),
            "targets": _bounded(run["targets"], limit=8 * 1024 * 1024),
        }
        run["receipt_summary"] = {key: value for key, value in receipt.items() if key != "targets"}
        run.pop("receipt", None)
        _save(path, state)
        fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
        run_lock.close()
        return receipt


def _save_state_locked(path: Path, state: Mapping[str, Any]) -> None:
    """Persist a state already read while holding the coordinator lock."""
    _save(path, state)


def validate_stop(
    state_path: str | Path,
    assignment_targets: Iterable[str] | None = None,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Validate outer pipeline accounting and whether continuation is due.

    This deliberately accepts only ``closer-pipeline/v1``. Legacy
    ``closer-queue/v1`` state remains owned by closer_queue.validate_stop.
    """
    path = Path(state_path)
    reasons: list[str] = []
    try:
        state = _load(path)
    except (ValueError, OSError) as exc:
        return {"valid": False, "stop_valid": False, "reasons": [f"invalid_state:{exc}"]}
    try:
        supplied = list(assignment_targets) if assignment_targets is not None else None
    except TypeError:
        supplied = []
        reasons.append("invalid_assignment")
    matches = [
        (key, value)
        for key, value in state["runs"].items()
        if isinstance(value, Mapping)
        and (supplied is None or value.get("assignment") == sorted(supplied))
    ]
    if not matches:
        return {
            "valid": False,
            "stop_valid": False,
            "reasons": reasons + ["no_matching_pipeline_run"],
        }
    run_id, run = max(matches, key=lambda pair: float(pair[1].get("created", 0)))
    targets = run.get("assignment")
    if not isinstance(targets, list) or not isinstance(run.get("targets"), dict):
        return {
            "valid": False,
            "stop_valid": False,
            "reasons": reasons + ["invalid_run_accounting"],
        }
    if supplied is not None and sorted(supplied) != targets:
        reasons.append("assignment_changed")
    deadline = run.get("deadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        reasons.append("invalid_deadline")
        deadline = float(now())
    rows = run["targets"]
    carry_targets = run.get("carry_targets", [])
    accounted_targets = set(targets) | (
        set(carry_targets) if isinstance(carry_targets, list) else set()
    )
    if set(rows) != accounted_targets:
        reasons.append("assignment_not_fully_accounted")
    continuation = False
    pending_targets = []
    for target in rows:
        item = rows.get(target)
        if not isinstance(item, Mapping):
            reasons.append(f"unaccounted:{target}")
            continue
        status = item.get("status")
        stage = item.get("stage")
        if status == "completed":
            continue
        continuation = True
        if status == "deferred" and item.get("deferred_reason") == "deadline_exhausted":
            if now() < float(deadline):
                reasons.append(f"premature_deadline_deferral:{target}")
            if not _valid_deadline_evidence(item.get("resource_evidence"), float(deadline), rows):
                reasons.append(f"unverified_deadline_boundary:{target}")
            continue
        if status == "pending":
            pending_kind = item.get("pending_kind")
            trigger = item.get("deferred_next_trigger")
            evidence = item.get("pending_evidence")
            if (
                pending_kind not in {"ci", "async_operation", "external_event"}
                or not isinstance(trigger, str)
                or not trigger.strip()
                or not isinstance(evidence, Mapping)
                or not evidence
            ):
                reasons.append(f"unverifiable_pending_work:{target}")
            elif pending_kind == "ci" and not evidence.get("check_run_id"):
                reasons.append(f"unverifiable_pending_work:{target}")
            elif pending_kind == "async_operation" and not evidence.get("operation_id"):
                reasons.append(f"unverifiable_pending_work:{target}")
            elif pending_kind == "external_event" and not evidence.get("event_id"):
                reasons.append(f"unverifiable_pending_work:{target}")
            else:
                pending_targets.append(target)
            continue
        if status in {"uncertain", "running", "queued"}:
            if (
                status == "uncertain"
                and now() >= float(deadline)
                and item.get("deferred_next_trigger") == "reconcile_before_retry"
            ):
                continue
            reasons.append(f"actionable_pipeline_work:{target}:{stage}")
            continue
        if status in {"failed", "parked"}:
            reasons.append(f"unresolved_pipeline_work:{target}:{status}")
            continue
        if status == "deferred":
            reason, trigger = item.get("deferred_reason"), item.get("deferred_next_trigger")
            allowed = {
                "deadline_exhausted",
                "claim_held",
                "worker_capacity",
                "provider_failure",
                "operation_running",
                "repo_operation_running",
                "receipt_reserve",
                "pending_ci",
                "github_capacity",
            }
            if reason not in allowed or not isinstance(trigger, str) or not trigger:
                reasons.append(f"unverified_deferral:{target}")
            elif reason == "deadline_exhausted" and now() < float(deadline):
                reasons.append(f"premature_deadline_deferral:{target}")
            elif reason == "deadline_exhausted" and not _valid_deadline_evidence(
                item.get("resource_evidence"), float(deadline), rows
            ):
                reasons.append(f"unverified_deadline_boundary:{target}")
            elif reason in {"claim_held"} and not (
                isinstance(item.get("resource_evidence"), Mapping)
                and item["resource_evidence"].get("holder")
            ):
                reasons.append(f"unverified_resource_deferral:{target}")
            elif reason in {"operation_running", "repo_operation_running"} and not (
                isinstance(item.get("resource_evidence"), Mapping)
                and item["resource_evidence"].get("operation_id")
            ):
                reasons.append(f"unverified_resource_deferral:{target}")
            elif reason == "worker_capacity" and not _valid_worker_capacity(
                item.get("resource_evidence"), now()
            ):
                reasons.append(f"unverified_resource_deferral:{target}")
            elif reason == "pending_ci" and not (
                isinstance(item.get("resource_evidence"), Mapping)
                and item["resource_evidence"].get("check_run_id")
            ):
                reasons.append(f"unverified_resource_deferral:{target}")
            elif reason == "provider_failure" and not _valid_provider_failure(
                item.get("resource_evidence")
            ):
                reasons.append(f"unverified_provider_failure:{target}")
            elif reason == "github_capacity" and not _valid_github_capacity_evidence(
                item.get("resource_evidence"), now()
            ):
                reasons.append(f"unverified_github_capacity_deferral:{target}")
            elif reason == "github_capacity":
                evidence = item.get("resource_evidence")
                capacity = (
                    evidence.get("github_capacity") if isinstance(evidence, Mapping) else None
                )
                throttle = capacity.get("throttle") if isinstance(capacity, Mapping) else None
                expected = (
                    f"retry_after_github_reset:{float(capacity['reset_at_epoch']):.3f}"
                    if isinstance(capacity, Mapping)
                    and isinstance(capacity.get("reset_at_epoch"), (int, float))
                    and isinstance(throttle, Mapping)
                    else None
                )
                if trigger != expected:
                    reasons.append(f"unmatched_github_capacity_trigger:{target}")
            continue
        reasons.append(f"invalid_target_status:{target}:{status}")
    if pending_targets and any(
        rows[target].get("status") not in {"completed", "pending", "deferred"} for target in rows
    ):
        reasons.append("pending_work_is_not_the_only_blocker")
    valid = not reasons
    return {
        "valid": valid,
        "stop_valid": valid,
        "run_id": run_id,
        "continuation_required": continuation,
        "deadline": float(deadline),
        "reasons": reasons,
    }
