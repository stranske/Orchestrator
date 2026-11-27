"""Muse's work ladder: simple work first, harder work as it succeeds (owner 2026-11-27, P3 gap C)."""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

import muse_ladder
import router


def _brain(path, runs):
    """runs: [(rung, durability)] as Muse runs whose route_decision recorded that rung."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE runs (run_id TEXT, agent TEXT, ts INTEGER, routing_metadata TEXT)"
        )
        conn.execute("CREATE TABLE outcomes (run_id TEXT, durability TEXT)")
        for i, (rung, durability) in enumerate(runs):
            meta = {"route_decision": {"muse_ladder": {"rung": rung, "level": 1, "lifted": True}}}
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?,?)", (f"m{i}", "muse", 1_000_000, json.dumps(meta))
            )
            if durability:
                conn.execute("INSERT INTO outcomes VALUES (?,?)", (f"m{i}", durability))
    return path


@pytest.mark.parametrize(
    ("task_type", "size", "rung"),
    [
        ("mechanical", None, 1),
        ("implement", 1, 1),
        ("implement", 2, 1),
        ("implement", 3, 2),
        ("implement", 4, 2),
        ("implement", 5, 3),
        ("implement", None, None),  # unknown size never lifts Muse
        ("review", 1, None),
    ],
)
def test_rung_of_the_work(task_type, size, rung):
    assert muse_ladder.rung_for(task_type, size) == rung


def test_level_one_is_open_with_no_history_and_prints_the_bar(tmp_path):
    stand = muse_ladder.standing(_brain(tmp_path / "b.db", []), now=1_000_100, fresh=True)
    assert stand["level"] == 1 and stand["state"] == "measured"
    assert stand["rungs"][1] == {"resolved": 0, "durable": 0, "rate": None}
    assert "level 1 of 3" in muse_ladder.line(stand) and "rung 1 0/5 resolved" in muse_ladder.line(
        stand
    )


def test_promotion_needs_five_resolved_at_eighty_percent_durable(tmp_path):
    four = [(1, "durable")] * 4
    assert (
        muse_ladder.standing(_brain(tmp_path / "a.db", four), 1_000_100, fresh=True)["level"] == 1
    )
    five = [(1, "durable")] * 4 + [(1, "broke_later")]
    stand = muse_ladder.standing(_brain(tmp_path / "b.db", five), 1_000_100, fresh=True)
    assert stand["level"] == 2 and stand["rungs"][1]["rate"] == pytest.approx(0.8)
    weak = [(1, "durable")] * 3 + [(1, "broke_later")] * 2
    assert (
        muse_ladder.standing(_brain(tmp_path / "c.db", weak), 1_000_100, fresh=True)["level"] == 1
    )
    # Unresolved runs never count, and a failing lower rung closes the rungs above it.
    mixed = [(1, None)] * 9 + [(2, "durable")] * 5
    assert (
        muse_ladder.standing(_brain(tmp_path / "d.db", mixed), 1_000_100, fresh=True)["level"] == 1
    )


def test_an_unreadable_brain_keeps_level_one_and_says_so(tmp_path):
    stand = muse_ladder.standing(tmp_path / "missing" / "b.db", fresh=True)
    assert stand["level"] == 1 and stand["state"] == "unmeasured" and stand["reason"]


def _cap():
    return {
        "agents": {
            "claude": {"state": "ok", "pacing": {"state": "ok", "routing_weight": 1.0}},
            "gemini": {"state": "ok"},
            "muse": {"state": "ok"},
        }
    }


def _picks(monkeypatch, size, level=1, env="1", n=400):
    monkeypatch.setenv(muse_ladder.KILL_SWITCH, env)
    monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", "0")
    monkeypatch.setattr(router.adapters, "advertised_models", lambda *a, **k: [])
    monkeypatch.setattr(
        muse_ladder, "standing", lambda *a, **k: {"level": level, "rungs": {}, "state": "measured"}
    )
    cap = _cap()
    learned = {a: {"posterior": 0.5, "score": 0.5, "rank": 0} for a in cap["agents"]}
    rng = random.Random(11)
    out = []
    for _ in range(n):
        pick = router.select_agent(
            "implement",
            cap,
            only=set(cap["agents"]),
            learned=learned,
            exploration_rate=0.0,
            rng=rng,
            simulate=True,
            size_tasks=size,
        )
        out.append(pick)
    return out


def test_small_work_lifts_muse_into_the_draw_and_records_the_rung(monkeypatch):
    picks = _picks(monkeypatch, size=2)
    assert any(p["agent"] == "muse" for p in picks)
    muse = next(p for p in picks if p["agent"] == "muse")
    assert muse["muse_ladder"] == {"rung": 1, "level": 1, "lifted": True}
    record = router.route_decision_record(muse)
    assert record["muse_ladder"]["rung"] == 1 and record["chosen"] == "muse"


@pytest.mark.parametrize(("size", "level", "env"), [(None, 1, "1"), (3, 1, "1"), (2, 1, "0")])
def test_muse_stays_late_above_its_level_unknown_size_or_switched_off(
    monkeypatch, size, level, env
):
    assert not any(p["agent"] == "muse" for p in _picks(monkeypatch, size, level, env))


def test_a_promoted_ladder_lifts_muse_on_rung_two(monkeypatch):
    assert any(p["agent"] == "muse" for p in _picks(monkeypatch, size=3, level=2))


def test_mechanical_work_offers_muse_on_rung_one():
    agents = [e["agent"] for e in router._ROUTE_TABLE_TYPED["mechanical"]["agents"]]
    assert "muse" in agents
    entry = next(
        e for e in router._ROUTE_TABLE_TYPED["mechanical"]["agents"] if e["agent"] == "muse"
    )
    assert entry["late"] is True  # late unless the ladder lifts it
