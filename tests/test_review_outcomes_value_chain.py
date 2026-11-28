"""The `review-outcomes` value-chain row, read from a real Brain schema (2026-11-28).

Acted on means a lane-judge pick drawn from the learned `review` weights after they first carried
grades (roles.ROLE_WEIGHTS_FROM_ROUTE_AS; the judge run records route_decision.learned_bucket).
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import capabilities
import feedback
import review_outcomes as ro
import value_chain_monitor as monitor

RELEARN = ro.PASS_SINCE + 3600
NOW = RELEARN + 7200


@pytest.fixture
def brain(monkeypatch, tmp_path):
    db_path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CLOSER_CORE_STATE", str(tmp_path / "closer"))
    monkeypatch.setenv("OPENER_CORE_STATE", str(tmp_path / "opener"))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    with feedback._conn():
        pass
    ledger = tmp_path / "capabilities.json"
    capabilities.load(ledger)
    return ledger, db_path


def _judge_run(
    db_path,
    run_id,
    ts,
    *,
    lane="closer",
    decision="close",
    verdict=None,
    bucket=None,
    n_obs=0,
    producer=None,
):
    state = ro.closer_state() if lane == "closer" else ro.opener_state()
    meta = (
        json.dumps(
            {
                "route_decision": {
                    "chosen": "codex",
                    "learned_bucket": bucket,
                    "learned_n_obs": n_obs,
                }
            }
        )
        if bucket
        else None
    )
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, mode, routing_metadata) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, ts, ro._caller(state), "review", "codex", "local", meta),
        )
        if decision:
            con.execute(
                "INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    producer or (ro.PRODUCER if lane == "closer" else ro.OPENER),
                    "o/r#1",
                    "o/r#2",
                    decision,
                    ts,
                    ts,
                ),
            )
        if verdict is not None:
            prefix = ro.NOTE_PREFIX if lane == "closer" else ro.OPENER_NOTE_PREFIX
            con.execute(
                "INSERT INTO outcomes (run_id, adjudicated_verdict, notes) VALUES (?,?,?)",
                (run_id, verdict or None, prefix + "fixture"),
            )


def _relearn(db_path, ts=RELEARN):
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO route_weights (version, ts, task_type, agent, prior, posterior, n_obs) "
            "VALUES (75, ?, 'review', 'codex', 0.5, 0.9, 433)",
            (ts,),
        )


def _row(ledger, db_path):
    inputs = monitor._brain(db_path, NOW - monitor.WINDOW_DAYS * 86400, NOW)
    section = monitor.report(now=NOW, path=ledger, env={}, inputs=inputs)
    return next(r for r in section["rows"] if r["capability"] == "review-outcomes"), section


def test_the_row_exists_and_reads_not_acted_on_before_any_graded_pick(brain):
    ledger, db_path = brain
    _judge_run(db_path, "r1", RELEARN - 10, verdict="PASS", bucket="role:adjudicator")
    row, section = _row(ledger, db_path)
    assert (row["situation_count"], row["invocation_count"], row["usable"]) == (1, 1, 1)
    assert row["acted_on"] == 0 and row["first_break"] == "not_acted_on"
    rendered = "\n".join(monitor.format_lines(section))
    assert "review-outcomes: demand 1; invocation 1; usable 1; acted_on 0" in rendered


def test_judge_picks_from_graded_weights_are_acted_on_and_graded(brain):
    ledger, db_path = brain
    _relearn(db_path)
    _judge_run(db_path, "before", RELEARN - 10, verdict="PASS", bucket="review", n_obs=433)
    _judge_run(db_path, "pass", RELEARN + 10, verdict="PASS", bucket="review", n_obs=433)
    _judge_run(
        db_path,
        "fail",
        RELEARN + 20,
        lane="opener",
        decision="start",
        verdict="FAIL",
        bucket="review",
        n_obs=121,
    )
    _judge_run(db_path, "wait", RELEARN + 30, bucket="review", n_obs=433)
    _judge_run(db_path, "norule", RELEARN + 40, decision="repair", bucket="review", n_obs=433)
    _judge_run(db_path, "noanswer", RELEARN + 50, decision=None, bucket="review", n_obs=433)
    _judge_run(db_path, "unobserved", RELEARN + 60, bucket="review", n_obs=0)
    _judge_run(db_path, "excluded", RELEARN + 70, verdict="", bucket="review", n_obs=433)
    row, section = _row(ledger, db_path)
    assert (row["situation_count"], row["invocation_count"], row["usable"]) == (8, 7, 3)
    assert (row["acted_on"], row["outcome"], row["outcome_success"]) == (6, 2, 1)
    assert (row["outcome_awaiting_sweep"], row["outcome_ungraded"], row["outcome_excluded"]) == (
        1,
        2,
        1,
    )
    assert row["first_break"] == "works"
    rendered = "\n".join(monitor.format_lines(section))
    assert "+2 ungraded" in rendered and "`ungraded`:" in rendered
    with sqlite3.connect(db_path) as con:
        acted = ro.acted_pass(con, NOW)
    assert (acted["value"], acted["since"], acted["passed"]) == (6, RELEARN, False)


def test_the_pass_number_waits_for_the_first_relearn_after_grading(brain):
    _, db_path = brain
    _relearn(db_path, ts=ro.PASS_SINCE - 1)  # a relearn before any grade existed carries none
    _judge_run(db_path, "pick", RELEARN + 10, verdict="PASS", bucket="review", n_obs=433)
    with sqlite3.connect(db_path) as con:
        acted = ro.acted_pass(con, NOW)
        assert ro.value_chain(con, 0, NOW)["acted_on"] == 0
    assert (acted["value"], acted["since"]) == (0, None)
    text = ro.format_probe({**ro.probe(), "acted_pass": acted})
    assert (
        "judge picks from graded weights 0 / 20 (not yet; awaiting the first weekly relearn" in text
    )


def test_an_unreadable_brain_is_unmeasured_not_zero(brain, tmp_path):
    ledger, _ = brain
    row, _ = _row(ledger, tmp_path / "missing" / "brain.db")
    assert row["situation_count"] is None and row["acted_on"] is None
    assert (
        monitor.acted_on_index(tmp_path / "missing" / "brain.db", NOW, ["review-outcomes"])[0]
        is None
    )
