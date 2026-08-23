#!/usr/bin/env python3
"""exploration_backfill.py - targeted route-coverage backfill planner.

This is Stage 3 of the exploration evidence ramp. Stage 1 reports the evidence
deficit and Stage 2 collects direct exploration-mode outcomes on low-risk opener
work. Stage 3 is narrower: when direct mode counts are moving but route-weight
coverage still lags, schedule real A/B research jobs for the missing
``(task_type, agent)`` cells. Nothing in this module writes synthetic evidence;
route weights move only after ``exp_abcd evaluate`` records real evaluations or
after normal production outcomes are ingested.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

import claims
import exp_abcd
import exploration_evidence_plan
import exploration_review
import feedback
import router

ENV_FLAG = "ORCH_EXPLORATION_BACKFILL"
BACKFILL_CLAIM_AGENT = "exploration-backfill"
DEFAULT_MAX_JOBS = 3
MAX_ARMS_PER_JOB = 5


def _route_agents(task_type: str, route_table: dict) -> list[str]:
    return exploration_review._route_agents(task_type, route_table)


def _capacity_states(capacity_snapshot: dict) -> dict[str, str]:
    return {
        agent: (row or {}).get("state", "unknown")
        for agent, row in (capacity_snapshot.get("agents") or {}).items()
    }


def _agent_available(agent: str, states: dict[str, str]) -> bool:
    return states.get(agent, "unknown") in {"ok", "warn"}


def _cell_target(
    task_type: str, agent: str, task_row: dict, route_agents: list[str]
) -> tuple[int, list[str]]:
    observed = int((task_row.get("agent_observations") or {}).get(agent, 0) or 0)
    target = 0
    reasons: list[str] = []
    if observed == 0:
        target = max(target, 1)
        reasons.append("zero_observation_cell")
    if (
        agent in set(task_row.get("top_agents") or [])
        and observed < exploration_review.MIN_TOP_AGENT_OBS
    ):
        target = max(target, exploration_review.MIN_TOP_AGENT_OBS)
        reasons.append("top_agent_min_observations")
    if (
        int(task_row.get("observed_agents") or 0) < exploration_review.MIN_OBSERVED_AGENTS
        and observed == 0
    ):
        target = max(target, 1)
        reasons.append("min_observed_agents")
    if int(task_row.get("needed_total_observations") or 0) > 0:
        per_agent_target = max(
            1,
            (exploration_review.MIN_TASK_OBS + max(1, len(route_agents)) - 1)
            // max(1, len(route_agents)),
        )
        if observed < per_agent_target:
            target = max(target, per_agent_target)
            reasons.append("min_task_observations")
    return target, reasons


def _missing_cells(coverage: dict, *, route_table: dict) -> list[dict]:
    cells: list[dict] = []
    for task_row in coverage.get("tasks") or []:
        task_type = task_row.get("task_type")
        if not task_type or task_row.get("ready_for_default_review"):
            continue
        agents = _route_agents(task_type, route_table)
        for index, agent in enumerate(agents):
            if agent in router.BACKUP_AGENTS:
                continue
            observed = int((task_row.get("agent_observations") or {}).get(agent, 0) or 0)
            target, reasons = _cell_target(task_type, agent, task_row, agents)
            if observed >= target or not reasons:
                continue
            priority = (
                (0 if "zero_observation_cell" in reasons else 1),
                (0 if "top_agent_min_observations" in reasons else 1),
                observed,
                index,
            )
            cells.append(
                {
                    "task_type": task_type,
                    "agent": agent,
                    "observations": observed,
                    "target_observations": target,
                    "needed_observations": max(0, target - observed),
                    "reasons": reasons,
                    "route_index": index,
                    "task_needed_total_observations": task_row.get("needed_total_observations"),
                    "task_needed_observed_agents": task_row.get("needed_observed_agents"),
                    "task_needed_top_agent_observations": task_row.get(
                        "needed_top_agent_observations"
                    ),
                    "_priority": priority,
                }
            )
    cells.sort(key=lambda row: (row["_priority"], row["task_type"], row["agent"]))
    for row in cells:
        row.pop("_priority", None)
    return cells


def _direct_mode_progress(review: dict) -> dict:
    recorded = review.get("recorded_exploration_evidence") or {}
    mode_counts = {
        row.get("mode"): int(row.get("outcome_runs") or 0)
        for row in recorded.get("mode_counts") or []
    }
    outcome_runs = int(recorded.get("outcome_exploration_runs") or 0)
    exploration_runs = int(recorded.get("exploration_runs") or 0)
    return {
        "progressing": outcome_runs > 0,
        "exploration_runs": exploration_runs,
        "outcome_exploration_runs": outcome_runs,
        "mode_outcome_runs": mode_counts,
        "ready_for_direct_comparison": bool(recorded.get("ready_for_direct_comparison")),
    }


def _route_lagging(acquisition: dict) -> bool:
    review = acquisition.get("exploration_review") or {}
    return not bool(review.get("route_ready"))


def _target_repo(target: str) -> str | None:
    if not target or "#" not in target:
        return None
    return target.split("#", 1)[0]


# FIVE `_slug`-SHAPED HELPERS EXIST IN THIS TREE AND THEY MUST NOT BE UNIFIED.
# `redirect_shadow._corpus_entry_slug` KEEPS `#`, `redirect_plan._prompt_path_slug` STRIPS it, and
# `exploration_backfill._exp_id_slug` uses `-` and does not map `/`->`__`. That divergence was
# filed as a hygiene item ("same target != same key across modules"), and the fix is NOT to merge
# them: verified 2026-08-21 that nothing joins their outputs -- each feeds a different identifier
# namespace (corpus entry_id / prompt file path / experiment id), and unifying would rewrite
# existing entry_ids, prompt paths and `backfill-` exp_ids, breaking dedupe against historical
# rows for no gain. The real hazard is that a shared NAME invites a future join, so each is named
# for its namespace instead. If you need a target key that crosses modules, add one deliberately;
# do not reach for whichever of these is nearest.
# The hygiene item said THREE; it is five. The other two are `claims._slug` (claim file path,
# and the only one deliberately called cross-module -- range_lane_rollout uses it to build a
# claim path, which is correct BECAUSE it is module-qualified) and `partitioned_review._slug`
# (partition_id, 48-char capped). Both are also namespace-local; neither was renamed because
# their names are already reached through their module.
def _exp_id_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-") or "target"


def _items_by_task(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    active = claims.active_claims()
    consumed = _evaluated_backfill_targets()
    for item in items:
        target = str(item.get("target") or "")
        task_type = item.get("task_type") or "implement"
        if item.get("lane") != "opener":
            continue
        if target in active:
            continue
        if target in consumed:
            continue
        out.setdefault(task_type, []).append(item)
    return out


def _target_from_backfill_spec(spec_file: Path) -> str | None:
    try:
        text = spec_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Target: "):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def _target_from_backfill_exp(exp_id: str) -> str | None:
    exp_dir = exp_abcd.exp_paths(exp_id)
    meta_file = exp_dir / "meta.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    target = str(meta.get("backfill_target") or "").strip()
    if target:
        return target
    return _target_from_backfill_spec(exp_dir / "spec.md")


def _evaluated_backfill_targets() -> set[str]:
    try:
        with feedback._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT experiment_id FROM evaluations WHERE experiment_id LIKE 'backfill-%'"
            ).fetchall()
    except Exception:
        return set()
    targets: set[str] = set()
    for (exp_id,) in rows:
        target = _target_from_backfill_exp(str(exp_id or ""))
        if target:
            targets.add(target)
    return targets


def _anchor_agent(
    task_type: str, missing_agents: list[str], states: dict[str, str], route_table: dict
) -> str | None:
    for agent in _route_agents(task_type, route_table):
        if agent in router.BACKUP_AGENTS:
            continue
        if agent in missing_agents:
            continue
        if _agent_available(agent, states):
            return agent
    for agent in _route_agents(task_type, route_table):
        if agent not in router.BACKUP_AGENTS and agent not in missing_agents:
            return agent
    return None


def _build_jobs(
    *,
    cells: list[dict],
    items: list[dict],
    capacity_snapshot: dict,
    route_table: dict,
    max_jobs: int,
) -> tuple[list[dict], list[dict]]:
    states = _capacity_states(capacity_snapshot)
    items_by_task = _items_by_task(items)
    cells_by_task: dict[str, list[dict]] = {}
    for cell in cells:
        cells_by_task.setdefault(cell["task_type"], []).append(cell)

    jobs: list[dict] = []
    blocked: list[dict] = []
    for task_type, task_cells in sorted(cells_by_task.items(), key=lambda item: item[0]):
        candidates = items_by_task.get(task_type) or []
        if not candidates:
            blocked.append(
                {
                    "task_type": task_type,
                    "reason": "no unclaimed opener backlog item for this task type",
                    "missing_cells": [
                        {
                            k: cell[k]
                            for k in (
                                "task_type",
                                "agent",
                                "observations",
                                "target_observations",
                                "needed_observations",
                                "reasons",
                            )
                        }
                        for cell in task_cells[:10]
                    ],
                }
            )
            continue
        for item in candidates:
            if len(jobs) >= max_jobs:
                break
            missing_agents = []
            covered_cells = []
            for cell in task_cells:
                agent = cell["agent"]
                if agent in missing_agents:
                    continue
                if not _agent_available(agent, states):
                    continue
                missing_agents.append(agent)
                covered_cells.append(cell)
                if len(missing_agents) >= MAX_ARMS_PER_JOB:
                    break
            if not missing_agents:
                blocked.append(
                    {
                        "task_type": task_type,
                        "target": item.get("target"),
                        "reason": "missing-cell agents are not currently ok/warn in capacity snapshot",
                    }
                )
                continue
            anchor = _anchor_agent(task_type, missing_agents, states, route_table)
            agents = list(missing_agents)
            if anchor and anchor not in agents:
                agents.insert(0, anchor)
            if len(agents) < 2:
                blocked.append(
                    {
                        "task_type": task_type,
                        "target": item.get("target"),
                        "reason": "need at least two runnable agents for an A/B backfill",
                        "agents": agents,
                    }
                )
                continue
            agents = agents[:MAX_ARMS_PER_JOB]
            target = str(item.get("target") or "")
            exp_id = f"backfill-{_exp_id_slug(target)}"
            jobs.append(
                {
                    "job_kind": "exp_abcd",
                    "target": target,
                    "repo": _target_repo(target),
                    "task_type": task_type,
                    "title": item.get("title") or "",
                    "agents": agents,
                    "covers_cells": [
                        {
                            k: cell[k]
                            for k in (
                                "task_type",
                                "agent",
                                "observations",
                                "target_observations",
                                "needed_observations",
                                "reasons",
                            )
                        }
                        for cell in covered_cells
                        if cell["agent"] in agents
                    ],
                    "counts_when": "only after exp_abcd evaluate records real evaluations, or after normal production outcomes are ingested",
                    "apply_command": (
                        f"{ENV_FLAG}=1 python3 Orchestrator/exploration_backfill.py "
                        f"--apply --confirm-backfill --target {shlex.quote(target)} "
                        f"--agents {shlex.quote(','.join(agents))}"
                    ),
                    "follow_up_commands": [
                        "python3 Orchestrator/exp_abcd.py status <exp_id>",
                        f"python3 Orchestrator/exp_abcd.py collect {_target_repo(target) or '<repo>'} <exp_id>",
                        f"python3 Orchestrator/exp_abcd.py evaluate {_target_repo(target) or '<repo>'} <spec_file> <exp_id>",
                    ],
                    "exp_id_template": exp_id,
                }
            )
        if len(jobs) >= max_jobs:
            break
    return jobs, blocked


def build_plan(
    *,
    backlog_path: Path | None = None,
    capacity_path: Path | None = None,
    backlog_payload: dict | None = None,
    capacity_payload: dict | None = None,
    route_table: dict | None = None,
    max_jobs: int = DEFAULT_MAX_JOBS,
    trials: int = 100,
    require_direct_progress: bool = True,
) -> dict:
    route_table = route_table or router.ROUTE_TABLE
    acquisition = exploration_evidence_plan.build_plan(
        backlog_path=backlog_path,
        capacity_path=capacity_path,
        backlog_payload=backlog_payload,
        capacity_payload=capacity_payload,
        route_table=route_table,
        trials=trials,
    )
    review = exploration_review.build_report(route_table=route_table, trials=trials)
    progress = _direct_mode_progress(review)
    coverage = acquisition.get("route_coverage_deficits") or {}
    cells = _missing_cells(coverage, route_table=route_table)
    items = exploration_evidence_plan._backlog_items(backlog_path, backlog_payload)
    capacity_snapshot = exploration_evidence_plan._capacity_snapshot(
        capacity_path, capacity_payload
    )
    jobs, blocked = _build_jobs(
        cells=cells,
        items=items,
        capacity_snapshot=capacity_snapshot,
        route_table=route_table,
        max_jobs=max(0, int(max_jobs)),
    )
    route_lagging = _route_lagging(acquisition)
    blockers: list[str] = []
    if require_direct_progress and not progress["progressing"]:
        blockers.append("direct exploration mode outcome counts are not progressing yet")
    if not route_lagging:
        blockers.append("route-weight coverage gates are already ready")
    if cells and not jobs:
        blockers.append("no schedulable real A/B subjects for missing route cells")
    if not cells:
        blockers.append("no missing route cells detected")

    active_eligible = (
        route_lagging and bool(jobs) and (progress["progressing"] or not require_direct_progress)
    )
    if active_eligible:
        status = "ready_to_schedule_backfill"
        next_action = (
            "run one guarded backfill job, then collect and evaluate it before counting evidence"
        )
    elif cells and not progress["progressing"] and require_direct_progress:
        status = "waiting_for_direct_mode_progress"
        next_action = (
            "continue Stage 2 supervised collection until direct exploration outcomes start moving"
        )
    elif cells and not jobs:
        status = "needs_backlog_or_research_subjects"
        next_action = "refresh backlog or author a targeted research subject for the missing cells"
    elif not route_lagging:
        status = "route_coverage_ready"
        next_action = "no route-coverage backfill needed"
    else:
        status = "blocked"
        next_action = "inspect blockers before scheduling backfill"

    return {
        "generated_at": int(time.time()),
        "read_only": True,
        "stage": "stage_3_route_coverage_backfill",
        "status": status,
        "next_action": next_action,
        "active_backfill_eligible": active_eligible,
        "requires_opt_in": ENV_FLAG,
        "safety_requirements": [
            "dry-run by default",
            f"active scheduling requires --apply --confirm-backfill and {ENV_FLAG}=1",
            "opener lane only",
            "unclaimed targets only",
            "no backup/paygo-only agents",
            "A/B jobs only create real route evidence after collect/evaluate records evaluations",
        ],
        "direct_mode_progress": progress,
        "route_lagging": route_lagging,
        "missing_cell_count": len(cells),
        "missing_cells": cells,
        "planned_jobs": jobs,
        "blocked_tasks": blocked,
        "blockers": blockers,
        "validation_commands": [
            {
                "command": "python3 Orchestrator/exploration_backfill.py --selftest",
                "purpose": "offline verification of missing-cell detection and guarded active scheduling",
                "expected": "exploration_backfill.py selftest: OK",
            },
            {
                "command": "python3 Orchestrator/exploration_backfill.py --json",
                "purpose": "live read-only Stage 3 backfill plan",
                "expected": "read_only=true and active_backfill_eligible explains current gates",
            },
        ],
    }


def _spec_from_item(item: dict, *, issue_body_fn=None) -> str | None:
    target = str(item.get("target") or "")
    title = (item.get("title") or "").strip()
    body = (item.get("body") or "").strip()
    if not body and issue_body_fn:
        body = (issue_body_fn(target) or "").strip()
    if not title and not body:
        return None
    return (
        "# Exploration route-coverage backfill subject\n\n"
        f"Target: {target}\n"
        f"Task type: {item.get('task_type') or 'implement'}\n\n"
        "This is a frozen A/B research specification for route-coverage evidence. "
        "Implement the requested issue completely in an isolated experiment branch. "
        "Do not push or open a pull request; the result will be collected and cross-evaluated.\n\n"
        f"## Original title\n\n{title or target}\n\n"
        f"## Original body\n\n{body}\n"
    )


def schedule_backfill(
    plan: dict,
    *,
    target: str | None = None,
    agents: list[str] | None = None,
    backlog_path: Path | None = None,
    backlog_payload: dict | None = None,
    confirm: bool = False,
    env: dict | None = None,
    prepare_fn=None,
    issue_body_fn=None,
) -> dict:
    env = os.environ if env is None else env
    if not confirm or env.get(ENV_FLAG) != "1":
        return {
            "error": f"active scheduling requires --confirm-backfill and {ENV_FLAG}=1",
            "read_only": True,
        }
    if not plan.get("active_backfill_eligible"):
        return {
            "blocked": True,
            "reason": "backfill plan is not active-eligible",
            "blockers": plan.get("blockers") or [],
        }
    jobs = plan.get("planned_jobs") or []
    job = next((row for row in jobs if not target or row.get("target") == target), None)
    if not job:
        return {
            "blocked": True,
            "reason": "requested target is not in planned_jobs",
            "target": target,
        }
    if agents:
        requested = list(dict.fromkeys(agents))
        planned = set(job.get("agents") or [])
        if len(requested) < 2 or not set(requested) <= planned:
            return {
                "blocked": True,
                "reason": "requested agents must be at least two agents from the planned backfill set",
                "requested_agents": requested,
                "planned_agents": job.get("agents") or [],
            }
        job = {**job, "agents": requested}
    target = job["target"]
    if not claims.claim(target, BACKFILL_CLAIM_AGENT):
        return {"blocked": True, "reason": "target already claimed", "target": target}
    try:
        repo = job.get("repo") or _target_repo(target)
        if not repo:
            raise ValueError(f"cannot infer repo from target {target}")
        backlog_items = exploration_evidence_plan._backlog_items(
            backlog_path=backlog_path,
            backlog_payload=backlog_payload,
        )
        item = next((row for row in backlog_items if row.get("target") == target), None)
        if item is None:
            item = {
                "target": target,
                "task_type": job.get("task_type"),
                "title": job.get("title") or "",
                "body": "",
            }
        spec = _spec_from_item(item, issue_body_fn=issue_body_fn)
        if not spec:
            claims.release(target, BACKFILL_CLAIM_AGENT)
            return {
                "blocked": True,
                "reason": "missing issue body/title for frozen A/B spec",
                "target": target,
            }
        spec_dir = Path(tempfile.mkdtemp(prefix="orch-exploration-backfill-"))
        spec_file = spec_dir / "spec.md"
        spec_file.write_text(spec, encoding="utf-8")
        exp_id = f"{job.get('exp_id_template')}-{int(time.time())}"
        # prepare_arms, not prepare -- the SECOND launcher that had this bug. `prepare` writes only
        # `meta["agents"]`, so `experiment_members()` falls back to `legacy=True`,
        # `record_evaluation_v2` never fires, and the arm/member/profile identity §2 requires is
        # replaced by an `agent_parent_projection`. All 21 on-disk manifests have
        # `schema_version: None` and no `members[]` for exactly this reason. `tick` was fixed;
        # this path was not, so the legacy shape would have kept being produced.
        prepare = prepare_fn or exp_abcd.prepare_arms
        launched = prepare(
            repo,
            str(spec_file),
            exp_id,
            exp_abcd.research_v2_arms(job["agents"], job.get("profiles")),
            job.get("task_type") or "implement",
        )
        meta_file = exp_abcd.exp_paths(exp_id) / "meta.json"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["backfill_target"] = target
            meta["backfill_task_type"] = job.get("task_type")
            meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {
            "active": True,
            "target": target,
            "repo": repo,
            "task_type": job.get("task_type"),
            "agents": job["agents"],
            "exp_id": exp_id,
            "spec_file": str(spec_file),
            "launched": launched,
            "counts_when": job.get("counts_when"),
            "follow_up_commands": [
                f"python3 Orchestrator/exp_abcd.py status {exp_id}",
                f"python3 Orchestrator/exp_abcd.py collect {repo} {exp_id}",
                f"python3 Orchestrator/exp_abcd.py evaluate {repo} {spec_file} {exp_id}",
            ],
        }
    except Exception as exc:
        claims.release(target, BACKFILL_CLAIM_AGENT)
        return {
            "blocked": True,
            "reason": "failed to launch backfill experiment",
            "error": str(exc),
            "target": target,
        }


def format_human(plan: dict) -> str:
    lines = [
        "exploration_backfill: "
        f"status={plan['status']} eligible={plan['active_backfill_eligible']} "
        f"missing_cells={plan['missing_cell_count']} planned_jobs={len(plan.get('planned_jobs') or [])}",
        f"next: {plan['next_action']}",
        "direct progress: "
        f"outcome_exploration_runs={plan['direct_mode_progress']['outcome_exploration_runs']} "
        f"progressing={plan['direct_mode_progress']['progressing']}",
    ]
    if plan.get("blockers"):
        lines.append("blockers:")
        for blocker in plan["blockers"]:
            lines.append(f"  - {blocker}")
    if plan.get("planned_jobs"):
        lines.append("planned jobs:")
        for job in plan["planned_jobs"][:5]:
            lines.append(
                f"  {job['target']}: {job['task_type']} agents={','.join(job['agents'])} "
                f"cells={len(job.get('covers_cells') or [])}"
            )
    elif plan.get("blocked_tasks"):
        lines.append("blocked task types:")
        for row in plan["blocked_tasks"][:5]:
            lines.append(f"  {row.get('task_type')}: {row.get('reason')}")
    lines.append(f"active scheduling requires --apply --confirm-backfill and {ENV_FLAG}=1")
    return "\n".join(lines)


def _insert_weight(c, version: int, task_type: str, agent: str, n_obs: int) -> None:
    now = int(time.time())
    posterior = 0.65
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
            "exploration_backfill selftest",
            now - 86400,
            now,
        ),
    )


def _selftest() -> None:
    old_db = feedback.DB_PATH
    old_handoff = os.environ.get("HANDOFF_DIR")
    tmp = tempfile.mkdtemp(prefix="exploration-backfill-")
    os.environ["HANDOFF_DIR"] = str(Path(tmp) / "handoff")
    feedback.DB_PATH = Path(tmp) / "feedback.db"
    try:
        route_table = {
            "implement": {
                "role": "code",
                "agents": [
                    {"agent": "codex", "mode": "full", "late": False},
                    {"agent": "cursor", "mode": "composer", "late": False},
                    {"agent": "vibe", "mode": "full", "late": False},
                ],
            }
        }
        with feedback._conn() as c:
            _insert_weight(c, 1, "implement", "codex", 2)
            _insert_weight(c, 1, "implement", "cursor", 0)
            _insert_weight(c, 1, "implement", "vibe", 0)
        feedback.record_run(
            "explore-progress",
            "o/r#progress",
            "implement",
            "codex",
            routing_metadata={
                "source": "router_assignment",
                "exploration": True,
                "exploration_mode": "epsilon-greedy",
            },
        )
        feedback.record_outcome(
            "explore-progress",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        backlog_payload = {
            "items": [
                {
                    "target": "o/r#1",
                    "task_type": "implement",
                    "lane": "opener",
                    "title": "Implement fixture",
                    "body": "Do the fixture work.",
                }
            ]
        }
        capacity_payload = {
            "agents": {
                "codex": {"state": "ok"},
                "cursor": {"state": "ok"},
                "vibe": {"state": "ok"},
            }
        }
        plan = build_plan(
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            route_table=route_table,
            max_jobs=2,
            trials=5,
        )
        assert plan["active_backfill_eligible"] is True, plan
        assert plan["missing_cell_count"] >= 2, plan["missing_cells"]
        assert plan["planned_jobs"] and plan["planned_jobs"][0]["target"] == "o/r#1", plan[
            "planned_jobs"
        ]
        planned_job = plan["planned_jobs"][0]
        assert {"cursor", "vibe"} <= set(planned_job["agents"]), planned_job
        assert planned_job["exp_id_template"] == "backfill-o-r-1", planned_job
        assert "ORCH_EXPLORATION_BACKFILL=1" in format_human(plan), format_human(plan)

        calls: list[dict] = []

        def fake_prepare(
            repo: str, spec_file: str, exp_id: str, arms: list[dict], task_type: str = "implement"
        ) -> dict:
            # ASSERT THE SHAPE, not just that something was called. This double took a plain agent
            # list and kept passing after the launcher switched to v2 arms -- a fake that accepts
            # anything cannot tell `prepare` from `prepare_arms`, which is precisely the confusion
            # that left `evaluations_v2` empty for 2,556 evaluations.
            assert isinstance(arms, list) and arms, arms
            for arm in arms:
                assert isinstance(arm, dict), f"legacy agent list reached prepare_arms: {arms!r}"
                assert arm.get("arm_id") and arm.get("agents"), arm
            calls.append(
                {
                    "repo": repo,
                    "spec_file": spec_file,
                    "exp_id": exp_id,
                    "agents": [a for arm in arms for a in arm["agents"]],
                    "arms": arms,
                    "task_type": task_type,
                }
            )
            assert "Do the fixture work." in Path(spec_file).read_text(), spec_file
            return {
                "repo": repo,
                "exp_id": exp_id,
                "agents": [a for arm in arms for a in arm["agents"]],
            }

        original_backlog_items = exploration_evidence_plan._backlog_items
        exploration_evidence_plan._backlog_items = lambda *args, **kwargs: (
            backlog_payload["items"]
        )  # type: ignore
        try:
            launched = schedule_backfill(
                plan,
                target="o/r#1",
                confirm=True,
                env={ENV_FLAG: "1"},
                prepare_fn=fake_prepare,
            )
        finally:
            exploration_evidence_plan._backlog_items = original_backlog_items  # type: ignore
        assert launched["active"] is True and calls, launched
        assert calls[0]["task_type"] == "implement", calls
        assert calls[0]["exp_id"].startswith(f"{planned_job['exp_id_template']}-"), calls
        assert claims.holder("o/r#1")["agent"] == BACKFILL_CLAIM_AGENT, claims.holder("o/r#1")

        no_progress_db = Path(tmp) / "no-progress.db"
        feedback.DB_PATH = no_progress_db
        with feedback._conn() as c:
            _insert_weight(c, 1, "implement", "codex", 2)
            _insert_weight(c, 1, "implement", "cursor", 0)
            _insert_weight(c, 1, "implement", "vibe", 0)
        blocked = build_plan(
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            route_table=route_table,
            trials=5,
        )
        assert blocked["active_backfill_eligible"] is False, blocked
        assert "direct exploration mode outcome counts" in " ".join(blocked["blockers"]), blocked[
            "blockers"
        ]
        print("exploration_backfill.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        if old_handoff is None:
            os.environ.pop("HANDOFF_DIR", None)
        else:
            os.environ["HANDOFF_DIR"] = old_handoff
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only route-coverage backfill planner.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--backlog-json", type=Path)
    parser.add_argument("--capacity-json", type=Path)
    parser.add_argument("--max-jobs", type=int, default=DEFAULT_MAX_JOBS)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--allow-before-direct-progress", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-backfill", action="store_true")
    parser.add_argument("--target")
    parser.add_argument("--agents", help="comma-separated planned agents to launch")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    plan = build_plan(
        backlog_path=args.backlog_json,
        capacity_path=args.capacity_json,
        max_jobs=max(0, args.max_jobs),
        trials=max(1, args.trials),
        require_direct_progress=not args.allow_before_direct_progress,
    )
    if args.apply:
        requested_agents = [a.strip() for a in (args.agents or "").split(",") if a.strip()] or None
        result = schedule_backfill(
            plan,
            target=args.target,
            agents=requested_agents,
            backlog_path=args.backlog_json,
            confirm=args.confirm_backfill,
        )
        print(json.dumps(result, indent=2, default=str) if args.as_json else format_human(plan))
        return 0 if result.get("active") else 2
    print(json.dumps(plan, indent=2, default=str) if args.as_json else format_human(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
