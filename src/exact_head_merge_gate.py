"""Fail-closed exact-head facts for terminal pull-request merges."""

from __future__ import annotations

import json
import math
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import provision

REVIEW_FLOOR_SECONDS = 420
MAX_THREAD_PAGES = 100

QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  id state isDraft headRefOid mergeable mergeStateStatus createdAt updatedAt
  headRepository{id pushedAt}
  headRef{target{oid}}
  timelineItems(last:1){updatedAt}
  commits(last:1){nodes{commit{oid statusCheckRollup{state}}}}
  reviewThreads(first:100,after:$cursor){
   totalCount pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated}
  }
 }}
}
""".strip()


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.astimezone(timezone.utc).timestamp()
        return timestamp if math.isfinite(timestamp) else None
    except ValueError:
        return None


def _merge_facts(pr: Any, *, expected_head: str) -> dict[str, Any]:
    if not isinstance(pr, dict):
        raise ValueError("malformed pull request")
    head_repo = pr.get("headRepository")
    head_ref = pr.get("headRef")
    timeline = pr.get("timelineItems")
    if (
        not isinstance(head_repo, dict)
        or not isinstance(head_ref, dict)
        or not isinstance(timeline, dict)
    ):
        raise ValueError("missing head or timeline evidence")
    try:
        commits = pr["commits"]["nodes"]
        commit = commits[0]["commit"]
        check_state = commit["statusCheckRollup"]["state"]
        head_target = head_ref["target"]["oid"]
    except (KeyError, TypeError, IndexError):
        raise ValueError("missing exact-head evidence") from None
    if not isinstance(commits, list) or len(commits) != 1 or not isinstance(commit, dict):
        raise ValueError("malformed exact-head evidence")

    timestamps: dict[str, float] = {}
    for key, value in {
        "pr_created_at": pr.get("createdAt"),
        "pr_updated_at": pr.get("updatedAt"),
        "timeline_updated_at": timeline.get("updatedAt"),
        "head_repository_pushed_at": head_repo.get("pushedAt"),
    }.items():
        parsed = _timestamp(value)
        if parsed is None:
            raise ValueError(f"{key} is unavailable")
        timestamps[key] = parsed

    facts = {
        "pr_id": pr.get("id"),
        "state": pr.get("state"),
        "is_draft": pr.get("isDraft"),
        "head": pr.get("headRefOid"),
        "head_target": head_target,
        "head_repository_id": head_repo.get("id"),
        "mergeable": pr.get("mergeable"),
        "merge_state_status": pr.get("mergeStateStatus"),
        "commit_oid": commit.get("oid"),
        "check_state": check_state,
        "timestamps": timestamps,
    }
    if (
        not isinstance(facts["pr_id"], str)
        or not isinstance(facts["head_repository_id"], str)
        or facts["state"] not in {"OPEN", "CLOSED", "MERGED"}
        or not isinstance(facts["is_draft"], bool)
        or not isinstance(facts["mergeable"], str)
        or not isinstance(facts["merge_state_status"], str)
        or not isinstance(facts["check_state"], str)
    ):
        raise ValueError("merge facts are malformed")
    if facts["head"] != expected_head or facts["head_target"] != expected_head:
        raise ValueError("observed head changed")
    if facts["commit_oid"] != expected_head:
        raise ValueError("exact-head commit changed")
    return facts


def snapshot(
    target: str,
    *,
    expected_head: str,
    run_fn=subprocess.run,
    now_fn=time.time,
    review_floor_seconds: int = REVIEW_FLOOR_SECONDS,
) -> dict[str, Any]:
    """Read every review-thread page and exact-head check/floor facts.

    Unknown, partial, malformed, changing, or incomplete observations block.
    Callers must take a fresh snapshot immediately before mutation.
    """
    repo, number = provision.parse_target(target)
    if number is None or "/" not in repo:
        return {"blocked": True, "reason": "target must be owner/repo#N"}
    owner, name = repo.split("/", 1)
    cursor: str | None = None
    seen: set[str] = set()
    active = 0
    first_facts: dict[str, Any] | None = None
    total_count: int | None = None
    thread_ids: set[str] = set()
    pages = 0

    while True:
        pages += 1
        if pages > MAX_THREAD_PAGES:
            return {"blocked": True, "reason": "review-thread pagination exceeded its page limit"}
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor is not None:
            cmd.extend(["-f", f"cursor={cursor}"])
        res = run_fn(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            return {"blocked": True, "reason": "review-thread GraphQL query failed"}
        try:
            doc = json.loads(res.stdout or "{}")
            if doc.get("errors"):
                raise ValueError("partial response")
            pr = doc["data"]["repository"]["pullRequest"]
            threads = pr["reviewThreads"]
            nodes = threads["nodes"]
            page = threads["pageInfo"]
            if not isinstance(nodes, list) or not isinstance(page, dict):
                raise ValueError("malformed response")
            facts = _merge_facts(pr, expected_head=expected_head)
        except ValueError as exc:
            if str(exc) in {"observed head changed", "exact-head commit changed"}:
                return {"blocked": True, "reason": "observed head changed"}
            return {"blocked": True, "reason": "review-thread state is unknown or malformed"}
        except (KeyError, TypeError, json.JSONDecodeError):
            return {"blocked": True, "reason": "review-thread state is unknown or malformed"}

        if first_facts is None:
            first_facts = facts
        elif facts != first_facts:
            return {
                "blocked": True,
                "reason": "merge facts changed during review-thread pagination",
            }

        observed_total = threads.get("totalCount")
        if isinstance(observed_total, bool) or not isinstance(observed_total, int):
            return {"blocked": True, "reason": "review-thread state is unknown or malformed"}
        if total_count is None:
            total_count = observed_total
        elif observed_total != total_count:
            return {"blocked": True, "reason": "review-thread count changed during pagination"}
        for thread in nodes:
            if (
                not isinstance(thread, dict)
                or not isinstance(thread.get("id"), str)
                or not isinstance(thread.get("isResolved"), bool)
                or not isinstance(thread.get("isOutdated"), bool)
            ):
                return {"blocked": True, "reason": "review-thread state is unknown or malformed"}
            if thread["id"] in thread_ids:
                return {"blocked": True, "reason": "review-thread pagination returned a duplicate"}
            thread_ids.add(thread["id"])
            active += int(not thread["isResolved"] and not thread["isOutdated"])

        has_next = page.get("hasNextPage")
        if not isinstance(has_next, bool):
            return {"blocked": True, "reason": "review-thread pagination is malformed"}
        if not has_next:
            break
        next_cursor = page.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            return {"blocked": True, "reason": "review-thread pagination is incomplete"}
        seen.add(next_cursor)
        cursor = next_cursor

    assert first_facts is not None and total_count is not None
    if len(thread_ids) != total_count:
        return {"blocked": True, "reason": "review-thread pagination is incomplete"}
    if first_facts["state"] != "OPEN" or first_facts["is_draft"] is not False:
        return {"blocked": True, "reason": "PR is not open and ready"}
    if active:
        return {
            "blocked": True,
            "reason": f"{active} active non-outdated review thread(s) remain",
            "active_threads": active,
        }
    if first_facts["mergeable"] != "MERGEABLE" or first_facts["merge_state_status"] != "CLEAN":
        return {"blocked": True, "reason": "PR is not cleanly mergeable"}
    now = float(now_fn())
    if not math.isfinite(now):
        return {"blocked": True, "reason": "current time is invalid"}
    review_not_before = max(first_facts["timestamps"].values())
    if review_not_before > now:
        return {"blocked": True, "reason": "review-floor timestamp is in the future"}
    age = now - review_not_before
    if age < review_floor_seconds:
        return {
            "blocked": True,
            "reason": f"exact-head review floor has {review_floor_seconds - age:.0f}s remaining",
            "review_age_seconds": age,
        }
    if first_facts["check_state"] != "SUCCESS":
        return {
            "blocked": True,
            "reason": f"exact-head required checks are {first_facts['check_state']!r}",
        }
    return {
        "blocked": False,
        "reason": None,
        "head": expected_head,
        "active_threads": 0,
        "review_age_seconds": age,
        "review_not_before": review_not_before,
        "check_state": first_facts["check_state"],
    }
