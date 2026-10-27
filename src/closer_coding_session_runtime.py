"""Source-owned local v2 supervision, never native account/token qualification."""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

import closer_coding_session_v2 as contract

if TYPE_CHECKING:
    from closer_coding_delivery import CodingDelivery
    from closer_correction_controller import _ProviderGuard

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_VALIDATOR_SOURCE_SHA256 = contract._LOADED_SOURCE_SHA256
CURRENT_FIELD = "coding_session_runtime_v2"


class SessionHeld(RuntimeError):
    """Constant diagnostics only; never echo provider text."""


@dataclass(frozen=True)
class _Plan:
    guard: _ProviderGuard
    origin: FrameType
    run: FrameType
    budget_utf8: bytes
    delivery: CodingDelivery
    started_mono: float
    deadline_mono: float
    thread: int


_OFFLOADS: dict[tuple[int, FrameType], _Plan] = {}
_RUNS: dict[tuple[int, FrameType], _Plan] = {}
_APPLIED: dict[tuple[int, FrameType], float] = {}
_FACTS: dict[tuple[int, FrameType], dict[str, int | bool]] = {}
_HISTORY_RESTRICTIONS: dict[tuple[int, FrameType], _ProviderGuard] = {}


def prepare_history_restriction(frame, callback):
    """Restrict telemetry before admission; this never authorizes execution."""
    guard = getattr(callback, "__self__", None)
    if guard is None:
        return
    import closer_correction_controller as controller
    import dispatcher

    if (
        type(guard) is controller._ProviderGuard
        and type(guard.controller) is controller.IndexController
        and getattr(callback, "__func__", None) is controller._ProviderGuard.starting
        and guard.active
        and guard.thread == threading.get_ident()
        and contract.POLICY_FIELD in guard.controller.config.consumers.get(guard.consumer_id, {})
    ):
        if (
            inspect.currentframe().f_back is not frame
            or frame.f_code is not dispatcher._CODING_DELIVERY_OFFLOAD_CODE
            or frame.f_globals is not dispatcher.__dict__
        ):
            raise SessionHeld("original coding preparation boundary required")
        _HISTORY_RESTRICTIONS[(threading.get_ident(), frame)] = guard


def history_restricted(frame):
    """No personal session-store fallback, including refusal before a plan exists."""
    origin = _origin(frame)
    return origin is not None and any(
        held_origin is origin for _, held_origin in _HISTORY_RESTRICTIONS
    )


def _origin(frame):
    import dispatcher

    if type(frame) is not FrameType:
        raise SessionHeld("original source frame required")
    while frame is not None:
        if (
            frame.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
            and frame.f_globals is dispatcher.__dict__
        ):
            return frame
        frame = frame.f_back
    return None


def _plan(frame):
    origin = _origin(frame)
    if origin is None:
        return None
    plan = _OFFLOADS.get((threading.get_ident(), origin))
    if plan is None and any(value.origin is origin for value in _OFFLOADS.values()):
        raise SessionHeld("coding-session crossed original source thread")
    return plan


def _budget(plan):
    return json.loads(plan.budget_utf8)


def _require_redaction():
    import credential_redaction

    if credential_redaction.disabled():
        raise SessionHeld("v2 coding requires enabled credential redaction")


def issue_before_popen(frame, *, bootstrap=False):
    """Only the actual source guard can issue; supplied JSON is never a permit."""
    import closer_coding_delivery as delivery
    import closer_correction_controller as controller
    import dispatcher

    caller = inspect.currentframe().f_back
    guard = caller.f_locals.get("self")
    if not (
        caller.f_code is controller._ProviderGuard.starting.__code__
        and caller.f_globals is controller.__dict__
        and caller.f_back is frame
        and type(guard) is controller._ProviderGuard
        and guard.active
        and (
            guard.used
            if not bootstrap
            else __import__("closer_codex_owned_stdio").reserved(frame, guard)
        )
        and frame.f_code is dispatcher._CODING_DELIVERY_RUN_CODE
        and frame.f_globals is dispatcher.__dict__
    ):
        raise SessionHeld("actual source coding-session guard required")
    _require_redaction()
    guard.controller._require_writer(guard.operation, guard.custody)
    registration = guard.controller.config.consumers[guard.consumer_id]
    record = guard.custody.read("admission-" + guard.selected["record_sha256"] + ".json")
    original = contract.snapshot_from_admission(record)
    policy = contract.validate_policy(registration[contract.POLICY_FIELD])
    limits = contract.minimum_budget(
        original["budget"], registration[CURRENT_FIELD], policy["budget"]
    )
    origin = _origin(frame)
    observed = delivery._capture(frame)
    held = delivery._ACTIVE.get((threading.get_ident(), frame))
    if (
        observed is None
        or observed != held
        or origin is None
        or origin.f_locals.get("max_network_retries") != 0
        or origin.f_locals.get("attempt") != 1
        or len(observed.prompt_utf8) > limits["max_prompt_utf8_bytes"]
        or original["consumer_id"] != guard.consumer_id
        or policy["consumer_id"] != guard.consumer_id
        or original["profile_sha256"] != registration["profile_sha256"]
        or policy["profile_sha256"] != registration["profile_sha256"]
    ):
        raise SessionHeld("exact original coding-session launch unavailable")
    now_mono = time.monotonic()
    remaining = guard.operation["deadline"] - time.time()
    requested_timeout = frame.f_locals.get("timeout")
    if requested_timeout is not None:
        if type(requested_timeout) not in (int, float) or requested_timeout <= 0:
            raise SessionHeld("actual coding-session timeout invalid")
        remaining = min(remaining, requested_timeout - (now_mono - frame.f_locals["started"]))
    if remaining <= 0:
        raise SessionHeld("current coding-session callback deadline expired")
    key = (threading.get_ident(), origin)
    if key in _OFFLOADS:
        import closer_codex_owned_stdio as owned_stdio

        plan = _OFFLOADS[key]
        if (
            bootstrap
            or plan.run is not frame
            or plan.guard is not guard
            or plan.delivery != observed
            or json.loads(plan.budget_utf8) != limits
            or plan.deadline_mono <= now_mono
            or _FACTS[key]["outer_submissions"] != 1
            or not _FACTS[key]["spawn_observed"]
        ):
            raise SessionHeld("original bootstrap session changed or expired")
        owned_stdio.accept_final_guard(frame, guard, plan)
        return True  # Reuse the sole plan/submission/pool and original deadline.

    plan = _Plan(
        guard,
        origin,
        frame,
        json.dumps(limits, sort_keys=True, separators=(",", ":")).encode(),
        observed,
        now_mono,
        now_mono + min(remaining, limits["max_runtime_ms"] / 1000),
        threading.get_ident(),
    )
    _OFFLOADS[key] = plan
    _RUNS[(plan.thread, frame)] = plan
    _FACTS[key] = {
        "outer_submissions": 0,
        "spawn_observed": False,
        "cli_completion_known": False,
        "owned_group_terminal": False,
        "captured_stdout_bytes": 0,
        "captured_stderr_bytes": 0,
        "local_elapsed_ms": 0,
    }


def activate(frame):
    import closer_coding_delivery as delivery

    plan = _plan(frame)
    if plan is None:
        return None
    if (
        inspect.currentframe().f_back is not frame
        or _RUNS.get((plan.thread, frame)) is not plan
        or plan.run is not frame
        or not plan.guard.active
        or delivery._capture(frame) != plan.delivery
        or (plan.thread, frame) in _APPLIED
    ):
        raise SessionHeld("original coding-session activation required")
    _require_redaction()
    plan.guard.controller._require_writer(plan.guard.operation, plan.guard.custody)
    remaining = plan.deadline_mono - time.monotonic()
    if remaining <= 0:
        raise SessionHeld("coding-session deadline elapsed before spawn")
    _APPLIED[(plan.thread, frame)] = remaining
    return remaining, _budget(plan)["max_captured_output_bytes"]


def supervision(frame):
    """Remaining time is recomputed against one fixed deadline, never renewed."""
    import closer_coding_delivery as delivery

    plan = _plan(frame)
    if plan is None:
        return None
    if (
        inspect.currentframe().f_back is not frame
        or _RUNS.get((threading.get_ident(), frame)) is not plan
        or plan.run is not frame
        or not plan.guard.active
        or delivery._capture(frame) != plan.delivery
        or _APPLIED.get((plan.thread, frame)) != frame.f_locals.get("timeout")
        or frame.f_locals.get("output_limit") != _budget(plan)["max_captured_output_bytes"]
    ):
        raise SessionHeld("live source coding-session supervision changed")
    remaining = plan.deadline_mono - time.monotonic()
    if remaining <= 0:
        raise SessionHeld("coding-session deadline elapsed before spawn or supervision")
    return remaining, _budget(plan)["max_captured_output_bytes"]


def mark_submission(frame):
    plan = _plan(frame)
    if plan is None:
        return
    if (
        inspect.currentframe().f_back is not frame
        or _RUNS.get((plan.thread, frame)) is not plan
        or plan.deadline_mono <= time.monotonic()
    ):
        raise SessionHeld("original unexpired coding submission required")
    facts = _FACTS[(plan.thread, plan.origin)]
    if facts["outer_submissions"] != 0:
        raise SessionHeld("coding outer submission already consumed")
    facts["outer_submissions"] = 1


def mark_started(frame):
    plan = _plan(frame)
    if plan is not None:
        if inspect.currentframe().f_back is not frame:
            raise SessionHeld("original coding process observation required")
        _FACTS[(plan.thread, plan.origin)]["spawn_observed"] = True


def mark_terminal(frame, observation, captured):
    plan = _plan(frame)
    if plan is None:
        return
    if inspect.currentframe().f_back is not frame:
        raise SessionHeld("original coding terminal observation required")
    child = frame.f_locals["child"]
    facts = _FACTS[(plan.thread, plan.origin)]
    import closer_codex_owned_stdio

    protocol_counts = closer_codex_owned_stdio.counted_bytes(frame)
    facts.update(
        cli_completion_known=type(child.poll()) is int,
        owned_group_terminal=observation.get("cleanup_terminal") is True,
        captured_stdout_bytes=(
            protocol_counts[0] if protocol_counts is not None else len(captured["stdout"])
        ),
        captured_stderr_bytes=(
            protocol_counts[1] if protocol_counts is not None else len(captured["stderr"])
        ),
        local_elapsed_ms=max(0, int((time.monotonic() - plan.started_mono) * 1000)),
    )


def capture_limit(frame):
    plan = _plan(frame)
    return _budget(plan)["max_captured_output_bytes"] if plan is not None else None


def cleanup_seconds(frame):
    plan = _plan(frame)
    return _budget(plan)["max_cleanup_ms"] / 1000 if plan is not None else None


def check_text(frame, *values):
    plan = _plan(frame)
    if plan is not None:
        _require_redaction()
        try:
            count = sum(len(value.encode("utf-8")) for value in values)
        except UnicodeError:
            raise SessionHeld("coding-session output encoding invalid") from None
        if count > _budget(plan)["max_retained_final_utf8_bytes"]:
            raise SessionHeld("coding-session redacted final output exceeds bound")


def check_serialized(frame, raw):
    plan = _plan(frame)
    if plan is not None:
        _require_redaction()
        if len(raw) > _budget(plan)["max_retained_receipt_bytes"]:
            raise SessionHeld("coding-session serialized result exceeds bound")


def bound_result(frame, result):
    plan = _plan(frame)
    if plan is None:
        return
    import credential_redaction

    _require_redaction()
    fields = ("output", "stderr_tail", "partial_stdout", "partial_stderr", "error")
    for field in fields:
        if isinstance(result.get(field), str):
            result[field] = credential_redaction.redact(result[field])
    check_text(frame, *(result.get(field, "") or "" for field in fields))
    result["usage"] = {}
    result["coding_session_v2"] = {
        "schema": "closer-coding-session-observation/v2",
        **_FACTS[(plan.thread, plan.origin)],
        "native_model_attempts": "UNKNOWN",
        "native_input_tokens": "UNKNOWN",
        "native_output_tokens": "UNKNOWN",
        "budget": _budget(plan),
    }
    if "attempts" in result:
        result["attempts_scope"] = "outer_cli_submission"
    check_serialized(frame, json.dumps(result, sort_keys=True).encode("utf-8"))


def release_guard(guard):
    """Revoke on every guard exit, including failure before or after Popen."""
    import closer_correction_controller as controller

    caller = inspect.currentframe().f_back
    if (
        caller.f_code is not controller.IndexController.final_provider_guard.__wrapped__.__code__
        or caller.f_globals is not controller.__dict__
    ):
        raise SessionHeld("original source guard cleanup required")
    import closer_coding_route_v2

    closer_coding_route_v2.release_guard(guard)
    import closer_codex_owned_stdio

    closer_codex_owned_stdio.release_guard(guard)
    for key, held_guard in tuple(_HISTORY_RESTRICTIONS.items()):
        if held_guard is guard:
            _HISTORY_RESTRICTIONS.pop(key, None)
    fence = None
    unknown_cleanup = False
    for key, plan in tuple(_OFFLOADS.items()):
        if plan.guard is guard:
            facts = _FACTS.get(key, {})
            if facts.get("spawn_observed") and not facts.get("owned_group_terminal"):
                import dispatcher

                fence = dispatcher._finite_pool_fence(plan.run.f_locals.get("observed"))
                unknown_cleanup = True
            _OFFLOADS.pop(key, None)
            _RUNS.pop((plan.thread, plan.run), None)
            _APPLIED.pop((plan.thread, plan.run), None)
            _FACTS.pop(key, None)
    if unknown_cleanup:
        if fence is not None:
            import closer_resources

            raise closer_resources.FinitePoolUncertain(fence)
        raise SessionHeld("coding-session cleanup unverified; original intent retained")


def cancel_starting(frame):
    """Known broker no-spawn only, scoped to the actual held v2 reservation."""
    import closer_correction_controller as controller
    import dispatcher

    callback = frame.f_locals.get("starting_callback")
    guard = getattr(callback, "__self__", None)
    if (
        type(guard) is not controller._ProviderGuard
        or type(guard.controller) is not controller.IndexController
        or not guard.active
        or guard.thread != threading.get_ident()
        or getattr(callback, "__func__", None) is not controller._ProviderGuard.starting
    ):
        return
    registration = guard.controller.config.consumers.get(guard.consumer_id)
    if type(registration) is not dict or contract.POLICY_FIELD not in registration:
        return
    if (
        inspect.currentframe().f_back is not frame
        or frame.f_code is not dispatcher._CODING_DELIVERY_RUN_CODE
        or frame.f_globals is not dispatcher.__dict__
    ):
        raise SessionHeld("original coding pre-start boundary required")
    current = getattr(controller.queue._callback_threads, "invocation", None)
    frontier = guard.operation.get("finite_frontier")
    if (
        type(current) is not dict
        or not controller.queue._currently_invoking(current.get("target"), guard.operation)
        or type(frontier) is not dict
    ):
        return
    import closer_resources

    expected = {
        "agent": registration["agent"],
        "profile_id": registration["profile_id"],
        "target": current["target"],
        "frontier_id": frontier["frontier_id"],
        "process_group": None,
        "process_birth": None,
    }
    closer_resources.finite_pool_no_start(frame.f_locals["observation"], expected=expected)
    closer_resources.finite_pool_no_start(expected=expected)
