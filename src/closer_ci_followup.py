"""Bounded CI terminal collection for an original proven no-effect queue wait."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, TypeGuard

import closer_queue as queue
import closer_verifier_followup as verifier_followup

SCHEMA = "closer-ci-terminal-followup/v1"


def _number(value: Any) -> TypeGuard[int | float]:
    return type(value) in {int, float} and math.isfinite(value)


def pending_evidence(row: Mapping[str, Any]) -> Mapping[str, Any]:
    action = (row.get("results") or {}).get("act") or {}
    evidence = action.get("pending_evidence")
    if not (
        row.get("status") == "held"
        and row.get("stage") == "act"
        and row.get("stage_status") == "completed"
        and action.get("status") == "pending"
        and action.get("pending_kind") == "ci"
        and isinstance(evidence, Mapping)
    ):
        raise ValueError("CI predecessor is not a completed no-effect read wait")
    return evidence


def source_snapshot(row):
    """Read full original retained PREPARE bytes, never compact source summaries."""
    import closer_finite_reentry as reentry

    prep = row["results"].get("prepare") or {}
    compact = prep.get("snapshot") or {}
    reference = prep.get("snapshot_ref")
    if reference is None:
        if "sources" not in compact:
            raise ValueError("CI original full source snapshot is unavailable")
        return compact
    original, sha = reentry._read(reference["path"])
    if not (
        sha == reference.get("sha256")
        and isinstance(original, Mapping)
        and original.get("target") == compact.get("target")
        and original.get("revision") == prep.get("revision")
        and all(original.get(key) == compact.get(key) for key in ("head", "base", "state", "kind"))
        and "sources" in original
    ):
        raise ValueError("CI original retained source snapshot changed")
    return original


def terminal_proof(
    action_path, target, frontier, row, observation, check, *, now=None, wait_proof=None
):
    """Validate exact old wait and fresh native check/source facts; no verdict authority."""
    current = time.time() if now is None else now
    evidence = pending_evidence(row)
    action = row["results"]["act"]
    wait = (
        wait_proof
        if wait_proof is not None
        else queue.ci_wait_no_effect_proof(
            action_path,
            target,
            frontier=frontier,
            receipt=action["queue_receipt"],
            evidence=evidence,
        )
    )
    original = source_snapshot(row)
    stamp = observation.get("observed_at")
    if not (
        isinstance(check, Mapping)
        and type(check.get("id")) is int
        and check["id"] == wait["check_run_id"]
        and check.get("head_sha") == wait["head"]
        and check.get("status") == "completed"
        and check.get("conclusion") == "success"
        and isinstance(original, Mapping)
        and original.get("head") == wait["head"]
        and observation.get("target") == target
        and observation.get("head") == wait["head"]
        and observation.get("_local_cached") is not True
        and observation.get("checks") == "SUCCESS"
        and observation.get("eligible") is True
        and observation.get("action") not in {None, "wait"}
        and _number(stamp)
        and 0 <= current - stamp <= 120
        and verifier_followup.source_requirements(original)
        == verifier_followup.source_requirements(observation)
    ):
        raise ValueError("CI terminal identity, readiness or original requirements changed")
    return {
        "schema": SCHEMA,
        "target": target,
        "original_wait": wait,
        "terminal_check": {
            key: check.get(key)
            for key in ("id", "head_sha", "status", "conclusion", "name", "html_url")
        },
        "source_requirements": verifier_followup.source_requirements(original),
        "substantive_witness": verifier_followup.substantive_witness(observation),
        "authority": False,
    }


def original_wait_proof(state_path, action_path, target, frontier, row):
    """On restart, admit only the already linked child beside original bytes."""
    import closer_finite_reentry as reentry
    import closer_frontier_policy as policy
    import closer_pipeline as pipeline

    action = row["results"]["act"]
    document = pipeline._load(state_path)
    matches = [
        value
        for value in (document.get("stage_successors") or {}).values()
        if isinstance(value, Mapping)
        and (value.get("stage_predecessor") or {}).get("predecessor_frontier_id")
        == frontier["frontier_id"]
        and (value.get("stage_predecessor") or {}).get("target") == target
    ]
    if not matches:
        return queue.ci_wait_no_effect_proof(
            action_path,
            target,
            frontier=frontier,
            receipt=action["queue_receipt"],
            evidence=pending_evidence(row),
        )
    if len(matches) != 1:
        raise ValueError("CI predecessor has conflicting successor owners")
    link = matches[0]
    lineage = link.get("stage_predecessor") or {}
    child = policy.validate_frontier(link["finite_frontier"])
    if not (
        link.get("schema") == "closer-stage-successor/v1"
        and lineage.get("kind") == "ci_no_effect_wait"
        and lineage.get("predecessor_row_sha256") == pipeline._digest(row)
        and lineage.get("predecessor_frontier_sha256") == pipeline._digest(frontier)
        and child["targets"] == [target]
        and child["owner"] == frontier["owner"]
    ):
        raise ValueError("CI original successor binding changed")
    reference = lineage["original_queue"]
    archived, sha = reentry._read(reference["path"])
    if sha != reference["sha256"]:
        raise ValueError("CI original no-effect snapshot changed")
    proof = queue._ci_wait_proof_data(
        archived,
        target,
        frontier=frontier,
        receipt=action["queue_receipt"],
        evidence=pending_evidence(row),
        now_value=time.time(),
    )
    proof["queue_sha256"] = sha
    if proof != lineage.get("original_wait"):
        raise ValueError("CI original no-effect proof differs from retained bytes")
    current = queue._read_state(action_path)
    if any(current.get(key) != archived.get(key) for key in ("schema", "items", "round")):
        raise ValueError("CI unrelated queue history changed")
    old_namespaces = archived.get("frontiers") or {}
    live_namespaces = current.get("frontiers") or {}
    from closer_stage_continuation import linked_chain

    chain, _leaf, _entry = linked_chain(document, frontier, target)
    allowed = {
        hop["record"]["finite_frontier"]["frontier_id"]: hop["record"]["finite_frontier"]
        for hop in chain
    }
    if (set(live_namespaces) - set(old_namespaces)) - set(allowed):
        raise ValueError("CI foreign or unlinked effect namespace exists")
    if any(live_namespaces.get(key) != value for key, value in old_namespaces.items()):
        raise ValueError("CI original queue namespace changed after linking")
    for name, descriptor in allowed.items():
        if name in live_namespaces:
            queue.state_for_frontier(current, name, expected_policy=descriptor)
    return proof


def read_terminal(backend, action_path, target, frontier, row, *, state_path=None):
    """Use the existing native GitHub client for one exact GET and fresh observe."""
    evidence = pending_evidence(row)
    action = row["results"]["act"]
    # Validate no-effect before collecting; unknown/history never routes to a new submission.
    wait = (
        original_wait_proof(state_path, action_path, target, frontier, row)
        if state_path is not None
        else queue.ci_wait_no_effect_proof(
            action_path,
            target,
            frontier=frontier,
            receipt=action["queue_receipt"],
            evidence=evidence,
        )
    )
    original = source_snapshot(row)
    subject = original.get("pr_target", target)
    repo, _number = subject.split("#")
    import closer_resources

    closer_resources.canonical_target(subject)
    check = backend.gh.api(f"repos/{repo}/check-runs/{wait['check_run_id']}")
    if not (
        isinstance(check, Mapping)
        and type(check.get("id")) is int
        and check["id"] == wait["check_run_id"]
        and check.get("head_sha") == wait["head"]
    ):
        raise ValueError("original CI check identity changed or is unavailable")
    if check.get("status") != "completed" or check.get("conclusion") is None:
        return {
            "status": "waiting",
            "reason": "original CI check is not terminal",
            "check_run_id": wait["check_run_id"],
        }
    if check.get("conclusion") != "success":
        return {
            "status": "escalated",
            "escalation_reason": "ci_check_failed_existing_corrective_lane",
            "proof": {
                "original_wait": wait,
                "terminal_check": {
                    key: check.get(key)
                    for key in ("id", "head_sha", "status", "conclusion", "name", "html_url")
                },
            },
            "authority": False,
        }
    observation = backend.observe(target)
    observation["observed_at"] = time.time()
    # Revalidate original queue after remote reads, not a cached valid flag.
    fresh_wait = (
        original_wait_proof(state_path, action_path, target, frontier, row)
        if state_path is not None
        else queue.ci_wait_no_effect_proof(
            action_path,
            target,
            frontier=frontier,
            receipt=action["queue_receipt"],
            evidence=evidence,
        )
    )
    if fresh_wait != wait:
        raise ValueError("CI original no-effect proof changed during reads")
    proof = terminal_proof(
        action_path, target, frontier, row, observation, check, wait_proof=fresh_wait
    )
    return {"status": "ready", "proof": proof, "observation": observation}
