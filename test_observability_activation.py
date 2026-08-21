from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import cadence_registry
import capabilities
import feedback
import human_calibration
import observability_dashboard as dashboard


ACCEPTANCE_CADENCE_KEYS = {
    "keepalive-stage2-plan",
    "keepalive-ingest",
    "local-outcomes-ingest",
    "durability-sweep",
    "langsmith-direct",
    "ledger-reconcile",
    "ccusage-reconcile",
    "range-rollout",
    "runtime-ac-flow",
    "relearn",
    "periodic-report",
    "keepalive-shadow",
    "keepalive-backfill",
}


def _minimal_report() -> dict:
    return {
        "generated_at": 1,
        "read_only": True,
        "db_path": "/tmp/fixture.db",
        "window_days": 90,
        "dataset": {
            "table_counts": {
                "runs": 300, "outcomes": 99, "costs": 0,
                "execution_traces": 0, "execution_attempts": 0,
                "completion_events": 0, "influence_edges": 0,
                "human_calibration": 0,
            },
            "route_weights_version": None,
            "previous_route_weights_version": None,
        },
        "route_weights": {"tasks": []},
        "outcomes": {
            "window_days": 90,
            "total": 99,
            "rollup": {
                "runs_total": 300, "outcome_rows": 99, "outcome_coverage": 0.33,
                "merged_count": 0, "durable_success_count": 0,
                "pending_durability_count": 0, "durability_failure_count": 0,
            },
            "by_task_agent_verdict": [],
        },
        "production_flow": {
            "status": "flowing", "production_runs": 100,
            "production_outcomes": 99, "outcome_coverage": 0.99,
            "recent_production_runs": 100, "recent_production_outcomes": 99,
        },
        "dry_seams": {
            "overall": "warn", "status_counts": {"warn": 1}, "findings": [],
            "completion_event_health": {},
            "outcome_gap_summary": {
                "total_runs_without_outcome": 201,
                "actionable_runs_without_outcome": 1,
                "advisory_or_expected_unlinked": 200,
                "categories": [
                    {"category": "outcome_ingest_candidate", "count": 1, "actionable": True},
                    {"category": "offload_or_advisory", "count": 200, "actionable": False},
                ],
            },
        },
        "route_weights": {"tasks": []},
        "execution_profiles": {"profiles": [], "shared_pool_burn": {}},
        "role_activation": {"roles": {}},
        "experiments": {"implementation_arms": [], "missing_arm_outcomes": []},
        "costs_traces": {"worker_model_provenance": {}},
        "research_subjects": {},
        "judge_reliability": {},
        "human_calibration": {},
        "evidence": {"schema_growth": {}, "proposals": []},
        "features": {"total": 0, "promotion_candidates": []},
        "capabilities": {"total": 0, "counts_by_status": {}, "capabilities": {}},
        "process_improvement": {},
        "keepalive_supervisor": {},
        "exploration_policy": {},
        "exploration_evidence_plan": {},
        "exploration_backfill_plan": {},
        "pattern_miner": {"status": {}, "inventory": {}},
    }


def test_all_cadence_fail_stamps_surface(tmp_path: Path) -> None:
    now = int(time.time())
    for key in ACCEPTANCE_CADENCE_KEYS:
        path = tmp_path / f".fail-{key}"
        path.write_text("2\n")
        os.utime(path, (now - 3600, now - 3600))
    health = dashboard._cadence_health(now=now, state_dir=tmp_path)
    failed = {
        row["key"] for row in health["steps"]
        if row["failure_count"] and row["key"] in ACCEPTANCE_CADENCE_KEYS
    }
    assert failed == ACCEPTANCE_CADENCE_KEYS, "11 cadence failure stamps were hidden"
    range_row = next(row for row in health["steps"] if row["key"] == "range-rollout")
    assert range_row["retry_state"] == "backoff"
    assert range_row["retry_after_s"] == 5 * 3600
    assert range_row["exact_reason"] == "range-rollout failed 2 consecutive attempt(s)"


def test_production_denominator_is_headline_not_expected_shadow_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _minimal_report()
    monkeypatch.setattr(dashboard, "_cadence_health", lambda: cadence_registry.inspect_cadence(tmp_path, now=1))
    built = dashboard.build_dashboard(report, capacity_snapshot={})
    assert built["scorecard"]["production_outcome_coverage"] == 0.99, (
        "production headline used the all-run denominator"
    )
    assert built["scorecard"]["all_run_outcome_coverage"] == 0.33
    assert built["scorecard"]["actionable_missing_production_joins"] == 1
    text = dashboard.format_markdown(built)
    assert "Production outcome coverage: 99.0% (99/100); actionable missing joins=1" in text
    assert "All-run outcome rows (diagnostic; expected dry rows separate): 33.0% (99/300)" in text


def test_zero_owner_calibration_emits_no_scoring_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    feedback.record_run("run-1", "owner/repo#1", "implement", "codex", experiment_id="exp-1")
    feedback.record_evaluation("exp-1", "codex", "judge-a", 8.0)
    queue = human_calibration.pending_queue()
    assert queue["owner_action_required"] is False
    assert queue["status"] == "objective_anchor_pending"
    assert queue["items"] and all("command" not in item for item in queue["items"])
    assert "score" not in queue["next_transition"].lower()


def test_exact_activation_reasons_render_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _minimal_report()
    cap = capabilities._blank_capability("matched-capability")
    cap.update(
        {
            "status": "wired", "last_match": 200, "last_invocation": 100,
            "gate_reason": "capacity gate withheld invocation",
            "next_transition": "retry when provider capacity is ok",
            "rollback": {"transition": "retired"},
        }
    )
    report["capabilities"] = {
        "total": 1, "counts_by_status": {"wired": 1},
        "capabilities": {"matched-capability": cap},
    }
    report["range_rollout"] = {
        "eligible": False,
        "blocked_reasons": ["owner/repo#7: target_claimed by research"],
        "blocked_details": [
            {"target": "owner/repo#7", "reason": "target_claimed", "claimed_by": "research"}
        ],
        "claimed_by": {"owner/repo#7": "research"},
        "capacity_rejections": [],
    }
    report["runtime_ac_flow"] = {
        "status": "ineligible", "runtime_ac_live_firing": False,
        "closer_proxy_present": True,
        "reason": "closer lacks runtime-AC label or verification spec",
        "actions": ["wait for an eligible label/spec-backed closer"],
    }
    monkeypatch.setattr(dashboard, "_cadence_health", lambda: cadence_registry.inspect_cadence(tmp_path, now=1))
    built = dashboard.build_dashboard(report, capacity_snapshot={})
    activation = built["activation"]
    assert activation["capabilities"][0]["liveness"] == "matched_not_invoked"
    assert activation["runtime_ac"]["exact_reason"] == "closer lacks runtime-AC label or verification spec"
    assert activation["range"]["exact_reason"] == "owner/repo#7: target_claimed by research"
    text = dashboard.format_markdown(built)
    assert "liveness=matched_not_invoked" in text
    assert "closer lacks runtime-AC label or verification spec" in text
    assert "owner/repo#7: target_claimed by research" in text
