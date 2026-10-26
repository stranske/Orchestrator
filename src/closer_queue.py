"""Durable, bounded scheduler core for closer actions.

This module only schedules and records work.  An observation is a proposal, not
authority to mutate a GitHub resource: an execution adapter must perform its own
fresh authoritative checks (including exact-head and policy checks) immediately
before any write.

Execution callbacks run in a bounded thread pool so independent repositories can
progress concurrently. They must honor their operation deadline; the coordinator
waits for every launched callback before writing its final receipt. Long-running
remote workers return ``running`` and are polled with ``reconcile``.
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
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import claims

SCHEMA = "closer-queue/v1"
RECEIPT_SCHEMA = "closer-queue-receipt/v1"
READ_ONLY_ACTIONS = frozenset({"collect_evidence", "review", "disposition"})
TERMINAL_ACTIONS = frozenset({"wait", "complete"})
MUTATING_ACTIONS = frozenset(
    {
        "respond_review",
        "repair",
        "initiate_verify",
        "merge",
        "post_merge_verify",
        "close_source",
    }
)
ACTION_NAMES = READ_ONLY_ACTIONS | TERMINAL_ACTIONS | MUTATING_ACTIONS
PENDING_OPERATION_STATES = frozenset({"submitted", "running", "uncertain"})
RESOURCE_DEFERRALS = frozenset(
    {
        "claim_held",
        "worker_capacity",
        "operation_running",
        "repo_operation_running",
        "receipt_reserve",
        "pass_limit",
        "finite_frontier_identity_mismatch",
        "observation_failed",
        "verifier_resource_wait",
        "merge_guard_refused",
    }
)
DEFAULT_RECEIPT_RESERVE_SECONDS = 15.0
DEFAULT_MIN_ACTION_SECONDS = 1.0
MAX_OPERATION_HISTORY = 12
MAX_CALLBACK_JSON_BYTES = 128 * 1024
_ledger_threads = threading.local()
_callback_threads = threading.local()


class QueueStateError(ValueError):
    """The durable queue is malformed or incompatible; fail closed."""


def _native_verify_wait_executor(execute):
    """Only the existing native adapter/registered ACT may assert this boundary."""
    import sys

    import closer_dispatcher
    import closer_pipeline
    import gh_capacity

    function = getattr(execute, "__func__", execute)
    namespace = getattr(function, "__globals__", {})
    trusted = [vars(closer_dispatcher)]
    main = sys.modules.get("__main__")
    if (
        main is not None
        and getattr(main, "__file__", None)
        and Path(main.__file__).resolve() == Path(closer_dispatcher.__file__).resolve()
    ):
        trusted.append(vars(main))
    if not any(namespace is actual for actual in trusted):
        return False
    native_class = namespace.get("Closer")
    current = getattr(execute, "__self__", None)
    if current is None:
        code = getattr(function, "__code__", None)
        if not any(
            getattr(act, "__globals__", None) is namespace
            and any(value is code for value in getattr(act, "__code__").co_consts)
            and getattr(code, "co_qualname", "")
            == "_run_staged_pipeline.<locals>.act.<locals>.execute_bound"
            for act in closer_pipeline._FINITE_DISPATCHER_CALLBACKS
            if getattr(act, "__code__", None) is not None
        ):
            return False
        closure = dict(zip(function.__code__.co_freevars, function.__closure__ or ()))
        current = closure.get("current")
        current = current.cell_contents if current is not None else None
    elif native_class is None or function is not native_class.execute:
        return False
    return (
        native_class is not None
        and type(current) is native_class
        and Path(namespace.get("__file__", "")).name == "closer_dispatcher.py"
        and current.offload_fn is None
        and current.run_fn is namespace.get("bounded_run")
        and current.gh.api_fn is gh_capacity.gh_run
        and current.runtime_binding is None
        and not current.require_runtime_binding
    )


def merge_guard_refusal_proof(operation, target):
    """Retain an invoked native callback whose guard rejected before merge."""
    import merge_guard

    if not isinstance(operation, dict):
        return None
    result = operation.get("result")
    binding = operation.get("binding")
    if not isinstance(result, dict) or not isinstance(binding, dict):
        return None
    proof = result.get("merge_guard_refusal")
    report = result.get("result")
    subject = binding.get("pr_target", target)
    expected = {
        "schema": "closer-merge-guard-refusal/v1",
        "target": target,
        "subject_pr": subject,
        "head": binding.get("head"),
        "revision": operation.get("revision"),
        "operation_id": operation.get("id"),
        "report_sha256": _digest(report),
        "callback_invoked": True,
        "merge_command_invoked": False,
        "native_remote_only": True,
    }
    if (
        operation.get("action") == "merge"
        and operation.get("status") == "failed"
        and operation.get("outcome") == "blocked"
        and isinstance(operation.get("id"), str)
        and bool(operation["id"])
        and isinstance(operation.get("revision"), str)
        and isinstance(binding.get("head"), str)
        and len(binding["head"]) == 40
        and all(char in "0123456789abcdef" for char in binding["head"])
        and binding.get("merge") is None
        and isinstance(proof, dict)
        and proof.get("callback_invoked") is True
        and proof.get("merge_command_invoked") is False
        and proof.get("native_remote_only") is True
        and proof == expected
        and merge_guard.no_merge_command_refusal(report, subject, binding["head"])
    ):
        return proof
    return None


def verifier_resource_wait_proof(operation, target):
    """Validate retained native pre-POST proof; no verdict or effect authority."""
    import re

    import closer_verifier_boundary

    if isinstance(operation, dict) and isinstance(operation.get("result"), dict):
        candidate = operation["result"].get("verifier_resource_wait")
        if (
            isinstance(candidate, dict)
            and candidate.get("schema") == closer_verifier_boundary.SCHEMA
        ):
            return closer_verifier_boundary.validate(operation, target)
    try:
        proof = operation["result"]["verifier_resource_wait"]
        value = {k: v for k, v in proof.items() if k != "sha256"}
        binding = operation["binding"]
        run = proof["blocking_run"]
        subject = binding.get("pr_target", target)
        repo, number = subject.rsplit("#", 1)
        linked = run["pull_requests"]
        if (
            operation.get("status") != "resource_wait"
            or operation.get("action") not in {"post_merge_verify", "initiate_verify"}
            or proof.get("schema") != "closer-verifier-prewrite-wait/v1"
            or proof.get("boundary") != "before_verify_label_post"
            or proof.get("sha256") != _digest(value)
            or proof.get("target") != target
            or proof.get("subject_pr") != subject
            or proof.get("revision") != operation.get("revision")
            or proof.get("head") != binding.get("head")
            or not re.fullmatch("[0-9a-f]{40}", proof["head"])
            or proof.get("merge") != binding.get("merge")
            or not re.fullmatch("[0-9a-f]{40}", proof["merge"])
            or type(run.get("id")) is not int
            or run["id"] < 1
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] < 1
            or run.get("path") != ".github/workflows/agents-verifier.yml"
            or run.get("event") not in {"workflow_dispatch", "pull_request_target"}
            or run.get("status") not in {"queued", "in_progress", "waiting", "requested", "pending"}
            or run.get("conclusion") is not None
            or run.get("repository", {}).get("full_name", "").lower() != repo.lower()
            or not re.fullmatch("[0-9a-f]{40}", run.get("head_sha", ""))
            or not isinstance(linked, list)
            or any(not isinstance(p, dict) or type(p.get("number")) is not int for p in linked)
            or (linked and int(number) not in [p["number"] for p in linked])
            or proof.get("endpoint")
            != f"repos/{repo}/actions/workflows/agents-verifier.yml/runs?per_page=100&status={run['status']}"
            or type(proof.get("observed_at")) not in {int, float}
            or not math.isfinite(proof["observed_at"])
            or type(operation.get("invoked_at")) not in {int, float}
            or not operation["invoked_at"]
            <= proof["observed_at"]
            <= operation.get("completed_at", 0)
        ):
            return None
        inventory_read = proof.get("inventory_read")
        if "inventory_read" in proof:
            if not isinstance(inventory_read, dict):
                return None
            # Legacy single-page receipts remain valid. New receipts identify
            # the actual page that observed the blocker, including its retained
            # content hash, rather than misattributing it to page one.
            page_endpoint = inventory_read.get("endpoint", "")
            base_endpoint = proof["endpoint"]
            if (
                inventory_read.get("status") != "observed"
                or not re.fullmatch("[0-9a-f]{64}", inventory_read.get("sha256", ""))
                or not (
                    page_endpoint == base_endpoint
                    or re.fullmatch(re.escape(base_endpoint) + r"&page=[1-9][0-9]*", page_endpoint)
                )
            ):
                return None
        return proof
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def verifier_resource_wait_event(proof):
    import closer_verifier_boundary

    if proof.get("schema") == closer_verifier_boundary.SCHEMA:
        return closer_verifier_boundary.event(proof)
    run = proof["blocking_run"]
    repo = proof["subject_pr"].split("#")[0]
    return f"github-verifier-resource:{repo}:{run['id']}:attempt:{run['run_attempt']}:terminal"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_copy(value: Any, *, limit: int = MAX_CALLBACK_JSON_BYTES) -> Any:
    """Validate that a callback value is bounded JSON data and detach it."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("callback JSON exceeds receipt size bound")
    return json.loads(encoded)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _assignment_list(targets: Iterable[str]) -> list[str]:
    result = list(targets)
    if any(not isinstance(target, str) or not target.strip() for target in result):
        raise ValueError("assignment targets must be non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError("assignment targets must be unique")
    return result


def _repo_key(target: str) -> str:
    """Map issue/PR aliases to the same repository mutation lane."""
    repo = target.split("#", 1)[0].strip().lower()
    if not repo:
        return target.strip().lower()
    return repo


def _observation(value: Any, target: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("observe must return an object")
    action = value.get("action")
    if action not in ACTION_NAMES:
        raise ValueError(f"unsupported closer action: {action!r}")
    eligible = value.get("eligible")
    if not isinstance(eligible, bool):
        raise ValueError("observation eligible must be a boolean")
    requires_coordinator = value.get("requires_coordinator", False)
    if not isinstance(requires_coordinator, bool):
        raise ValueError("observation requires_coordinator must be a boolean")
    revision = value.get("revision")
    # A missing revision is accounted for, but cannot authorize execution.
    if revision is not None:
        revision = _json_copy(revision, limit=4096)
    evidence = _json_copy(value.get("evidence", {}))
    if not isinstance(evidence, (dict, list, str, int, float, bool, type(None))):
        raise ValueError("evidence must be JSON data")
    reason = value.get("reason")
    next_trigger = value.get("next_trigger")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("observation reason must be a string or null")
    if next_trigger is not None and not isinstance(next_trigger, str):
        raise ValueError("next_trigger must be a string or null")
    aliases = value.get("claim_targets", [target])
    if not isinstance(aliases, (list, tuple)) or any(
        not isinstance(alias, str) or not alias.strip() for alias in aliases
    ):
        raise ValueError("claim_targets must be a list of non-empty strings")
    claim_targets = list(dict.fromkeys([target, *aliases]))
    explicit_binding = value.get("operation_binding")
    if explicit_binding is None:
        source = evidence if isinstance(evidence, Mapping) else {}
        explicit_binding = {
            key: source[key] for key in ("head", "merge", "review_revision") if key in source
        }
        explicit_binding["pr_target"] = target
        explicit_binding["source_targets"] = sorted(
            row.get("target")
            for row in source.get("sources", [])
            if isinstance(row, Mapping) and isinstance(row.get("target"), str)
        )
    if not isinstance(explicit_binding, Mapping):
        raise ValueError("operation_binding must be an object")
    operation_binding = _json_copy(explicit_binding, limit=8192)
    if action in MUTATING_ACTIONS:
        source_targets = operation_binding.get("source_targets", [])
        if not isinstance(source_targets, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in source_targets
        ):
            raise ValueError("source_targets must be a list of non-empty strings")
        claim_targets = list(dict.fromkeys([*claim_targets, *source_targets]))
    repo_keys = sorted({_repo_key(alias) for alias in claim_targets})
    if action in MUTATING_ACTIONS:
        claim_targets = sorted({*claim_targets, *(f"closer-repo:{repo}" for repo in repo_keys)})
    return {
        "target": target,
        "revision": revision,
        "action": action,
        "eligible": eligible,
        "requires_coordinator": requires_coordinator,
        "reason": reason,
        "next_trigger": next_trigger,
        "evidence": evidence,
        "operation_binding": operation_binding,
        "claim_targets": claim_targets,
        "repo_keys": repo_keys,
    }


def _observe_or_error(observe: Callable[[str], Mapping[str, Any]], target: str) -> dict[str, Any]:
    return _observation(observe(target), target)


def _execution_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Restore adapter-specific source fields while retaining scheduler metadata."""
    evidence = snapshot.get("evidence")
    payload = dict(evidence) if isinstance(evidence, Mapping) else {}
    payload.update({key: value for key, value in snapshot.items() if key != "evidence"})
    return payload


def _new_state() -> dict[str, Any]:
    return {"schema": SCHEMA, "items": {}, "round": {}}


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueStateError(f"unable to read closer queue: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise QueueStateError("unsupported or malformed closer queue schema")
    if not isinstance(data.get("items"), dict) or not isinstance(data.get("round"), dict):
        raise QueueStateError("closer queue items and round must be objects")
    if "frontiers" in data and not isinstance(data["frontiers"], dict):
        raise QueueStateError("closer queue finite frontier namespace must be an object")
    return data


def state_for_frontier(
    state: Mapping[str, Any],
    frontier_id: str,
    *,
    expected_policy: Mapping[str, Any] | None = None,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    """Return the selected finite queue namespace as a validation view.

    This does not mutate the durable document. Missing IDs can be represented as
    an empty selection only for callers that explicitly request ``allow_missing``;
    malformed or mismatched existing buckets always fail closed.
    """
    if not isinstance(frontier_id, str) or not frontier_id.strip():
        raise QueueStateError("finite frontier id is invalid")
    frontiers = state.get("frontiers", {})
    if not isinstance(frontiers, Mapping):
        raise QueueStateError("closer queue finite frontier namespace must be an object")
    if frontier_id not in frontiers:
        if allow_missing:
            return None
        raise QueueStateError("finite frontier is missing")
    bucket = frontiers[frontier_id]
    if not isinstance(bucket, Mapping):
        raise QueueStateError("finite frontier bucket is corrupt")
    policy = bucket.get("policy")
    items = bucket.get("items")
    round_state = bucket.get("round")
    if not isinstance(policy, Mapping) or policy.get("frontier_id") != frontier_id:
        raise QueueStateError("finite frontier binding or state namespace is corrupt")
    if expected_policy is not None and dict(policy) != dict(expected_policy):
        raise QueueStateError("finite frontier id is already bound to another policy")
    if not isinstance(items, Mapping) or not isinstance(round_state, Mapping):
        raise QueueStateError("finite frontier binding or state namespace is corrupt")
    round_policy = round_state.get("finite_frontier")
    if round_state and round_policy != policy:
        raise QueueStateError("finite frontier round policy differs from its bucket")
    return {**state, "items": items, "round": round_state}


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                payload, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def _ledger_lock(path: Path, *, cutoff: float, now: Callable[[], float]):
    """Acquire the process-wide ledger lock without waiting beyond the reserve."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    key = (os.getpid(), str(lock_path.resolve()))
    held = getattr(_ledger_threads, "held", None)
    if held is None:
        held = _ledger_threads.held = set()
    if key in held:
        # Recovery revalidates a stop while committing under this same queue
        # transaction. Other threads/processes still have to acquire flock.
        yield True
        return
    deadline_mono = time.monotonic() + max(0.0, cutoff - float(now()))
    with lock_path.open("a+") as stream:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline_mono - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.025, remaining))
        try:
            held.add(key)
            yield True
        finally:
            held.remove(key)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _set_observation(
    item: dict[str, Any],
    snapshot: Mapping[str, Any],
    *,
    owner: str,
    source_revisions: dict[str, Any],
) -> bool:
    old_revision = item.get("revision")
    new_revision = snapshot.get("revision")
    changed = old_revision != new_revision
    if changed:
        item["evidence"] = {}
    item.update(
        {
            "revision": new_revision,
            "owner": owner,
            "action": snapshot["action"],
            "eligible": snapshot["eligible"],
            "requires_coordinator": snapshot["requires_coordinator"],
            "reason": snapshot["reason"],
            "next_trigger": snapshot["next_trigger"],
            "evidence": snapshot["evidence"],
            "claim_targets": snapshot["claim_targets"],
            "repo_keys": snapshot["repo_keys"],
            "observation_freshness": "fresh",
        }
    )
    source_revisions[snapshot["target"]] = new_revision
    return changed


def _event(round_state: dict[str, Any], target: str, kind: str, **details: Any) -> None:
    events = round_state.setdefault("events", [])
    event = {"at": round_state.get("last_event_at"), "target": target, "kind": kind}
    event.update(details)
    events.append(event)
    # The round is bounded, and this protects state size across noisy callbacks.
    if len(events) > 256:
        del events[:-256]


def _remaining(deadline: float, reserve: float, now: Callable[[], float]) -> float:
    return deadline - float(now()) - reserve


def _budget_available(
    deadline: float, reserve: float, minimum: float, now: Callable[[], float]
) -> bool:
    available = _remaining(deadline, reserve, now)
    return available > 0 and available >= minimum


@contextlib.contextmanager
def _callback_invocation(target, operation, now):
    """Private live callback identity; never serialized or restored on restart."""
    if getattr(_callback_threads, "invocation", None) is not None:
        raise QueueStateError("nested queue callback invocation refused")
    _callback_threads.invocation = {
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "target": target,
        "operation": _json_copy(operation),
        "now": now,
    }
    try:
        yield
    finally:
        del _callback_threads.invocation


def _currently_invoking(target, operation):
    """Recognize only this thread's exact live original submitted operation."""
    current = getattr(_callback_threads, "invocation", None)
    if current is None:
        return False
    try:
        return (
            current["pid"] == os.getpid()
            and current["thread"] == threading.get_ident()
            and current["target"] == target
            and operation.get("status") == "submitted"
            and _canonical_bytes(operation) == _canonical_bytes(current["operation"])
            and float(current["now"]()) < float(current["operation"]["deadline"])
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _record_verification_prewrite_no_start(target, operation, result):
    """Retain producer provenance only in this exact callback's private context."""
    if _currently_invoking(target, operation):
        _callback_threads.invocation["verification_prewrite_no_start"] = _json_copy(result)


def verification_callback_not_invoked_proof(operation, target):
    """Validate a queue-owned cutoff receipt; a callback result cannot mint it."""
    proof = (operation.get("result") or {}).get("verification_callback_not_invoked")
    deadline = operation.get("deadline")
    if (
        not isinstance(proof, Mapping)
        or "invoked_at" in operation
        or operation.get("action") not in {"initiate_verify", "post_merge_verify"}
        or not isinstance(operation.get("id"), str)
        or not operation["id"]
        or not isinstance(operation.get("revision"), str)
        or not operation["revision"]
        or not isinstance(operation.get("binding"), Mapping)
        or type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or type(proof.get("observed_at")) not in {int, float}
        or not math.isfinite(proof["observed_at"])
        or proof["observed_at"] < deadline
    ):
        return None
    expected = {
        "schema": "closer-verification-callback-not-invoked/v1",
        "target": target,
        "operation_id": operation["id"],
        "action": operation["action"],
        "revision": operation["revision"],
        "binding_sha256": _digest(operation["binding"]),
        "deadline": deadline,
        "observed_at": proof["observed_at"],
        "callback_invoked": False,
    }
    return dict(proof) if dict(proof) == expected else None


def _invoke_before_cutoff(
    execute, target, action, snapshot, operation, now, execution_context=None
):
    """A queued callback rechecks its original request and held resource context."""
    if getattr(_callback_threads, "invocation", None) is not None:
        raise QueueStateError("nested queue callback invocation refused")

    def invoke():
        result = execute(target, action, snapshot, operation)
        if isinstance(result, Mapping):
            proof = result.get("verification_no_start")
            untrusted_precore = False
            if isinstance(proof, Mapping) and proof.get("phase") == "before_core_verify":
                current = getattr(_callback_threads, "invocation", {})
                untrusted_precore = current.get("verification_prewrite_no_start") != dict(result)
            if untrusted_precore or "verification_callback_not_invoked" in result:
                result = dict(result)
                if untrusted_precore:
                    result.pop("verification_no_start", None)
                # This receipt is emitted only below by the actual no-invoke
                # queue branch, never by a callback (including JSON output).
                result.pop("verification_callback_not_invoked", None)
        return result

    invoked_at = float(now())
    if invoked_at >= float(operation["deadline"]):
        return None, None
    if operation.get("finite_frontier") is not None:
        import closer_resources

        with closer_resources.finite_execution_context(
            execution_context,
            target=target,
            frontier=operation["finite_frontier"],
            deadline=operation["deadline"],
            now=now,
        ):
            remaining = closer_resources.finite_request_remaining()
            if remaining is None or remaining <= 0:
                return None, None
            with _callback_invocation(target, operation, now):
                return invoke(), invoked_at
    with _callback_invocation(target, operation, now):
        return invoke(), invoked_at


def _operation_id(owner: str, target: str, action: str, revision: Any) -> str:
    return hashlib.sha256(
        _canonical_bytes([owner, target, action, revision, uuid.uuid4().hex])
    ).hexdigest()[:32]


def _active_operations(items: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    running: dict[str, dict[str, Any]] = {}
    repos: set[str] = set()
    for target, item in items.items():
        operation = item.get("operation") if isinstance(item, dict) else None
        if isinstance(operation, dict) and operation.get("status") in PENDING_OPERATION_STATES:
            running[target] = operation
            if operation.get("action") in MUTATING_ACTIONS:
                repos.update(operation.get("repo_keys", []))
    return running, repos


def _account_row(target: str, item: Mapping[str, Any]) -> dict[str, Any]:
    raw_operation = item.get("operation")
    operation = raw_operation if isinstance(raw_operation, dict) else {}
    return {
        "target": target,
        "revision": item.get("revision"),
        "action": item.get("action"),
        "eligible": bool(item.get("eligible")),
        "requires_coordinator": bool(item.get("requires_coordinator")),
        "status": item.get("status", "observed"),
        "reason": item.get("deferred_reason") or item.get("reason"),
        "next_trigger": item.get("deferred_next_trigger") or item.get("next_trigger"),
        "operation_id": operation.get("id"),
        "operation_status": operation.get("status"),
        "observation_freshness": item.get("observation_freshness", "fresh"),
    }


def _validate_data(
    state: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    assignment_targets: list[str] | None,
    now_value: float,
    holder_fn: Callable[[str], Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    round_state = state.get("round", {})
    if not isinstance(round_state, Mapping) or not isinstance(receipt, Mapping):
        return {"valid": False, "stop_valid": False, "reasons": ["missing_round_or_receipt"]}
    targets = round_state.get("assignment_targets")
    if not isinstance(targets, list) or any(not isinstance(target, str) for target in targets):
        return {"valid": False, "stop_valid": False, "reasons": ["invalid_assignment"]}
    expected_hash = _digest(targets)
    if (
        round_state.get("assignment_hash") != expected_hash
        or receipt.get("assignment_hash") != expected_hash
    ):
        reasons.append("assignment_hash_mismatch")
    if assignment_targets is not None:
        try:
            supplied = _assignment_list(assignment_targets)
        except ValueError:
            supplied = []
            reasons.append("invalid_supplied_assignment")
        if supplied != targets:
            reasons.append("assignment_changed")
    source_revisions = round_state.get("source_revisions")
    if not isinstance(source_revisions, dict):
        reasons.append("missing_source_revisions")
        source_revisions = {}
    expected_source_hash = _digest({target: source_revisions.get(target) for target in targets})
    if (
        round_state.get("source_hash") != expected_source_hash
        or receipt.get("source_hash") != expected_source_hash
    ):
        reasons.append("source_hash_mismatch")
    accounted = receipt.get("accounted_targets")
    if (
        not isinstance(accounted, list)
        or len(accounted) != len(targets)
        or set(accounted) != set(targets)
    ):
        reasons.append("assignment_not_fully_accounted")
    deadline = round_state.get("deadline")
    reserve = round_state.get("receipt_reserve_seconds")
    finished_at = receipt.get("finished_at")
    frontier = round_state.get("finite_frontier")
    finite_frontier = frontier is not None
    if finite_frontier:
        try:
            from closer_frontier_policy import validate_frontier

            frontier_target = round_state.get("frontier_target")
            frontier = validate_frontier(frontier)
            if frontier_target is None:
                if targets != frontier["targets"]:
                    raise ValueError("finite frontier assignment differs")
            elif targets != [frontier_target] or frontier_target not in frontier["targets"]:
                raise ValueError("finite frontier target differs")
        except (TypeError, ValueError):
            reasons.append("invalid_finite_frontier")
        if deadline is not None or reserve != 0:
            reasons.append("finite_frontier_has_aggregate_deadline")
    elif not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        reasons.append("invalid_deadline")
        deadline = now_value
    if not isinstance(reserve, (int, float)) or isinstance(reserve, bool) or reserve < 0:
        reasons.append("invalid_receipt_reserve")
        reserve = 0.0
    if finite_frontier:
        legacy_deadline = None
    else:
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            legacy_deadline = float(now_value)
        else:
            legacy_deadline = float(deadline)
    legacy_reserve = float(reserve)
    if not isinstance(finished_at, (int, float)) or isinstance(finished_at, bool):
        reasons.append("missing_finish_time")
        finished_at = now_value
    if finite_frontier:
        overrun_seconds = None
        available_at_finish = None
    else:
        assert legacy_deadline is not None
        overrun_seconds = max(0.0, finished_at - legacy_deadline)
        if overrun_seconds:
            warnings.append("deadline_overrun")
        assert legacy_deadline is not None
        available_at_finish = legacy_deadline - finished_at - legacy_reserve
    items = state.get("items", {})
    # Expired recovery may finalize a truthful resource stop, but it may never
    # retroactively authorize an intent or callback launched after its cutoff.
    if finite_frontier:
        cutoff = None
    else:
        assert legacy_deadline is not None
        cutoff = legacy_deadline - legacy_reserve
    for target, item in items.items():
        if not isinstance(item, Mapping):
            continue
        operations = [item.get("operation"), *item.get("operation_history", [])]
        for operation in operations:
            if not isinstance(operation, Mapping) or operation.get("owner") != round_state.get(
                "owner"
            ):
                continue
            for field in ("submitted_at", "invoked_at"):
                at = operation.get(field)
                if finite_frontier:
                    request_deadline = operation.get("deadline")
                    if (
                        isinstance(request_deadline, bool)
                        or not isinstance(request_deadline, (int, float))
                        or not math.isfinite(request_deadline)
                        or not isinstance(at, (int, float))
                        or at >= request_deadline
                    ):
                        reasons.append(f"invalid_request_window:{target}:{field}")
                elif isinstance(at, (int, float)) and cutoff is not None and at >= cutoff:
                    reasons.append(f"operation_after_cutoff:{target}:{field}")
    rows = receipt.get("accounting")
    if not isinstance(rows, list):
        reasons.append("missing_accounting_rows")
        rows = []
    by_target = {row.get("target"): row for row in rows if isinstance(row, dict)}
    continuation_required = False
    integration_required: list[str] = []
    for target in targets:
        row = by_target.get(target)
        if row is None:
            reasons.append(f"unaccounted:{target}")
            continue
        item = items.get(target, {}) if isinstance(items, Mapping) else {}
        if not isinstance(item, Mapping) or row != _account_row(target, item):
            reasons.append(f"accounting_state_mismatch:{target}")
        if row.get("requires_coordinator"):
            integration_required.append(target)
            continuation_required = True
            if available_at_finish is not None and available_at_finish > 0:
                reasons.append(f"coordinator_integration_required:{target}")
            elif not finite_frontier and row.get("reason") != "receipt_reserve":
                reasons.append(f"integration_deferral_without_budget_exhaustion:{target}")
            if not row.get("next_trigger"):
                reasons.append(f"missing_integration_trigger:{target}")
        if not row.get("eligible"):
            if row.get("status") not in {"complete", "deferred", "ineligible"}:
                reasons.append(f"invalid_accounting_status:{target}")
            if row.get("status") != "complete" and not row.get("next_trigger"):
                reasons.append(f"missing_next_trigger:{target}")
            continue
        if row.get("status") in {"complete", "completed"}:
            continue
        continuation_required = True
        reason = row.get("reason")
        trigger = row.get("next_trigger")
        if not trigger:
            reasons.append(f"missing_next_trigger:{target}")
            continue
        if reason not in RESOURCE_DEFERRALS:
            reasons.append(f"eligible_without_resource_reason:{target}")
            continue
        # A scheduler cannot declare a made-up capacity or holder conflict.
        if reason == "verifier_resource_wait":
            item = items.get(target, {})
            operation = item.get("operation") if isinstance(item, dict) else None
            proof = verifier_resource_wait_proof(operation, target)
            if (
                proof is None
                or not isinstance(operation, dict)
                or row.get("operation_id") != operation.get("id")
                or row.get("revision") != proof.get("revision")
                or trigger != verifier_resource_wait_event(proof)
            ):
                reasons.append(f"unverified_verifier_resource_wait:{target}")
        elif reason == "merge_guard_refused":
            operation = item.get("operation") if isinstance(item, dict) else None
            if (
                merge_guard_refusal_proof(operation, target) is None
                or trigger != "refresh_merge_guard_before_action"
            ):
                reasons.append(f"unverified_merge_guard_refusal:{target}")
        elif reason == "worker_capacity":
            max_workers = round_state.get("max_workers", 0)
            active, _ = _active_operations(items)
            if not isinstance(max_workers, int) or max_workers <= 0 or len(active) < max_workers:
                reasons.append(f"unverified_worker_capacity:{target}")
        elif reason == "operation_running":
            item = items.get(target, {}) if isinstance(items, dict) else {}
            operation = item.get("operation") if isinstance(item, dict) else None
            if (
                not isinstance(operation, dict)
                or operation.get("status") not in PENDING_OPERATION_STATES
            ):
                reasons.append(f"unverified_running_operation:{target}")
        elif reason == "repo_operation_running":
            active, active_repos = _active_operations(items)
            item = items.get(target, {}) if isinstance(items, dict) else {}
            repos = set(item.get("repo_keys", [])) if isinstance(item, dict) else set()
            if not active or not active_repos.intersection(repos):
                reasons.append(f"unverified_repo_operation:{target}")
        elif reason == "claim_held":
            item = items.get(target, {}) if isinstance(items, dict) else {}
            aliases = item.get("claim_targets", [target]) if isinstance(item, dict) else [target]
            holders = []
            for alias in aliases:
                try:
                    holder = holder_fn(alias)
                except Exception:
                    holder = {"agent": "unreadable"}
                if holder:
                    holders.append(holder)
            if not holders:
                reasons.append(f"unverified_claim_conflict:{target}")
        elif reason == "receipt_reserve":
            if finite_frontier or (available_at_finish is not None and available_at_finish > 0):
                reasons.append(f"premature_deadline_deferral:{target}")
        elif reason == "pass_limit":
            if not receipt.get("continuation_required"):
                reasons.append(f"missing_continuation:{target}")
            if round_state.get("passes", 0) < round_state.get("max_passes", 1):
                reasons.append(f"premature_pass_limit:{target}")
            if available_at_finish is not None and available_at_finish > 0:
                reasons.append(f"pass_limit_requires_continuation:{target}")
        elif reason == "finite_frontier_identity_mismatch":
            mismatch = item.get("frontier_identity_mismatch") if isinstance(item, dict) else None
            frozen = round_state.get("finite_frontier")
            expected = frozen.get("bindings", {}).get(target) if isinstance(frozen, dict) else None
            if (
                not finite_frontier
                or not isinstance(mismatch, dict)
                or not isinstance(expected, dict)
                or mismatch.get("expected") != expected
                or (
                    mismatch.get("observed_revision") == expected.get("revision")
                    and mismatch.get("observed_action_id") == expected.get("action_id")
                )
                or item.get("operation") is not None
            ):
                reasons.append(f"unverified_frontier_identity_mismatch:{target}")
        elif reason == "observation_failed":
            invalid = round_state.get("invalid_observations")
            if (
                not finite_frontier
                or not isinstance(invalid, dict)
                or target not in invalid
                or item.get("operation") is not None
                or row.get("next_trigger") != "retry_observation_next_round"
            ):
                reasons.append(f"unverified_observation_failure:{target}")
    if continuation_required != bool(receipt.get("continuation_required")):
        reasons.append("continuation_flag_mismatch")
    if receipt.get("integration_required") != integration_required:
        reasons.append("integration_required_mismatch")
    if available_at_finish is not None and available_at_finish > 0:
        # An explicit continuation preserves the queue, but cannot justify early
        # finalization without a verified actual resource reason above.
        for target in targets:
            row = by_target.get(target, {})
            if row.get("eligible") and row.get("status") not in {"complete", "completed"}:
                if row.get("reason") not in RESOURCE_DEFERRALS:
                    reasons.append(f"work_left_with_time:{target}")
    return {
        "valid": not reasons,
        "stop_valid": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "deadline_overrun_seconds": overrun_seconds,
        "continuation_required": continuation_required,
        "integration_required": integration_required,
    }


def validate_stop(
    state_path: str | Path,
    assignment_targets: Iterable[str] | None = None,
    *,
    now: Callable[[], float] = time.time,
    holder_fn: Callable[[str], Any] | None = None,
    receipt: Mapping[str, Any] | None = None,
    frontier_id: str | None = None,
) -> dict[str, Any]:
    """Validate the latest durable receipt against its assignments and source hash."""
    path = Path(state_path)
    holder = holder_fn or claims.holder
    with _ledger_lock(path, cutoff=float(now()) + 1.0, now=now) as locked:
        if not locked:
            return {"valid": False, "stop_valid": False, "reasons": ["ledger_lock_busy"]}
        state = _read_state(path)
        if frontier_id is not None:
            try:
                selected_state = state_for_frontier(state, frontier_id)
            except QueueStateError as exc:
                return {
                    "valid": False,
                    "stop_valid": False,
                    "reasons": [f"invalid_frontier:{exc}"],
                }
            assert selected_state is not None
            state = selected_state
        round_state = state.get("round", {})
        stored = round_state.get("receipt") if isinstance(round_state, dict) else None
        selected = receipt if receipt is not None else stored
        if not isinstance(selected, Mapping):
            return {"valid": False, "stop_valid": False, "reasons": ["missing_receipt"]}
        if frontier_id is not None and (
            not isinstance(stored, Mapping) or dict(selected) != dict(stored)
        ):
            return {
                "valid": False,
                "stop_valid": False,
                "reasons": ["finite_frontier_receipt_mismatch"],
            }
        supplied = _assignment_list(assignment_targets) if assignment_targets is not None else None
        return _validate_data(
            state,
            selected,
            assignment_targets=supplied,
            now_value=float(now()),
            holder_fn=holder,
        )


def completed_progress_events(receipt, target, operation):
    """Exact submitted/advanced causal events shared by finite progress and waits."""
    events = receipt.get("events")
    if not isinstance(events, list):
        return False
    return any(
        isinstance(event, Mapping)
        and event.get("kind") == "operation_submitted"
        and event.get("target") == target
        and event.get("operation_id") == operation.get("id")
        and event.get("action") == operation.get("action")
        for event in events
    ) and any(
        isinstance(event, Mapping)
        and event.get("kind") == "action_advanced"
        and event.get("target") == target
        and event.get("action") == operation.get("action")
        and event.get("operation_id", operation.get("id")) == operation.get("id")
        and isinstance(event.get("revision"), str)
        and bool(event["revision"])
        and event["revision"] != operation.get("revision")
        for event in events
    )


def _accepted_request_events(bucket, target, operation):
    """Use the POST's own receipt, including when today's wait is another frontier.

    A static acknowledgment needs no changed remote revision. Any progress the
    original round actually claims must nevertheless have its causal event.
    """
    origin_round = bucket.get("round") or {}
    receipt = origin_round.get("receipt") or {}
    events = receipt.get("events")
    progress = receipt.get("progress")
    if (
        not isinstance(events, list)
        or not isinstance(progress, list)
        or events != origin_round.get("events")
        or progress != origin_round.get("progress")
    ):
        return False
    rows = [row for row in receipt.get("accounting", []) if row.get("target") == target]
    original_item = (bucket.get("items") or {}).get(target) or {}
    if (
        origin_round.get("finite_frontier") != bucket.get("policy")
        or receipt.get("schema") != RECEIPT_SCHEMA
        or len(rows) != 1
        or rows[0] != _account_row(target, original_item)
        or rows[0].get("operation_id") != operation.get("id")
    ):
        return False
    row = rows[0]
    if not any(
        event.get("kind") == "operation_submitted"
        and event.get("target") == target
        and event.get("operation_id") == operation.get("id")
        and event.get("action") == operation.get("action")
        for event in events
    ):
        return False
    advances = [
        event
        for event in events
        if event.get("kind") == "action_advanced" and event.get("target") == target
    ]
    claimed = (
        row.get("action") != operation.get("action")
        or row.get("revision") != operation.get("revision")
        or any(entry.get("target") == target for entry in progress)
        or bool(advances)
    )
    return (
        not claimed
        or bool(advances)
        and all(
            event.get("action") == operation.get("action")
            and event.get("operation_id", operation.get("id")) == operation.get("id")
            and isinstance(event.get("revision"), str)
            and bool(event["revision"])
            for event in advances
        )
    )


def accepted_verifier_wait_proof(state_path, target, *, frontier, receipt, evidence):
    """Authenticate a completed request awaiting its result, never a no-effect retry.

    The callback's event label is insufficient. Re-read the exact durable queue
    receipt and its completed, frontier-bound POST before accepting this wait.
    """
    import re

    from closer_frontier_policy import action_identity, validate_frontier

    try:
        frontier = validate_frontier(frontier)
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("target") != target
            or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("event_id", ""))
            or not isinstance(evidence.get("operation_id"), str)
            or not evidence["operation_id"]
            or any(
                not re.fullmatch(r"[0-9a-f]{40}", evidence.get(name, ""))
                for name in ("head", "merge")
            )
        ):
            return None
        path = Path(state_path)
        with _ledger_lock(path, cutoff=time.time() + 1.0, now=time.time) as locked:
            if not locked:
                return None
            document = _read_state(path)
            selected = state_for_frontier(
                document, frontier["frontier_id"], expected_policy=frontier
            )
            if selected["round"].get("receipt") != receipt:
                return None
            checked = _validate_data(
                selected,
                receipt,
                assignment_targets=[target],
                now_value=time.time(),
                holder_fn=claims.holder,
            )
            rows = receipt.get("accounting", [])
            matched = [
                row for row in rows if isinstance(row, Mapping) and row.get("target") == target
            ]
            if len(matched) != 1 or not (checked.get("valid") and checked.get("stop_valid")):
                return None
            row = matched[0]
            item = selected["items"][target]
            snapshot = item.get("evidence") or {}
            accepted_reasons = {
                "same verification request already accepted",
                "collect terminal or unattributed verifier evidence; do not rerun",
            }
            legacy_wait = (
                row.get("eligible") is False
                and row.get("action") in {"wait", "collect_evidence"}
                and row.get("reason") in accepted_reasons
                and row.get("status") in {"deferred", "ineligible"}
            )
            progress_wait = (
                row.get("eligible") is True
                and row.get("action") == "collect_evidence"
                and row.get("reason") == "pass_limit"
                and row.get("status") == "deferred"
                and row.get("operation_status") == "completed"
                and row.get("operation_id") == evidence["operation_id"]
                and item.get("eligible") is True
                and item.get("action") == row.get("action")
                and item.get("revision") == row.get("revision")
                and snapshot.get("revision") == row.get("revision")
                and item.get("reason") in accepted_reasons
            )
            if (
                not (legacy_wait or progress_wait)
                or item.get("provider_outcome_uncertain") not in (None, False)
                or item.get("recovery_barrier") is not None
                or any(
                    item.get(key) not in (None, False)
                    for key in ("outcome_uncertain", "no_effect", "advisory_only")
                )
                or item.get("observation_freshness") != "fresh"
                or evidence.get("observed_revision") != row.get("revision")
                or snapshot.get("target") != target
                or any(snapshot.get(name) != evidence[name] for name in ("head", "merge"))
            ):
                return None
            candidates = []
            for bucket in document.get("frontiers", {}).values():
                if not isinstance(bucket, Mapping):
                    continue
                items = bucket.get("items")
                item = items.get(target) if isinstance(items, Mapping) else None
                operation = item.get("operation") if isinstance(item, Mapping) else None
                if not isinstance(operation, Mapping):
                    continue
                if operation.get("id") == evidence["operation_id"]:
                    candidates.append((bucket, item, operation))
            if len(candidates) != 1:
                return None
            for bucket, original_item, operation in candidates:
                policy = validate_frontier(bucket.get("policy"))
                binding = operation.get("binding") or {}
                result = operation.get("result") or {}
                accepted = result.get("verification_post_accepted") or {}
                expected = policy["bindings"].get(target) or {}
                if (
                    operation.get("finite_frontier") == policy
                    and _accepted_request_events(bucket, target, operation)
                    and original_item.get("provider_outcome_uncertain") in (None, False)
                    and original_item.get("recovery_barrier") is None
                    and all(
                        original_item.get(key) in (None, False)
                        for key in ("outcome_uncertain", "no_effect", "advisory_only")
                    )
                    and (
                        not progress_wait
                        or (
                            policy == frontier
                            and (selected["items"][target].get("operation") or {}).get("id")
                            == operation.get("id")
                            and completed_progress_events(receipt, target, operation)
                        )
                    )
                    and operation.get("revision") == expected.get("revision")
                    and action_identity(
                        target, operation.get("revision"), operation.get("action"), binding
                    )
                    == expected.get("action_id")
                    and operation.get("status") == "completed"
                    and operation.get("action") == "post_merge_verify"
                    and binding.get("pr_target", target) == snapshot.get("pr_target", target)
                    and all(binding.get(name) == evidence[name] for name in ("head", "merge"))
                    and result.get("verification_request_basis") == evidence["event_id"]
                    and accepted.get("request_basis") == evidence["event_id"]
                    and accepted.get("mutation_kind") in {"label", "workflow_dispatch"}
                    and all(
                        record.get(key) is None or record.get(key) is False
                        for record in (operation, result)
                        for key in (
                            "provider_outcome_uncertain",
                            "outcome_uncertain",
                            "no_effect",
                            "advisory_only",
                        )
                    )
                ):
                    return {
                        "schema": "closer-accepted-verifier-wait/v1",
                        **dict(evidence),
                        "operation_frontier": policy["frontier_id"],
                        "receipt_operation_id": row.get("operation_id"),
                        "queue_receipt_sha256": _digest(receipt),
                    }
            return None
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return None


def _failure_observation(target: str, error: Exception) -> dict[str, Any]:
    return {
        "target": target,
        "revision": None,
        "action": "wait",
        "eligible": True,
        "reason": "observation_failed",
        "next_trigger": "retry_observation",
        "requires_coordinator": False,
        "operation_binding": {"pr_target": target, "source_targets": []},
        "evidence": {
            "error_type": type(error).__name__,
            "error": str(error)[:500],
            "unknown": True,
        },
        "claim_targets": [target],
        "repo_keys": [_repo_key(target)],
    }


def _cached_snapshot(target: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """Return durable last-known data for accounting without treating it as fresh."""
    action = item.get("action")
    if action not in ACTION_NAMES:
        action = "wait"
    aliases = item.get("claim_targets", [target])
    if not isinstance(aliases, list):
        aliases = [target]
    repos = item.get("repo_keys", [_repo_key(target)])
    if not isinstance(repos, list):
        repos = [_repo_key(target)]
    return {
        "target": target,
        "revision": item.get("revision"),
        "action": action,
        # Without a fresh read, an old terminal/ineligible assessment cannot
        # certify that no work remains.
        "eligible": True,
        "requires_coordinator": bool(item.get("requires_coordinator", False)),
        "reason": item.get("reason") or "last_known_observation_only",
        "next_trigger": item.get("next_trigger") or "retry_observation_next_round",
        "evidence": item.get("evidence", {}),
        "operation_binding": item.get("operation_binding", {}),
        "claim_targets": aliases,
        "repo_keys": repos,
    }


def _defer(item: dict[str, Any], reason: str, trigger: str) -> None:
    item["status"] = "deferred"
    item["deferred_reason"] = reason
    item["deferred_next_trigger"] = trigger


def _claim_all(
    targets: list[str],
    owner: str,
    claim_fn: Callable[[str, str], bool],
    release_fn: Callable[[str, str], bool],
    holder_fn: Callable[[str], Any],
) -> tuple[list[str], str | None, Any]:
    claimed: list[str] = []
    for target in targets:
        try:
            acquired = bool(claim_fn(target, owner))
        except Exception as exc:
            acquired = False
            failure = f"claim_error:{type(exc).__name__}"
        else:
            failure = "claim_held"
        if not acquired:
            try:
                holder = holder_fn(target)
            except Exception:
                holder = {"agent": "unreadable"}
            for prior in reversed(claimed):
                with contextlib.suppress(Exception):
                    release_fn(prior, owner)
            return [], failure, holder
        claimed.append(target)
    return claimed, None, None


def _release_all(
    targets: list[str], owner: str, release_fn: Callable[[str, str], bool]
) -> list[str]:
    failed: list[str] = []
    for target in reversed(targets):
        try:
            if not release_fn(target, owner):
                failed.append(target)
        except Exception:
            failed.append(target)
    return failed


def reconcile_only(
    state_path: str | Path,
    target: str,
    operation_id: str,
    revision: str,
    *,
    frontier_id: str,
    reconcile: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    request_timeout_seconds: float = 120.0,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Settle only one original effect fence, without selecting or submitting work.

    The reconciler may CAS-settle the original Core operation after exact remote
    readback. This is not wholly read-only, and never invokes ``execute`` or
    manufactures a completed callback/receipt for the outer pipeline.
    """
    from closer_frontier_policy import action_identity, request_window, validate_frontier

    if not all(
        isinstance(value, str) and value.strip() for value in (target, operation_id, revision)
    ):
        raise ValueError("exact original operation identity required")
    path = Path(state_path)
    if path.resolve() != path or path.is_symlink():
        raise QueueStateError("reconciliation queue path is aliased")
    window = request_window(now(), request_timeout_seconds)
    with _ledger_lock(path, cutoff=window["deadline"], now=now) as locked:
        if not locked:
            raise QueueStateError("original operation queue is busy")
        document = _read_state(path)
        selected = state_for_frontier(document, frontier_id)
        assert selected is not None
        item = selected["items"].get(target)
        operation = item.get("operation") if isinstance(item, dict) else None
        if not isinstance(operation, dict) or (
            operation.get("id") != operation_id or operation.get("revision") != revision
        ):
            raise QueueStateError("original operation identity changed")
        frontier = validate_frontier(document["frontiers"][frontier_id]["policy"])
        expected = frontier["bindings"].get(target) or {}
        action, binding = operation.get("action"), operation.get("binding")
        if not isinstance(action, str) or not isinstance(binding, Mapping):
            raise QueueStateError("original operation binding is invalid")
        if (
            operation.get("finite_frontier") != frontier
            or expected.get("revision") != revision
            or action_identity(target, revision, action, binding) != expected.get("action_id")
        ):
            raise QueueStateError("original operation binding changed")
        if operation.get("status") == "completed":
            return {"status": "completed", "operation_id": operation_id, "idempotent": True}
        if operation.get("status") not in PENDING_OPERATION_STATES:
            return {"status": operation.get("status", "unknown"), "operation_id": operation_id}
        bounded = {
            **operation,
            "effect_deadline": operation.get("deadline"),
            "deadline": window["deadline"],
            "timeout_seconds": request_timeout_seconds,
        }
        resolution = reconcile(target, bounded)
        if not isinstance(resolution, Mapping):
            resolution = {"status": "unknown"}
        status = resolution.get("status")
        # Preserve failed/unknown boundaries. A no-start readback is knowledge,
        # never permission for this consumer to resubmit the operation.
        if status in {"completed", "confirmed_not_started"}:
            operation["status"] = status
            operation["reconciled_at"] = float(now())
            operation["reconciliation"] = _json_copy(dict(resolution))
            if status == "completed":
                operation["completed_at"] = float(now())
                operation["result"] = _json_copy(resolution.get("result", {}))
            _atomic_write(path, document)
        return {
            "status": status if isinstance(status, str) else "unknown",
            "operation_id": operation_id,
        }


def run_round(
    state_path: str | Path,
    assignment_targets: Iterable[str],
    observe: Callable[[str], Mapping[str, Any]],
    execute: Callable[[str, str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    *,
    reconcile: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    owner: str | None = None,
    deadline: float | None,
    receipt_reserve_seconds: float = DEFAULT_RECEIPT_RESERVE_SECONDS,
    max_workers: int = 2,
    max_passes: int = 4,
    max_operation_retries: int = 1,
    min_action_seconds: float = DEFAULT_MIN_ACTION_SECONDS,
    now: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    claim_fn: Callable[[str, str], bool] | None = None,
    holder_fn: Callable[[str], Any] | None = None,
    release_fn: Callable[[str, str], bool] | None = None,
    finite_frontier: Mapping[str, Any] | None = None,
    frontier_target: str | None = None,
    finite_readmission: Mapping[str, Any] | None = None,
    request_timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run a fair, durable, deadline-bounded action round.

    ``observe`` must be a bounded read. ``execute`` and ``reconcile`` must honor
    the ``operation['deadline']`` and ``operation['timeout_seconds']`` fields.
    Every non-read-only action is recorded before callback invocation. A pending
    operation is reconciled before any retry; only ``confirmed_not_started`` can
    authorize a bounded retry of the same idempotency ID.
    """
    path = Path(state_path)
    targets = _assignment_list(assignment_targets)
    frontier = None
    if finite_frontier is not None:
        from closer_frontier_policy import request_window, validate_frontier

        frontier = validate_frontier(finite_frontier)
        if frontier_target is None:
            if targets != frontier["targets"]:
                raise ValueError("finite frontier assignment differs")
        elif targets != [frontier_target] or frontier_target not in frontier["targets"]:
            raise ValueError("finite frontier target differs")
        if deadline is not None or receipt_reserve_seconds != 0:
            raise ValueError("finite frontier requires no aggregate deadline or receipt reserve")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("finite frontier request timeout must be positive and finite")
        if max_operation_retries != 0:
            raise ValueError("finite frontier does not retry uncertain operations")
        finite_timeout = float(request_timeout_seconds)
        request_window(now(), finite_timeout)
    elif not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise ValueError("deadline must be an epoch timestamp")
    else:
        finite_timeout = None
    if frontier is None:
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            raise ValueError("deadline must be an epoch timestamp")
        legacy_deadline = float(deadline)
    else:
        legacy_deadline = None
    if receipt_reserve_seconds < 0 or min_action_seconds < 0:
        raise ValueError("time reserves must be non-negative")
    if max_workers < 1 or max_passes < 1 or max_operation_retries < 0:
        raise ValueError("worker, pass, and retry bounds must be positive")
    assignment_hash = _digest(targets)
    claim_call = claim_fn or claims.claim
    holder_call = holder_fn or claims.holder
    release_call = release_fn or claims.release
    if frontier is not None:
        assert finite_timeout is not None
        cutoff = float(now()) + finite_timeout
    else:
        assert legacy_deadline is not None
        cutoff = legacy_deadline - float(receipt_reserve_seconds)

    def budget_available() -> bool:
        if frontier is not None:
            return True
        assert legacy_deadline is not None
        return _budget_available(legacy_deadline, receipt_reserve_seconds, min_action_seconds, now)

    def request_deadline() -> float:
        if frontier is not None:
            from closer_frontier_policy import request_window

            assert finite_timeout is not None
            return request_window(now(), finite_timeout)["deadline"]
        assert legacy_deadline is not None
        return legacy_deadline - float(receipt_reserve_seconds)

    def frontier_identity_matches(target: str, snapshot: Mapping[str, Any]) -> bool:
        if frontier is None:
            return True
        from closer_frontier_policy import action_identity

        expected = frontier["bindings"].get(target)
        if not isinstance(expected, Mapping):
            return False
        revision = snapshot.get("revision")
        action = snapshot.get("action")
        actual = None
        if isinstance(revision, str) and isinstance(action, str):
            operation_binding = snapshot.get("operation_binding")
            actual = action_identity(
                target,
                revision,
                action,
                operation_binding if isinstance(operation_binding, Mapping) else {},
            )
        return revision == expected["revision"] and actual == expected["action_id"]

    with _ledger_lock(path, cutoff=cutoff, now=now) as locked:
        if not locked:
            accounting = [
                {
                    "target": target,
                    "revision": None,
                    "action": None,
                    "eligible": True,
                    "status": "deferred",
                    "reason": "ledger_lock_busy" if frontier is not None else "receipt_reserve",
                    "next_trigger": "retry_after_ledger_unlock",
                    "operation_id": None,
                    "operation_status": None,
                }
                for target in targets
            ]
            return {
                "schema": RECEIPT_SCHEMA,
                "assignment_hash": assignment_hash,
                "source_hash": None,
                "accounted_targets": targets,
                "accounting": accounting,
                "eligible_remaining": targets,
                "deferred": accounting,
                "progress": [],
                "events": [],
                "valid": False,
                "stop_valid": False,
                "continuation_required": bool(targets),
                "reasons": ["ledger_lock_busy"],
            }

        state = _read_state(path)
        frontier_bucket = None
        if frontier is not None:
            frontiers = state.setdefault("frontiers", {})
            if not isinstance(frontiers, dict):
                raise QueueStateError("finite frontier namespace is corrupt")
            frontier_id = frontier["frontier_id"]
            if frontier_id not in frontiers:
                frontier_bucket = {"policy": frontier, "items": {}, "round": {}}
                frontiers[frontier_id] = frontier_bucket
                items = frontier_bucket["items"]
                current_round = frontier_bucket["round"]
            else:
                selected_state = state_for_frontier(state, frontier_id, expected_policy=frontier)
                if selected_state is None:
                    raise QueueStateError("finite frontier is missing")
                frontier_bucket = frontiers[frontier_id]
                items = selected_state["items"]
                current_round = selected_state["round"]
            if not isinstance(items, dict) or not isinstance(current_round, dict):
                raise QueueStateError("finite frontier state namespace is corrupt")
            if set(items).difference(frontier["targets"]):
                raise QueueStateError("finite frontier ledger contains a foreign target")
        else:
            items = state["items"]
            current_round = state.get("round", {})
        timestamp = float(now())
        if (
            frontier is not None
            and current_round.get("status") == "finished"
            and finite_readmission is not None
        ):
            import closer_finite_reentry

            # This is not a caller flag: the original native ACT capability,
            # live scoped lease, applied audit and exact saved queue bytes must
            # all agree. UNKNOWN/attempted effects never obtain this audit.
            if (
                len(targets) == 1
                and _native_verify_wait_executor(execute)
                and closer_finite_reentry.guard_queue_readmission(
                    finite_readmission,
                    path,
                    frontier,
                    targets[0],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            ):
                assert frontier_bucket is not None  # validated finite namespace above
                retained_operation = items[targets[0]].get("operation")
                history = list(items[targets[0]].get("operation_history", []))
                # Keep the invoked failed/no-command operation unchanged. It is
                # history, not a confirmed-not-started intent for replay.
                # The exact complete before-image is already immutable in the
                # applied reentry audit; retain the normal bounded hot history.
                items[targets[0]]["operation_history"] = (history + [retained_operation])[
                    -MAX_OPERATION_HISTORY:
                ]
                items[targets[0]]["operation"] = None
                current_round = {}
                frontier_bucket["round"] = current_round
        if frontier is not None and current_round.get("status") == "finished":
            # A finite frontier is a single immutable selection, not a reusable
            # scheduler round. Return its retained terminal receipt (including
            # UNKNOWN/invalid receipts) without observing, reconciling, or
            # invoking callbacks again under the same action identities.
            if (
                current_round.get("finite_frontier") != frontier
                or current_round.get("frontier_target") != frontier_target
                or current_round.get("assignment_targets") != targets
                or (owner is not None and current_round.get("owner") != owner)
            ):
                raise QueueStateError("finished finite frontier identity differs")
            retained = current_round.get("receipt")
            if not isinstance(retained, Mapping):
                raise QueueStateError("finished finite frontier has no retained receipt")
            retained_accounting = retained.get("accounting")
            revisions = current_round.get("source_revisions")
            if (
                retained.get("schema") != RECEIPT_SCHEMA
                or retained.get("assignment_targets") != targets
                or retained.get("accounted_targets") != targets
                or retained.get("assignment_hash") != assignment_hash
                or not isinstance(retained_accounting, list)
                or retained_accounting
                != [_account_row(target, items[target]) for target in targets]
                or not isinstance(revisions, Mapping)
                or retained.get("source_hash")
                != _digest({target: revisions.get(target) for target in targets})
            ):
                raise QueueStateError("finished finite frontier receipt does not match its ledger")
            validation_state = {**state, "items": items, "round": current_round}
            validation = _validate_data(
                validation_state,
                retained,
                assignment_targets=targets,
                now_value=timestamp,
                holder_fn=holder_call,
            )
            return {**dict(retained), **validation}
        # A slice ending with an invalid stop is still the same bounded round.
        # Refreshing assignments must not purchase another wall-clock budget.
        resume = current_round.get("status") == "running" or (
            current_round.get("status") == "finished"
            and not current_round.get("receipt", {}).get("stop_valid", False)
        )
        if resume:
            round_state = current_round
            if round_state.get("finite_frontier") != frontier:
                raise QueueStateError("resume finite frontier policy differs from durable round")
            if round_state.get("frontier_target") != frontier_target:
                raise QueueStateError("resume finite frontier target differs")
            round_owner = str(round_state["owner"])
            if owner is not None and owner != round_owner:
                raise QueueStateError("resume owner differs from durable round owner")
            # A restarted process continues the original budget; it cannot extend
            # an unfinished round by supplying a later wall-clock deadline.
            if frontier is None:
                # Historical rounds must retain their original finite deadline.
                if round_state.get("deadline") is None:
                    raise QueueStateError("legacy round has no original deadline")
                deadline = float(round_state["deadline"])
                receipt_reserve_seconds = float(round_state["receipt_reserve_seconds"])
            elif round_state.get("deadline") is not None:
                raise QueueStateError("finite frontier cannot resume a deadline round")
            max_workers = int(round_state.get("max_workers", max_workers))
            max_passes = (
                len(targets)
                if frontier is not None
                else int(round_state.get("max_passes", max_passes))
            )
            if frontier is not None:
                round_state["max_passes"] = len(targets)
            max_operation_retries = int(
                round_state.get("max_operation_retries", max_operation_retries)
            )
            min_action_seconds = float(round_state.get("min_action_seconds", min_action_seconds))
            round_state["assignment_targets"] = targets
            round_state["assignment_hash"] = assignment_hash
            round_state["status"] = "running"
            round_state.pop("receipt", None)
        else:
            round_owner = owner or f"closer-round:{uuid.uuid4().hex}"
            if not isinstance(round_owner, str) or not round_owner.strip():
                raise ValueError("owner must be a non-empty string")
            round_state = {
                "started_at": timestamp,
                **({"deadline": legacy_deadline} if frontier is None else {}),
                "receipt_reserve_seconds": (
                    0.0 if frontier is not None else float(receipt_reserve_seconds)
                ),
                "finite_frontier": frontier,
                "frontier_target": frontier_target,
                "owner": round_owner,
                "assignment_targets": targets,
                "assignment_hash": assignment_hash,
                "source_revisions": {},
                "source_hash": None,
                "status": "running",
                "max_workers": max_workers,
                "max_passes": len(targets) if frontier is not None else max_passes,
                "max_operation_retries": max_operation_retries,
                "min_action_seconds": min_action_seconds,
                "passes": 0,
                "events": [],
                "progress": [],
            }
            if frontier_bucket is None:
                state["round"] = round_state
            else:
                frontier_bucket["round"] = round_state

        if frontier is None:
            if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
                raise QueueStateError("legacy round has no original deadline")
            legacy_deadline = float(deadline)

        def persist_state():
            if frontier_bucket is not None:
                frontier_bucket["items"] = items
                frontier_bucket["round"] = round_state
            _atomic_write(path, state)

        cutoff = request_deadline()
        budget_available_at_start = budget_available()

        # An unfinished operation from any assignment remains a duplicate-action
        # barrier. Reconcile all such records, not merely today's assignment set.
        for pending_target, pending_item in list(items.items()):
            operation = pending_item.get("operation") if isinstance(pending_item, dict) else None
            if (
                not isinstance(operation, dict)
                or operation.get("status") not in PENDING_OPERATION_STATES
            ):
                continue
            if not budget_available_at_start or not budget_available():
                pending_item["deferred_reason"] = "receipt_reserve"
                pending_item["deferred_next_trigger"] = "reconcile_next_round"
                continue
            if reconcile is None:
                resolution: Mapping[str, Any] = {
                    "status": "unknown",
                    "next_trigger": "reconcile_operation",
                }
            else:
                try:
                    bounded = dict(operation)
                    if frontier is not None:
                        # A fresh reconciliation read has its own request window;
                        # preserve the original effect deadline separately.
                        bounded["effect_deadline"] = operation.get("deadline")
                        assert finite_timeout is not None
                        bounded["deadline"] = float(now()) + finite_timeout
                        bounded["timeout_seconds"] = finite_timeout
                    else:
                        bounded["deadline"] = cutoff
                        assert legacy_deadline is not None
                        bounded["timeout_seconds"] = max(
                            0.0, _remaining(legacy_deadline, receipt_reserve_seconds, now)
                        )
                    resolution = reconcile(pending_target, bounded)
                except Exception as exc:
                    resolution = {"status": "unknown", "error_type": type(exc).__name__}
            if not isinstance(resolution, Mapping):
                resolution = {"status": "unknown"}
            resolution_status = resolution.get("status")
            if resolution_status == "completed":
                operation["status"] = "completed"
                operation["result"] = _json_copy(resolution.get("result", {}))
                operation["completed_at"] = float(now())
                pending_item["status"] = "observed"
                pending_item.pop("deferred_reason", None)
                pending_item.pop("deferred_next_trigger", None)
                _event(
                    round_state,
                    pending_target,
                    "reconciled_completed",
                    operation_id=operation.get("id"),
                )
            elif resolution_status == "confirmed_not_started":
                operation["status"] = "confirmed_not_started"
                operation["reconcile_next_trigger"] = resolution.get(
                    "next_trigger", "retry_same_operation"
                )
            elif resolution_status == "running":
                operation["status"] = "running"
                pending_item["status"] = "deferred"
                pending_item["deferred_reason"] = "operation_running"
                pending_item["deferred_next_trigger"] = resolution.get(
                    "next_trigger", "reconcile_operation"
                )
            elif resolution_status == "failed":
                operation["status"] = "failed"
                operation["result"] = _json_copy(resolution.get("result", {}))
                operation["next_trigger"] = resolution.get(
                    "next_trigger", "inspect_operation_failure"
                )
                pending_item["status"] = "deferred"
                pending_item["deferred_reason"] = "operation_failed"
                pending_item["deferred_next_trigger"] = operation["next_trigger"]
            else:
                operation["status"] = "uncertain"
                operation["next_trigger"] = resolution.get("next_trigger", "reconcile_operation")
                pending_item["status"] = "deferred"
                pending_item["deferred_reason"] = "operation_uncertain"
                pending_item["deferred_next_trigger"] = operation["next_trigger"]
            persist_state()

        # Observe every assigned target before queueing. Unexpected/invalid reads
        # become explicit blockers, never apparent idle targets.
        snapshots: dict[str, dict[str, Any]] = {}
        source_revisions: dict[str, Any] = round_state.setdefault("source_revisions", {})
        # Identity mismatches are terminal for this immutable frontier and
        # must be excluded from later progression in the same round.
        stalled: set[str] = set()
        for target in targets:
            item = items.setdefault(target, {"operation": None, "operation_history": []})
            if not budget_available_at_start or not budget_available():
                if "revision" in item or "evidence" in item:
                    snapshot = _cached_snapshot(target, item)
                    item.update(snapshot)
                    item["owner"] = round_owner
                    item["observation_freshness"] = "cached_budget_exhausted"
                    source_revisions[target] = item.get("revision")
                else:
                    snapshot = {
                        "target": target,
                        "revision": None,
                        "action": "wait",
                        "eligible": True,
                        "requires_coordinator": False,
                        "reason": "unobserved_budget_exhausted",
                        "next_trigger": "retry_observation_next_round",
                        "evidence": {},
                        "operation_binding": {},
                        "claim_targets": [target],
                        "repo_keys": [_repo_key(target)],
                    }
                    item.update(snapshot)
                    item["owner"] = round_owner
                    item["observation_freshness"] = "unobserved_budget_exhausted"
                    source_revisions[target] = None
                snapshots[target] = snapshot
                _defer(item, "receipt_reserve", "retry_observation_next_round")
                persist_state()
                continue
            try:
                snapshot = _observe_or_error(observe, target)
            except Exception as exc:
                snapshot = _failure_observation(target, exc)
                round_state.setdefault("invalid_observations", {})[target] = type(exc).__name__
            changed = _set_observation(
                item, snapshot, owner=round_owner, source_revisions=source_revisions
            )
            snapshots[target] = snapshot
            if frontier is not None:
                from closer_frontier_policy import action_identity

                expected = frontier["bindings"][target]
                actual = None
                if isinstance(snapshot.get("revision"), str):
                    actual = action_identity(
                        target,
                        snapshot["revision"],
                        snapshot["action"],
                        snapshot["operation_binding"],
                    )
                if target not in round_state.get("invalid_observations", {}) and (
                    snapshot.get("revision") != expected["revision"]
                    or actual != expected["action_id"]
                ):
                    _defer(item, "finite_frontier_identity_mismatch", "new_frontier_required")
                    item["frontier_identity_mismatch"] = {
                        "expected": dict(expected),
                        "observed_revision": snapshot.get("revision"),
                        "observed_action_id": actual,
                    }
                    stalled.add(target)
                    persist_state()
                    continue
            if changed:
                _event(round_state, target, "revision_refreshed", revision=snapshot["revision"])
            item["status"] = "observed"
            item.pop("deferred_reason", None)
            item.pop("deferred_next_trigger", None)
            persist_state()
        round_state["source_hash"] = _digest(
            {target: source_revisions.get(target) for target in targets}
        )

        progress_targets: list[str] = []
        active_ops, active_mutating_repos = _active_operations(items)
        # Reconcile-completed assigned operations and confirmed-not-started items
        # using the current observation; stale snapshots are never executed.
        for target in targets:
            item = items[target]
            operation = item.get("operation")
            if not isinstance(operation, dict):
                continue
            if operation.get("status") == "completed":
                pass
            elif operation.get("status") in {"running", "uncertain", "submitted"}:
                _defer(
                    item,
                    (
                        "operation_running"
                        if operation.get("status") == "running"
                        else "operation_uncertain"
                    ),
                    operation.get("next_trigger", "reconcile_operation"),
                )
                stalled.add(target)
            elif operation.get("status") == "resource_wait":
                proof = verifier_resource_wait_proof(operation, target)
                if proof is None:
                    operation["status"] = "uncertain"
                    _defer(item, "operation_uncertain", "reconcile_invalid_resource_wait")
                else:
                    _defer(item, "verifier_resource_wait", verifier_resource_wait_event(proof))
                stalled.add(target)
            elif operation.get("status") == "failed":
                _defer(
                    item,
                    "operation_failed",
                    operation.get("next_trigger", "inspect_operation_failure"),
                )
                stalled.add(target)
            elif operation.get("status") == "confirmed_not_started":
                if operation.get("attempts", 0) > max_operation_retries:
                    operation["status"] = "uncertain"
                    _defer(item, "operation_uncertain", "manual_operation_reconciliation")
                    stalled.add(target)
                elif (
                    operation.get("revision") != snapshots[target].get("revision")
                    or operation.get("action") != snapshots[target].get("action")
                    or operation.get("binding") != snapshots[target].get("operation_binding")
                ):
                    operation["status"] = "abandoned"
                    operation["next_trigger"] = "reobserve_changed_revision"
                    item["operation_history"] = (item.get("operation_history", []) + [operation])[
                        -MAX_OPERATION_HISTORY:
                    ]
                    item["operation"] = None
                    _event(round_state, target, "abandoned_stale_intent")
            else:
                item["operation"] = None
        active_ops, active_mutating_repos = _active_operations(items)
        stop_launch_reason: str | None = None
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="closer-action")
        pending_claims: dict[int, list[str]] = {}

        def release_acquired(acquired: list[str]) -> list[str]:
            # An ordinary release attempt already reports its own failures. Do
            # not silently retry that cleanup when unwinding a later exception.
            pending_claims.pop(id(acquired), None)
            return _release_all(acquired, round_owner, release_call)

        try:
            slice_start_pass = int(round_state.get("passes", 0))
            # A finite frontier's only queue-pass bound is its immutable cardinality:
            # each selected target can start at most one source-bound operation.
            # Legacy rounds retain their configured pass limit unchanged.
            slice_pass_limit = (
                len(targets) if frontier is not None else slice_start_pass + max_passes
            )
            for pass_number in range(slice_start_pass, slice_pass_limit):
                if not budget_available_at_start or not budget_available():
                    stop_launch_reason = "receipt_reserve"
                    break
                round_state["passes"] = pass_number + 1
                round_state["last_event_at"] = float(now())
                made_progress = False
                launched: list[
                    tuple[str, dict[str, Any], dict[str, Any], Future[Any], list[str], float]
                ] = []
                # Rotate start order each pass so repeated rounds don't privilege the
                # same first assignment.
                ordered = (
                    targets[pass_number % len(targets) :] + targets[: pass_number % len(targets)]
                    if targets
                    else []
                )
                for target in ordered:
                    if target in stalled:
                        continue
                    item = items[target]
                    snapshot = snapshots[target]
                    operation = item.get("operation")
                    if (
                        isinstance(operation, dict)
                        and operation.get("status") in PENDING_OPERATION_STATES
                    ):
                        _defer(
                            item,
                            (
                                "operation_running"
                                if operation.get("status") == "running"
                                else "operation_uncertain"
                            ),
                            operation.get("next_trigger", "reconcile_operation"),
                        )
                        stalled.add(target)
                        continue
                    if snapshot["action"] == "complete":
                        item["status"] = "complete"
                        item.pop("deferred_reason", None)
                        item.pop("deferred_next_trigger", None)
                        stalled.add(target)
                        continue
                    if not frontier_identity_matches(target, snapshot):
                        _defer(item, "finite_frontier_identity_mismatch", "new_frontier_required")
                        item["frontier_identity_mismatch"] = {
                            "expected": (
                                dict(frontier["bindings"][target]) if frontier is not None else {}
                            ),
                            "observed_revision": snapshot.get("revision"),
                            "observed_action": snapshot.get("action"),
                        }
                        stalled.add(target)
                        continue
                    if snapshot["action"] == "wait" or not snapshot["eligible"]:
                        why = snapshot["reason"] or "not_eligible"
                        trigger = snapshot["next_trigger"] or "wait_for_trigger"
                        _defer(item, why, trigger)
                        stalled.add(target)
                        continue
                    action = snapshot["action"]
                    if action not in ACTION_NAMES:
                        _defer(item, "invalid_action", "repair_observation")
                        stalled.add(target)
                        continue
                    if frontier is not None:
                        assert finite_timeout is not None
                        available = finite_timeout
                    else:
                        assert legacy_deadline is not None
                        available = _remaining(legacy_deadline, receipt_reserve_seconds, now)
                    if available <= 0 or available < min_action_seconds:
                        _defer(item, "receipt_reserve", "continue_next_round")
                        stop_launch_reason = "receipt_reserve"
                        continue
                    active_ops, active_mutating_repos = _active_operations(items)
                    if len(active_ops) >= max_workers:
                        _defer(item, "worker_capacity", "reconcile_running_operations")
                        continue
                    is_mutating = action not in READ_ONLY_ACTIONS and action not in TERMINAL_ACTIONS
                    repos = set(snapshot["repo_keys"])
                    if is_mutating and repos.intersection(active_mutating_repos):
                        _defer(item, "repo_operation_running", "reconcile_repo_operation")
                        continue

                    # Read immediately before the action. A moving revision invalidates
                    # prior evidence and this pass defers rather than acting on stale data.
                    try:
                        fresh = _observe_or_error(observe, target)
                    except Exception as exc:
                        fresh = _failure_observation(target, exc)
                        round_state.setdefault("invalid_observations", {})[target] = type(
                            exc
                        ).__name__
                    if fresh["revision"] != snapshot["revision"]:
                        _set_observation(
                            item, fresh, owner=round_owner, source_revisions=source_revisions
                        )
                        snapshots[target] = fresh
                        _defer(item, "revision_changed", "reobserve_next_pass")
                        _event(round_state, target, "stale_snapshot_rejected", action=action)
                        stalled.add(target)
                        continue
                    snapshot = fresh
                    snapshots[target] = fresh
                    _set_observation(
                        item, fresh, owner=round_owner, source_revisions=source_revisions
                    )
                    if not frontier_identity_matches(target, fresh):
                        _defer(item, "finite_frontier_identity_mismatch", "new_frontier_required")
                        item["frontier_identity_mismatch"] = {
                            "expected": (
                                dict(frontier["bindings"][target]) if frontier is not None else {}
                            ),
                            "observed_revision": fresh.get("revision"),
                            "observed_action": fresh.get("action"),
                        }
                        stalled.add(target)
                        continue
                    # Eligibility and lifecycle action can change without a new head
                    # (e.g. a human hold or a check conclusion). Never execute the old
                    # action with refreshed but contradictory selection evidence.
                    if fresh["action"] != action or not fresh["eligible"]:
                        _defer(item, "selection_changed", "reselect_current_lifecycle_action")
                        made_progress = True
                        continue
                    if fresh.get("revision") is None:
                        _defer(item, "revision_unavailable", "restore_source_observation")
                        stalled.add(target)
                        continue

                    acquired: list[str] = []
                    if is_mutating:
                        active_ops, active_mutating_repos = _active_operations(items)
                        repos = set(fresh["repo_keys"])
                        if repos.intersection(active_mutating_repos):
                            _defer(item, "repo_operation_running", "reconcile_repo_operation")
                            continue
                        # Statefile locks are private to a round. Shared repository
                        # claims serialize distinct targets across separate ledgers.
                        claim_targets = sorted(
                            {*(f"closer-repo:{repo}" for repo in repos), *fresh["claim_targets"]}
                        )
                        item["claim_targets"] = claim_targets
                        acquired, claim_error, claim_holder = _claim_all(
                            claim_targets, round_owner, claim_call, release_call, holder_call
                        )
                        if acquired:
                            pending_claims[id(acquired)] = acquired
                        if claim_error:
                            item["holder"] = (
                                _json_copy(claim_holder)
                                if isinstance(
                                    claim_holder, (dict, list, str, int, float, bool, type(None))
                                )
                                else {"status": "unreadable"}
                            )
                            _defer(item, "claim_held", "retry_after_claim_release")
                            _event(round_state, target, "claim_refused", holder=item["holder"])
                            stalled.add(target)
                            continue

                    if not budget_available():
                        release_acquired(acquired)
                        _defer(item, "receipt_reserve", "continue_next_round")
                        stop_launch_reason = "receipt_reserve"
                        continue
                    previous = item.get("operation")
                    # Keep a durable completed result available to the coordinator;
                    # restarting a round must not repeat the same action at the same
                    # source revision merely because process-local worker memory reset.
                    if (
                        isinstance(previous, dict)
                        and previous.get("status") == "completed"
                        and previous.get("action") == action
                        and previous.get("revision") == fresh["revision"]
                        and previous.get("binding") == fresh["operation_binding"]
                    ):
                        _defer(
                            item,
                            "completed_result_pending_integration",
                            "integrate_saved_operation_result",
                        )
                        item["saved_operation_result"] = _json_copy(previous.get("result", {}))
                        stalled.add(target)
                        release_acquired(acquired)
                        continue
                    if (
                        isinstance(previous, dict)
                        and previous.get("status") == "confirmed_not_started"
                    ):
                        op = previous
                        if op.get("attempts", 0) > max_operation_retries:
                            release_acquired(acquired)
                            _defer(item, "operation_uncertain", "manual_operation_reconciliation")
                            stalled.add(target)
                            continue
                        op["status"] = "submitted"
                        op["attempts"] = int(op.get("attempts", 0)) + 1
                        op["submitted_at"] = float(now())
                    else:
                        history = item.get("operation_history", [])
                        if isinstance(previous, dict):
                            history = (history + [previous])[-MAX_OPERATION_HISTORY:]
                        item["operation_history"] = history
                        op = {
                            "id": _operation_id(round_owner, target, action, fresh["revision"]),
                            "action": action,
                            "status": "submitted",
                            "attempts": 1,
                            "revision": fresh["revision"],
                            "owner": round_owner,
                            "repo_keys": fresh["repo_keys"],
                            "claim_targets": (
                                claim_targets if is_mutating else fresh["claim_targets"]
                            ),
                            "binding": _json_copy(fresh["operation_binding"], limit=8192),
                            "submitted_at": float(now()),
                        }
                    execution_context = None
                    if frontier is not None:
                        import closer_resources

                        try:
                            execution_context = closer_resources.capture_finite_execution_context(
                                target, frontier
                            )
                        except closer_resources.ResourceBlocked:
                            release_acquired(acquired)
                            _defer(
                                item, "finite_execution_context_refused", "inspect_original_owner"
                            )
                            stalled.add(target)
                            continue
                    op_deadline = request_deadline()
                    op["request_started_at"] = float(now())
                    op["deadline"] = op_deadline
                    op["timeout_seconds"] = max(0.0, op_deadline - float(now()))
                    if frontier is not None:
                        op["finite_frontier"] = frontier
                    item["operation"] = op
                    item["status"] = "submitted"
                    item.pop("deferred_reason", None)
                    item.pop("deferred_next_trigger", None)
                    persist_state()  # intent is durable before the callback
                    _event(
                        round_state,
                        target,
                        "operation_submitted",
                        operation_id=op["id"],
                        action=action,
                    )
                    persist_state()
                    started = monotonic()
                    callback_snapshot = _execution_snapshot(fresh)
                    future = executor.submit(
                        _invoke_before_cutoff,
                        execute,
                        target,
                        action,
                        callback_snapshot,
                        dict(op),
                        now,
                        execution_context,
                    )
                    launched.append((target, item, op, future, acquired, started))

                # Wait for every action launched this pass before deciding whether to
                # launch another pass. The pool bounds concurrency and no background
                # callback survives round finalization.
                for target, item, op, future, acquired, started in launched:
                    action = op["action"]
                    result_persist_failed = False
                    try:
                        result, invoked_at = future.result()
                        if invoked_at is None:
                            op["status"] = "confirmed_not_started"
                            # Only the real queued cutoff path reaches here without
                            # entering execute. Never infer this from adapter JSON.
                            if action in {"initiate_verify", "post_merge_verify"}:
                                candidate = {
                                    "schema": "closer-verification-callback-not-invoked/v1",
                                    "target": target,
                                    "operation_id": op["id"],
                                    "action": action,
                                    "revision": op["revision"],
                                    "binding_sha256": _digest(op["binding"]),
                                    "deadline": op["deadline"],
                                    "observed_at": float(now()),
                                    "callback_invoked": False,
                                }
                                op["result"] = {"verification_callback_not_invoked": candidate}
                                if verification_callback_not_invoked_proof(op, target) is None:
                                    op.pop("result")
                            op["next_trigger"] = "continue_next_round"
                            _defer(item, "receipt_reserve", op["next_trigger"])
                            stop_launch_reason = "receipt_reserve"
                            stalled.add(target)
                            continue
                        op["invoked_at"] = invoked_at
                        if not isinstance(result, Mapping):
                            raise ValueError("execute must return an object")
                        result = dict(result)
                        result_status = result.get("status")
                        if result_status not in {
                            "completed",
                            "running",
                            "failed",
                            "uncertain",
                            "blocked",
                            "deferred",
                            "stale",
                            "not_started",
                            "resource_wait",
                        }:
                            raise ValueError("execute returned an unsupported operation status")
                        stored_result = {
                            key: value
                            for key, value in result.items()
                            if key not in {"status", "advanced", "next_trigger"}
                        }
                        op["result"] = _json_copy(stored_result)
                        op["elapsed_seconds"] = max(0.0, monotonic() - started)
                        op["status"] = result_status
                        op["next_trigger"] = result.get("next_trigger")
                        item["operation"] = op
                        if result_status == "resource_wait":
                            op["completed_at"] = float(now())
                            proof = verifier_resource_wait_proof(op, target)
                            if not _native_verify_wait_executor(execute) or proof is None:
                                raise ValueError("untrusted_native_verifier_prewrite_wait")
                            op["next_trigger"] = verifier_resource_wait_event(proof)
                            _defer(item, "verifier_resource_wait", op["next_trigger"])
                            stalled.add(target)
                        elif result_status == "completed":
                            op["completed_at"] = float(now())
                            # Post-effect observation may consult this same durable
                            # operation (for example an acknowledged verifier POST).
                            # Publish its confirmed result before that read; external
                            # snapshots need not change when a request is accepted.
                            try:
                                persist_state()
                            except Exception:
                                result_persist_failed = True
                                raise
                            if not budget_available_at_start or not budget_available():
                                item["observation_freshness"] = "cached_budget_exhausted"
                                if result.get("advisory_only") or result.get(
                                    "requires_coordinator"
                                ):
                                    item["requires_coordinator"] = True
                                    item["saved_operation_result"] = _json_copy(stored_result)
                                _defer(item, "receipt_reserve", "retry_observation_next_round")
                                stalled.add(target)
                                continue
                            after = _observe_or_error(observe, target)
                            changed = _set_observation(
                                item, after, owner=round_owner, source_revisions=source_revisions
                            )
                            snapshots[target] = after
                            advanced = (
                                bool(result.get("advanced")) or changed or after["action"] != action
                            )
                            if advanced:
                                made_progress = True
                                progress_targets.append(target)
                                round_state.setdefault("progress", []).append(
                                    {"target": target, "action": action, "at": float(now())}
                                )
                                item["status"] = "observed"
                                item.pop("deferred_reason", None)
                                item.pop("deferred_next_trigger", None)
                                _event(
                                    round_state,
                                    target,
                                    "action_advanced",
                                    action=action,
                                    revision=after["revision"],
                                )
                            else:
                                _defer(
                                    item,
                                    "no_progress",
                                    result.get("next_trigger") or "inspect_action_result",
                                )
                                stalled.add(target)
                        elif result_status == "running":
                            _defer(
                                item,
                                "operation_running",
                                result.get("next_trigger") or "reconcile_operation",
                            )
                            stalled.add(target)
                        elif result_status == "uncertain":
                            _defer(
                                item,
                                "operation_uncertain",
                                result.get("next_trigger") or "reconcile_operation",
                            )
                            stalled.add(target)
                        elif result_status == "not_started":
                            # The adapter must return this only before entering a remote
                            # mutation. Keep the durable intent for a later same-binding
                            # retry without misclassifying a failed read as an unknown write.
                            op["status"] = "confirmed_not_started"
                            op["next_trigger"] = (
                                result.get("next_trigger") or "refresh_remote_before_action"
                            )
                            _defer(item, "remote_action_not_started", op["next_trigger"])
                            stalled.add(target)
                        elif result_status == "blocked" and "merge_guard_refusal" in stored_result:
                            op.update(status="failed", outcome="blocked", completed_at=float(now()))
                            if (
                                not _native_verify_wait_executor(execute)
                                or merge_guard_refusal_proof(op, target) is None
                            ):
                                raise ValueError("untrusted_native_merge_guard_refusal")
                            op["next_trigger"] = "refresh_merge_guard_before_action"
                            _defer(item, "merge_guard_refused", op["next_trigger"])
                            stalled.add(target)
                        else:
                            if result_status == "stale":
                                try:
                                    latest = _observe_or_error(observe, target)
                                    _set_observation(
                                        item,
                                        latest,
                                        owner=round_owner,
                                        source_revisions=source_revisions,
                                    )
                                    snapshots[target] = latest
                                except Exception as refresh_exc:
                                    round_state.setdefault("invalid_observations", {})[target] = (
                                        type(refresh_exc).__name__
                                    )
                            op["status"] = "failed"
                            op["outcome"] = result_status
                            _defer(
                                item,
                                "operation_failed",
                                result.get("next_trigger") or "inspect_operation_failure",
                            )
                            stalled.add(target)
                    except Exception as exc:
                        op["status"] = "uncertain"
                        op["next_trigger"] = "reconcile_operation_before_retry"
                        op["error_type"] = type(exc).__name__
                        _defer(item, "operation_uncertain", op["next_trigger"])
                        stalled.add(target)
                        _event(
                            round_state, target, "execute_uncertain", error_type=type(exc).__name__
                        )
                        if result_persist_failed:
                            # Durable control-plane failures are fatal, not provider
                            # failures. The finally write retains the unknown intent
                            # for reconciliation without allowing a second effect.
                            raise
                    finally:
                        released = release_acquired(acquired)
                        if released:
                            item["claim_release_failures"] = released
                            item["status"] = "deferred"
                            item["deferred_reason"] = "claim_release_failed"
                            item["deferred_next_trigger"] = "inspect_claim_state"
                            stalled.add(target)
                        persist_state()

                # Check an active write/observe callback did not overrun the action
                # cutoff. No further task is launched when only receipt reserve remains.
                if not budget_available_at_start or not budget_available():
                    stop_launch_reason = "receipt_reserve"
                    break
                # A full worker batch may have made no progress while untouched
                # assignments were only deferred for temporary capacity. Fill those
                # free slots rather than ending the whole round on the first stall.
                runnable_deferred = any(
                    target not in stalled
                    and items[target].get("eligible")
                    and items[target].get("deferred_reason")
                    in {"worker_capacity", "repo_operation_running"}
                    for target in targets
                )
                if not made_progress and not runnable_deferred:
                    break

        finally:
            # Fatal writes and BaseException exits may skip unprocessed futures.
            # Cancel only callbacks not yet started; wait for every actual worker
            # before dropping any remaining repository/source claims. Their
            # durable submitted/uncertain intents remain reconciliation fences.
            executor.shutdown(wait=True, cancel_futures=True)
            for acquired in list(pending_claims.values()):
                release_acquired(acquired)
        # Final fresh observations are the basis of assignment accounting. Pending
        # operation state still prevents any second action even if the source read
        # already reflects its external change.
        active_ops, active_mutating_repos = _active_operations(items)
        for target in targets:
            item = items[target]
            if frontier is not None and target in round_state.get("invalid_observations", {}):
                _defer(item, "observation_failed", "retry_observation_next_round")
                continue
            if (
                frontier is not None
                and item.get("deferred_reason") == "finite_frontier_identity_mismatch"
            ):
                continue
            if not budget_available_at_start or not budget_available():
                # No final read is worth crossing the cutoff. The durable item is
                # explicitly marked cached or unobserved when the round started
                # without budget; otherwise it retains the freshest in-round view.
                if (
                    item.get("observation_freshness") == "fresh"
                    and snapshots.get(target, {}).get("action") == "complete"
                ):
                    item["status"] = "complete"
                    item.pop("deferred_reason", None)
                    item.pop("deferred_next_trigger", None)
                elif item.get("eligible") or item.get("requires_coordinator"):
                    _defer(item, "receipt_reserve", "retry_observation_next_round")
                continue
            operation = item.get("operation")
            if isinstance(operation, dict) and operation.get("status") in PENDING_OPERATION_STATES:
                if (
                    frontier is None
                    and legacy_deadline is not None
                    and _remaining(legacy_deadline, receipt_reserve_seconds, now) <= 0
                ):
                    _defer(item, "receipt_reserve", "reconcile_next_round")
                    continue
                reason = (
                    "operation_running"
                    if operation.get("status") == "running"
                    else "operation_uncertain"
                )
                _defer(item, reason, operation.get("next_trigger") or "reconcile_operation")
                continue
            try:
                final = _observe_or_error(observe, target)
            except Exception as exc:
                final = _failure_observation(target, exc)
                round_state.setdefault("invalid_observations", {})[target] = type(exc).__name__
            _set_observation(item, final, owner=round_owner, source_revisions=source_revisions)
            snapshots[target] = final
            if frontier is not None and target in round_state.get("invalid_observations", {}):
                _defer(item, "observation_failed", "retry_observation_next_round")
            elif (
                final.get("requires_coordinator")
                and frontier is None
                and legacy_deadline is not None
                and _remaining(legacy_deadline, receipt_reserve_seconds, now) <= 0
            ):
                _defer(item, "receipt_reserve", "continue_next_round")
            elif final["action"] == "complete":
                item["status"] = "complete"
                item.pop("deferred_reason", None)
                item.pop("deferred_next_trigger", None)
            elif not final["eligible"]:
                reason = final["reason"] or "not_eligible"
                trigger = final["next_trigger"] or "wait_for_trigger"
                _defer(item, reason, trigger)
            elif item.get("deferred_reason") in RESOURCE_DEFERRALS:
                item["status"] = "deferred"
            elif stop_launch_reason == "receipt_reserve":
                _defer(item, "receipt_reserve", "continue_next_round")
            elif item.get("deferred_reason") in {
                "operation_failed",
                "operation_uncertain",
                "no_progress",
                "revision_changed",
                "claim_release_failed",
                "completed_result_pending_integration",
            }:
                item["status"] = "deferred"
            elif (
                int(round_state.get("passes", 0)) >= slice_pass_limit
                and item.get("action") not in TERMINAL_ACTIONS
            ):
                _defer(item, "pass_limit", "continue_next_round")
            else:
                _defer(item, "unresolved_eligible_work", "continue_next_round")

        round_state["source_revisions"] = source_revisions
        round_state["source_hash"] = _digest(
            {target: source_revisions.get(target) for target in targets}
        )
        round_state["status"] = "finished"
        finished_at = float(now())
        accounting = [_account_row(target, items[target]) for target in targets]
        accounted_targets = [row["target"] for row in accounting]
        eligible_remaining = [
            row["target"]
            for row in accounting
            if (row["eligible"] or row["requires_coordinator"]) and row["status"] != "complete"
        ]
        integration_required = [row["target"] for row in accounting if row["requires_coordinator"]]
        deferred = [row for row in accounting if row["status"] == "deferred"]
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "started_at": round_state["started_at"],
            "finished_at": finished_at,
            **({"deadline": round_state["deadline"]} if frontier is None else {}),
            "receipt_reserve_seconds": round_state["receipt_reserve_seconds"],
            "owner": round_owner,
            "assignment_targets": targets,
            "assignment_hash": assignment_hash,
            "source_hash": round_state["source_hash"],
            "accounted_targets": accounted_targets,
            "accounting": accounting,
            "eligible_remaining": eligible_remaining,
            "integration_required": integration_required,
            "deferred": deferred,
            "progress": list(round_state.get("progress", [])),
            "events": list(round_state.get("events", [])),
            "continuation_required": bool(eligible_remaining),
        }
        validation_state = (
            {**state, "items": items, "round": round_state}
            if frontier_bucket is not None
            else state
        )
        validation = _validate_data(
            validation_state,
            receipt,
            assignment_targets=targets,
            now_value=finished_at,
            holder_fn=holder_call,
        )
        receipt.update(validation)
        round_state["receipt"] = receipt
        persist_state()
        return receipt


__all__ = ["ACTION_NAMES", "QueueStateError", "run_round", "validate_stop"]
