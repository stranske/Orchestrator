#!/usr/bin/env python3
"""observability_dashboard.py - compact operator dashboard for Orchestrator health.

This is the first dashboard layer on top of periodic_report.py. It stays
read-only, reuses the periodic report as its data source, and adds the legible
scorecard an operator needs: productivity, quality/durability, live capacity,
learning coverage, and alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import cadence_registry
import capabilities
import capacity
import features
import feedback
import periodic_report

DEFAULT_STATE_DIR = Path.home() / ".codex" / "orchestrator"


def _rate(numerator: float | None, denominator: float | None) -> float | None:
    if denominator in (None, 0) or numerator is None:
        return None
    return float(numerator) / float(denominator)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _alert(
    alerts: list[dict],
    severity: str,
    key: str,
    message: str,
    detail: dict | None = None,
) -> None:
    alerts.append(
        {
            "severity": severity,
            "key": key,
            "message": message,
            "detail": detail or {},
        }
    )


def _productivity(report: dict) -> dict:
    outcomes = report.get("outcomes") or {}
    rollup = outcomes.get("rollup") or {}
    production_flow = report.get("production_flow") or {}
    production_runs = int(production_flow.get("production_runs") or 0)
    production_outcomes = int(production_flow.get("production_outcomes") or 0)
    production_coverage = production_flow.get("outcome_coverage")
    actionable_missing = int(
        ((report.get("dry_seams") or {}).get("outcome_gap_summary") or {}).get(
            "actionable_runs_without_outcome", 0
        )
    )
    return {
        "window_days": outcomes.get("window_days"),
        # Operator headline: production joins only. Advisory/offload/role/experiment
        # rows are separate denominators and must not make healthy production look dry.
        "runs": production_runs,
        "outcomes": production_outcomes,
        "outcome_coverage": production_coverage,
        "production_runs": production_runs,
        "production_outcomes": production_outcomes,
        "production_outcome_coverage": production_coverage,
        "actionable_missing_production_joins": actionable_missing,
        "all_runs": rollup.get("runs_total", 0),
        "all_outcome_rows": rollup.get("outcome_rows", outcomes.get("total", 0)),
        "all_run_outcome_coverage": rollup.get("outcome_coverage"),
        "pass_count": rollup.get("pass_count", 0),
        "fail_count": rollup.get("fail_count", 0),
        "merged_count": rollup.get("merged_count", 0),
        "merged_rate": rollup.get("merged_rate"),
        "durable_success_count": rollup.get("durable_success_count", 0),
        "durable_success_rate": rollup.get("durable_success_rate"),
        "pending_durability_count": rollup.get("pending_durability_count", 0),
        "durability_failure_count": rollup.get("durability_failure_count", 0),
        "durability_failure_rate": rollup.get("durability_failure_rate"),
        "by_source_assignment": outcomes.get("by_source_assignment") or [],
        "production_flow": production_flow,
        "production_flow_status": production_flow.get("status"),
        "recent_production_runs": production_flow.get("recent_production_runs", 0),
        "recent_production_outcomes": production_flow.get("recent_production_outcomes", 0),
        "latest_production_run_age_days": production_flow.get("latest_run_age_days"),
    }


def _capacity(capacity_snapshot: dict | None) -> dict:
    if not capacity_snapshot:
        return {"available": False, "state_counts": {}, "agents": []}
    state_counts: dict[str, int] = {}
    agents = []
    for agent, row in (capacity_snapshot.get("agents") or {}).items():
        state = row.get("state") or "unknown"
        state_counts[state] = state_counts.get(state, 0) + 1
        used_5h = row.get("used_5h")
        cap_5h = row.get("soft_units_5h") or row.get("window_soft_cap")
        used_weekly = row.get("used_weekly")
        cap_weekly = row.get("soft_units_weekly") or row.get("weekly_soft_cap")
        agents.append(
            {
                "agent": agent,
                "state": state,
                "policy": row.get("policy"),
                "window": row.get("window"),
                "reason": row.get("reason"),
                "availability": row.get("availability"),
                "next_action": row.get("next_action"),
                "minutes_to_window_refresh": row.get("minutes_to_window_refresh"),
                "used_5h": used_5h,
                "soft_units_5h": cap_5h,
                "used_5h_rate": _rate(used_5h, cap_5h),
                "used_weekly": used_weekly,
                "soft_units_weekly": cap_weekly,
                "used_weekly_rate": _rate(used_weekly, cap_weekly),
            }
        )
    order = {"shed": 0, "warn": 1, "unknown": 2, "ok": 3}
    agents.sort(key=lambda item: (order.get(item["state"], 2), item["agent"]))
    return {
        "available": True,
        "generated_at": capacity_snapshot.get("generated_at"),
        "ccusage_active": capacity_snapshot.get("ccusage_active"),
        "state_counts": state_counts,
        "agents": agents,
    }


def _cadence_health(now: int | None = None, state_dir: Path | None = None) -> dict:
    root = state_dir or Path(os.environ.get("ORCH_STATE_DIR", str(DEFAULT_STATE_DIR)))
    return cadence_registry.inspect_cadence(root, now=now)


def _read_json(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _capability_rows(report: dict) -> list[dict]:
    declared = (report.get("capabilities") or {}).get("capabilities") or {}
    rows = []
    for capability_id, cap in sorted(declared.items()):
        events = cap.get("event_history") or []
        last_outcome = (
            max(
                (int(row.get("timestamp") or 0) for row in events if row.get("type") == "outcome"),
                default=0,
            )
            or None
        )
        liveness = capabilities.classify_liveness(cap)
        if liveness == "matched_not_invoked":
            exact_reason = "matching work was observed after the last invocation"
        elif liveness == "invoked_without_outcomes":
            exact_reason = "invocation has no joined outcome evidence"
        elif liveness == "no_matching_work":
            exact_reason = "no matching work has been observed"
        else:
            exact_reason = cap.get("gate_reason") or liveness
        rows.append(
            {
                "capability_id": capability_id,
                "status": cap.get("status"),
                "liveness": liveness,
                "exact_reason": exact_reason,
                "gate_reason": cap.get("gate_reason"),
                "last_match": cap.get("last_match"),
                "last_invocation": cap.get("last_invocation"),
                "last_success": cap.get("last_success"),
                "last_outcome": last_outcome,
                "outcome_links": len(cap.get("outcome_links") or []),
                "expiry": cap.get("expiry"),
                "next_transition": cap.get("next_transition"),
                "kill_switch": cap.get("kill_switch"),
                "rollback": cap.get("rollback"),
                "upstream_sync": {
                    "predecessor": cap.get("predecessor"),
                    "successor": cap.get("successor"),
                },
            }
        )
    return rows


def _activation_state(report: dict, cadence: dict) -> dict:
    range_step: Any = next(
        (row for row in cadence.get("steps") or [] if row.get("key") == "range-rollout"),
        {},
    )
    range_state = report.get("range_rollout") or _read_json(
        Path(range_step["artifact_path"]) if range_step.get("artifact_path") else None
    )
    blocked_reasons = list(range_state.get("blocked_reasons") or [])
    blocked_details = list(range_state.get("blocked_details") or [])
    if range_state.get("eligible"):
        range_reason = "eligible range work is available"
        range_next = "dispatch remains bounded by apply, confirmation, cap, and kill switch"
    elif blocked_reasons:
        range_reason = blocked_reasons[0]
        range_next = "retry when the reported claim, capacity, scope, or cap condition clears"
    else:
        range_reason = "no current range rollout artifact"
        range_next = "next cadence preview refreshes exact eligibility"

    runtime = report.get("runtime_ac_flow") or report.get("runtime_ac_state") or {}
    runtime_reason = runtime.get("reason")
    if not runtime_reason:
        alerts = runtime.get("alerts") or []
        runtime_reason = alerts[0].get("message") if alerts else None
    if not runtime_reason:
        if runtime.get("closer_proxy_present") and not runtime.get("runtime_ac_live_firing"):
            runtime_reason = "closer traffic exists but no eligible runtime-AC event fired"
        elif runtime.get("status"):
            runtime_reason = str(runtime.get("status"))
        else:
            runtime_reason = "runtime-AC flow state unavailable"

    experiments = report.get("experiments") or {}
    missing_arm_outcomes = experiments.get("missing_arm_outcomes") or []
    compiler = report.get("pattern_miner") or {}
    compiler_inventory = compiler.get("inventory") or {}
    compiler_status = compiler.get("status") or {}
    capability_rows = _capability_rows(report)
    role_rows = (report.get("role_activation") or {}).get("roles") or {}
    profiles = report.get("execution_profiles") or {}
    provenance = (report.get("costs_traces") or {}).get("worker_model_provenance") or {}
    completion = (report.get("dry_seams") or {}).get("completion_event_health") or {}
    outcomes_rollup = (report.get("outcomes") or {}).get("rollup") or {}
    production = report.get("production_flow") or {}
    experiment_observations = sum(
        int(row.get("evaluation_observations") or 0)
        for row in experiments.get("implementation_arms") or []
    )
    capability_invoked = sum(bool(row.get("last_invocation")) for row in capability_rows)
    capability_with_outcomes = sum(
        bool(row.get("last_outcome") or row.get("outcome_links")) for row in capability_rows
    )
    role_runs = sum(int(row.get("role_runs") or 0) for row in role_rows.values())
    role_linked = sum(int(row.get("linked") or 0) for row in role_rows.values())
    feature_report = report.get("features") or {}
    return {
        "capabilities": capability_rows,
        "roles": role_rows,
        "profiles": report.get("execution_profiles") or {},
        "model_provenance": provenance,
        "experiments": {
            **experiments,
            "promotion_state": (
                "blocked_missing_arm_outcomes" if missing_arm_outcomes else "evidence_complete"
            ),
            "next_transition": (
                "run bounded experiment followup/evaluation"
                if missing_arm_outcomes
                else "promotion policy may evaluate complete arm evidence"
            ),
        },
        "compiler": {
            "status": compiler_status.get("status") or "unknown",
            "emitted_candidates": compiler_inventory.get("emitted_candidate_count", 0),
            "expired_candidates": compiler_inventory.get("expired_candidate_count", 0),
            "tombstones": compiler_inventory.get("tombstone_count", 0),
            "next_actions": compiler_inventory.get("next_actions") or [],
        },
        "evidence_contracts": {
            "schema_state": (
                ((report.get("evidence") or {}).get("schema_growth") or {}).get("status")
            ),
            "candidate_count": len((report.get("evidence") or {}).get("proposals") or []),
            "next_transition": (
                ((report.get("evidence") or {}).get("schema_growth") or {}).get("recommendation")
            ),
        },
        "range": {
            "eligible": range_state.get("eligible"),
            "exact_reason": range_reason,
            "next_transition": range_next,
            "blocked_reasons": blocked_reasons,
            "blocked_details": blocked_details,
            "claimed_by": range_state.get("claimed_by") or {},
            "capacity_rejections": range_state.get("capacity_rejections") or [],
        },
        "runtime_ac": {
            "status": runtime.get("status") or "unknown",
            "eligible_event_fired": bool(runtime.get("runtime_ac_live_firing")),
            "required_event_denominator": int(runtime.get("required_event_denominator") or 0),
            "executed_gate_numerator": int(runtime.get("executed_gate_numerator") or 0),
            "closer_proxy_present": bool(runtime.get("closer_proxy_present")),
            "closer_proxy_is_diagnostic_only": bool(
                runtime.get("closer_proxy_is_diagnostic_only", True)
            ),
            "target_spec_attribution_alert": bool(runtime.get("target_spec_attribution_alert")),
            "materialization_alert": bool(runtime.get("materialization_alert")),
            "exact_reason": runtime_reason,
            "next_transitions": runtime.get("actions") or [],
            "gate_history": runtime.get("gate_history") or {},
        },
        "research": {
            "duplicate_rejections": (report.get("research_subjects") or {}).get(
                "duplicate_rejections", 0
            ),
            "unevaluated_backlog": (report.get("research_subjects") or {}).get(
                "unevaluated_backlog", 0
            ),
            "unevaluated_cap": (report.get("research_subjects") or {}).get("unevaluated_cap", 0),
            "production_collisions": (report.get("research_subjects") or {}).get(
                "research_production_collisions", 0
            ),
            "rejections_by_reason": (report.get("research_subjects") or {}).get(
                "rejections_by_reason"
            )
            or {},
        },
        "denominators": {
            "production_outcomes": {
                "numerator": int(production.get("production_outcomes") or 0),
                "denominator": int(production.get("production_runs") or 0),
                "coverage": production.get("outcome_coverage"),
            },
            "all_run_outcomes_diagnostic": {
                "numerator": int(outcomes_rollup.get("outcome_rows") or 0),
                "denominator": int(outcomes_rollup.get("runs_total") or 0),
                "coverage": outcomes_rollup.get("outcome_coverage"),
            },
            "experiment_evaluations": {
                "numerator": experiment_observations,
                "denominator": sum(
                    len(row.get("members") or [])
                    for row in experiments.get("implementation_arms") or []
                ),
                "missing": len(missing_arm_outcomes),
            },
            "role_outcome_links": {
                "numerator": role_linked,
                "denominator": role_runs,
                "durable": sum(int(row.get("durable") or 0) for row in role_rows.values()),
            },
            "offload_cost_rows": {
                "numerator": int(
                    ((report.get("dataset") or {}).get("table_counts") or {}).get("costs", 0)
                ),
                "denominator": int(
                    ((report.get("dataset") or {}).get("table_counts") or {}).get("runs", 0)
                ),
                "note": "all retained cost rows; offload source detail remains in costs_by_source",
            },
            "synthesis_promotion": {
                "numerator": len(feature_report.get("promotion_candidates") or []),
                "denominator": int(feature_report.get("total") or 0),
            },
            "completion_events": {
                "numerator": int(completion.get("complete") or 0),
                "denominator": int(completion.get("total") or 0),
                "accepted_linked": int(completion.get("accepted_influence_linked") or 0),
            },
            "worker_model_provenance": {
                "numerator": int(provenance.get("resolved_worker_runs") or 0),
                "denominator": int(provenance.get("eligible_worker_runs") or 0),
                "unknown": int(provenance.get("unknown_worker_runs") or 0),
            },
            "capability_outcomes": {
                "numerator": capability_with_outcomes,
                "denominator": capability_invoked,
            },
            "profile_resolution": {
                "numerator": sum(
                    int(row.get("resolved_attempts") or 0) for row in profiles.get("profiles") or []
                ),
                "denominator": sum(
                    int(row.get("attempts") or 0) for row in profiles.get("profiles") or []
                ),
                "shared_pool": profiles.get("shared_pool_burn") or {},
            },
        },
    }


def _learning(report: dict) -> dict:
    tasks = report.get("route_weights", {}).get("tasks") or []
    top_by_task = []
    cold_start_tasks = []
    divergent_tasks = []
    zero_observation_cells = 0
    observed_cells = 0
    for task in tasks:
        task_type = task.get("task_type")
        if task.get("cold_start"):
            cold_start_tasks.append(task_type)
        if task.get("diverges_from_prior"):
            divergent_tasks.append(task_type)
        rows = task.get("rows") or []
        zero_observation_cells += sum(1 for row in rows if not row.get("n_obs"))
        observed_cells += sum(1 for row in rows if row.get("n_obs"))
        learned_order = task.get("learned_order") or []
        top = learned_order[0] if learned_order else None
        top_row: dict[str, Any] = next((row for row in rows if row.get("agent") == top), {})
        top_by_task.append(
            {
                "task_type": task_type,
                "top_agent": top,
                "posterior": top_row.get("posterior"),
                "score": top_row.get("score"),
                "n_obs": top_row.get("n_obs"),
                "cold_start": bool(task.get("cold_start")),
                "diverges_from_prior": bool(task.get("diverges_from_prior")),
            }
        )
    return {
        "route_weights_version": report.get("dataset", {}).get("route_weights_version"),
        "previous_route_weights_version": report.get("dataset", {}).get(
            "previous_route_weights_version"
        ),
        "task_count": len(tasks),
        "cold_start_task_count": len(cold_start_tasks),
        "cold_start_tasks": cold_start_tasks,
        "divergent_task_count": len(divergent_tasks),
        "divergent_tasks": divergent_tasks,
        "observed_cells": observed_cells,
        "zero_observation_cells": zero_observation_cells,
        "top_by_task": top_by_task,
    }


def _route_coverage_summary(
    learning: dict,
    exploration: dict | None,
    acquisition: dict | None,
    backfill: dict | None,
) -> dict:
    exploration = exploration or {}
    acquisition = acquisition or {}
    backfill = backfill or {}
    review = acquisition.get("exploration_review") or {}
    coverage = acquisition.get("route_coverage_deficits") or {}
    route_tasks = exploration.get("tasks") or coverage.get("tasks") or []
    missing_cells = backfill.get("missing_cells") or []
    planned_jobs = backfill.get("planned_jobs") or []

    default_review_zero_cells = sum(
        len(task.get("zero_observation_agents") or []) for task in route_tasks
    )
    backfill_zero_cells = sum(
        1 for cell in missing_cells if "zero_observation_cell" in (cell.get("reasons") or [])
    )
    planned_zero_cells = sum(
        1
        for job in planned_jobs
        for cell in (job.get("covers_cells") or [])
        if "zero_observation_cell" in (cell.get("reasons") or [])
    )
    active_backfill_eligible = bool(backfill.get("active_backfill_eligible"))
    stage = acquisition.get("stage")
    direct_ready = review.get("direct_ready")
    if direct_ready is None:
        direct_ready = (exploration.get("recorded_exploration_evidence") or {}).get(
            "ready_for_direct_comparison"
        )
    route_ready = review.get("route_ready")
    if route_ready is None and stage == "stage_4_default_review_complete":
        route_ready = True
    stage4_complete = stage == "stage_4_default_review_complete"

    if stage4_complete and not active_backfill_eligible:
        recommendation = (
            "Stage 4 is complete; treat remaining prior-only cells as a learning-quality backlog "
            "and collect real evidence only when safe, non-duplicative subjects exist."
        )
    elif active_backfill_eligible:
        recommendation = (
            "Run one guarded backfill job, then collect and evaluate it before counting evidence."
        )
    else:
        recommendation = "Wait for safe opener subjects or future exploration candidates before collecting route evidence."

    return {
        "raw_zero_observation_cells": int(learning.get("zero_observation_cells") or 0),
        "default_review_zero_observation_cells": default_review_zero_cells,
        "backfill_missing_zero_observation_cells": backfill_zero_cells,
        "active_backfill_collectable_zero_observation_cells": (
            planned_zero_cells if active_backfill_eligible else 0
        ),
        "backfill_missing_cells": int(backfill.get("missing_cell_count") or 0),
        "active_backfill_eligible": active_backfill_eligible,
        "route_ready": bool(route_ready),
        "direct_ready": bool(direct_ready),
        "acquisition_stage": stage,
        "stage4_complete": stage4_complete,
        "recommendation": recommendation,
    }


def _data_health(report: dict) -> dict:
    dataset = report.get("dataset") or {}
    counts = dataset.get("table_counts") or {}
    costs = report.get("costs_traces") or {}
    artifact_health = costs.get("langsmith_artifact_distribution") or {}
    langsmith_telemetry = costs.get("langsmith_telemetry") or {}
    worker_provenance = costs.get("worker_model_provenance") or {}
    research_subjects = report.get("research_subjects") or {}
    profile_report = report.get("execution_profiles") or {}
    model_trial = report.get("model_profile_trial") or {}
    transport_qualification = report.get("model_profile_transport_qualification") or {}
    reliability = report.get("judge_reliability") or {}
    calibration = report.get("human_calibration") or {}
    evidence = report.get("evidence") or {}
    schema_growth = evidence.get("schema_growth") or {}
    active_review = schema_growth.get("active_type_review") or {}
    dry = report.get("dry_seams") or {}
    completion_health = dry.get("completion_event_health") or {}
    outcome_gaps = dry.get("outcome_gap_summary") or {}
    features_report = report.get("features") or {}
    capabilities_report = report.get("capabilities") or {}
    role_activation = report.get("role_activation") or {}
    return {
        "table_counts": counts,
        "cost_rows": counts.get("costs", 0),
        "trace_rows": counts.get("execution_traces", 0),
        "execution_attempt_rows": counts.get("execution_attempts", 0),
        "completion_event_rows": counts.get("completion_events", 0),
        "influence_edge_rows": counts.get("influence_edges", 0),
        "completion_event_health": completion_health,
        "role_activation": role_activation,
        "cost_sources": costs.get("costs_by_source") or [],
        "trace_status_counts": costs.get("trace_status_counts") or [],
        "langsmith_artifact_status": artifact_health.get("status"),
        "langsmith_artifact_registered_repos": artifact_health.get("registered_repos", 0),
        "langsmith_artifact_expected_repos": artifact_health.get(
            "expected_repos", artifact_health.get("registered_repos", 0)
        ),
        "langsmith_artifact_exempted_repos": artifact_health.get("exempted_repos", 0),
        "langsmith_artifact_visible_found": artifact_health.get(
            "visible_artifacts_found",
            artifact_health.get("per_repo_artifacts_found", 0),
        ),
        "langsmith_artifact_per_repo_found": artifact_health.get("per_repo_artifacts_found", 0),
        "langsmith_artifact_missing_with_recent_runs": artifact_health.get(
            "missing_expected_with_recent_runs", 0
        ),
        "langsmith_artifact_missing_with_recent_producer_runs": artifact_health.get(
            "missing_expected_with_recent_producer_runs", 0
        ),
        "langsmith_artifact_missing_without_recent_runs": artifact_health.get(
            "missing_expected_without_recent_runs", 0
        ),
        "langsmith_artifact_missing_diagnostic_errors": artifact_health.get(
            "missing_expected_diagnostic_errors", 0
        ),
        "langsmith_artifact_rollup_found": bool(artifact_health.get("rollup_artifact_found")),
        "langsmith_artifact_recommendation": artifact_health.get("recommendation"),
        "langsmith_telemetry_status": langsmith_telemetry.get("status"),
        "langsmith_telemetry_cost_rows": langsmith_telemetry.get("cost_rows", 0),
        "langsmith_telemetry_trace_rows": langsmith_telemetry.get("trace_rows", 0),
        "langsmith_telemetry_recommendation": langsmith_telemetry.get("recommendation"),
        "worker_provenance_runs_total": worker_provenance.get("runs_total", 0),
        "eligible_worker_runs": worker_provenance.get("eligible_worker_runs", 0),
        "excluded_nonworker_runs": worker_provenance.get("excluded_nonworker_runs", 0),
        "requested_worker_runs": worker_provenance.get("requested_worker_runs", 0),
        "resolved_worker_runs": worker_provenance.get("resolved_worker_runs", 0),
        "unknown_worker_runs": worker_provenance.get("unknown_worker_runs", 0),
        "requested_worker_coverage": worker_provenance.get("requested_worker_coverage"),
        "resolved_worker_coverage": worker_provenance.get("resolved_worker_coverage"),
        "unknown_worker_coverage": worker_provenance.get("unknown_worker_coverage"),
        "attempts_by_operation_role": worker_provenance.get("attempts_by_operation_role") or {},
        "worker_evaluator_role_overlap_runs": worker_provenance.get(
            "worker_evaluator_role_overlap_runs", 0
        ),
        "worker_evaluator_resolved_model_collision_runs": worker_provenance.get(
            "worker_evaluator_resolved_model_collision_runs", 0
        ),
        "legacy_worker_nonworker_model_collision_runs": worker_provenance.get(
            "legacy_worker_nonworker_model_collision_runs", 0
        ),
        "unmigrated_legacy_trace_rows": worker_provenance.get("unmigrated_legacy_trace_rows", 0),
        "legacy_execution_attempt_migration_complete": bool(
            worker_provenance.get("legacy_migration_complete")
        ),
        "capability_total": capabilities_report.get("total", 0),
        "capability_counts_by_status": capabilities_report.get("counts_by_status") or {},
        "capability_active_without_edges": capabilities_report.get("active_without_edges") or [],
        "research_subject_count": research_subjects.get("registered_subjects", 0),
        "research_independent_subject_count": research_subjects.get("independent_subjects", 0),
        "research_unevaluated_backlog": research_subjects.get("unevaluated_backlog", 0),
        "research_unevaluated_cap": research_subjects.get("unevaluated_cap", 0),
        "research_unevaluated_cap_reached": bool(
            research_subjects.get("unevaluated_backlog_cap_reached")
        ),
        "research_duplicate_rejections": research_subjects.get("duplicate_rejections", 0),
        "research_production_collisions": research_subjects.get(
            "research_production_collisions", 0
        ),
        "research_true_task_type_distribution": research_subjects.get("true_task_type_distribution")
        or {},
        "research_rejections_by_reason": research_subjects.get("rejections_by_reason") or {},
        "research_effective_sample_count": research_subjects.get("effective_sample_count", 0.0),
        "execution_profile_ready": profile_report.get("ready_profiles", 0),
        "execution_profile_cold_starts": profile_report.get("cold_starts", 0),
        "execution_profile_resolved_coverage": {
            row.get("profile_id"): row.get("resolved_model_coverage", 0.0)
            for row in profile_report.get("profiles") or []
        },
        "execution_profile_fallback_rate": {
            row.get("profile_id"): row.get("fallback_rate", 0.0)
            for row in profile_report.get("profiles") or []
        },
        "execution_profile_evidence_age_days": {
            row.get("profile_id"): row.get("evidence_age_days")
            for row in profile_report.get("profiles") or []
        },
        "execution_profile_shared_pool_burn": profile_report.get("shared_pool_burn") or {},
        "execution_profile_mean_propensity": profile_report.get("mean_assignment_probability", 0.0),
        "execution_profile_weight_reads_enabled": bool(
            profile_report.get("profile_weight_reads_enabled")
        ),
        "model_profile_trial_status": model_trial.get("status", "not_run"),
        "model_profile_trial_ready": bool(model_trial.get("ready")),
        "model_profile_trial_lifecycle": model_trial.get("lifecycle", "shadow"),
        "model_profile_trial_attempts": int(model_trial.get("attempt_count") or 0),
        "model_profile_trial_source_unchanged": bool(
            (model_trial.get("source_integrity") or {}).get("unchanged")
        ),
        "model_profile_trial_shared_pool_debit": (model_trial.get("shared_pool_debit") or {}),
        "model_profile_trial_learning_enabled": bool(model_trial.get("learning_enabled")),
        "model_profile_transport_qualification_status": transport_qualification.get(
            "status", "not_qualified"
        ),
        "model_profile_transport_contract_qualified": bool(
            transport_qualification.get("transport_contract_qualified")
        ),
        "model_profile_provider_identity_status": transport_qualification.get(
            "provider_identity_status", "unavailable_unclaimed"
        ),
        "model_profile_qualification_learning_enabled": bool(
            transport_qualification.get("learning_enabled")
        ),
        "model_profile_qualification_quality_weight_updates_allowed": bool(
            transport_qualification.get("quality_weight_updates_allowed")
        ),
        "judge_ready": bool(reliability.get("ready")),
        "judge_count": reliability.get("judge_count", 0),
        "ready_judge_count": reliability.get("ready_judge_count", 0),
        "open_gap_kinds": len(evidence.get("open_gaps_by_recurrence") or []),
        "evidence_proposals": len(evidence.get("proposals") or []),
        "evidence_schema_status": schema_growth.get("status"),
        "evidence_schema_clustered_proposals": schema_growth.get("clustered_proposal_count", 0),
        "evidence_schema_prune_candidates": active_review.get("prune_candidate_count", 0),
        "evidence_schema_active_review": active_review.get("active") or [],
        "evidence_schema_recommendation": schema_growth.get("recommendation"),
        "active_evidence_types": (
            evidence.get("evidence_types", {}).get("counts_by_status") or {}
        ).get("active", 0),
        "human_calibration_count": counts.get("human_calibration", 0),
        "human_calibration_status": calibration.get("status"),
        "human_calibration_ready": bool(calibration.get("ready")),
        "human_calibration_pairs": calibration.get("matched_pair_count", 0),
        "human_calibration_recommendation": calibration.get("recommendation"),
        "dry_seams_overall": dry.get("overall"),
        "dry_seam_counts": dry.get("status_counts") or {},
        "outcome_gap_total": outcome_gaps.get("total_runs_without_outcome", 0),
        "outcome_gap_actionable": outcome_gaps.get("actionable_runs_without_outcome", 0),
        "outcome_gap_advisory_or_unlinked": outcome_gaps.get("advisory_or_expected_unlinked", 0),
        "outcome_gap_categories": outcome_gaps.get("categories") or [],
        "feature_total": features_report.get("total", 0),
        "feature_promotion_candidates": len(features_report.get("promotion_candidates") or []),
    }


def _process_improvement(report: dict) -> dict:
    process = report.get("process_improvement") or {}
    signals = process.get("signals") or []
    issue_failures = process.get("non_durable_issue_runs") or []
    reviewed_issue_failures = process.get("reviewed_issue_failures") or []
    suppressed = process.get("suppressed_process_failures") or []
    return {
        "window_days": process.get("window_days"),
        "work_type_rollup": process.get("work_type_rollup") or [],
        "non_agent_by_work_type": process.get("non_agent_by_work_type") or [],
        "signals": signals,
        "signal_count": len(signals),
        "suppressed_process_failures": suppressed,
        "suppressed_process_failure_count": len(suppressed),
        "non_durable_issue_runs": issue_failures,
        "non_durable_issue_count": len(issue_failures),
        "reviewed_issue_failures": reviewed_issue_failures,
        "reviewed_issue_failure_count": len(reviewed_issue_failures),
    }


def _keepalive_supervisor(report: dict) -> dict:
    supervisor = report.get("keepalive_supervisor") or {}
    summary = supervisor.get("summary") or {}
    thresholds = supervisor.get("thresholds") or {}
    stage2 = supervisor.get("stage2_proposal_corpus") or {}
    stage2_summary = stage2.get("summary") or {}
    live_plan = stage2.get("live_plan") or {}
    return {
        "status": supervisor.get("status"),
        "live_supervisor_allowed": bool(supervisor.get("live_supervisor_allowed")),
        "recommendation": supervisor.get("recommendation"),
        "corpus_path": supervisor.get("corpus_path"),
        "labeled_outcomes": summary.get("labeled_outcomes", 0),
        "failure_outcomes": summary.get("failure_outcomes", 0),
        "meaningful_disagreements": summary.get("meaningful_disagreements", 0),
        "raw_disagreement_rate": summary.get("raw_disagreement_rate"),
        "disagreement_rate": summary.get("disagreement_rate"),
        "thresholds": thresholds,
        "stage2_status": stage2.get("status"),
        "stage2_ready_for_supervised_apply": bool(stage2.get("ready_for_supervised_apply")),
        "stage2_ready_for_historical_replay_analysis": bool(
            stage2.get("ready_for_historical_replay_analysis")
        ),
        "stage2_corpus_path": stage2.get("corpus_path"),
        "stage2_valid_proposals": stage2_summary.get("valid_proposals", 0),
        "stage2_readiness_target": stage2_summary.get("readiness_target"),
        "stage2_live_dispatches": stage2_summary.get("live_dispatches", 0),
        "stage2_live_valid_proposals": stage2_summary.get("live_valid_proposals", 0),
        "stage2_live_invalid_or_fallback_proposals": stage2_summary.get(
            "live_invalid_or_fallback_proposals", 0
        ),
        "stage2_outcome_links": stage2_summary.get("outcome_links", 0),
        "stage2_accepted_links": stage2_summary.get("accepted_links", 0),
        "stage2_synced_role_outcomes": stage2_summary.get("synced_role_outcomes", 0),
        "stage2_linked_outcome_target": stage2_summary.get("linked_outcome_target"),
        "stage2_linked_disagreements": stage2_summary.get("linked_disagreements", 0),
        "stage2_disagreement_outcome_target": stage2_summary.get("disagreement_outcome_target"),
        "stage2_historical_linked_disagreements": stage2_summary.get(
            "historical_linked_disagreements", 0
        ),
        "stage2_historical_candidates_remaining": stage2.get("historical_candidates_remaining", 0),
        "stage2_calibration_candidates_remaining": stage2.get(
            "calibration_candidates_remaining", 0
        ),
        "stage2_recommendation": stage2.get("recommendation"),
        "stage2_live_plan_path": live_plan.get("path"),
        "stage2_live_plan_status": live_plan.get("status"),
        "stage2_live_plan_age_s": live_plan.get("age_s"),
        "stage2_live_candidate_count": live_plan.get("live_candidate_count", 0),
        "stage2_eligible_live_candidate_count": live_plan.get("eligible_live_candidate_count", 0),
        "stage2_unrecorded_live_candidate_count": live_plan.get(
            "unrecorded_live_candidate_count", 0
        ),
        "stage2_live_targets": live_plan.get("live_targets") or [],
        "stage2_live_commands": live_plan.get("commands") or [],
        "stage2_live_plan_recommendation": live_plan.get("recommendation"),
    }


def _build_alerts(
    productivity: dict,
    capacity_summary: dict,
    cadence: dict,
    learning: dict,
    health: dict,
    process: dict,
    supervisor: dict,
    report: dict,
    route_coverage: dict | None = None,
) -> list[dict]:
    alerts: list[dict] = []
    route_coverage = route_coverage or _route_coverage_summary(
        learning,
        report.get("exploration_policy") or {},
        report.get("exploration_evidence_plan") or {},
        report.get("exploration_backfill_plan") or {},
    )
    for step in cadence.get("steps") or []:
        if not step.get("failure_count"):
            continue
        _alert(
            alerts,
            "ERROR" if int(step.get("failure_count") or 0) >= 3 else "WARN",
            f"cadence_failure:{step.get('key')}",
            (
                f"{step.get('exact_reason')}; retry={step.get('retry_state')} "
                f"retry_after_s={step.get('retry_after_s')}"
            ),
            {
                "step": step.get("key"),
                "retry_after_s": step.get("retry_after_s"),
                "retry_state": step.get("retry_state"),
                "failure_path": step.get("failure_path"),
                "log_path": step.get("log_path"),
                "artifact_path": step.get("artifact_path"),
                "gate": step.get("gate"),
                "next_transition": step.get("next_transition"),
            },
        )
    production_flow = productivity.get("production_flow") or {}
    coverage = productivity.get("outcome_coverage")
    actionable_outcome_gaps = health.get("outcome_gap_actionable")
    production_outcome_coverage = production_flow.get("outcome_coverage")
    low_coverage_needs_action = (
        actionable_outcome_gaps is None
        or int(actionable_outcome_gaps or 0) > 0
        or (production_outcome_coverage is not None and production_outcome_coverage < 0.8)
    )
    if coverage is not None and coverage < 0.8 and low_coverage_needs_action:
        _alert(
            alerts,
            "warn",
            "low_outcome_coverage",
            f"Only {_fmt_pct(coverage)} of production runs have outcome rows.",
            {
                "runs": productivity.get("runs"),
                "outcomes": productivity.get("outcomes"),
                "actionable_missing_joins": productivity.get("actionable_missing_production_joins"),
            },
        )
    if productivity.get("pending_durability_count", 0):
        _alert(
            alerts,
            "info",
            "pending_durability",
            f"{productivity['pending_durability_count']} outcome rows are still pending durability.",
        )
        if cadence.get("durability_sweep_stamp_status") in {"missing", "stale"}:
            _alert(
                alerts,
                "warn",
                "durability_sweep_stale",
                "Pending durability rows exist, but the durability sweep cadence stamp is not fresh.",
                {
                    "stamp": cadence.get("durability_sweep_stamp"),
                    "status": cadence.get("durability_sweep_stamp_status"),
                    "age_s": cadence.get("durability_sweep_stamp_age_s"),
                    "stale_after_s": cadence.get("durability_sweep_stale_after_s"),
                },
            )
    if health.get("research_unevaluated_cap_reached"):
        _alert(
            alerts,
            "warn",
            "research_unevaluated_backlog_cap",
            "Research launches are paused because the unevaluated experiment backlog reached its cap.",
            {
                "unevaluated_backlog": health.get("research_unevaluated_backlog"),
                "unevaluated_cap": health.get("research_unevaluated_cap"),
            },
        )
    if production_flow.get("status") in {"stale", "dry"}:
        _alert(
            alerts,
            "warn",
            "production_flow_stale",
            production_flow.get("recommendation")
            or "No recent production runs are flowing through the Orchestrator dataset.",
            {
                "status": production_flow.get("status"),
                "recent_runs": production_flow.get("recent_production_runs", 0),
                "latest_run_age_days": production_flow.get("latest_run_age_days"),
            },
        )
    if health.get("human_calibration_count", 0) == 0:
        _alert(
            alerts,
            "info",
            "no_human_calibration",
            "No human calibration anchors are recorded.",
        )
    elif not health.get("human_calibration_ready"):
        _alert(
            alerts,
            "info",
            "human_calibration_not_ready",
            health.get("human_calibration_recommendation")
            or "Human calibration anchors exist but are not regression-ready yet.",
            {"matched_pairs": health.get("human_calibration_pairs", 0)},
        )
    if health.get("active_evidence_types", 0) == 0:
        clustered = health.get("evidence_schema_clustered_proposals", 0)
        message = (
            f"No active evidence types are registered yet; {clustered} clustered proposal(s) are approval-ready."
            if clustered
            else "No active evidence types are registered yet."
        )
        _alert(
            alerts,
            "warn",
            "no_evidence_types",
            message,
            {"clustered_proposals": clustered},
        )
    elif health.get("evidence_schema_prune_candidates", 0):
        _alert(
            alerts,
            "info",
            "evidence_type_prune_candidates",
            f"{health['evidence_schema_prune_candidates']} active evidence type(s) are stale and uncited.",
            {"active_review": health.get("evidence_schema_active_review") or []},
        )
    artifact_status = health.get("langsmith_artifact_status")
    if artifact_status in {"rollup_only", "partial", "dry", "unknown"}:
        telemetry_status = health.get("langsmith_telemetry_status")
        telemetry_flowing = telemetry_status in {"flowing", "cost_only", "trace_only"}
        severity = "fail" if artifact_status == "dry" and not telemetry_flowing else "warn"
        telemetry_note = (
            f"; durable telemetry sink is {telemetry_status}" if telemetry_flowing else ""
        )
        _alert(
            alerts,
            severity,
            "langsmith_artifact_distribution",
            f"LangSmith GitHub artifact distribution is {artifact_status}: "
            f"{health.get('langsmith_artifact_recommendation') or 'inspect langsmith_fetch dry-run output'}"
            f"{telemetry_note}",
            {
                "per_repo_found": health.get("langsmith_artifact_per_repo_found"),
                "expected_repos": health.get("langsmith_artifact_expected_repos"),
                "registered_repos": health.get("langsmith_artifact_registered_repos"),
                "exempted_repos": health.get("langsmith_artifact_exempted_repos"),
                "visible_artifacts_found": health.get("langsmith_artifact_visible_found"),
                "missing_with_recent_runs": health.get(
                    "langsmith_artifact_missing_with_recent_runs"
                ),
                "missing_with_recent_producer_runs": health.get(
                    "langsmith_artifact_missing_with_recent_producer_runs"
                ),
                "missing_without_recent_runs": health.get(
                    "langsmith_artifact_missing_without_recent_runs"
                ),
                "missing_diagnostic_errors": health.get(
                    "langsmith_artifact_missing_diagnostic_errors"
                ),
                "rollup_found": health.get("langsmith_artifact_rollup_found"),
                "telemetry_status": telemetry_status,
                "telemetry_cost_rows": health.get("langsmith_telemetry_cost_rows"),
                "telemetry_trace_rows": health.get("langsmith_telemetry_trace_rows"),
            },
        )
    for agent in capacity_summary.get("agents") or []:
        if agent["state"] in {"warn", "shed", "unknown"}:
            severity = "fail" if agent["state"] == "shed" else "warn"
            _alert(
                alerts,
                severity,
                f"capacity_{agent['agent']}",
                f"{agent['agent']} capacity is {agent['state']}: {agent.get('reason') or ''}".strip(),
                agent,
            )
    if learning.get("zero_observation_cells", 0):
        route_stage4_done = bool(route_coverage.get("stage4_complete"))
        route_active = bool(route_coverage.get("active_backfill_eligible"))
        severity = "info" if route_stage4_done and not route_active else "warn"
        if route_stage4_done:
            message = (
                f"{route_coverage['raw_zero_observation_cells']} raw route-weight cells have zero observations; "
                f"Stage 4 route coverage is complete "
                f"({route_coverage['default_review_zero_observation_cells']} default-review zero cells, "
                f"{route_coverage['active_backfill_collectable_zero_observation_cells']} active backfill-collectable)."
            )
        else:
            message = f"{route_coverage['raw_zero_observation_cells']} route-weight cells have zero observations."
        _alert(
            alerts,
            severity,
            "prior_only_cells",
            message,
            route_coverage,
        )
    for signal in process.get("signals") or []:
        severity = "fail" if signal.get("severity") == "HIGH" else "warn"
        _alert(
            alerts,
            severity,
            f"process_{signal.get('work_type')}",
            f"{signal.get('work_type')} process signal: "
            f"{signal.get('failure_count')} failed maintenance rows; {signal.get('recommendation')}.",
            {"examples": signal.get("examples") or []},
        )
    if process.get("non_durable_issue_count", 0):
        _alert(
            alerts,
            "info",
            "non_durable_issue_runs",
            f"{process['non_durable_issue_count']} non-durable issue runs are available for failure-focused review.",
        )
    if supervisor.get("status") == "armed_for_layered_ab_review":
        _alert(
            alerts,
            "info",
            "keepalive_supervisor_gate",
            "Keepalive supervisor shadow corpus reached the layered A/B review gate; live supervisor remains disabled.",
            {
                "failure_outcomes": supervisor.get("failure_outcomes"),
                "meaningful_disagreements": supervisor.get("meaningful_disagreements"),
            },
        )
    live_dispatches = int(supervisor.get("stage2_live_dispatches") or 0)
    live_valid_proposals = int(supervisor.get("stage2_live_valid_proposals") or 0)
    synced_role_outcomes = int(supervisor.get("stage2_synced_role_outcomes") or 0)
    linked_outcome_target = int(supervisor.get("stage2_linked_outcome_target") or 0)
    linked_disagreements = int(supervisor.get("stage2_linked_disagreements") or 0)
    disagreement_target = int(supervisor.get("stage2_disagreement_outcome_target") or 0)
    if (
        supervisor.get("stage2_ready_for_historical_replay_analysis")
        and not supervisor.get("stage2_ready_for_supervised_apply")
        and live_valid_proposals > synced_role_outcomes
    ):
        _alert(
            alerts,
            "warn",
            "stage2_live_link_gap",
            (
                "Keepalive Stage 2 has live RedirectAgent proposal dispatches, "
                "but valid live proposal advice is not fully linked to downstream outcomes."
            ),
            {
                "live_dispatches": live_dispatches,
                "live_valid_proposals": live_valid_proposals,
                "live_invalid_or_fallback_proposals": supervisor.get(
                    "stage2_live_invalid_or_fallback_proposals"
                ),
                "synced_role_outcomes": synced_role_outcomes,
                "linked_outcome_target": linked_outcome_target,
                "linked_disagreements": linked_disagreements,
                "disagreement_outcome_target": disagreement_target,
                "outcome_links": supervisor.get("stage2_outcome_links"),
                "accepted_links": supervisor.get("stage2_accepted_links"),
                "recommendation": supervisor.get("stage2_recommendation"),
            },
        )
    if supervisor.get("stage2_status") == "waiting_for_candidates":
        _alert(
            alerts,
            "warn",
            "stage2_waiting_for_candidates",
            "Stage 2 proposal evidence is blocked: no live or historical candidates are currently available.",
            {
                "historical_linked_disagreements": supervisor.get(
                    "stage2_historical_linked_disagreements"
                ),
                "target": supervisor.get("stage2_disagreement_outcome_target"),
                "recommendation": supervisor.get("stage2_recommendation"),
            },
        )
    if int(supervisor.get("stage2_unrecorded_live_candidate_count") or 0) > 0:
        _alert(
            alerts,
            "warn",
            "stage2_live_candidates",
            (
                f"{supervisor.get('stage2_unrecorded_live_candidate_count')} "
                "post-escalation keepalive PR(s) need Stage 2 proposal recording."
            ),
            {
                "live_targets": supervisor.get("stage2_live_targets") or [],
                "commands": supervisor.get("stage2_live_commands") or [],
                "plan_path": supervisor.get("stage2_live_plan_path"),
                "plan_age_s": supervisor.get("stage2_live_plan_age_s"),
            },
        )
    elif supervisor.get("stage2_live_plan_status") in {"missing", "unreadable"}:
        _alert(
            alerts,
            "info",
            "stage2_live_plan_unavailable",
            "Stage 2 live-candidate plan has not been refreshed.",
            {
                "status": supervisor.get("stage2_live_plan_status"),
                "plan_path": supervisor.get("stage2_live_plan_path"),
                "recommendation": supervisor.get("stage2_live_plan_recommendation"),
            },
        )
    elif int(supervisor.get("stage2_live_plan_age_s") or 0) > 172800:
        _alert(
            alerts,
            "warn",
            "stage2_live_plan_stale",
            "Stage 2 live-candidate plan is stale.",
            {
                "plan_path": supervisor.get("stage2_live_plan_path"),
                "plan_age_s": supervisor.get("stage2_live_plan_age_s"),
                "recommendation": supervisor.get("stage2_live_plan_recommendation"),
            },
        )
    dry = report.get("dry_seams") or {}
    for item in (dry.get("findings") or [])[:8]:
        severity = item.get("status") if item.get("status") in {"fail", "warn", "info"} else "warn"
        detail = {"recommendation": item.get("recommendation")}
        if item.get("sink") == "route_weights":
            detail.update(route_coverage)
            if route_coverage.get("stage4_complete") and not route_coverage.get(
                "active_backfill_eligible"
            ):
                severity = "info"
        _alert(
            alerts,
            severity,
            f"dry_seam_{item.get('sink')}",
            f"{item.get('sink')}: {item.get('finding')}",
            detail,
        )
    severity_rank = {"fail": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda row: (severity_rank.get(row["severity"], 3), row["key"]))
    return alerts


def _actionability_item(
    alert: dict,
    status: str,
    next_step: str,
    reason: str,
) -> dict:
    return {
        "key": alert.get("key"),
        "severity": alert.get("severity"),
        "message": alert.get("message"),
        "status": status,
        "next_step": next_step,
        "reason": reason,
        "detail": alert.get("detail") or {},
    }


def _classify_alert_actionability(alert: dict, dashboard: dict) -> dict:
    key = alert.get("key") or ""
    severity = alert.get("severity")
    detail = alert.get("detail") or {}
    health = dashboard.get("data_health") or {}
    learning = dashboard.get("learning") or {}
    backfill = dashboard.get("exploration_backfill_plan") or {}
    acquisition = dashboard.get("exploration_evidence_plan") or {}

    if key.startswith("cadence_failure:"):
        retry_after = int(detail.get("retry_after_s") or 0)
        if retry_after:
            return _actionability_item(
                alert,
                "gated",
                f"Backoff is active for {retry_after}s; the next cadence tick retries automatically.",
                detail.get("gate") or "Cadence retry backoff prevents hot-loop failures.",
            )
        return _actionability_item(
            alert,
            "actionable",
            detail.get("next_transition")
            or "Inspect the exact log/artifact and rerun the bounded cadence step.",
            detail.get("log_path")
            or detail.get("artifact_path")
            or "Cadence failure evidence is retained.",
        )
    if key in {"low_outcome_coverage", "production_flow_stale"}:
        return _actionability_item(
            alert,
            "actionable",
            "Run outcome ingest/relearn diagnostics and repair missing production outcome joins.",
            "Production outcome flow directly affects learner quality.",
        )
    if key == "research_unevaluated_backlog_cap":
        return _actionability_item(
            alert,
            "actionable",
            "Run the bounded exp_abcd followup cadence and repair stalled evaluation rows before launching more research.",
            "The subject gate is intentionally preserving production capacity and evidence independence.",
        )
    if key == "langsmith_artifact_distribution":
        return _actionability_item(
            alert,
            "actionable",
            "Inspect langsmith_fetch dry-run output and repair producer artifact or repo-local emitter gaps.",
            "Artifact distribution gaps usually have a concrete repo/workflow producer to fix.",
        )
    if key.startswith("capacity_"):
        agent = key.removeprefix("capacity_") or "agent"
        state = detail.get("state")
        policy = detail.get("policy")
        reason_text = detail.get("reason") or alert.get("message") or ""
        if state == "shed":
            return _actionability_item(
                alert,
                "actionable",
                f"Repair the {agent} seat before dispatch: inspect its shed flag and latest runtime log, "
                "then clear the flag only after a successful auth/quota probe or documented reset.",
                "Shed means an observed 429, rate-limit, or auth failure is authoritative.",
            )
        if state == "unknown":
            return _actionability_item(
                alert,
                "actionable",
                f"Inspect the {agent} capacity source before dispatch; unknown capacity is not routable.",
                "The router skips unknown seats because it cannot prove availability.",
            )
        if agent == "gemini" and policy in {"window-soft-cap", "weekly-soft-cap"}:
            next_step = detail.get("next_action") or (
                "Gemini is usable but soft-budget constrained; prefer ok seats and use Gemini only for "
                "substantial good-fit work."
            )
            return _actionability_item(
                alert,
                "actionable",
                next_step,
                reason_text,
            )
        if state == "warn":
            return _actionability_item(
                alert,
                "actionable",
                f"Prefer ok seats before {agent}; if you dispatch it anyway, carry the exact capacity reason in the handoff.",
                reason_text or "Capacity warnings can change immediate routing choices.",
            )
    if key.startswith("process_") or key == "non_durable_issue_runs":
        return _actionability_item(
            alert,
            "actionable",
            "Inspect the listed rows, record process_ignore= or issue_review= when reviewed, and promote real lessons to repo knowledge.",
            "These alerts are backed by concrete failed rows.",
        )
    if key == "no_evidence_types":
        if int(detail.get("clustered_proposals") or 0) > 0:
            return _actionability_item(
                alert,
                "actionable",
                "Review evidence_schema.py proposals and approve concrete evidence types deliberately.",
                "Clustered evidence proposals are available for operator review.",
            )
        return _actionability_item(
            alert,
            "gated",
            "Wait for recurring evaluator gaps before approving schema growth.",
            "No clustered evidence proposal exists yet.",
        )
    if key == "evidence_type_prune_candidates":
        return _actionability_item(
            alert,
            "actionable",
            "Review stale uncited evidence types and prune or keep them with rationale.",
            "The schema review has concrete stale candidates.",
        )
    if key in {"prior_only_cells", "dry_seam_route_weights"}:
        route_coverage = detail or learning.get("route_coverage") or {}
        if backfill.get("active_backfill_eligible"):
            return _actionability_item(
                alert,
                "actionable",
                "Run the guarded exploration backfill/collection plan on eligible opener subjects.",
                "Route-weight coverage has an eligible evidence acquisition path.",
            )
        if route_coverage.get("stage4_complete") and route_coverage.get("route_ready"):
            return _actionability_item(
                alert,
                "informational",
                "Treat remaining prior-only route cells as a learning-quality backlog; collect only real production or evaluated A/B evidence when safe subjects appear.",
                "Stage 4 route coverage is already complete and guarded backfill is not active-eligible.",
            )
        if acquisition.get("recommended_task_types"):
            return _actionability_item(
                alert,
                "actionable",
                "Run a supervised exploration evidence window for the recommended low-risk task types.",
                "Exploration acquisition has recommended task types.",
            )
        return _actionability_item(
            alert,
            "gated",
            "Wait for safe opener subjects or future exploration candidates before collecting route evidence.",
            "The current backlog has no eligible evidence-acquisition subject.",
        )
    if key == "stage2_waiting_for_candidates":
        return _actionability_item(
            alert,
            "gated",
            "Wait for future live escalations or deliberately expand the historical source.",
            "Stage 2 has no unrecorded live candidates and no unreplayed historical candidates.",
        )
    if key == "stage2_live_link_gap":
        return _actionability_item(
            alert,
            "actionable",
            "Inspect the live Stage 2 proposal dispatches, link any accepted/applied advice to downstream outcomes, or record why none were accepted.",
            "Historical proposal evidence is ready, but live proposal-to-outcome links are not flowing.",
        )
    if key == "stage2_live_candidates":
        return _actionability_item(
            alert,
            "actionable",
            "Run the emitted Stage 2 record command(s), review any advice manually, and link downstream outcomes only if advice is accepted or applied.",
            "Open post-escalation keepalive PRs are now visible and need proposal evidence before they disappear.",
        )
    if key in {"stage2_live_plan_unavailable", "stage2_live_plan_stale"}:
        return _actionability_item(
            alert,
            "actionable",
            "Refresh the live candidate plan with keepalive_supervisor.py --stage2-plan --stage2-backend cursor --json.",
            "Without a fresh plan, escalated keepalive PRs may not surface in the dashboard.",
        )
    if key == "pending_durability":
        return _actionability_item(
            alert,
            "gated",
            "Wait for the configured durability grace window, then let durability_sweep.py classify matured outcomes.",
            "Pending durability rows become actionable only after their PRs are old enough to judge revert/reopen status.",
        )
    if key == "durability_sweep_stale":
        return _actionability_item(
            alert,
            "actionable",
            "Inspect durability_sweep.py and the GitHub calls it is waiting on, then rerun the sweep or repair the cadence.",
            "Pending durability is only safely gated while the sweep cadence is fresh.",
        )
    if key in {
        "dry_seam_human_calibration",
        "no_human_calibration",
        "human_calibration_not_ready",
    }:
        return _actionability_item(
            alert,
            "gated",
            "Let experiment followup and the objective-anchor/referee lane produce anchors; no owner score is requested.",
            "Calibration remains data-gated, but objective evidence is the zero-owner transition.",
        )
    if key == "dry_seam_outcomes":
        if int(health.get("outcome_gap_actionable") or 0) > 0:
            return _actionability_item(
                alert,
                "actionable",
                "Run outcomes.py for actionable production-ingest candidates.",
                "Dry-seam audit reports concrete outcome rows that can be joined.",
            )
        return _actionability_item(
            alert,
            "informational",
            "Do not link offload/experiment/role rows unless their output influenced production work.",
            "Current outcome gaps are advisory or expected-unlinked evidence.",
        )
    if key == "keepalive_supervisor_gate":
        return _actionability_item(
            alert,
            "informational",
            "Keep the supervisor shadow collection running; do not enable live apply from this alert alone.",
            "The gate is armed, but live supervisor promotion is separately evidence-gated.",
        )
    if severity in {"fail", "warn"}:
        return _actionability_item(
            alert,
            "actionable",
            "Inspect this warning and either fix the underlying row or mark the gate explicitly.",
            "Unrecognized warn/fail alerts default to operator action.",
        )
    return _actionability_item(
        alert,
        "informational",
        "Track this informational status; no immediate operator action is implied.",
        "Informational alert.",
    )


def _classify_actionability(alerts: list[dict], dashboard: dict) -> dict:
    buckets: dict[str, list] = {"actionable": [], "gated": [], "informational": []}
    for alert in alerts:
        item = _classify_alert_actionability(alert, dashboard)
        buckets[item["status"]].append(item)
    return {
        "actionable_count": len(buckets["actionable"]),
        "gated_count": len(buckets["gated"]),
        "informational_count": len(buckets["informational"]),
        "actionable": buckets["actionable"],
        "gated": buckets["gated"],
        "informational": buckets["informational"],
    }


def build_dashboard(report: dict, capacity_snapshot: dict | None = None) -> dict:
    productivity = _productivity(report)
    capacity_summary = _capacity(capacity_snapshot)
    cadence = _cadence_health()
    activation = _activation_state(report, cadence)
    learning = _learning(report)
    health = _data_health(report)
    process = _process_improvement(report)
    supervisor = _keepalive_supervisor(report)
    exploration = report.get("exploration_policy") or {}
    recorded_exploration = exploration.get("recorded_exploration_evidence") or {}
    acquisition = report.get("exploration_evidence_plan") or {}
    backfill = report.get("exploration_backfill_plan") or {}
    route_coverage = _route_coverage_summary(learning, exploration, acquisition, backfill)
    learning["route_coverage"] = route_coverage
    alerts = _build_alerts(
        productivity,
        capacity_summary,
        cadence,
        learning,
        health,
        process,
        supervisor,
        report,
        route_coverage,
    )
    dashboard = {
        "generated_at": int(time.time()),
        "read_only": True,
        "source_report_generated_at": report.get("generated_at"),
        "db_path": report.get("db_path"),
        "window_days": report.get("window_days"),
        "scorecard": {
            "outcome_coverage": productivity.get("outcome_coverage"),
            "production_outcome_coverage": productivity.get("production_outcome_coverage"),
            "all_run_outcome_coverage": productivity.get("all_run_outcome_coverage"),
            "actionable_missing_production_joins": productivity.get(
                "actionable_missing_production_joins"
            ),
            "merged_rate": productivity.get("merged_rate"),
            "durable_success_rate": productivity.get("durable_success_rate"),
            "durability_failure_rate": productivity.get("durability_failure_rate"),
            "production_flow_status": productivity.get("production_flow_status"),
            "recent_production_runs": productivity.get("recent_production_runs"),
            "recent_production_outcomes": productivity.get("recent_production_outcomes"),
            "latest_production_run_age_days": productivity.get("latest_production_run_age_days"),
            "capacity_ok": capacity_summary.get("state_counts", {}).get("ok", 0),
            "capacity_warn": capacity_summary.get("state_counts", {}).get("warn", 0),
            "capacity_shed": capacity_summary.get("state_counts", {}).get("shed", 0),
            "route_weight_version": learning.get("route_weights_version"),
            "zero_observation_cells": learning.get("zero_observation_cells"),
            "raw_zero_observation_cells": route_coverage.get("raw_zero_observation_cells"),
            "default_review_zero_observation_cells": route_coverage.get(
                "default_review_zero_observation_cells"
            ),
            "active_backfill_collectable_zero_observation_cells": route_coverage.get(
                "active_backfill_collectable_zero_observation_cells"
            ),
            "route_weight_stage4_complete": route_coverage.get("stage4_complete"),
            "route_weight_route_ready": route_coverage.get("route_ready"),
            "exploration_policy_status": exploration.get("status"),
            "exploration_policy_recommendation": exploration.get("recommendation"),
            "exploration_direct_ready": recorded_exploration.get("ready_for_direct_comparison"),
            "exploration_acquisition_stage": acquisition.get("stage"),
            "exploration_backfill_status": backfill.get("status"),
            "exploration_backfill_eligible": backfill.get("active_backfill_eligible"),
            "process_signal_count": process.get("signal_count"),
            "non_durable_issue_count": process.get("non_durable_issue_count"),
            "keepalive_supervisor_status": supervisor.get("status"),
            "dry_seams_overall": health.get("dry_seams_overall"),
            "alert_count": len(alerts),
        },
        "productivity": productivity,
        "capacity": capacity_summary,
        "cadence": cadence,
        "activation": activation,
        "learning": learning,
        "exploration_policy": exploration,
        "exploration_evidence_plan": acquisition,
        "exploration_backfill_plan": backfill,
        "process_improvement": process,
        "keepalive_supervisor": supervisor,
        "data_health": health,
        "alerts": alerts,
    }
    actionability = _classify_actionability(alerts, dashboard)
    dashboard["actionability"] = actionability
    # Bind the nested dict once. `dashboard["scorecard"][...]` made mypy resolve the OUTER value
    # union on every assignment; a local binding is the same object, so the writes still land.
    scorecard = cast("dict[str, Any]", dashboard["scorecard"])
    scorecard["actionable_alert_count"] = actionability["actionable_count"]
    scorecard["gated_alert_count"] = actionability["gated_count"]
    scorecard["informational_alert_count"] = actionability["informational_count"]
    return dashboard


def format_markdown(dashboard: dict) -> str:
    score = dashboard["scorecard"]
    prod = dashboard["productivity"]
    cap = dashboard["capacity"]
    cadence = dashboard.get("cadence") or {}
    learning = dashboard["learning"]
    process = dashboard["process_improvement"]
    supervisor = dashboard["keepalive_supervisor"]
    exploration = dashboard.get("exploration_policy") or {}
    acquisition = dashboard.get("exploration_evidence_plan") or {}
    backfill = dashboard.get("exploration_backfill_plan") or {}
    health = dashboard["data_health"]
    activation = dashboard.get("activation") or {}
    actionability = dashboard.get("actionability") or {}
    lines = [
        "# Orchestrator Observability Dashboard",
        "",
        f"- Window: {dashboard.get('window_days')} days",
        f"- DB: `{dashboard.get('db_path')}`",
        f"- Read-only: {dashboard.get('read_only')}",
        "",
        "## Scorecard",
        "",
        f"- Production outcome coverage: {_fmt_pct(score.get('production_outcome_coverage'))} "
        f"({prod.get('production_outcomes')}/{prod.get('production_runs')}); "
        f"actionable missing joins={score.get('actionable_missing_production_joins')}",
        f"- All-run outcome rows (diagnostic; expected dry rows separate): "
        f"{_fmt_pct(score.get('all_run_outcome_coverage'))} "
        f"({prod.get('all_outcome_rows')}/{prod.get('all_runs')})",
        f"- Merged rate: {_fmt_pct(score.get('merged_rate'))} ({prod.get('merged_count')} merged outcomes)",
        f"- Durable success rate: {_fmt_pct(score.get('durable_success_rate'))} ({prod.get('durable_success_count')} durable successes)",
        f"- Durability failure rate: {_fmt_pct(score.get('durability_failure_rate'))} ({prod.get('durability_failure_count')} failures)",
        f"- Production flow: {score.get('production_flow_status') or 'unknown'} "
        f"({score.get('recent_production_runs')} runs / "
        f"{score.get('recent_production_outcomes')} outcomes in 7d)",
        f"- Capacity: ok={score.get('capacity_ok')} warn={score.get('capacity_warn')} shed={score.get('capacity_shed')}",
        f"- Cadence: steps={cadence.get('step_count')} failed={cadence.get('failed_step_count')} "
        f"backoff={cadence.get('backoff_step_count')} ready_to_retry={cadence.get('ready_to_retry_count')}",
        f"- Route weights: v{score.get('route_weight_version') or 'none'}, "
        f"raw_zero={score.get('raw_zero_observation_cells')} "
        f"default_review_zero={score.get('default_review_zero_observation_cells')} "
        f"active_backfill_zero={score.get('active_backfill_collectable_zero_observation_cells')} "
        f"stage4_complete={score.get('route_weight_stage4_complete')}",
        f"- Exploration policy: {score.get('exploration_policy_recommendation') or 'unknown'} ({score.get('exploration_policy_status') or 'unknown'})",
        f"- Exploration acquisition: {score.get('exploration_acquisition_stage') or 'unknown'}",
        f"- Exploration backfill: {score.get('exploration_backfill_status') or 'unknown'}",
        f"- Process signals: {score.get('process_signal_count')} maintenance, {score.get('non_durable_issue_count')} issue failures",
        f"- Actionability: actionable={score.get('actionable_alert_count')} "
        f"gated={score.get('gated_alert_count')} "
        f"info={score.get('informational_alert_count')}",
        f"- Keepalive supervisor gate: {score.get('keepalive_supervisor_status') or 'unknown'}",
        f"- Dry seams: {score.get('dry_seams_overall')}",
        "",
        "## Capacity",
        "",
    ]
    if not cap.get("available"):
        lines.append("- Capacity snapshot unavailable")
    else:
        for agent in cap.get("agents") or []:
            bits = [f"state={agent['state']}"]
            if agent.get("policy"):
                bits.append(f"policy={agent['policy']}")
            if agent.get("used_5h_rate") is not None:
                bits.append(f"5h={_fmt_pct(agent.get('used_5h_rate'))}")
            if agent.get("used_weekly_rate") is not None:
                bits.append(f"weekly={_fmt_pct(agent.get('used_weekly_rate'))}")
            lines.append(f"- {agent['agent']}: " + ", ".join(bits))
    lines.extend(["", "## Cadence Activation", ""])
    for step in cadence.get("steps") or []:
        lines.append(
            f"- {step['key']}: success={step.get('success_status')} "
            f"failures={step.get('failure_count')} retry={step.get('retry_state')} "
            f"retry_after_s={step.get('retry_after_s')} reason={step.get('exact_reason')} "
            f"gate={step.get('gate')} next={step.get('next_transition')}"
        )
    lines.extend(["", "## Learning", ""])
    lines.append(
        f"- Tasks: {learning.get('task_count')} total, "
        f"{learning.get('cold_start_task_count')} cold-start, "
        f"{learning.get('divergent_task_count')} diverged from prior"
    )
    if exploration:
        lines.append(
            f"- Exploration: default={exploration.get('current_default')} "
            f"recommendation={exploration.get('recommendation')} "
            f"ready_tasks={exploration.get('ready_task_count')}/{exploration.get('task_count')} "
            f"direct_ready={(exploration.get('recorded_exploration_evidence') or {}).get('ready_for_direct_comparison')}"
        )
    if acquisition:
        deficits = {
            row.get("mode"): row.get("remaining_outcome_runs")
            for row in acquisition.get("direct_mode_deficits") or []
        }
        lines.append(
            f"- Acquisition: stage={acquisition.get('stage')} "
            f"epsilon_remaining={deficits.get('epsilon-greedy')} "
            f"thompson_remaining={deficits.get('thompson-hybrid')}"
        )
    if backfill:
        lines.append(
            f"- Backfill: status={backfill.get('status')} "
            f"eligible={backfill.get('active_backfill_eligible')} "
            f"missing_cells={backfill.get('missing_cell_count')} "
            f"planned_jobs={len(backfill.get('planned_jobs') or [])}"
        )
    for item in (learning.get("top_by_task") or [])[:10]:
        flags = []
        if item.get("cold_start"):
            flags.append("cold")
        if item.get("diverges_from_prior"):
            flags.append("diverged")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(
            f"- {item['task_type']}: {item.get('top_agent') or '-'} "
            f"posterior={_fmt_num(item.get('posterior'))} n={_fmt_num(item.get('n_obs'))}{suffix}"
        )
    lines.extend(["", "## Data Health", ""])
    lines.append(
        f"- Costs: {health.get('cost_rows')} rows; traces: {health.get('trace_rows')} rows"
    )
    lineage = health.get("completion_event_health") or {}
    lines.append(
        "- Completion lineage: "
        f"events={lineage.get('total', 0)} complete={lineage.get('complete', 0)} "
        f"durable={lineage.get('durable', 0)} redacted={lineage.get('redacted', 0)} "
        f"rejected={lineage.get('rejected', 0)} orphans={lineage.get('orphan_edges', 0)} "
        f"accepted_linked={lineage.get('accepted_influence_linked', 0)}/"
        f"{lineage.get('accepted_influence_total', 0)}"
    )
    lines.append(
        f"- Worker provenance: requested={health.get('requested_worker_runs')}/"
        f"{health.get('eligible_worker_runs')} "
        f"resolved={health.get('resolved_worker_runs')}/"
        f"{health.get('eligible_worker_runs')} "
        f"unknown={health.get('unknown_worker_runs')} "
        f"excluded non-worker={health.get('excluded_nonworker_runs')} "
        f"worker/evaluator overlap={health.get('worker_evaluator_role_overlap_runs')} "
        f"resolved-model collisions="
        f"{health.get('worker_evaluator_resolved_model_collision_runs')} "
        f"legacy non-worker collisions="
        f"{health.get('legacy_worker_nonworker_model_collision_runs')} "
        f"unmigrated traces={health.get('unmigrated_legacy_trace_rows')}"
    )
    lines.append(
        f"- Capability lifecycle: total={health.get('capability_total')} "
        f"states={json.dumps(health.get('capability_counts_by_status') or {}, sort_keys=True)} "
        f"invalid active edges={len(health.get('capability_active_without_edges') or [])}"
    )
    lines.append(
        "- Model profile trial: "
        f"status={health.get('model_profile_trial_status')} "
        f"lifecycle={health.get('model_profile_trial_lifecycle')} "
        f"attempts={health.get('model_profile_trial_attempts')} "
        f"source_unchanged={health.get('model_profile_trial_source_unchanged')} "
        f"shared_pool_debit={json.dumps(health.get('model_profile_trial_shared_pool_debit') or {}, sort_keys=True)} "
        f"learning={health.get('model_profile_trial_learning_enabled')}"
    )
    lines.append(
        "- Model profile transport qualification: "
        f"status={health.get('model_profile_transport_qualification_status')} "
        f"transport_contract={health.get('model_profile_transport_contract_qualified')} "
        f"provider_identity={health.get('model_profile_provider_identity_status')} "
        f"learning={health.get('model_profile_qualification_learning_enabled')} "
        f"quality_weights={health.get('model_profile_qualification_quality_weight_updates_allowed')}"
    )
    lines.append(
        f"- Research subjects: registered={health.get('research_subject_count')} "
        f"independent={health.get('research_independent_subject_count')} "
        f"unevaluated={health.get('research_unevaluated_backlog')}/"
        f"{health.get('research_unevaluated_cap')} "
        f"duplicate_rejections={health.get('research_duplicate_rejections')} "
        f"production_collisions={health.get('research_production_collisions')} "
        f"effective_n={health.get('research_effective_sample_count')}"
    )
    if health.get("langsmith_artifact_status"):
        lines.append(
            f"- LangSmith artifacts: {health.get('langsmith_artifact_status')} "
            f"per-repo={health.get('langsmith_artifact_per_repo_found')}/"
            f"{health.get('langsmith_artifact_expected_repos')} "
            f"registered={health.get('langsmith_artifact_registered_repos')} "
            f"exempted={health.get('langsmith_artifact_exempted_repos')} "
            f"missing_with_runs={health.get('langsmith_artifact_missing_with_recent_runs')} "
            f"missing_with_producer={health.get('langsmith_artifact_missing_with_recent_producer_runs')} "
            f"rollup={'yes' if health.get('langsmith_artifact_rollup_found') else 'no'}"
        )
    if health.get("langsmith_telemetry_status"):
        lines.append(
            f"- LangSmith telemetry: {health.get('langsmith_telemetry_status')} "
            f"cost_rows={health.get('langsmith_telemetry_cost_rows')} "
            f"trace_rows={health.get('langsmith_telemetry_trace_rows')}"
        )
    lines.append(f"- Judges: {health.get('ready_judge_count')}/{health.get('judge_count')} ready")
    lines.append(
        f"- Human calibration: status={health.get('human_calibration_status')} "
        f"ready={health.get('human_calibration_ready')} "
        f"pairs={health.get('human_calibration_pairs')}"
    )
    lines.append(
        f"- Evidence: {health.get('open_gap_kinds')} open gap kinds, "
        f"{health.get('evidence_proposals')} proposals, "
        f"{health.get('active_evidence_types')} active types"
    )
    lines.append(
        f"- Evidence schema: status={health.get('evidence_schema_status')} "
        f"clustered_proposals={health.get('evidence_schema_clustered_proposals')} "
        f"prune_candidates={health.get('evidence_schema_prune_candidates')}"
    )
    lines.append(
        f"- Outcome gaps: total={health.get('outcome_gap_total')} "
        f"actionable={health.get('outcome_gap_actionable')} "
        f"advisory_or_unlinked={health.get('outcome_gap_advisory_or_unlinked')}"
    )
    for row in (health.get("outcome_gap_categories") or [])[:5]:
        lines.append(
            f"- Outcome gap category: {row['category']} "
            f"count={row['count']} actionable={row['actionable']}"
        )
    lines.append(
        f"- Features: {health.get('feature_total')} tracked, "
        f"{health.get('feature_promotion_candidates')} promotion candidates"
    )
    lines.extend(["", "## Activation and Learning", ""])
    for name, denominator in (activation.get("denominators") or {}).items():
        lines.append(
            f"- denominator {name}: numerator={denominator.get('numerator')} "
            f"denominator={denominator.get('denominator')} "
            f"coverage={_fmt_pct(denominator.get('coverage')) if denominator.get('coverage') is not None else 'n/a'}"
        )
    for row in activation.get("capabilities") or []:
        lines.append(
            f"- capability {row['capability_id']}: state={row.get('status')} "
            f"liveness={row.get('liveness')} reason={row.get('exact_reason')} "
            f"match={row.get('last_match')} invocation={row.get('last_invocation')} "
            f"outcome={row.get('last_outcome')} expiry={row.get('expiry')} "
            f"next={row.get('next_transition')} rollback={json.dumps(row.get('rollback'), sort_keys=True)} "
            f"upstream={json.dumps(row.get('upstream_sync'), sort_keys=True)}"
        )
    for role_name, row in (activation.get("roles") or {}).items():
        lines.append(
            f"- role {role_name}: matched={row.get('matched', 0)} invoked={row.get('invoked', 0)} "
            f"accepted={row.get('accepted', 0)} rejected={row.get('rejected', 0)} "
            f"linked={row.get('linked', 0)} durable={row.get('durable', 0)} "
            f"profile_fit={row.get('profile_fit')} evidence={row.get('evidence_readiness')}"
        )
    for profile in (activation.get("profiles") or {}).get("profiles") or []:
        lines.append(
            f"- profile {profile.get('profile_id')}: requested={profile.get('requested_model')} "
            f"resolved_coverage={_fmt_pct(profile.get('resolved_model_coverage'))} "
            f"fallback={_fmt_pct(profile.get('fallback_rate'))} "
            f"evidence_age_days={profile.get('evidence_age_days')}"
        )
    range_state = activation.get("range") or {}
    lines.append(
        f"- range: eligible={range_state.get('eligible')} reason={range_state.get('exact_reason')} "
        f"claims={json.dumps(range_state.get('claimed_by') or {}, sort_keys=True)} "
        f"capacity_rejections={len(range_state.get('capacity_rejections') or [])} "
        f"next={range_state.get('next_transition')}"
    )
    runtime_state = activation.get("runtime_ac") or {}
    lines.append(
        f"- runtime-ac: status={runtime_state.get('status')} "
        f"event_fired={runtime_state.get('eligible_event_fired')} "
        f"required={runtime_state.get('required_event_denominator')} "
        f"executed={runtime_state.get('executed_gate_numerator')} "
        f"reason={runtime_state.get('exact_reason')} "
        f"next={json.dumps(runtime_state.get('next_transitions') or [])}"
    )
    research_state = activation.get("research") or {}
    lines.append(
        f"- research: duplicates={research_state.get('duplicate_rejections')} "
        f"unevaluated={research_state.get('unevaluated_backlog')}/{research_state.get('unevaluated_cap')} "
        f"collisions={research_state.get('production_collisions')} "
        f"reasons={json.dumps(research_state.get('rejections_by_reason') or {}, sort_keys=True)}"
    )
    compiler_state = activation.get("compiler") or {}
    lines.append(
        f"- compiler: status={compiler_state.get('status')} "
        f"candidates={compiler_state.get('emitted_candidates')} "
        f"expired={compiler_state.get('expired_candidates')} "
        f"tombstones={compiler_state.get('tombstones')} "
        f"next={json.dumps(compiler_state.get('next_actions') or [])}"
    )
    experiment_state = activation.get("experiments") or {}
    lines.append(
        f"- experiment promotion: state={experiment_state.get('promotion_state')} "
        f"missing_arms={len(experiment_state.get('missing_arm_outcomes') or [])} "
        f"next={experiment_state.get('next_transition')}"
    )
    evidence_state = activation.get("evidence_contracts") or {}
    lines.append(
        f"- evidence contracts: state={evidence_state.get('schema_state')} "
        f"candidates={evidence_state.get('candidate_count')} "
        f"next={evidence_state.get('next_transition')}"
    )
    lines.extend(["", "## Process Improvement", ""])
    if process.get("signals"):
        lines.append("- Maintenance signals:")
        for signal in process.get("signals") or []:
            lines.append(
                f"- [{signal['severity']}] {signal['work_type']}: "
                f"{signal['failure_count']} failures; {signal['recommendation']}"
            )
    else:
        lines.append("- Maintenance signals: none")
    if process.get("non_durable_issue_runs"):
        lines.append("- Non-durable issue runs:")
        for item in (process.get("non_durable_issue_runs") or [])[:5]:
            lines.append(
                f"- {item['target']}: {item['durability']} "
                f"via {item.get('agent') or 'unknown'} ({item['run_id']})"
            )
    else:
        lines.append("- Non-durable issue runs: none")
    lines.append(f"- Reviewed issue failures: {process.get('reviewed_issue_failure_count', 0)}")
    lines.extend(["", "## Keepalive Supervisor Gate", ""])
    thresholds = supervisor.get("thresholds") or {}
    lines.append(f"- Status: {supervisor.get('status') or 'unknown'}")
    lines.append(
        f"- Evidence: labeled={supervisor.get('labeled_outcomes')}/"
        f"{thresholds.get('labeled_outcomes')} "
        f"failures={supervisor.get('failure_outcomes')}/{thresholds.get('failure_outcomes')} "
        f"meaningful_disagreements={supervisor.get('meaningful_disagreements')}/"
        f"{thresholds.get('meaningful_disagreements')}"
    )
    lines.append(
        f"- Stage 2 proposals: status={supervisor.get('stage2_status') or 'unknown'} "
        f"valid={supervisor.get('stage2_valid_proposals')}/{supervisor.get('stage2_readiness_target')} "
        f"historical_ready={supervisor.get('stage2_ready_for_historical_replay_analysis')} "
        f"live_dispatches={supervisor.get('stage2_live_dispatches')} "
        f"live_valid={supervisor.get('stage2_live_valid_proposals')} "
        f"outcome_links={supervisor.get('stage2_outcome_links')} "
        f"linked={supervisor.get('stage2_synced_role_outcomes')}/{supervisor.get('stage2_linked_outcome_target')} "
        f"disagreement_links={supervisor.get('stage2_linked_disagreements')}/"
        f"{supervisor.get('stage2_disagreement_outcome_target')} "
        f"historical_disagreements={supervisor.get('stage2_historical_linked_disagreements')}/"
        f"{supervisor.get('stage2_disagreement_outcome_target')} "
        f"remaining_candidates={supervisor.get('stage2_historical_candidates_remaining')}+"
        f"{supervisor.get('stage2_calibration_candidates_remaining')} "
        f"ready_for_supervised_apply={supervisor.get('stage2_ready_for_supervised_apply')}"
    )
    lines.append(
        f"- Stage 2 live plan: status={supervisor.get('stage2_live_plan_status') or 'unknown'} "
        f"age_s={supervisor.get('stage2_live_plan_age_s')} "
        f"live={supervisor.get('stage2_live_candidate_count')} "
        f"eligible={supervisor.get('stage2_eligible_live_candidate_count')} "
        f"unrecorded={supervisor.get('stage2_unrecorded_live_candidate_count')}"
    )
    lines.append(f"- Live supervisor allowed: {supervisor.get('live_supervisor_allowed')}")
    if supervisor.get("recommendation"):
        lines.append(f"- Recommendation: {supervisor['recommendation']}")
    lines.extend(["", "## Actionability", ""])
    if actionability.get("actionable"):
        lines.append("- Actionable now:")
        for item in actionability.get("actionable") or []:
            lines.append(f"- {item['key']}: {item['next_step']}")
    else:
        lines.append("- Actionable now: none")
    if actionability.get("gated"):
        lines.append("- Gated / waiting:")
        for item in actionability.get("gated") or []:
            lines.append(f"- {item['key']}: {item['next_step']}")
    if actionability.get("informational"):
        lines.append(f"- Informational: {actionability.get('informational_count', 0)} alert(s)")
    lines.extend(["", "## Alerts", ""])
    if not dashboard.get("alerts"):
        lines.append("- None")
    for alert in dashboard.get("alerts") or []:
        lines.append(f"- [{alert['severity']}] {alert['message']}")
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    import shutil

    old_db = feedback.DB_PATH
    temp_dir = Path(tempfile.mkdtemp(prefix="observability-dashboard-selftest-"))
    feedback.DB_PATH = temp_dir / "feedback.db"
    try:
        now = int(time.time())
        route_table = {
            "implement": {
                "role": "code",
                "agents": [
                    {"agent": "prior_top", "mode": "full", "late": False},
                    {"agent": "evidence_wins", "mode": "full", "late": False},
                    {"agent": "also_ran", "mode": "full", "late": False},
                ],
            }
        }
        hypotheses_path = periodic_report._seed_selftest_data(route_table, now)
        keepalive_corpus_path = temp_dir / "keepalive-shadow.jsonl"
        periodic_report._seed_keepalive_supervisor_corpus(keepalive_corpus_path)
        redirect_corpus_path = temp_dir / "redirect-shadow.jsonl"
        periodic_report._seed_redirect_stage2_corpus(redirect_corpus_path)
        features_path = temp_dir / "features.json"
        features.record_use(
            "dashboard-fixture", "task-1", "surface operator scorecards", features_path
        )
        features.record_use("dashboard-fixture", "task-2", path=features_path)
        features.record_use("dashboard-fixture", "task-3", path=features_path)
        report = periodic_report.build_report(
            window_days=90,
            route_table=route_table,
            hypotheses_path=hypotheses_path,
            features_path=features_path,
            keepalive_corpus_path=keepalive_corpus_path,
            redirect_corpus_path=redirect_corpus_path,
        )
        fake_capacity = {
            "generated_at": now,
            "agents": {
                "cursor": {"state": "ok", "reason": "fixture ok", "window": "monthly"},
                "gemini": {
                    "state": "warn",
                    "reason": "usable but 5h soft-budget constrained: fixture",
                    "window": "5h+weekly",
                    "policy": "window-soft-cap",
                    "availability": "usable_soft_constrained",
                    "next_action": "Prefer ok seats until the 5h window refreshes; fixture next action.",
                    "minutes_to_window_refresh": 42,
                    "used_5h": 6.0,
                    "soft_units_5h": 8.0,
                },
            },
        }
        dash = build_dashboard(report, fake_capacity)
        assert dash["productivity"]["runs"] == 10, dash["productivity"]
        assert dash["productivity"]["outcome_coverage"] == 1.0, dash["productivity"]
        assert dash["scorecard"]["production_flow_status"] == "flowing", dash["scorecard"]
        assert dash["scorecard"]["recent_production_runs"] == 10, dash["scorecard"]
        assert dash["capacity"]["state_counts"]["warn"] == 1, dash["capacity"]
        assert dash["learning"]["divergent_task_count"] == 1, dash["learning"]
        assert dash["process_improvement"]["signal_count"] == 2, dash["process_improvement"]
        assert dash["process_improvement"]["suppressed_process_failure_count"] == 1, dash[
            "process_improvement"
        ]
        assert dash["scorecard"]["non_durable_issue_count"] == 2, dash["scorecard"]
        assert dash["process_improvement"]["reviewed_issue_failure_count"] == 1, dash[
            "process_improvement"
        ]
        assert dash["data_health"]["outcome_gap_total"] == 0, dash["data_health"]
        assert dash["data_health"]["langsmith_telemetry_status"] == "flowing", dash["data_health"]
        assert dash["data_health"]["resolved_worker_runs"] == 4, dash["data_health"]
        assert dash["data_health"]["unknown_worker_runs"] == 6, dash["data_health"]
        assert dash["data_health"]["worker_evaluator_role_overlap_runs"] == 1, dash["data_health"]
        assert dash["data_health"]["research_subject_count"] == 1, dash["data_health"]
        assert dash["data_health"]["research_duplicate_rejections"] == 1, dash["data_health"]
        assert dash["data_health"]["research_production_collisions"] == 1, dash["data_health"]
        assert dash["keepalive_supervisor"]["status"] == "armed_for_layered_ab_review", dash[
            "keepalive_supervisor"
        ]
        assert (
            dash["keepalive_supervisor"]["stage2_status"] == "ready_for_supervised_apply_review"
        ), dash["keepalive_supervisor"]
        assert any(alert["key"] == "capacity_gemini" for alert in dash["alerts"]), dash["alerts"]
        actionable_keys = {item["key"] for item in dash["actionability"]["actionable"]}
        gated_keys = {item["key"] for item in dash["actionability"]["gated"]}
        assert "capacity_gemini" in actionable_keys, dash["actionability"]
        gemini_capacity_item = next(
            item for item in dash["actionability"]["actionable"] if item["key"] == "capacity_gemini"
        )
        assert (
            "usable but 5h soft-budget constrained" in gemini_capacity_item["reason"]
        ), gemini_capacity_item
        assert (
            "Prefer ok seats until the 5h window refreshes" in gemini_capacity_item["next_step"]
        ), gemini_capacity_item
        assert "process_renovate" in actionable_keys, dash["actionability"]
        assert "non_durable_issue_runs" in actionable_keys, dash["actionability"]
        assert "no_human_calibration" in gated_keys, dash["actionability"]
        pending_item = _classify_alert_actionability(
            {
                "key": "pending_durability",
                "severity": "info",
                "message": "fixture pending durability",
                "detail": {},
            },
            dash,
        )
        assert pending_item["status"] == "gated", pending_item
        assert "durability_sweep.py" in pending_item["next_step"], pending_item
        stale_sweep_item = _classify_alert_actionability(
            {
                "key": "durability_sweep_stale",
                "severity": "warn",
                "message": "fixture stale durability sweep",
                "detail": {},
            },
            dash,
        )
        assert stale_sweep_item["status"] == "actionable", stale_sweep_item
        assert "repair the cadence" in stale_sweep_item["next_step"], stale_sweep_item
        human_item = _classify_alert_actionability(
            {
                "key": "no_human_calibration",
                "severity": "info",
                "message": "fixture no human calibration",
                "detail": {},
            },
            dash,
        )
        assert human_item["status"] == "gated", human_item
        assert "objective-anchor" in human_item["next_step"], human_item
        assert "owner score" in human_item["next_step"], human_item
        assert "durability" not in human_item["next_step"], human_item
        stage4_route_summary = _route_coverage_summary(
            {"zero_observation_cells": 36},
            {"tasks": [{"zero_observation_agents": ["claude"]}]},
            {
                "stage": "stage_4_default_review_complete",
                "exploration_review": {"direct_ready": True, "route_ready": True},
            },
            {
                "active_backfill_eligible": False,
                "missing_cell_count": 2,
                "missing_cells": [
                    {"reasons": ["zero_observation_cell"]},
                    {"reasons": ["min_task_observations"]},
                ],
                "planned_jobs": [{"covers_cells": [{"reasons": ["zero_observation_cell"]}]}],
            },
        )
        assert stage4_route_summary["raw_zero_observation_cells"] == 36, stage4_route_summary
        assert (
            stage4_route_summary["default_review_zero_observation_cells"] == 1
        ), stage4_route_summary
        assert (
            stage4_route_summary["backfill_missing_zero_observation_cells"] == 1
        ), stage4_route_summary
        assert (
            stage4_route_summary["active_backfill_collectable_zero_observation_cells"] == 0
        ), stage4_route_summary
        route_alerts = _build_alerts(
            {"production_flow": {}},
            {"agents": []},
            {"durability_sweep_stamp_status": "fresh"},
            {"zero_observation_cells": 36},
            {
                "active_evidence_types": 1,
                "human_calibration_count": 1,
                "human_calibration_ready": True,
            },
            {"signals": [], "non_durable_issue_count": 0},
            {},
            {
                "dry_seams": {
                    "findings": [
                        {
                            "sink": "route_weights",
                            "status": "warn",
                            "finding": "routing cells are still prior-only",
                        }
                    ]
                }
            },
            stage4_route_summary,
        )
        prior_alert = next(alert for alert in route_alerts if alert["key"] == "prior_only_cells")
        dry_route_alert = next(
            alert for alert in route_alerts if alert["key"] == "dry_seam_route_weights"
        )
        assert prior_alert["severity"] == "info", prior_alert
        assert dry_route_alert["severity"] == "info", dry_route_alert
        route_dash = {
            "learning": {"route_coverage": stage4_route_summary},
            "exploration_backfill_plan": {"active_backfill_eligible": False},
            "exploration_evidence_plan": {"recommended_task_types": ["testgen"]},
            "data_health": {},
        }
        route_item = _classify_alert_actionability(prior_alert, route_dash)
        assert route_item["status"] == "informational", route_item
        active_route_summary = {
            **stage4_route_summary,
            "stage4_complete": False,
            "route_ready": False,
            "active_backfill_eligible": True,
            "active_backfill_collectable_zero_observation_cells": 1,
        }
        active_route_item = _classify_alert_actionability(
            {
                "key": "prior_only_cells",
                "severity": "warn",
                "message": "fixture prior-only cells",
                "detail": active_route_summary,
            },
            {
                "learning": {"route_coverage": active_route_summary},
                "exploration_backfill_plan": {"active_backfill_eligible": True},
                "exploration_evidence_plan": {},
                "data_health": {},
            },
        )
        assert active_route_item["status"] == "actionable", active_route_item
        assert dash["scorecard"]["actionable_alert_count"] == len(
            dash["actionability"]["actionable"]
        ), dash["scorecard"]
        artifact_status = dash["data_health"].get("langsmith_artifact_status")
        artifact_alert = next(
            (
                alert
                for alert in dash["alerts"]
                if alert["key"] == "langsmith_artifact_distribution"
            ),
            None,
        )
        if artifact_status in {"rollup_only", "partial", "dry", "unknown"}:
            assert artifact_alert is not None, dash["alerts"]
            assert artifact_alert["detail"].get("telemetry_status") == "flowing", artifact_alert
        else:
            assert artifact_alert is None, dash["alerts"]
        dry_human_alert = next(
            (alert for alert in dash["alerts"] if alert["key"] == "dry_seam_human_calibration"),
            None,
        )
        if dry_human_alert:
            assert dry_human_alert["severity"] == "info", dry_human_alert
        aggregate_gap_productivity = {
            "runs": 10,
            "outcomes": 5,
            "outcome_coverage": 0.5,
            "production_flow": {
                "status": "flowing",
                "outcome_coverage": 1.0,
                "recent_production_runs": 10,
            },
        }
        base_alert_health = {
            "outcome_gap_actionable": 0,
            "human_calibration_count": 0,
            "active_evidence_types": 1,
        }
        suppressed_coverage_alerts = _build_alerts(
            aggregate_gap_productivity,
            {"agents": []},
            {"durability_sweep_stamp_status": "fresh"},
            {"zero_observation_cells": 0},
            base_alert_health,
            {"signals": [], "non_durable_issue_count": 0},
            {},
            {"dry_seams": {"findings": []}},
        )
        assert not any(
            alert["key"] == "low_outcome_coverage" for alert in suppressed_coverage_alerts
        ), suppressed_coverage_alerts
        no_human_alert = next(
            alert for alert in suppressed_coverage_alerts if alert["key"] == "no_human_calibration"
        )
        assert no_human_alert["severity"] == "info", no_human_alert
        actionable_coverage_alerts = _build_alerts(
            aggregate_gap_productivity,
            {"agents": []},
            {"durability_sweep_stamp_status": "fresh"},
            {"zero_observation_cells": 0},
            {**base_alert_health, "outcome_gap_actionable": 2},
            {"signals": [], "non_durable_issue_count": 0},
            {},
            {"dry_seams": {"findings": []}},
        )
        assert any(
            alert["key"] == "low_outcome_coverage" for alert in actionable_coverage_alerts
        ), actionable_coverage_alerts
        assert any(alert["key"] == "process_renovate" for alert in dash["alerts"]), dash["alerts"]
        assert any(alert["key"] == "keepalive_supervisor_gate" for alert in dash["alerts"]), dash[
            "alerts"
        ]
        blocked_report = json.loads(json.dumps(report))
        blocked_stage2 = blocked_report["keepalive_supervisor"]["stage2_proposal_corpus"]
        blocked_stage2["status"] = "waiting_for_candidates"
        blocked_stage2["ready_for_supervised_apply"] = False
        blocked_stage2["ready_for_historical_replay_analysis"] = False
        blocked_stage2["historical_candidates_remaining"] = 0
        blocked_stage2["calibration_candidates_remaining"] = 0
        blocked_stage2["recommendation"] = (
            "wait for future live escalations or expand the historical source"
        )
        blocked_stage2["summary"]["historical_linked_disagreements"] = 2
        blocked_stage2["summary"]["disagreement_outcome_target"] = 3
        blocked_dash = build_dashboard(blocked_report, fake_capacity)
        assert (
            blocked_dash["keepalive_supervisor"]["stage2_status"] == "waiting_for_candidates"
        ), blocked_dash["keepalive_supervisor"]
        assert any(
            alert["key"] == "stage2_waiting_for_candidates" for alert in blocked_dash["alerts"]
        ), blocked_dash["alerts"]
        assert any(
            item["key"] == "stage2_waiting_for_candidates"
            for item in blocked_dash["actionability"]["gated"]
        ), blocked_dash["actionability"]
        live_candidate_report = json.loads(json.dumps(report))
        live_candidate_stage2 = live_candidate_report["keepalive_supervisor"][
            "stage2_proposal_corpus"
        ]
        live_candidate_stage2["live_plan"] = {
            "path": "/tmp/stage2-plan.json",
            "status": "record_live_stage2_proposals",
            "age_s": 30,
            "live_candidate_count": 1,
            "eligible_live_candidate_count": 1,
            "unrecorded_live_candidate_count": 1,
            "live_targets": ["stranske/example#1"],
            "commands": [
                {
                    "kind": "live_stage2_record",
                    "target": "stranske/example#1",
                    "command": ["python3", "roles.py", "redirect"],
                }
            ],
        }
        live_candidate_dash = build_dashboard(live_candidate_report, fake_capacity)
        assert any(
            alert["key"] == "stage2_live_candidates" for alert in live_candidate_dash["alerts"]
        ), live_candidate_dash["alerts"]
        assert any(
            item["key"] == "stage2_live_candidates"
            for item in live_candidate_dash["actionability"]["actionable"]
        ), live_candidate_dash["actionability"]
        text = format_markdown(dash)
        assert "Production outcome coverage: 100.0%" in text, text
        assert "Actionability: actionable=" in text, text
        assert "## Actionability" in text, text
        assert "Production flow: flowing" in text, text
        assert "Outcome gaps: total=0 actionable=0 advisory_or_unlinked=0" in text, text
        assert "Exploration backfill:" in text, text
        assert "Process Improvement" in text, text
        assert "Reviewed issue failures: 1" in text, text
        assert "Keepalive Supervisor Gate" in text, text
        assert "Stage 2 proposals" in text, text
        assert "Stage 2 live plan" in text, text
        if artifact_status:
            assert f"LangSmith artifacts: {artifact_status}" in text, text
        assert "LangSmith telemetry: flowing" in text, text
        assert "dashboard-fixture" in json.dumps(report["features"]), report["features"]
        print("observability_dashboard.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(temp_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Orchestrator productivity/quality dashboard."
    )
    parser.add_argument("--window-days", type=periodic_report._positive_int, default=90)
    parser.add_argument("--min-gap-recurrence", type=periodic_report._positive_int, default=3)
    parser.add_argument("--snapshot-json", type=Path, help="read feedback.snapshot_json() output")
    parser.add_argument("--json", action="store_true", help="print dashboard JSON")
    parser.add_argument(
        "--markdown", action="store_true", help="print dashboard Markdown (default)"
    )
    parser.add_argument("--write-json", type=Path, help="also write dashboard JSON to this path")
    parser.add_argument(
        "--write-markdown", type=Path, help="also write dashboard Markdown to this path"
    )
    parser.add_argument("--no-capacity", action="store_true", help="skip live capacity probe")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    if args.snapshot_json:
        report = periodic_report.build_report_from_snapshot(
            args.snapshot_json.resolve(),
            window_days=args.window_days,
            min_gap_recurrence=args.min_gap_recurrence,
        )
    else:
        report = periodic_report.build_report(
            window_days=args.window_days,
            min_gap_recurrence=args.min_gap_recurrence,
        )
    cap = None if args.no_capacity else capacity.build()
    dashboard = build_dashboard(report, cap)
    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(
            json.dumps(dashboard, indent=2, default=str) + "\n", encoding="utf-8"
        )
    markdown = format_markdown(dashboard)
    if args.write_markdown:
        args.write_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.write_markdown.write_text(markdown, encoding="utf-8")
    if args.json:
        print(json.dumps(dashboard, indent=2, default=str))
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
