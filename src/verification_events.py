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


def material_selectors(value: Any) -> list[dict[str, Any]]:
    """Validate bounded exact locators; the hosted collector authenticates bytes."""
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("repeat verification requires specific material evidence")
    result = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("invalid verification material selector")
        if row.get("kind") == "comment":
            if set(row) != {"kind", "url"} or not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pull|issues)/[1-9][0-9]*#issuecomment-[1-9][0-9]*",
                str(row.get("url")),
            ):
                raise ValueError("verification comment needs an exact public GitHub locator")
        elif row.get("kind") == "artifact_member":
            from pathlib import PurePosixPath

            path = row.get("path")
            if (
                set(row) != {"kind", "run_id", "artifact_id", "path"}
                or type(row.get("run_id")) is not int
                or row["run_id"] < 1
                or type(row.get("artifact_id")) is not int
                or row["artifact_id"] < 1
                or not isinstance(path, str)
                or not path
                or "\\" in path
                or "\x00" in path
                or len(path) > 512
                or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
            ):
                raise ValueError("verification artifact needs exact run/member identity")
        else:
            raise ValueError("unsupported verification material selector")
        if row not in result:
            result.append(dict(row))
    return sorted(result, key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))


def request_matches(snapshot: dict[str, Any], disposition: dict[str, Any]) -> dict[str, Any] | None:
    """Return an evidence-bound repeat request, never an acceptance assertion."""
    request = disposition.get("verification_request")
    prior = snapshot.get("verifier")
    if not isinstance(request, dict):
        return None
    import closer_dispatcher
    import verifier_evidence

    repo, number = snapshot.get("pr_target", snapshot["target"]).split("#")
    if not isinstance(prior, dict):
        lifecycle = snapshot.get("verifier_lifecycle") or {}
        if lifecycle.get("schema") != verifier_evidence.STATE_MARKER:
            return None
        candidates = [
            record
            for record in lifecycle.get("_lifecycle_history", [])
            if isinstance(record, dict)
            and "verdict" in record
            and ((record.get("verification_event") or {}).get("event_id") or legacy_id(record))
            == request.get("predecessor_event_id")
        ]
        if len(candidates) != 1:
            return None
        prior = candidates[0]
    if not verifier_evidence.decision_matches_identity(
        prior, repo, int(number), snapshot.get("head"), snapshot.get("merge")
    ):
        return None
    predecessor = (prior.get("verification_event") or {}).get("event_id") or legacy_id(prior)
    if not prior.get("verification_event"):
        progress = request.get("recovery_progress") or {}
        if (
            not isinstance(progress, dict)
            or not HEX256.fullmatch(str(progress.get("before")))
            or not HEX256.fullmatch(str(progress.get("after")))
            or progress["before"] == progress["after"]
        ):
            return None
    if (
        request.get("schema") != "verification-request-reentry/v1"
        or request.get("head") != snapshot.get("head")
        or request.get("merge") != snapshot.get("merge")
        or request.get("review_revision") != snapshot.get("review_revision")
        or request.get("source_digest") != closer_dispatcher.source_digest(snapshot["sources"])
        or request.get("predecessor_event_id") != predecessor
    ):
        return None
    try:
        selected = material_selectors(request.get("material"))
    except ValueError:
        return None
    return {**request, "material": selected}


def recovered_required_material(prep, packet, root, proof_hashes):
    """Prove legacy re-entry from retained necessary collection progress.

    Legacy reports have no comparable material inventory. A schema-version or
    packet-serialization change therefore cannot authorize another evaluation.
    """
    from pathlib import Path

    import closer_evidence
    import closer_evidence_recovery

    progress = prep.get("verification_recovery_progress") or {}
    sections = progress.get("sections")
    ref = progress.get("before_packet_ref") or {}
    if (
        progress.get("schema") != "verification-recovery-progress/v1"
        or not isinstance(sections, list)
        or not sections
        or any(
            not isinstance(section, str) or section not in packet.get("sections", {})
            for section in sections
        )
    ):
        return None
    required = {
        row.get("section")
        for row in prep.get("recovery_requests", [])
        if isinstance(row, dict) and row.get("necessity") == "necessary"
    }
    if not set(sections) <= required:
        return None
    path = Path(ref.get("path", ""))
    try:
        path.resolve(strict=True).relative_to((Path(root) / "manifests").resolve(strict=True))
        if path.is_symlink() or path.stat().st_size > 8_000_000:
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.get("sha256"):
            return None
        before = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if (
        not closer_evidence.validate_packet(before)
        or before.get("producer_sha256") != packet.get("producer_sha256")
        or any(
            before.get(key) != packet.get(key)
            for key in ("target", "subject_pr", "head", "base", "capture_revision", "bindings")
        )
    ):
        return None
    prior = closer_evidence_recovery.progress_fingerprint(before, set(sections))
    current = closer_evidence_recovery.progress_fingerprint(packet, set(sections))
    if prior == current:
        return None

    def hashes(value):
        found = set()
        if isinstance(value, dict):
            if (
                HEX256.fullmatch(str(value.get("sha256")))
                and type(value.get("bytes")) is int
                and value["bytes"] > 0
            ):
                found.add(value["sha256"])
            for child in value.values():
                found.update(hashes(child))
        elif isinstance(value, list):
            for child in value:
                found.update(hashes(child))
        return found

    new_hashes = set().union(
        *(
            hashes(packet["sections"][name].get("content"))
            - hashes(before["sections"][name].get("content"))
            for name in sections
        )
    )
    if not new_hashes.intersection(proof_hashes):
        return None
    return {"before_packet_ref": ref, "before": prior, "after": current, "sections": sections}


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


def pending_no_evaluation_proof(state, run, inventory):
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
    if len(pending) != 1 or len(evaluator) != 1:
        return None
    pending, evaluator = pending[0], evaluator[0]
    if (
        pending.get("status") != "completed"
        or pending.get("conclusion") != "success"
        or evaluator.get("status") != "completed"
        or evaluator.get("conclusion") != "skipped"
        or type(pending.get("number")) is not int
        or type(evaluator.get("number")) is not int
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
        "pending_step_number": pending["number"],
        "evaluator_step_number": evaluator["number"],
        "evaluator_step": binding["evaluator_step"],
        "conclusion": "skipped",
        "source": "actions.listJobsForWorkflowRunAttempt",
        "jobs_sha256": content_hash(jobs),
    }
