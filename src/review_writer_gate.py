"""Read-only all-provider native writer terminality proof."""

from __future__ import annotations

import re
from typing import Callable

import outcomes


def _pages(call, endpoint):
    rows = []
    for page in range(1, 101):
        value = call(f"{endpoint}{'&' if '?' in endpoint else '?'}page={page}")
        if not isinstance(value, list):
            raise ValueError("malformed page")
        rows.extend(value)
        if len(value) < 100:
            return rows
    raise ValueError("pagination limit")


def _job_pages(call, endpoint):
    rows = []
    for page in range(1, 101):
        value = call(f"{endpoint}{'&' if '?' in endpoint else '?'}page={page}")
        if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
            raise ValueError("malformed jobs page")
        chunk = value["jobs"]
        rows.extend(chunk)
        if len(chunk) < 100:
            return rows
    raise ValueError("jobs pagination limit")


def _attempt(value):
    if not isinstance(value, str):
        return None
    parts = value.rsplit(":", 2)
    if (
        len(parts) != 3
        or not parts[1].isdigit()
        or not parts[2].isdigit()
        or not re.fullmatch(r"[\w.-]+/[\w.-]+", parts[0])
    ):
        return None
    return parts[0], int(parts[1]), int(parts[2])


def _records(comments, pr):
    reservations, malformed = [], []
    for comment in comments:
        if not isinstance(comment, dict):
            malformed.append("comment")
            continue
        if not outcomes._trusted_runner_comment(comment):
            continue
        for kind, provider, number, raw in outcomes.RUNNER_MARKER_RE.findall(
            str(comment.get("body") or "")
        ):
            if int(number) != pr:
                continue
            payload = outcomes._runner_payload(raw)
            if payload is None:
                malformed.append(f"{provider}:{kind}")
            elif kind == "runner-reservation":
                if (
                    not isinstance(payload.get("reservation_id"), str)
                    or payload.get("provider") != provider
                    or payload.get("pr_number") != pr
                ):
                    malformed.append(f"{provider}:reservation")
                else:
                    reservations.append(payload)
    return reservations, malformed


def _completion(comments, reservation, pr):
    rid, provider = reservation["reservation_id"], reservation["provider"]
    for comment in comments:
        if not outcomes._trusted_runner_comment(comment):
            continue
        for kind, marked, number, raw in outcomes.RUNNER_MARKER_RE.findall(
            str(comment.get("body") or "")
        ):
            if kind != "runner-completion" or marked != provider or int(number) != pr:
                continue
            payload = outcomes._runner_payload(raw)
            record = payload.get("record") if isinstance(payload, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("schema") == outcomes.RUNNER_RECEIPT_SCHEMA
                and payload.get("reservation_id") == rid
                and isinstance(record, dict)
                and record.get("reservation_id") == rid
                and record.get("provider") == provider
                and record.get("pr_number") == pr
                and record.get("status") == "completed"
                and not record.get("completion_incomplete", False)
                and (
                    record.get("workflow_attempt_id")
                    in (None, reservation.get("workflow_attempt_id"))
                )
            ):
                return True
    return False


def _child_terminal(jobs, provider, parent_status=None):
    found = False
    all_terminal = True
    provider_jobs = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        step_rows = job.get("steps")
        if not isinstance(step_rows, list):
            step_rows = []
        steps = {str(x.get("name")): x for x in step_rows if isinstance(x, dict)}
        run, push = steps.get(f"Run {provider.title()}"), steps.get("Commit and push changes")
        # Queued jobs may not expose steps yet. A stale completion receipt must
        # never override this direct evidence of an admitted implementation.
        named_writer = re.search(
            rf"\brun\s+{re.escape(provider)}\b", str(job.get("name", "")), re.I
        )
        if named_writer:
            provider_jobs.append(job)
        if named_writer and job.get("status") in {"queued", "in_progress"} and not (run and push):
            return False, "implementation child queued/in progress without step evidence"
        if run and push:
            found = True
            if run.get("status") != "completed" or push.get("status") != "completed":
                all_terminal = False
    if found and all_terminal:
        return True, "actual implementation and push terminal"
    if found and not all_terminal:
        return False, "implementation child queued/failed"
    if parent_status == "completed":
        if not provider_jobs:
            return True, "implementation child skipped (terminal)"
        if all(
            j.get("status") == "completed" and j.get("conclusion") == "skipped"
            for j in provider_jobs
        ):
            return True, "implementation child skipped (terminal)"
    return False, (
        "implementation child absent or skipped"
        if not found
        else "implementation child queued/failed"
    )


def writer_terminality(
    repo: str, pr_number: int, gh_json_callable: Callable[[str], object]
) -> dict:
    """Prove every provider reservation terminal; missing evidence is UNKNOWN."""
    try:
        comments = _pages(
            gh_json_callable, f"repos/{repo}/issues/{pr_number}/comments?per_page=100"
        )
    except Exception as exc:
        return {
            "terminal": False,
            "reason": f"comments API failed: {type(exc).__name__}",
            "evidence": [],
        }
    reservations, malformed = _records(comments, pr_number)
    if malformed:
        return {
            "terminal": False,
            "reason": f"malformed trusted marker {malformed[0]}",
            "evidence": [],
        }
    evidence: list[dict] = []
    attempt_cache = {}
    for record in reservations:
        rid, provider = record["reservation_id"], record["provider"]
        parsed = _attempt(record.get("workflow_attempt_id"))
        if not parsed:
            return {
                "terminal": False,
                "reason": f"reservation {rid} missing/malformed workflow_attempt_id",
                "evidence": evidence,
            }
        source_repo, run_id, attempt = parsed
        base = f"repos/{source_repo}/actions/runs/{run_id}/attempts/{attempt}"
        cache_key = (source_repo, run_id, attempt)
        try:
            if cache_key not in attempt_cache:
                attempt_cache[cache_key] = (
                    gh_json_callable(base),
                    _job_pages(gh_json_callable, base + "/jobs?per_page=100"),
                )
            run, jobs = attempt_cache[cache_key]
        except Exception as exc:
            return {
                "terminal": False,
                "reason": f"reservation {rid} attempt API failed: {type(exc).__name__}",
                "evidence": evidence,
            }
        if (
            not isinstance(run, dict)
            or run.get("id") != run_id
            or run.get("run_attempt") != attempt
        ):
            return {
                "terminal": False,
                "reason": f"reservation {rid} attempt identity mismatch",
                "evidence": evidence,
            }
        child_ok, child_reason = _child_terminal(
            jobs, provider, parent_status=run.get("status") if isinstance(run, dict) else None
        )
        completion = _completion(comments, record, pr_number)
        evidence.append(
            {
                "reservation_id": rid,
                "provider": provider,
                "workflow_attempt_id": record["workflow_attempt_id"],
                "parent_status": run.get("status") if isinstance(run, dict) else None,
                "child": child_reason,
                "completion": completion,
            }
        )
        if "queued" in child_reason or not (completion or child_ok):
            return {
                "terminal": False,
                "reason": f"reservation {rid}: {child_reason}",
                "evidence": evidence,
            }
    return {"terminal": True, "reason": "all provider reservations terminal", "evidence": evidence}
