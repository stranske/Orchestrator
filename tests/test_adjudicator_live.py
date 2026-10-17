"""The adjudicator on NEW disputes, live, in shadow: certified once, graded on truth (#643)."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

import pytest

import adjudicator_live as live
import feedback
import roles
import switch_review


@pytest.fixture
def world(tmp_path, monkeypatch):
    sha = live._toy_repository(tmp_path)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    now = int(time.time())
    with feedback._conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES ('live-1',?,'owner/repo#1')", (now,)
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
            "VALUES ('live-1','NON_PASS','PASS',1)"
        )
    calls = []

    def runner(**kw):
        calls.append(kw)
        return {
            "backend_run_id": "backend-live",
            "backend": "stub",
            "proposal": {"decision": "uphold_blocker", "rationale": "x"},
            "advisory_plan": {"decision": "uphold_blocker"},
            "errors": [],
        }

    @contextmanager
    def provider(target, head):
        assert (target, head) == ("owner/repo#1", sha)
        yield str(tmp_path / "repo")

    snapshot = {"number": 1, "headRefOid": sha, "body": live.AC_BODY}
    common = dict(
        db=feedback.DB_PATH,
        path=tmp_path / "live.json",
        now=now + 1,
        env={},
        evidence_reader=lambda _row: live._evidence_for(sha),
        runner=runner,
        provider=provider,
        snapshot_reader=lambda _t: snapshot,
    )
    return {
        "sha": sha,
        "now": now,
        "calls": calls,
        "common": common,
        "path": tmp_path / "live.json",
    }


def test_a_new_dispute_is_certified_and_advised_once_without_touching_outcomes(world):
    first = live.run_live(**world["common"])
    assert (first["attempted"], first["advised"], first["uncertified"]) == (1, 1, 0)
    row = first["rows"][0]
    assert row["certification"]["status"] == "certified" and row["source"] == "live"
    assert row["decision"] == "uphold_blocker" and row["disposition"] == "uphold_blocker"
    call = world["calls"][0]
    assert call["source"] == "live" and isinstance(call["case"], roles.CollectedAdjudicationCase)
    second = live.run_live(**world["common"])
    assert second["attempted"] == 0 and len(world["calls"]) == 1
    with sqlite3.connect(feedback.DB_PATH) as conn:
        assert conn.execute(
            "SELECT verifier_verdict,adjudicated_verdict FROM outcomes"
        ).fetchall() == [("NON_PASS", "PASS")]
    report = json.loads(world["path"].read_text())
    assert report["summary"]["cases"] == 1 and report["population"] == 1


def test_later_truth_grades_the_live_advice(world):
    live.run_live(**world["common"])
    checked = feedback.DURABILITY_DETECTION_SINCE + 10
    with feedback._conn() as conn:
        conn.execute(
            "UPDATE outcomes SET durability='broke_later', durability_checked_ts=? WHERE run_id='live-1'",
            (checked,),
        )
    graded = live.run_live(**world["common"])
    # uphold_blocker against a delivery that broke later is agreement.
    assert graded["summary"]["graded"] == 1 and graded["summary"]["agree"] == 1
    line = live.weekly_line(world["path"])
    assert (
        line
        == "adjudicator live: disputes 1, advised 1, uncertified 0, graded 1 (agree 1, disagree 0)"
    )


def test_a_body_without_criteria_is_uncertified_and_never_dispatched(world):
    common = {
        **world["common"],
        "snapshot_reader": lambda _t: {
            "number": 1,
            "headRefOid": world["sha"],
            "body": "no criteria",
        },
    }
    result = live.run_live(**common)
    assert (result["attempted"], result["advised"], result["uncertified"]) == (1, 0, 1)
    summary = json.loads(world["path"].read_text())["summary"]
    assert summary["uncertified_reasons"] == {"no_criteria": 1}
    assert "uncertified 1 (no_criteria 1)" in live.weekly_line(world["path"])
    assert live.uncertified_class("qualified prompt exceeds byte limit; x") == "prompt_too_large"
    assert live.uncertified_class("something new") == "other"
    assert world["calls"] == []
    assert "explicit acceptance criteria" in result["rows"][0]["certification"]["reason"]


def test_the_kill_switch_and_the_limit_hold(world):
    off = live.run_live(**{**world["common"], "env": {live.LIVE_SWITCH: "off"}})
    assert off["disabled"] and not world["path"].exists()
    none = live.run_live(**{**world["common"], "limit": 0})
    assert none["attempted"] == 0 and world["calls"] == []
    assert live.configured_limit({live.LIVE_LIMIT: "7"}) == 7
    assert live.configured_limit({live.LIVE_LIMIT: "x"}) == live.DEFAULT_LIMIT


def test_weekly_review_prints_the_live_line(world, monkeypatch):
    live.run_live(**world["common"])
    monkeypatch.setattr(live, "report_path", lambda: world["path"])
    rendered = switch_review.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "mirror_drift": {"status": "ok"},
            "adjudicator_live": live.weekly_line(),
        }
    )
    assert "adjudicator live: disputes 1, advised 1, uncertified 0, graded 0" in rendered
    # Deliberate break, in place: without the case-id dedup the same dispute is advised again.
    world["path"].write_text(json.dumps({"rows": [], "summary": {}, "population": 1}))
    again = live.run_live(**world["common"])
    assert again["attempted"] == 1 and len(world["calls"]) == 2
