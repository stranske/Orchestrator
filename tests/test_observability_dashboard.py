"""Hermetic coverage for autopilot round #1182 (exploration_collection, observability_dashboard, ccusage_reconcile)."""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import tempfile

import advisor_hermetic
import pytest

import adapters
import feedback
import observability_dashboard as dashboard


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_hermetic, "root", lambda: tmp_path)
    with advisor_hermetic.activated(monkeypatch):
        monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
        monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
        monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "feedback.db")
        # HANDOFF is captured outside ORCH_LOCAL_RUNTIME at import time.
        monkeypatch.setattr(adapters, "HANDOFF", tmp_path / "handoff")
        monkeypatch.setattr(adapters, "LEDGER", tmp_path / "handoff" / "ledger.ndjson")
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
                body = json.dumps({"content": base64.b64encode(raw).decode("ascii")})
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd and cmd[0] == "agy" and "models" in cmd:
                # Offline catalog probe shape (tab-separated id + label).
                body = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd and cmd[0] == "cursor-agent" and "--list-models" in cmd:
                body = "composer-2.5 - Composer 2.5 (current)\n"
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd:
                joined = " ".join(str(c) for c in cmd)
                if len(cmd) >= 3 and cmd[0] == "codex" and cmd[1] == "login" and cmd[2] == "status":
                    return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
                if len(cmd) >= 3 and cmd[0] == "claude" and cmd[1] == "auth" and cmd[2] == "status":
                    return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
                if "ccusage" in joined and ("session" in joined or "blocks" in joined):
                    if "blocks" in joined:
                        body = json.dumps({"blocks": []})
                    else:
                        body = "{}"
                    return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            attempted_external.append((cmd, kwargs))
            pytest.fail("tests/test_coverage_round_1182.py attempted unstubbed external I/O")

        def forbid_external(*args, **kwargs):
            attempted_external.append((args, kwargs))
            pytest.fail("tests/test_coverage_round_1182.py attempted external I/O")

        monkeypatch.setattr(subprocess, "run", guarded_subprocess_run)
        monkeypatch.setattr(subprocess, "Popen", forbid_external)
        monkeypatch.setattr(socket.socket, "connect", forbid_external)
        monkeypatch.setattr(socket.socket, "connect_ex", forbid_external)
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
