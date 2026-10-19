"""Join closer allocation/execution and independent validation in the existing Brain.

An accepted assessment is stage-quality evidence, never source completion or a
durable merge. Missing prices, usage and opportunity cost stay unknown.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable

TASKS = {"closer_interpret", "closer_exception", "closer_repair"}
VALIDATORS = {"closer_disposition.integrate_ordinary", "closer_dispatcher.response_binding"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


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
