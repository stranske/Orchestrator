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

    assert (
        rate_incidents.main(
            [
                "record-lane-round",
                "--agent",
                "codex",
                "--surface",
                "another-relay",
                "--lane",
                "closer",
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


# --- feedback journal: a hosted seat's writes reach the Mac's Brain (2026-11-25) ---------------------------


def _journal_store(tmp_path, monkeypatch, name):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / f"{name}.db")
    return tmp_path / f"{name}.db"


def _rows(db, sql):
    with sqlite3.connect(db) as c:
        return c.execute(sql).fetchall()


def test_journal_is_off_unless_the_seat_sets_it(tmp_path, monkeypatch):
    monkeypatch.delenv(feedback.JOURNAL_ENV, raising=False)
    _journal_store(tmp_path, monkeypatch, "runner")
    feedback.record_run(
        "remote:o/r#5:codex", "o/r#5", "implement", "codex", mode="remote", pr_number=5
    )
    assert list(tmp_path.glob("*.jsonl")) == []


def test_a_hosted_start_replays_into_another_store_with_its_original_time(tmp_path, monkeypatch):
    journal = tmp_path / "hosted" / "run-1.jsonl"
    runner = _journal_store(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv(feedback.JOURNAL_ENV, str(journal))
    feedback.record_run(
        "remote:o/r#5:codex",
        "o/r#5",
        "implement",
        "codex",
        mode="remote",
        rationale="hosted start",
        pr_number=5,
    )
    feedback.record_outcome("remote:o/r#5:codex", merged=1)
    # ts given positionally must still bind once
    feedback.record_run(
        "offload:1", "o/r#6", "review", "claude", "offload", None, None, None, 6, None, 1700000000
    )
    monkeypatch.delenv(feedback.JOURNAL_ENV)
    assert [json.loads(ln)["fn"] for ln in journal.read_text().splitlines()] == [
        "record_run",
        "record_outcome",
        "record_run",
    ]
    original = _rows(runner, "SELECT run_id, ts, mode, rationale FROM runs ORDER BY run_id")

    mac = _journal_store(tmp_path, monkeypatch, "mac")
    first = feedback.replay_journal(journal)
    again = feedback.replay_journal(journal)  # a retried replay merges instead of duplicating
    assert (first["replayed"], first["rejected"], again["rejected"]) == (3, 0, 0)
    assert _rows(mac, "SELECT run_id, ts, mode, rationale FROM runs ORDER BY run_id") == original
    assert _rows(mac, "SELECT merged FROM outcomes WHERE run_id='remote:o/r#5:codex'") == [(1,)]
    assert _rows(mac, "SELECT COUNT(*) FROM completion_events") == _rows(
        runner, "SELECT COUNT(*) FROM completion_events"
    )


def test_a_bad_line_is_rejected_without_holding_back_the_rest(tmp_path, monkeypatch):
    _journal_store(tmp_path, monkeypatch, "mac")
    journal = tmp_path / "j.jsonl"
    good = {
        "fn": "record_run",
        "at": 1,
        "args": {
            "run_id": "remote:o/r#7:gemini",
            "target": "o/r#7",
            "task_type": "implement",
            "agent": "gemini",
            "ts": 1,
        },
    }
    journal.write_text(
        "\n".join([json.dumps({"fn": "no_such_writer", "args": {}}), "{not json", json.dumps(good)])
        + "\n"
    )
    report = feedback.replay_journal(journal)
    assert (report["replayed"], report["rejected"]) == (1, 2)
    assert [e["line"] for e in report["errors"]] == [1, 2]


def test_coverage_names_a_table_written_outside_the_journal(tmp_path, monkeypatch):
    journal = tmp_path / "j.jsonl"
    runner = _journal_store(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv(feedback.JOURNAL_ENV, str(journal))
    feedback.record_run(
        "remote:o/r#8:codex", "o/r#8", "implement", "codex", mode="remote", pr_number=8
    )
    monkeypatch.delenv(feedback.JOURNAL_ENV)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert feedback.journal_coverage(journal, scratch)["uncovered"] == {}
    with sqlite3.connect(runner) as c:  # a raw write no journaled writer made
        c.execute(
            "INSERT INTO costs (run_id, tokens_in, tokens_out, cost_usd, latency_s, source, pulled_ts) "
            "VALUES ('x', 1, 1, 0, 0, 'raw', 0)"
        )
    (scratch / "journal-replay.db").unlink()
    assert feedback.journal_coverage(journal, scratch)["uncovered"] == {
        "costs": {"store": 1, "journal": 0}
    }


def test_a_completion_payload_is_journaled_redacted_and_replays_the_same_row(tmp_path, monkeypatch):
    journal = tmp_path / "j.jsonl"
    runner = _journal_store(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv(feedback.JOURNAL_ENV, str(journal))
    secret = "ghp_" + "a1b2c3d4e5f6g7h8"
    feedback.record_completion_event(
        "remote:o/r#9:codex",
        payload={
            "raw_prompt": "implement it, token " + secret,
            "capability_ids": {"cap-a"},  # a set: the sanitizer accepts it, so the journal must too
            "role_ids": [secret],
            "artifact_refs": [
                {
                    "artifact_id": "receipt",
                    "raw_prompt": "private text",
                    "content_hash": "plain body",
                }
            ],
            "result_hashes": ["plain result text"],
        },
        timestamp=1700000000,
    )
    feedback.record_completion_event(  # shapes the Brain keeps only as hashes, and a multi-item set
        "remote:o/r#9c:codex",
        payload={
            "capability_ids": {"cap-c", "cap-a", "cap-b", "cap-d"},
            "artifact_refs": ["string artifact text", {"artifact_id": "prose artifact id text"}],
            "role_ids": ["role prose text"],
            "result": {"proposal_hash": "proposal body text", "status": "ok"},
            "verification": {"result_hashes": {"pytest": "verification body text"}},
        },
        timestamp=1700000000,
    )
    feedback.record_completion_event(  # the single-value shapes the sanitizer also accepts
        "remote:o/r#9b:codex",
        payload={
            "artifact_refs": {"artifact_id": "receipt", "raw_prompt": "single ref text"},
            "result_hashes": "scalar result text",
        },
        timestamp=1700000000,
    )
    feedback.record_completion_event(
        "remote:o/r#9d:codex",
        payload={"artifact_refs": "scalar artifact text"},
        timestamp=1700000000,
    )
    monkeypatch.delenv(feedback.JOURNAL_ENV)
    text = journal.read_text()
    assert "unserializable" not in text
    for private in (
        secret,
        "implement it",
        "private text",
        "plain body",
        "plain result text",
        "single ref text",
        "scalar result text",
        "string artifact text",
        "proposal body text",
        "verification body text",
        "prose artifact id text",
        "role prose text",
        "scalar artifact text",
    ):
        assert private not in text
    sql = (
        "SELECT event_id, payload_json, content_hash, validation_status, redaction_count "
        "FROM completion_events ORDER BY event_id"
    )
    original = _rows(runner, sql)
    assert secret not in str(original)

    mac = _journal_store(tmp_path, monkeypatch, "mac")
    assert feedback.replay_journal(journal)["rejected"] == 0
    assert _rows(mac, sql) == original


def test_a_replay_keeps_every_clock_default_and_clock_derived_identity(tmp_path, monkeypatch):
    journal = tmp_path / "j.jsonl"
    _journal_store(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv(feedback.JOURNAL_ENV, str(journal))
    ticks = iter(range(1_700_000_000, 1_700_000_100))  # every clock read crosses a second boundary
    with monkeypatch.context() as m:
        m.setattr(feedback._real_time, "time", lambda: float(next(ticks)))
        feedback.record_cost("remote:o/r#10:codex", 10, 20, 0.5, 3.0, "hosted")
    runner_cost = _rows(tmp_path / "runner.db", "SELECT pulled_ts FROM costs")
    mac0 = _journal_store(tmp_path, monkeypatch, "mac0")
    feedback.replay_journal(journal)
    assert _rows(mac0, "SELECT pulled_ts FROM costs") == runner_cost  # the runner's own clock value
    _journal_store(tmp_path, monkeypatch, "runner")
    journal.unlink()
    feedback.record_cost("remote:o/r#10:codex", 10, 20, 0.5, 3.0, "hosted")
    feedback.record_role_selector_event("needs-human", "no_matching_work", reason="none")
    monkeypatch.delenv(feedback.JOURNAL_ENV)
    at = 1_600_000_000.25  # as if the Mac replays the pass long after the runner made it
    lines = [json.loads(ln) for ln in journal.read_text().splitlines()]
    journal.write_text("".join(json.dumps({**e, "at": at}) + "\n" for e in lines))

    events = "SELECT event_id FROM completion_events WHERE producer='roles'"
    runner_events = _rows(tmp_path / "runner.db", events)
    first = _journal_store(tmp_path, monkeypatch, "mac")
    assert feedback.replay_journal(journal)["rejected"] == 0
    assert _rows(first, "SELECT pulled_ts FROM costs") == [(1_600_000_000,)]
    second = _journal_store(tmp_path, monkeypatch, "mac2")
    feedback.replay_journal(journal)
    assert _rows(first, events) == _rows(second, events) == runner_events != []


def test_a_retried_replay_does_not_write_old_values_over_newer_local_ones(tmp_path, monkeypatch):
    journal = tmp_path / "j.jsonl"
    _journal_store(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv(feedback.JOURNAL_ENV, str(journal))
    feedback.record_run("remote:o/r#11:codex", "o/r#11", "implement", "codex", mode="remote")
    feedback.record_outcome("remote:o/r#11:codex", merged=0, durability="pending")
    monkeypatch.delenv(feedback.JOURNAL_ENV)

    mac = _journal_store(tmp_path, monkeypatch, "mac")
    assert feedback.replay_journal(journal)["replayed"] == 2
    feedback.record_outcome("remote:o/r#11:codex", merged=1, durability="durable")
    again = feedback.replay_journal(journal)  # e.g. the branch delete failed and the file came back
    assert (again["replayed"], again["skipped"], again["rejected"]) == (0, 2, 0)
    assert _rows(mac, "SELECT merged, durability FROM outcomes") == [(1, "durable")]


def test_a_write_inside_a_callers_transaction_is_not_journaled(tmp_path, monkeypatch):
    journal = tmp_path / "j.jsonl"
    _journal_store(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv(feedback.JOURNAL_ENV, str(journal))
    c = feedback._conn()
    try:
        feedback.record_run("trial:1", "o/r#12", "implement", "codex", conn=c)
        c.rollback()
    finally:
        c.close()
    assert not journal.exists()
