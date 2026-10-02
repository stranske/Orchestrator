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
    has_next: bool = False,
    cursor: str | None = None,
    head: str = HEAD,
    pushed_at: str = "1970-01-01T00:16:40Z",
    check_state: str = "SUCCESS",
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": "OPEN",
                    "isDraft": False,
                    "headRefOid": head,
                    "mergeStateStatus": "CLEAN",
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": head,
                                    "pushedDate": pushed_at,
                                    "statusCheckRollup": {"state": check_state},
                                }
                            }
                        ]
                    },
                    "reviewThreads": {
                        "nodes": threads or [],
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
            _page(has_next=True, cursor="page-2"),
            _page(threads=[{"isResolved": False, "isOutdated": False}]),
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
