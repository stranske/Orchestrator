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

# The review floor holds a merge until REVIEW_FLOOR_SECONDS have passed since THIS PR's latest event,
# so the review bots have had a whole floor to read the exact head. Its anchor is the latest of the
# PR's creation, its last update (comments, reviews and edits on it), the last time it was marked
# ready for review if it ever was, and the move of its head ref to the expected head (HEAD_REF_MOVED,
# read from the head repository's activity log, since GraphQL no longer supports
# `Commit.pushedDate`). Every one of them is about this PR alone. Until 2026-10-04 two were not:
# `headRepository.pushedAt` is the repository's last push to ANY branch, and the timeline
# connection's `updatedAt` moves whenever another PR or issue, in any repository, references this one
# (#447 re-stamped #411 and #439 at 22:14:44Z while neither PR changed). With several sessions and the
# keepalive bots pushing every few minutes, a 420-second quiet window existed in 20% of 19:00-21:00Z
# that day: #439 was blocked 4 times though its head never moved after the PR opened, and #437 19.
# Latched-gate answers. (1) The clock drains it, and only this PR's own events reset the anchor.
# (2) Nothing the gate forbids is needed for that: a PR goes quiet without merging. (3) The anchor
# measures this PR, and only this PR going quiet drains it; the repository-wide anchor measured every
# branch in the repository. (4) A drained floor reports `review_floor_remaining_seconds` 0 beside its
# anchor, and a blocked one names the anchor that reset it. A head move it cannot read blocks, and is
# never treated as an old one.
HEAD_REF_MOVED = "head_ref_moved_at"

QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  id state isDraft headRefOid headRefName mergeable mergeStateStatus createdAt updatedAt
  headRepository{id nameWithOwner}
  headRef{target{oid}}
  readyForReview:timelineItems(last:1,itemTypes:[READY_FOR_REVIEW_EVENT]){
   nodes{... on ReadyForReviewEvent{createdAt}}
  }
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
    ready = pr.get("readyForReview")
    if (
        not isinstance(head_repo, dict)
        or not isinstance(head_ref, dict)
        or not isinstance(ready, dict)
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

    # This PR's own timestamps; the head ref's move joins them in `snapshot`, from the activity log.
    # Never read: the head REPOSITORY's `pushedAt` (a push to any branch) and the timeline
    # connection's `updatedAt` (a reference from any other PR or issue).
    timestamps: dict[str, float] = {}
    for key, value in {
        "pr_created_at": pr.get("createdAt"),
        "pr_updated_at": pr.get("updatedAt"),
    }.items():
        parsed = _timestamp(value)
        if parsed is None:
            raise ValueError(f"{key} is unavailable")
        timestamps[key] = parsed
    # A PR opened as a draft is first reviewable when it is marked ready, and the bots review it
    # then. No such event is an answer (it never was a draft); an event without a time is unknown.
    ready_nodes = ready.get("nodes")
    if not isinstance(ready_nodes, list) or len(ready_nodes) > 1:
        raise ValueError("ready-for-review evidence is malformed")
    if ready_nodes:
        node = ready_nodes[0]
        parsed = _timestamp(node.get("createdAt")) if isinstance(node, dict) else None
        if parsed is None:
            raise ValueError("ready_for_review_at is unavailable")
        timestamps["ready_for_review_at"] = parsed

    facts = {
        "pr_id": pr.get("id"),
        "state": pr.get("state"),
        "is_draft": pr.get("isDraft"),
        "head": pr.get("headRefOid"),
        "head_target": head_target,
        "head_ref_name": pr.get("headRefName"),
        "head_repository": head_repo.get("nameWithOwner"),
        "head_repository_id": head_repo.get("id"),
        "mergeable": pr.get("mergeable"),
        "merge_state_status": pr.get("mergeStateStatus"),
        "commit_oid": commit.get("oid"),
        "check_state": check_state,
        "timestamps": timestamps,
    }
    if (
        not isinstance(facts["pr_id"], str)
        or not isinstance(facts["head_ref_name"], str)
        or not facts["head_ref_name"]
        or not isinstance(facts["head_repository"], str)
        or facts["head_repository"].count("/") != 1
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


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def head_ref_moved_at(
    head_repository: str,
    head_ref_name: str,
    *,
    expected_head: str,
    run_fn=subprocess.run,
) -> dict[str, Any]:
    """When this PR's head ref last moved, which must have been to `expected_head`.

    The head repository's activity log, filtered to the head ref, lists every push, force-push,
    branch creation or merge that moved it; the latest one put the current head there. Returns
    `{"at", "activity_type", "timestamp"}`, or `{"blocked_reason"}` when the move is unknown: the
    read failed or is malformed, the log records no move of the ref, or its latest move is to
    another commit. An unknown move blocks the merge and is never read as an old one.
    """
    ref = f"refs/heads/{head_ref_name}"
    cmd = [
        "gh",
        "api",
        "-X",
        "GET",
        f"repos/{head_repository}/activity",
        "-f",
        f"ref={ref}",
        "-f",
        "direction=desc",
        "-F",
        "per_page=1",
    ]
    res = run_fn(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"blocked_reason": f"the head ref's activity could not be read ({ref})"}
    try:
        entries = json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        entries = None
    if not isinstance(entries, list):
        return {"blocked_reason": f"the head ref's activity is malformed ({ref})"}
    if not entries:
        return {
            "blocked_reason": (
                f"the activity log records no move of {ref} to {expected_head[:12]}; "
                "a push to the head records one"
            )
        }
    entry = entries[0]
    moved = _timestamp(entry.get("timestamp")) if isinstance(entry, dict) else None
    if (
        not isinstance(entry, dict)
        or moved is None
        or entry.get("ref") != ref
        or not isinstance(entry.get("after"), str)
        or not isinstance(entry.get("activity_type"), str)
    ):
        return {"blocked_reason": f"the head ref's activity is malformed ({ref})"}
    if entry["after"] != expected_head:
        return {
            "blocked_reason": (
                f"{ref} last moved to {entry['after'][:12]} ({entry['activity_type']} at "
                f"{entry['timestamp']}), not to the expected head {expected_head[:12]}"
            )
        }
    return {"at": moved, "activity_type": entry["activity_type"], "timestamp": entry["timestamp"]}


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
    moved = head_ref_moved_at(
        first_facts["head_repository"],
        first_facts["head_ref_name"],
        expected_head=expected_head,
        run_fn=run_fn,
    )
    if "at" not in moved:
        return {"blocked": True, "reason": moved["blocked_reason"]}
    # The head move first, so a tie (a push that also bumps the PR's updatedAt) names the push.
    anchors = {HEAD_REF_MOVED: moved["at"], **first_facts["timestamps"]}
    anchor = max(anchors, key=lambda key: anchors[key])
    review_not_before = anchors[anchor]
    if review_not_before > now:
        return {"blocked": True, "reason": "review-floor timestamp is in the future"}
    age = now - review_not_before
    # The blocking quantity, the anchor that set it, and every candidate anchor, on every answer
    # from here on: "N s remaining" alone cannot say whether this PR or something else reset it.
    floor = {
        "review_floor_remaining_seconds": max(0.0, review_floor_seconds - age),
        "review_age_seconds": age,
        "review_not_before": review_not_before,
        "review_anchor": anchor,
        "review_anchor_at": _iso(review_not_before),
        "review_anchors": {key: _iso(value) for key, value in anchors.items()},
        "head_ref_activity": moved["activity_type"],
    }
    if age < review_floor_seconds:
        return {
            "blocked": True,
            "reason": (
                f"exact-head review floor has {review_floor_seconds - age:.0f}s remaining "
                f"(reset by {anchor} at {floor['review_anchor_at']})"
            ),
            **floor,
        }
    if first_facts["check_state"] != "SUCCESS":
        return {
            "blocked": True,
            "reason": f"exact-head required checks are {first_facts['check_state']!r}",
            **floor,
        }
    return {
        "blocked": False,
        "reason": None,
        "head": expected_head,
        "active_threads": 0,
        "check_state": first_facts["check_state"],
        **floor,
    }
