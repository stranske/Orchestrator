from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any

import pytest

import exact_head_merge_gate as gate

HEAD = "a" * 40
NOW = 2_000.0


def _page(
    *,
    threads: list[dict[str, Any]] | None = None,
    has_next: Any = False,
    cursor: str | None = None,
    head: str = HEAD,
    head_ref_name: str = "feature",
    head_repository: str = "o/r",
    pr_created_at: str | None = "1970-01-01T00:01:40Z",
    pr_updated_at: str | None = "1970-01-01T00:03:20Z",
    ready_for_review_at: str | None = None,
    repository_pushed_at: str | None = None,
    cross_referenced_at: str | None = None,
    check_state: str = "SUCCESS",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    total_count: int | None = None,
) -> dict[str, Any]:
    thread_nodes = threads or []
    for index, thread in enumerate(thread_nodes):
        thread.setdefault("id", f"thread-{index}")
    head_repo: dict[str, Any] = {"id": "R_head", "nameWithOwner": head_repository}
    if repository_pushed_at is not None:
        # The query no longer asks for it. A page that carries it anyway is the repository's last
        # push to ANY branch, which the floor must ignore.
        head_repo["pushedAt"] = repository_pushed_at
    ready_nodes = [] if ready_for_review_at is None else [{"createdAt": ready_for_review_at}]
    pull_request: dict[str, Any] = {
        "id": "PR_5",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": head,
        "headRefName": head_ref_name,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state_status,
        "createdAt": pr_created_at,
        "updatedAt": pr_updated_at,
        "headRepository": head_repo,
        "headRef": {"target": {"oid": head}},
        "readyForReview": {"nodes": ready_nodes},
    }
    if cross_referenced_at is not None:
        # Nor this: the timeline connection's updatedAt, which moves when ANOTHER PR or issue
        # references this one.
        pull_request["timelineItems"] = {"updatedAt": cross_referenced_at}
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    **pull_request,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": head,
                                    "statusCheckRollup": {"state": check_state},
                                }
                            }
                        ]
                    },
                    "reviewThreads": {
                        "totalCount": len(thread_nodes) if total_count is None else total_count,
                        "nodes": thread_nodes,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    },
                }
            }
        }
    }


def _activity(
    *,
    after: str = HEAD,
    timestamp: str = "1970-01-01T00:16:40Z",
    activity_type: str = "push",
    ref: str = "refs/heads/feature",
) -> list[dict[str, Any]]:
    """One entry of GET /repos/{owner}/{repo}/activity, as GitHub lists it."""
    return [
        {
            "id": 1,
            "node_id": "RA_1",
            "before": "0" * 40,
            "after": after,
            "ref": ref,
            "timestamp": timestamp,
            "activity_type": activity_type,
            "actor": {"login": "stranske"},
        }
    ]


def _runner(
    *pages: dict[str, Any],
    activity: Any = None,
    activity_rc: int = 0,
    activity_stdout: str | None = None,
    calls: list[list[str]] | None = None,
):
    remaining = list(pages)
    entries = _activity() if activity is None else activity

    def run(cmd, capture_output=True, text=True):
        if calls is not None:
            calls.append(cmd)
        if cmd[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(remaining.pop(0)), stderr=""
            )
        assert cmd[:4] == ["gh", "api", "-X", "GET"] and cmd[4].endswith("/activity"), cmd
        stdout = json.dumps(entries) if activity_stdout is None else activity_stdout
        return subprocess.CompletedProcess(cmd, activity_rc, stdout=stdout, stderr="")

    return run


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def test_incident_shape_blocks_active_review_thread() -> None:
    calls: list[list[str]] = []
    result = gate.snapshot(
        "o/r#391",
        expected_head=HEAD,
        run_fn=_runner(_page(threads=[{"isResolved": False, "isOutdated": False}]), calls=calls),
        now_fn=lambda: NOW,
    )

    assert result["blocked"] is True
    assert result["active_threads"] == 1
    # Blocked before the floor, so the head ref's activity is never read.
    assert [cmd[:3] for cmd in calls] == [["gh", "api", "graphql"]]


def test_reads_every_page_before_allowing_merge() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(
            _page(has_next=True, cursor="page-2", total_count=1),
            _page(
                threads=[{"isResolved": False, "isOutdated": False}],
                total_count=1,
            ),
        ),
        now_fn=lambda: NOW,
    )

    assert result["blocked"] is True
    assert result["active_threads"] == 1


def test_partial_graphql_response_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner({**_page(), "errors": [{"message": "timeout"}]}),
        now_fn=lambda: NOW,
    )

    assert result == {"blocked": True, "reason": "review-thread state is unknown or malformed"}


def test_incomplete_pagination_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(has_next=True, cursor=None)),
        now_fn=lambda: NOW,
    )

    assert result == {"blocked": True, "reason": "review-thread pagination is incomplete"}


def test_non_boolean_pagination_flag_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(has_next="false")),
        now_fn=lambda: NOW,
    )

    assert result == {"blocked": True, "reason": "review-thread pagination is malformed"}


def test_merge_fact_drift_across_pages_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(
            _page(has_next=True, cursor="page-2", check_state="SUCCESS"),
            _page(check_state="FAILURE"),
        ),
        now_fn=lambda: NOW,
    )

    assert result == {
        "blocked": True,
        "reason": "merge facts changed during review-thread pagination",
    }


def test_review_floor_blocks_at_419_seconds_and_passes_at_420() -> None:
    page = _page()
    blocked = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(page),
        now_fn=lambda: 1_419.0,
    )
    allowed = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(page),
        now_fn=lambda: 1_420.0,
    )

    assert blocked["blocked"] is True
    assert "1s remaining" in blocked["reason"]
    assert allowed["blocked"] is False


def test_review_floor_uses_latest_supported_timestamp() -> None:
    page = _page(
        pr_created_at="1970-01-01T00:10:00Z",
        pr_updated_at="1970-01-01T00:20:00Z",
        ready_for_review_at="1970-01-01T00:25:00Z",
    )
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(page, activity=_activity(timestamp="1970-01-01T00:30:00Z")),
        now_fn=lambda: 2_220.0,
    )

    assert result["blocked"] is False
    assert result["review_not_before"] == 1_800.0
    assert result["review_anchor"] == gate.HEAD_REF_MOVED


def test_unrelated_branch_push_does_not_reset_the_floor() -> None:
    """stranske/Orchestrator#439, 2026-10-04: its head ref was created at 19:33:55Z and never moved
    again; the PR last changed at 19:40:32Z. Other sessions pushed OTHER branches at 19:43:27,
    19:44:36, 19:45:12, 19:51:32 and 19:55:24Z, and the repository-wide anchor read each of them as
    this PR's: the attempt that measured 189s (19:48:21Z, after the 19:45:12Z push) was blocked
    with 231s left. Its own floor had passed at 19:47:32Z."""
    page = _page(
        pr_created_at="2026-10-04T19:34:35Z",
        pr_updated_at="2026-10-04T19:40:32Z",
        repository_pushed_at="2026-10-04T19:45:12Z",
    )
    result = gate.snapshot(
        "stranske/Orchestrator#439",
        expected_head=HEAD,
        run_fn=_runner(
            page,
            activity=_activity(timestamp="2026-10-04T19:33:55Z", activity_type="branch_creation"),
        ),
        now_fn=lambda: _epoch("2026-10-04T19:48:21Z"),
    )

    assert result["blocked"] is False, result
    assert result["review_anchor"] == "pr_updated_at"
    assert result["review_anchor_at"] == "2026-10-04T19:40:32Z"
    assert result["review_age_seconds"] == 469.0
    assert "2026-10-04T19:45:12Z" not in result["review_anchors"].values()


def test_a_cross_reference_from_another_pr_does_not_reset_the_floor() -> None:
    """2026-10-04 22:14:44Z: PR #447's body cited #411, #439 and #404, and that moved each one's
    timeline-connection updatedAt while their own updatedAt stayed put (#411's at 14:28:38Z). Read
    as an anchor, every PR that cites another re-arms its floor for 420 seconds."""
    page = _page(
        pr_created_at="2026-10-04T13:00:00Z",
        pr_updated_at="2026-10-04T14:28:38Z",
        cross_referenced_at="2026-10-04T22:14:44Z",
    )
    result = gate.snapshot(
        "stranske/Orchestrator#411",
        expected_head=HEAD,
        run_fn=_runner(page, activity=_activity(timestamp="2026-10-04T13:05:00Z")),
        now_fn=lambda: _epoch("2026-10-04T22:16:00Z"),
    )

    assert result["blocked"] is False, result
    assert result["review_anchor"] == "pr_updated_at"
    assert "2026-10-04T22:14:44Z" not in result["review_anchors"].values()


def test_marking_the_pr_ready_for_review_resets_the_floor() -> None:
    """A draft is first reviewable when it is marked ready, so that event holds the floor itself,
    whether or not GitHub also moves the PR's updatedAt for it."""
    page = _page(
        pr_created_at="2026-10-04T19:20:00Z",
        pr_updated_at="2026-10-04T19:30:00Z",
        ready_for_review_at="2026-10-04T19:45:00Z",
    )
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(page, activity=_activity(timestamp="2026-10-04T19:25:00Z")),
        now_fn=lambda: _epoch("2026-10-04T19:48:21Z"),
    )

    assert result["blocked"] is True
    assert result["reason"] == (
        "exact-head review floor has 219s remaining "
        "(reset by ready_for_review_at at 2026-10-04T19:45:00Z)"
    )


@pytest.mark.parametrize(
    "ready",
    [None, {"nodes": None}, {"nodes": [{}]}, {"nodes": [{"createdAt": "x"}, {"createdAt": "y"}]}],
    ids=["no-connection", "no-nodes", "no-time", "two-events"],
)
def test_malformed_ready_for_review_evidence_fails_closed(ready: Any) -> None:
    page = _page()
    page["data"]["repository"]["pullRequest"]["readyForReview"] = ready
    result = gate.snapshot("o/r#5", expected_head=HEAD, run_fn=_runner(page), now_fn=lambda: NOW)

    assert result == {"blocked": True, "reason": "review-thread state is unknown or malformed"}


def test_a_push_to_this_prs_own_head_resets_the_floor() -> None:
    """The head move alone holds the floor: it does not rely on GitHub also bumping updatedAt."""
    page = _page(
        pr_created_at="2026-10-04T19:34:35Z",
        pr_updated_at="2026-10-04T19:40:32Z",
    )
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(page, activity=_activity(timestamp="2026-10-04T19:46:41Z")),
        now_fn=lambda: _epoch("2026-10-04T19:48:21Z"),
    )

    assert result["blocked"] is True
    assert result["reason"] == (
        "exact-head review floor has 320s remaining "
        "(reset by head_ref_moved_at at 2026-10-04T19:46:41Z)"
    )
    assert result["review_floor_remaining_seconds"] == 320.0
    assert result["review_anchor"] == gate.HEAD_REF_MOVED
    assert result["head_ref_activity"] == "push"


def test_drained_floor_reports_zero_remaining_beside_its_anchor() -> None:
    """What the gate says at zero: a pass that names the anchor and reports 0 seconds remaining."""
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page()),
        now_fn=lambda: 1_000.0 + gate.REVIEW_FLOOR_SECONDS,
    )

    assert result["blocked"] is False and result["reason"] is None
    assert result["review_floor_remaining_seconds"] == 0.0
    assert result["review_age_seconds"] == float(gate.REVIEW_FLOOR_SECONDS)
    assert result["review_anchor"] == gate.HEAD_REF_MOVED
    # A PR that was never a draft has no ready-for-review anchor: an answer, not an unknown.
    assert set(result["review_anchors"]) == {gate.HEAD_REF_MOVED, "pr_created_at", "pr_updated_at"}


def test_head_ref_activity_is_read_for_the_head_repository_and_ref() -> None:
    calls: list[list[str]] = []
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(
            _page(head_repository="fork-owner/r", head_ref_name="claude/fix-floor"),
            activity=_activity(ref="refs/heads/claude/fix-floor"),
            calls=calls,
        ),
        now_fn=lambda: NOW,
    )

    assert result["blocked"] is False, result
    activity_calls = [cmd for cmd in calls if cmd[:3] != ["gh", "api", "graphql"]]
    assert len(activity_calls) == 1
    cmd = activity_calls[0]
    assert cmd[4] == "repos/fork-owner/r/activity"
    assert "ref=refs/heads/claude/fix-floor" in cmd
    assert "per_page=1" in cmd and "direction=desc" in cmd


def test_head_ref_moved_to_another_commit_blocks() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(), activity=_activity(after="b" * 40, activity_type="force_push")),
        now_fn=lambda: NOW,
    )

    assert result["blocked"] is True
    assert result["reason"] == (
        "refs/heads/feature last moved to bbbbbbbbbbbb (force_push at 1970-01-01T00:16:40Z), "
        "not to the expected head aaaaaaaaaaaa"
    )


def test_no_recorded_head_ref_move_blocks_and_names_its_drain() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(), activity=[]),
        now_fn=lambda: NOW,
    )

    assert result == {
        "blocked": True,
        "reason": (
            "the activity log records no move of refs/heads/feature to aaaaaaaaaaaa; "
            "a push to the head records one"
        ),
    }


def test_unreadable_head_ref_activity_blocks() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(), activity_rc=1, activity_stdout=""),
        now_fn=lambda: NOW,
    )

    assert result == {
        "blocked": True,
        "reason": "the head ref's activity could not be read (refs/heads/feature)",
    }


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        json.dumps({"message": "Not Found"}),
        json.dumps(_activity(ref="refs/heads/main")),
        json.dumps(_activity(timestamp="")),
        json.dumps([{**_activity()[0], "after": None}]),
        json.dumps(["entry"]),
    ],
    ids=["not-json", "not-a-list", "another-ref", "no-timestamp", "no-after", "not-an-object"],
)
def test_malformed_head_ref_activity_blocks(stdout: str) -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(), activity_stdout=stdout),
        now_fn=lambda: NOW,
    )

    assert result == {
        "blocked": True,
        "reason": "the head ref's activity is malformed (refs/heads/feature)",
    }


def test_missing_pr_timestamp_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(pr_updated_at=None)),
        now_fn=lambda: NOW,
    )

    assert result == {"blocked": True, "reason": "review-thread state is unknown or malformed"}


def test_missing_head_ref_name_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(head_ref_name="")),
        now_fn=lambda: NOW,
    )

    assert result == {"blocked": True, "reason": "review-thread state is unknown or malformed"}


def test_non_success_exact_head_checks_fail_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(check_state="FAILURE")),
        now_fn=lambda: NOW,
    )

    assert result["blocked"] is True
    assert "FAILURE" in result["reason"]


def test_expected_head_drift_fails_closed() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(head="b" * 40)),
        now_fn=lambda: NOW,
    )

    assert result == {"blocked": True, "reason": "observed head changed"}
