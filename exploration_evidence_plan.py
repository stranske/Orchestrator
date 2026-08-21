#!/usr/bin/env python3
"""exploration_evidence_plan.py - read-only planner for exploration evidence collection.

This is the Stage 1 planner behind the exploration default-review gate. It does
not dispatch, mutate router policy, label PRs, or write state. It reads the
current exploration review, local backlog/capacity snapshots when available, and
the feedback DB, then reports exactly what evidence is still needed before any
epsilon-greedy vs Thompson-hybrid default decision is actionable.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import backlog
import capacity
import exploration_review
import feedback
import router

MODES = ("epsilon-greedy", "thompson-hybrid")
LOW_RISK_OPENER_TASK_TYPES = {"implement", "testgen", "codemod", "mechanical", "review"}
SUPERVISED_COLLECTION_RATE = 0.20
SUPERVISED_COLLECTION_DAILY_CAP = 4


def _load_json_file(path: Path | None) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _backlog_items(backlog_path: Path | None = None, backlog_payload: dict | None = None) -> list[dict]:
    payload = backlog_payload if backlog_payload is not None else _load_json_file(backlog_path or backlog.BACKLOG_JSON)
    if not payload:
        return []
    items = payload.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _capacity_snapshot(capacity_path: Path | None = None, capacity_payload: dict | None = None) -> dict:
    if capacity_payload is not None:
        return capacity_payload
    payload = _load_json_file(capacity_path or capacity.OUT)
    if payload:
        return payload
    try:
        return capacity.build()
    except Exception:
        return {"agents": {}}


def _outcomes_by_task(window_days: int) -> dict[str, int]:
    since = int(time.time()) - window_days * 86400
    try:
        with feedback._conn() as c:
            rows = c.execute(
                "SELECT r.task_type, COUNT(*) "
                "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE r.ts>=? GROUP BY r.task_type",
                (since,),
            ).fetchall()
    except Exception:
        rows = []
    return {task_type: int(count) for task_type, count in rows}


def _mode_deficits(review: dict) -> list[dict]:
    target = review["thresholds"]["min_recorded_exploration_outcomes_per_mode"]
    recorded = review.get("recorded_exploration_evidence") or {}
    by_mode = {row.get("mode"): row for row in recorded.get("mode_counts") or []}
    deficits = []
    for mode in MODES:
        row = by_mode.get(mode) or {}
        outcome_runs = int(row.get("outcome_runs") or 0)
        deficits.append({
            "mode": mode,
            "target_outcome_runs": target,
            "outcome_runs": outcome_runs,
            "remaining_outcome_runs": max(0, target - outcome_runs),
            "success_rate": row.get("success_rate"),
            "task_types": row.get("task_types") or {},
            "agents": row.get("agents") or {},
        })
    return deficits


def _route_agent_observations(task_type: str, *, version: int | None, route_table: dict) -> dict[str, int]:
    agents = exploration_review._route_agents(task_type, route_table)
    try:
        rows = feedback.current_weights(task_type, version=version)
    except Exception:
        rows = []
    observed = {str(row["agent"]): int(row.get("n_obs") or 0) for row in rows if row.get("agent")}
    return {agent: observed.get(agent, 0) for agent in agents}


def _route_coverage_deficits(review: dict, *, route_table: dict) -> dict:
    thresholds = review["thresholds"]
    version = review.get("route_weights_version")
    task_rows = []
    for task in review.get("tasks") or []:
        task_type = task["task_type"]
        agent_observations = _route_agent_observations(task_type, version=version, route_table=route_table)
        total_obs = int(task.get("total_observations") or 0)
        observed_agents = int(task.get("observed_agents") or 0)
        top_min = int(task.get("top_agent_min_observations") or 0)
        task_rows.append({
            "task_type": task_type,
            "ready_for_default_review": bool(task.get("ready_for_default_review")),
            "total_observations": total_obs,
            "needed_total_observations": max(0, thresholds["min_task_observations"] - total_obs),
            "observed_agents": observed_agents,
            "needed_observed_agents": max(0, thresholds["min_observed_agents"] - observed_agents),
            "top_agents": task.get("top_agents") or [],
            "top_agent_min_observations": top_min,
            "needed_top_agent_observations": max(0, thresholds["min_top_agent_observations"] - top_min),
            "zero_observation_agents": task.get("zero_observation_agents") or [],
            "agent_observations": agent_observations,
        })
    task_rows.sort(
        key=lambda row: (
            row["ready_for_default_review"],
            -row["total_observations"],
            row["task_type"],
        )
    )
    return {
        "ready_task_count": review.get("ready_task_count"),
        "needed_ready_task_count": max(0, thresholds["min_ready_tasks"] - int(review.get("ready_task_count") or 0)),
        "task_count": review.get("task_count"),
        "zero_observation_cell_rate": review.get("zero_observation_cell_rate"),
        "max_zero_observation_cell_rate": thresholds["max_zero_cell_rate"],
        "tasks": task_rows,
    }


def _candidate_task_types(items: list[dict], outcome_counts: dict[str, int], coverage: dict) -> list[dict]:
    opener_counts: dict[str, int] = {}
    sample_targets: dict[str, list[str]] = {}
    for item in items:
        if item.get("lane") != "opener":
            continue
        task_type = item.get("task_type") or "implement"
        opener_counts[task_type] = opener_counts.get(task_type, 0) + 1
        sample_targets.setdefault(task_type, [])
        if len(sample_targets[task_type]) < 3:
            sample_targets[task_type].append(item.get("target") or "")
    route_rows = {row["task_type"]: row for row in coverage.get("tasks") or []}
    candidates = []
    for task_type in sorted(set(route_rows) | set(opener_counts) | set(outcome_counts)):
        route = route_rows.get(task_type) or {}
        opener_items = opener_counts.get(task_type, 0)
        outcome_rows = outcome_counts.get(task_type, 0)
        low_risk = task_type in LOW_RISK_OPENER_TASK_TYPES
        has_current_openers = opener_items > 0
        has_outcome_path = outcome_rows > 0
        recommended = low_risk and has_current_openers and task_type in route_rows
        if not low_risk:
            reason = "excluded from supervised collection: planning/high-risk or no low-risk opener policy"
        elif not has_current_openers:
            reason = "not currently useful for supervised windows: no opener backlog items"
        elif task_type not in route_rows:
            reason = "not in route table"
        else:
            reason = (
                "candidate low-risk opener task type with recent outcome history"
                if has_outcome_path
                else "candidate low-risk opener task type; confirm outcome path before Stage 2"
            )
        candidates.append({
            "task_type": task_type,
            "recommended": recommended,
            "reason": reason,
            "opener_backlog_items": opener_items,
            "recent_outcome_rows": outcome_rows,
            "has_outcome_path": has_outcome_path,
            "route_ready": bool(route.get("ready_for_default_review")),
            "needed_total_observations": route.get("needed_total_observations"),
            "needed_observed_agents": route.get("needed_observed_agents"),
            "sample_targets": [target for target in sample_targets.get(task_type, []) if target],
        })
    candidates.sort(
        key=lambda row: (
            not row["recommended"],
            -row["opener_backlog_items"],
            -row["recent_outcome_rows"],
            row["task_type"],
        )
    )
    return candidates


def _capacity_summary(snapshot: dict) -> dict:
    states: dict[str, int] = {}
    usable_agents = []
    for agent, row in (snapshot.get("agents") or {}).items():
        state = row.get("state") or "unknown"
        states[state] = states.get(state, 0) + 1
        if state in {"ok", "warn"}:
            usable_agents.append(agent)
    return {
        "available": bool(snapshot.get("agents")),
        "state_counts": states,
        "usable_agents": sorted(usable_agents),
    }


def _supervised_windows(mode_deficits: list[dict], candidates: list[dict], capacity_summary: dict) -> dict:
    recommended_tasks = [row["task_type"] for row in candidates if row.get("recommended")][:3]
    has_capacity = bool(capacity_summary.get("usable_agents"))
    windows = []
    for deficit in mode_deficits:
        remaining = int(deficit["remaining_outcome_runs"])
        windows.append({
            "mode": deficit["mode"],
            "needed_outcomes": remaining,
            "eligible": remaining > 0 and bool(recommended_tasks) and has_capacity,
            "env": {
                "ORCH_EXPLORATION_EVIDENCE": "1",
                "ORCH_EXPLORATION_MODE": deficit["mode"],
                "ORCH_EXPLORATION_RATE": f"{SUPERVISED_COLLECTION_RATE:.2f}",
            },
            "candidate_task_types": recommended_tasks,
            "max_exploratory_dispatches_per_day": SUPERVISED_COLLECTION_DAILY_CAP,
        })
    return {
        "enabled_by_default": False,
        "requires_opt_in": "ORCH_EXPLORATION_EVIDENCE=1",
        "safety_requirements": [
            "opener lane only",
            "same-tier router exploration only",
            "no closer or merge-critical work",
            "no late/paygo jumps",
            "small per-day exploratory dispatch cap",
            "real outcome rows required before counts advance",
        ],
        "windows": windows,
    }


def _validation_commands() -> list[dict]:
    return [
        {
            "command": "python3 Orchestrator/exploration_evidence_plan.py --selftest",
            "purpose": "offline verification of deficit math, candidate selection, and safe-window output",
            "expected": "exploration_evidence_plan.py selftest: OK",
        },
        {
            "command": "python3 Orchestrator/exploration_evidence_plan.py --json",
            "purpose": "live read-only acquisition plan with remaining mode deficits and candidate task types",
            "expected": "read_only=true and supervised_collection.enabled_by_default=false",
        },
        {
            "command": "python3 Orchestrator/exploration_review.py --json",
            "purpose": "cross-check direct comparison readiness and route coverage gates",
            "expected": "recommendation changes only after direct and route coverage gates are ready",
        },
    ]


def _readiness_summary(
    *,
    direct_ready: bool,
    route_ready: bool,
    mode_deficits: list[dict],
    review: dict,
    candidates: list[dict],
    window_days: int,
) -> dict:
    blocks = []
    if not direct_ready:
        blocks.append("direct exploration mode evidence below threshold")
    if not route_ready:
        blocks.append("route-weight coverage gates below threshold")
    if not any(row.get("recommended") for row in candidates):
        blocks.append("no low-risk opener task types currently recommended for supervised collection")
    max_remaining = max((int(row.get("remaining_outcome_runs") or 0) for row in mode_deficits), default=0)
    recorded = review.get("recorded_exploration_evidence") or {}
    velocity = (float(recorded.get("outcome_exploration_runs") or 0) / float(window_days)) if window_days else 0.0
    estimated_days = int((max_remaining + velocity - 1) // velocity) if velocity > 0 and max_remaining else None
    return {
        "overall_ready": direct_ready and route_ready,
        "blocks_to_stage_2_or_review": blocks,
        "max_remaining_mode_outcomes": max_remaining,
        "outcome_exploration_runs_per_day": velocity,
        "estimated_days_to_direct_evidence_ready": estimated_days,
    }


def build_plan(
    *,
    backlog_path: Path | None = None,
    capacity_path: Path | None = None,
    backlog_payload: dict | None = None,
    capacity_payload: dict | None = None,
    route_table: dict | None = None,
    trials: int = 100,
    window_days: int = 120,
) -> dict:
    route_table = route_table or router.ROUTE_TABLE
    review = exploration_review.build_report(route_table=route_table, trials=trials)
    mode_deficits = _mode_deficits(review)
    coverage = _route_coverage_deficits(review, route_table=route_table)
    items = _backlog_items(backlog_path, backlog_payload)
    outcomes = _outcomes_by_task(window_days)
    candidates = _candidate_task_types(items, outcomes, coverage)
    cap_summary = _capacity_summary(_capacity_snapshot(capacity_path, capacity_payload))
    windows = _supervised_windows(mode_deficits, candidates, cap_summary)

    direct_ready = bool((review.get("recorded_exploration_evidence") or {}).get("ready_for_direct_comparison"))
    route_ready = (
        int(review.get("ready_task_count") or 0) >= review["thresholds"]["min_ready_tasks"]
        and float(review.get("zero_observation_cell_rate") or 1.0) <= review["thresholds"]["max_zero_cell_rate"]
    )
    default_mode = review.get("current_default")
    recommendation = review.get("recommendation")
    if direct_ready and route_ready and default_mode == "thompson-hybrid" and recommendation == "consider_thompson_hybrid_default":
        stage = "stage_4_default_review_complete"
        next_action = "monitor Thompson-hybrid default outcomes and durability; no route-coverage backfill needed"
    elif direct_ready and route_ready and default_mode == "epsilon-greedy" and recommendation == "keep_epsilon_greedy":
        stage = "stage_4_default_review_complete"
        next_action = "keep epsilon-greedy default; monitor future Thompson-hybrid evidence before another policy change"
    elif direct_ready and route_ready:
        stage = "stage_4_default_review_ready"
        next_action = "run the default review; do not change policy without comparing direct outcomes and simulations"
    elif any(row["recommended"] for row in candidates):
        stage = "stage_1_read_only_planner"
        next_action = "review supervised window recommendations; enable Stage 2 only by explicit opt-in if passive progress stalls"
    else:
        stage = "stage_1_needs_backlog_or_research_targets"
        next_action = "refresh backlog or schedule targeted research/A-B jobs before supervised collection"

    return {
        "generated_at": int(time.time()),
        "read_only": True,
        "stage": stage,
        "next_action": next_action,
        "exploration_review": {
            "route_weights_version": review.get("route_weights_version"),
            "current_default": default_mode,
            "status": review.get("status"),
            "recommendation": recommendation,
            "reason": review.get("reason"),
            "direct_ready": direct_ready,
            "route_ready": route_ready,
        },
        "direct_mode_deficits": mode_deficits,
        "route_coverage_deficits": coverage,
        "candidate_task_types": candidates,
        "capacity": cap_summary,
        "supervised_collection": windows,
        "readiness": _readiness_summary(
            direct_ready=direct_ready,
            route_ready=route_ready,
            mode_deficits=mode_deficits,
            review=review,
            candidates=candidates,
            window_days=window_days,
        ),
        "validation_commands": _validation_commands(),
    }


def format_human(plan: dict) -> str:
    lines = [
        "exploration_evidence_plan: "
        f"stage={plan['stage']} "
        f"recommendation={plan['exploration_review']['recommendation']} "
        f"direct_ready={plan['exploration_review']['direct_ready']} "
        f"route_ready={plan['exploration_review']['route_ready']}",
        f"next: {plan['next_action']}",
        "mode deficits:",
    ]
    for row in plan["direct_mode_deficits"]:
        lines.append(
            f"  {row['mode']}: {row['outcome_runs']}/{row['target_outcome_runs']} "
            f"outcome runs, remaining={row['remaining_outcome_runs']}"
        )
    coverage = plan["route_coverage_deficits"]
    lines.append(
        "route coverage: "
        f"ready_tasks={coverage['ready_task_count']}/{coverage['task_count']} "
        f"needed_ready_tasks={coverage['needed_ready_task_count']} "
        f"zero_cell_rate={coverage['zero_observation_cell_rate']:.1%}"
    )
    candidates = [row for row in plan["candidate_task_types"] if row.get("recommended")]
    if candidates:
        lines.append("candidate task types:")
        for row in candidates[:5]:
            lines.append(
                f"  {row['task_type']}: opener_items={row['opener_backlog_items']} "
                f"recent_outcomes={row['recent_outcome_rows']} "
                f"route_ready={row['route_ready']}"
            )
    else:
        lines.append("candidate task types: none currently recommended")
    lines.append("supervised windows: disabled by default; require ORCH_EXPLORATION_EVIDENCE=1")
    for row in plan["supervised_collection"]["windows"]:
        lines.append(
            f"  {row['mode']}: eligible={row['eligible']} needed={row['needed_outcomes']} "
            f"rate={row['env']['ORCH_EXPLORATION_RATE']} daily_cap={row['max_exploratory_dispatches_per_day']}"
        )
    readiness = plan.get("readiness") or {}
    if readiness.get("blocks_to_stage_2_or_review"):
        lines.append("blocks:")
        for block in readiness["blocks_to_stage_2_or_review"]:
            lines.append(f"  - {block}")
    return "\n".join(lines)


def _insert_weight(c, version: int, task_type: str, agent: str, n_obs: int) -> None:
    now = int(time.time())
    posterior = 0.7
    c.execute(
        "INSERT OR REPLACE INTO route_weights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version,
            now,
            task_type,
            agent,
            posterior,
            posterior,
            n_obs,
            posterior,
            None,
            posterior,
            "exploration_evidence_plan selftest",
            now - 86400,
            now,
        ),
    )


def _selftest() -> None:
    old_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="exploration-evidence-plan-")
    feedback.DB_PATH = Path(tmp) / "feedback.db"
    try:
        now = int(time.time())
        with feedback._conn() as c:
            version = 1
            for task_type in ("implement", "testgen", "codemod"):
                for index, agent in enumerate(exploration_review._route_agents(task_type)[:3]):
                    _insert_weight(c, version, task_type, agent, 6 - index)
        for mode, count in (("epsilon-greedy", 5), ("thompson-hybrid", 2)):
            for i in range(count):
                task_type = ("implement", "testgen", "codemod")[i % 3]
                rid = f"{mode}-{i}"
                feedback.record_run(
                    rid,
                    f"o/r#{i}",
                    task_type,
                    "codex",
                    ts=now - i,
                    routing_metadata={
                        "source": "router_assignment",
                        "exploration": True,
                        "exploration_mode": mode,
                    },
                )
                feedback.record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
        backlog_payload = {
            "items": [
                {"target": "o/r#1", "task_type": "implement", "lane": "opener"},
                {"target": "o/r#2", "task_type": "implement", "lane": "opener"},
                {"target": "o/r#3", "task_type": "testgen", "lane": "opener"},
                {"target": "o/r#4", "task_type": "epic", "lane": "opener"},
                {"target": "o/r#5", "task_type": "implement", "lane": "closer"},
            ]
        }
        capacity_payload = {"agents": {"codex": {"state": "ok"}, "vibe": {"state": "ok"}}}
        plan = build_plan(
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            route_table={
                "implement": router.ROUTE_TABLE["implement"],
                "testgen": router.ROUTE_TABLE["testgen"],
                "codemod": router.ROUTE_TABLE["codemod"],
                "epic": router.ROUTE_TABLE["epic"],
            },
            trials=10,
        )
        deficits = {row["mode"]: row for row in plan["direct_mode_deficits"]}
        assert deficits["epsilon-greedy"]["remaining_outcome_runs"] == 25, deficits
        assert deficits["thompson-hybrid"]["remaining_outcome_runs"] == 28, deficits
        candidates = {row["task_type"]: row for row in plan["candidate_task_types"]}
        assert candidates["implement"]["recommended"] is True, candidates["implement"]
        assert candidates["testgen"]["recommended"] is True, candidates["testgen"]
        assert candidates["epic"]["recommended"] is False, candidates["epic"]
        assert plan["supervised_collection"]["enabled_by_default"] is False, plan["supervised_collection"]
        assert all(row["eligible"] for row in plan["supervised_collection"]["windows"]), plan
        assert plan["readiness"]["max_remaining_mode_outcomes"] == 28, plan["readiness"]
        assert plan["validation_commands"], plan
        assert "ORCH_EXPLORATION_EVIDENCE=1" in format_human(plan), format_human(plan)
        print("exploration_evidence_plan.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only exploration evidence acquisition planner.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--backlog-json", type=Path, help="local backlog snapshot to read")
    parser.add_argument("--capacity-json", type=Path, help="local capacity snapshot to read")
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--window-days", type=int, default=120)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    plan = build_plan(
        backlog_path=args.backlog_json,
        capacity_path=args.capacity_json,
        trials=max(1, args.trials),
        window_days=max(1, args.window_days),
    )
    if args.as_json:
        print(json.dumps(plan, indent=2))
    else:
        print(format_human(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
