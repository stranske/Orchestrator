"""Fail-closed exact-head facts for terminal pull-request merges."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from typing import Any

import provision

REVIEW_FLOOR_SECONDS = 420

QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  state isDraft headRefOid mergeStateStatus
  commits(last:1){nodes{commit{oid pushedDate statusCheckRollup{state}}}}
  reviewThreads(first:100,after:$cursor){
   pageInfo{hasNextPage endCursor} nodes{isResolved isOutdated}
  }
 }}
}
""".strip()


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    first: dict[str, Any] | None = None

    while True:
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
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {"blocked": True, "reason": "review-thread state is unknown or malformed"}

        if pr.get("headRefOid") != expected_head:
            return {"blocked": True, "reason": "observed head changed"}
        if first is None:
            first = pr
        for thread in nodes:
            if (
                not isinstance(thread, dict)
                or not isinstance(thread.get("isResolved"), bool)
                or not isinstance(thread.get("isOutdated"), bool)
            ):
                return {"blocked": True, "reason": "review-thread state is unknown or malformed"}
            active += int(not thread["isResolved"] and not thread["isOutdated"])

        if page.get("hasNextPage") is False:
            break
        next_cursor = page.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            return {"blocked": True, "reason": "review-thread pagination is incomplete"}
        seen.add(next_cursor)
        cursor = next_cursor

    assert first is not None
    if first.get("state") != "OPEN" or first.get("isDraft") is not False:
        return {"blocked": True, "reason": "PR is not open and ready"}
    if active:
        return {
            "blocked": True,
            "reason": f"{active} active non-outdated review thread(s) remain",
            "active_threads": active,
        }
    try:
        commit = first["commits"]["nodes"][0]["commit"]
        check_state = commit["statusCheckRollup"]["state"]
    except (KeyError, TypeError, IndexError):
        return {"blocked": True, "reason": "exact-head check or push-time state is unknown"}
    pushed_at = _timestamp(commit.get("pushedDate"))
    if commit.get("oid") != expected_head or pushed_at is None:
        return {"blocked": True, "reason": "exact-head push-time state is unknown"}
    age = float(now_fn()) - pushed_at
    if age < review_floor_seconds:
        return {
            "blocked": True,
            "reason": f"exact-head review floor has {review_floor_seconds - age:.0f}s remaining",
            "review_age_seconds": age,
        }
    if check_state != "SUCCESS":
        return {"blocked": True, "reason": f"exact-head required checks are {check_state!r}"}
    return {
        "blocked": False,
        "reason": None,
        "head": expected_head,
        "active_threads": 0,
        "review_age_seconds": age,
        "check_state": check_state,
    }
