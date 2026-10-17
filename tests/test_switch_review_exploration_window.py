"""Published rate and the measured hosted-switch count stay distinct from process settings."""

import json
import sqlite3
from datetime import datetime, timezone

import exploration_review
import feedback
import switch_review


def test_window_reports_published_rate_and_brain_count(tmp_path, monkeypatch):
    database = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", database)
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0.20")
    calls = []

    def gh(args, **kwargs):
        calls.append(args)
        return True, json.dumps({"exploration": {"rate": 0.05}}), ""

    monkeypatch.setattr(switch_review, "_gh_call", gh)
    monkeypatch.setattr(exploration_review, "build_report", lambda **kw: {"status": "ok"})
    start = int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp())
    # Initialise migrations before inserting modern UTC observations. The renderer
    # also reads the Brain; a legacy migration must not rebase this fixture later.
    with feedback._conn() as conn:
        for offset, source, delegation in [
            (-1, "policy", "route_weights_exploration"),
            (0, "label", "route_weights_exploration"),
            (1, "policy", "route_weights"),
        ]:
            conn.execute(
                "INSERT INTO agent_switches "
                "(pr_ref, from_agent, to_agent, switched_ts, recorded_ts, source, delegation_source) "
                "VALUES ('example/repo#1', 'codex', 'cursor', ?, ?, ?, ?)",
                (start + offset, start, source, delegation),
            )
    for expected in (0, 1):
        gate = switch_review._exploration_gate()
        assert gate["window"]["switches"] == expected
        report = {
            "review_days": 7,
            "raise_count": 0,
            "exploration_gate": gate,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
        }
        rendered = switch_review.format_report(report)
        assert (
            f"exploration window: rate 0.05 (env 0.20), exploration switches since 2026-10-17 {expected}"
            in rendered
        )
        if expected == 0:
            with sqlite3.connect(database) as conn:
                conn.execute(
                    "INSERT INTO agent_switches "
                    "(pr_ref, from_agent, to_agent, switched_ts, recorded_ts, source, delegation_source) "
                    "VALUES ('example/repo#2', 'codex', 'cursor', ?, ?, 'policy', "
                    "'route_weights_exploration')",
                    (start, start),
                )
    assert "ref=exports/route-weights" in calls[0][1]


def test_window_unreadable_is_unknown_without_creating_brain(tmp_path, monkeypatch):
    database = tmp_path / "absent.db"
    monkeypatch.setattr(feedback, "DB_PATH", database)
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    monkeypatch.setattr(switch_review, "_gh_call", lambda *a, **kw: (False, "", "offline"))
    window = switch_review._exploration_window()
    assert window["rate"] == window["switches"] == "UNKNOWN"
    assert window["env"] == "unset"
    assert len(window["errors"]) == 2
    assert not database.exists()
