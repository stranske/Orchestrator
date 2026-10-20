"""Lane C coverage chunk for Orchestrator #802 (redirect_apply, evidence_acquisition, exploration_backfill, execution_profiles)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import evidence_acquisition as ea
import execution_profiles as ep
import exploration_backfill as eb
import feedback
import redirect_apply as ra
import redirect_plan


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep imported state paths private and fail if any external work starts."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(ra.capabilities, "REG", runtime / "capabilities.json")
    monkeypatch.setattr(redirect_plan, "PROMPT_DIR", tmp_path / "redirect-prompts")
    db = runtime / "feedback" / "orchestrator.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(db))
    monkeypatch.setattr(feedback, "DB_PATH", db)
    started = []

    def forbid(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("coverage tests must not start external work")

    monkeypatch.setattr("subprocess.run", forbid)
    monkeypatch.setattr("subprocess.Popen", forbid)
    monkeypatch.setattr("socket.create_connection", forbid)
    monkeypatch.setattr("socket.socket.connect", forbid)
    yield
    assert started == []


def _cap(**over):
    base = {
        "status": "shadow",
        "liveness": None,
        "evidence_threshold": {"min_independent_durable_reuse": 3},
        "gate_blocks_execution": True,
        "gate_reason": "needs independent durable reuses",
        "gate_criteria": {"min_independent_durable_reuse": 3},
        "causal_evidence": {
            "readiness": {
                "durable_subjects": ["subject-a"],
                "terminal_outcomes": 2,
                "failures": 0,
                "rework": 0,
                "latest_evidence_ts": 1_700_000_000,
                "ready": False,
            }
        },
        "matcher": {"field": "task_type", "operator": "in", "value": ["implement"]},
        "kill_switch": "disable via config",
        "rollback": "revert",
        "outcome_links": [],
        "event_history": [],
    }
    base.update(over)
    import capabilities

    return {**capabilities._blank_capability("feed-me"), **base}


def test_evidence_acquisition_feedable_orders_by_remaining_debt() -> None:
    now = 1_700_000_000
    # Distinct durable-subject counts → distinct evidence_debt remaining (not id tie-break).
    ledger = {
        "capabilities": {
            "high-debt": _cap(
                causal_evidence={
                    "readiness": {
                        "durable_subjects": [],  # remaining = threshold 3
                        "terminal_outcomes": 0,
                        "failures": 0,
                        "rework": 0,
                        "latest_evidence_ts": now,
                        "ready": False,
                    }
                }
            ),
            "low-debt": _cap(
                causal_evidence={
                    "readiness": {
                        "durable_subjects": ["a", "b"],  # remaining = 1
                        "terminal_outcomes": 0,
                        "failures": 0,
                        "rework": 0,
                        "latest_evidence_ts": now,
                        "ready": False,
                    }
                }
            ),
        }
    }
    rows = ea.feedable(ledger, now=now)
    assert [(r["capability_id"], r["remaining"]) for r in rows] == [
        ("low-debt", 1),
        ("high-debt", 3),
    ]


def test_evidence_acquisition_plan_no_matching_work_state(tmp_path: Path) -> None:
    now = 1_700_000_000
    ledger = {"capabilities": {"c0": _cap()}}
    items = [{"target": "o/r#9", "task_type": "review"}]
    out = ea.plan(items, ledger=ledger, now=now)
    assert out["state"] == "no_matching_work"
    assert out["assignments"][0]["items"] == []


def test_evidence_acquisition_run_live_routes_planned_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_700_000_000
    ledger = {"capabilities": {"c0": _cap()}}
    items = [{"target": "o/r#1", "task_type": "implement"}]
    monkeypatch.setattr(
        ea.capabilities,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("heartbeat must not abort run")),
    )
    out = ea.run(
        items,
        ledger=ledger,
        now=now,
        env={ea.LIVE_FLAG: "1"},
        write=tmp_path / "plan.json",
    )
    assert out["live"] is True
    assert out["state"] == "planned"
    assert out["routed"] and out["routed"][0]["items"] == ["o/r#1"]
    assert (tmp_path / "plan.json").is_file()
    assert ea.render_tick_line(tmp_path / "plan.json").endswith("[LIVE]")


def test_evidence_acquisition_main_tick_line_and_json(
    capsys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ea.capabilities, "production_heartbeat", lambda *a, **k: False)
    plan = tmp_path / "p.json"
    plan.write_text(
        json.dumps(
            {
                "live": False,
                "state": "nothing_to_feed",
                "summary": "feedable 0 / capped 1 / candidates 0 / fed 0",
            }
        ),
        encoding="utf-8",
    )
    assert ea.main(["tick-line", "--plan", str(plan)]) == 0
    assert "EVIDENCE-ACQ" in capsys.readouterr().out
    assert ea.main(["--json"]) == 0


def test_redirect_apply_format_candidate_counts_and_merge() -> None:
    assert "UNKNOWN" in ra.format_candidate_counts({"supervisor": 1, "sweep": 2, "eligible": None})
    merged, sup_n, sweep_n = ra._merge_apply_candidates(
        [{"report": {"target": "a/r#1"}}],
        [{"report": {"target": "a/r#1"}}, {"report": {"target": "b/r#2"}}],
    )
    assert sup_n == 1 and sweep_n == 1
    assert [c["report"]["target"] for c in merged] == ["a/r#1", "b/r#2"]


def test_redirect_apply_sweep_stalled_candidates(tmp_path: Path) -> None:
    now = 1_700_000_000
    sweep = tmp_path / "sweep.json"
    sweep.write_text(
        json.dumps(
            {
                "generated_at": now,
                "actionable": [
                    {"target": "o/r#1", "state": "stalled", "agent": "keepalive"},
                    {"target": "o/r#2", "state": "running"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rows, meta = ra.sweep_stalled_candidates(sweep, now=now)
    assert meta["status"] == "current" and len(rows) == 1
    stale, stale_meta = ra.sweep_stalled_candidates(sweep, now=now + ra.DAY_SECONDS + 1)
    assert stale == [] and stale_meta["status"] == "stale"


def test_redirect_apply_reports_on_disk_and_format_status(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    good = report_dir / "lane.keepalive-supervisor-report.json"
    good.write_text(json.dumps({"target": "o/r#1", "agent": "keepalive"}), encoding="utf-8")
    (report_dir / "broken.keepalive-supervisor-report.json").write_text("{", encoding="utf-8")
    rows = ra._reports_on_disk(report_dir)
    assert len(rows) == 2
    status = ra.status(
        corpus_path=tmp_path / "empty.jsonl",
        env={},
        report_dir=report_dir,
        plan_path=tmp_path / "absent-plan.json",
        sweep_path=tmp_path / "absent-sweep.json",
    )
    lines = ra.format_status(status)
    assert lines and "deficits" in lines[0].lower() or "bootstrap" in " ".join(lines).lower()


def test_exploration_backfill_pure_helpers() -> None:
    states = eb._capacity_states(
        {"agents": {"codex": {"state": "ok"}, "claude": {"state": "shed"}}}
    )
    assert eb._agent_available("codex", states) is True
    assert eb._agent_available("claude", states) is False
    assert eb._target_repo("stranske/Orchestrator#1") == "stranske/Orchestrator"
    assert eb._target_repo("bad") is None
    slug = eb._exp_id_slug("stranske/Orchestrator#802")
    assert "/" not in slug and slug
    target, reasons = eb._cell_target(
        "implement",
        "codex",
        {"agent_observations": {"codex": 0}, "observed_agents": 0, "needed_total_observations": 5},
        ["codex", "claude"],
    )
    assert target >= 1 and "zero_observation_cell" in reasons


def test_execution_profiles_validate_and_resolve() -> None:
    profile = ep.get_profile("codex-6.1-sol-medium")
    assert profile["profile_id"] == "codex-6.1-sol-medium"
    assert ep.resolve_production_profile_id("codex-6.1-sol-medium") == "codex-6.1-sol-medium"
    assert ep.default_codex_profile("implement") in ep.PROFILE_REGISTRY
    assert (
        ep.replay_decision(
            ep.select_profile("implement", None, ["codex-6.1-sol-medium"], rng_seed=1)
        )["selected_profile_id"]
        == "codex-6.1-sol-medium"
    )


def test_execution_profiles_select_and_record_decision() -> None:
    conn = sqlite3.connect(":memory:")
    ep.ensure_schema(conn, now=1_700_000_000)
    candidates = [p["profile_id"] for p in ep.profiles_for_agent("codex")][:3]
    envelope = ep.select_profile(
        "implement",
        "o/r#1",
        candidates,
        rng_seed=42,
        scores={pid: float(i) for i, pid in enumerate(candidates)},
    )
    assert envelope["selected_profile_id"] in candidates
    ep.record_decision(conn, envelope, ts=1_700_000_000)
    stored = conn.execute(
        "SELECT selected_profile_id FROM routing_decisions_v2 WHERE task_type=?",
        ("implement",),
    ).fetchone()
    assert stored and stored[0] == envelope["selected_profile_id"]
