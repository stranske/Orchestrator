"""The operator log stays short while the complete tick plan remains inspectable."""

from __future__ import annotations

import json

import tick


def test_summary_prints_one_headline_and_writes_the_plan(monkeypatch, tmp_path, capsys):
    plan = {
        "chosen": [{"target": "stranske/Ready#1", "applied": False}],
        "no_capacity": [{"target": "stranske/Ready#2"}],
        "deferred": [],
        "blocked": [],
    }
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tick.router, "load_capacity", lambda: {})
    monkeypatch.setattr(tick.router, "load_backlog", lambda: [])
    monkeypatch.setattr(tick.router, "learned_ranks", lambda: {})
    monkeypatch.setattr(tick, "remote_tick", lambda *args, **kwargs: plan)

    assert tick.main(["--summary"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("TICK-PLAN:")
    assert "1 targets chosen, 0 applied, 1 skipped (shadow)" in lines[0]
    assert "1 no capacity" in lines[0]
    assert str(tmp_path / "tick-plan.json") in lines[0]
    assert json.loads((tmp_path / "tick-plan.json").read_text()) == plan

    assert tick.main([]) == 0
    assert json.loads(capsys.readouterr().out) == plan
