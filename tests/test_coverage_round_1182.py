"""Hermetic coverage for autopilot round #1182 (exploration_collection, observability_dashboard, ccusage_reconcile)."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile
from datetime import datetime, timezone

import pytest

import ccusage_reconcile as ccusage
import claims
import exploration_collection as ec
import exploration_evidence_plan as eep
import feedback
import observability_dashboard as dashboard
import router


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "feedback.db")
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)

    attempted_external = []

    def guarded_subprocess_run(cmd, **kwargs):
        if (
            cmd
            and cmd[0] == "gh"
            and len(cmd) >= 3
            and cmd[1] == "api"
            and "langsmith_fleet_registry.json" in cmd[2]
        ):
            raw = json.dumps({"repos": []}).encode()
            body = json.dumps(
                {"content": base64.b64encode(raw).decode("ascii")}
            )
            return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
        if cmd and cmd[0] == "agy" and "models" in cmd:
            # Offline catalog probe shape (tab-separated id + label).
            body = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
        if cmd and cmd[0] == "cursor-agent" and "--list-models" in cmd:
            body = "composer-2.5 - Composer 2.5 (current)\n"
            return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
        if cmd and cmd[0] == "npx" and "ccusage" in cmd:
            return type("Proc", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        attempted_external.append((cmd, kwargs))
        pytest.fail("tests/test_coverage_round_1182.py attempted unstubbed external I/O")

    def forbid_external(*args, **kwargs):
        attempted_external.append((args, kwargs))
        pytest.fail("tests/test_coverage_round_1182.py attempted external I/O")

    monkeypatch.setattr(subprocess, "run", guarded_subprocess_run)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)

    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp_under_tmp(suffix="", prefix=None, dir=None):
        root = tmp_path / "_tempfile_root"
        root.mkdir(parents=True, exist_ok=True)
        # Always parent beneath tmp_path so selftests never allocate outside the test sandbox.
        return real_mkdtemp(suffix=suffix, prefix=prefix or "tmp", dir=str(root))

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_under_tmp)

    yield

    assert attempted_external == []


def _capacity_payload() -> dict:
    return {
        "agents": {
            "claude": {"state": "ok"},
            "codex": {"state": "ok"},
            "cursor": {"state": "ok"},
        }
    }


def _eligible_acquisition(
    *, mode: str = "epsilon-greedy", eligible: bool = True, daily_cap: int = 4
) -> dict:
    return {
        "stage": "stage_2_supervised_collection",
        "next_action": "collect",
        "direct_mode_deficits": [
            {"mode": "epsilon-greedy", "remaining_outcome_runs": 5},
            {"mode": "thompson-hybrid", "remaining_outcome_runs": 2},
        ],
        "candidate_task_types": [
            {"task_type": "implement", "recommended": True},
            {"task_type": "testgen", "recommended": True},
        ],
        "supervised_collection": {
            "windows": [
                {
                    "mode": mode,
                    "eligible": eligible,
                    "max_exploratory_dispatches_per_day": daily_cap,
                }
            ]
        },
    }


# --- exploration_collection.py ---


def test_choose_mode_aliases_and_deficits():
    plan = {
        "direct_mode_deficits": [
            {"mode": "epsilon-greedy", "remaining_outcome_runs": 1},
            {"mode": "thompson-hybrid", "remaining_outcome_runs": 3},
        ]
    }
    assert ec.choose_mode(plan, "thompson") == "thompson-hybrid"
    assert ec.choose_mode(plan, "epsilon") == "epsilon-greedy"
    assert ec.choose_mode(plan, "auto") == "thompson-hybrid"
    tied = {
        "direct_mode_deficits": [
            {"mode": "epsilon-greedy", "remaining_outcome_runs": 2},
            {"mode": "thompson-hybrid", "remaining_outcome_runs": 2},
        ]
    }
    week = 7 * 86400
    assert ec.choose_mode(tied, "auto", now=0) == "epsilon-greedy"
    assert ec.choose_mode(tied, "auto", now=week) == "thompson-hybrid"


def test_filter_backlog_low_risk_and_sanitize_paths(monkeypatch):
    plan = {
        "candidate_task_types": [
            {"task_type": "epic", "recommended": True},
            {"task_type": "implement", "recommended": True},
        ]
    }
    backlog = {"items": [{"target": "o/r#1", "task_type": "epic", "lane": "opener"}]}
    selected, skipped = ec._filter_backlog(plan, backlog, 5)
    assert not selected
    assert skipped[0]["reason"] == "task type is not low-risk for supervised collection"

    late_table = {
        "implement": {
            "agents": [
                {"agent": "codex", "mode": "full", "late": True},
                "not-a-dict",
            ]
        }
    }
    monkeypatch.setattr(router, "ROUTE_TABLE", late_table)
    assert ec._entry_is_late("implement", {"agent": "codex", "mode": "full"}) is True
    assert ec._entry_is_late("implement", {"agent": "claude", "mode": "full"}) is False

    decision = {
        "assignments": [
            {"lane": "closer", "task_type": "implement", "agent": "codex", "exploration": True},
            {"lane": "opener", "task_type": "epic", "agent": "codex", "exploration": True},
            {"lane": "opener", "task_type": "implement", "agent": "aider", "exploration": True},
            {
                "lane": "opener",
                "task_type": "implement",
                "agent": "codex",
                "mode": "full",
                "exploration": True,
            },
            {"lane": "opener", "task_type": "implement", "agent": "codex", "exploration": False},
        ]
    }
    monkeypatch.setattr(router, "BACKUP_AGENTS", {"aider"})
    clean, rejected = ec._sanitize_decision(decision, require_exploration=True)
    assert [row["reason"] for row in rejected] == [
        "not opener lane",
        "not a low-risk supervised-collection task type",
        "backup/paygo agent is not allowed in supervised exploration windows",
        "late/paygo route entry is not allowed in supervised exploration windows",
        "non-exploratory assignment does not collect direct exploration evidence",
    ]
    assert len(rejected) == 5
    assert "rejected 5" in " ".join(clean.get("notes") or [])


def test_claim_release_and_router_env_restore(monkeypatch):
    released = []

    def boom(target, agent):
        released.append((target, agent))
        if target == "bad":
            raise OSError("release failed")

    monkeypatch.setattr(claims, "release", boom)
    ec._release_rejected_claims(
        [
            {"assignment": {"target": "ok", "agent": "codex"}},
            {"assignment": {"target": "bad", "agent": "codex"}},
        ]
    )
    ec._release_assignments([{"target": "ok2", "agent": "claude"}])
    assert released == [("ok", "codex"), ("bad", "codex"), ("ok2", "claude")]

    monkeypatch.delenv("ORCH_EXPLORATION_MODE", raising=False)
    monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
    with ec._temporary_router_env(mode="epsilon-greedy", rate=0.1):
        assert os.environ["ORCH_EXPLORATION_MODE"] == "epsilon-greedy"
    assert "ORCH_EXPLORATION_MODE" not in os.environ

    monkeypatch.setenv("ORCH_EXPLORATION_MODE", "keep-me")
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0.5")
    with ec._temporary_router_env(mode="thompson-hybrid", rate=0.2):
        pass
    assert os.environ["ORCH_EXPLORATION_MODE"] == "keep-me"
    assert os.environ["ORCH_EXPLORATION_RATE"] == "0.5"

    with ec._temporary_random_seed(None):
        pass


def test_seed_candidates_and_blocked_window_reasons(monkeypatch):
    assert ec._seed_candidates(42, 10) == [42]
    assert ec._seed_candidates(None, 0) == [None]

    monkeypatch.setattr(eep, "build_plan", lambda **kw: _eligible_acquisition())
    backlog = {"items": [{"target": "o/r#1", "task_type": "implement", "lane": "opener"}]}
    monkeypatch.setattr(
        eep,
        "build_plan",
        lambda **kw: _eligible_acquisition(daily_cap=0),
    )
    monkeypatch.setattr(eep, "SUPERVISED_COLLECTION_DAILY_CAP", 0)
    zero_cap = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        min_exploratory_dispatches=1,
        max_dispatches=1,
    )
    assert any("max dispatch cap is zero" in r for r in zero_cap["blocked_reasons"])
    monkeypatch.setattr(eep, "SUPERVISED_COLLECTION_DAILY_CAP", 4)
    monkeypatch.setattr(eep, "build_plan", lambda **kw: _eligible_acquisition())
    zero_min = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        min_exploratory_dispatches=0,
        max_dispatches=2,
    )
    assert any("min exploratory" in r for r in zero_min["blocked_reasons"])


def test_build_window_active_path_and_format_human(monkeypatch, tmp_path):
    acquisition = _eligible_acquisition()
    monkeypatch.setattr(eep, "build_plan", lambda **kw: acquisition)
    backlog = {
        "items": [
            {
                "target": "o/r#9",
                "task_type": "implement",
                "lane": "opener",
                "title": "Fix",
                "body": "body",
            }
        ]
    }

    def fake_plan(items, capacity_snapshot, **kwargs):
        exploratory = [
            {
                "target": "o/r#9",
                "task_type": "implement",
                "lane": "opener",
                "agent": "codex",
                "mode": "full",
                "exploration": kwargs.get("dry_run", True),
            }
        ]
        if kwargs.get("dry_run") is False:
            exploratory.append(
                {
                    "target": "o/r#9",
                    "task_type": "implement",
                    "lane": "opener",
                    "agent": "claude",
                    "mode": "full",
                    "exploration": False,
                }
            )
        return {"assignments": exploratory, "notes": []}

    monkeypatch.setattr(router, "plan", fake_plan)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    monkeypatch.setattr(ec, "_release_rejected_claims", lambda rows: None)
    monkeypatch.setattr(ec, "_release_assignments", lambda rows: None)
    handoff = tmp_path / "handoff"
    monkeypatch.setattr(router, "HANDOFF", handoff)
    monkeypatch.setattr(router, "DECISION_JSON", handoff / "decision.json")

    dry = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=True,
        rng_seed=0,
        min_exploratory_dispatches=1,
        seed_search_limit=3,
    )
    assert dry["seed_search"]["found"] is True
    text = ec.format_human({**dry, "blocked_reasons": ["cap blocked", "no seed"]})
    assert "blocked:" in text and "cap blocked" in text

    plan_for_seed = ec._plan_for_seed

    def never_enough(*args, **kwargs):
        return plan_for_seed(*args, **kwargs)

    monkeypatch.setattr(ec, "_plan_for_seed", never_enough)
    monkeypatch.setattr(ec, "_exploratory_count", lambda decision: 0)
    no_seed = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=True,
        min_exploratory_dispatches=99,
        seed_search_limit=2,
    )
    assert any("no router seed" in r for r in no_seed["blocked_reasons"])

    monkeypatch.setattr(ec, "_exploratory_count", lambda decision: 1)
    live = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=False,
        rng_seed=0,
        min_exploratory_dispatches=1,
        seed_search_limit=1,
    )
    assert live["active_dispatch"] is True

    monkeypatch.setattr(ec, "_exploratory_count", lambda decision: 0)
    race = ec.build_window(
        backlog_payload=backlog,
        capacity_payload=_capacity_payload(),
        dry_run=False,
        rng_seed=0,
        min_exploratory_dispatches=1,
        seed_search_limit=1,
    )
    assert any("active claim race" in r for r in race["blocked_reasons"])


def test_exploration_collection_main_paths(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ec, "_selftest", lambda: None)
    assert ec.main(["--selftest"]) == 0

    monkeypatch.setenv("ORCH_EXPLORATION_EVIDENCE", "0")
    assert ec.main(["--apply"]) == 2
    cap = capsys.readouterr()
    err = cap.out + cap.err
    expected = (
        "active dispatch requires --confirm-window and ORCH_EXPLORATION_EVIDENCE=1"
    )
    assert err.strip() == expected

    monkeypatch.setattr(
        ec,
        "build_window",
        lambda **kw: {
            "eligible": True,
            "read_only": False,
            "decision": {"assignments": [{"target": "o/r#1"}]},
            "mode": "epsilon-greedy",
            "exploration_rate": 0.2,
            "direct_exploration_assignments": 1,
            "selected_backlog": [],
            "blocked_reasons": [],
            "seed_search": {},
            "acquisition_stage": "s",
            "acquisition_next_action": "n",
        },
    )
    monkeypatch.setattr(router, "HANDOFF", tmp_path / "h")
    monkeypatch.setattr(router, "DECISION_JSON", tmp_path / "h" / "decision.json")
    monkeypatch.setattr(ec.dispatcher, "run", lambda decision, dry_run=False: {"count": 1})
    monkeypatch.setenv("ORCH_EXPLORATION_EVIDENCE", "1")
    assert ec.main(["--apply", "--confirm-window", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dispatch_result"]["count"] == 1

    monkeypatch.setattr(
        ec,
        "build_window",
        lambda **kw: {
            "eligible": False,
            "read_only": True,
            "decision": {"assignments": []},
            "mode": "auto",
            "exploration_rate": 0.2,
            "direct_exploration_assignments": 0,
            "selected_backlog": [],
            "blocked_reasons": ["blocked"],
            "seed_search": {},
            "acquisition_stage": None,
            "acquisition_next_action": None,
        },
    )
    assert ec.main(["--apply", "--confirm-window"]) == 0

    monkeypatch.setattr(
        ec,
        "build_window",
        lambda **kw: {
            "eligible": True,
            "read_only": True,
            "decision": {"assignments": []},
            "mode": "auto",
            "exploration_rate": 0.2,
            "direct_exploration_assignments": 0,
            "selected_backlog": [],
            "blocked_reasons": [],
            "seed_search": {},
            "acquisition_stage": None,
            "acquisition_next_action": None,
        },
    )
    assert ec.main([]) == 0
    assert "exploration_collection:" in capsys.readouterr().out


# --- ccusage_reconcile.py ---


def test_ccusage_read_ledger_and_parsers(tmp_path):
    missing, errors = ccusage._read_ledger(tmp_path / "absent.ndjson")
    assert missing == [] and errors == []

    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                "",
                "not-json",
                json.dumps([]),
                json.dumps({"run_id": "r1", "event": "start", "ts": 1}),
            ]
        )
    )
    rows, errors = ccusage._read_ledger(ledger)
    assert len(rows) == 1
    assert len(errors) == 2

    assert ccusage._parse_iso_ts(10) == 10
    assert ccusage._parse_iso_ts("2026-01-01T00:00:00Z") is not None
    assert ccusage._parse_iso_ts("") is None
    assert ccusage._parse_iso_ts("not-a-date") is None

    assert ccusage._parse_codex_period_ts(42) is None
    assert ccusage._parse_codex_period_ts("rollout-2026-06-22T06-00-00-id") is not None
    assert ccusage._parse_codex_period_ts("rollout-not-a-ts") is None


def test_ccusage_run_windows_skips(tmp_path, monkeypatch):
    feedback.record_run("known", "o/r#1", "implement", "codex", ts=1)
    feedback.record_run("run-vibe", "o/r#2", "implement", "vibe", ts=1)
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                json.dumps({"run_id": "", "event": "start", "ts": 1}),
                json.dumps({"run_id": "unknown", "event": "start", "ts": 1}),
                json.dumps({"run_id": "run-vibe", "event": "start", "ts": 1, "agent": "vibe"}),
                json.dumps({"run_id": "run-vibe", "event": "complete", "ts": 10, "agent": "vibe"}),
                json.dumps({"run_id": "known", "event": "start", "ts": 10}),
                json.dumps({"run_id": "bad-window", "event": "start", "ts": 100, "agent": "codex"}),
                json.dumps(
                    {"run_id": "bad-window", "event": "complete", "ts": 50, "agent": "codex"}
                ),
            ]
        )
    )
    windows, skipped = ccusage._run_windows(
        [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()],
        {"known": "codex", "run-vibe": "vibe", "bad-window": "codex"},
    )
    assert not windows
    assert skipped["missing_run_id"] == 1
    assert skipped["unknown_run_id"] == 1
    assert skipped["unsupported_agent"] == 1
    assert skipped["incomplete_run_window"] == 1
    assert skipped["invalid_run_window"] == 1


def test_ccusage_load_sessions_and_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ccusage.shutil, "which", lambda _: None)

    def fail_run(cmd, **kwargs):
        if cmd[0] == "npx":
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "nope"})()
        raise OSError("boom")

    monkeypatch.setattr(ccusage.subprocess, "run", fail_run)
    sessions, errors = ccusage._load_ccusage_sessions(since_days=7)
    assert sessions == [] and errors

    def ok_run(cmd, **kwargs):
        payload = json.dumps({"sessions": [{"agent": "codex", "period": "p"}]})
        return type("P", (), {"returncode": 0, "stdout": payload, "stderr": ""})()

    monkeypatch.setattr(ccusage.subprocess, "run", ok_run)
    sessions, errors = ccusage._load_ccusage_sessions(since_days=7)
    assert len(sessions) == 1 and not errors

    monkeypatch.setattr(
        ccusage.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
    )
    _, errors = ccusage._load_ccusage_sessions()
    assert errors and "ccusage failed" in errors[0]

    summary = {
        "dry_run": True,
        "sessions_read": 0,
        "completed_windows": 0,
        "prepared": 0,
        "written_cost_rows": 0,
        "skipped": {"x": 1},
        "errors": ["e"],
    }
    ccusage._print_summary(summary, as_json=False)
    text = capsys.readouterr().out
    assert "ccusage_reconcile:" in text and "skipped:" in text

    monkeypatch.setattr(ccusage, "_selftest", lambda: None)
    assert ccusage.main(["--selftest"]) == 0
    with pytest.raises(SystemExit) as exc:
        ccusage.main([])
    assert exc.value.code == 2

    monkeypatch.setattr(
        ccusage,
        "reconcile",
        lambda *a, **k: {
            "dry_run": True,
            "sessions_read": 0,
            "completed_windows": 0,
            "prepared": 0,
            "written_cost_rows": 0,
            "skipped": {},
            "errors": [],
            "strict_failed": True,
        },
    )
    assert ccusage.main(["reconcile", "--strict"]) == 2


def test_ccusage_reconcile_uses_loaded_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(ccusage, "_load_ccusage_sessions", lambda **kw: ([], ["load err"]))
    monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    (tmp_path / "ledger.ndjson").write_text("")
    out = ccusage.reconcile(dry_run=True)
    assert "load err" in out["errors"]


# --- observability_dashboard.py ---


def _dash_report() -> dict:
    return {
        "generated_at": 1,
        "db_path": "/tmp/x.db",
        "window_days": 90,
        "dataset": {
            "table_counts": {},
            "route_weights_version": 1,
            "previous_route_weights_version": 0,
        },
        "route_weights": {
            "tasks": [
                {
                    "task_type": "implement",
                    "cold_start": True,
                    "diverges_from_prior": True,
                    "learned_order": ["codex"],
                    "rows": [{"agent": "codex", "n_obs": 0, "posterior": 0.1, "score": 1.0}],
                }
            ]
        },
        "outcomes": {
            "window_days": 90,
            "rollup": {"runs_total": 1, "outcome_rows": 1, "outcome_coverage": 1.0},
        },
        "production_flow": {
            "status": "flowing",
            "production_runs": 1,
            "production_outcomes": 1,
            "outcome_coverage": 1.0,
        },
        "dry_seams": {"findings": [], "outcome_gap_summary": {}},
        "capabilities": {
            "capabilities": {
                "wired-cap": {
                    "status": "wired",
                    "last_match": 5,
                    "last_invocation": 1,
                    "event_history": [],
                },
                "orphan-cap": {
                    "status": "wired",
                    "last_match": 0,
                    "event_history": [],
                },
            }
        },
        "range_rollout": {"eligible": True},
        "runtime_ac_flow": {
            "status": "ok",
            "closer_proxy_present": True,
            "runtime_ac_live_firing": False,
        },
        "exploration_policy": {},
        "exploration_evidence_plan": {
            "stage": "stage_4_default_review_complete",
            "exploration_review": {},
        },
        "exploration_backfill_plan": {"active_backfill_eligible": False},
        "keepalive_supervisor": {},
        "process_improvement": {"signals": [], "non_durable_issue_count": 0},
        "role_activation": {
            "roles": {"redirect": {"matched": 1, "invoked": 1, "linked": 0, "durable": 0}}
        },
        "execution_profiles": {
            "profiles": [
                {"profile_id": "p", "requested_model": "m", "resolved_model_coverage": 0.5}
            ]
        },
        "evidence": {"schema_growth": {}, "proposals": []},
        "features": {},
        "experiments": {"implementation_arms": [], "missing_arm_outcomes": []},
        "costs_traces": {"worker_model_provenance": {}},
        "research_subjects": {},
        "pattern_miner": {"status": {}, "inventory": {}},
    }


def test_dashboard_helpers_and_activation_branches(tmp_path, monkeypatch):
    assert dashboard._fmt_num(1.2345) == "1.234"
    assert dashboard._fmt_num(None) == "n/a"
    assert dashboard._read_json(None) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert dashboard._read_json(bad) == {}

    report = _dash_report()
    rows = dashboard._capability_rows(report)
    by_id = {row["capability_id"]: row for row in rows}
    assert (
        by_id["wired-cap"]["exact_reason"] == "matching work was observed after the last invocation"
    )
    assert by_id["orphan-cap"]["exact_reason"] == "no matching work has been observed"

    cadence = dashboard._cadence_health(state_dir=tmp_path, now=1)
    activation = dashboard._activation_state(report, cadence)
    assert activation["range"]["exact_reason"] == "eligible range work is available"

    blocked = dict(report)
    blocked["range_rollout"] = {"eligible": False, "blocked_reasons": ["claim busy"]}
    activation_blocked = dashboard._activation_state(blocked, cadence)
    assert activation_blocked["range"]["exact_reason"] == "claim busy"

    runtime_only = dict(report)
    runtime_only["runtime_ac_flow"] = {
        "closer_proxy_present": True,
        "runtime_ac_live_firing": False,
    }
    activation_runtime = dashboard._activation_state(runtime_only, cadence)
    assert "closer traffic" in activation_runtime["runtime_ac"]["exact_reason"]

    learning = dashboard._learning(report)
    route_stage4 = dashboard._route_coverage_summary(
        learning,
        {},
        {"stage": "stage_4_default_review_complete"},
        {"active_backfill_eligible": False},
    )
    assert route_stage4["stage4_complete"] is True
    assert "Stage 4 is complete" in route_stage4["recommendation"]

    route_backfill = dashboard._route_coverage_summary(
        learning, {}, {}, {"active_backfill_eligible": True}
    )
    assert "guarded backfill" in route_backfill["recommendation"]

    route_wait = dashboard._route_coverage_summary(learning, {}, {}, {})
    assert "Wait for safe opener" in route_wait["recommendation"]


def test_dashboard_alerts_and_actionability(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "_cadence_health",
        lambda **kw: {"steps": [], "durability_sweep_stamp_status": "stale"},
    )
    report = _dash_report()
    report["dry_seams"] = {
        "findings": [{"sink": "route_weights", "finding": "prior-only", "status": "warn"}],
        "outcome_gap_summary": {"actionable_runs_without_outcome": 2},
    }
    report["production_flow"] = {
        "status": "stale",
        "outcome_coverage": 0.5,
        "recommendation": "stale production",
        "recent_production_runs": 0,
    }
    report["outcomes"] = {"rollup": {"pending_durability_count": 2}}
    report["keepalive_supervisor"] = {
        "status": "armed_for_layered_ab_review",
        "summary": {"failure_outcomes": 1, "meaningful_disagreements": 1},
        "stage2_proposal_corpus": {
            "status": "waiting_for_candidates",
            "ready_for_historical_replay_analysis": True,
            "ready_for_supervised_apply": False,
            "summary": {
                "live_valid_proposals": 3,
                "synced_role_outcomes": 1,
            },
            "live_plan": {"status": "missing", "age_s": 200000},
        },
    }
    built = dashboard.build_dashboard(
        report,
        {
            "agents": {
                "gemini": {
                    "state": "warn",
                    "reason": "soft cap",
                    "policy": "window-soft-cap",
                    "next_action": "wait",
                    "used_5h": 1,
                    "soft_units_5h": 2,
                    "used_weekly": 1,
                    "soft_units_weekly": 2,
                },
                "aider": {"state": "unknown", "reason": "probe failed"},
            }
        },
    )
    keys = {alert["key"] for alert in built["alerts"]}
    assert "production_flow_stale" in keys
    assert "pending_durability" in keys
    assert "durability_sweep_stale" in keys
    assert "stage2_live_link_gap" in keys
    assert "stage2_waiting_for_candidates" in keys
    assert "stage2_live_plan_unavailable" in keys or "stage2_live_plan_stale" in keys

    health = {
        "outcome_gap_actionable": 1,
        "human_calibration_count": 1,
        "human_calibration_ready": False,
        "human_calibration_recommendation": "need more pairs",
        "human_calibration_pairs": 1,
        "active_evidence_types": 0,
        "evidence_schema_clustered_proposals": 2,
        "evidence_schema_prune_candidates": 1,
        "evidence_schema_active_review": [],
        "langsmith_artifact_status": "partial",
        "langsmith_telemetry_status": "flowing",
        "langsmith_artifact_recommendation": "fix fetch",
    }
    learning = {"zero_observation_cells": 3, "route_coverage": {"stage4_complete": False}}
    alert = {
        "key": "cadence_failure:ledger-reconcile",
        "severity": "warn",
        "message": "m",
        "detail": {"retry_after_s": 30, "gate": "backoff"},
    }
    gated = dashboard._classify_alert_actionability(
        alert,
        {
            "data_health": health,
            "learning": learning,
            "exploration_backfill_plan": {},
            "exploration_evidence_plan": {},
        },
    )
    assert gated["status"] == "gated"

    alert_no_retry = {**alert, "detail": {"log_path": "/tmp/log"}}
    actionable = dashboard._classify_alert_actionability(
        alert_no_retry,
        {
            "data_health": health,
            "learning": learning,
            "exploration_backfill_plan": {},
            "exploration_evidence_plan": {},
        },
    )
    assert actionable["status"] == "actionable"

    for key, expected in [
        ("low_outcome_coverage", "actionable"),
        ("research_unevaluated_backlog_cap", "actionable"),
        ("langsmith_artifact_distribution", "actionable"),
        ("capacity_gemini", "actionable"),
        ("capacity_aider", "actionable"),
        ("process_renovate", "actionable"),
        ("no_evidence_types", "actionable"),
        ("evidence_type_prune_candidates", "actionable"),
        ("prior_only_cells", "gated"),
        ("dry_seam_outcomes", "actionable"),
        ("keepalive_supervisor_gate", "informational"),
        ("unknown_warn", "actionable"),
    ]:
        sample = {
            "key": key,
            "severity": "warn",
            "message": "m",
            "detail": {
                "state": "warn",
                "policy": "window-soft-cap",
                "reason": "r",
                "clustered_proposals": 1,
            },
        }
        item = dashboard._classify_alert_actionability(
            sample,
            {
                "data_health": health,
                "learning": learning,
                "exploration_backfill_plan": {"active_backfill_eligible": False},
                "exploration_evidence_plan": {"recommended_task_types": []},
            },
        )
        assert item["status"] == expected, key


def test_dashboard_markdown_and_main(monkeypatch, tmp_path, capsys):
    report = _dash_report()
    report["data_health"] = {
        "langsmith_artifact_status": "partial",
        "langsmith_telemetry_status": "flowing",
        "langsmith_artifact_per_repo_found": 1,
        "langsmith_artifact_expected_repos": 2,
        "langsmith_artifact_registered_repos": 1,
        "langsmith_artifact_exempted_repos": 0,
        "langsmith_artifact_missing_with_recent_runs": 0,
        "langsmith_artifact_missing_with_recent_producer_runs": 0,
        "langsmith_artifact_rollup_found": True,
        "langsmith_telemetry_cost_rows": 1,
        "langsmith_telemetry_trace_rows": 1,
    }
    monkeypatch.setattr(dashboard, "_cadence_health", lambda **kw: {"steps": []})
    dash = dashboard.build_dashboard(
        report,
        {
            "agents": {
                "gemini": {
                    "state": "ok",
                    "used_5h": 1.0,
                    "soft_units_5h": 2.0,
                    "used_weekly": 1.0,
                    "soft_units_weekly": 4.0,
                }
            }
        },
    )
    dash["alerts"] = []
    dash["actionability"] = {"actionable": [], "gated": [], "informational_count": 0}
    text = dashboard.format_markdown(dash)
    assert "weekly=25.0%" in text
    assert "- None" in text.split("## Alerts")[-1]

    monkeypatch.setattr(dashboard, "_selftest", lambda: None)
    assert dashboard.main(["--selftest"]) == 0

    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    monkeypatch.setattr(
        dashboard.periodic_report,
        "build_report_from_snapshot",
        lambda *a, **k: report,
    )
    monkeypatch.setattr(dashboard.capacity, "build", lambda: {})
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    assert (
        dashboard.main(
            [
                "--snapshot-json",
                str(snap),
                "--json",
                "--write-json",
                str(out_json),
                "--write-markdown",
                str(out_md),
                "--no-capacity",
            ]
        )
        == 0
    )
    assert out_json.exists() and out_md.exists()
    assert json.loads(out_json.read_text())


def test_filter_backlog_skip_reasons():
    plan = {
        "candidate_task_types": [{"task_type": "implement", "recommended": True}],
    }
    items = [
        {"target": "o/r#1", "task_type": "implement", "lane": "opener"},
        {"target": "o/r#2", "task_type": "epic", "lane": "opener"},
        {"target": "o/r#3", "task_type": "implement", "lane": "closer"},
    ]
    selected, skipped = ec._filter_backlog(plan, {"items": items}, 5, exclude_targets={"o/r#1"})
    reasons = {row["target"]: row["reason"] for row in skipped}
    assert reasons["o/r#1"] == "target excluded from this supervised window"
    assert reasons["o/r#2"] == "task type not recommended for this collection window"
    assert reasons["o/r#3"] == "not opener lane"
    assert selected == []


def test_release_assignment_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(claims, "release", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    ec._release_assignments([{"target": "t", "agent": "codex"}])


def test_direct_evidence_prompts_and_attach():
    testgen = ec._direct_evidence_prompt(
        {"target": "o/r#12", "task_type": "testgen", "title": "T", "body": "B"}
    )
    assert testgen and "Closes #12" in testgen
    codemod = ec._direct_evidence_prompt(
        {"target": "o/r#13", "task_type": "codemod", "title": "C", "body": ""}
    )
    assert codemod and "no issue body" in codemod
    assert ec._direct_evidence_prompt({"task_type": "implement"}) is None
    decision = ec._attach_direct_evidence_prompts(
        {"assignments": [{"target": "o/r#12", "task_type": "testgen"}]},
        [{"target": "o/r#12", "task_type": "testgen", "title": "T", "body": "B"}],
    )
    assert decision["assignments"][0]["prompt"]


def test_build_window_acquisition_blocked(monkeypatch):
    monkeypatch.setattr(
        eep,
        "build_plan",
        lambda **kw: _eligible_acquisition(eligible=False),
    )
    blocked = ec.build_window(
        backlog_payload={"items": []},
        capacity_payload=_capacity_payload(),
    )
    assert any("does not currently mark" in r for r in blocked["blocked_reasons"])
    assert any("no eligible low-risk" in r for r in blocked["blocked_reasons"])


def test_exploration_collection_selftest_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "fb.db")
    ec._selftest()


def test_ccusage_measurement_and_match_helpers():
    assert ccusage._number(-1) == 0.0
    assert ccusage.RunWindow("r", "codex", 1, 10).latency_s == 9.0
    window = ccusage.RunWindow("run-a", "codex", 100, 200)
    session = {
        "agent": "vibe",
        "period": "p",
        "metadata": {"lastActivity": "1970-01-01T00:01:50Z"},
    }
    assert ccusage._match_window(session, [window], slack_seconds=0) == (None, "unsupported_agent")
    session["agent"] = "codex"
    session["metadata"] = {}
    assert ccusage._match_window(session, [window], slack_seconds=0) == (
        None,
        "missing_last_activity",
    )
    session["metadata"] = {"lastActivity": "1970-01-01T00:05:00Z"}
    assert ccusage._match_window(session, [window], slack_seconds=0)[1] == "no_matching_run_window"
    session["metadata"] = {"lastActivity": "1970-01-01T00:01:50Z"}
    assert (
        ccusage._match_window(session, [window, window], slack_seconds=0)[1]
        == "ambiguous_run_window"
    )


def test_ccusage_reconcile_write_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    base = 1_700_000_000
    feedback.record_run("run-a", "o/r#1", "implement", "codex", ts=base)
    ccusage.adapters.record_ledger("codex", event="start", run_id="run-a", ts=base)
    ccusage.adapters.record_ledger("codex", event="complete", run_id="run-a", ts=base + 60)
    sessions = [
        {
            "agent": "codex",
            "period": "p1",
            "metadata": {
                "lastActivity": datetime.fromtimestamp(base + 30, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            },
            "inputTokens": 2,
            "outputTokens": 1,
            "totalCost": 0.1,
        }
    ]
    dry = ccusage.reconcile(
        ccusage.adapters.LEDGER, sessions=sessions, dry_run=True, slack_seconds=0
    )
    assert dry["prepared"] == 1
    live = ccusage.reconcile(
        ccusage.adapters.LEDGER, sessions=sessions, dry_run=False, slack_seconds=0
    )
    assert live["written_cost_rows"] == 1
    ccusage._print_summary(live, as_json=True)


def test_ccusage_selftest_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "fb.db")
    monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    ccusage._selftest()


def test_observability_dashboard_selftest_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "fb.db")
    dashboard._selftest()


def test_dashboard_extended_branches(tmp_path, monkeypatch):
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[]")
    assert dashboard._read_json(non_dict) == {}

    report = _dash_report()
    report["capabilities"]["capabilities"]["invoked-cap"] = {
        "status": "wired",
        "last_invocation": 5,
        "event_history": [],
    }
    rows = dashboard._capability_rows(report)
    invoked = next(row for row in rows if row["capability_id"] == "invoked-cap")
    assert invoked["exact_reason"] == "invocation has no joined outcome evidence"

    cadence = {"steps": []}
    no_range = dict(report)
    no_range["range_rollout"] = {}
    activation = dashboard._activation_state(no_range, cadence)
    assert activation["range"]["exact_reason"] == "no current range rollout artifact"

    runtime_status = dict(report)
    runtime_status["runtime_ac_flow"] = {"status": "blocked"}
    activation_status = dashboard._activation_state(runtime_status, cadence)
    assert activation_status["runtime_ac"]["exact_reason"] == "blocked"

    productivity = dashboard._productivity(
        {
            "outcomes": {"rollup": {"pending_durability_count": 1}},
            "production_flow": {"outcome_coverage": 0.5, "status": "flowing"},
            "dry_seams": {"outcome_gap_summary": {"actionable_runs_without_outcome": 0}},
        }
    )
    learning = {"zero_observation_cells": 2}
    route_cov = {
        "stage4_complete": True,
        "raw_zero_observation_cells": 2,
        "default_review_zero_observation_cells": 1,
        "active_backfill_collectable_zero_observation_cells": 0,
    }
    health = {
        "research_unevaluated_cap_reached": True,
        "research_unevaluated_backlog": 3,
        "research_unevaluated_cap": 3,
        "human_calibration_count": 2,
        "human_calibration_ready": True,
        "active_evidence_types": 2,
        "evidence_schema_prune_candidates": 1,
        "langsmith_artifact_status": "dry",
        "langsmith_telemetry_status": "unknown",
        "langsmith_artifact_recommendation": "fix",
        "outcome_gap_actionable": 0,
    }
    cadence_fail = {
        "steps": [
            {
                "key": "ledger-reconcile",
                "failure_count": 4,
                "exact_reason": "failed",
                "retry_state": "backoff",
                "retry_after_s": 10,
            }
        ],
        "durability_sweep_stamp_status": "fresh",
    }
    supervisor = {
        "stage2_ready_for_historical_replay_analysis": True,
        "stage2_ready_for_supervised_apply": False,
        "stage2_live_valid_proposals": 2,
        "stage2_synced_role_outcomes": 0,
        "stage2_unrecorded_live_candidate_count": 0,
        "stage2_live_plan_path": "/tmp/plan.json",
        "stage2_live_plan_age_s": 200000,
        "stage2_live_plan_status": "ok",
        "stage2_live_plan_recommendation": "refresh",
    }
    alerts = dashboard._build_alerts(
        productivity,
        {"agents": [{"agent": "codex", "state": "shed", "reason": "429"}]},
        cadence_fail,
        learning,
        health,
        {
            "signals": [
                {
                    "work_type": "renovate",
                    "severity": "HIGH",
                    "failure_count": 2,
                    "recommendation": "fix",
                }
            ],
            "non_durable_issue_count": 1,
        },
        supervisor,
        {"dry_seams": {"findings": [{"sink": "outcomes", "finding": "gap", "status": "warn"}]}},
        route_cov,
    )
    keys = {a["key"] for a in alerts}
    assert "cadence_failure:ledger-reconcile" in keys
    assert "research_unevaluated_backlog_cap" in keys
    assert "evidence_type_prune_candidates" in keys
    assert "stage2_live_plan_stale" in keys

    dash = dashboard.build_dashboard(report, {"agents": {"codex": {"state": "ok"}}})
    dash["actionability"] = dashboard._classify_actionability(
        [
            {
                "key": "capacity_codex",
                "severity": "warn",
                "message": "m",
                "detail": {"state": "shed"},
            }
        ],
        dash,
    )
    text = dashboard.format_markdown(
        {
            **dash,
            "alerts": [{"severity": "info", "message": "hello"}],
            "actionability": {
                "actionable": [{"key": "k", "next_step": "do"}],
                "gated": [{"key": "g", "next_step": "wait"}],
                "informational_count": 1,
            },
            "scorecard": dash["scorecard"],
            "capacity": {
                "available": True,
                "agents": [{"agent": "codex", "state": "ok", "policy": "p", "used_5h_rate": 0.1}],
            },
            "cadence": {
                "steps": [
                    {
                        "key": "x",
                        "success_status": "retired",
                        "retired": {"line": "off"},
                        "failure_count": 0,
                        "retry_state": "idle",
                        "retry_after_s": 0,
                        "exact_reason": "r",
                        "gate": "g",
                        "next_transition": "n",
                    }
                ]
            },
            "learning": {
                "task_count": 1,
                "cold_start_task_count": 1,
                "divergent_task_count": 1,
                "top_by_task": [
                    {
                        "task_type": "implement",
                        "top_agent": "codex",
                        "cold_start": True,
                        "diverges_from_prior": True,
                        "posterior": 0.1,
                        "n_obs": 0,
                    }
                ],
            },
            "exploration_policy": {
                "current_default": "auto",
                "recommendation": "wait",
                "ready_task_count": 0,
                "task_count": 1,
                "recorded_exploration_evidence": {},
            },
            "exploration_evidence_plan": {"stage": "s", "direct_mode_deficits": []},
            "exploration_backfill_plan": {
                "status": "idle",
                "active_backfill_eligible": False,
                "missing_cell_count": 0,
                "planned_jobs": [],
            },
            "data_health": {
                **report.get("data_health", {}),
                "langsmith_artifact_status": "partial",
                "langsmith_telemetry_status": "flowing",
                "outcome_gap_categories": [{"category": "x", "count": 1, "actionable": True}],
                "feature_total": 1,
                "feature_promotion_candidates": 0,
            },
            "keepalive_supervisor": {
                "recommendation": "keep collecting",
                "live_supervisor_allowed": False,
            },
        }
    )
    assert "cold" in text and "diverged" in text
    assert "Actionable now:" in text
    assert "Gated / waiting:" in text

    monkeypatch.setattr(dashboard.periodic_report, "build_report", lambda **k: report)
    monkeypatch.setattr(dashboard.capacity, "build", lambda: {})
    monkeypatch.setattr(
        dashboard, "build_dashboard", lambda report, cap: {"generated_at": 1, "alerts": []}
    )
    monkeypatch.setattr(dashboard, "format_markdown", lambda dash: "# md\n")
    assert dashboard.main(["--markdown"]) == 0
