"""Vibe priced from its own session records (owner 2026-11-27, campaign P3 gap C)."""

from __future__ import annotations

import json
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

import adapters
import comparative
import feedback
import ledger_reconcile


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(adapters, "AGENT_RUNTIME", tmp_path / "agent-runtime")
    monkeypatch.setattr(adapters, "VIBE_SESSIONS", tmp_path / "home-vibe-sessions")
    return db


def _session(root, name, workspace, start, tokens_in, tokens_out, cost):
    d = root / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "start_time": start,
                "environment": {"working_directory": str(workspace)},
                "stats": {
                    "session_prompt_tokens": tokens_in,
                    "session_completion_tokens": tokens_out,
                    "session_cost": cost,
                },
                "config": {"active_model": "mistral-medium-3.5"},
            }
        )
    )


def test_session_cost_joins_by_workspace_and_window_and_sums_restarts(brain, tmp_path):
    ws = tmp_path / "worktree"
    ws.mkdir()
    runtime = adapters.AGENT_RUNTIME / "vibe" / ".vibe" / "logs" / "session"
    _session(runtime, "a", ws, "2027-01-15T08:00:10+00:00", 1000, 100, 2.5)
    _session(runtime, "b", ws, "2027-01-15T08:03:00+00:00", 500, 50, 1.0)  # a restart in the run
    _session(runtime, "c", ws, "2027-01-15T12:00:00+00:00", 9, 9, 9.0)  # another run, later
    _session(runtime, "d", tmp_path / "elsewhere", "2027-01-15T08:00:10+00:00", 9, 9, 9.0)
    start = adapters._iso_to_epoch("2027-01-15T08:00:00+00:00")
    assert adapters.vibe_session_cost(str(ws), start, start + 300) == (
        1500,
        150,
        pytest.approx(3.5),
        adapters.VIBE_SESSION_COST_SOURCE,
    )
    # The real home is read too (sessions from before the runtime home existed).
    _session(adapters.VIBE_SESSIONS, "e", ws, "2027-01-16T08:00:10+00:00", 10, 1, 0.25)
    day2 = adapters._iso_to_epoch("2027-01-16T08:00:00+00:00")
    assert adapters.vibe_session_cost(str(ws), day2, day2 + 60)[2] == pytest.approx(0.25)


def test_a_refused_session_is_not_a_price(brain, tmp_path):
    """A 402 'Vibe budget exhausted' run writes a session with 0 tokens and $0: no observation."""
    ws = tmp_path / "w"
    ws.mkdir()
    runtime = adapters.AGENT_RUNTIME / "vibe" / ".vibe" / "logs" / "session"
    _session(runtime, "a", ws, "2027-01-15T08:00:10+00:00", 0, 0, 0.0)
    start = adapters._iso_to_epoch("2027-01-15T08:00:00+00:00")
    assert adapters.vibe_session_cost(str(ws), start, start + 60) is None


@pytest.mark.parametrize("shared", [True, False])
def test_reconcile_prices_a_vibe_run_from_its_session(brain, tmp_path, shared):
    ws = tmp_path / "worktree"
    ws.mkdir()
    runtime = adapters.AGENT_RUNTIME / "vibe" / ".vibe" / "logs" / "session"
    start = int(time.time()) - 60
    iso = datetime.fromtimestamp(start + 5, tz=timezone.utc).isoformat()
    _session(runtime, "a", ws, iso, 1200, 300, 4.25)
    log = tmp_path / ("example__repo_1.vibe.log" if shared else "offload.vibe.1.log")
    run_id = "vibe-run-1"
    stamp = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"=== {stamp} dispatch vibe/full -> example/repo#1 [implement] cwd={ws} run_id={run_id} ==="
        if shared
        else f"=== {stamp} offload vibe/mid cwd={ws} process_cwd={ws} ==="
    )
    log.write_text(header + "\nworking...\n")
    feedback.record_run(run_id, "example/repo#1", "implement", "vibe")
    adapters.record_ledger(
        "vibe", run_id=run_id, log_file=str(log), event="start", started_ts=start
    )
    adapters.record_ledger(
        "vibe", run_id=run_id, log_file=str(log), event="complete", started_ts=start, exit=0
    )
    ledger_reconcile.reconcile(adapters.LEDGER)
    with closing(feedback._conn()) as conn:
        row = conn.execute(
            "SELECT tokens_in,tokens_out,cost_usd,source FROM costs WHERE run_id=?", (run_id,)
        ).fetchone()
    assert row == (1200, 300, pytest.approx(4.25), adapters.VIBE_SESSION_COST_SOURCE)
    # Priced rows are protected: a second pass rewrites nothing.
    assert ledger_reconcile.reconcile(adapters.LEDGER)["written_cost_rows"] == 0


def test_vibe_enters_the_solve_cost_only_with_enough_priced_runs(brain):
    now = 1_800_000_000
    q = {"vibe": {"implement": 0.9}}

    def add_run(run_id, source, cost_usd):
        feedback.record_run(run_id, "example/repo#1", "implement", "vibe", ts=now)
        feedback.record_cost(run_id, source=source, cost_usd=cost_usd)

    add_run("zero", "ledger", 0.0)
    for index in range(comparative.MIN_OBS - 1):
        add_run(f"priced-{index}", adapters.VIBE_SESSION_COST_SOURCE, 3.0)
    below = comparative.measure(brain, {}, now=now, q=q)
    assert below["excluded"] == ["vibe"]
    assert any(
        f"{comparative.MIN_OBS - 1} of {comparative.MIN_OBS} runs priced" in reason
        for reason in below["reasons"]
    ), below["reasons"]
    add_run("threshold", adapters.VIBE_SESSION_COST_SOURCE, 3.0)
    included = comparative.measure(brain, {}, now=now, q=q)
    assert included["excluded"] == []
    assert included["inputs"]["cost"]["vibe"]["implement"] == pytest.approx(3.0)
    assert included["inputs"]["cost_source_by_agent"]["vibe"]["source"] == (
        adapters.VIBE_SESSION_COST_SOURCE
    )
