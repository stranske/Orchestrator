"""The research lane has its own feed, refuses a closed issue at launch, cools down per issue, and
says what it had to choose from.

THE DEFECT (2026-10-12). The lane's only input was `backlog.json`, written by dispatch discovery,
which is retired with the dispatch lane: the file was last written 2026-09-17 with 0 issues, and
every tick printed `no_subject`, which reads the same for "no input" and "every input filtered".
Before that, its subject history held no row from this lane and the cooldown was keyed by arm set,
so Workflows#2710 was launched 123 times. The launch path re-read the issue body but not its state.

THE RULES THIS PINS.
- `backlog.build_research_items` keeps open issues that are prioritised or ready; it drops
  trackers, automated or sync issues, paused or in-progress ones, epic/cross-repo/mechanical ones,
  and anything with an open PR, and it counts each reason. A repo it cannot read contributes
  nothing and is named.
- `tick.load_research_feed` contributes items only when the feed is fresh, and reports every status.
- `remote_tick` plans research from the feed beside the backlog's opener items, and reports both
  inputs and the skip reasons with the research status.
- `research_scheduler` accepts `lane: research` items.
- `tick._fetch_issue_body` returns nothing for an issue that is not open, so the launch is refused.
- `research_subjects.assess_candidate` cools down an issue whatever arm set is proposed."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time

import pytest

import backlog
import feedback
import research_scheduler
import research_subjects
import tick


def _issue(n, *labels, title="t", body="b"):
    return {"number": n, "labels": [{"name": lab} for lab in labels], "title": title, "body": body}


def _feed():
    issues = {
        "o/a": [
            _issue(1, "priority:high"),
            _issue(2, "status: ready"),
            _issue(3, "tracker:durable"),
            _issue(4, "automation", "priority:normal"),
            _issue(5, "agents:auto-pilot-pause", "priority:high"),
            _issue(6, "epic", "priority:high"),
            _issue(7),
            _issue(8, "priority:normal"),
        ],
        "o/b": [_issue(9, "priority:high")],
    }
    prs = {"o/a": [{"number": 50, "body": "Closes #8", "closingIssuesReferences": []}]}

    def fetch_issues(repo):
        return issues[repo]

    def fetch_prs(repo):
        if repo == "o/b":
            raise RuntimeError("HTTP 502")
        return prs.get(repo, [])

    return backlog.build_research_items(["o/a", "o/b"], fetch_issues, fetch_prs)


def test_the_feed_keeps_prioritised_open_unworked_issues_and_counts_the_rest():
    feed = _feed()
    assert [i["target"] for i in feed["items"]] == ["o/a#1", "o/a#2"], feed["items"]
    assert all(i["lane"] == "research" for i in feed["items"])
    assert feed["counts"] == {
        "open_issues": 8,
        "excluded_label": 3,
        "excluded_class": 1,
        "not_prioritised": 1,
        "open_pr": 1,
        "eligible": 2,
    }, feed["counts"]
    assert feed["repos_failed"] == ["o/b"] and feed["repos_read"] == 1, feed


def test_the_lane_accepts_research_items():
    skipped: list = []
    research_scheduler.research_job_candidates(
        [{"target": "o/a#1", "task_type": "implement", "lane": "research"}],
        {"codex": 2, "cursor": 2},
        [],
        skipped=skipped,
    )
    assert not [row for row in skipped if row.get("reason") == "not_opener"], skipped


def test_only_a_fresh_feed_contributes_items(tmp_path):
    path = tmp_path / "research-backlog.json"
    assert tick.load_research_feed(path)["status"] == "absent"
    path.write_text("{not json")
    assert tick.load_research_feed(path)["status"] == "unreadable"
    path.write_text(json.dumps({"generated_at": 1000, "items": [{"target": "o/a#1"}]}))
    stale = tick.load_research_feed(path, now=1000 + tick.RESEARCH_FEED_MAX_AGE_S + 1)
    assert (stale["status"], stale["items"]) == ("stale", []), stale
    fresh = tick.load_research_feed(path, now=1001)
    assert (fresh["status"], len(fresh["items"])) == ("fresh", 1), fresh


def test_the_tick_plans_research_from_the_feed_and_reports_its_inputs():
    seen: list = []

    def research(items, cap, **kwargs):
        seen.extend(items)
        return {"status": "no_subject", "planned": [], "skipped": [{"reason": "subject_cooldown"}]}

    backlog_items = [{"target": "o/a#1", "task_type": "implement", "lane": "opener"}]
    feed = {
        "status": "fresh",
        "age_seconds": 60,
        "items": [
            {"target": "o/a#1", "task_type": "implement", "lane": "research"},
            {"target": "o/a#2", "task_type": "implement", "lane": "research"},
        ],
    }
    out = tick.remote_tick(
        backlog_items,
        {},
        dry_run=True,
        do_ingest=False,
        max_delegations=0,
        research_tick_fn=research,
        research_feed=feed,
    )
    assert [i["target"] for i in seen] == ["o/a#1", "o/a#2"], seen
    inputs = out["research"]["inputs"]
    assert (inputs["backlog_opener"], inputs["feed_status"], inputs["feed_items"]) == (
        1,
        "fresh",
        1,
    ), inputs
    assert inputs["skipped"] == {"subject_cooldown": 1}, inputs
    line = tick.plan_headline(out, "PLAN", lane_live=False)
    assert "research no_subject (feed fresh 1, opener 1; skipped subject_cooldown 1)" in line


@pytest.mark.parametrize(("state", "launchable"), (("OPEN", True), ("CLOSED", False)))
def test_a_closed_issue_is_never_launched(monkeypatch, state, launchable):
    def run(argv, **kwargs):
        assert "state" in argv[-1], argv
        payload = {"title": "T", "body": "B", "state": state}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert (tick._fetch_issue_body("o/a#1") is not None) is launchable


def test_the_cooldown_holds_the_issue_whatever_arms_are_proposed():
    conn = sqlite3.connect(":memory:")
    conn.executescript(feedback.SCHEMA)
    feedback._migrate_schema(conn)
    research_subjects.ensure_schema(conn)
    now = int(time.time())
    base = {
        "target": "o/a#2710",
        "task_type": "implement",
        "spec": "spec",
        "base_sha": "abc",
        "conn": conn,
        "now": now,
        "unevaluated_cap": 99,
    }
    first = research_subjects.assess_candidate(**base, arms=["codex", "cursor"])
    assert first["eligible"], first
    research_subjects.record_subject(first, lifecycle="evaluated", exp_id="e1", conn=conn, now=now)
    for arms in (["gemini", "vibe"], ["codex", "claude", "cursor"]):
        again = research_subjects.assess_candidate(**base, arms=arms)
        assert (again["eligible"], again["reason"]) == (False, "subject_cooldown"), again
    edited = research_subjects.assess_candidate(**{**base, "spec": "edited body"}, arms=["vibe"])
    assert edited["reason"] == "subject_cooldown", "an edited body must not reopen the issue"
    later = research_subjects.assess_candidate(
        **{**base, "now": now + 200 * 3600}, arms=["gemini", "vibe"]
    )
    assert later["eligible"], later
    conn.close()


@pytest.mark.parametrize(
    ("labels", "eligible"),
    (
        (["priority:high"], True),
        (["priority:high", "agents:paused"], False),
        (["priority:high", "agent:codex"], False),
        (["status: ready", "agent:claude"], False),
    ),
)
def test_a_paused_or_owned_issue_is_never_a_research_subject(labels, eligible):
    """Review of #566: the feed admitted `agents:paused` and `agent:*` issues. The dispatcher's
    ownership rule now decides, and these are its own refusal cases plus a positive control."""
    feed = backlog.build_research_items(["o/a"], lambda repo: [_issue(1, *labels)], lambda repo: [])
    assert (feed["counts"]["eligible"] == 1) is eligible, feed["counts"]
    if not eligible:
        assert feed["counts"]["owned_or_paused"] == 1, feed["counts"]


@pytest.mark.parametrize(
    ("labels", "launchable"),
    ((["priority:high"], True), (["agents:paused"], False), (["agent:codex"], False)),
)
def test_a_pause_or_writer_added_since_the_feed_stops_the_launch(monkeypatch, labels, launchable):
    def run(argv, **kwargs):
        assert "labels" in argv[-1], argv
        payload = {
            "title": "T",
            "body": "B",
            "state": "OPEN",
            "labels": [{"name": lab} for lab in labels],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert (tick._fetch_issue_body("o/a#1") is not None) is launchable


@pytest.mark.parametrize(
    ("payload", "status"),
    (
        ("[]", "unreadable"),
        ('"text"', "unreadable"),
        ('{"generated_at": "soon", "items": []}', "unreadable"),
        ('{"items": [{"target": "o/a#1"}]}', "stale"),
        ('{"generated_at": null, "items": [{"target": "o/a#1"}]}', "stale"),
    ),
)
def test_a_malformed_feed_is_reported_never_crashes_the_tick(tmp_path, payload, status):
    path = tmp_path / "research-backlog.json"
    path.write_text(payload)
    feed = tick.load_research_feed(path, now=10_000_000)
    assert (feed["status"], feed["items"]) == (status, []), feed


def test_a_feed_exactly_at_its_horizon_is_fresh_and_drops_malformed_items(tmp_path):
    path = tmp_path / "research-backlog.json"
    items = [{"target": "o/a#1"}, "junk", {"target": ""}, {"task_type": "implement"}]
    path.write_text(json.dumps({"generated_at": 1000, "items": items}))
    feed = tick.load_research_feed(path, now=1000 + tick.RESEARCH_FEED_MAX_AGE_S)
    assert (feed["status"], feed["items"]) == ("fresh", [{"target": "o/a#1"}]), feed


def test_a_row_without_a_number_is_counted_not_fatal_and_a_failed_read_says_why():
    def issues(repo):
        if repo == "o/b":
            raise RuntimeError("HTTP 502")
        return [{"labels": [{"name": "priority:high"}]}, _issue(2, "priority:high")]

    feed = backlog.build_research_items(["o/a", "o/b"], issues, lambda repo: [])
    assert [i["target"] for i in feed["items"]] == ["o/a#2"], feed["items"]
    assert feed["counts"]["malformed"] == 1, feed["counts"]
    assert feed["repo_errors"] == {"o/b": "HTTP 502"}, feed
