"""Controller-owned follow-up for scoped advice; never action/acceptance authority.

Hashes bind retained local context, not a remote attestation. The production
adapter must authenticate the current callback before calling ``bind``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import PurePath

SCHEMA = "closer-adjudication-action-policy/v1"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _need(ok, reason):
    if not ok:
        raise ValueError(reason)


def current_callback(context, preparation, snapshot, request, *, now):
    """Authenticate the actual live queue fence before projecting local policy."""
    import closer_correction_owner as owner

    _row, lease, _fence = owner._current(context, preparation, now=now)
    return bind(context, snapshot, request, lease)


def bind(context, snapshot, request, lease):
    """Project an already authenticated original EXCEPTION callback, not ACT."""
    frontier = context["finite_frontier"]
    target = snapshot["target"]
    start, deadline = context["request_started_at"], context["deadline"]
    _need(
        all(type(x) in (int, float) and math.isfinite(x) for x in (start, deadline))
        and start < deadline
        and request["target"] == target
        and request["revision"] == snapshot["revision"] == context["revision"]
        and request["head"] == snapshot.get("head")
        and request["base"] == snapshot.get("base")
        and context["frontier_binding"] == frontier["bindings"][target]
        and frontier["bindings"][target]["revision"] == context["revision"]
        and set(request["basis"]) == {"finding_id"}
        and isinstance(request["basis"]["finding_id"], str)
        and bool(request["basis"]["finding_id"].strip())
        and all(
            isinstance(request[key], str) and re.fullmatch(r"[0-9a-f]{64}", request[key])
            for key in ("request_id", "packet_sha256")
        )
        and PurePath(context["state_path"]).is_absolute()
        and all(
            isinstance(context[key], str) and bool(context[key].strip())
            for key in ("work_key", "pipeline_run_id")
        )
        and set(lease) == {"target", "owner", "epoch"}
        and type(lease["epoch"]) is int
        and lease["epoch"] > 0
        and lease["target"] == target.lower()
        and isinstance(lease.get("owner"), str)
        and bool(lease["owner"]),
        "original exception callback identity unavailable",
    )
    value = {
        "schema": SCHEMA,
        "request_id": request["request_id"],
        "basis": copy.deepcopy(request["basis"]),
        "target": target,
        "head": snapshot.get("head"),
        "base": snapshot.get("base"),
        "revision": snapshot["revision"],
        "packet_sha256": request["packet_sha256"],
        "callback": {
            "state_path": context["state_path"],
            "operation_id": context["work_key"],
            "run_id": context["pipeline_run_id"],
            "frontier": copy.deepcopy(frontier),
            "lease": copy.deepcopy(lease),
            "started_at": start,
            "deadline": deadline,
        },
        "action": "wait",
        "wake": {
            "kind": "source_revision_changed",
            "target": target,
            "revision": snapshot["revision"],
        },
        "escalation": {"kind": "existing_campaign_owner", "at": deadline},
        "attempt_budget": 1,
        "authority": False,
        "acceptance_authority": False,
    }
    value["policy_id"] = digest(value)
    return value


def validate(value):
    """Validate a retained policy without renewing any clock or budget."""
    try:
        callback = value["callback"]
        expected = bind(
            {
                "finite_frontier": callback["frontier"],
                "revision": value["revision"],
                "frontier_binding": callback["frontier"]["bindings"][value["target"]],
                "request_started_at": callback["started_at"],
                "deadline": callback["deadline"],
                "state_path": callback["state_path"],
                "work_key": callback["operation_id"],
                "pipeline_run_id": callback["run_id"],
            },
            {key: value[key] for key in ("target", "head", "base", "revision")},
            {
                key: value[key]
                for key in (
                    "target",
                    "head",
                    "base",
                    "revision",
                    "request_id",
                    "basis",
                    "packet_sha256",
                )
            },
            callback["lease"],
        )
        return value == expected
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def pending(value):
    _need(validate(value), "invalid source follow-up policy")
    return {
        "status": "pending",
        "pending_kind": "adjudication_evidence",
        "reason": "named_finding_requires_source_evidence_event",
        "pending_evidence": {
            "event_id": value["policy_id"],
            "target": value["target"],
            "revision": value["revision"],
            "basis": value["basis"],
        },
        "next_trigger": "exact source revision change or original callback cutoff owner escalation",
        "controller_policy": copy.deepcopy(value),
        "authority": False,
    }


def available_job_requests(proposal, snapshot, packet, screen, *, basis, gh):
    """Only existing canonical, necessary exact-job requests with current metadata.

    This is a read observation, not a reservation: the existing collector still
    rechecks its own job/run/head binding and the Store gate still bounds retries.
    No locator is inferred from a diagnosis, missing future patch, or action enum.
    """
    import closer_evidence_recovery as recovery

    requests = recovery.validate_requests(proposal, snapshot, packet, screen)
    satisfied = {r["id"] for r in screen.get("requirements", []) if r.get("satisfied")}
    selected = [
        r
        for r in requests
        if r.get("necessity") == "necessary"
        and r["id"] not in satisfied
        and r.get("basis") == basis
        and r.get("locator", {}).get("kind") == "job_log"
    ]
    subject = snapshot.get("pr_target", snapshot["target"])
    repo, number = subject.split("#")
    available = []
    for request in selected:
        job_id = request["locator"]["job_id"]
        job = gh.api(f"repos/{repo}/actions/jobs/{job_id}")
        run_id = job.get("run_id")
        if job.get("id") != job_id or type(run_id) is not int or job.get("status") != "completed":
            continue
        run = gh.api(f"repos/{repo}/actions/runs/{run_id}")
        if (
            run.get("id") == run_id
            and run.get("status") == "completed"
            and run.get("head_sha") == snapshot.get("head")
            and int(number) in [r.get("number") for r in run.get("pull_requests", [])]
        ):
            available.append(request)
    return available


def finish_advice(
    value,
    *,
    context,
    preparation,
    snapshot,
    packet,
    screen,
    proposal,
    recovery_enabled,
    store,
    github_factory,
    collect,
    actual,
    now,
):
    """Source adapter after validated terminal advice, inside the same callback.

    ``collect`` is the existing dispatcher collection callback, not a model or
    action runner. Its result can retain fresh PREPARE data but never reenter a
    model or grant a fresh admission here. Unknown availability remains owned.
    """
    result = pending(value)
    if not recovery_enabled or now >= value["callback"]["deadline"]:
        return result
    try:
        import closer_evidence_recovery as recovery

        current = current_callback(
            context,
            preparation,
            snapshot,
            {
                key: value[key]
                for key in (
                    "target",
                    "head",
                    "base",
                    "revision",
                    "request_id",
                    "basis",
                    "packet_sha256",
                )
            },
            now=now,
        )
        _need(current == value, "original follow-up callback changed")
        gh = github_factory(deadline=value["callback"]["deadline"])
        requests = available_job_requests(
            proposal, snapshot, packet, screen, basis=value["basis"], gh=gh
        )
        collection_screen = recovery.apply_requests(screen, requests, packet)
        attempt = store.begin(snapshot, packet, collection_screen) if requests else None
        if attempt is None:
            return result
        collection_proposal = {
            **proposal,
            "evidence_requests": [
                {
                    "locator": request["locator"],
                    "basis": request["basis"],
                    "necessity": "necessary",
                    "reason": request["reason"],
                    "question": request["question"],
                }
                for request in requests
            ],
        }
        collected = collect(
            snapshot["target"],
            context,
            preparation,
            snapshot,
            packet,
            collection_screen,
            collection_proposal,
            attempt,
            actual,
            collection_only=True,
        )
        refreshed = collected.get("_reenter_recovered") or collected.get("recovered_prepare")
        if refreshed is not None:
            result["recovered_prepare"] = refreshed
        result["controller_collection"] = {
            "action": "collect_evidence",
            "attempt_key": attempt,
            "requests": requests,
            "outcome": collected.get("_collection_outcome")
            or (collected.get("evidence_recovery") or {}).get("outcome", "owned_pending"),
            "authority": False,
        }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        # An interrupted read attempt remains started in the existing Store;
        # never mark an uncertain observation retryable or invoke another model.
        result["collection_unavailable"] = type(error).__name__
    return result
