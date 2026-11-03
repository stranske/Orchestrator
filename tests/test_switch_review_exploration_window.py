"""Published rate and the measured hosted-switch count stay distinct from process settings."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import exploration_review
import feedback
import switch_review


def test_window_reports_published_rate_and_brain_count(tmp_path, monkeypatch):
    database = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", database)
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0.20")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    (tmp_path / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-17",
                "end": "20261215",
                "extended": True,
                "n_by_policy": {"epsilon-greedy": 29, "thompson-hybrid": 40},
            }
        )
    )
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
    assert (
        "exploration window: end 2026-12-15 (extended: yes, per-policy n epsilon-greedy 29 / thompson-hybrid 40)"
        in rendered
    )
    assert "ref=exports/route-weights" in calls[0][1]


def test_window_unreadable_is_unknown_without_creating_brain(tmp_path, monkeypatch):
    database = tmp_path / "absent.db"
    monkeypatch.setattr(feedback, "DB_PATH", database)
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    monkeypatch.setattr(switch_review, "_gh_call", lambda *a, **kw: (False, "", "offline"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    window = switch_review._exploration_window()
    assert window["rate"] == window["switches"] == "UNKNOWN"
    assert window["env"] == "unset"
    assert len(window["errors"]) == 3
    assert window["end"] == window["start"] == "unmeasured"
    assert not database.exists()


def test_review_reads_window_dates_from_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(switch_review, "_gh_call", lambda *a, **kw: (False, "", "offline"))
    (tmp_path / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-24",
                "end": "20261117",
                "extended": False,
                "n_by_policy": {"epsilon-greedy": 0, "thompson-hybrid": None},
            }
        )
    )
    with sqlite3.connect(feedback.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE agent_switches (source TEXT, delegation_source TEXT, switched_ts INTEGER)"
        )
        conn.execute(
            "INSERT INTO agent_switches VALUES ('policy', 'route_weights_exploration', ?)",
            (int(datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()),),
        )
    window = switch_review._exploration_window()
    assert window["start"] == "2026-10-24"
    assert window["end"] == "2026-11-17"
    assert window["extended"] == "no"
    assert window["switches"] == 0
    report = {
        "review_days": 7,
        "raise_count": 0,
        "exploration_gate": {"window": window},
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
    }
    assert "epsilon-greedy 0 / thompson-hybrid unmeasured" in switch_review.format_report(report)


def test_weekly_switch_count_uses_window_decision_instant(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(switch_review, "_gh_call", lambda *a, **kw: (False, "", "offline"))
    cutoff = int(datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp())
    (tmp_path / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-17",
                "end": "20261215",
                "extended": True,
                "n_by_policy": {"epsilon-greedy": 1, "thompson-hybrid": 1},
                "decided_at": "2026-11-18T00:00:00Z",
            }
        )
    )
    with sqlite3.connect(feedback.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE agent_switches (source TEXT, delegation_source TEXT, switched_ts INTEGER)"
        )
        conn.executemany(
            "INSERT INTO agent_switches VALUES ('policy', 'route_weights_exploration', ?)",
            [(cutoff - 1,), (cutoff,), (cutoff + 1,)],
        )
    assert switch_review._exploration_window()["switches"] == 2


@pytest.mark.parametrize(
    "decided_at", ["garbage", None, "2026-11-18T00:00:00", "2026-10-16T00:00:00Z"]
)
def test_malformed_decision_does_not_turn_unknown_into_zero(tmp_path, monkeypatch, decided_at):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(switch_review, "_gh_call", lambda *a, **kw: (False, "", "offline"))
    (tmp_path / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-17",
                "end": "20261215",
                "extended": True,
                "n_by_policy": {"epsilon-greedy": None, "thompson-hybrid": None},
                "decided_at": decided_at,
            }
        )
    )
    window = switch_review._exploration_window()
    assert window["switches"] == "UNKNOWN"
    assert any("decision" in error or "unmeasured" in error for error in window["errors"])
    assert not feedback.DB_PATH.exists()


def test_legacy_window_bounds_future_switches_to_review_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(switch_review, "_gh_call", lambda *a, **kw: (False, "", "offline"))
    start = int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp())
    (tmp_path / "exploration-window.json").write_text(
        json.dumps(
            {
                "start": "2026-10-17",
                "end": "20261117",
                "extended": False,
                "n_by_policy": {},
            }
        )
    )
    with sqlite3.connect(feedback.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE agent_switches (source TEXT, delegation_source TEXT, switched_ts INTEGER)"
        )
        conn.executemany(
            "INSERT INTO agent_switches VALUES ('policy', 'route_weights_exploration', ?)",
            [(start,), (start + 1,)],
        )
    assert switch_review._exploration_window(now=start)["switches"] == 1
