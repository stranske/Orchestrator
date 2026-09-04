"""GraphQL-backed review-thread counting for switch_review fleet gates."""

from __future__ import annotations

import json

import switch_review


def test_unresolved_review_threads_counts_graphql_nodes():
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": True},
                            {"isResolved": True},
                            {"isResolved": False},
                        ]
                    }
                }
            }
        }
    }

    def fake_gh(args, *, timeout_s=30):
        assert args[:2] == ["api", "graphql"]
        return True, json.dumps(payload), ""

    unresolved, reason = switch_review._unresolved_review_threads(
        "stranske/Example", 42, gh_fn=fake_gh
    )
    assert unresolved == 1
    assert reason == ""


def test_unresolved_review_threads_failure_is_unmeasured():
    def fail_gh(args, *, timeout_s=30):
        return False, "", "unmeasured: auth required"

    unresolved, reason = switch_review._unresolved_review_threads(
        "stranske/Example", 42, gh_fn=fail_gh
    )
    assert unresolved is None
    assert reason.startswith("unmeasured: ")
