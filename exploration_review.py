#!/usr/bin/env python3
"""exploration_review.py - read-only epsilon vs Thompson-hybrid policy review.

The router already supports two exploration modes:

- epsilon-greedy: when the epsilon gate fires, pick a same-tier least-observed
  challenger.
- thompson-hybrid: keep the same epsilon gate, but choose a same-tier challenger
  from a reconstructed posterior sample.

This module answers the backlog question "is there enough outcome volume to
change the default?" without mutating router policy. It reads latest
route_weights, simulates both challenger selectors, and emits a conservative
recommendation.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import feedback
import router

MIN_TASK_OBS = 20
MIN_OBSERVED_AGENTS = 3
MIN_TOP_AGENT_OBS = 3
MIN_READY_TASKS = 3
MAX_ZERO_CELL_RATE = 0.25
MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE = 30
MIN_RECORDED_EXPLORATION_TASK_TYPES = 3
SIM_TRIALS = 400


def _route_agents(task_type: str, route_table: dict | None = None) -> list[str]:
    spec = (route_table or router.ROUTE_TABLE).get(task_type) or {}
    out: list[str] = []
    for row in spec.get("agents") or []:
        agent = row.get("agent")
        if not agent or agent in out or agent in router.BACKUP_AGENTS:
            continue
        out.append(agent)
    return out


def _all_route_agents(route_table: dict | None = None) -> list[str]:
    agents: list[str] = []
    for task_type in route_table or router.ROUTE_TABLE:
        for agent in _route_agents(task_type, route_table):
            if agent not in agents:
                agents.append(agent)
    return agents


def _neutral_capacity(route_table: dict | None = None) -> dict:
    return {"agents": {agent: {"state": "ok"} for agent in _all_route_agents(route_table)}}


def _latest_version() -> int:
    try:
        with feedback._conn() as c:
            return int(
                c.execute("SELECT COALESCE(MAX(version),0) FROM route_weights").fetchone()[0]
            )
    except Exception:
        return 0


def _current_rows(task_type: str, *, version: int | None = None) -> list[dict]:
    try:
        return feedback.current_weights(task_type, version=version)
    except Exception:
        return []


def _learned(rows: list[dict]) -> dict[str, dict]:
    return {
        str(row["agent"]): {
            "rank": index,
            "n_obs": int(row.get("n_obs") or 0),
            "posterior": row.get("posterior"),
            "score": row.get("score"),
        }
        for index, row in enumerate(rows)
        if row.get("agent")
    }


def _agent_metric(rows_by_agent: dict[str, dict], agent: str, key: str) -> float:
    row = rows_by_agent.get(agent) or {}
    try:
        value = row.get(key)
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _simulate_mode(
    task_type: str,
    learned: dict[str, dict],
    rows_by_agent: dict[str, dict],
    *,
    mode: str,
    trials: int,
    route_table: dict | None = None,
) -> dict:
    cap = _neutral_capacity(route_table)
    counts: dict[str, int] = {}
    score_total = 0.0
    posterior_total = 0.0
    for seed in range(max(1, trials)):
        pick = router.select_agent(
            task_type,
            cap,
            learned=learned,
            exploration_rate=1.0,
            exploration_mode=mode,
            rng=random.Random(seed),
        )
        if not pick:
            continue
        agent = pick["agent"]
        counts[agent] = counts.get(agent, 0) + 1
        score_total += _agent_metric(rows_by_agent, agent, "score")
        posterior_total += _agent_metric(rows_by_agent, agent, "posterior")
    total = sum(counts.values())
    return {
        "mode": mode,
        "trials": total,
        "selection_counts": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        "expected_score": (score_total / total) if total else None,
        "expected_posterior": (posterior_total / total) if total else None,
    }


def _task_summary(
    task_type: str,
    *,
    version: int | None = None,
    route_table: dict | None = None,
    trials: int = SIM_TRIALS,
) -> dict:
    agents = _route_agents(task_type, route_table)
    rows = _current_rows(task_type, version=version)
    rows_by_agent = {str(row["agent"]): row for row in rows if row.get("agent")}
    learned = _learned(rows)
    total_obs = sum(int((rows_by_agent.get(agent) or {}).get("n_obs") or 0) for agent in agents)
    observed_agents = sum(
        1 for agent in agents if int((rows_by_agent.get(agent) or {}).get("n_obs") or 0) > 0
    )
    zero_agents = [
        agent for agent in agents if int((rows_by_agent.get(agent) or {}).get("n_obs") or 0) == 0
    ]
    ordered = [agent for agent in [row.get("agent") for row in rows] if agent in agents]
    top_agents = ordered[:2]
    top_agent_min_obs = min(
        [int((rows_by_agent.get(agent) or {}).get("n_obs") or 0) for agent in top_agents],
        default=0,
    )
    ready = (
        total_obs >= MIN_TASK_OBS
        and observed_agents >= MIN_OBSERVED_AGENTS
        and top_agent_min_obs >= MIN_TOP_AGENT_OBS
    )
    exploitation = router.select_agent(
        task_type,
        _neutral_capacity(route_table),
        learned=learned,
        exploration_rate=0.0,
    )
    epsilon = _simulate_mode(
        task_type,
        learned,
        rows_by_agent,
        mode="epsilon-greedy",
        trials=trials,
        route_table=route_table,
    )
    thompson = _simulate_mode(
        task_type,
        learned,
        rows_by_agent,
        mode="thompson-hybrid",
        trials=trials,
        route_table=route_table,
    )
    return {
        "task_type": task_type,
        "agents": agents,
        "total_observations": total_obs,
        "observed_agents": observed_agents,
        "zero_observation_agents": zero_agents,
        "top_agents": top_agents,
        "top_agent_min_observations": top_agent_min_obs,
        "ready_for_default_review": ready,
        "exploitation_pick": exploitation,
        "epsilon_greedy": epsilon,
        "thompson_hybrid": thompson,
    }


def _decode_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _recorded_exploration_evidence(window_days: int = 120) -> dict:
    since = int(time.time()) - window_days * 86400
    by_mode: dict[str, dict] = {}
    task_types_with_outcomes: set[str] = set()
    instrumented_runs = 0
    router_decision_runs = 0
    exploration_runs = 0
    outcome_exploration_runs = 0
    try:
        with feedback._conn() as c:
            rows = c.execute(
                "SELECT r.run_id, r.task_type, r.agent, r.routing_metadata, "
                "o.durability, o.adjudicated_verdict, o.verifier_verdict "
                "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE r.routing_metadata IS NOT NULL AND r.ts>=?",
                (since,),
            ).fetchall()
    except Exception:
        rows = []
    for run_id, task_type, agent, raw_metadata, durability, adjudicated, verifier in rows:
        metadata = _decode_metadata(raw_metadata)
        if not metadata:
            continue
        instrumented_runs += 1
        if metadata.get("source") != "router_assignment":
            continue
        router_decision_runs += 1
        if not metadata.get("exploration"):
            continue
        exploration_runs += 1
        mode = metadata.get("exploration_mode") or "epsilon-greedy"
        stat = by_mode.setdefault(
            mode,
            {
                "mode": mode,
                "runs": 0,
                "outcome_runs": 0,
                "successes": 0,
                "success_rate": None,
                "task_types": {},
                "agents": {},
            },
        )
        stat["runs"] += 1
        stat["task_types"][task_type] = stat["task_types"].get(task_type, 0) + 1
        stat["agents"][agent] = stat["agents"].get(agent, 0) + 1
        if feedback._has_outcome_evidence(durability, adjudicated, verifier):
            outcome_exploration_runs += 1
            stat["outcome_runs"] += 1
            task_types_with_outcomes.add(task_type)
            if feedback._is_success(durability, adjudicated, verifier):
                stat["successes"] += 1
    for stat in by_mode.values():
        if stat["outcome_runs"]:
            stat["success_rate"] = stat["successes"] / stat["outcome_runs"]
        stat["task_types"] = dict(sorted(stat["task_types"].items()))
        stat["agents"] = dict(sorted(stat["agents"].items()))
    modes = {
        mode: stat["outcome_runs"]
        for mode, stat in by_mode.items()
        if mode in {"epsilon-greedy", "thompson-hybrid"}
    }
    missing_modes = [
        mode
        for mode in ("epsilon-greedy", "thompson-hybrid")
        if modes.get(mode, 0) < MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE
    ]
    ready_tasks = len(task_types_with_outcomes)
    if missing_modes:
        ready = False
        reason = (
            "need at least "
            f"{MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE} outcome-bearing exploration runs "
            f"for each mode; below threshold for {', '.join(missing_modes)}"
        )
    elif ready_tasks < MIN_RECORDED_EXPLORATION_TASK_TYPES:
        ready = False
        reason = (
            f"need exploration outcomes across {MIN_RECORDED_EXPLORATION_TASK_TYPES} task types; "
            f"currently {ready_tasks}"
        )
    else:
        ready = True
        reason = "direct instrumented exploration outcome volume is ready for mode comparison"
    return {
        "window_days": window_days,
        "instrumented_runs": instrumented_runs,
        "router_decision_runs": router_decision_runs,
        "exploration_runs": exploration_runs,
        "outcome_exploration_runs": outcome_exploration_runs,
        "task_types_with_outcomes": sorted(task_types_with_outcomes),
        "mode_counts": sorted(by_mode.values(), key=lambda item: item["mode"]),
        "ready_for_direct_comparison": ready,
        "reason": reason,
    }


def build_report(
    *,
    route_table: dict | None = None,
    version: int | None = None,
    trials: int = SIM_TRIALS,
) -> dict:
    route_table = route_table or router.ROUTE_TABLE
    version = _latest_version() if version is None else version
    tasks = [
        _task_summary(task_type, version=version, route_table=route_table, trials=trials)
        for task_type in route_table
    ]
    total_cells = sum(len(task["agents"]) for task in tasks)
    zero_cells = sum(len(task["zero_observation_agents"]) for task in tasks)
    ready_tasks = [task for task in tasks if task["ready_for_default_review"]]
    zero_cell_rate = (zero_cells / total_cells) if total_cells else 1.0
    recorded_evidence = _recorded_exploration_evidence()

    if not tasks or version == 0:
        status = "no_route_weight_evidence"
        recommendation = "keep_epsilon_greedy"
        reason = "no route_weights are available yet"
    elif len(ready_tasks) < MIN_READY_TASKS:
        status = "insufficient_task_coverage"
        recommendation = "keep_epsilon_greedy"
        reason = (
            f"{len(ready_tasks)} task types meet the evidence gate; "
            f"{MIN_READY_TASKS} are required before changing the default"
        )
    elif zero_cell_rate > MAX_ZERO_CELL_RATE:
        status = "too_many_zero_observation_cells"
        recommendation = "keep_epsilon_greedy"
        reason = (
            f"{zero_cell_rate:.1%} of route cells have zero observations; "
            f"threshold is {MAX_ZERO_CELL_RATE:.1%}"
        )
    elif not recorded_evidence["ready_for_direct_comparison"]:
        status = "direct_mode_evidence_not_ready"
        recommendation = "keep_epsilon_greedy"
        reason = recorded_evidence["reason"]
    else:
        mode_rows = {row["mode"]: row for row in recorded_evidence["mode_counts"]}
        epsilon_rate = (mode_rows.get("epsilon-greedy") or {}).get("success_rate")
        thompson_rate = (mode_rows.get("thompson-hybrid") or {}).get("success_rate")
        thompson_wins = 0
        comparable = 0
        for task in ready_tasks:
            e_score = task["epsilon_greedy"].get("expected_score")
            t_score = task["thompson_hybrid"].get("expected_score")
            if e_score is None or t_score is None:
                continue
            comparable += 1
            if t_score >= e_score:
                thompson_wins += 1
        if (
            comparable
            and thompson_wins >= max(1, comparable // 2 + comparable % 2)
            and thompson_rate is not None
            and epsilon_rate is not None
            and thompson_rate >= epsilon_rate
        ):
            status = "eligible_for_thompson_default_review"
            recommendation = "consider_thompson_hybrid_default"
            reason = (
                f"evidence gates are met and Thompson-hybrid has equal-or-better "
                f"simulated expected score in {thompson_wins}/{comparable} ready tasks "
                f"plus direct success_rate {thompson_rate:.3f} vs epsilon {epsilon_rate:.3f}"
            )
        else:
            status = "epsilon_still_preferred"
            recommendation = "keep_epsilon_greedy"
            reason = (
                "evidence gates are met, but Thompson-hybrid does not improve both "
                "simulated challenger quality and direct exploration outcomes"
            )

    return {
        "generated_at": int(time.time()),
        "read_only": True,
        "route_weights_version": version,
        "current_default": router.EXPLORATION_MODE_DEFAULT,
        "recommendation": recommendation,
        "status": status,
        "reason": reason,
        "thresholds": {
            "min_task_observations": MIN_TASK_OBS,
            "min_observed_agents": MIN_OBSERVED_AGENTS,
            "min_top_agent_observations": MIN_TOP_AGENT_OBS,
            "min_ready_tasks": MIN_READY_TASKS,
            "max_zero_cell_rate": MAX_ZERO_CELL_RATE,
            "min_recorded_exploration_outcomes_per_mode": MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE,
            "min_recorded_exploration_task_types": MIN_RECORDED_EXPLORATION_TASK_TYPES,
            "simulation_trials": trials,
        },
        "ready_task_count": len(ready_tasks),
        "task_count": len(tasks),
        "zero_observation_cell_rate": zero_cell_rate,
        "recorded_exploration_evidence": recorded_evidence,
        "tasks": tasks,
    }


def format_human(report: dict) -> str:
    lines = [
        "exploration_review: "
        f"version={report['route_weights_version'] or 'none'} "
        f"default={report['current_default']} "
        f"status={report['status']} "
        f"recommendation={report['recommendation']}",
        f"reason: {report['reason']}",
        (
            "coverage: "
            f"ready_tasks={report['ready_task_count']}/{report['task_count']} "
            f"zero_cell_rate={report['zero_observation_cell_rate']:.1%}"
        ),
        (
            "recorded exploration: "
            f"instrumented={report['recorded_exploration_evidence']['instrumented_runs']} "
            f"exploration_outcomes={report['recorded_exploration_evidence']['outcome_exploration_runs']} "
            f"ready={report['recorded_exploration_evidence']['ready_for_direct_comparison']}"
        ),
    ]
    for task in report["tasks"]:
        lines.append(
            f"  {task['task_type']}: obs={task['total_observations']} "
            f"agents={task['observed_agents']}/{len(task['agents'])} "
            f"ready={task['ready_for_default_review']} "
            f"exploit={(task['exploitation_pick'] or {}).get('agent') or '-'}"
        )
    return "\n".join(lines)


def _insert_weight(
    c, version: int, task_type: str, agent: str, posterior: float, n_obs: int, rank: int
) -> None:
    now = int(time.time())
    score = posterior
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
            score,
            f"selftest rank={rank}",
            now - 86400,
            now,
        ),
    )


def _selftest() -> None:
    old_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="exploration-review-")
    feedback.DB_PATH = Path(tmp) / "feedback.db"
    try:
        empty = build_report(route_table={"implement": router.ROUTE_TABLE["implement"]}, trials=10)
        assert empty["recommendation"] == "keep_epsilon_greedy", empty
        with feedback._conn() as c:
            version = 1
            for task_type in ("implement", "testgen", "codemod"):
                for rank, agent in enumerate(_route_agents(task_type)[:4]):
                    _insert_weight(c, version, task_type, agent, 0.8 - rank * 0.05, 6, rank)
        ready = build_report(
            route_table={
                "implement": router.ROUTE_TABLE["implement"],
                "testgen": router.ROUTE_TABLE["testgen"],
                "codemod": router.ROUTE_TABLE["codemod"],
            },
            version=1,
            trials=30,
        )
        assert ready["ready_task_count"] == 3, ready
        assert ready["recommendation"] == "keep_epsilon_greedy", ready
        assert ready["status"] == "direct_mode_evidence_not_ready", ready
        assert "epsilon_greedy" in ready["tasks"][0], ready["tasks"][0]
        now = int(time.time())
        task_types = ("implement", "testgen", "codemod")
        for mode in ("epsilon-greedy", "thompson-hybrid"):
            for i in range(MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE):
                task_type = task_types[i % len(task_types)]
                agent = _route_agents(task_type)[0]
                rid = f"{mode}-{i}"
                feedback.record_run(
                    rid,
                    f"o/r#{mode}-{i}",
                    task_type,
                    agent,
                    ts=now - i,
                    routing_metadata={
                        "source": "router_assignment",
                        "exploration": True,
                        "exploration_mode": mode,
                    },
                )
                feedback.record_outcome(
                    rid, adjudicated_verdict="PASS", merged=True, durability="durable"
                )
        direct_ready = build_report(
            route_table={
                "implement": router.ROUTE_TABLE["implement"],
                "testgen": router.ROUTE_TABLE["testgen"],
                "codemod": router.ROUTE_TABLE["codemod"],
            },
            version=1,
            trials=30,
        )
        recorded = direct_ready["recorded_exploration_evidence"]
        assert recorded["ready_for_direct_comparison"] is True, recorded
        mode_counts = {row["mode"]: row for row in recorded["mode_counts"]}
        assert (
            mode_counts["epsilon-greedy"]["outcome_runs"]
            == MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE
        )
        assert (
            mode_counts["thompson-hybrid"]["outcome_runs"]
            == MIN_RECORDED_EXPLORATION_OUTCOMES_PER_MODE
        )
        print("exploration_review.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only epsilon-greedy vs Thompson-hybrid exploration policy review."
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--trials", type=int, default=SIM_TRIALS)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    report = build_report(trials=max(1, args.trials))
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(format_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
