"""Observed handoff relay exits enter the Brain and authoritative capacity incidents."""

import json
import sqlite3
import time
from datetime import datetime, timedelta

import capacity
import feedback
import rate_incidents


def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")
    monkeypatch.setattr(capacity, "SHED_DIR", rate_incidents.SHED_DIR)


def test_usage_limit_tail_sheds_the_agent(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    now = int(time.time())
    future = (datetime.fromtimestamp(now) + timedelta(hours=30)).replace(second=0, microsecond=0)
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(future.day, "th")
    clock = f"{future:%b} {future.day}{suffix}, {future:%Y %I:%M %p}"
    tail = tmp_path / "tail.txt"
    tail.write_text(f"You've hit your usage limit. Try again at {clock}\n")

    assert (
        rate_incidents.main(
            [
                "record-lane-round",
                "--agent",
                "codex",
                "--surface",
                "handoff-relay",
                "--lane",
                "opener",
                "--exit",
                "1",
                "--output-file",
                str(tail),
                "--ts",
                str(now),
            ]
        )
        == 0
    )
    marker = json.loads((rate_incidents.SHED_DIR / "codex").read_text())
    assert marker["expires_at"] == int(future.timestamp())
    assert capacity._shed("codex") is True
    assert len(rate_incidents.INCIDENT_FILE.read_text().splitlines()) == 1


def test_full_month_reset_and_cli_surface_reach_incident(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    now = int(time.time())
    future = (datetime.fromtimestamp(now) + timedelta(hours=30)).replace(second=0, microsecond=0)
    tail = tmp_path / "tail.txt"
    tail.write_text(f"You've hit your usage limit. Try again at {future:%B %d, %Y %I:%M %p}\n")

    assert rate_incidents.main(
        [
            "record-lane-round", "--agent", "codex", "--surface", "another-relay",
            "--lane", "closer", "--exit", "1", "--output-file", str(tail),
            "--ts", str(now),
        ]
    ) == 0
    incident = json.loads(rate_incidents.INCIDENT_FILE.read_text().splitlines()[0])
    assert incident["surface"] == "another-relay"
    assert incident["reset_at"] == int(future.timestamp())


def test_round_lands_in_routing_decisions_v2(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    decision_id = feedback.record_lane_round(
        "codex", "opener", "router:capacity", 1, "quota_exhausted", 1789770000
    )
    with feedback._conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM routing_decisions_v2 WHERE decision_id=?", (decision_id,)
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM routing_decisions_v2").fetchone()[0] == 1
    assert row["record_kind"] == "lane_round"
    assert (row["agent"], row["lane"], row["exit_status"], row["error_class"]) == (
        "codex",
        "opener",
        1,
        "quota_exhausted",
    )
    assert row["receiver_reason"] == "router:capacity"
    assert row["selected_profile_id"] is None
    assert row["assignment_probability"] is None
    assert (
        feedback.record_lane_round(
            "codex", "opener", "router:capacity", 1, "quota_exhausted", 1789770000
        )
        == decision_id
    )


def test_clean_exit_records_no_incident(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    tail = tmp_path / "tail.txt"
    tail.write_text("Tests include a quoted quota exhausted example; all checks passed.\n")
    result = rate_incidents.record_lane_round(
        agent="codex",
        lane="closer",
        receiver_reason=None,
        exit_status=0,
        output_file=tail,
    )
    assert result["error_class"] == "none"
    assert result["incident_id"] is None
    assert not rate_incidents.INCIDENT_FILE.exists()
    assert not rate_incidents.SHED_DIR.exists()
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routing_decisions_v2").fetchone()[0] == 1


def test_noncapacity_failure_records_unknown_without_shed(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    tail = tmp_path / "tail.txt"
    tail.write_text("checkout failed: file not found\n")
    result = rate_incidents.record_lane_round(
        agent="claude",
        lane="opener",
        receiver_reason="router",
        exit_status=2,
        output_file=tail,
    )
    assert result["error_class"] == "unknown"
    assert not rate_incidents.INCIDENT_FILE.exists()
    assert not rate_incidents.SHED_DIR.exists()


def test_legacy_profile_decision_migrates_without_invented_lane_fields(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    with sqlite3.connect(feedback.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE routing_decisions_v2 (decision_id TEXT PRIMARY KEY, "
            "ts INTEGER NOT NULL, task_type TEXT NOT NULL, target TEXT, "
            "candidate_profiles_json TEXT NOT NULL, gate_results_json TEXT NOT NULL, "
            "scores_json TEXT NOT NULL, selected_profile_id TEXT, exploration INTEGER NOT NULL, "
            "exploration_policy TEXT NOT NULL, rng_seed INTEGER NOT NULL, "
            "policy_version TEXT NOT NULL, assignment_probability REAL NOT NULL, "
            "causal_context_json TEXT, replay_hash TEXT NOT NULL, "
            "profile_attempt_ids_json TEXT NOT NULL DEFAULT '[]')"
        )
        conn.execute(
            "INSERT INTO routing_decisions_v2 VALUES "
            "('old',1,'implement',NULL,'[]','{}','{}',NULL,0,'best',1,'v1',0.5,'{}','hash','[]')"
        )
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT record_kind,task_type,assignment_probability,agent "
            "FROM routing_decisions_v2 WHERE decision_id='old'"
        ).fetchone()
    assert row == ("profile_selection", "implement", 0.5, None)
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routing_decisions_v2").fetchone()[0] == 1
