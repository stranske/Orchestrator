"""Hermetic coverage for tick's pure target, feed, and plan helpers."""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

import dispatcher
import gh_capacity
import tick


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """These helper tests may only use private state and in-process work."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("helper coverage attempted external process, provider, or network work")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(gh_capacity, "gh_run", forbidden)
    monkeypatch.setattr(dispatcher, "delegate", forbidden)
    monkeypatch.setattr(dispatcher, "offload", forbidden)
    monkeypatch.setattr(tick.exp_abcd, "EXP_DIR", state / "experiments")
    yield
    assert calls == []


def test_target_helpers_reject_malformed_targets_and_split_valid_targets() -> None:
    assert tick._target_repo("") is None
    assert tick._target_number("owner/repo") is None
    assert tick._target_repo("owner/repo#17") == "owner/repo"
    assert tick._target_number("owner/repo#17") == "17"
    assert tick._target_repo("owner/repo#17#nested") == "owner/repo"
    assert tick._target_number("owner/repo#17#nested") == "nested"


def test_research_feed_reports_each_invalid_and_fresh_state(tmp_path) -> None:
    feed = tmp_path / "research.json"
    now = 10_000.0
    assert tick.load_research_feed(feed, now=now) == {"status": "absent", "items": []}

    feed.write_text("not json")
    unreadable = tick.load_research_feed(feed, now=now)
    assert unreadable["status"] == "unreadable"

    feed.write_text(json.dumps(["not an object"]))
    assert tick.load_research_feed(feed, now=now)["error"] == "feed is not an object"

    feed.write_text(
        json.dumps({"generated_at": now - tick.RESEARCH_FEED_MAX_AGE_S - 1, "items": []})
    )
    assert tick.load_research_feed(feed, now=now)["status"] == "stale"

    feed.write_text(
        json.dumps(
            {
                "generated_at": now - 2,
                "counts": {"selected": 2},
                "repos_failed": ["owner/failed"],
                "items": [{"target": "owner/repo#1"}, "bad", {}],
            }
        )
    )
    assert tick.load_research_feed(feed, now=now) == {
        "status": "fresh",
        "age_seconds": 2,
        "counts": {"selected": 2},
        "repos_failed": ["owner/failed"],
        "items": [{"target": "owner/repo#1"}],
    }


def test_public_plan_metadata_and_order_helpers_preserve_only_safe_fields() -> None:
    assert tick._public_research_plan(
        {"candidates": [{"item": {"body": "large"}, "target": "a#1"}], "planned": [{}]}
    ) == {"candidates": [{"target": "a#1"}], "planned": [{}]}
    assert tick._research_claim_metadata(
        {
            "launched": [
                {"pid": "12", "log": "one.log", "worktree": "one"},
                {"pid": "invalid"},
                "invalid",
            ],
            "exp_id": 42,
        }
    ) == {
        "pids": [12],
        "logs": ["one.log"],
        "worktrees": ["one"],
        "exp_id": "42",
        "experiment_dir": str(tick.exp_abcd.exp_paths("42")),
    }
    assert tick.delegation_bounds({}, max_delegations=2) == (2, 8)
    assert tick.is_delegation({"skip": "owned"}) is False
    assert tick.is_delegation({"error": "failed"}) is False
    assert tick.is_delegation({"labels_read": False}) is True
    ordered = tick.examination_order(
        [{"target": "owned", "labels": ["agent:codex"]}, {"target": "free", "labels": []}]
    )
    assert [(row["target"], delegable) for row, delegable in ordered] == [
        ("free", True),
        ("owned", False),
    ]
