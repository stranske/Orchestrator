"""Shared, transport-independent identities for the existing verifier lifecycle.

Only the collector can establish the request/material digests. Hash validation is
integrity checking, not provenance: callers must still authenticate the producer
and bind the event to the actual merged delivery before using a verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA = "verification-event/v1"
HEX256 = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
CORE = ("schema", "repo", "pr", "head_sha", "evaluated_sha", "request_sha256", "material_sha256")


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def event_id(event: dict[str, Any]) -> str:
    return content_hash({key: event[key] for key in CORE})


def valid_event(event: Any, repo: str, number: int, head: Any, merge: Any) -> bool:
    if not isinstance(event, dict) or event.get("schema") != SCHEMA:
        return False
    if (
        event.get("repo") != repo.lower()
        or event.get("pr") != str(number)
        or event.get("head_sha") != head
        or event.get("evaluated_sha") != merge
        or not SHA.fullmatch(str(head))
        or not SHA.fullmatch(str(merge))
        or not HEX256.fullmatch(str(event.get("request_sha256")))
        or not HEX256.fullmatch(str(event.get("material_sha256")))
    ):
        return False
    return event.get("event_id") == event_id(event)


def legacy_id(decision: dict[str, Any]) -> str:
    """Address an immutable old result without claiming an evidence digest."""
    return content_hash(
        {
            "schema": "verification-legacy-event/v1",
            **{
                key: str(decision[key]).lower() if key == "repo" else str(decision[key])
                for key in ("repo", "pr", "head_sha", "evaluated_sha", "run_id", "run_attempt")
            },
        }
    )


def recovery_progress_descriptor(previous, requests, root):
    """Optional retained proof metadata must not break ordinary preparation."""
    from pathlib import Path

    sections = sorted({r["section"] for r in requests if r.get("necessity") == "necessary"})
    if not isinstance(previous, dict) or not sections:
        return None
    packet_sha = (previous.get("manifest") or {}).get("packet_sha256")
    if not HEX256.fullmatch(str(packet_sha)):
        return None
    path = Path(root) / "manifests" / (packet_sha + ".json")
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return {
        "schema": "verification-recovery-progress/v1",
        "sections": sections,
        "before_packet_ref": {"path": str(path), "sha256": digest},
    }


def execution_binding(value):
    """Fixed producer execution metadata, never caller-selected executable names."""
    expected = {
        "schema": "verifier-execution-binding/v1",
        "workflow_path": ".github/workflows/agents-verifier.yml",
        "job_name": "Run post-merge verifier",
        "pending_step": "Record verification event pending",
    }
    if not isinstance(value, dict) or set(value) != {*expected, "evaluator_step"}:
        return None
    if any(value.get(key) != wanted for key, wanted in expected.items()):
        return None
    if value.get("evaluator_step") not in {"Run LLM evaluation", "Run multi-provider comparison"}:
        return None
    return dict(value)


def pending_no_evaluation_proof(state, run, inventory, *, _require_pending=True):
    """Reduce exact authenticated terminal execution evidence; absence proves nothing."""
    binding = execution_binding(state.get("execution_binding"))
    assessment = state.get("assessment_identity") or {}
    expected_step = {
        "evaluate": "Run LLM evaluation",
        "compare": "Run multi-provider comparison",
    }.get(assessment.get("mode"))
    if (
        not binding
        or expected_step is None
        or binding["evaluator_step"] != expected_step
        or assessment.get("canonical") is not True
    ):
        return None
    if (
        not binding
        or state.get("status") != "pending"
        or not isinstance(run, dict)
        or not isinstance(inventory, dict)
    ):
        return None
    if (
        str(run.get("id")) != str(state.get("run_id"))
        or str(run.get("run_attempt")) != str(state.get("run_attempt"))
        or (run.get("repository") or {}).get("full_name", "").lower() != state.get("repo")
        or (run.get("head_repository") or {}).get("full_name", "").lower() != state.get("repo")
        or run.get("path") != binding["workflow_path"]
        or run.get("event") not in {"workflow_dispatch", "pull_request_target"}
        or run.get("status") != "completed"
        or not isinstance(run.get("head_sha"), str)
        or not SHA.fullmatch(run["head_sha"])
    ):
        return None
    jobs = inventory.get("jobs")
    if (
        not isinstance(jobs, list)
        or type(inventory.get("total_count")) is not int
        or inventory["total_count"] != len(jobs)
        or len(jobs) > 1000
    ):
        return None
    ids = set()
    for row in jobs:
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or row["id"] in ids
            or str(row.get("run_id")) != str(state["run_id"])
            or str(row.get("run_attempt")) != str(state["run_attempt"])
            or row.get("head_sha") != run["head_sha"]
        ):
            return None
        ids.add(row["id"])
    matching = [
        job
        for job in jobs
        if isinstance(job, dict)
        and isinstance(job.get("name"), str)
        and (
            job["name"] == binding["job_name"] or job["name"].endswith(" / " + binding["job_name"])
        )
    ]
    if len(matching) != 1:
        return None
    job = matching[0]
    if (
        type(job.get("id")) is not int
        or job["id"] <= 0
        or job.get("status") != "completed"
        or ("run_id" in job and str(job["run_id"]) != str(state["run_id"]))
        or ("run_attempt" in job and str(job["run_attempt"]) != str(state["run_attempt"]))
        or not isinstance(job.get("steps"), list)
    ):
        return None
    for step in (
        step
        for row in jobs
        if isinstance(row, dict)
        for step in (row.get("steps") or [])
        if isinstance(row.get("steps"), list)
    ):
        if (
            isinstance(step, dict)
            and step.get("name") in {"Run LLM evaluation", "Run multi-provider comparison"}
            and (step.get("status") != "completed" or step.get("conclusion") != "skipped")
        ):
            return None
    pending = [
        s for s in job["steps"] if isinstance(s, dict) and s.get("name") == binding["pending_step"]
    ]
    evaluator = [
        s
        for s in job["steps"]
        if isinstance(s, dict) and s.get("name") == binding["evaluator_step"]
    ]
    if len(evaluator) != 1 or (_require_pending and len(pending) != 1):
        return None
    pending = pending[0] if len(pending) == 1 else None
    evaluator = evaluator[0]
    if (
        evaluator.get("status") != "completed"
        or evaluator.get("conclusion") != "skipped"
        or type(evaluator.get("number")) is not int
        or evaluator["number"] <= 0
    ):
        return None
    if _require_pending and (
        pending.get("status") != "completed"
        or pending.get("conclusion") != "success"
        or type(pending.get("number")) is not int
        or not 0 < pending["number"] < evaluator["number"]
    ):
        return None
    return {
        "schema": "verifier-no-evaluation-proof/v1",
        "repo": state["repo"],
        "event_id": state["verification_event"]["event_id"],
        "assessment_identity": state.get("assessment_identity"),
        "run_id": str(state["run_id"]),
        "run_attempt": str(state["run_attempt"]),
        "run_head_sha": run["head_sha"],
        "workflow_path": binding["workflow_path"],
        "job_id": job["id"],
        "pending_step_number": pending["number"] if pending else None,
        "evaluator_step_number": evaluator["number"],
        "evaluator_step": binding["evaluator_step"],
        "conclusion": "skipped",
        "source": "actions.listJobsForWorkflowRunAttempt",
        "jobs_sha256": content_hash(jobs),
    }


def pending_execution_identity(run):
    """Stable authority fields for an exact attempt and the separate latest read."""
    if not isinstance(run, dict):
        return None
    result = {
        key: run.get(key)
        for key in ("id", "run_attempt", "status", "conclusion", "head_sha", "path", "event")
    }
    for key in ("repository", "head_repository"):
        repository = run.get(key)
        result[key] = (
            repository.get("full_name", "").lower() if isinstance(repository, dict) else None
        )
    return result


def pending_no_evaluation_history_proof(state, attempts, latest):
    """Authenticate every intervening terminal attempt without rewriting the marker."""
    try:
        first, last = int(state["run_attempt"]), int(latest["run_attempt"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        first < 1
        or last < first
        or last - first >= 10
        or not isinstance(attempts, list)
        or len(attempts) != last - first + 1
    ):
        return None
    proofs = []
    workflow_head = None
    for number, observation in zip(range(first, last + 1), attempts):
        if not isinstance(observation, dict):
            return None
        run = observation.get("run")
        bound_state = {**state, "run_attempt": str(number)}
        proof = pending_no_evaluation_proof(
            bound_state, run, observation.get("inventory"), _require_pending=number == first
        )
        if proof is None or (workflow_head is not None and proof["run_head_sha"] != workflow_head):
            return None
        workflow_head = proof["run_head_sha"]
        proofs.append(proof)
    if pending_execution_identity(latest) != pending_execution_identity(attempts[-1]["run"]):
        return None
    result = dict(proofs[0])
    result["latest_run_attempt"] = str(last)
    result["subsequent_attempts"] = [
        {
            key: proof[key]
            for key in (
                "run_attempt",
                "run_head_sha",
                "workflow_path",
                "job_id",
                "evaluator_step_number",
                "evaluator_step",
                "conclusion",
                "jobs_sha256",
            )
        }
        for proof in proofs[1:]
    ]
    return result
