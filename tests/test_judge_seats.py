"""Judge seats from the graded record (owner, 2026-11-28).

Cursor joined the adjudicator role on probation: its own lane-judge record makes it last choice,
then removes it, when it judges worse than its peers. Muse earns a review rung from its graded
lane-judge record and, until then, gets a weekly floor of low-stakes judge calls.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

import feedback
import muse_ladder
import review_outcomes as ro
import roles
import router

NOW = ro.PASS_SINCE + 30 * 86400
DAY = 86400


def rec(runs=0, usable=0, graded=0, fail=0, runs_7d=0, fail_ts=None):
    return {
        "runs": runs,
        "runs_7d": runs_7d,
        "usable": usable,
        "graded": graded,
        "fail": fail,
        "fail_ts": list(fail_ts or []),
    }


PEERS = {
    "codex": rec(200, 160, 100, 5),
    "gemini": rec(100, 80, 60, 3),
    "claude": rec(40, 35, 20, 0),
}


# ---- cursor's probation ---------------------------------------------------------------------------
def test_cursor_starts_eligible_and_stays_so_on_a_record_like_its_peers():
    assert ro.cursor_seat({**PEERS}, NOW)["state"] == "eligible"
    seat = ro.cursor_seat({**PEERS, "cursor": rec(30, 25, 20, 1, fail_ts=[NOW])}, NOW)
    assert seat["state"] == "eligible" and seat["first_fail_ages_out"] is None


def test_cursor_goes_last_on_unusable_answers():
    seat = ro.cursor_seat({**PEERS, "cursor": rec(runs=10, usable=4)}, NOW)
    assert seat["state"] == "last_choice" and "usable answers 40%" in seat["why"]
    assert ro.cursor_seat({**PEERS, "cursor": rec(runs=9, usable=0)}, NOW)["state"] == "eligible"


def test_cursor_goes_last_on_three_times_the_peer_fail_rate():
    fails = [NOW - 10 * DAY, NOW - 5 * DAY, NOW - DAY]
    seat = ro.cursor_seat({**PEERS, "cursor": rec(12, 12, 10, 3, fail_ts=fails)}, NOW)
    assert seat["state"] == "last_choice", seat
    # It drains: the bar reads the window, so the first FAIL leaves it 90 days after it was made.
    assert seat["first_fail_ages_out"] == NOW - 10 * DAY + ro.JUDGE_WINDOW_DAYS * DAY
    assert ro.cursor_seat({**PEERS, "cursor": rec(12, 12, 10, 2)}, NOW)["state"] == "eligible"


def test_cursor_is_removed_on_a_large_bad_record():
    seat = ro.cursor_seat({**PEERS, "cursor": rec(25, 25, 20, 5, fail_ts=[NOW] * 5)}, NOW)
    assert seat["state"] == "removed", seat
    assert ro.cursor_seat({**PEERS, "cursor": rec(25, 25, 19, 5)}, NOW)["state"] == "last_choice"


# ---- muse's rung and floor ------------------------------------------------------------------------
def test_muse_rung_lifts_at_five_graded_and_eighty_percent():
    assert ro.muse_seat({"muse": rec(graded=5, fail=1, runs_7d=0)})["lifted"] is True
    assert ro.muse_seat({"muse": rec(graded=5, fail=2)})["lifted"] is False
    assert ro.muse_seat({"muse": rec(graded=4, fail=0)})["lifted"] is False


def test_muse_floor_is_due_until_the_week_is_filled_or_the_rung_lifts():
    assert ro.muse_seat({})["floor_due"] is True
    assert ro.muse_seat({"muse": rec(runs_7d=6)})["floor_due"] is True
    assert ro.muse_seat({"muse": rec(runs_7d=7)})["floor_due"] is False
    assert ro.muse_seat({"muse": rec(graded=5, runs_7d=0)})["floor_due"] is False


# ---- the record, from a real Brain schema ---------------------------------------------------------
@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("CLOSER_CORE_STATE", str(tmp_path / "closer"))
    monkeypatch.setenv("OPENER_CORE_STATE", str(tmp_path / "opener"))
    ro._SEATS_CACHE.clear()
    with feedback._conn():
        pass
    yield tmp_path / "brain.db"
    ro._SEATS_CACHE.clear()


def _run(db, run_id, agent, ts, *, lane="opener", decision="start", verdict=None):
    state = ro.opener_state() if lane == "opener" else ro.closer_state()
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, mode) VALUES (?,?,?,?,?,?)",
            (run_id, ts, ro._caller(state), "review", agent, "local"),
        )
        if decision:
            con.execute(
                "INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    ro.OPENER if lane == "opener" else ro.PRODUCER,
                    "o/r#1",
                    "o/r#2",
                    decision,
                    ts,
                    ts,
                ),
            )
        if verdict:
            prefix = ro.OPENER_NOTE_PREFIX if lane == "opener" else ro.NOTE_PREFIX
            con.execute(
                "INSERT INTO outcomes (run_id, adjudicated_verdict, notes) VALUES (?,?,?)",
                (run_id, verdict, prefix + "fixture"),
            )


def test_the_record_counts_lane_judge_runs_in_the_window(brain):
    _run(brain, "m1", "muse", NOW - DAY, verdict="PASS")
    _run(brain, "m2", "muse", NOW - 2 * DAY, lane="closer", decision="close", verdict="FAIL")
    _run(brain, "m3", "muse", NOW - 10 * DAY, decision=None)
    _run(brain, "old", "muse", NOW - (ro.JUDGE_WINDOW_DAYS + 1) * DAY, verdict="FAIL")
    with sqlite3.connect(brain) as con:
        con.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, mode) VALUES "
            "('x', ?, 'offload:/elsewhere', 'review', 'muse', 'local')",
            (NOW,),
        )
    with sqlite3.connect(brain) as con:
        m = ro.judge_records(con, NOW)["muse"]
    assert (m["runs"], m["runs_7d"], m["usable"], m["graded"], m["fail"]) == (3, 2, 2, 2, 1)
    seats = ro.judge_seats(NOW, fresh=True)
    assert seats["state"] == "measured" and seats["muse"]["floor_due"] is True
    line = ro.seats_line(seats)
    assert "cursor eligible" in line and "muse review rung late (graded 2 / 5, PASS 50%)" in line
    assert "floor 2 / 7 this week (due)" in line


def test_an_unreadable_brain_keeps_cursor_eligible_and_forces_no_muse(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "missing" / "brain.db")
    ro._SEATS_CACHE.clear()
    seats = ro.judge_seats(NOW, fresh=True)
    assert seats["state"] == "unmeasured"
    assert seats["cursor"]["state"] == "eligible" and seats["muse"]["floor_due"] is False
    ro._SEATS_CACHE.clear()


# ---- the router applies the seats -----------------------------------------------------------------
CAP = {"agents": {a: {"state": "ok"} for a in ("codex", "gemini", "claude", "muse", "cursor")}}


@pytest.fixture
def routing(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "brain.db")
    monkeypatch.setattr(feedback, "current_weights", lambda *a, **k: [])
    monkeypatch.setattr(router, "load_capacity", lambda: CAP)
    holder = {"seats": None}
    monkeypatch.setattr(roles, "_judge_seats", lambda: holder["seats"])
    monkeypatch.setattr(ro, "judge_seats", lambda *a, **k: holder["seats"] or _seats())
    return holder


def _seats(cursor="eligible", floor_due=False, lifted=False):
    return {
        "state": "measured",
        "cursor": {"state": cursor},
        "muse": {"lifted": lifted, "floor_due": floor_due, "graded": 0},
    }


CURSOR_FIRST = {
    "review": {
        "cursor": {"rank": 0, "n_obs": 50},
        "gemini": {"rank": 1, "n_obs": 50},
        "codex": {"rank": 2, "n_obs": 50},
    }
}


def test_cursor_is_an_eligible_judge():
    assert "cursor" in roles.get_role("adjudicator").eligible_backends


def test_last_choice_cursor_is_tried_after_everyone_else(routing):
    routing["seats"] = _seats()
    assert (
        roles.route_role("adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0)[
            "agent"
        ]
        == "cursor"
    )
    routing["seats"] = _seats(cursor="last_choice")
    pick = roles.route_role("adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0)
    assert pick["agent"] != "cursor" and "seat_policy" not in pick
    only_cursor = roles.route_role(
        "adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0, only={"cursor"}
    )
    assert (only_cursor["agent"], only_cursor["seat_policy"]) == ("cursor", "cursor_last_choice")
    assert router.route_decision_record(only_cursor)["seat_policy"] == "cursor_last_choice"


def test_removed_cursor_is_never_picked(routing):
    routing["seats"] = _seats(cursor="removed")
    assert (
        roles.route_role(
            "adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0, only={"cursor"}
        )
        is None
    )


def test_a_low_stakes_call_fills_muses_floor(routing):
    routing["seats"] = _seats(floor_due=True)
    pick = roles.route_role(
        "adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0, low_stakes=True
    )
    assert (pick["agent"], pick["seat_policy"]) == ("muse", "muse_review_floor")
    for kwargs in ({}, {"low_stakes": True, "high_leverage": True}):
        assert (
            roles.route_role(
                "adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0, **kwargs
            )["agent"]
            != "muse"
        )
    routing["seats"] = _seats(floor_due=False)
    assert (
        roles.route_role(
            "adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0, low_stakes=True
        )["agent"]
        != "muse"
    )


def test_a_shed_muse_does_not_block_the_judge(routing):
    routing["seats"] = _seats(floor_due=True)
    cap = {"agents": {**CAP["agents"], "muse": {"state": "shed"}}}
    pick = roles.route_role(
        "adjudicator", cap=cap, learned=CURSOR_FIRST, exploration_rate=0.0, low_stakes=True
    )
    assert pick["agent"] != "muse" and "seat_policy" not in pick


def test_other_roles_ignore_the_judge_seats(routing):
    routing["seats"] = _seats(cursor="removed", floor_due=True)
    pick = roles.route_role(
        "triage",
        cap=CAP,
        learned={"role:triage": {"cursor": {"rank": 0}}},
        exploration_rate=0.0,
        low_stakes=True,
    )
    assert pick["agent"] == "cursor"


def test_the_review_rung_lifts_muse_from_the_record(routing):
    routing["seats"] = _seats(lifted=False)
    assert muse_ladder.decision("review", None) == {
        "rung": "review",
        "level": None,
        "lifted": False,
        "graded": 0,
    }
    routing["seats"] = _seats(lifted=True)
    assert muse_ladder.decision("review", None)["lifted"] is True
    assert muse_ladder.decision("review", None, env={"ORCH_MUSE_LADDER": "0"})["lifted"] is False
    # Implement rungs are unchanged.
    assert muse_ladder.rung_for("review", None) is None


def test_a_lifted_muse_is_no_longer_a_late_review_seat(routing):
    cap = {"agents": {"muse": {"state": "ok"}, "gemini": {"state": "ok"}}}
    muse_first = {"muse": {"rank": 0, "n_obs": 9}, "gemini": {"rank": 1, "n_obs": 9}}
    routing["seats"] = _seats(lifted=False)
    late = router.select_agent(
        "review",
        cap,
        only={"muse", "gemini"},
        learned=muse_first,
        exploration_rate=0.0,
        simulate=True,
    )
    routing["seats"] = _seats(lifted=True)
    lifted = router.select_agent(
        "review",
        cap,
        only={"muse", "gemini"},
        learned=muse_first,
        exploration_rate=0.0,
        simulate=True,
    )
    assert (late["agent"], lifted["agent"]) == ("gemini", "muse")
    assert lifted["muse_ladder"]["rung"] == "review"


def test_seats_are_cached_briefly(brain, monkeypatch):
    first = ro.judge_seats(NOW, fresh=True)
    monkeypatch.setattr(ro, "judge_records", lambda *a, **k: pytest.fail("cache bypassed"))
    assert ro.judge_seats(NOW) is first
    monkeypatch.setattr(time, "time", lambda: 10**12)
    with pytest.raises(pytest.fail.Exception):
        ro.judge_seats(NOW)


def test_reserve_claude_answers_before_a_last_choice_cursor(routing):
    """CodeRabbit on #1534: with the ordinary judges shed, the reserve seat comes before last choice."""
    routing["seats"] = _seats(cursor="last_choice")
    cap = {
        "agents": {
            "codex": {"state": "shed"},
            "gemini": {"state": "shed"},
            "muse": {"state": "shed"},
            "claude": {"state": "ok"},
            "cursor": {"state": "ok"},
        }
    }
    pick = roles.route_role("adjudicator", cap=cap, learned=CURSOR_FIRST, exploration_rate=0.0)
    assert pick["agent"] == "claude" and "seat_policy" not in pick
    cap["agents"]["claude"] = {"state": "shed"}
    pick = roles.route_role("adjudicator", cap=cap, learned=CURSOR_FIRST, exploration_rate=0.0)
    assert (pick["agent"], pick["seat_policy"]) == ("cursor", "cursor_last_choice")


def test_a_failing_seat_policy_routes_anyway_and_says_so(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(feedback, "current_weights", lambda *a, **k: [])

    def broken(*a, **k):
        raise RuntimeError("seat bug")

    monkeypatch.setattr(ro, "judge_seats", broken)
    pick = roles.route_role("adjudicator", cap=CAP, learned=CURSOR_FIRST, exploration_rate=0.0)
    assert pick["agent"] == "cursor"
    assert "judge seats unavailable (RuntimeError('seat bug'))" in capsys.readouterr().err
