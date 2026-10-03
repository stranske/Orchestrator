from __future__ import annotations

import json
import subprocess
from typing import Any

import exact_head_merge_gate as gate

HEAD = "a" * 40
NOW = 2_000.0


def _page(
    *,
    threads: list[dict[str, Any]] | None = None,
    has_next: Any = False,
    cursor: str | None = None,
    head: str = HEAD,
    pr_created_at: str = "1970-01-01T00:01:40Z",
    pr_updated_at: str = "1970-01-01T00:03:20Z",
    timeline_updated_at: str = "1970-01-01T00:08:20Z",
    repository_pushed_at: str | None = "1970-01-01T00:16:40Z",
    check_state: str = "SUCCESS",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    total_count: int | None = None,
) -> dict[str, Any]:
    thread_nodes = threads or []
    for index, thread in enumerate(thread_nodes):
        thread.setdefault("id", f"thread-{index}")
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "id": "PR_5",
                    "state": "OPEN",
                    "isDraft": False,
                    "headRefOid": head,
                    "mergeable": mergeable,
                    "mergeStateStatus": merge_state_status,
                    "createdAt": pr_created_at,
                    "updatedAt": pr_updated_at,
                    "headRepository": {"id": "R_head", "pushedAt": repository_pushed_at},
                    "headRef": {"target": {"oid": head}},
                    "timelineItems": {"updatedAt": timeline_updated_at},
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


def _runner(*pages: dict[str, Any]):
    remaining = list(pages)

    def run(cmd, capture_output=True, text=True):
        assert cmd[:3] == ["gh", "api", "graphql"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(remaining.pop(0)), stderr="")

    return run


def test_incident_shape_blocks_active_review_thread() -> None:
    result = gate.snapshot(
        "o/r#391",
        expected_head=HEAD,
        run_fn=_runner(_page(threads=[{"isResolved": False, "isOutdated": False}])),
        now_fn=lambda: NOW,
    )

    assert result["blocked"] is True
    assert result["active_threads"] == 1


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
        timeline_updated_at="1970-01-01T00:25:00Z",
        repository_pushed_at="1970-01-01T00:30:00Z",
    )
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(page),
        now_fn=lambda: 2_220.0,
    )

    assert result["blocked"] is False
    assert result["review_not_before"] == 1_800.0


def test_missing_supported_timestamp_fails_closed_without_pushed_date() -> None:
    result = gate.snapshot(
        "o/r#5",
        expected_head=HEAD,
        run_fn=_runner(_page(repository_pushed_at=None)),
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
