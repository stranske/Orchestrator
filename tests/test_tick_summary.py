"""The operator log stays short while the complete tick plan remains inspectable."""

from __future__ import annotations

import json

import tick


def test_summary_prints_one_headline_and_writes_the_plan(monkeypatch, tmp_path, capsys):
    owned = "already in agent pipeline (agent:codex) — not re-delegating"
    plan = {
        "chosen": [
            {"target": "stranske/Ready#1", "applied": None, "skip": owned, "labels_read": True}
        ],
        "no_capacity": [{"target": "stranske/Ready#2"}],
        "deferred": ["stranske/Ready#3"],
        "blocked": [],
        "cap": 3,
        "examine_cap": 12,
        "delegations": 0,
        "refused": 1,
        "errors": 0,
        "examined": 2,
        "deferral": {
            "delegable": 1,
            "drainable": 0,
            "by_cap": [],
            "by_examine_cap": ["stranske/Ready#3"],
            "delegable_targets": ["stranske/Ready#3"],
        },
    }
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tick.router, "load_capacity", lambda: {})
    monkeypatch.setattr(tick.router, "load_backlog", lambda: [])
    monkeypatch.setattr(tick.router, "learned_ranks", lambda: {})
    monkeypatch.setattr(tick, "remote_tick", lambda *args, **kwargs: plan)

    assert tick.main(["--summary"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("TICK-PLAN:")
    assert "delegations 0/3 would apply (shadow); 1 refused; examined 2/12;" in lines[0]
    assert "1 no capacity, 0 blocked; deferred 1 (delegable 1, drainable 0)" in lines[0]
    assert str(tmp_path / "tick-plan.json") in lines[0]
    assert json.loads((tmp_path / "tick-plan.json").read_text()) == plan

    assert tick.main([]) == 0
    assert json.loads(capsys.readouterr().out) == plan


def _headline(monkeypatch, tmp_path, capsys, chosen: list[dict]) -> str:
    plan = {"chosen": chosen, "no_capacity": [], "deferred": [], "blocked": []}
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tick.router, "load_capacity", lambda: {})
    monkeypatch.setattr(tick.router, "load_backlog", lambda: [])
    monkeypatch.setattr(tick.router, "learned_ranks", lambda: {})
    monkeypatch.setattr(tick, "remote_tick", lambda *args, **kwargs: plan)
    assert tick.main(["--summary"]) == 0
    [line] = capsys.readouterr().out.splitlines()
    return line


def test_summary_counts_the_label_reads_github_did_not_answer(monkeypatch, tmp_path, capsys):
    """A target whose labels did not come back is refused; the headline carries that count beside
    the reads that answered, so a refusal that persists across ticks is visible in the log."""
    owned = "already in agent pipeline (agent:codex) — not re-delegating"
    unread = "labels unread (gh exit 1: HTTP 502) — ownership unknown, not delegating"
    line = _headline(
        monkeypatch,
        tmp_path,
        capsys,
        [
            {"target": "o/r#1", "applied": None, "skip": None, "labels_read": True},
            {"target": "o/r#2", "applied": None, "skip": owned, "labels_read": True},
            {"target": "o/r#3", "applied": None, "skip": unread, "labels_read": False},
            {"target": "o/r", "applied": None, "skip": None},  # an error row: no read happened
        ],
    )
    assert "label reads 2 answered, 1 unanswered (refused)" in line, line


def test_summary_prints_the_refusal_fully_drained(monkeypatch, tmp_path, capsys):
    """Fully drained must be a line some input produces: every read answered, zero refused."""
    row = {"target": "o/r#1", "applied": None, "skip": None, "labels_read": True}
    line = _headline(monkeypatch, tmp_path, capsys, [dict(row) for _ in range(3)])
    assert "label reads 3 answered, 0 unanswered (refused)" in line, line
