"""Cached panels must preserve author exclusion and release follow-up locks on timeout."""

import subprocess

import pytest

import adversarial
import feedback
import merge_guard

HEAD = "a" * 40


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))


@pytest.mark.parametrize("verdict", ["PASS", "BLOCKED"])
def test_automatic_cached_author_is_rejudged(brain, verdict):
    def judge(agent, exclude):
        return adversarial.review_at_head(
            "o/r#7",
            HEAD,
            reviewers=["auto"],
            exclude=exclude,
            worktree="/fake",
            head_fn=lambda _: HEAD,
            choose_fn=lambda _: {"agent": agent},
            review_fn=lambda *a: {"verdict": verdict, "blockers": []},
        )

    first = judge("codex", set())
    assert first["status"] == "executed"
    second = judge("claude", {"codex"})
    assert second["status"] == "executed"
    assert second["result"]["reviewed_by"] == ["claude"]
    # Same-second judgments have hash-based IDs, not chronologically sortable IDs.
    with feedback._conn() as conn:
        rows = conn.execute("SELECT rowid FROM completion_events ORDER BY rowid").fetchall()
        assert len(rows) == 2
        conn.execute("UPDATE completion_events SET updated_ts=1")
        conn.execute("UPDATE completion_events SET event_id='z-old' WHERE rowid=?", rows[0])
        conn.execute("UPDATE completion_events SET event_id='a-new' WHERE rowid=?", rows[1])
    assert judge("unused", {"codex"})["status"] == "reused"


@pytest.mark.parametrize("result", [None, {}, {"reviewed_by": ["codex"]}])
def test_guard_refuses_unproven_or_author_cached_panel(monkeypatch, result):
    monkeypatch.setattr(
        adversarial,
        "recorded_verdict",
        lambda *a: {"state": "found", "verdict": "PASS", "result": result},
    )
    out = merge_guard.adversarial_review_status(
        "o/r#7",
        {"labels": ["risk:major", "agent:codex"], "title": "auth", "closing_issues": []},
        head=HEAD,
        env={},
    )
    assert out["status"] == "deferred_to_post_merge"
    assert out["exclude_agents"] == ["codex"]


def test_post_merge_lookup_timeout_is_bounded(monkeypatch):
    def timeout(cmd, **kwargs):
        assert 0 < kwargs["timeout"] <= 60
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    out = adversarial.spawn_post_merge_panel("o/r#7", HEAD, "review", {"codex"})
    assert out["status"] == "spawn_failed"
    assert "timed out" in out["error"]


def test_git_fetch_has_bounded_timeout(monkeypatch):
    def run(cmd, **kwargs):
        assert 0 < kwargs["timeout"] <= 60
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert adversarial._git("/fake", "fetch", "origin", HEAD).returncode == 0


def test_followup_timeout_releases_lock_for_retry(brain, monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        assert 0 < kwargs["timeout"] <= 60
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    outcome = {"verdict": "BLOCKED", "reviewers": ["auto"], "result": {"blockers": []}}
    for _ in range(2):
        out = adversarial.file_followup("o/r#7", HEAD, outcome)
        assert out["filed"] is False and "timed out" in out["error"]
    assert len(calls) == 2
