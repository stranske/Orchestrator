"""Hermetic unit coverage for the default-off evidence-acquisition lane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evidence_acquisition as ea


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    return tmp_path


def _feedable_cap(matcher: dict | None = None) -> dict:
    return {
        "matcher": matcher or {"field": "task_type", "operator": "in", "value": ["implement"]},
        "status": "shadow",
    }


def test_plan_sorts_matches_and_caps_without_routing(isolated_runtime, monkeypatch):
    def unblock(cap, *, now):
        return {"feed": True, "blocker": "needs evidence", "action": "route"}

    def debt(cap):
        return {"remaining": cap["remaining"]}

    monkeypatch.setattr(ea.capabilities, "unblock", unblock)
    monkeypatch.setattr(ea.capabilities, "evidence_debt", debt)
    monkeypatch.setattr(ea, "MAX_FEEDS_PER_CYCLE", 1)
    ledger = {
        "capabilities": {
            "later": {**_feedable_cap(), "remaining": 3},
            "first": {**_feedable_cap(), "remaining": 1},
        }
    }
    candidates = [
        {"target": "o/r#1", "task_type": "implement"},
        {"target": "o/r#2", "task_type": "review"},
    ]
    plan = ea.plan(candidates, ledger=ledger, now=9, env={})
    assert plan["state"] == "planned"
    assert plan["assignments"][0]["capability_id"] == "first"
    assert plan["assignments"][0]["items"] == ["o/r#1"]
    assert plan["deferred"] == ["later"]
    written = isolated_runtime / "plan.json"
    result = ea.run(candidates, ledger=ledger, now=9, env={}, write=written)
    assert result["routed"] == [] and json.loads(written.read_text())["live"] is False
    assert ea.run(candidates, ledger=ledger, now=9, env={ea.LIVE_FLAG: "1"})["routed"]


def test_empty_and_bad_tick_plans_are_named_not_silenced(isolated_runtime, monkeypatch):
    monkeypatch.setattr(ea.capabilities, "unblock", lambda *_a, **_k: {"feed": False})
    assert ea.plan([], ledger={"capabilities": {}}, now=1)["state"] == "nothing_to_feed"
    path = isolated_runtime / "plan.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    assert "not a JSON object" in ea.render_tick_line(path)
    path.write_text("bad json")
    assert "plan unreadable" in ea.render_tick_line(path)
    assert ea.main(["tick-line", "--plan", str(path)]) == 0
