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
import threading
import time
import weakref
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import closer_resources

SCHEMA = "closer-pipeline/v1"
UNBOUND_UNCERTAIN_PROVIDER_ERROR = "provider_outcome_uncertainty_without_valid_bound_run"
STAGES = ("prepare", "interpret", "exception", "act", "reconcile")
CEILINGS = {"io": 8, "low": 4, "exception": 2}
MAX_TARGETS = 1024
CALLBACK_RESULT_BYTES = 32 * 1024
PRELAUNCH_ROUTE_BUSY_ERROR = (
    "RuntimeError: low provider route remained internally busy through deadline"
)
SEMANTIC_CACHE_PREFIX = "closer-evidence-semantic/v1:"
INNER_WAIT_SCHEMA = "closer-inner-wait/v1"
NONPASS_WAIT_SCHEMA = "closer-ordinary-nonpass-wait/v1"
NONPASS_WAIT_PREFIX = "ordinary NON_PASS disposition retained: "
READ_BARRIER_SCHEMA = "closer-unverified-read-barrier/v1"
READ_BARRIER_ERROR = "expired read deferral lacks a verified terminal resource boundary"
READ_ORIGIN_BARRIER_ERROR = "expired read retained a historical wait from another revision"
READ_INTERRUPTED_BARRIER_ERROR = "interrupted read lacks a verified terminal boundary"
READ_BARRIER_TRIGGER = "reconcile_retained_read_before_retry"

# Finite mode is supported only for the concrete callbacks constructed inside the
# production dispatcher. A code-object or caller-supplied marker is insufficient:
# this weak set records the actual function objects created by that exact driver
# invocation, before resource admission can write an intent.
_FINITE_DISPATCHER_CALLBACKS: weakref.WeakSet = weakref.WeakSet()


def _register_dispatcher_finite_callbacks(callbacks: Mapping[str, Any]) -> None:
    """Register actual dispatcher-created callbacks; never a public opt-in flag."""
    import inspect

    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    dispatcher_globals = caller.f_globals if caller is not None else {}
    driver = dispatcher_globals.get("_run_staged_pipeline")
    if caller is None or driver is None or caller.f_code is not driver.__code__:
        raise ValueError("finite callbacks may only be registered by the production dispatcher")
    launcher = caller.f_back
    main_admitted = dispatcher_globals.get("_main_admitted")
    backend = caller.f_locals.get("backend")
    if (
        launcher is None
        or main_admitted is None
        or launcher.f_code is not main_admitted.__code__
        or launcher.f_locals.get("backend") is not backend
        or launcher.f_globals is not dispatcher_globals
        or type(backend) is not dispatcher_globals.get("Closer")
        or type(getattr(backend, "gh", None)) is not dispatcher_globals.get("GitHub")
    ):
        raise ValueError("finite callbacks require the exact CLI-constructed dispatcher backend")
    if backend is None:
        raise ValueError("finite callbacks require a dispatcher backend")
    import gh_capacity

    if (
        backend.offload_fn is not None
        or backend.run_fn is not dispatcher_globals.get("bounded_run")
        or backend.gh.api_fn is not gh_capacity.gh_run
        or backend.worker.__func__ is not dispatcher_globals["Closer"].worker
        or backend.select.__func__ is not dispatcher_globals["Closer"].select
        or getattr(backend.blocker_fn, "__code__", None) is None
        or backend.blocker_fn.__globals__ is not dispatcher_globals
        or backend.blocker_fn.__code__.co_qualname != "_main_admitted.<locals>.read_blockers"
        or getattr(backend.assignment_guard_fn, "__code__", None) is None
        or backend.assignment_guard_fn.__globals__ is not dispatcher_globals
        or backend.assignment_guard_fn.__code__.co_qualname != "_main_admitted.<locals>.<lambda>"
    ):
        raise ValueError("finite dispatcher backend contains an unsupported transport override")
    expected = {"prepare", "interpret", "exception", "act"}
    if set(callbacks) not in (expected, expected | {"reconcile"}):
        raise ValueError("finite dispatcher callback set is incomplete")
    source = str(Path(dispatcher_globals["__file__"]).resolve())
    driver_args = caller.f_locals.get("args")
    selected_frontier = caller.f_locals.get("finite_frontier")
    for name in sorted(expected):
        callback = callbacks[name]
        code = getattr(callback, "__code__", None)
        closure = dict(zip(code.co_freevars, callback.__closure__ or ())) if code else {}
        captured = {key: cell.cell_contents for key, cell in closure.items()}
        if (
            not callable(callback)
            or code is None
            or callback.__globals__ is not dispatcher_globals
            or Path(code.co_filename).resolve() != Path(source)
            or code.co_qualname != f"_run_staged_pipeline.<locals>.{name}"
            or ("backend" in captured and captured["backend"] is not backend)
            or ("args" in captured and captured["args"] is not driver_args)
            or ("finite_frontier" in captured and captured["finite_frontier"] != selected_frontier)
        ):
            raise ValueError(f"finite {name} callback is not the dispatcher-owned stage driver")
    reconcile = callbacks.get("reconcile")
    if reconcile is not None and (
        not callable(reconcile)
        or getattr(reconcile, "__globals__", None) is not dispatcher_globals
        or getattr(getattr(reconcile, "__code__", None), "co_qualname", None)
        != "_run_staged_pipeline.<locals>.reconcile_action"
    ):
        raise ValueError("finite reconcile callback is not dispatcher-owned")
    for callback in callbacks.values():
        if callback is not None:
            _FINITE_DISPATCHER_CALLBACKS.add(callback)


def _require_dispatcher_finite_callbacks(callbacks: Mapping[str, Any]) -> None:
    expected = ("prepare", "interpret", "exception", "act")
    if any(callbacks.get(name) not in _FINITE_DISPATCHER_CALLBACKS for name in expected):
        raise ValueError(
            "finite frontier requires the exact registered production dispatcher callbacks"
        )
    reconcile = callbacks.get("reconcile")
    if reconcile is not None and reconcile not in _FINITE_DISPATCHER_CALLBACKS:
        raise ValueError("finite reconcile requires the registered production dispatcher callback")


def _interrupted_read(previous: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Recognize receipt-less reads without treating queued exception work as proven."""
    return previous.get("receipt_summary") is None and (
        (
            item.get("status") in {"queued", "running"}
            and item.get("stage") in {"prepare", "interpret"}
        )
        or (item.get("status") == "running" and item.get("stage") == "exception")
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _native_route_result(value: Mapping[str, Any]) -> bool:
    classification = value.get("classification")
    return isinstance(classification, str) and classification.startswith("deterministic_")


def _nonpass_wait_store(path: Path) -> Path:
    return path.parent / f"{path.stem}.wait-proofs"


def _valid_nonpass_wait_record(record, target, revision, snapshot, reason, trigger) -> bool:
    """A helper-accounted NON_PASS may prove waiting, never acceptance or an operation."""
    import closer_dispatcher
    import closer_disposition
    import closer_evidence

    if not isinstance(record, Mapping) or not isinstance(snapshot, Mapping):
        return False
    disposition, verifier = record.get("disposition"), snapshot.get("verifier")
    if not isinstance(disposition, Mapping) or not isinstance(verifier, Mapping):
        return False
    subject = closer_evidence._subject_target(dict(snapshot))
    verifier_subject = f"{verifier.get('repo')}#{verifier.get('pr')}"
    if (
        record.get("schema") != NONPASS_WAIT_SCHEMA
        or record.get("target") != target
        or record.get("revision") != revision
        or record.get("reason") != reason
        or record.get("next_trigger") != trigger
        or snapshot.get("target") != target
        or snapshot.get("revision") != revision
        or snapshot.get("kind") != "pr"
        or snapshot.get("state") != "MERGED"
        or not isinstance(reason, str)
        or not reason.startswith(NONPASS_WAIT_PREFIX)
        or not isinstance(trigger, str)
        or not trigger.strip()
        or disposition.get("acceptance_authority") is not False
        or disposition.get("repair") is not None
        or record.get("disposition_sha256") != _digest(disposition)
        or record.get("verifier") != verifier
        or verifier.get("schema") != "verifier-corpus-decision/v1"
        or verifier.get("verdict") != "NON_PASS"
        or verifier.get("head_sha") != snapshot.get("head")
        or not closer_dispatcher.TARGET.fullmatch(verifier_subject)
        or verifier_subject != subject
        or not isinstance(verifier.get("evaluated_sha"), str)
        or not closer_dispatcher.SHA.fullmatch(verifier["evaluated_sha"])
        or verifier.get("evaluated_sha") != snapshot.get("merge")
        or any(
            not str(verifier.get(field, "")).isdigit() or int(str(verifier[field])) <= 0
            for field in ("run_id", "run_attempt")
        )
        or any(
            record.get(field) != snapshot.get(field)
            or disposition.get(field) != snapshot.get(field)
            for field in ("head", "base", "review_revision")
        )
        or disposition.get("source_digest")
        != closer_dispatcher.source_digest(snapshot.get("sources", []))
    ):
        return False
    raw = disposition.get("raw_verdicts")
    resolutions = disposition.get("resolutions")
    actions = disposition.get("next_guarded_actions")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(row, dict) and isinstance(row.get("raw"), dict) for row in raw)
        or any(row.get("finding_id") != closer_disposition._finding_id(row["raw"]) for row in raw)
        or not any(
            row["raw"] == {"source": "native_verifier", "verdict": "NON_PASS", "raw": verifier}
            for row in raw
        )
        or not isinstance(resolutions, list)
        or not all(isinstance(row, dict) for row in resolutions)
        or sorted(row.get("finding_id", "") for row in resolutions)
        != sorted(row["finding_id"] for row in raw)
        or not isinstance(actions, list)
        or actions != [row.get("next_guarded_action") for row in resolutions]
        or any(action not in {"wait", "collect_evidence"} for action in actions)
    ):
        return False
    return all(
        row.get("status")
        in {
            "unresolved_missing_evidence",
            "unresolved_mechanical",
            "resolved_duplicate",
            "resolved_nonissue",
        }
        and (
            not row["status"].startswith("unresolved_")
            or (isinstance(row.get("reason"), str) and bool(row["reason"].strip()))
        )
        for row in resolutions
    )


EVIDENCE_REFUSAL_REASONS = frozenset(
    {"substantive_or_ambiguous_finding", "source_coverage_incomplete"}
)
EVIDENCE_REFUSAL_TRIGGER = "source owner supplies current-head proof or resolves the named finding"


def _valid_inner_wait(
    pipeline_path: Path,
    target: str,
    revision: Any,
    snapshot: Any,
    reason: Any,
    trigger: Any,
    proof: Any,
    *,
    frontier_id: str | None = None,
    finite_frontier: Mapping[str, Any] | None = None,
    now: Callable[[], float] = time.time,
) -> bool:
    """Bind a terminal inner wait to its durable, current one-target receipt."""
    if not isinstance(snapshot, Mapping) or not isinstance(proof, Mapping):
        return False
    head = snapshot.get("head")
    if (
        proof.get("schema") != INNER_WAIT_SCHEMA
        or proof.get("target") != target
        or proof.get("revision") != revision
        or proof.get("head") != head
        or snapshot.get("target") != target
        or snapshot.get("revision") != revision
        or not isinstance(revision, str)
        or not revision
        or not isinstance(reason, str)
        or not isinstance(trigger, str)
        or not trigger.strip()
        or proof.get("reason") != reason
        or proof.get("next_trigger") != trigger
        or not isinstance(proof.get("receipt_sha256"), str)
        or len(proof["receipt_sha256"]) != 64
    ):
        return False
    if head is None:
        if snapshot.get("kind") != "issue":
            return False
    elif (
        not isinstance(head, str)
        or len(head) != 40
        or any(char not in "0123456789abcdef" for char in head)
    ):
        return False
    if reason == "worker_json_invalid" or reason in EVIDENCE_REFUSAL_REASONS:
        if trigger != EVIDENCE_REFUSAL_TRIGGER:
            return False
    elif reason.startswith("current item-scoped hold: "):
        if trigger == "wait_for_trigger" or trigger == "resume_target_from_current_evidence":
            return False
    elif reason.startswith(NONPASS_WAIT_PREFIX):
        if not isinstance(proof.get("ordinary_nonpass_ref"), Mapping):
            return False
    else:
        return False

    import closer_queue

    proof_frontier_id = proof.get("frontier_id")
    proof_frontier = proof.get("finite_frontier")
    if (
        frontier_id is not None and (not isinstance(frontier_id, str) or not frontier_id.strip())
    ) or (
        proof_frontier_id is not None
        and (not isinstance(proof_frontier_id, str) or not proof_frontier_id.strip())
    ):
        return False
    if frontier_id is not None and proof_frontier_id not in {None, frontier_id}:
        return False
    selected_frontier_id = frontier_id or proof_frontier_id
    if selected_frontier_id is not None and (
        not isinstance(selected_frontier_id, str) or not selected_frontier_id.strip()
    ):
        return False
    if selected_frontier_id is not None and (
        not isinstance(proof_frontier, Mapping)
        or proof_frontier.get("frontier_id") != selected_frontier_id
        or (finite_frontier is not None and dict(proof_frontier) != dict(finite_frontier))
    ):
        return False
    inner_path = pipeline_path.parent / f"{pipeline_path.stem}.actions" / f"{_digest(target)}.json"
    if proof.get("inner_path") != str(inner_path) or not inner_path.is_file():
        return False
    try:
        with closer_queue._ledger_lock(inner_path, cutoff=float(now()) + 1, now=now) as locked:
            if not locked:
                return False
            state = closer_queue._read_state(inner_path)
            if selected_frontier_id is None:
                round_state = state.get("round", {})
                items = state.get("items", {})
                validation_state = state
            else:
                selected_state = closer_queue.state_for_frontier(
                    state,
                    selected_frontier_id,
                    expected_policy=finite_frontier or proof_frontier,
                )
                if selected_state is None:
                    return False
                validation_state = selected_state
                round_state = validation_state["round"]
                items = validation_state["items"]
            receipt = round_state.get("receipt")
            item = items.get(target) if isinstance(items, Mapping) else None
            if not isinstance(receipt, Mapping) or not isinstance(item, Mapping):
                return False
            checked = closer_queue._validate_data(
                validation_state,
                receipt,
                assignment_targets=[target],
                now_value=float(now()),
                holder_fn=closer_queue.claims.holder,
            )
            if not checked.get("stop_valid") or not checked.get("valid"):
                return False
            rows = receipt.get("accounting")
            if (
                round_state.get("status") != "finished"
                or receipt.get("schema") != closer_queue.RECEIPT_SCHEMA
                or receipt.get("stop_valid") is not True
                or receipt.get("assignment_targets") != [target]
                or not isinstance(rows, list)
                or len(rows) != 1
                or rows[0] != closer_queue._account_row(target, item)
                or _digest(receipt) != proof["receipt_sha256"]
                or round_state.get("source_revisions", {}).get(target) != revision
            ):
                return False
            row = rows[0]
            if (
                row.get("target") != target
                or row.get("revision") != revision
                or row.get("action") != "wait"
                or row.get("eligible") is not False
                or row.get("requires_coordinator") is not False
                or row.get("status") not in {"deferred", "ineligible"}
                or row.get("reason") != reason
                or row.get("next_trigger") != trigger
                or row.get("observation_freshness") != "fresh"
                or row.get("operation_id") is not None
                or row.get("operation_status") is not None
                or item.get("evidence", {}).get("head") != head
            ):
                return False
            history = item.get("operation_history", [])
            if not isinstance(history, list):
                return False
            if reason in EVIDENCE_REFUSAL_REASONS and (
                item.get("operation") is not None
                or history
                or receipt.get("progress") != []
                or receipt.get("eligible_remaining") != []
            ):
                # A refusal is durable scheduling evidence only. It cannot
                # reconcile an operation or establish criterion acceptance.
                return False
            for operation in [item.get("operation"), *history]:
                if operation is None:
                    continue
                if not isinstance(operation, Mapping) or operation.get("status") not in {
                    "completed",
                    "failed",
                    "confirmed_not_started",
                    "abandoned",
                }:
                    return False
            if reason.startswith("current item-scoped hold: "):
                labels = set(item.get("evidence", {}).get("labels", []))
                for source in item.get("evidence", {}).get("sources", []):
                    if isinstance(source, Mapping):
                        labels.update(source.get("labels", []))
                holds = labels.intersection(
                    {
                        "agents:auto-pilot-pause",
                        "agents:paused",
                        "agent:paused",
                        "do-not-merge",
                        "hold",
                        "needs-human",
                    }
                )
                if reason != f"current item-scoped hold: {sorted(holds)}" or not holds:
                    return False
            elif reason.startswith(NONPASS_WAIT_PREFIX):
                import closer_disposition

                raw = closer_disposition._safe_raw(
                    proof.get("ordinary_nonpass_ref"), _nonpass_wait_store(pipeline_path)
                )
                if raw is None or not _valid_nonpass_wait_record(
                    json.loads(raw), target, revision, item.get("evidence"), reason, trigger
                ):
                    return False
            return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


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


def _valid_uncertain_provider_outcome(value: Any, target: str, item: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "uncertain":
        return False
    outcome = value.get("provider_outcome")
    if not isinstance(outcome, Mapping) or outcome.get("schema") != (
        "closer-provider-outcome-uncertain/v1"
    ):
        return False
    head = outcome.get("head")
    revision = item.get("revision")
    if (
        outcome.get("target") != target
        or not isinstance(revision, str)
        or not revision.strip()
        or outcome.get("revision") != revision
        or outcome.get("identity") != item.get("identity", target)
    ):
        return False
    prepared = item.get("results", {}).get("prepare", {})
    if not isinstance(prepared, Mapping):
        return False
    snapshot = prepared.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return False
    if head is None:
        # Standalone issue snapshots have no PR head. Their exact preparation
        # revision plus target/identity is the binding that survives a restart.
        if snapshot.get("kind") != "issue" or snapshot.get("head") is not None:
            return False
    elif (
        not isinstance(head, str)
        or len(head) != 40
        or any(char not in "0123456789abcdef" for char in head)
        or snapshot.get("head") != head
    ):
        return False
    semantic = _semantic_cache_token(prepared)
    if not semantic or outcome.get("semantic_cache_key") != semantic:
        return False
    actual = outcome.get("actual")
    if not isinstance(actual, Mapping):
        return False
    identities = [actual.get(field) for field in ("agent", "requested_profile", "run_id")]
    return (
        all(isinstance(value, str) and bool(value.strip()) for value in identities)
        and type(actual.get("exit")) is int
        and actual.get("exit") != 0
        and actual.get("outcome_status") == "uncertain"
        and actual.get("retry_safe") is False
    )


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


AUDIT_ARCHIVE_SCHEMA = "closer-invalid-stop-audit-archive/v1"
NATIVE_WAIT_AUDIT_SCHEMA = "closer-native-wait-continuation-audit/v1"


def _audit_archive_dir(path: Path) -> Path:
    return path.with_name(path.name + ".audit")


def _archived_history(path: Path, reference: Any) -> list[dict[str, Any]]:
    """Verify every state-owned immutable audit chunk before relying on it."""
    if reference is None:
        return []
    if (
        not isinstance(reference, Mapping)
        or reference.get("schema") != AUDIT_ARCHIVE_SCHEMA
        or not isinstance(reference.get("sha256"), str)
        or type(reference.get("count")) is not int
        or reference["count"] < 1
    ):
        raise ValueError("invalid pipeline audit archive reference")
    directory = _audit_archive_dir(path)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("missing or redirected pipeline audit archive")
    sha = reference["sha256"]
    seen: set[str] = set()
    chunks: list[list[dict[str, Any]]] = []
    total = 0
    while sha is not None:
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or sha in seen
            or len(seen) >= 100_000
        ):
            raise ValueError("invalid pipeline audit archive chain")
        seen.add(sha)
        source = directory / f"{sha}.json"
        if source.is_symlink() or not source.is_file():
            raise ValueError("missing or redirected pipeline audit archive chunk")
        raw = source.read_bytes()
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("tampered pipeline audit archive chunk")
        try:
            chunk = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("malformed pipeline audit archive chunk") from exc
        entries = chunk.get("entries") if isinstance(chunk, Mapping) else None
        if (
            not isinstance(chunk, Mapping)
            or chunk.get("schema") != AUDIT_ARCHIVE_SCHEMA
            or not isinstance(entries, list)
            or not entries
            or not all(isinstance(entry, dict) for entry in entries)
        ):
            raise ValueError("invalid pipeline audit archive chunk")
        total += len(entries)
        if total > reference["count"]:
            raise ValueError("pipeline audit archive count mismatch")
        chunks.append(entries)
        sha = chunk.get("previous_sha256")
    if total != reference["count"]:
        raise ValueError("pipeline audit archive count mismatch")
    return [entry for chunk in reversed(chunks) for entry in chunk]


def _resolved_audit_history(path: Path, previous: Mapping[str, Any]) -> list[dict[str, Any]]:
    local = previous.get("invalid_stop_history", [])
    if not isinstance(local, list) or not all(isinstance(entry, dict) for entry in local):
        raise ValueError("invalid pipeline audit history")
    archived = _archived_history(path, previous.get("invalid_stop_archive"))
    if archived and (
        not local
        or local[0].get("schema")
        not in {"closer-expired-invalid-round-recovery/v1", NATIVE_WAIT_AUDIT_SCHEMA}
    ):
        raise ValueError("pipeline audit archive lost its origin")
    return local[:1] + archived + local[1:] if archived else local


def _stage_audit_history(
    path: Path,
    previous: Mapping[str, Any],
    new_entry: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Archive overflow first; the caller then atomically saves the new ledger ref."""
    _resolved_audit_history(path, previous)
    local = list(previous.get("invalid_stop_history", []))
    if new_entry is not None:
        local.append(dict(new_entry))
    reference = previous.get("invalid_stop_archive")
    try:
        _bounded(local, limit=512 * 1024)
        return local, dict(reference) if isinstance(reference, Mapping) else None
    except ValueError:
        if len(local) < 2:
            raise
    entries = local[1:]
    previous_sha = reference.get("sha256") if isinstance(reference, Mapping) else None
    chunk = {
        "schema": AUDIT_ARCHIVE_SCHEMA,
        "previous_sha256": previous_sha,
        "entries": entries,
    }
    raw = _canonical(chunk)
    if len(raw) > 1024 * 1024:
        raise ValueError("pipeline audit archive chunk too large")
    sha = hashlib.sha256(raw).hexdigest()
    directory = _audit_archive_dir(path)
    if directory.is_symlink():
        raise ValueError("redirected pipeline audit archive")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{sha}.json"
    if destination.is_symlink():
        raise ValueError("redirected pipeline audit archive chunk")
    if destination.exists():
        if destination.read_bytes() != raw:
            raise ValueError("conflicting pipeline audit archive chunk")
    else:
        fd, temporary = tempfile.mkstemp(prefix=".audit-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
    count = (reference.get("count", 0) if isinstance(reference, Mapping) else 0) + len(entries)
    return local[:1], {"schema": AUDIT_ARCHIVE_SCHEMA, "sha256": sha, "count": count}


def _carry_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Retain only facts needed to resume/reconcile across generations."""
    if item.get("deferred_reason") == "scope_quarantined":
        # A not-started proof binds the full inputs and the exact original row.
        # Compacting it would destroy the only lineage permitting fresh admission.
        return _bounded(item, limit=256 * 1024)
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
        "provider_outcome_uncertain",
        "recovery_barrier",
        "recovery_excluded",
        "known_completed_action",
        "carried",
        "scope_proof",
    )
    carried = {key: item[key] for key in keys if key in item}
    results: dict[str, Any] = {}
    if item.get("status") == "uncertain" and item.get("stage") in {"interpret", "exception"}:
        outcome = item.get("provider_outcome_uncertain")
        if isinstance(outcome, Mapping):
            prepared = item.get("results", {}).get("prepare", {})
            prepared_snapshot = prepared.get("snapshot") if isinstance(prepared, Mapping) else None
            kind = prepared_snapshot.get("kind") if isinstance(prepared_snapshot, Mapping) else None
            results["prepare"] = {
                "revision": item.get("revision"),
                "identity": item.get("identity"),
                "semantic_cache_key": outcome.get("semantic_cache_key"),
                "snapshot": {"head": outcome.get("head"), "kind": kind},
            }
    return {**carried, "results": results}


def _retained_blocked_act_wait_proof(path: Path, target: str, item: Mapping[str, Any]) -> bool:
    """Validate a retained blocked action as an actual non-mutating inner wait."""
    results = item.get("results")
    if not isinstance(results, Mapping):
        return False
    act = results.get("act")
    prepared = results.get("prepare")
    if not isinstance(act, Mapping) or not isinstance(prepared, Mapping):
        return False
    receipt = act.get("queue_receipt")
    if (
        act.get("status") != "blocked"
        or not isinstance(receipt, Mapping)
        or receipt.get("schema") != "closer-queue-receipt/v1"
        or receipt.get("stop_valid") is not True
        or receipt.get("valid") is not True
    ):
        return False
    rows = receipt.get("accounting")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return False
    row = rows[0]
    reason, trigger = row.get("reason"), row.get("next_trigger")
    if act.get("reason") != reason or act.get("next_trigger") != trigger:
        return False
    if (
        isinstance(reason, str)
        and reason in EVIDENCE_REFUSAL_REASONS
        and (item.get("provider_outcome_uncertain") or item.get("known_completed_action") is True)
    ):
        return False
    inner_path = path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json"
    snapshot = prepared.get("snapshot")
    proof = {
        "schema": INNER_WAIT_SCHEMA,
        "target": target,
        "revision": item.get("revision"),
        "head": snapshot.get("head") if isinstance(snapshot, Mapping) else None,
        "reason": reason,
        "next_trigger": trigger,
        "inner_path": str(inner_path),
        "receipt_sha256": _digest(receipt),
    }
    return _valid_inner_wait(path, target, item.get("revision"), snapshot, reason, trigger, proof)


def _legacy_failed_act_wait_proof(path: Path, target: str, item: Mapping[str, Any]) -> bool:
    """Recover only the pre-fix adapter failure backed by its original inner receipt."""
    return (
        item.get("status") == "failed"
        and item.get("stage") == "act"
        and item.get("error") == "act returned unsupported or missing status"
        and _retained_blocked_act_wait_proof(path, target, item)
    )


def _validated_wait_audit_origin(history, target, item):
    """Find the immutable origin for an already native-validated retained wait."""
    results = item.get("results")
    act = results.get("act") if isinstance(results, Mapping) else None
    if (
        not isinstance(act, Mapping)
        or not isinstance(item.get("identity"), str)
        or not item["identity"].strip()
        or not isinstance(item.get("revision"), str)
        or not item["revision"]
    ):
        return None
    for record in history:
        if not isinstance(record, Mapping):
            continue
        if record.get("schema") not in {
            "closer-expired-invalid-round-recovery/v1",
            NATIVE_WAIT_AUDIT_SCHEMA,
        }:
            continue
        source = record.get("source_run_id")
        rows = record.get("targets")
        row = rows.get(target) if isinstance(rows, Mapping) else None
        digest = row.get("item_sha256") if isinstance(row, Mapping) else None
        if (
            isinstance(source, str)
            and bool(source.strip())
            and isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
            and row.get("recovery") == "validated_inner_wait"
            and row.get("act_result") == act
            and row.get("revision") == item.get("revision")
            and row.get("identity") == item.get("identity")
        ):
            return {"previous_run_id": source, "previous_item_sha256": digest}
    return None


def _native_wait_origin_record(path, target, item, previous, source_run_id, prior_stop, now):
    """Record a current observed no-operation boundary; never label it expired."""
    if (
        item.get("known_completed_action") is True
        or item.get("provider_outcome_uncertain")
        or item.get("recovery_barrier") is not None
        or not _legacy_failed_act_wait_proof(path, target, item)
    ):
        return None
    try:
        act = _bounded(item["results"]["act"], limit=32 * 1024)
        row = {
            "item_sha256": _digest(item),
            "status": item.get("status"),
            "stage": item.get("stage"),
            "revision": item.get("revision"),
            "identity": item.get("identity"),
            "work_key": item.get("work_key"),
            "error": item.get("error"),
            "resource_evidence": item.get("resource_evidence"),
            "provider_outcome_uncertain": item.get("provider_outcome_uncertain"),
            "recovery_barrier": item.get("recovery_barrier"),
            "act_result": act,
            "act_receipt_sha256": _digest(act["queue_receipt"]),
            "recovery": "validated_inner_wait",
        }
        return _bounded(
            {
                "schema": NATIVE_WAIT_AUDIT_SCHEMA,
                "boundary": "validated_nonmutating_native_wait",
                "acceptance_authority": False,
                "source_run_id": source_run_id,
                "source_created": previous.get("created"),
                "source_deadline": previous.get("deadline"),
                "source_assignment": list(previous.get("assignment", [])),
                "recorded_at": now(),
                "prior_stop": prior_stop,
                "targets": {target: row},
            },
            limit=512 * 1024,
        )
    except (ValueError, TypeError, KeyError):
        return None


def _historical_validated_wait_result(
    previous: Mapping[str, Any],
    target: str,
    item: Mapping[str, Any],
    history: list[dict[str, Any]],
) -> bool:
    """Identify a stale, audited non-mutating action behind a fresh read result."""
    results = item.get("results")
    if not isinstance(results, Mapping):
        return False
    prepared = results.get("prepare")
    act_result = results.get("act")
    recovered = previous.get("prior_invalid_stop_evidence")
    return (
        isinstance(prepared, Mapping)
        and prepared.get("revision") == item.get("revision")
        and isinstance(act_result, Mapping)
        and isinstance(recovered, Mapping)
        and target in recovered.get("recovered_inner_wait_targets", [])
        and any(
            isinstance(record, Mapping)
            and isinstance(record.get("targets"), Mapping)
            and isinstance(record["targets"].get(target), Mapping)
            and record["targets"][target].get("recovery") == "validated_inner_wait"
            and record["targets"][target].get("act_result") == act_result
            and record["targets"][target].get("revision") != item.get("revision")
            for record in history
        )
    )


def _audited_retained_wait(
    path: Path,
    previous: Mapping[str, Any],
    target: str,
    item: Mapping[str, Any],
    history: list[dict[str, Any]],
    historical_now: float,
    source_run_id: str,
) -> bool:
    """Prove an original no-operation wait for read retry or origin preservation.

    A current act/validated_inner_wait row is accepted only so the constructor
    can retain its audited origin when admitting a later read cadence.
    """
    results = item.get("results")
    recovered = previous.get("prior_invalid_stop_evidence")
    recovery = previous.get("recovered_invalid_stop")
    summary = previous.get("receipt_summary")
    finished_at = summary.get("finished_at") if isinstance(summary, Mapping) else None
    # This new failed-read route requires an actual matching terminal receipt;
    # the older deferred-resource paths retain their existing time fallback.
    failed_read_terminal_proven = (
        isinstance(summary, Mapping)
        and summary.get("schema") == "closer-pipeline-receipt/v1"
        and summary.get("run_id") == source_run_id
        and summary.get("deadline") == previous.get("deadline")
        and isinstance(finished_at, (int, float))
        and not isinstance(finished_at, bool)
        and math.isfinite(float(finished_at))
        and float(previous["created"])
        <= float(previous["deadline"])
        <= float(finished_at)
        <= historical_now
    )
    anchor = (
        recovery.get("targets", {}).get(target)
        if isinstance(recovery, Mapping) and isinstance(recovery.get("targets"), Mapping)
        else None
    )
    if (
        not (
            (
                item.get("status") == "deferred"
                and (
                    (
                        item.get("stage") in {"prepare", "interpret", "exception"}
                        and item.get("deferred_reason")
                        in {
                            "deadline_exhausted",
                            "worker_capacity",
                            "provider_failure",
                            "github_capacity",
                        }
                    )
                    or (
                        item.get("stage") == "act"
                        and item.get("deferred_reason") == "validated_inner_wait"
                    )
                )
            )
            or (
                item.get("status") == "failed"
                and item.get("stage") == "interpret"
                and item.get("error") == PRELAUNCH_ROUTE_BUSY_ERROR
                and failed_read_terminal_proven
            )
            or _interrupted_read(previous, item)
        )
        or item.get("known_completed_action") is True
        or item.get("provider_outcome_uncertain")
        or not isinstance(results, Mapping)
        or not isinstance(recovered, Mapping)
        or not isinstance(recovered.get("recovered_inner_wait_targets"), list)
        or not all(
            isinstance(value, str) and value for value in recovered["recovered_inner_wait_targets"]
        )
        or target not in recovered.get("recovered_inner_wait_targets", [])
        or not isinstance(recovery, Mapping)
        or recovery.get("schema") != "closer-recovered-inner-wait/v1"
        or not isinstance(recovery.get("previous_run_id"), str)
        or not recovery["previous_run_id"].strip()
        or not isinstance(anchor, Mapping)
        or not isinstance(anchor.get("previous_run_id", recovery["previous_run_id"]), str)
        or not anchor.get("previous_run_id", recovery["previous_run_id"]).strip()
        or not isinstance(anchor.get("previous_item_sha256"), str)
        or len(anchor["previous_item_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in anchor["previous_item_sha256"])
    ):
        return False
    prepared, act = results.get("prepare"), results.get("act")
    if not isinstance(prepared, Mapping) or not isinstance(act, Mapping):
        return False
    if act.get("status") == "blocked":
        # A compact pre-fix refusal can be restored as scheduling proof, then
        # encounter a new read-only capacity/deadline boundary. Preserve that
        # same immutable native refusal across the next cadence; it still has
        # no acceptance or action authority.
        receipt = act.get("queue_receipt")
        snapshot = prepared.get("snapshot")
        if (
            not isinstance(act.get("reason"), str)
            or act.get("reason") not in EVIDENCE_REFUSAL_REASONS
            or act.get("next_trigger") != EVIDENCE_REFUSAL_TRIGGER
            or not isinstance(receipt, Mapping)
            or not isinstance(snapshot, Mapping)
            or prepared.get("revision", snapshot.get("revision")) != item.get("revision")
            or snapshot.get("revision") != item.get("revision")
            or anchor.get("revision") != item.get("revision")
            or anchor.get("head") != snapshot.get("head")
            or anchor.get("reason") != act.get("reason")
            or anchor.get("next_trigger") != act.get("next_trigger")
            or anchor.get("inner_path")
            != str(path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json")
            or anchor.get("receipt_sha256") != _digest(receipt)
            or not isinstance(item.get("identity"), str)
            or not item["identity"]
            or not any(
                isinstance(record, Mapping)
                and record.get("source_run_id")
                == anchor.get("previous_run_id", recovery["previous_run_id"])
                and isinstance(record.get("targets"), Mapping)
                and isinstance(record["targets"].get(target), Mapping)
                and record["targets"][target].get("recovery") == "validated_inner_wait"
                and record["targets"][target].get("act_result") == act
                and record["targets"][target].get("revision") == item.get("revision")
                and record["targets"][target].get("identity") == item.get("identity")
                and record["targets"][target].get("item_sha256")
                == anchor.get("previous_item_sha256")
                for record in history
            )
        ):
            return False
        return _retained_blocked_act_wait_proof(path, target, item)
    resource = act.get("resource_evidence")
    proof = resource.get("inner_wait") if isinstance(resource, Mapping) else None
    receipt = act.get("queue_receipt")
    if (
        act.get("status") != "deferred"
        or act.get("reason") != "validated_inner_wait"
        or not isinstance(proof, Mapping)
        or not isinstance(receipt, Mapping)
        or proof.get("receipt_sha256") != _digest(receipt)
        or act.get("next_trigger") != proof.get("next_trigger")
        or anchor.get("revision") != item.get("revision")
        or anchor.get("head") != proof.get("head")
        or anchor.get("reason") != act.get("reason")
        or anchor.get("next_trigger") != act.get("next_trigger")
        or anchor.get("inner_path") != proof.get("inner_path")
        or anchor.get("receipt_sha256") != proof.get("receipt_sha256")
        or prepared.get("revision") != item.get("revision")
        or proof.get("revision") != item.get("revision")
        or not isinstance(item.get("identity"), str)
        or not item["identity"]
        or not any(
            isinstance(record, Mapping)
            and record.get("source_run_id")
            == anchor.get("previous_run_id", recovery["previous_run_id"])
            and isinstance(record.get("targets"), Mapping)
            and isinstance(record["targets"].get(target), Mapping)
            and record["targets"][target].get("recovery") == "validated_inner_wait"
            and record["targets"][target].get("act_result") == act
            and record["targets"][target].get("revision") == item.get("revision")
            and record["targets"][target].get("identity") == item.get("identity")
            and record["targets"][target].get("item_sha256") == anchor.get("previous_item_sha256")
            for record in history
        )
    ):
        return False
    return _valid_inner_wait(
        path,
        target,
        item.get("revision"),
        prepared.get("snapshot"),
        proof.get("reason"),
        proof.get("next_trigger"),
        proof,
        frontier_id=proof.get("frontier_id"),
        now=lambda: historical_now,
    )


def _safe_prior_continuation(
    prior_stop: Mapping[str, Any], previous: Mapping[str, Any], path: Path
) -> tuple[bool, bool, set[str]]:
    """Carry only exact native, read-only, or durable pre-fix wait failures."""
    reasons = prior_stop.get("reasons")
    rows = previous.get("targets")
    if prior_stop.get("valid") is not False or not isinstance(reasons, list) or not reasons:
        return False, False, set()
    if not isinstance(rows, Mapping):
        return False, False, set()

    native_targets: set[str] = set()
    recovered_wait_targets: set[str] = set()
    matched_reasons: set[str] = set()
    has_pending_blocker = False
    for reason in reasons:
        if reason == "pending_work_is_not_the_only_blocker":
            has_pending_blocker = True
            matched_reasons.add(reason)
            continue
        if not isinstance(reason, str):
            return False, False, set()
        if reason.startswith("unresolved_native_provider_outcome:"):
            matched = False
            for target, item in rows.items():
                if not isinstance(target, str) or not isinstance(item, Mapping):
                    continue
                if item.get("status") != "uncertain" or item.get("stage") not in {
                    "interpret",
                    "exception",
                }:
                    continue
                interpretation = {
                    "status": "uncertain",
                    "provider_outcome": item.get("provider_outcome_uncertain"),
                }
                if not _valid_uncertain_provider_outcome(interpretation, target, item):
                    continue
                run_id = item["provider_outcome_uncertain"]["actual"]["run_id"]
                expected = f"unresolved_native_provider_outcome:{target}:{run_id}"
                if reason == expected:
                    native_targets.add(target)
                    matched_reasons.add(reason)
                    matched = True
                    break
            if not matched:
                return False, False, set()
            continue
        if reason.startswith("unresolved_pipeline_work:"):
            matched = False
            for target, item in rows.items():
                if not isinstance(target, str) or not isinstance(item, Mapping):
                    continue
                status = item.get("status")
                if reason != f"unresolved_pipeline_work:{target}:{status}":
                    continue
                stage = item.get("stage")
                if (
                    status == "failed"
                    and stage in {"prepare", "interpret", "exception"}
                    and item.get("error") != UNBOUND_UNCERTAIN_PROVIDER_ERROR
                ):
                    matched = True
                elif (
                    status == "parked"
                    and stage == "act"
                    and item.get("known_completed_action") is True
                    and isinstance(item.get("progress_key"), str)
                    and bool(item.get("progress_key"))
                ):
                    matched = True
                elif (
                    status == "failed"
                    and stage == "act"
                    and _legacy_failed_act_wait_proof(path, target, item)
                ):
                    recovered_wait_targets.add(target)
                    matched = True
                if matched:
                    matched_reasons.add(reason)
                    break
            if not matched:
                return False, False, set()
            continue
        return False, False, set()

    if matched_reasons != set(reasons):
        return False, False, set()
    if has_pending_blocker and not any(
        isinstance(item, Mapping) and item.get("status") == "pending" for item in rows.values()
    ):
        return False, False, set()

    # Verify every nonterminal target is explained by one of the exact safe
    # categories above. This prevents a good reason from masking unknown work.
    for target, item in rows.items():
        if not isinstance(target, str) or not isinstance(item, Mapping):
            return False, False, set()
        status = item.get("status")
        if status in {"completed", "deferred", "pending"}:
            continue
        if (
            target in native_targets
            and status == "uncertain"
            and item.get("stage") in {"interpret", "exception"}
        ):
            continue
        if (
            status == "failed"
            and item.get("stage") in {"prepare", "interpret", "exception"}
            and item.get("error") != UNBOUND_UNCERTAIN_PROVIDER_ERROR
        ):
            continue
        if target in recovered_wait_targets and status == "failed" and item.get("stage") == "act":
            continue
        if (
            status == "parked"
            and item.get("stage") == "act"
            and item.get("known_completed_action") is True
            and isinstance(item.get("progress_key"), str)
            and bool(item.get("progress_key"))
        ):
            continue
        return False, False, set()
    return True, bool(native_targets), recovered_wait_targets


def _restored_compact_wait(
    target: str, item: Mapping[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Restore only a derived wait view bound to immutable and current native proof."""
    if not (
        item.get("carried") is True
        and item.get("recovery_excluded") is True
        and item.get("status") == "deferred"
        and item.get("stage") == "act"
        and item.get("deferred_reason") == "validated_inner_wait"
        and item.get("results") == {}
    ):
        return None
    resource = item.get("resource_evidence")
    proof = resource.get("inner_wait") if isinstance(resource, Mapping) else None
    if not isinstance(proof, Mapping):
        return None
    candidates = [
        entry["targets"][target]
        for entry in history
        if isinstance(entry, Mapping)
        and isinstance(entry.get("targets"), Mapping)
        and isinstance(entry["targets"].get(target), Mapping)
        and entry["targets"][target].get("recovery") == "validated_inner_wait"
        and all(
            entry["targets"][target].get(key) == item.get(key)
            for key in ("status", "stage", "revision", "identity", "work_key", "resource_evidence")
        )
        and entry["targets"][target].get("next_trigger") == item.get("deferred_next_trigger")
    ]
    if not candidates:
        return None
    act = candidates[0].get("act_result")
    if (
        not isinstance(act, Mapping)
        or any(row.get("act_result") != act for row in candidates)
        or act.get("status") != "deferred"
        or act.get("reason") != "validated_inner_wait"
        or act.get("resource_evidence") != resource
        or act.get("next_trigger") != item.get("deferred_next_trigger")
        or not isinstance(act.get("queue_receipt"), Mapping)
        or _digest(act["queue_receipt"]) != proof.get("receipt_sha256")
        or any(row.get("act_receipt_sha256") != proof.get("receipt_sha256") for row in candidates)
    ):
        return None
    # This projection is wait proof only, never interpretation or completion evidence.
    snapshot = {
        "target": target,
        "revision": item.get("revision"),
        "head": proof.get("head"),
        "kind": "issue" if proof.get("head") is None else "pr",
        "wait_proof_only": True,
    }
    restored = dict(item)
    restored["results"] = {"prepare": {"snapshot": snapshot}, "act": dict(act)}
    return restored


def _restored_compact_refusal(
    path: Path, target: str, item: Mapping[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Derive scheduling proof from an audited refusal, never model evidence."""
    barrier = item.get("recovery_barrier")
    if not (
        item.get("status") == "failed"
        and item.get("stage") == "act"
        and item.get("error") == "act returned unsupported or missing status"
        and item.get("results") == {}
        and not item.get("provider_outcome_uncertain")
        and item.get("known_completed_action") is not True
        and isinstance(barrier, Mapping)
        and barrier.get("schema") == "closer-unverified-act-barrier/v1"
        and isinstance(barrier.get("source_run_id"), str)
        and bool(barrier["source_run_id"])
        and all(
            isinstance(barrier.get(key), str)
            and len(barrier[key]) == 64
            and all(char in "0123456789abcdef" for char in barrier[key])
            for key in ("item_sha256", "act_result_sha256")
        )
        and all(barrier.get(key) == item.get(key) for key in ("revision", "identity", "work_key"))
    ):
        return None
    candidates = [
        entry["targets"][target]
        for entry in history
        if isinstance(entry, Mapping)
        and entry.get("source_run_id") == barrier.get("source_run_id")
        and isinstance(entry.get("targets"), Mapping)
        and isinstance(entry["targets"].get(target), Mapping)
        and entry["targets"][target].get("item_sha256") == barrier.get("item_sha256")
        and all(
            entry["targets"][target].get(key) == item.get(key)
            for key in ("status", "stage", "error", "revision", "identity", "work_key")
        )
    ]
    if not candidates:
        return None
    act = candidates[0].get("act_result")
    if (
        not isinstance(act, Mapping)
        or any(row.get("act_result") != act for row in candidates)
        or _digest(act) != barrier.get("act_result_sha256")
        or act.get("status") != "blocked"
        or not isinstance(act.get("reason"), str)
        or act.get("reason") not in EVIDENCE_REFUSAL_REASONS
        or act.get("next_trigger") != EVIDENCE_REFUSAL_TRIGGER
        or not isinstance(act.get("queue_receipt"), Mapping)
        or any(row.get("act_receipt_sha256") != _digest(act["queue_receipt"]) for row in candidates)
    ):
        return None
    import closer_queue

    inner_path = path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json"
    try:
        native = closer_queue._read_state(inner_path).get("items", {}).get(target, {})
        evidence = native.get("evidence")
        if not isinstance(evidence, Mapping):
            return None
        # The adapter's original identity commits to target/head/merge. Do not
        # infer a historical head solely from a mutable native evidence field.
        identity = hashlib.sha256(
            json.dumps(
                [target, evidence.get("head"), evidence.get("merge")], sort_keys=True
            ).encode()
        ).hexdigest()
        if (
            identity != item.get("identity")
            or evidence.get("target") != target
            or evidence.get("revision") != item.get("revision")
            or native.get("revision") != item.get("revision")
        ):
            return None
        snapshot = {
            key: evidence.get(key) for key in ("target", "revision", "head", "merge", "kind")
        }
        snapshot.update(wait_proof_only=True, refusal_proof_only=True)
        restored = dict(item)
        restored["results"] = {"prepare": {"snapshot": snapshot}, "act": dict(act)}
        return restored if _legacy_failed_act_wait_proof(path, target, restored) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _inherited_unstarted_wait_deferral(
    previous: Mapping[str, Any],
    target: str,
    item: Mapping[str, Any],
    history: list[dict[str, Any]],
) -> bool:
    """Recognize an inherited read cutoff without validating a later round's stop.

    Callers must independently prove the retained native no-operation wait.
    Scheduling stamps distinguish an unstarted prepare from a running provider
    callback. The exact earlier cutoff and action remain in the immutable audit.
    """
    created, scheduled = previous.get("created"), item.get("last_scheduled")
    resource = item.get("resource_evidence")
    results = item.get("results")
    if (
        item.get("status") != "deferred"
        or item.get("stage") != "prepare"
        or item.get("deferred_reason") != "deadline_exhausted"
        or item.get("deferred_next_trigger") != "next_round"
        or item.get("fresh_inner_wait_admission") is not True
        or item.get("provider_outcome_uncertain")
        or item.get("known_completed_action") is True
        or not isinstance(resource, Mapping)
        or not isinstance(results, Mapping)
        or not isinstance(results.get("act"), Mapping)
        or not isinstance(created, (int, float))
        or not isinstance(scheduled, (int, float))
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (created, scheduled, resource.get("deadline"), resource.get("observed_at"))
        )
        or not float(scheduled) < float(created)
        or not float(resource["deadline"]) <= float(resource["observed_at"]) < float(created)
        or not _valid_deadline_evidence(
            resource, float(resource["deadline"]), previous.get("targets", {})
        )
    ):
        return False
    candidates = [
        record["targets"][target]
        for record in history
        if isinstance(record, Mapping)
        and record.get("source_deadline") == resource["deadline"]
        and isinstance(record.get("source_run_id"), str)
        and bool(record["source_run_id"])
        and isinstance(record.get("targets"), Mapping)
        and isinstance(record["targets"].get(target), Mapping)
    ]
    return bool(candidates) and all(
        row.get("status") == "deferred"
        and row.get("stage") == "prepare"
        and row.get("recovery") == "retry_fresh"
        and row.get("deferred_reason") == "deadline_exhausted"
        and row.get("next_trigger") == "next_round"
        and row.get("resource_evidence") == resource
        and row.get("act_result") == results["act"]
        and all(row.get(key) == item.get(key) for key in ("revision", "identity", "work_key"))
        for row in candidates
    )


def _valid_retained_act_barrier(
    barrier: Any, history: list[dict[str, Any]], identity: Any, target: str
) -> bool:
    """Check an existing action barrier before retaining it inside a read barrier."""
    if barrier is None:
        return True
    if (
        not isinstance(barrier, Mapping)
        or barrier.get("schema") != "closer-unverified-act-barrier/v1"
        or not isinstance(barrier.get("source_run_id"), str)
        or not barrier["source_run_id"]
        or not isinstance(barrier.get("work_key"), str)
        or not barrier["work_key"]
        or any(
            not isinstance(barrier.get(key), str)
            or len(barrier[key]) != 64
            or any(char not in "0123456789abcdef" for char in barrier[key])
            for key in ("item_sha256", "act_result_sha256")
        )
    ):
        return False
    return any(
        record.get("schema") == "closer-expired-invalid-round-recovery/v1"
        and isinstance(record.get("targets"), Mapping)
        and isinstance(record["targets"].get(target), Mapping)
        and record.get("source_run_id") == barrier.get("source_run_id")
        and record["targets"][target].get("item_sha256") == barrier.get("item_sha256")
        and record["targets"][target].get("revision") == barrier.get("revision")
        and record["targets"][target].get("identity") == barrier.get("identity") == identity
        and record["targets"][target].get("work_key") == barrier.get("work_key")
        and _digest(record["targets"][target].get("act_result")) == barrier.get("act_result_sha256")
        for record in history
    )


def _audited_prior_revision_wait(
    path: Path,
    previous: Mapping[str, Any],
    target: str,
    item: Mapping[str, Any],
    history: list[dict[str, Any]],
    historical_now: float,
    source_run_id: str,
) -> bool:
    """Prove only the old no-operation origin; never authorize current-revision work."""
    results = item.get("results")
    prepared = results.get("prepare") if isinstance(results, Mapping) else None
    snapshot = prepared.get("snapshot") if isinstance(prepared, Mapping) else None
    recovery = previous.get("recovered_invalid_stop")
    anchors = recovery.get("targets") if isinstance(recovery, Mapping) else None
    anchor = anchors.get(target) if isinstance(anchors, Mapping) else None
    status, reason = item.get("status"), item.get("deferred_reason")
    pending_kind = item.get("pending_kind")
    pending_field = {
        "ci": "check_run_id",
        "async_operation": "operation_id",
        "external_event": "event_id",
    }.get(pending_kind if isinstance(pending_kind, str) else "")
    pending_evidence = item.get("pending_evidence")
    recognized_read = (
        _interrupted_read(previous, item)
        or (
            status == "deferred"
            and reason
            in {"deadline_exhausted", "worker_capacity", "provider_failure", "github_capacity"}
        )
        or (
            status == "pending"
            and pending_field is not None
            and isinstance(pending_evidence, Mapping)
            and isinstance(pending_evidence.get(pending_field), str)
            and bool(pending_evidence[pending_field])
        )
    )
    if (
        not recognized_read
        or item.get("stage") not in {"prepare", "interpret", "exception"}
        or item.get("fresh_inner_wait_admission") is not True
        or not isinstance(results, Mapping)
        or not isinstance(prepared, Mapping)
        or not isinstance(snapshot, Mapping)
        or not isinstance(anchor, Mapping)
        or not isinstance(anchor.get("revision"), str)
        or not anchor["revision"]
        or not isinstance(item.get("revision"), str)
        or not item["revision"]
        or anchor["revision"] == item.get("revision")
        or prepared.get("revision") != item.get("revision")
        or prepared.get("identity") != item.get("identity")
        or snapshot.get("target") != target
        or snapshot.get("revision") != item.get("revision")
        or snapshot.get("head") != anchor.get("head")
        or not _valid_retained_act_barrier(
            item.get("recovery_barrier"), history, item.get("identity"), target
        )
    ):
        return False
    # This minimal view is used solely to validate the immutable old wait. It
    # is not saved, supplied to a model, or passed to a current action callback.
    origin = {
        **item,
        # This shape selects only the native immutable wait-proof validator.
        # It says nothing about the validity of the current read's boundary.
        "status": "deferred",
        "deferred_reason": "deadline_exhausted",
        "revision": anchor["revision"],
        "results": {
            **results,
            "prepare": {
                "revision": anchor["revision"],
                "snapshot": {
                    "target": target,
                    "revision": anchor["revision"],
                    "head": anchor.get("head"),
                    "kind": snapshot.get("kind"),
                    "wait_proof_only": True,
                },
            },
        },
    }
    return _audited_retained_wait(
        path, previous, target, origin, history, historical_now, source_run_id
    )


def _valid_read_recovery_barrier(
    target: str, item: Mapping[str, Any], history: list[dict[str, Any]]
) -> bool:
    """Retain an audited read barrier without authorizing any callback or retry."""
    barrier = item.get("recovery_barrier")
    results = item.get("results")
    reason = barrier.get("reason") if isinstance(barrier, Mapping) else None
    expected_error = (
        READ_ORIGIN_BARRIER_ERROR
        if reason == "historical_wait_revision_changed"
        else (
            READ_INTERRUPTED_BARRIER_ERROR if reason == "interrupted_read" else READ_BARRIER_ERROR
        )
    )
    if (
        item.get("status") != "failed"
        or item.get("stage") not in {"prepare", "interpret", "exception"}
        or item.get("error") != expected_error
        or item.get("deferred_next_trigger") != READ_BARRIER_TRIGGER
        or item.get("known_completed_action") is True
        or item.get("provider_outcome_uncertain")
        or not isinstance(results, Mapping)
        or not isinstance(barrier, Mapping)
        or barrier.get("schema") != READ_BARRIER_SCHEMA
        or reason
        not in (
            None,
            "terminal_resource_boundary_invalid",
            "historical_wait_revision_changed",
            "interrupted_read",
        )
        or not isinstance(barrier.get("source_run_id"), str)
        or not barrier["source_run_id"]
        or any(
            not isinstance(barrier.get(key), str)
            or len(barrier[key]) != 64
            or any(char not in "0123456789abcdef" for char in barrier[key])
            for key in ("item_sha256", "act_result_sha256")
        )
    ):
        return False
    for record in history:
        targets = record.get("targets")
        row = targets.get(target) if isinstance(targets, Mapping) else None
        if (
            record.get("schema") == "closer-expired-invalid-round-recovery/v1"
            and record.get("source_run_id") == barrier["source_run_id"]
            and isinstance(row, Mapping)
            and row.get("item_sha256") == barrier.get("item_sha256")
            and row.get("recovery") == "quarantine_read"
            and row.get("status")
            in (
                {"queued", "running"}
                if reason == "interrupted_read"
                else (
                    {"deferred", "pending", "queued", "running"}
                    if reason == "historical_wait_revision_changed"
                    else {"deferred"}
                )
            )
            and (
                row.get("status") not in {"queued", "running"}
                or record.get("source_receipt_missing") is True
            )
            and row.get("stage") == item.get("stage")
            and row.get("deferred_reason") == item.get("deferred_reason")
            and (
                reason in {"historical_wait_revision_changed", "interrupted_read"}
                or row.get("deferred_reason") == "deadline_exhausted"
            )
            and row.get("pending_kind") == item.get("pending_kind")
            and row.get("pending_evidence") == item.get("pending_evidence")
            and row.get("resource_evidence") == item.get("resource_evidence")
            and barrier.get("prior_recovery_barrier") == row.get("recovery_barrier")
            and (row.get("read_barrier_reason") or "terminal_resource_boundary_invalid")
            == (reason or "terminal_resource_boundary_invalid")
            and all(
                row.get(key) == item.get(key) == barrier.get(key)
                for key in ("revision", "identity", "work_key")
            )
            and barrier.get("act_result_sha256") == _digest(row.get("act_result"))
            and (not results or results.get("act") == row.get("act_result"))
        ):
            return True
    return False


def _completed_prepare_capacity_observation(
    previous: Mapping[str, Any], item: Any, run_id: str, now_value: float
) -> bool:
    """Bind a result-free read deferral to its terminal scheduling receipt only."""
    if not isinstance(item, Mapping):
        return False
    summary = previous.get("receipt_summary")
    resource = item.get("resource_evidence")
    capacity = resource.get("github_capacity") if isinstance(resource, Mapping) else None
    observed = capacity.get("observed_at") if isinstance(capacity, Mapping) else None
    finished = summary.get("finished_at") if isinstance(summary, Mapping) else None
    return (
        item.get("status") == "deferred"
        and item.get("stage") == "prepare"
        and item.get("deferred_reason") == "github_capacity"
        and item.get("results") == {}
        and item.get("known_completed_action") is not True
        and not item.get("provider_outcome_uncertain")
        and item.get("recovery_barrier") is None
        and isinstance(summary, Mapping)
        and summary.get("schema") == "closer-pipeline-receipt/v1"
        and summary.get("run_id") == run_id
        and summary.get("deadline") == previous["deadline"]
        and isinstance(finished, (int, float))
        and not isinstance(finished, bool)
        and math.isfinite(float(finished))
        and float(previous["created"]) <= finished <= now_value
        and isinstance(observed, (int, float))
        and not isinstance(observed, bool)
        and float(previous["created"]) <= observed <= min(finished, float(previous["deadline"]))
        and isinstance(capacity, Mapping)
        and _valid_github_capacity_evidence(resource, observed)
        and item.get("deferred_next_trigger")
        == f"retry_after_github_reset:{float(capacity['reset_at_epoch']):.3f}"
    )


def _expired_invalid_round_recovery(
    prior_stop: Mapping[str, Any],
    previous: Mapping[str, Any],
    path: Path,
    run_id: str,
    now_value: float,
) -> tuple[bool, set[str], set[str], set[str], set[str], dict[str, Any]]:
    """Admit fresh work after an interrupted round without validating its old stop.

    Only non-action deferrals may be retried. An unproved action is carried as
    a non-runnable barrier; an uncertain action can enter reconciliation only.
    The old invalid reasons and per-item facts remain in the new run's audit.
    """
    reasons = prior_stop.get("reasons")
    rows = previous.get("targets")
    assignment = previous.get("assignment")
    carry_targets = previous.get("carry_targets", [])
    if (
        prior_stop.get("valid") is not False
        or not isinstance(reasons, list)
        or not reasons
        or not isinstance(rows, Mapping)
        or not isinstance(assignment, list)
        or not isinstance(carry_targets, list)
        or not all(isinstance(target, str) and target for target in assignment + carry_targets)
        or len(set(assignment)) != len(assignment)
        or len(set(carry_targets)) != len(carry_targets)
        or set(rows) != set(assignment) | set(carry_targets)
        or not isinstance(previous.get("deadline"), (int, float))
        or isinstance(previous.get("deadline"), bool)
        or not math.isfinite(float(previous["deadline"]))
        or float(previous["deadline"]) > now_value
        or not isinstance(previous.get("created"), (int, float))
        or isinstance(previous.get("created"), bool)
        or not math.isfinite(float(previous["created"]))
        or float(previous["created"]) > float(previous["deadline"])
        or not isinstance(previous.get("invalid_stop_history", []), list)
        or (
            previous.get("receipt_summary")
            and not previous.get("invalid_stop_history")
            and not (
                rows
                and all(
                    _completed_prepare_capacity_observation(previous, item, run_id, now_value)
                    for item in rows.values()
                )
            )
        )
    ):
        return False, set(), set(), set(), set(), {}

    try:
        history = _resolved_audit_history(path, previous)
    except (ValueError, OSError):
        return False, set(), set(), set(), set(), {}

    expected: set[str] = set()
    summary = previous.get("receipt_summary")
    finished_at = summary.get("finished_at") if isinstance(summary, Mapping) else None
    historical_now = (
        float(finished_at)
        if isinstance(finished_at, (int, float))
        and not isinstance(finished_at, bool)
        and math.isfinite(float(finished_at))
        and float(previous["created"]) <= float(finished_at) <= now_value
        else now_value
    )
    retryable: set[str] = set()
    waits: set[str] = set()
    quarantined: set[str] = set()
    changed_wait_origins: set[str] = set()
    uncertain: set[str] = set()
    pending: set[str] = set()
    audited: dict[str, Any] = {}
    restored_waits: dict[str, Any] = {}
    for target, item in rows.items():
        if not isinstance(target, str) or not isinstance(item, Mapping):
            return False, set(), set(), set(), set(), {}
        original_item = item
        restored = _restored_compact_wait(target, item, history) or _restored_compact_refusal(
            path, target, item, history
        )
        if restored is not None:
            item = restored
        status, stage = item.get("status"), item.get("stage")
        results = item.get("results")
        if not isinstance(results, Mapping):
            return False, set(), set(), set(), set(), {}
        no_action = "act" not in results and item.get("known_completed_action") is not True
        retained_read_wait = not no_action and _audited_retained_wait(
            path, previous, target, item, history, historical_now, run_id
        )
        prior_revision_wait = (
            not no_action
            and not retained_read_wait
            and _audited_prior_revision_wait(
                path, previous, target, item, history, historical_now, run_id
            )
        )
        if prior_revision_wait:
            if previous.get("receipt_summary") is None and status in {"queued", "running"}:
                expected.add(f"actionable_pipeline_work:{target}:{stage}")
            # Quarantine the exact current read; an immutable old no-operation
            # wait cannot authorize any callback after its revision changes.
            expected.update(
                value
                for value in (
                    f"premature_deadline_deferral:{target}",
                    f"unverified_deadline_boundary:{target}",
                    f"unverified_resource_deferral:{target}",
                    f"unverified_provider_failure:{target}",
                    f"unverified_github_capacity_deferral:{target}",
                    f"unmatched_github_capacity_trigger:{target}",
                )
                if value in reasons
            )
            quarantined.add(target)
            changed_wait_origins.add(target)
            if status == "pending":
                pending.add(target)
        elif (
            status == "failed"
            and stage in {"prepare", "interpret", "exception"}
            and (
                item.get("recovery_barrier") is not None
                or item.get("error")
                in {READ_BARRIER_ERROR, READ_ORIGIN_BARRIER_ERROR, READ_INTERRUPTED_BARRIER_ERROR}
            )
        ):
            if not _valid_read_recovery_barrier(target, item, history):
                return False, set(), set(), set(), set(), {}
            expected.add(f"unresolved_pipeline_work:{target}:failed")
            quarantined.add(target)
        elif status == "completed":
            pass
        elif (
            _interrupted_read(previous, item)
            and (no_action or retained_read_wait)
            and not item.get("provider_outcome_uncertain")
            and _valid_retained_act_barrier(
                item.get("recovery_barrier"), history, item.get("identity"), target
            )
        ):
            expected.add(f"actionable_pipeline_work:{target}:{stage}")
            if item.get("recovery_barrier") is not None or (
                status == "running" and stage in {"interpret", "exception"}
            ):
                # A missing return is not proof that its provider finished. Keep
                # the exact assessment non-runnable while disjoint reads advance.
                quarantined.add(target)
            else:
                retryable.add(target)
        elif (
            status == "deferred"
            and stage in {"prepare", "interpret", "exception"}
            and (no_action or retained_read_wait)
            and item.get("deferred_reason") == "deadline_exhausted"
            and not item.get("provider_outcome_uncertain")
        ):
            invalid_reason = f"unverified_deadline_boundary:{target}"
            inherited_unstarted_wait = (
                retained_read_wait
                and invalid_reason in reasons
                and _inherited_unstarted_wait_deferral(previous, target, item, history)
            )
            premature_reason = f"premature_deadline_deferral:{target}"
            invalid_retained_boundary = (
                retained_read_wait
                and not inherited_unstarted_wait
                and (
                    historical_now < float(previous["deadline"])
                    or not _valid_deadline_evidence(
                        item.get("resource_evidence"), float(previous["deadline"]), rows
                    )
                )
            )
            if no_action and premature_reason in reasons:
                # The round is now expired and this read retained no action.
                # Audit the false old cutoff without authorizing a mutation.
                if invalid_reason not in reasons:
                    return False, set(), set(), set(), set(), {}
                expected.update({premature_reason, invalid_reason})
                retryable.add(target)
            elif premature_reason in reasons or invalid_retained_boundary:
                # The old stop remains invalid. Keep this exact read non-runnable
                # rather than let its false deadline block disjoint fresh work.
                recognized = {invalid_reason, premature_reason}.intersection(reasons)
                if not recognized:
                    return False, set(), set(), set(), set(), {}
                if not _valid_retained_act_barrier(
                    item.get("recovery_barrier"), history, item.get("identity"), target
                ):
                    return False, set(), set(), set(), set(), {}
                expected.update(recognized)
                quarantined.add(target)
            else:
                if invalid_reason in reasons:
                    expected.add(invalid_reason)
                elif historical_now < float(previous["deadline"]) or not _valid_deadline_evidence(
                    item.get("resource_evidence"), float(previous["deadline"]), rows
                ):
                    return False, set(), set(), set(), set(), {}
                retryable.add(target)
        elif (
            status == "deferred"
            and stage in {"prepare", "interpret", "exception"}
            and (no_action or retained_read_wait)
            and item.get("deferred_reason") == "worker_capacity"
        ):
            invalid_reason = f"unverified_resource_deferral:{target}"
            if retained_read_wait and not _valid_worker_capacity(
                item.get("resource_evidence"), historical_now
            ):
                return False, set(), set(), set(), set(), {}
            if invalid_reason in reasons:
                expected.add(invalid_reason)
            elif not _valid_worker_capacity(item.get("resource_evidence"), historical_now):
                return False, set(), set(), set(), set(), {}
            retryable.add(target)
        elif (
            status == "deferred"
            and stage == "prepare"
            and item.get("deferred_reason") == "github_capacity"
            and (no_action or retained_read_wait)
        ):
            resource = item.get("resource_evidence")
            if not _valid_github_capacity_evidence(resource, historical_now):
                # A throttle can expire before a terminal receipt is written.
                # Its original observation proves only read scheduling, never
                # today's stop or an action. Completed rounds additionally need
                # an exact receipt binding and a wholly unstarted prepare row.
                capacity = (
                    resource.get("github_capacity") if isinstance(resource, Mapping) else None
                )
                observed = capacity.get("observed_at") if isinstance(capacity, Mapping) else None
                completed_unstarted_prepare = _completed_prepare_capacity_observation(
                    previous, item, run_id, now_value
                )
                if not (
                    (summary is None or completed_unstarted_prepare)
                    and isinstance(observed, (int, float))
                    and not isinstance(observed, bool)
                    and float(previous["created"]) <= observed <= float(previous["deadline"])
                    and _valid_github_capacity_evidence(resource, observed)
                ):
                    return False, set(), set(), set(), set(), {}
                invalid_reason = f"unverified_github_capacity_deferral:{target}"
                if invalid_reason in reasons:
                    expected.add(invalid_reason)
            capacity = resource["github_capacity"]
            expected_trigger = f"retry_after_github_reset:{float(capacity['reset_at_epoch']):.3f}"
            if item.get("deferred_next_trigger") != expected_trigger:
                return False, set(), set(), set(), set(), {}
            retryable.add(target)
        elif (
            status == "deferred"
            and stage == "interpret"
            and item.get("deferred_reason") == "provider_failure"
        ):
            interpreted = results.get("interpret")
            if (
                not (
                    no_action
                    or retained_read_wait
                    or _historical_validated_wait_result(previous, target, item, history)
                )
                or f"unverified_provider_failure:{target}" in reasons
                or item.get("provider_outcome_uncertain")
                or not _valid_provider_failure(item.get("resource_evidence"))
                or not isinstance(interpreted, Mapping)
                or interpreted.get("status") != "deferred"
                or interpreted.get("reason") != "provider_failure"
                or interpreted.get("resource_evidence") != item.get("resource_evidence")
                or interpreted.get("next_trigger") != item.get("deferred_next_trigger")
            ):
                return False, set(), set(), set(), set(), {}
            retryable.add(target)
        elif (
            status == "deferred"
            and stage == "act"
            and item.get("deferred_reason") == "validated_inner_wait"
        ):
            resource = item.get("resource_evidence")
            proof = resource.get("inner_wait") if isinstance(resource, Mapping) else None
            prepared = results.get("prepare")
            snapshot = prepared.get("snapshot") if isinstance(prepared, Mapping) else None
            act_result = results.get("act")
            if (
                (f"unverified_inner_wait:{target}" in reasons and restored is None)
                or not isinstance(act_result, Mapping)
                or act_result.get("status") != "deferred"
                or act_result.get("reason") != "validated_inner_wait"
                or act_result.get("resource_evidence") != resource
                or act_result.get("next_trigger") != item.get("deferred_next_trigger")
                or not _valid_inner_wait(
                    path,
                    target,
                    item.get("revision"),
                    snapshot,
                    proof.get("reason") if isinstance(proof, Mapping) else None,
                    item.get("deferred_next_trigger"),
                    proof,
                    frontier_id=proof.get("frontier_id") if isinstance(proof, Mapping) else None,
                    now=lambda: historical_now,
                )
            ):
                return False, set(), set(), set(), set(), {}
            if restored is not None:
                expected.add(f"unverified_inner_wait:{target}")
                restored_waits[target] = restored
            waits.add(target)
        elif (
            status == "failed"
            and stage in {"prepare", "interpret", "exception"}
            and (
                no_action
                or (item.get("error") == PRELAUNCH_ROUTE_BUSY_ERROR and retained_read_wait)
            )
            and item.get("error") != UNBOUND_UNCERTAIN_PROVIDER_ERROR
        ):
            expected.add(f"unresolved_pipeline_work:{target}:failed")
            retryable.add(target)
        elif status == "failed" and stage == "act":
            expected.add(f"unresolved_pipeline_work:{target}:failed")
            barrier = item.get("recovery_barrier")
            if barrier is not None:
                if (
                    not isinstance(barrier, Mapping)
                    or barrier.get("schema") != "closer-unverified-act-barrier/v1"
                ):
                    return False, set(), set(), set(), set(), {}
                matching = [
                    record["targets"][target]
                    for record in history
                    if isinstance(record, Mapping)
                    and record.get("source_run_id") == barrier.get("source_run_id")
                    and isinstance(record.get("targets"), Mapping)
                    and isinstance(record["targets"].get(target), Mapping)
                    and record["targets"][target].get("item_sha256") == barrier.get("item_sha256")
                ]
                if not any(
                    barrier.get("revision") == row.get("revision") == item.get("revision")
                    and barrier.get("identity") == row.get("identity") == item.get("identity")
                    and barrier.get("work_key") == row.get("work_key") == item.get("work_key")
                    and barrier.get("act_result_sha256") == _digest(row.get("act_result"))
                    for row in matching
                ):
                    return False, set(), set(), set(), set(), {}
            if _legacy_failed_act_wait_proof(path, target, item):
                waits.add(target)
                if restored is not None:
                    restored_waits[target] = restored
            else:
                quarantined.add(target)
        elif (
            status == "deferred"
            and stage == "reconcile"
            and item.get("deferred_reason") == "worker_capacity"
        ):
            # A recovered uncertain operation can be queued for reconciliation
            # and then lose its I/O slot before the callback runs. The capacity
            # sample is only scheduling evidence: preserve the audited original
            # operation as reconciliation-only on the next cadence.
            reconciliation = results.get("reconcile")
            if (
                not no_action
                or item.get("provider_outcome_uncertain")
                or item.get("deferred_next_trigger") != "capacity_recheck"
                or not _valid_worker_capacity(item.get("resource_evidence"), historical_now)
                or (
                    reconciliation is not None
                    and (
                        not isinstance(reconciliation, Mapping)
                        or reconciliation.get("outcome") != "unknown"
                    )
                )
                or not all(
                    isinstance(item.get(key), str) and item[key]
                    for key in ("revision", "identity", "work_key")
                )
                or not any(
                    isinstance(record, Mapping)
                    and isinstance(record.get("targets"), Mapping)
                    and isinstance(record["targets"].get(target), Mapping)
                    and record["targets"][target].get("recovery") == "reconcile_only"
                    and all(
                        record["targets"][target].get(key) == item[key]
                        for key in ("revision", "identity", "work_key")
                    )
                    for record in history
                )
            ):
                return False, set(), set(), set(), set(), {}
            uncertain.add(target)
        elif (
            status == "deferred"
            and stage in {"act", "reconcile"}
            and item.get("deferred_reason") == "deadline_exhausted"
            and no_action
            and not item.get("provider_outcome_uncertain")
        ):
            # An action queued at the cutoff may still depend on an unresolved
            # original operation. Never turn its deadline into permission to act.
            if (
                f"unverified_deadline_boundary:{target}" in reasons
                or historical_now < float(previous["deadline"])
                or not _valid_deadline_evidence(
                    item.get("resource_evidence"), float(previous["deadline"]), rows
                )
            ):
                return False, set(), set(), set(), set(), {}
            uncertain.add(target)
        elif (
            previous.get("receipt_summary") is None
            and status in {"queued", "running"}
            and stage in {"act", "reconcile"}
            and not item.get("provider_outcome_uncertain")
            and item.get("recovery_barrier") is None
            and item.get("known_completed_action") is not True
            and all(
                isinstance(item.get(key), str) and item[key]
                for key in ("revision", "identity", "work_key")
            )
        ):
            # A missing callback return is an unknown effect, never a retry.
            # Retain its exact running row in the audit; only the successor's
            # projection enters the existing original-operation reconciliation.
            uncertain.add(target)
            expected.add(f"actionable_pipeline_work:{target}:{stage}")
        elif (
            status == "uncertain"
            and stage in {"act", "reconcile"}
            and (
                item.get("deferred_next_trigger") == "reconcile_before_retry"
                or (
                    stage == "reconcile"
                    and "deferred_next_trigger" not in item
                    and isinstance(results.get("reconcile"), Mapping)
                    and results["reconcile"].get("outcome") == "unknown"
                    and isinstance(results["reconcile"].get("reason"), str)
                    and results["reconcile"]["reason"]
                    == "legacy uncertain operation lacks external-run or live-owner proof; "
                    "reconcile it in the original closer queue"
                )
            )
            and not item.get("provider_outcome_uncertain")
        ):
            uncertain.add(target)
            active_reason = f"actionable_pipeline_work:{target}:{stage}"
            if active_reason in reasons:
                expected.add(active_reason)
        elif status == "pending" and stage in {"prepare", "interpret", "exception"}:
            if not no_action:
                interpreted = results.get("interpret")
                historical_wait = (
                    stage == "interpret"
                    and isinstance(interpreted, Mapping)
                    and interpreted.get("status") == "pending"
                    and interpreted.get("pending_kind") == item.get("pending_kind")
                    and interpreted.get("pending_evidence") == item.get("pending_evidence")
                    and interpreted.get("next_trigger") == item.get("deferred_next_trigger")
                    and _historical_validated_wait_result(previous, target, item, history)
                )
                if not historical_wait and not _retained_blocked_act_wait_proof(path, target, item):
                    return False, set(), set(), set(), set(), {}
            kind = item.get("pending_kind")
            evidence = item.get("pending_evidence")
            id_field = {
                "ci": "check_run_id",
                "async_operation": "operation_id",
                "external_event": "event_id",
            }.get(kind if isinstance(kind, str) else "")
            if (
                not id_field
                or not isinstance(item.get("deferred_next_trigger"), str)
                or not item["deferred_next_trigger"].strip()
                or not isinstance(evidence, Mapping)
                or not isinstance(evidence.get(id_field), str)
                or not evidence[id_field]
            ):
                return False, set(), set(), set(), set(), {}
            pending.add(target)
            retryable.add(target)
        else:
            return False, set(), set(), set(), set(), {}
        act = results.get("act")
        receipt = act.get("queue_receipt") if isinstance(act, Mapping) else None
        try:
            retained_act = _bounded(act, limit=32 * 1024) if isinstance(act, Mapping) else None
        except ValueError:
            return False, set(), set(), set(), set(), {}
        recovery = (
            "retry_fresh"
            if target in retryable
            else (
                "validated_inner_wait"
                if target in waits
                else (
                    (
                        "quarantine_read"
                        if stage in {"prepare", "interpret", "exception"}
                        else "quarantine"
                    )
                    if target in quarantined
                    else "reconcile_only" if target in uncertain else "completed"
                )
            )
        )
        material = (
            {
                "status": status,
                "stage": stage,
                "deferred_reason": item.get("deferred_reason"),
                "recovery": recovery,
            }
            if status == "deferred" and no_action and target not in uncertain
            else {
                "status": status,
                "stage": stage,
                "revision": item.get("revision"),
                "identity": item.get("identity"),
                "error": item.get("error"),
                "deferred_reason": item.get("deferred_reason"),
                "resource_evidence": item.get("resource_evidence"),
                "provider_outcome_uncertain": item.get("provider_outcome_uncertain"),
                "recovery_barrier": item.get("recovery_barrier"),
                "act_receipt_sha256": _digest(receipt) if isinstance(receipt, Mapping) else None,
                "recovery": recovery,
            }
        )
        if restored is not None:
            material["restored_wait_source_sha256"] = _digest(original_item)
        material_sha256 = _digest(material)
        audited[target] = {
            **(
                {
                    "recovery_barrier": item.get("recovery_barrier"),
                    "read_barrier_reason": (
                        "historical_wait_revision_changed"
                        if target in changed_wait_origins
                        else (
                            "interrupted_read"
                            if previous.get("receipt_summary") is None
                            and status in {"queued", "running"}
                            and stage in {"prepare", "interpret", "exception"}
                            else (item.get("recovery_barrier") or {}).get("reason")
                            or "terminal_resource_boundary_invalid"
                        )
                    ),
                }
                if target in quarantined and stage in {"prepare", "interpret", "exception"}
                else {}
            ),
            "item_sha256": _digest(original_item),
            "material_sha256": material_sha256,
            "status": status,
            "stage": stage,
            "revision": item.get("revision"),
            "identity": item.get("identity"),
            "work_key": item.get("work_key"),
            "deferred_reason": item.get("deferred_reason"),
            "next_trigger": item.get("deferred_next_trigger"),
            "error": item.get("error"),
            "resource_evidence": item.get("resource_evidence"),
            "pending_kind": item.get("pending_kind"),
            "pending_evidence": item.get("pending_evidence"),
            "act_result": retained_act,
            "restored_wait_view": restored is not None,
            "act_receipt_sha256": _digest(receipt) if isinstance(receipt, Mapping) else None,
            "recovery": recovery,
        }
    # The only relaxation is this exact interrupted-round shape. Never treat
    # an unrecognized validation failure as an expired capacity/deadline stop.
    if pending and "pending_work_is_not_the_only_blocker" in reasons:
        expected.add("pending_work_is_not_the_only_blocker")
    if len(reasons) != len(expected) or set(reasons) != expected or not expected:
        return False, set(), set(), set(), set(), {}
    try:
        audit = _bounded(
            {
                "schema": "closer-expired-invalid-round-recovery/v1",
                "source_run_id": run_id,
                "source_deadline": previous.get("deadline"),
                "source_receipt_missing": previous.get("receipt_summary") is None,
                "source_assignment": list(assignment),
                "prior_stop": prior_stop,
                "targets": audited,
            },
            limit=512 * 1024,
        )
        recorded = {
            (target, row.get("material_sha256"))
            for entry in history
            if isinstance(entry, Mapping) and isinstance(entry.get("targets"), Mapping)
            for target, row in entry["targets"].items()
            if isinstance(target, str) and isinstance(row, Mapping)
        }
        fresh_material = {
            target: row
            for target, row in audited.items()
            if (
                row["status"] not in {"completed", "pending"}
                and (target, row["material_sha256"]) not in recorded
            )
            # A newly created read barrier pins this exact source run and row.
            # Older identical material cannot satisfy that new origin pointer.
            or (
                row["recovery"] == "quarantine_read"
                and row["status"] in {"deferred", "pending", "queued", "running"}
            )
        }
        history_entry = (
            dict(audit)
            if not history
            else {**audit, "targets": fresh_material} if fresh_material else None
        )
        _bounded(previous.get("invalid_stop_history", []), limit=512 * 1024)
        _bounded(prior_stop, limit=64 * 1024)
        prior_origin = previous.get("prior_invalid_stop_evidence")
        if isinstance(prior_origin, Mapping):
            _bounded(prior_origin)
    except ValueError:
        return False, set(), set(), set(), set(), {}
    audit["history_entry"] = history_entry
    audit["restored_wait_items"] = restored_waits
    return True, retryable, waits, quarantined, uncertain, audit


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "runs": {}, "carry": {}, "cache": {}, "frontiers": {}}


@contextlib.contextmanager
def _locked(path: Path, *, timeout_seconds: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        if timeout_seconds is None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        else:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
            ):
                raise ValueError("finite state-lock timeout must be positive and finite")
            deadline = time.monotonic() + float(timeout_seconds)
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _FiniteStateLockTimeout("finite pipeline state lock timed out")
                    time.sleep(min(0.025, remaining))
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class _FiniteStateLockTimeout(TimeoutError):
    """A finite-mode coordinator could not safely read or persist its journal."""


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
    doc.setdefault("frontiers", {})
    if not isinstance(doc["frontiers"], dict):
        raise ValueError("malformed finite frontier namespace")
    return doc


def _save(path: Path, doc: Mapping[str, Any]) -> None:
    if (
        path.name == "state.json"
        and path.parent.parent.name == "targets"
        and path.parent.parent.parent.name.endswith(".scoped")
    ):
        # The census binds all source bytes; unrelated short shard commits must
        # not invalidate every other target's admission under concurrent load.
        with closer_resources.inventory_lock(path.parent.parent.parent, exclusive=True):
            return _save_snapshot(path, doc)
    return _save_snapshot(path, doc)


def _save_snapshot(path: Path, doc: Mapping[str, Any]) -> None:
    fence = closer_resources.fence_for(path)
    if fence is not None:
        fence()  # scoped mode: a stale or expired lease holder must not commit
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
        if fence is not None:
            fence()  # serialization/fsync may have crossed the lease deadline
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


def _run_pipeline_impl(
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
    priority_targets: Iterable[str] = (),
    max_io_workers: int = 4,
    max_low_workers: int = 2,
    max_exception_workers: int = 1,
    round_deadline_exception: type[BaseException] | None = None,
    round_capacity_exception: type[BaseException] | None = None,
    now: Callable[[], float] = time.time,
    _run_lock_ref: list[Any] | None = None,
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
    priority = frozenset(priority_targets)
    if not priority.issubset(target_list):
        raise ValueError("priority targets must belong to the current assignment")
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
    observed_at = now()
    if float(deadline) <= observed_at:
        return {
            "schema": "closer-pipeline-receipt/v1",
            "authority": False,
            "status": "deferred",
            "reason": "admission_deadline_expired",
            "deadline": float(deadline),
            "now": observed_at,
            "continuation_required": True,
            "finished_at": observed_at,
            "targets": {},
        }
    run_guard = path.with_name(path.name + ".runlock")
    run_guard.parent.mkdir(parents=True, exist_ok=True)
    run_lock = run_guard.open("a+")
    acquired = False
    contended = False
    while now() < float(deadline):
        try:
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except BlockingIOError:
            contended = True
            time.sleep(min(0.025, max(0.0, float(deadline) - now())))
    if not acquired:
        run_lock.close()
        observed_at = now()
        return {
            "schema": "closer-pipeline-receipt/v1",
            "authority": False,
            "status": "deferred",
            "reason": "coordinator_lock_unavailable" if contended else "admission_deadline_expired",
            **({} if contended else {"deadline": float(deadline), "now": observed_at}),
            "continuation_required": True,
            "finished_at": observed_at,
            "targets": {},
        }
    if _run_lock_ref is not None:
        _run_lock_ref.append(run_lock)
    with _locked(path):
        prior_stop: dict[str, Any] | None = None
        prior_wait_targets: set[str] = set()
        retained_read_targets: set[str] = set()
        recovery_retryable: set[str] = set()
        recovery_quarantined: set[str] = set()
        recovery_uncertain: set[str] = set()
        recovery_audit: dict[str, Any] | None = None
        previous: dict[str, Any] | None = None
        state = _load(path)
        candidates = [
            (key, value)
            for key, value in state["runs"].items()
            if float(value.get("deadline", 0)) > now()
            and any(
                row.get("status") in {"queued", "running", "uncertain", "pending"}
                for row in value.get("targets", {}).values()
            )
        ]
        if candidates:
            run_id, run = max(candidates, key=lambda pair: float(pair[1].get("created", 0)))
            run["carry_targets"] = sorted(set(run.get("targets", {})) - set(assignment))
            run["assignment"] = assignment
        else:
            prior_invalid_stop_evidence = None
            if state["runs"]:
                previous_run_id, previous = max(
                    state["runs"].items(), key=lambda pair: float(pair[1].get("created", 0))
                )
                prior_wait_targets = {
                    target
                    for target, row in previous.get("targets", {}).items()
                    if isinstance(row, Mapping)
                    and row.get("status") == "deferred"
                    and row.get("deferred_reason") == "validated_inner_wait"
                }
                previous_assignment = previous.get("assignment", [])
                if float(previous.get("deadline", 0)) <= now():
                    for old_item in previous.get("targets", {}).values():
                        if (
                            previous.get("receipt_summary") is None
                            and old_item.get("status") in {"queued", "running"}
                            and old_item.get("stage") in {"act", "reconcile"}
                        ):
                            # Recovery records the actual interrupted boundary;
                            # mutate only the successor's reconciliation view.
                            continue
                        if (
                            previous.get("receipt_summary") is None
                            and old_item.get("stage") in {"prepare", "interpret", "exception"}
                            and old_item.get("status") in {"queued", "running"}
                        ):
                            # Preserve the interrupted read's actual boundary
                            # for recovery: a running assessment is not an
                            # unstarted deadline deferral.
                            continue
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
                if prior_stop.get("valid"):
                    # validate_stop already checked each current native wait.
                    # Retain those full proof rows over their compact carry views.
                    prior_wait_targets.update(
                        target
                        for target, item in previous.get("targets", {}).items()
                        if item.get("status") == "deferred"
                        and item.get("stage") == "act"
                        and item.get("deferred_reason") == "validated_inner_wait"
                    )
                if not prior_stop.get("valid"):
                    safe_continuation, has_native_uncertainty, recovered_wait_targets = (
                        _safe_prior_continuation(prior_stop, previous, path)
                    )
                    if not safe_continuation:
                        (
                            safe_continuation,
                            recovery_retryable,
                            recovered_wait_targets,
                            recovery_quarantined,
                            recovery_uncertain,
                            recovery_audit_value,
                        ) = _expired_invalid_round_recovery(
                            prior_stop, previous, path, previous_run_id, now()
                        )
                        if safe_continuation:
                            recovery_audit = recovery_audit_value
                    if not safe_continuation:
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
                    if has_native_uncertainty or recovered_wait_targets or recovery_audit:
                        prior_wait_targets.update(recovered_wait_targets)
                        prior_invalid_stop_evidence = {
                            "valid": False,
                            "reasons": list(prior_stop.get("reasons", [])),
                            "continuation_required": True,
                        }
                        if recovered_wait_targets:
                            prior_invalid_stop_evidence["recovered_inner_wait_targets"] = sorted(
                                recovered_wait_targets
                            )
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
            try:
                origin_history = _resolved_audit_history(path, previous) if previous else []
            except (ValueError, OSError):
                # Unavailable immutable history cannot establish a wait origin.
                origin_history = []
            if prior_invalid_stop_evidence is not None:
                run["prior_invalid_stop_evidence"] = prior_invalid_stop_evidence
                if recovered_wait_targets and previous is not None:
                    wait_records: dict[str, dict[str, Any]] = {}
                    for target in sorted(recovered_wait_targets):
                        old_item = (
                            recovery_audit.get("restored_wait_items", {}).get(target)
                            if isinstance(recovery_audit, Mapping)
                            else None
                        ) or previous["targets"][target]
                        old_results = old_item["results"]
                        old_act = old_results["act"]
                        old_resource = old_item.get("resource_evidence")
                        old_proof = (
                            old_resource.get("inner_wait")
                            if isinstance(old_resource, Mapping)
                            else None
                        )
                        old_receipt = old_act.get("queue_receipt")
                        receipt_hash = (
                            _digest(old_receipt)
                            if isinstance(old_receipt, Mapping)
                            else (
                                old_proof.get("receipt_sha256")
                                if isinstance(old_proof, Mapping)
                                else None
                            )
                        )
                        available_origins = []
                        if isinstance(recovery_audit, Mapping) and isinstance(
                            recovery_audit.get("history_entry"), Mapping
                        ):
                            available_origins.append(recovery_audit["history_entry"])
                        available_origins.extend(origin_history)
                        origin = _validated_wait_audit_origin(available_origins, target, old_item)
                        if origin is None and (
                            not recovery_audit or recovery_audit.get("native_wait_provenance_only")
                        ):
                            observed = _native_wait_origin_record(
                                path, target, old_item, previous, previous_run_id, prior_stop, now
                            )
                            if observed is not None:
                                if recovery_audit:
                                    recovery_audit["history_entry"]["targets"].update(
                                        observed["targets"]
                                    )
                                else:
                                    recovery_audit = {
                                        "history_entry": observed,
                                        "native_wait_provenance_only": True,
                                    }
                                origin = _validated_wait_audit_origin(
                                    [recovery_audit["history_entry"]], target, old_item
                                )
                        if origin is None:
                            return {
                                "schema": "closer-pipeline-receipt/v1",
                                "authority": False,
                                "status": "blocked",
                                "reason": "validated_wait_audit_origin_unavailable",
                                "target": target,
                                "continuation_required": True,
                                "finished_at": now(),
                                "targets": {},
                            }
                        if origin["previous_run_id"] == previous_run_id:
                            origin = {"previous_item_sha256": origin["previous_item_sha256"]}
                        wait_records[target] = {
                            **origin,
                            "restored_wait_view": old_item is not previous["targets"][target],
                            "previous_error": old_item.get("error"),
                            "revision": old_item.get("revision"),
                            "head": old_results["prepare"]["snapshot"].get("head"),
                            "reason": old_act.get("reason"),
                            "next_trigger": old_act.get("next_trigger"),
                            "inner_path": str(
                                path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json"
                            ),
                            "receipt_sha256": receipt_hash,
                        }
                    run["recovered_invalid_stop"] = {
                        "schema": "closer-recovered-inner-wait/v1",
                        "previous_run_id": previous_run_id,
                        "prior_stop_reasons": (
                            list(prior_stop.get("reasons", [])) if prior_stop is not None else []
                        ),
                        "targets": wait_records,
                    }
            if previous is not None:
                retained_read_targets = {
                    target
                    for target, item in previous.get("targets", {}).items()
                    if _audited_retained_wait(
                        path,
                        previous,
                        target,
                        item,
                        origin_history,
                        prior_stop_now(),
                        previous_run_id,
                    )
                }
                if retained_read_targets:
                    # A new read receipt never supersedes the original wait's
                    # source-run/item/receipt anchor. Different targets may now
                    # have different recovery origins within the same cadence.
                    old_recovery = previous["recovered_invalid_stop"]
                    retained_recovery = run.setdefault(
                        "recovered_invalid_stop", _bounded(old_recovery)
                    )
                    for target in sorted(retained_read_targets):
                        anchor = _bounded(old_recovery["targets"][target])
                        origin = anchor.get("previous_run_id", old_recovery["previous_run_id"])
                        if origin != retained_recovery["previous_run_id"]:
                            anchor["previous_run_id"] = origin
                        retained_recovery["targets"][target] = anchor
                    marker = run.setdefault(
                        "prior_invalid_stop_evidence",
                        _bounded(previous["prior_invalid_stop_evidence"]),
                    )
                    marker["recovered_inner_wait_targets"] = sorted(
                        set(marker.get("recovered_inner_wait_targets", [])) | retained_read_targets
                    )
                new_history_entry = (
                    recovery_audit.get("history_entry")
                    if isinstance(recovery_audit, Mapping)
                    else None
                )
                try:
                    next_history, archive_reference = _stage_audit_history(
                        path, previous, new_history_entry
                    )
                except (ValueError, OSError) as exc:
                    return {
                        "schema": "closer-pipeline-receipt/v1",
                        "authority": False,
                        "status": "blocked",
                        "reason": "audit_archive_unavailable",
                        "error": str(exc)[:300],
                        "prior_stop": prior_stop,
                        "continuation_required": True,
                        "finished_at": now(),
                        "targets": {},
                    }
                if next_history:
                    run["invalid_stop_history"] = next_history
                if archive_reference is not None:
                    run["invalid_stop_archive"] = archive_reference
                prior_origin = previous.get("prior_invalid_stop_evidence")
                if isinstance(prior_origin, Mapping):
                    run.setdefault("prior_invalid_stop_evidence", _bounded(prior_origin))
            latest: dict[str, dict[str, Any]] = {}
            for old_run in sorted(
                state["runs"].values(), key=lambda row: float(row.get("created", 0))
            ):
                for old_target, old_item in old_run.get("targets", {}).items():
                    if old_item.get("status") not in {"completed", "deferred"} or (
                        old_target in recovery_uncertain
                    ):
                        latest[old_target] = old_item
            for old_target, old_item in state["carry"].items():
                latest[old_target] = old_item
            if recovery_audit is not None and previous is not None:
                # Compact carry is a derived view, not a replacement for the
                # audited preparation needed to reconcile an original operation.
                for old_target in (
                    recovery_uncertain | recovered_wait_targets | recovery_quarantined
                ):
                    latest[old_target] = recovery_audit.get("restored_wait_items", {}).get(
                        old_target, previous["targets"][old_target]
                    )
            if previous is not None:
                for old_target in prior_wait_targets:
                    if old_target not in (recovery_audit or {}).get("restored_wait_items", {}):
                        latest[old_target] = previous["targets"][old_target]
                for old_target in retained_read_targets:
                    # Compact read-deferral carry omits the prior action. Keep
                    # this fully proven native wait as provenance, not authority.
                    latest[old_target] = previous["targets"][old_target]
            carried = []
            scope_origins = {}
            scope_carry = {}
            for old_target, old_item in latest.items():
                # An old non-action failure outside today's assignment remains
                # in the immutable audit, not in today's work queue.
                if (
                    recovery_audit is not None
                    and old_target not in assignment
                    and old_target in recovery_retryable
                ):
                    continue
                item = _bounded(old_item)
                if (
                    old_target not in assignment
                    and item.get("deferred_reason") == "scope_quarantined"
                ):
                    origins = [
                        (name, old_run)
                        for name, old_run in state["runs"].items()
                        if old_run.get("targets", {}).get(old_target) == old_item
                        and closer_resources.valid_scope_refusal(path, old_target, old_item, name)
                    ]
                    if len(origins) != 1:
                        return {
                            "schema": "closer-pipeline-receipt/v1",
                            "authority": False,
                            "status": "blocked",
                            "reason": "scope_refusal_origin_unavailable",
                            "targets": {},
                            "continuation_required": True,
                            "finished_at": now(),
                        }
                    origin_id, origin_run = origins[0]
                    scope_origins[origin_id] = origin_run
                    scope_carry[old_target] = item
                    continue
                if recovery_audit is not None and old_target in recovery_uncertain:
                    item.update(
                        status="uncertain",
                        stage="reconcile",
                        deferred_next_trigger="reconcile_before_retry",
                    )
                if old_target in prior_wait_targets | recovery_retryable | retained_read_targets:
                    item["fresh_inner_wait_admission"] = True
                if recovery_audit is not None and old_target in recovery_quarantined:
                    audit_row = recovery_audit["targets"][old_target]
                    prior_barrier = item.get("recovery_barrier")
                    read_barrier = audit_row["recovery"] == "quarantine_read"
                    if not isinstance(prior_barrier, Mapping) or (
                        read_barrier and prior_barrier.get("schema") != READ_BARRIER_SCHEMA
                    ):
                        item["recovery_barrier"] = {
                            "schema": (
                                READ_BARRIER_SCHEMA
                                if audit_row["recovery"] == "quarantine_read"
                                else "closer-unverified-act-barrier/v1"
                            ),
                            "source_run_id": recovery_audit["source_run_id"],
                            "item_sha256": audit_row["item_sha256"],
                            "revision": audit_row["revision"],
                            "identity": audit_row["identity"],
                            "work_key": audit_row["work_key"],
                            "act_result_sha256": _digest(audit_row["act_result"]),
                        }
                        if read_barrier:
                            item["recovery_barrier"]["reason"] = audit_row["read_barrier_reason"]
                            if prior_barrier is not None:
                                item["recovery_barrier"]["prior_recovery_barrier"] = _bounded(
                                    prior_barrier
                                )
                    if item["recovery_barrier"].get("schema") == READ_BARRIER_SCHEMA:
                        item.update(
                            status="failed",
                            error=(
                                READ_ORIGIN_BARRIER_ERROR
                                if item["recovery_barrier"].get("reason")
                                == "historical_wait_revision_changed"
                                else (
                                    READ_INTERRUPTED_BARRIER_ERROR
                                    if item["recovery_barrier"].get("reason") == "interrupted_read"
                                    else READ_BARRIER_ERROR
                                )
                            ),
                            deferred_next_trigger=READ_BARRIER_TRIGGER,
                        )
                if item.get("status") == "running":
                    item.update(status="uncertain" if item.get("stage") == "act" else "pending")
                if (
                    prior_stop is not None
                    and prior_stop.get("valid") is True
                    and item.get("status") == "deferred"
                    and item.get("stage") == "reconcile"
                    and item.get("deferred_reason") == "worker_capacity"
                    and item.get("deferred_next_trigger") == "capacity_recheck"
                    and isinstance(item.get("results"), Mapping)
                    and "act" not in item["results"]
                    and item.get("known_completed_action") is not True
                    and not item.get("provider_outcome_uncertain")
                ):
                    # A valid stop can still have an unrun reconciliation. Its
                    # capacity deferral is never permission to restart at act.
                    item.update(status="uncertain", deferred_next_trigger="reconcile_before_retry")
                if old_target in assignment:
                    item.pop("recovery_excluded", None)
                    if item.get("deferred_reason") == "scope_quarantined":
                        origins = [
                            (name, old_run)
                            for name, old_run in state["runs"].items()
                            if old_run.get("targets", {}).get(old_target) == old_item
                            and closer_resources.valid_scope_refusal(
                                path, old_target, old_item, name
                            )
                        ]
                        if len(origins) != 1:
                            return {
                                "schema": "closer-pipeline-receipt/v1",
                                "authority": False,
                                "status": "blocked",
                                "reason": "scope_refusal_origin_unavailable",
                                "targets": {},
                                "continuation_required": True,
                                "finished_at": now(),
                            }
                        origin_id, origin_run = origins[0]
                        scope_origins[origin_id] = origin_run
                        run.setdefault("prior_scope_refusals", {})[old_target] = {
                            "source_run_id": origin_id,
                            "item_sha256": _digest(old_item),
                            "scope_proof": old_item["scope_proof"],
                        }
                        # Project a fresh scheduling view only after proving
                        # this exact callback never started. Retain its full
                        # original run and proof, without rebinding either.
                        for field in (
                            "scope_proof",
                            "deferred_reason",
                            "deferred_next_trigger",
                            "error",
                        ):
                            item.pop(field, None)
                    if item.get("status") not in {"uncertain", "failed"} or (
                        item.get("status") == "failed" and old_target not in recovery_quarantined
                    ):
                        item.update(status="queued", stage="prepare")
                else:
                    if (
                        old_target in recovery_quarantined | recovery_uncertain | prior_wait_targets
                        or (
                            item.get("status") == "uncertain"
                            and item.get("stage") in {"act", "reconcile"}
                        )
                    ):
                        item["recovery_excluded"] = True
                    elif item.get("status") not in {"uncertain", "pending"}:
                        item.update(status="queued", stage="prepare")
                    item["carried"] = True
                    carried.append(old_target)
                run["targets"][old_target] = item
            run["carry_targets"] = sorted(carried)
            if prior_stop is not None and not prior_stop.get("valid"):
                run["prior_stop"] = _bounded(prior_stop, limit=64 * 1024)
            # Keep only a small run history; unresolved targets are additionally
            # copied into the top-level carry ledger before old runs are trimmed.
            state["runs"] = {**scope_origins, run_id: run}
            state["carry"] = scope_carry
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
            if target in prior_wait_targets or target in recovery_retryable:
                item["fresh_inner_wait_admission"] = True
            # A process can disappear after launching a callback. Its outcome is
            # unknown; never automatically replay an action from that state.
            if item.get("status") == "running":
                item["status"] = (
                    "uncertain" if item.get("stage") in {"act", "reconcile"} else "pending"
                )
                item["recovered_at"] = now()
                if item["status"] == "uncertain":
                    item["deferred_next_trigger"] = "reconcile_before_retry"
            if (
                item.get("status") == "uncertain"
                and item.get("stage") in {"act", "reconcile"}
                and not item.get("recovery_excluded")
                and reconcile is not None
            ):
                item.update(status="queued", stage="reconcile")
            if item.get("status") == "pending" and not item.get("carried"):
                item.update(status="queued", stage="prepare")
        for item in run["targets"].values():
            if (
                item.get("status") == "uncertain"
                and item.get("stage") in {"act", "reconcile"}
                and not item.get("recovery_excluded")
                and reconcile is not None
            ):
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
                    if isinstance(exc, closer_resources.ScopeBlocked):
                        value, error = {"_scope_not_started": exc.proof, "reason": str(exc)}, None
                    elif (
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
                    if value.get("_scope_not_started"):
                        # This typed signal is emitted only by the pre-callback
                        # resource gate. No provider/effect was invoked; it cannot
                        # discharge any historical outcome or authorize a retry.
                        item.update(
                            status="deferred",
                            deferred_reason="scope_quarantined",
                            deferred_next_trigger="reconcile_scope_before_retry",
                            scope_proof=value["_scope_not_started"],
                            error=value.get("reason"),
                        )
                        if stage == "reconcile":
                            # A refused reconciliation proves nothing about the
                            # earlier action. Keep that original unknown alive.
                            item.update(
                                status="uncertain", deferred_next_trigger="reconcile_before_retry"
                            )
                        return
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
                    if (
                        stage in {"interpret", "exception"}
                        and (
                            value.get("status") == "uncertain"
                            or value.get("outcome_uncertain") is True
                        )
                    ) or (
                        stage == "act"
                        and value.get("status") == "uncertain"
                        and isinstance(value.get("provider_outcome"), Mapping)
                        and value["provider_outcome"].get("schema")
                        == "closer-completed-response-retention/v1"
                    ):
                        if cache_key:
                            doc.get("cache", {}).pop(cache_key, None)
                        if not _valid_uncertain_provider_outcome(value, target, item):
                            item.update(
                                status="failed",
                                error=UNBOUND_UNCERTAIN_PROVIDER_ERROR,
                                deferred_next_trigger="human_review",
                            )
                            return
                        outcome = value["provider_outcome"]
                        actual = outcome["actual"]
                        run_id = actual["run_id"]
                        item.update(
                            status="uncertain",
                            stage=stage,
                            deferred_next_trigger=f"unresolved_native_provider_outcome:{run_id}",
                            provider_outcome_uncertain=outcome,
                        )
                        return
                    if (
                        stage in {"interpret", "exception"}
                        and cache_key
                        and not (stage == "interpret" and _native_route_result(value))
                    ):
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
                    # Admission guards may stop before any provider call. Preserve
                    # that exact event dependency instead of escalating it to the
                    # strong-worker lane or advancing to an action.
                    if (
                        stage in {"interpret", "exception"}
                        and value.get("status") == "pending"
                        and value.get("pending_kind") in {"ci", "async_operation", "external_event"}
                        and isinstance(value.get("next_trigger"), str)
                        and isinstance(value.get("pending_evidence"), Mapping)
                    ):
                        evidence_value = value.get("pending_evidence")
                        kind_value = value.get("pending_kind")
                        evidence = evidence_value if isinstance(evidence_value, Mapping) else None
                        kind = kind_value if isinstance(kind_value, str) else ""
                        id_fields = {
                            "ci": "check_run_id",
                            "async_operation": "operation_id",
                            "external_event": "event_id",
                        }
                        has_id = id_fields.get(kind)
                        event_id = evidence.get(has_id) if evidence is not None and has_id else None
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
                        if value.get("classification") == "capacity_deferred":
                            # Capacity is transient operational evidence. Never
                            # retain it in the semantic cache, even for legacy
                            # callback results that lack a typed deferral.
                            if cache_key:
                                doc.get("cache", {}).pop(cache_key, None)
                            resource = value.get("resource_evidence")
                            reason = value.get("reason")
                            trigger = value.get("next_trigger")
                            if (
                                value.get("status") == "deferred"
                                and reason == "worker_capacity"
                                and isinstance(trigger, str)
                                and trigger.strip()
                                and isinstance(resource, Mapping)
                                and _valid_worker_capacity(resource, now())
                            ):
                                item.update(
                                    status="deferred",
                                    deferred_reason=reason,
                                    deferred_next_trigger=trigger,
                                    resource_evidence=dict(resource),
                                )
                            else:
                                item.update(
                                    status="failed",
                                    error="capacity_deferral_without_fresh_route_evidence",
                                    deferred_next_trigger="fresh_capacity_report",
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
                        if value.get("status") == "failed":
                            # A returned worker failure is not a routing decision.
                            # Retain its native result, but never advance it to act
                            # or cache a transient failure across a fresh round.
                            if cache_key:
                                doc.get("cache", {}).pop(cache_key, None)
                            item["metrics"]["errors"] += 1
                            item.update(
                                status="failed",
                                error="exception_worker_failed",
                                deferred_next_trigger="fresh_round",
                            )
                        elif value.get("status") == "deferred":
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
                            item.update(
                                status="uncertain",
                                stage="reconcile",
                                deferred_next_trigger="reconcile_before_retry",
                            )
                    else:
                        status = value.get("status")
                        if status == "completed":
                            item["known_completed_action"] = True
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
                            resource = value.get("resource_evidence")
                            wait_proof = (
                                resource.get("inner_wait")
                                if isinstance(resource, Mapping)
                                else None
                            )
                            if value.get(
                                "reason"
                            ) == "validated_inner_wait" and not _valid_inner_wait(
                                path,
                                target,
                                item.get("revision"),
                                item.get("results", {}).get("prepare", {}).get("snapshot"),
                                (
                                    wait_proof.get("reason")
                                    if isinstance(wait_proof, Mapping)
                                    else None
                                ),
                                value.get("next_trigger"),
                                wait_proof,
                                now=now,
                            ):
                                item.update(status="failed", error="unverified_inner_wait")
                            else:
                                item.update(
                                    status="deferred",
                                    deferred_reason=value.get("reason"),
                                    deferred_next_trigger=value.get("next_trigger"),
                                    resource_evidence=resource,
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
            # An explicit coordinator disposition can advance through its next
            # stage without waiting behind every older advisory interpretation.
            # Within each class, retain fair round-robin ordering. Priority
            # changes scheduling only; every evidence and action guard remains.
            with _locked(path):
                state = _load(path)
                current = state["runs"][run_id]
                rows = current["targets"]
                base_order = sorted(
                    rows,
                    key=lambda t: (t not in priority, rows[t].get("last_scheduled", 0), t),
                )
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
                        "pipeline_run_id": run_id,
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
                    scope_lease = closer_resources.fence_identity(path)
                    if scope_lease is not None:
                        item["scope_lease"] = scope_lease
                    _save_state_locked(path, state)

                    def invoke(cb=callback, name=target, ctx=safe_context):
                        if now() >= active_deadline:
                            return {"_not_started_deadline": True}
                        return cb(name, ctx)

                    cached = state.get("cache", {}).get(cache_key) if cache_key else None
                    if isinstance(cached, Mapping) and isinstance(cached.get("value"), Mapping):
                        cached_value = cached["value"]
                        if stage == "interpret" and (
                            cached_value.get("classification") == "capacity_deferred"
                            or _native_route_result(cached_value)
                        ):
                            # Older coordinator receipts cached this transient
                            # route result as a semantic decision. Force a fresh
                            # route/capacity check on the next cadence.
                            state.get("cache", {}).pop(cache_key, None)
                            cached = None
                        elif stage == "interpret" and item.get("fresh_inner_wait_admission"):
                            # An ordinary cached result predates today's campaign
                            # admission, including an expired-round readmission.
                            # Re-enter the callback before dispatching any worker.
                            state.get("cache", {}).pop(cache_key, None)
                            cached = None
                        else:
                            future = Future()
                            future.set_result(cached_value)
                    if not isinstance(cached, Mapping):
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
                # Deferred work is waiting on measured capacity; a terminally
                # parked uncertainty barrier has no safe callback to retry.
                # Both should return a truthful receipt immediately.
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
        return receipt


def _run_pipeline_finite(
    state_path: str | Path,
    targets: Iterable[str],
    *,
    prepare,
    interpret,
    act,
    exception,
    reconcile=None,
    capacity_fn,
    finite_frontier,
    frontier_target: str | None = None,
    request_timeout_seconds: float = 120.0,
    cancelled_fn=None,
    paused_fn=None,
    max_io_workers: int = 1,
    max_low_workers: int = 1,
    max_exception_workers: int = 1,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run a new immutable frontier with bounded per-stage request windows.

    This is an explicit policy branch. Legacy runs retain their numeric aggregate
    deadline and recovery rules. A callback stage is durably marked running before
    invocation; after interruption that stage is UNKNOWN and is never replayed.
    """
    from closer_frontier_policy import request_window, validate_frontier

    path = Path(state_path)
    selected = list(targets)
    frontier = validate_frontier(finite_frontier)
    if frontier_target is not None:
        if selected != [frontier_target] or frontier_target not in frontier["targets"]:
            raise ValueError("finite pipeline target differs from the immutable frontier")
    elif selected != frontier["targets"]:
        raise ValueError("finite pipeline assignment differs from the immutable frontier")
    if len({closer_resources.canonical_target(target) for target in selected}) != len(selected):
        raise ValueError("finite pipeline targets must be canonically unique")
    request_window(now(), request_timeout_seconds)
    bounds = {"io": max_io_workers, "low": max_low_workers, "exception": max_exception_workers}
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in bounds.values()
    ):
        raise ValueError("finite pipeline pool ceilings must be positive integers")
    callbacks = {
        "prepare": (prepare, "io"),
        "interpret": (interpret, "low"),
        "exception": (exception, "exception"),
        "act": (act, "io"),
    }
    if reconcile is not None:
        callbacks["reconcile"] = (reconcile, "io")
    run_id = _digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[:32]
    lock_path = path.with_name(path.name + ".runlock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    run_lock = lock_path.open("a+")
    try:
        fcntl.flock(run_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        run_lock.close()
        return {
            "schema": "closer-pipeline-receipt/v1",
            "run_id": run_id,
            "assignment": selected,
            "finite_frontier": frontier,
            "valid": False,
            "stop_valid": False,
            "status": "held",
            "continuation_required": True,
            "targets": {},
        }

    lane_by_stage = {name: lane for name, (_callback, lane) in callbacks.items()}
    rows: dict[str, dict[str, Any]] = {}

    @contextlib.contextmanager
    def finite_journal_lock(request_deadline: float | None = None):
        deadline_value = (
            request_window(now(), request_timeout_seconds)["deadline"]
            if request_deadline is None
            else request_deadline
        )
        remaining = deadline_value - float(now())
        if not math.isfinite(remaining) or remaining <= 0:
            raise _FiniteStateLockTimeout("finite pipeline state lock window already expired")
        with closer_resources.inventory_request(deadline_value, now=now):
            with _locked(path, timeout_seconds=remaining):
                yield

    def mutate_frontier(change, *, request_deadline: float | None = None) -> None:
        with finite_journal_lock(request_deadline):
            state = _load(path)
            entries = state.setdefault("frontiers", {})
            entry = entries.get(frontier["frontier_id"])
            if entry is None:
                entry = {
                    "schema": "closer-pipeline-frontier/v2",
                    "run_id": run_id,
                    "finite_frontier": frontier,
                    "created_at": now(),
                    "items": {},
                }
                entries[frontier["frontier_id"]] = entry
            if (
                entry.get("schema") != "closer-pipeline-frontier/v2"
                or entry.get("run_id") != run_id
                or entry.get("finite_frontier") != frontier
            ):
                raise ValueError("finite pipeline frontier identity changed")
            existing_items = entry.get("items")
            if not isinstance(existing_items, dict) or set(existing_items).difference(
                frontier["targets"]
            ):
                raise ValueError("finite pipeline frontier contains a foreign target")
            change(entry)
            entry["updated_at"] = now()
            _save(path, state)

    def control_status(target):
        if cancelled_fn is not None and cancelled_fn():
            return "cancelled"
        if paused_fn is not None and paused_fn():
            return "paused"
        return None

    try:
        # Claim all selected target rows before callbacks. Existing in-flight stages
        # are held UNKNOWN; only a known completed stage may advance after restart.
        def initialize(entry):
            items = entry.setdefault("items", {})
            for target in selected:
                item = items.setdefault(
                    target,
                    {
                        "status": "pending",
                        "stage": "prepare",
                        "results": {},
                        "metrics": {"requests": 0},
                    },
                )
                if item.get("stage_status") == "running":
                    item.update(
                        status="unknown",
                        error="process_interrupted_during_finite_request",
                        deferred_next_trigger="manual_reconciliation",
                    )
                    item["stage_status"] = "unknown"
            rows.update({target: dict(items[target]) for target in selected})

        mutate_frontier(initialize)

        for target in selected:
            while True:
                with finite_journal_lock():
                    state = _load(path)
                    entry = state["frontiers"][frontier["frontier_id"]]
                    stored_items = entry.get("items")
                    if not isinstance(stored_items, dict) or set(stored_items).difference(
                        frontier["targets"]
                    ):
                        raise ValueError("finite pipeline frontier contains a foreign target")
                    item = entry["items"][target]
                    if item.get("status") in {
                        "completed",
                        "held",
                        "paused",
                        "cancelled",
                        "capacity_blocked",
                        "unknown",
                        "failed",
                    }:
                        rows[target] = dict(item)
                        break
                    stage = item.get("stage")
                    if stage not in callbacks:
                        item.update(status="held", error="unsupported_finite_stage")
                        _save(path, state)
                        rows[target] = dict(item)
                        break
                    stop = control_status(target)
                    if stop:
                        item.update(status=stop, stage_status=stop)
                        item["deferred_next_trigger"] = "resume_after_operator_action"
                        _save(path, state)
                        rows[target] = dict(item)
                        break
                    try:
                        capacity = _capacity(capacity_fn)
                    except Exception as exc:
                        item.update(
                            status="capacity_blocked",
                            error=f"capacity_unavailable:{type(exc).__name__}",
                        )
                        item["deferred_next_trigger"] = "fresh_capacity_report"
                        _save(path, state)
                        rows[target] = dict(item)
                        break
                    lane = lane_by_stage[stage]
                    if capacity.get(lane, 0) < 1:
                        item.update(
                            status="capacity_blocked",
                            deferred_reason="worker_capacity",
                            deferred_next_trigger="capacity_recheck",
                            resource_evidence={
                                "capacity": {
                                    "lane": lane,
                                    "available": capacity.get(lane, 0),
                                    "sampled_at": now(),
                                }
                            },
                        )
                        _save(path, state)
                        rows[target] = dict(item)
                        break
                    window = request_window(now(), request_timeout_seconds)
                    item.update(
                        status="running",
                        stage_status="running",
                        stage_started_at=window["started_at"],
                        stage_deadline=window["deadline"],
                        last_scheduled=window["started_at"],
                    )
                    item.setdefault("metrics", {})["requests"] = (
                        int(item.setdefault("metrics", {}).get("requests", 0)) + 1
                    )
                    scope_lease = closer_resources.fence_identity(path)
                    if scope_lease is not None:
                        item["scope_lease"] = scope_lease
                    context = {
                        **item.get("results", {}),
                        "revision": item.get("revision"),
                        "identity": item.get("identity", target),
                        "deadline": window["deadline"],
                        "request_started_at": window["started_at"],
                        "request_timeout_seconds": request_timeout_seconds,
                        "finite_frontier": frontier,
                        "frontier_binding": frontier["bindings"][target],
                        "state_path": str(path),
                        "pipeline_run_id": run_id,
                        "action_state_path": str(
                            path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json"
                        ),
                        "work_key": _digest(
                            [target, stage, item.get("revision"), frontier["bindings"][target]]
                        ),
                    }
                    item["work_key"] = context["work_key"]
                    with closer_resources.inventory_request(window["deadline"], now=now):
                        _save(path, state)
                    rows[target] = dict(item)

                callback = callbacks[stage][0]
                try:
                    result = callback(target, _bounded(context, limit=256 * 1024))
                    if not isinstance(result, Mapping):
                        raise ValueError("callback must return an object")
                    result = _bounded(dict(result), limit=CALLBACK_RESULT_BYTES)
                except Exception as exc:
                    # Model/provider stages may have started a billable request
                    # before transport loss. Preserve that unresolved attempt as
                    # UNKNOWN so a later frontier cannot silently repeat it.
                    terminal = (
                        "unknown"
                        if stage in {"interpret", "exception", "act", "reconcile"}
                        else "held"
                    )
                    error_text = f"{type(exc).__name__}: {str(exc)[:200]}"

                    def fail(entry):
                        current = entry["items"][target]
                        current.update(
                            status=terminal,
                            stage_status=terminal,
                            error=error_text,
                            deferred_next_trigger=(
                                "manual_reconciliation"
                                if terminal == "unknown"
                                else "operator_review"
                            ),
                        )

                    mutate_frontier(fail, request_deadline=window["deadline"])
                    continue

                # An elapsed request window never authorizes a second attempt. A late
                # read is held; a late action is UNKNOWN because an effect may exist.
                if now() >= window["deadline"]:
                    terminal = (
                        "unknown"
                        if stage in {"interpret", "exception", "act", "reconcile"}
                        else "held"
                    )

                    def late(entry):
                        current = entry["items"][target]
                        current.setdefault("late_results", {})[stage] = result
                        current.update(
                            status=terminal,
                            stage_status=terminal,
                            error="finite request window expired",
                            deferred_next_trigger=(
                                "manual_reconciliation"
                                if terminal == "unknown"
                                else "fresh_observation"
                            ),
                        )

                    mutate_frontier(late, request_deadline=window["deadline"])
                    continue

                def complete_stage(entry):
                    current = entry["items"][target]
                    current.setdefault("results", {})[stage] = result
                    current.setdefault("stage_results", {})[stage] = {
                        "status": "completed",
                        "started_at": window["started_at"],
                        "finished_at": now(),
                        "deadline": window["deadline"],
                        "work_key": context["work_key"],
                    }
                    current["stage_status"] = "completed"
                    if stage == "prepare":
                        revision = result.get("revision")
                        expected = frontier["bindings"][target]["revision"]
                        if revision != expected:
                            current.update(status="held", error="frontier revision binding differs")
                        else:
                            current.update(
                                status="pending",
                                revision=revision,
                                identity=result.get("identity", target),
                                stage="interpret",
                            )
                    elif stage == "interpret":
                        if (
                            result.get("status") in {"uncertain", "unknown", "running", "submitted"}
                            or result.get("provider_outcome_uncertain") is not None
                        ):
                            current.update(
                                status="unknown", error="interpretation outcome is unresolved"
                            )
                            current["deferred_next_trigger"] = result.get(
                                "next_trigger", "manual_reconciliation"
                            )
                        elif result.get("classification") == "capacity_deferred" or (
                            result.get("status") == "deferred"
                            and result.get("reason") == "worker_capacity"
                        ):
                            current.update(
                                status="capacity_blocked",
                                deferred_reason=result.get("reason", "capacity"),
                            )
                            current["deferred_next_trigger"] = result.get(
                                "next_trigger", "capacity_recheck"
                            )
                        elif result.get("status") == "pending":
                            pending_kind = result.get("pending_kind")
                            pending_evidence = result.get("pending_evidence")
                            pending_field = {
                                "ci": "check_run_id",
                                "async_operation": "operation_id",
                                "external_event": "event_id",
                            }.get(pending_kind)
                            pending_revision = (
                                pending_evidence.get("observed_revision")
                                or pending_evidence.get("revision")
                                if isinstance(pending_evidence, Mapping)
                                else None
                            )
                            assignment_hold = (
                                pending_kind == "external_event"
                                and isinstance(pending_evidence, Mapping)
                                and isinstance(pending_evidence.get("fingerprint"), str)
                                and bool(pending_evidence["fingerprint"])
                                and pending_evidence.get("execution_state") not in {None, "active"}
                            )
                            pending_identifier = (
                                pending_evidence.get(pending_field)
                                if isinstance(pending_evidence, Mapping) and pending_field
                                else None
                            )
                            valid_pending_identifier = (
                                type(pending_identifier) is int and pending_identifier > 0
                                if pending_kind == "ci"
                                else isinstance(pending_identifier, str)
                                and bool(pending_identifier.strip())
                            )
                            if (
                                pending_field
                                and isinstance(pending_evidence, Mapping)
                                and pending_evidence.get("target") == target
                                and valid_pending_identifier
                                and (pending_revision == current.get("revision") or assignment_hold)
                                and isinstance(result.get("next_trigger"), str)
                                and result["next_trigger"].strip()
                            ):
                                current.update(
                                    status="held",
                                    deferred_reason=f"pending_{pending_kind}",
                                )
                                current["deferred_next_trigger"] = result["next_trigger"]
                            else:
                                current.update(
                                    status="unknown",
                                    error="interpretation pending result lacks exact event evidence",
                                )
                                current["deferred_next_trigger"] = "manual_reconciliation"
                        elif result.get("status") == "deferred":
                            evidence = result.get("resource_evidence")
                            if (
                                result.get("reason") == "provider_failure"
                                and isinstance(result.get("next_trigger"), str)
                                and result["next_trigger"].strip()
                                and _valid_provider_failure(evidence)
                            ):
                                current.update(
                                    status="held",
                                    deferred_reason="provider_failure",
                                    resource_evidence=dict(evidence),
                                )
                                current["deferred_next_trigger"] = result["next_trigger"]
                            else:
                                current.update(
                                    status="unknown",
                                    error="interpretation deferral lacks validated no-effect evidence",
                                )
                                current["deferred_next_trigger"] = "manual_reconciliation"
                        elif result.get("substantive_needs"):
                            current.update(status="pending", stage="exception")
                        elif result.get("substantive_needs") == []:
                            current.update(status="pending", stage="act")
                        else:
                            current.update(
                                status="held",
                                error="interpretation lacks explicit substantive_needs",
                            )
                    elif stage == "exception":
                        if (
                            result.get("status") in {"uncertain", "unknown", "running", "submitted"}
                            or result.get("provider_outcome_uncertain") is not None
                        ):
                            current.update(
                                status="unknown", error="exception outcome is unresolved"
                            )
                            current["deferred_next_trigger"] = result.get(
                                "next_trigger", "manual_reconciliation"
                            )
                        elif result.get("status") in {"failed", "deferred", "pending"}:
                            current.update(
                                status=(
                                    "capacity_blocked"
                                    if result.get("reason") == "worker_capacity"
                                    else "held"
                                ),
                                error=result.get("reason", "exception did not produce a route"),
                            )
                            current["deferred_next_trigger"] = result.get(
                                "next_trigger", "operator_review"
                            )
                        else:
                            current.update(status="pending", stage="act")
                    elif stage == "act":
                        status = result.get("status")
                        if status == "completed" and result.get("target_complete") is True:
                            current.update(status="completed", stage_status="completed")
                        elif status == "completed" and result.get("target_complete") is False:
                            # A finite target may make one verified queue transition
                            # without finishing its overall workflow. Preserve that
                            # proof and stop this frontier; only a newly reviewed
                            # frontier may schedule the next transition.
                            queue_receipt = result.get("queue_receipt")
                            progress_key = result.get("progress_key")
                            action_state_path = context.get("action_state_path")
                            verified_progress = False
                            if (
                                isinstance(action_state_path, str)
                                and isinstance(queue_receipt, Mapping)
                                and isinstance(progress_key, str)
                                and progress_key
                            ):
                                try:
                                    import closer_queue

                                    checked = closer_queue.validate_stop(
                                        action_state_path,
                                        [target],
                                        receipt=queue_receipt,
                                        frontier_id=frontier["frontier_id"],
                                    )
                                    queue_state = closer_queue._read_state(Path(action_state_path))
                                    queue_frontier_state = closer_queue.state_for_frontier(
                                        queue_state,
                                        frontier["frontier_id"],
                                        expected_policy=frontier,
                                    )
                                    queue_item = (
                                        queue_frontier_state.get("items", {}).get(target)
                                        if isinstance(queue_frontier_state, Mapping)
                                        else None
                                    )
                                    accounting = queue_receipt.get("accounting")
                                    queue_row = (
                                        next(
                                            (
                                                row
                                                for row in accounting
                                                if isinstance(row, Mapping)
                                                and row.get("target") == target
                                            ),
                                            None,
                                        )
                                        if isinstance(accounting, list)
                                        else None
                                    )
                                    expected_progress_key = (
                                        hashlib.sha256(
                                            json.dumps(
                                                {
                                                    "revision": queue_row.get("revision"),
                                                    "action": queue_row.get("action"),
                                                    "operation_id": queue_row.get("operation_id"),
                                                    "operation_status": queue_row.get(
                                                        "operation_status"
                                                    ),
                                                    "events": queue_receipt.get("events", [])[-8:],
                                                },
                                                sort_keys=True,
                                            ).encode()
                                        ).hexdigest()
                                        if isinstance(queue_row, Mapping)
                                        else None
                                    )
                                    operation_id = (
                                        queue_row.get("operation_id")
                                        if isinstance(queue_row, Mapping)
                                        else None
                                    )
                                    operations = []
                                    if isinstance(queue_item, Mapping):
                                        operations.append(queue_item.get("operation"))
                                        history = queue_item.get("operation_history", [])
                                        if isinstance(history, list):
                                            operations.extend(history)
                                    completed_original_action = False
                                    if isinstance(operation_id, str) and operation_id:
                                        from closer_frontier_policy import action_identity

                                        for operation in operations:
                                            if not isinstance(operation, Mapping):
                                                continue
                                            revision = operation.get("revision")
                                            action = operation.get("action")
                                            binding = operation.get("binding")
                                            try:
                                                identity = action_identity(
                                                    target, revision, action, binding
                                                )
                                            except (TypeError, ValueError):
                                                continue
                                            if (
                                                operation.get("id") == operation_id
                                                and operation.get("status") == "completed"
                                                and operation.get("finite_frontier") == frontier
                                                and operation.get("revision")
                                                == frontier["bindings"][target]["revision"]
                                                and identity
                                                == frontier["bindings"][target]["action_id"]
                                            ):
                                                completed_original_action = True
                                                break
                                    verified_progress = bool(
                                        checked.get("valid")
                                        and checked.get("stop_valid")
                                        and queue_receipt.get("stop_valid")
                                        and isinstance(queue_row, Mapping)
                                        and queue_row.get("eligible") is True
                                        and (
                                            queue_row.get("status") in {"completed", "observed"}
                                            or (
                                                queue_row.get("status") == "deferred"
                                                and queue_row.get("reason") == "pass_limit"
                                            )
                                        )
                                        and queue_row.get("operation_status") == "completed"
                                        and completed_original_action
                                        and expected_progress_key == progress_key
                                    )
                                except (OSError, TypeError, ValueError, KeyError):
                                    verified_progress = False
                            if verified_progress:
                                current.update(
                                    status="held",
                                    stage_status="completed",
                                    progress_key=progress_key,
                                    deferred_next_trigger="new_frontier_required",
                                    error="verified partial action progress; new frontier required",
                                )
                            else:
                                current.update(
                                    status="unknown",
                                    error="partial action progress lacks an exact verified queue receipt",
                                )
                                current["deferred_next_trigger"] = "manual_reconciliation"
                        elif status in {"uncertain", "running", "submitted", "unknown", "failed"}:
                            current.update(status="unknown", error="action outcome is unresolved")
                            current["deferred_next_trigger"] = result.get(
                                "next_trigger", "manual_reconciliation"
                            )
                        elif status == "deferred" and result.get("reason") == "worker_capacity":
                            current.update(
                                status="capacity_blocked", deferred_reason="worker_capacity"
                            )
                            current["deferred_next_trigger"] = result.get(
                                "next_trigger", "capacity_recheck"
                            )
                        elif status in {"pending", "deferred", "blocked", "not_started"}:
                            # A status label alone cannot prove no effect. Require
                            # the exact inner queue stop receipt and a matching
                            # accounting row before classifying the wait as held.
                            action_state_path = context.get("action_state_path")
                            queue_receipt = result.get("queue_receipt")
                            reason = result.get("reason")
                            trigger = result.get("next_trigger")
                            row = None
                            proof_ok = False
                            if (
                                isinstance(action_state_path, str)
                                and isinstance(queue_receipt, Mapping)
                                and isinstance(trigger, str)
                                and trigger.strip()
                            ):
                                try:
                                    import closer_queue

                                    checked = closer_queue.validate_stop(
                                        action_state_path,
                                        [target],
                                        receipt=queue_receipt,
                                        frontier_id=frontier["frontier_id"],
                                    )
                                    rows = queue_receipt.get("accounting")
                                    row = (
                                        next(
                                            (
                                                value
                                                for value in rows
                                                if isinstance(value, Mapping)
                                                and value.get("target") == target
                                            ),
                                            None,
                                        )
                                        if isinstance(rows, list)
                                        else None
                                    )
                                    proof_ok = bool(
                                        checked.get("valid")
                                        and checked.get("stop_valid")
                                        and queue_receipt.get("stop_valid") is True
                                        and isinstance(row, Mapping)
                                        and row.get("operation_status")
                                        not in {
                                            "running",
                                            "submitted",
                                            "uncertain",
                                        }
                                    )
                                    if status == "pending":
                                        kind = result.get("pending_kind")
                                        evidence = result.get("pending_evidence")
                                        field = {
                                            "ci": "check_run_id",
                                            "async_operation": "operation_id",
                                            "external_event": "event_id",
                                        }.get(kind)
                                        identifier = (
                                            evidence.get(field)
                                            if isinstance(evidence, Mapping) and field
                                            else None
                                        )
                                        valid_identifier = (
                                            type(identifier) is int and identifier > 0
                                            if kind == "ci"
                                            else isinstance(identifier, str)
                                            and bool(identifier.strip())
                                        )
                                        proof_ok = bool(
                                            proof_ok
                                            and field
                                            and isinstance(evidence, Mapping)
                                            and evidence.get("target") == target
                                            and valid_identifier
                                            and evidence.get("observed_revision")
                                            == row.get("revision")
                                            and (
                                                (
                                                    kind in {"ci", "async_operation"}
                                                    and row.get("eligible") is False
                                                    and row.get("action") == "wait"
                                                )
                                                or (
                                                    kind == "external_event"
                                                    and row.get("action") == "collect_evidence"
                                                    and row.get("reason")
                                                    == "collect terminal or unattributed verifier evidence; do not rerun"
                                                )
                                            )
                                        )
                                    elif reason == "validated_inner_wait":
                                        resource = result.get("resource_evidence")
                                        proof = (
                                            resource.get("inner_wait")
                                            if isinstance(resource, Mapping)
                                            else None
                                        )
                                        prepared = context.get("prepare")
                                        snapshot = (
                                            prepared.get("snapshot")
                                            if isinstance(prepared, Mapping)
                                            else None
                                        )
                                        proof_ok = bool(
                                            proof_ok
                                            and isinstance(proof, Mapping)
                                            and _valid_inner_wait(
                                                path,
                                                target,
                                                context.get("revision"),
                                                snapshot,
                                                proof.get("reason"),
                                                trigger,
                                                proof,
                                                frontier_id=frontier["frontier_id"],
                                                finite_frontier=frontier,
                                            )
                                        )
                                    else:
                                        proof_ok = bool(
                                            proof_ok
                                            and row.get("reason") == reason
                                            and reason in closer_queue.RESOURCE_DEFERRALS
                                            and reason
                                            not in {"operation_running", "repo_operation_running"}
                                        )
                                except (OSError, TypeError, ValueError, KeyError):
                                    proof_ok = False
                            if proof_ok:
                                current.update(
                                    status="held",
                                    deferred_reason=reason or result.get("pending_kind"),
                                    queue_receipt=dict(queue_receipt),
                                )
                                if reason == "validated_inner_wait":
                                    evidence = result.get("resource_evidence")
                                    if isinstance(evidence, Mapping):
                                        current["resource_evidence"] = dict(evidence)
                                current["deferred_next_trigger"] = trigger
                            else:
                                current.update(
                                    status="unknown",
                                    error="action wait lacks exact verified no-effect queue proof",
                                )
                                current["deferred_next_trigger"] = "manual_reconciliation"
                        else:
                            current.update(
                                status="unknown", error="action returned an unsupported outcome"
                            )
                            current["deferred_next_trigger"] = "manual_reconciliation"

                mutate_frontier(complete_stage, request_deadline=window["deadline"])

        with finite_journal_lock():
            state = _load(path)
            entry = state["frontiers"][frontier["frontier_id"]]
            rows = {target: dict(entry["items"][target]) for target in selected}
            _save(path, state)
        frontier_terminal = all(
            row.get("status") in {"completed", "held", "paused", "cancelled", "capacity_blocked"}
            for row in rows.values()
        )
        accepted = frontier_terminal and all(
            row.get("status") == "completed" for row in rows.values()
        )
        return {
            "schema": "closer-pipeline-receipt/v1",
            "run_id": run_id,
            "assignment": selected,
            "finite_frontier": frontier,
            "targets": rows,
            "status": "completed" if accepted else "held" if frontier_terminal else "unknown",
            "valid": frontier_terminal
            and not any(row.get("status") == "unknown" for row in rows.values()),
            "stop_valid": frontier_terminal
            and not any(row.get("status") == "unknown" for row in rows.values()),
            "continuation_required": not frontier_terminal,
            "finished_at": now(),
        }
    except _FiniteStateLockTimeout:
        # If intent was already written, leave its durable stage_status=running
        # untouched. Restart converts that exact in-flight stage to UNKNOWN, so
        # a possibly submitted action cannot be replayed. The local receipt is
        # also fail-closed and does not claim a terminal stop.
        for target in selected:
            row = rows.get(target, {})
            if row.get("stage_status") == "running" or row.get("status") == "running":
                row.update(
                    status="unknown",
                    stage_status="unknown",
                    error="finite state journal lock unavailable after intent",
                    deferred_next_trigger="manual_reconciliation",
                )
            else:
                row.setdefault("status", "held")
            rows[target] = row
        return {
            "schema": "closer-pipeline-receipt/v1",
            "run_id": run_id,
            "assignment": selected,
            "finite_frontier": frontier,
            "targets": rows,
            "status": (
                "unknown"
                if any(row.get("status") == "unknown" for row in rows.values())
                else "held"
            ),
            "valid": False,
            "stop_valid": False,
            "continuation_required": True,
            "error": "finite state journal lock timed out",
        }
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
        run_lock.close()


def _run_pipeline_legacy(
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
    priority_targets: Iterable[str] = (),
    max_io_workers: int = 4,
    max_low_workers: int = 2,
    max_exception_workers: int = 1,
    round_deadline_exception: type[BaseException] | None = None,
    round_capacity_exception: type[BaseException] | None = None,
    now: Callable[[], float] = time.time,
    _terminal_boundary: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the scheduler while guaranteeing its process lease is always released."""
    held: list[Any] = []
    try:
        receipt = _run_pipeline_impl(
            state_path,
            targets,
            prepare=prepare,
            interpret=interpret,
            act=act,
            exception=exception,
            reconcile=reconcile,
            capacity_fn=capacity_fn,
            deadline=deadline,
            priority_targets=priority_targets,
            max_io_workers=max_io_workers,
            max_low_workers=max_low_workers,
            max_exception_workers=max_exception_workers,
            round_deadline_exception=round_deadline_exception,
            round_capacity_exception=round_capacity_exception,
            now=now,
            _run_lock_ref=held,
        )
        # Keep the process runlock across durable stop validation and this owner's
        # alias cleanup. A successor must not observe a completed run while its
        # still-live predecessor is between receipt persistence and advertisement release.
        if _terminal_boundary is not None and held and not held[0].closed:
            _terminal_boundary(receipt)
        return receipt
    finally:
        if held:
            run_lock = held[0]
            if not run_lock.closed:
                with contextlib.suppress(OSError):
                    fcntl.flock(run_lock.fileno(), fcntl.LOCK_UN)
                run_lock.close()


def run_pipeline(state_path: str | Path, targets: Iterable[str], **kwargs: Any) -> dict[str, Any]:
    """Run the scheduler. ``coordinator_mode`` is ``legacy`` (default; one round lock and one
    shared ledger) or ``resource_scoped`` (opt-in; per-target leases and shards, see
    ``closer_resources.run_scoped``). Unset, it reads ``ORCH_CLOSER_COORDINATOR_MODE``."""
    mode = closer_resources.resolve_mode(kwargs.pop("coordinator_mode", None))
    contract_pin = kwargs.pop("coordinator_contract_pin", None)
    admission = kwargs.pop("coordination_admission", None)
    finite_frontier = kwargs.pop("finite_frontier", None)
    if finite_frontier is not None and mode != "resource_scoped":
        raise ValueError("finite frontier requires explicit resource_scoped coordination")
    if finite_frontier is not None:
        _require_dispatcher_finite_callbacks(kwargs)
    if mode == "legacy":
        if contract_pin is not None:
            raise ValueError("coordinator_contract_pin applies to resource_scoped mode only")
        assigned = list(targets)
        canonical = [closer_resources.canonical_target(t) for t in assigned]
        if len(set(canonical)) != len(canonical):
            raise ValueError("targets must be unique when compared canonically")
        resource_keys = kwargs.pop("resource_keys", None)
        kwargs.pop("pool_lease", None)
        kwargs.pop("quarantine_workspaces", None)
        now = kwargs.get("now", time.time)
        owner = f"legacy:{os.getpid()}:{threading.get_ident()}:{now()}"
        with contextlib.ExitStack() as gates:
            held_rows = {}
            gated_targets = []
            for target in sorted(assigned, key=closer_resources.canonical_target):
                try:
                    gates.enter_context(
                        closer_resources.admission_gate(
                            admission, state_path, target, mode="legacy"
                        )
                    )
                    gated_targets.append(target)
                except closer_resources.ResourceBlocked:
                    held_rows[target] = closer_resources._fenced_row("resource_contested")
            fenced_rows, runnable = closer_resources.rollback_fence(state_path, gated_targets)
            fenced_rows.update(held_rows)
            if not runnable:
                return closer_resources.legacy_refusal_receipt(
                    state_path, assigned, fenced_rows, float(kwargs.get("deadline", 0)), now
                )
            original_act = kwargs["act"]

            def guarded_act(target, ctx):
                if (
                    resource_keys is None
                    and not closer_resources.scoped_root(state_path).exists()
                    and not closer_resources.unknown_reservations(
                        closer_resources.scoped_root(state_path)
                    )
                ):
                    return original_act(target, ctx)
                if resource_keys is None:
                    with closer_resources.mutation_reservation(
                        state_path,
                        target,
                        ctx,
                        None,
                        owner=owner,
                        deadline=kwargs["deadline"],
                        now=now,
                    ):
                        return original_act(target, ctx)
                keys = [
                    *closer_resources.default_resource_keys(target),
                    *resource_keys(target, ctx),
                ]
                with closer_resources.legacy_reserve(
                    closer_resources.scoped_root(state_path),
                    keys,
                    target=target,
                    owner=owner,
                    deadline=kwargs["deadline"],
                    now=now,
                    causal_binding={
                        "pipeline_run_id": ctx["pipeline_run_id"],
                        "revision": ctx["revision"],
                        "identity": ctx["identity"],
                        "work_key": ctx["work_key"],
                    },
                ):
                    return original_act(target, ctx)

            kwargs["act"] = guarded_act

            def terminal_boundary(receipt):
                # Preserve legacy's existing known asynchronous waits. Its advertisement
                # ends only after the durable ledger validates those native continuation
                # proofs. Scoped Core intents still require their COMPLETE receipt.
                if resource_keys is not None and _validate_stop_legacy(
                    state_path, runnable, now=now
                ).get("valid"):
                    closer_resources.release_legacy_boundary(
                        closer_resources.scoped_root(state_path), owner
                    )
                for target, row in receipt.get("targets", {}).items():
                    if row.get("status") == "completed":
                        closer_resources.release_target(
                            closer_resources.scoped_root(state_path), target, owner=owner
                        )

            receipt = _run_pipeline_legacy(
                state_path, runnable, _terminal_boundary=terminal_boundary, **kwargs
            )
            if fenced_rows:
                receipt["targets"] = {**receipt.get("targets", {}), **fenced_rows}
                receipt["scoped_rollback"] = {"fenced_targets": sorted(fenced_rows), "drainable": 0}
            return receipt
    return closer_resources.run_scoped(
        state_path,
        targets,
        contract_pin=contract_pin,
        coordination_admission=admission,
        **({"finite_frontier": finite_frontier} if finite_frontier is not None else {}),
        **kwargs,
    )


def validate_stop_scoped(
    state_path: str | Path,
    round_id: str,
    assignment_targets: Iterable[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """``validate_stop`` for one named resource-scoped round (see closer_resources.validate_stop)."""
    return closer_resources.validate_stop(state_path, round_id, assignment_targets, **kwargs)


def scoped_capability() -> dict[str, Any]:
    """Version-pin capability for a release installer (integration unproved here)."""
    return closer_resources.capability(SCHEMA)


def _save_state_locked(path: Path, state: Mapping[str, Any]) -> None:
    """Persist a state already read while holding the coordinator lock."""
    _save(path, state)


def _validate_stop_legacy(
    state_path: str | Path,
    assignment_targets: Iterable[str] | None = None,
    *,
    now: Callable[[], float] = time.time,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate outer pipeline accounting and whether continuation is due.

    This deliberately accepts only ``closer-pipeline/v1``. Legacy
    ``closer-queue/v1`` state remains owned by closer_queue.validate_stop.
    A supplied expected_run_id selects only that exact existing assignment/run;
    native proof still validates at its original path with no latest fallback.
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
        and (expected_run_id is None or key == expected_run_id)
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
        if status == "uncertain" and stage in {"interpret", "exception"}:
            continuation = True
            outcome = item.get("provider_outcome_uncertain")
            actual = outcome.get("actual") if isinstance(outcome, Mapping) else None
            if (
                isinstance(outcome, Mapping)
                and outcome.get("schema") == "closer-provider-outcome-uncertain/v1"
                and isinstance(actual, Mapping)
                and isinstance(actual.get("run_id"), str)
                and actual.get("run_id")
            ):
                reasons.append(f"unresolved_native_provider_outcome:{target}:{actual['run_id']}")
            else:
                reasons.append(f"unbound_uncertain_provider_outcome:{target}")
            continue
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
                "validated_inner_wait",
                "scope_quarantined",
            }
            if reason not in allowed or not isinstance(trigger, str) or not trigger:
                reasons.append(f"unverified_deferral:{target}")
            elif reason == "scope_quarantined" and not (
                trigger == "reconcile_scope_before_retry"
                and closer_resources.valid_scope_refusal(path, target, item, run_id)
            ):
                reasons.append(f"unverified_scope_refusal:{target}")
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
            elif reason == "validated_inner_wait":
                resource = item.get("resource_evidence")
                proof = resource.get("inner_wait") if isinstance(resource, Mapping) else None
                prepared = item.get("results", {}).get("prepare", {})
                snapshot = prepared.get("snapshot") if isinstance(prepared, Mapping) else None
                if not _valid_inner_wait(
                    path,
                    target,
                    item.get("revision"),
                    snapshot,
                    proof.get("reason") if isinstance(proof, Mapping) else None,
                    trigger,
                    proof,
                    frontier_id=proof.get("frontier_id") if isinstance(proof, Mapping) else None,
                    now=now,
                ):
                    reasons.append(f"unverified_inner_wait:{target}")
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


def _validate_stop_finite(
    state_path: str | Path,
    assignment_targets: Iterable[str] | None,
    *,
    finite_frontier,
    frontier_target: str,
    expected_run_id: str | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Validate one exact target receipt from an immutable finite frontier."""
    from closer_frontier_policy import validate_frontier

    try:
        frontier = validate_frontier(finite_frontier)
        if frontier_target not in frontier["targets"]:
            raise ValueError("target is outside finite frontier")
        if assignment_targets is not None and list(assignment_targets) != [frontier_target]:
            raise ValueError("finite frontier assignment differs")
        state = _load(Path(state_path))
        entry = state.get("frontiers", {}).get(frontier["frontier_id"])
        if (
            not isinstance(entry, Mapping)
            or entry.get("schema") != "closer-pipeline-frontier/v2"
            or entry.get("finite_frontier") != frontier
            or (expected_run_id is not None and entry.get("run_id") != expected_run_id)
        ):
            raise ValueError("no exact finite frontier receipt")
        item = entry.get("items", {}).get(frontier_target)
        if not isinstance(item, Mapping):
            raise ValueError("finite frontier target row missing")
        status = item.get("status")
        terminal = status in {"completed", "held", "paused", "cancelled", "capacity_blocked"}
        unknown = status == "unknown"
        valid = terminal and not unknown
        unverified_inner_wait = False
        if valid and status == "held" and item.get("deferred_reason") == "validated_inner_wait":
            resource = item.get("resource_evidence")
            proof = resource.get("inner_wait") if isinstance(resource, Mapping) else None
            prepared = item.get("results", {}).get("prepare", {})
            snapshot = prepared.get("snapshot") if isinstance(prepared, Mapping) else None
            valid = _valid_inner_wait(
                Path(state_path),
                frontier_target,
                item.get("revision"),
                snapshot,
                proof.get("reason") if isinstance(proof, Mapping) else None,
                item.get("deferred_next_trigger"),
                proof,
                frontier_id=frontier["frontier_id"],
                finite_frontier=frontier,
                now=now,
            )
            unverified_inner_wait = not valid
        return {
            "valid": valid,
            "stop_valid": valid,
            "run_id": entry["run_id"],
            "assignment": [frontier_target],
            "targets": {frontier_target: dict(item)},
            "finite_frontier": frontier,
            "continuation_required": not valid,
            "reasons": (
                []
                if valid
                else [
                    (
                        "finite_frontier_unverified_inner_wait"
                        if unverified_inner_wait
                        else f"finite_frontier_{status or 'unaccounted'}"
                    )
                ]
            ),
        }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {
            "valid": False,
            "stop_valid": False,
            "continuation_required": True,
            "reasons": [f"invalid_finite_frontier_receipt:{exc}"],
        }


def validate_stop(state_path, assignment_targets=None, **kwargs):
    """Legacy accounting plus the common mutation-intent fence (never ignore unknowns)."""
    finite_frontier = kwargs.pop("finite_frontier", None)
    frontier_target = kwargs.pop("frontier_target", None)
    if finite_frontier is not None:
        if not isinstance(frontier_target, str) or not frontier_target:
            result = {
                "valid": False,
                "stop_valid": False,
                "continuation_required": True,
                "reasons": ["finite_frontier_target_missing"],
            }
        else:
            result = _validate_stop_finite(
                state_path,
                assignment_targets,
                finite_frontier=finite_frontier,
                frontier_target=frontier_target,
                **kwargs,
            )
    else:
        result = _validate_stop_legacy(state_path, assignment_targets, **kwargs)
    unknown = closer_resources.unknown_reservations(closer_resources.scoped_root(state_path))
    if unknown:
        result = {
            **result,
            "valid": False,
            "stop_valid": False,
            "continuation_required": True,
            "reasons": [*result.get("reasons", []), "unresolved_resource_reservations"],
            "unknown_reservations": unknown,
        }
    return result
