#!/usr/bin/env python3
"""periodic_report.py - read-only review of the Orchestrator feedback dataset.

`relearn_report.py` runs the learner and writes a new route_weights version. This
module does not relearn by default. It is the operator-facing window into the
current dataset: route beliefs, recent outcomes, cost/trace evidence, evidence
growth proposals, and hypothesis status.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import dry_seam_audit
import capabilities
import evidence_schema
import exploration_backfill
import exploration_evidence_plan
import exploration_review
import execution_profiles
import feedback
import features
import human_calibration
import judge_reliability
import keepalive_outcomes
import keepalive_shadow
import langsmith_fetch
import model_profile_trial
import model_profile_trial_bridge
import redirect_shadow
import research_subjects
import router
import runtime_ac_flow_monitor
from relearn_report import _task_report, priors_from_route_table

ORCH = Path(__file__).resolve().parent
HYPOTHESES_JSON = ORCH / "experiments" / "hypotheses.json"
DEFAULT_STAGE2_PLAN_JSON = Path(
    os.environ.get(
        "ORCH_KEEPALIVE_STAGE2_PLAN_JSON",
        Path.home()
        / ".codex"
        / "orchestrator"
        / "keepalive-supervisor-stage2-plan.json",
    )
)
TABLES = [
    "runs",
    "outcomes",
    "costs",
    "execution_traces",
    "execution_attempts",
    "completion_events",
    "influence_edges",
    "route_weights",
    "evaluations",
    "evaluations_v2",
    "human_calibration",
    "evidence_gaps",
    "evidence_types",
]
PROCESS_WORK_TYPES = {"renovate", "sync", "tooling", "docs"}
PROCESS_FAILURE_DURABILITIES = {
    "abandoned",
    "reverted",
    "reworked",
    "reopened",
    "broke_later",
}
ISSUE_REVIEW_RE = re.compile(r"\bissue_review=([a-z0-9_:-]+)\b", re.IGNORECASE)
KEEPALIVE_SUPERVISOR_MIN_FAILURE_OUTCOMES = 5
KEEPALIVE_SUPERVISOR_MIN_MEANINGFUL_DISAGREEMENTS = 3
_LANGSMITH_ARTIFACT_HEALTH_UNSET = object()


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _since(window_days: int) -> int:
    return int(time.time()) - window_days * 86400


def _table_counts() -> dict[str, int]:
    with feedback._conn() as c:
        return {
            table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def _max_route_version() -> int:
    with feedback._conn() as c:
        return c.execute(
            "SELECT COALESCE(MAX(version),0) FROM route_weights"
        ).fetchone()[0]


def _experiment_identity_summary(window_days: int) -> dict:
    """Report exact arm/member/evaluator coverage without counting legacy dual writes twice."""
    since = _since(window_days)
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT experiment_id, implementer_arm_id, implementer_member_id, "
            "implementer_profile_id, implementation_agent, evaluator_id, "
            "evaluator_arm_id, evaluator_profile_id, evaluator_agent, score "
            "FROM evaluations_v2 WHERE ts>=?",
            (since,),
        ).fetchall()
        run_rows = c.execute(
            "SELECT experiment_id, agent, routing_metadata FROM runs "
            "WHERE ts>=? AND experiment_id IS NOT NULL AND experiment_id!=''",
            (since,),
        ).fetchall()
    expected: dict[tuple[str, str], dict[str, set[str]]] = {}
    for experiment_id, agent, raw_metadata in run_rows:
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        arm_id = metadata.get("experiment_arm_id")
        member_id = metadata.get("experiment_member_id")
        if arm_id and member_id:
            expected_arm = expected.setdefault(
                (str(experiment_id), str(arm_id)),
                {"members": set(), "agents": set(), "profiles": set()},
            )
            expected_arm["members"].add(str(member_id))
            expected_arm["agents"].add(str(agent))
            if metadata.get("profile_id"):
                expected_arm["profiles"].add(str(metadata["profile_id"]))
    arm_rows: dict[tuple[str, str], dict] = {}
    evaluators: dict[str, dict] = {}
    for (
        experiment_id,
        arm_id,
        member_id,
        profile_id,
        implementation_agent,
        evaluator_id,
        evaluator_arm_id,
        evaluator_profile_id,
        evaluator_agent,
        score,
    ) in rows:
        key = (str(experiment_id), str(arm_id))
        arm = arm_rows.setdefault(
            key,
            {"experiment_id": key[0], "arm_id": key[1], "members": set(), "agents": set(), "profiles": set(), "scores": []},
        )
        arm["members"].add(str(member_id))
        arm["agents"].add(str(implementation_agent))
        if profile_id:
            arm["profiles"].add(str(profile_id))
        arm["scores"].append(float(score))
        evaluators.setdefault(
            str(evaluator_id),
            {
                "evaluator_id": str(evaluator_id),
                "agent": str(evaluator_agent),
                "arm_id": evaluator_arm_id,
                "profile_id": evaluator_profile_id,
                "observations": 0,
            },
        )["observations"] += 1
    arms = []
    for key, row in sorted(arm_rows.items()):
        scores = row.pop("scores")
        expected_members = (expected.get(key) or {}).get("members") or set()
        observed_members = set(row["members"])
        complete = not expected_members or expected_members.issubset(observed_members)
        member_mean = round(sum(scores) / len(scores), 4)
        arms.append(
            {
                **row,
                "members": sorted(row["members"]),
                "agents": sorted(row["agents"]),
                "profiles": sorted(row["profiles"]),
                "evaluation_observations": len(scores),
                "mean_member_evaluation_score": member_mean,
                "arm_outcome_complete": complete,
                "mean_score": member_mean if complete else None,
                "missing_members": sorted(expected_members - observed_members),
            }
        )
    shared_agents: dict[str, set[str]] = {}
    for (experiment_id, arm_id), expected_arm in expected.items():
        for agent in expected_arm["agents"]:
            shared_agents.setdefault(agent, set()).add(f"{experiment_id}:{arm_id}")
    missing = []
    for (experiment_id, arm_id), expected_arm in sorted(expected.items()):
        observed_members = set(
            (arm_rows.get((experiment_id, arm_id)) or {}).get("members") or set()
        )
        expected_members = set(expected_arm["members"])
        if not observed_members:
            missing.append(
                {
                    "experiment_id": experiment_id,
                    "arm_id": arm_id,
                    "members": sorted(expected_members),
                    "reason": "no_exact_arm_evaluation",
                }
            )
        elif not expected_members.issubset(observed_members):
            missing.append(
                {
                    "experiment_id": experiment_id,
                    "arm_id": arm_id,
                    "members": sorted(expected_members),
                    "missing_members": sorted(expected_members - observed_members),
                    "reason": "incomplete_exact_member_evaluation",
                }
            )
    return {
        "identity_version": 2,
        "implementation_arms": arms,
        "evaluator_identities": sorted(evaluators.values(), key=lambda row: row["evaluator_id"]),
        "shared_agents_across_arms": {
            agent: sorted(arm_ids) for agent, arm_ids in shared_agents.items() if len(arm_ids) > 1
        },
        "missing_arm_outcomes": missing,
    }


def _version_exists(version: int | None) -> bool:
    if version is None or version < 1:
        return False
    with feedback._conn() as c:
        return (
            c.execute(
                "SELECT 1 FROM route_weights WHERE version=? LIMIT 1", (version,)
            ).fetchone()
            is not None
        )


def _load_snapshot_into_temp_db(snapshot_path: Path) -> Path:
    """Load a feedback.snapshot_json() export into an isolated temp SQLite DB."""
    data = json.loads(snapshot_path.read_text())
    fd, db_name = tempfile.mkstemp(prefix="orch-periodic-report-", suffix=".db")
    os.close(fd)
    temp_db = Path(db_name)
    with sqlite3.connect(str(temp_db)) as c:
        c.executescript(feedback.SCHEMA)
        # Migration-added columns (failure_class, influenced_by_run_id, ...) appear in snapshot
        # rows; the temp DB must carry the same migrations or restores break on new columns.
        feedback._migrate_schema(c)
        for table in TABLES:
            rows = data.get(table) or []
            if not rows:
                continue
            columns = list(rows[0])
            col_sql = ",".join(columns)
            placeholders = ",".join("?" for _ in columns)
            for row in rows:
                c.execute(
                    f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
                    [row.get(col) for col in columns],
                )
    return temp_db


def build_report_from_snapshot(
    snapshot_path: Path,
    window_days: int = 90,
    min_gap_recurrence: int = 3,
    *,
    route_table=None,
    hypotheses_path: Path | None = None,
    features_path: Path | None = None,
    capabilities_path: Path | None = None,
    keepalive_corpus_path: Path | None = None,
    redirect_corpus_path: Path | None = None,
    model_profile_trial_path: Path | None = None,
    model_profile_qualification_path: Path | None = None,
    langsmith_artifact_health=_LANGSMITH_ARTIFACT_HEALTH_UNSET,
    probe_langsmith_artifacts: bool = False,
) -> dict:
    """Build a report from a snapshot export while scoping feedback.DB_PATH to this call."""
    original_db = feedback.DB_PATH
    temp_db = _load_snapshot_into_temp_db(snapshot_path)
    try:
        feedback.DB_PATH = temp_db
        report = build_report(
            window_days=window_days,
            min_gap_recurrence=min_gap_recurrence,
            route_table=route_table,
            hypotheses_path=hypotheses_path,
            features_path=features_path,
            capabilities_path=capabilities_path,
            keepalive_corpus_path=keepalive_corpus_path,
            redirect_corpus_path=redirect_corpus_path,
            model_profile_trial_path=model_profile_trial_path,
            model_profile_qualification_path=model_profile_qualification_path,
            langsmith_artifact_health=langsmith_artifact_health,
            probe_langsmith_artifacts=probe_langsmith_artifacts,
        )
        report["source"] = "snapshot"
        report["snapshot_path"] = str(snapshot_path)
        return report
    finally:
        feedback.DB_PATH = original_db
        temp_db.unlink(missing_ok=True)


def _route_weight_summary(route_table=None) -> dict:
    task_type_priors = priors_from_route_table(route_table or router.ROUTE_TABLE)
    latest_version = _max_route_version()
    previous_version = (
        latest_version - 1 if _version_exists(latest_version - 1) else None
    )
    tasks = []
    for task_type, priors in task_type_priors.items():
        learned = (
            feedback.current_weights(task_type, latest_version)
            if latest_version
            else []
        )
        if learned:
            tasks.append(
                _task_report(task_type, priors, latest_version, previous_version)
            )
        else:
            tasks.append(
                {
                    "task_type": task_type,
                    "prior_order": list(priors),
                    "learned_order": [],
                    "previous_order": None,
                    "diverges_from_prior": False,
                    "cold_start": True,
                    "note": "no route_weights for this task_type yet",
                    "rows": [],
                }
            )
    return {
        "latest_version": latest_version or None,
        "previous_version": previous_version,
        "tasks": tasks,
    }


def _outcome_summary(window_days: int) -> dict:
    since = _since(window_days)
    failure_durabilities = {
        "abandoned",
        "reverted",
        "reworked",
        "reopened",
        "broke_later",
    }
    with feedback._conn() as c:
        runs_total = c.execute(
            "SELECT COUNT(*) FROM runs WHERE ts>=?", (since,)
        ).fetchone()[0]
        total = c.execute(
            "SELECT COUNT(*) FROM runs r JOIN outcomes o ON r.run_id=o.run_id WHERE r.ts>=?",
            (since,),
        ).fetchone()[0]
        rollup = c.execute(
            "SELECT "
            "COUNT(o.run_id), "
            "COALESCE(SUM(CASE WHEN UPPER(COALESCE(o.adjudicated_verdict,''))='PASS' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.adjudicated_verdict,'')!='' "
            "AND UPPER(COALESCE(o.adjudicated_verdict,''))!='PASS' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN o.merged=1 THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.durability,'')='durable' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN UPPER(COALESCE(o.adjudicated_verdict,''))='PASS' "
            "AND COALESCE(o.durability,'')='durable' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.durability,'')='pending' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.durability,'') IN "
            "('abandoned','reverted','reworked','reopened','broke_later') THEN 1 ELSE 0 END),0) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id WHERE r.ts>=?",
            (since,),
        ).fetchone()
        rows = c.execute(
            "SELECT r.task_type, r.agent, COALESCE(o.adjudicated_verdict,''), "
            "COALESCE(o.durability,''), COUNT(*) "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.ts>=? "
            "GROUP BY r.task_type, r.agent, o.adjudicated_verdict, o.durability "
            "ORDER BY r.task_type, r.agent, o.adjudicated_verdict, o.durability",
            (since,),
        ).fetchall()
        by_source = c.execute(
            "SELECT COALESCE(r.source,''), COALESCE(r.assignment,''), COUNT(*), "
            "COALESCE(SUM(CASE WHEN o.run_id IS NOT NULL THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN o.merged=1 THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN UPPER(COALESCE(o.adjudicated_verdict,''))='PASS' "
            "AND COALESCE(o.durability,'')='durable' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.durability,'') IN "
            "('abandoned','reverted','reworked','reopened','broke_later') THEN 1 ELSE 0 END),0) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.ts>=? "
            "GROUP BY r.source, r.assignment ORDER BY r.source, r.assignment",
            (since,),
        ).fetchall()
    (
        outcome_rows,
        pass_count,
        fail_count,
        merged_count,
        durable_count,
        durable_success,
        pending,
        failures,
    ) = rollup
    outcome_coverage = (outcome_rows / runs_total) if runs_total else None
    merged_rate = (merged_count / outcome_rows) if outcome_rows else None
    durable_success_rate = (durable_success / outcome_rows) if outcome_rows else None
    durability_failure_rate = (failures / outcome_rows) if outcome_rows else None
    return {
        "window_days": window_days,
        "total": total,
        "rollup": {
            "runs_total": runs_total,
            "outcome_rows": outcome_rows,
            "outcome_coverage": outcome_coverage,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "merged_count": merged_count,
            "merged_rate": merged_rate,
            "durable_count": durable_count,
            "durable_success_count": durable_success,
            "durable_success_rate": durable_success_rate,
            "pending_durability_count": pending,
            "durability_failure_count": failures,
            "durability_failure_rate": durability_failure_rate,
            "failure_durabilities": sorted(failure_durabilities),
        },
        "by_source_assignment": [
            {
                "source": source or None,
                "assignment": assignment or None,
                "runs": runs,
                "outcomes": outcomes,
                "merged": merged,
                "durable_success": durable,
                "durability_failures": failures,
                "outcome_coverage": (outcomes / runs) if runs else None,
                "durable_success_rate": (durable / outcomes) if outcomes else None,
            }
            for source, assignment, runs, outcomes, merged, durable, failures in by_source
        ],
        "by_task_agent_verdict": [
            {
                "task_type": task_type,
                "agent": agent,
                "adjudicated_verdict": verdict or None,
                "durability": durability or None,
                "count": count,
            }
            for task_type, agent, verdict, durability, count in rows
        ],
    }


def _production_flow_summary(window_days: int) -> dict:
    """Summarize whether real production work is flowing, excluding advisory/eval/offload rows."""
    now = int(time.time())
    since_window = now - window_days * 86400
    since_7d = now - 7 * 86400
    production_filter = (
        "COALESCE(r.mode,'') NOT IN ('offload','role') "
        "AND COALESCE(r.task_type,'') NOT IN "
        "('offload','review','ux_review','synthesize') "
        "AND COALESCE(r.task_type,'') NOT LIKE 'role:%' "
        "AND COALESCE(r.experiment_id,'')='' "
        "AND COALESCE(r.run_id,'') NOT LIKE '%:eval:%' "
        "AND COALESCE(r.target,'') NOT LIKE '%[exp %'"
    )
    with feedback._conn() as c:
        window_row = c.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN o.run_id IS NOT NULL THEN 1 ELSE 0 END),0), "
            "MAX(r.ts), MAX(CASE WHEN o.run_id IS NOT NULL THEN r.ts ELSE NULL END) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            f"WHERE r.ts>=? AND {production_filter}",
            (since_window,),
        ).fetchone()
        recent_row = c.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN o.run_id IS NOT NULL THEN 1 ELSE 0 END),0) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            f"WHERE r.ts>=? AND {production_filter}",
            (since_7d,),
        ).fetchone()
        by_source = c.execute(
            "SELECT COALESCE(r.source,''), COALESCE(r.assignment,''), COUNT(*), "
            "COALESCE(SUM(CASE WHEN o.run_id IS NOT NULL THEN 1 ELSE 0 END),0), "
            "MAX(r.ts) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            f"WHERE r.ts>=? AND {production_filter} "
            "GROUP BY r.source, r.assignment ORDER BY COUNT(*) DESC, r.source, r.assignment",
            (since_window,),
        ).fetchall()
    window_runs, window_outcomes, latest_run_ts, latest_outcome_run_ts = window_row
    recent_runs, recent_outcomes = recent_row
    latest_age_days = (
        max(0, (now - int(latest_run_ts)) // 86400) if latest_run_ts else None
    )
    latest_outcome_age_days = (
        max(0, (now - int(latest_outcome_run_ts)) // 86400)
        if latest_outcome_run_ts
        else None
    )
    status = "flowing" if recent_runs else "stale" if window_runs else "dry"
    recommendation = (
        "Production runs are flowing in the last 7 days."
        if status == "flowing"
        else "Exercise the loop on a safe real backlog item or inspect why production cadence stopped."
    )
    return {
        "window_days": window_days,
        "recent_days": 7,
        "status": status,
        "production_runs": window_runs,
        "production_outcomes": window_outcomes,
        "recent_production_runs": recent_runs,
        "recent_production_outcomes": recent_outcomes,
        "outcome_coverage": (window_outcomes / window_runs) if window_runs else None,
        "latest_run_ts": latest_run_ts,
        "latest_run_age_days": latest_age_days,
        "latest_outcome_run_ts": latest_outcome_run_ts,
        "latest_outcome_age_days": latest_outcome_age_days,
        "by_source_assignment": [
            {
                "source": source or None,
                "assignment": assignment or None,
                "runs": runs,
                "outcomes": outcomes,
                "latest_run_age_days": (
                    max(0, (now - int(last_ts)) // 86400) if last_ts else None
                ),
            }
            for source, assignment, runs, outcomes, last_ts in by_source
        ],
        "recommendation": recommendation,
    }


def _process_action(work_type: str) -> str:
    if work_type == "renovate":
        return "review Renovate grouping, pinning, and schedule rules before more dependency churn lands"
    if work_type == "sync":
        return "inspect sync manifest/template drift and tighten the source-to-consumer rollout checklist"
    if work_type == "tooling":
        return "audit the CI/tooling guardrail that produced failed maintenance PRs"
    if work_type == "docs":
        return "check doc freshness triggers and managed-block export rules for the affected repo"
    return "review the recurring maintenance loop before dispatching similar work"


def _process_rollup_row(row: tuple) -> dict:
    work_type, runs, outcomes, merged, durable, pending, failures = row
    return {
        "work_type": work_type or "issue",
        "runs": runs,
        "outcomes": outcomes,
        "merged": merged,
        "durable_success": durable,
        "pending": pending,
        "durability_failures": failures,
        "outcome_coverage": (outcomes / runs) if runs else None,
        "durable_success_rate": (durable / outcomes) if outcomes else None,
        "durability_failure_rate": (failures / outcomes) if outcomes else None,
    }


def issue_review_reason(text: str | None) -> str | None:
    match = ISSUE_REVIEW_RE.search(text or "")
    return match.group(1).lower() if match else None


def _process_improvement_summary(window_days: int) -> dict:
    since = _since(window_days)
    failure_sql = ",".join("?" for _ in PROCESS_FAILURE_DURABILITIES)
    process_sql = ",".join("?" for _ in PROCESS_WORK_TYPES)
    work_type_expr = "COALESCE(NULLIF(r.work_type,''),'issue')"
    unsuppressed_failure_expr = (
        f"COALESCE(o.durability,'') IN ({failure_sql}) "
        "AND LOWER(COALESCE(o.notes,'')) NOT LIKE '%process_ignore=%'"
    )
    with feedback._conn() as c:
        work_type_rows = c.execute(
            f"SELECT {work_type_expr} AS work_type, COUNT(*), "
            "COALESCE(SUM(CASE WHEN o.run_id IS NOT NULL THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN o.merged=1 THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN UPPER(COALESCE(o.adjudicated_verdict,''))='PASS' "
            "AND COALESCE(o.durability,'')='durable' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.durability,'')='pending' THEN 1 ELSE 0 END),0), "
            f"COALESCE(SUM(CASE WHEN {unsuppressed_failure_expr} THEN 1 ELSE 0 END),0) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.ts>=? "
            "GROUP BY work_type ORDER BY work_type",
            (*sorted(PROCESS_FAILURE_DURABILITIES), since),
        ).fetchall()
        non_agent_rows = c.execute(
            f"SELECT {work_type_expr} AS work_type, COUNT(*), "
            "COALESCE(SUM(CASE WHEN o.run_id IS NOT NULL THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN o.merged=1 THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN UPPER(COALESCE(o.adjudicated_verdict,''))='PASS' "
            "AND COALESCE(o.durability,'')='durable' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN COALESCE(o.durability,'')='pending' THEN 1 ELSE 0 END),0), "
            f"COALESCE(SUM(CASE WHEN {unsuppressed_failure_expr} THEN 1 ELSE 0 END),0) "
            "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.ts>=? AND COALESCE(r.assignment,'experimental')='none' "
            "GROUP BY work_type ORDER BY work_type",
            (*sorted(PROCESS_FAILURE_DURABILITIES), since),
        ).fetchall()
        failed_process = c.execute(
            f"SELECT r.run_id, r.target, r.pr_number, r.agent, COALESCE(r.assignment,''), "
            f"{work_type_expr} AS work_type, COALESCE(o.durability,''), "
            "COALESCE(o.adjudicated_verdict,''), COALESCE(o.notes,''), r.ts "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            f"WHERE r.ts>=? AND {work_type_expr} IN ({process_sql}) "
            f"AND COALESCE(o.durability,'') IN ({failure_sql}) "
            "ORDER BY r.ts DESC LIMIT 50",
            (since, *sorted(PROCESS_WORK_TYPES), *sorted(PROCESS_FAILURE_DURABILITIES)),
        ).fetchall()
        issue_failures = c.execute(
            f"SELECT r.run_id, r.target, r.pr_number, r.agent, COALESCE(r.source,''), "
            "COALESCE(r.assignment,''), COALESCE(o.durability,''), "
            "COALESCE(o.adjudicated_verdict,''), COALESCE(o.notes,''), r.ts "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            f"WHERE r.ts>=? AND {work_type_expr}='issue' "
            f"AND COALESCE(o.durability,'') IN ({failure_sql}) "
            "AND LOWER(COALESCE(o.notes,'')) NOT LIKE '%issue_review=%' "
            "ORDER BY r.ts DESC LIMIT 12",
            (since, *sorted(PROCESS_FAILURE_DURABILITIES)),
        ).fetchall()
        reviewed_issue_failures = c.execute(
            f"SELECT r.run_id, r.target, r.pr_number, r.agent, COALESCE(r.source,''), "
            "COALESCE(r.assignment,''), COALESCE(o.durability,''), "
            "COALESCE(o.adjudicated_verdict,''), COALESCE(o.notes,''), r.ts "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            f"WHERE r.ts>=? AND {work_type_expr}='issue' "
            f"AND COALESCE(o.durability,'') IN ({failure_sql}) "
            "AND LOWER(COALESCE(o.notes,'')) LIKE '%issue_review=%' "
            "ORDER BY r.ts DESC LIMIT 50",
            (since, *sorted(PROCESS_FAILURE_DURABILITIES)),
        ).fetchall()

    signals_by_type: dict[str, dict] = {}
    suppressed_process_failures = []
    for (
        run_id,
        target,
        pr_number,
        agent,
        assignment,
        work_type,
        durability,
        verdict,
        notes,
        ts,
    ) in failed_process:
        suppression_reason = keepalive_outcomes.process_suppression_reason(notes)
        if suppression_reason:
            suppressed_process_failures.append(
                {
                    "run_id": run_id,
                    "target": target,
                    "pr": pr_number,
                    "agent": agent or "unknown",
                    "assignment": assignment or None,
                    "work_type": work_type,
                    "durability": durability,
                    "reason": suppression_reason,
                    "notes": notes[:160] if notes else None,
                    "ts": ts,
                }
            )
            continue
        signal = signals_by_type.setdefault(
            work_type,
            {
                "work_type": work_type,
                "failure_count": 0,
                "reverted_count": 0,
                "abandoned_count": 0,
                "other_failure_count": 0,
                "severity": "MED",
                "recommendation": _process_action(work_type),
                "examples": [],
            },
        )
        signal["failure_count"] += 1
        if durability == "reverted":
            signal["reverted_count"] += 1
            signal["severity"] = "HIGH"
        elif durability == "abandoned":
            signal["abandoned_count"] += 1
        else:
            signal["other_failure_count"] += 1
        if len(signal["examples"]) < 5:
            signal["examples"].append(
                {
                    "run_id": run_id,
                    "target": target,
                    "pr": pr_number,
                    "agent": agent or "unknown",
                    "assignment": assignment or None,
                    "durability": durability,
                    "adjudicated_verdict": verdict or None,
                    "notes": notes[:160] if notes else None,
                    "ts": ts,
                }
            )

    signals = sorted(
        signals_by_type.values(),
        key=lambda row: (
            0 if row["severity"] == "HIGH" else 1,
            -row["failure_count"],
            row["work_type"],
        ),
    )
    return {
        "window_days": window_days,
        "work_type_rollup": [_process_rollup_row(row) for row in work_type_rows],
        "non_agent_by_work_type": [_process_rollup_row(row) for row in non_agent_rows],
        "signals": signals,
        "suppressed_process_failures": suppressed_process_failures,
        "non_durable_issue_runs": [
            {
                "run_id": run_id,
                "target": target,
                "pr": pr_number,
                "agent": agent or "unknown",
                "source": source or None,
                "assignment": assignment or None,
                "durability": durability,
                "adjudicated_verdict": verdict or None,
                "notes": notes[:160] if notes else None,
                "ts": ts,
            }
            for run_id, target, pr_number, agent, source, assignment, durability, verdict, notes, ts in issue_failures
        ],
        "reviewed_issue_failures": [
            {
                "run_id": run_id,
                "target": target,
                "pr": pr_number,
                "agent": agent or "unknown",
                "source": source or None,
                "assignment": assignment or None,
                "durability": durability,
                "adjudicated_verdict": verdict or None,
                "reason": issue_review_reason(notes),
                "notes": notes[:160] if notes else None,
                "ts": ts,
            }
            for run_id, target, pr_number, agent, source, assignment, durability, verdict, notes, ts in reviewed_issue_failures
        ],
    }


def _redirect_stage2_summary(
    corpus_path: Path | None = None,
    keepalive_corpus_path: Path | None = None,
) -> dict:
    path = corpus_path or redirect_shadow.CORPUS_PATH
    summary = redirect_shadow.summarize(path)
    historical_preview = redirect_shadow.collect_historical_from_keepalive(
        keepalive_corpus_path=keepalive_corpus_path,
        limit=1,
        include_calibration=False,
        corpus_path=path,
    )
    calibration_preview = redirect_shadow.collect_historical_from_keepalive(
        keepalive_corpus_path=keepalive_corpus_path,
        limit=1,
        include_calibration=True,
        corpus_path=path,
    )
    historical_remaining = int(historical_preview.get("would_collect") or 0)
    calibration_remaining = int(calibration_preview.get("would_collect") or 0)
    if not path.exists() or int(summary.get("n") or 0) == 0:
        status = "no_proposal_corpus"
        recommendation = "wait for eligible post-escalation candidates, then execute stage2_record_command"
    elif not summary.get("ready_for_analysis"):
        status = "collecting_proposals"
        recommendation = "continue recording valid RedirectAgent proposals for eligible Stage 1 candidates"
    elif not summary.get("ready_for_historical_replay_analysis"):
        if historical_remaining:
            status = "collect_historical_replay"
            recommendation = "run bounded historical replay collection before considering any apply path"
        elif calibration_remaining:
            status = "collect_calibration_replay"
            recommendation = (
                "strict historical replay is exhausted; run bounded calibration replay while "
                "disagreement evidence remains thin"
            )
        else:
            status = "waiting_for_candidates"
            recommendation = (
                "historical replay still needs linked disagreement evidence, but no unreplayed strict "
                "or calibration candidates remain; wait for future live escalations or expand the "
                "historical source"
            )
    elif not summary.get("ready_for_supervised_apply"):
        status = "linking_outcomes"
        recommendation = "link accepted/applied proposal advice to downstream outcomes before any apply path"
    else:
        status = "ready_for_supervised_apply_review"
        recommendation = "review Stage 3 supervised-apply design; live supervisor remains disabled until implemented"
    return {
        "corpus_path": str(path),
        "status": status,
        "ready_for_supervised_apply": bool(summary.get("ready_for_supervised_apply")),
        "ready_for_historical_replay_analysis": bool(
            summary.get("ready_for_historical_replay_analysis")
        ),
        "historical_candidates_remaining": historical_remaining,
        "calibration_candidates_remaining": calibration_remaining,
        "recommendation": recommendation,
        "historical_preview": {
            "candidate_count": historical_preview.get("candidate_count", 0),
            "would_collect": historical_remaining,
        },
        "calibration_preview": {
            "candidate_count": calibration_preview.get("candidate_count", 0),
            "would_collect": calibration_remaining,
        },
        "live_plan": _stage2_live_plan_summary(DEFAULT_STAGE2_PLAN_JSON),
        "summary": summary,
    }


def _stage2_live_plan_summary(path: Path) -> dict:
    """Read the cadence-written Stage 2 plan without querying GitHub."""
    base = {
        "path": str(path),
        "exists": path.exists(),
        "status": "missing",
        "age_s": None,
        "generated_at": None,
        "live_candidate_count": 0,
        "eligible_live_candidate_count": 0,
        "unrecorded_live_candidate_count": 0,
        "commands": [],
        "live_targets": [],
        "recommendation": "run keepalive_supervisor.py --stage2-plan to refresh live candidate discovery",
    }
    if not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            **base,
            "exists": True,
            "status": "unreadable",
            "error": str(exc)[:240],
            "age_s": max(0, int(time.time() - path.stat().st_mtime)),
        }
    generated_at = data.get("generated_at")
    try:
        age_s = max(0, int(time.time() - int(generated_at)))
    except (TypeError, ValueError):
        age_s = max(0, int(time.time() - path.stat().st_mtime))
    return {
        **base,
        "status": data.get("status") or "unknown",
        "generated_at": generated_at,
        "age_s": age_s,
        "live_candidate_count": int(data.get("live_candidate_count") or 0),
        "eligible_live_candidate_count": int(
            data.get("eligible_live_candidate_count") or 0
        ),
        "unrecorded_live_candidate_count": int(
            data.get("unrecorded_live_candidate_count") or 0
        ),
        "commands": data.get("commands") or [],
        "live_targets": data.get("live_targets") or [],
        "recommendation": data.get("recommendation") or base["recommendation"],
    }


def _keepalive_supervisor_summary(
    corpus_path: Path | None = None,
    redirect_corpus_path: Path | None = None,
) -> dict:
    """Read-only trigger check for the deferred live keepalive supervisor.

    The backlog explicitly defers live action until the shadow corpus has enough
    failure-outcome disagreement signal. This summary makes that gate visible in
    the periodic report without changing any controller behavior.
    """
    path = corpus_path or keepalive_shadow.CORPUS_PATH
    summary = keepalive_shadow.summarize(path)
    labeled_ready = bool(summary.get("ready_for_ab"))
    failure_outcomes = int(summary.get("failure_outcomes") or 0)
    meaningful = int(summary.get("meaningful_disagreements") or 0)
    failure_ready = failure_outcomes >= KEEPALIVE_SUPERVISOR_MIN_FAILURE_OUTCOMES
    disagreement_ready = meaningful >= KEEPALIVE_SUPERVISOR_MIN_MEANINGFUL_DISAGREEMENTS
    stage2 = _redirect_stage2_summary(redirect_corpus_path, corpus_path)
    if not path.exists():
        status = "no_corpus"
        recommendation = "keep shadow-only corpus collection running; no live supervisor evidence exists yet"
    elif not labeled_ready:
        status = "collecting_labels"
        recommendation = "continue shadow/backfill collection until labeled trajectories reach the readiness target"
    elif not failure_ready:
        status = "success_heavy_underpowered"
        recommendation = (
            "do not run a live-supervisor A/B yet; collect more failure outcomes first"
        )
    elif not disagreement_ready:
        status = "failure_disagreement_underpowered"
        recommendation = "do not run a live-supervisor A/B yet; failures exist but shadow-vs-keepalive divergence is too thin"
    else:
        status = "armed_for_layered_ab_review"
        if stage2["ready_for_supervised_apply"]:
            recommendation = (
                "proposal evidence is ready for Stage 3 design review; live supervisor remains disabled "
                "until an explicit supervised-apply path is implemented"
            )
        else:
            recommendation = (
                f"{stage2.get('recommendation')}; live supervisor remains disabled"
            )
    return {
        "corpus_path": str(path),
        "status": status,
        "live_supervisor_allowed": False,
        "recommendation": recommendation,
        "thresholds": {
            "labeled_outcomes": summary.get(
                "readiness_target", keepalive_shadow.READINESS_TARGET
            ),
            "failure_outcomes": KEEPALIVE_SUPERVISOR_MIN_FAILURE_OUTCOMES,
            "meaningful_disagreements": KEEPALIVE_SUPERVISOR_MIN_MEANINGFUL_DISAGREEMENTS,
        },
        "labeled_ready": labeled_ready,
        "failure_signal_ready": failure_ready and disagreement_ready,
        "summary": summary,
        "stage2_proposal_corpus": stage2,
    }


def _skipped_langsmith_artifact_distribution(reason: str) -> dict:
    return {
        "schema_version": langsmith_fetch.ARTIFACT_DISTRIBUTION_SCHEMA_VERSION,
        "status": "skipped",
        "registry": str(langsmith_fetch.DEFAULT_REGISTRY),
        "registered_repos": 0,
        "expected_repos": 0,
        "exempted_repos": 0,
        "visible_artifacts_found": 0,
        "per_repo_artifacts_found": 0,
        "per_repo_artifacts_missing": 0,
        "per_repo_coverage": None,
        "missing_expected_with_recent_runs": 0,
        "missing_expected_with_recent_producer_runs": 0,
        "missing_expected_without_recent_runs": 0,
        "missing_expected_diagnostic_errors": 0,
        "missing_repos": [],
        "exempted_missing_repos": [],
        "rollup_artifact_found": False,
        "rollup_repo": langsmith_fetch.DEFAULT_ROLLUP_REPO,
        "rollup_prefix": langsmith_fetch.DEFAULT_ROLLUP_PREFIX,
        "rollup_artifact": None,
        "error_count": 0,
        "error_samples": [],
        "recommendation": reason,
    }


def _langsmith_artifact_distribution(
    *,
    artifact_health=_LANGSMITH_ARTIFACT_HEALTH_UNSET,
    probe: bool = True,
) -> dict:
    if artifact_health is not _LANGSMITH_ARTIFACT_HEALTH_UNSET:
        return artifact_health
    if not probe:
        return _skipped_langsmith_artifact_distribution(
            "LangSmith GitHub artifact probe skipped."
        )
    try:
        return langsmith_fetch.diagnose_artifact_distribution()
    except Exception as exc:
        skipped = _skipped_langsmith_artifact_distribution(
            "LangSmith GitHub artifact probe failed; run langsmith_fetch.py --dry-run --json for details."
        )
        skipped["status"] = "unknown"
        skipped["error_count"] = 1
        skipped["error_samples"] = [str(exc)]
        return skipped


def _langsmith_telemetry_summary(
    costs_by_source: list[dict],
    traces_by_source: list[dict],
) -> dict:
    cost_row = next(
        (row for row in costs_by_source if row.get("source") == "langsmith"),
        {},
    )
    trace_row = next(
        (row for row in traces_by_source if row.get("source") == "langsmith"),
        {},
    )
    cost_rows = int(cost_row.get("count") or 0)
    trace_rows = int(trace_row.get("count") or 0)
    if cost_rows and trace_rows:
        status = "flowing"
        recommendation = "Durable LangSmith telemetry rows are flowing; GitHub artifact distribution can be treated as a producer-coverage gap."
    elif cost_rows:
        status = "cost_only"
        recommendation = "LangSmith cost rows exist but trace rows are missing; inspect trace ingestion joins before treating telemetry as complete."
    elif trace_rows:
        status = "trace_only"
        recommendation = "LangSmith trace rows exist but cost rows are missing; inspect cost aggregation before using effort-aware routing."
    else:
        status = "dry"
        recommendation = "No durable LangSmith telemetry rows are visible; run langsmith_direct.py or langsmith_fetch.py ingestion."
    return {
        "status": status,
        "cost_rows": cost_rows,
        "trace_rows": trace_rows,
        "cost_usd": round(float(cost_row.get("cost_usd") or 0.0), 6),
        "avg_latency_s": trace_row.get("avg_latency_s"),
        "recommendation": recommendation,
    }


def _cost_trace_summary(
    window_days: int,
    *,
    langsmith_artifact_health=_LANGSMITH_ARTIFACT_HEALTH_UNSET,
    probe_langsmith_artifacts: bool = True,
) -> dict:
    since = _since(window_days)
    with feedback._conn() as c:
        costs = c.execute(
            "SELECT COALESCE(co.source,''), COUNT(*), "
            "COALESCE(SUM(co.tokens_in),0), COALESCE(SUM(co.tokens_out),0), "
            "COALESCE(SUM(co.cost_usd),0.0), COALESCE(AVG(co.latency_s),0.0) "
            "FROM costs co JOIN runs r ON co.run_id=r.run_id "
            "WHERE r.ts>=? GROUP BY co.source ORDER BY co.source",
            (since,),
        ).fetchall()
        trace_status = c.execute(
            "SELECT COALESCE(et.status,''), COUNT(*) "
            "FROM execution_traces et JOIN runs r ON et.run_id=r.run_id "
            "WHERE r.ts>=? GROUP BY et.status ORDER BY et.status",
            (since,),
        ).fetchall()
        traces = c.execute(
            "SELECT COALESCE(et.source,''), COUNT(*), "
            "COALESCE(SUM(et.cost_usd),0.0), COALESCE(AVG(et.latency_s),0.0) "
            "FROM execution_traces et JOIN runs r ON et.run_id=r.run_id "
            "WHERE r.ts>=? GROUP BY et.source ORDER BY et.source",
            (since,),
        ).fetchall()
    costs_by_source = [
        {
            "source": source or None,
            "count": count,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost_usd, 6),
            "avg_latency_s": round(avg_latency_s, 3),
        }
        for source, count, tokens_in, tokens_out, cost_usd, avg_latency_s in costs
    ]
    traces_by_source = [
        {
            "source": source or None,
            "count": count,
            "cost_usd": round(cost_usd, 6),
            "avg_latency_s": round(avg_latency_s, 3),
        }
        for source, count, cost_usd, avg_latency_s in traces
    ]
    return {
        "window_days": window_days,
        "costs_by_source": costs_by_source,
        "trace_status_counts": [
            {"status": status or None, "count": count} for status, count in trace_status
        ],
        "traces_by_source": traces_by_source,
        "worker_model_provenance": feedback.worker_model_provenance_summary(
            window_days=window_days
        ),
        "langsmith_telemetry": _langsmith_telemetry_summary(
            costs_by_source, traces_by_source
        ),
        "langsmith_artifact_distribution": _langsmith_artifact_distribution(
            artifact_health=langsmith_artifact_health,
            probe=probe_langsmith_artifacts,
        ),
    }


def _evidence_summary(window_days: int, min_gap_recurrence: int) -> dict:
    since = _since(window_days)
    with feedback._conn() as c:
        gaps = c.execute(
            "SELECT gap, COUNT(*) FROM evidence_gaps "
            "WHERE status='open' AND ts>=? GROUP BY gap ORDER BY COUNT(*) DESC, gap",
            (since,),
        ).fetchall()
        type_counts = c.execute(
            "SELECT status, COUNT(*) FROM evidence_types GROUP BY status ORDER BY status"
        ).fetchall()
        active = c.execute(
            "SELECT name, influence, rationale FROM evidence_types "
            "WHERE status='active' ORDER BY name"
        ).fetchall()
        retired = c.execute(
            "SELECT name, influence, rationale FROM evidence_types "
            "WHERE status='retired' ORDER BY name"
        ).fetchall()
    return {
        "window_days": window_days,
        "min_gap_recurrence": min_gap_recurrence,
        "open_gaps_by_recurrence": [
            {"gap": gap, "recurrence": count} for gap, count in gaps
        ],
        "proposals": feedback.propose_evidence_changes(
            min_recurrence=min_gap_recurrence,
            window_days=window_days,
        ),
        "evidence_types": {
            "counts_by_status": {status: count for status, count in type_counts},
            "active": [
                {"name": name, "influence": influence, "rationale": rationale}
                for name, influence, rationale in active
            ],
            "retired": [
                {"name": name, "influence": influence, "rationale": rationale}
                for name, influence, rationale in retired
            ],
        },
        "schema_growth": evidence_schema.build_report(
            window_days=window_days,
            min_recurrence=min_gap_recurrence,
        ),
    }


def _hypothesis_summary(path: Path | None = None) -> dict:
    path = path or HYPOTHESES_JSON
    try:
        raw_items = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raw_items = []
    status_counts: dict[str, int] = {}
    items = []
    for item in sorted(raw_items, key=lambda row: row.get("id", "")):
        evidence = item.get("evidence") or {}
        status = evidence.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        claim = " ".join((item.get("claim") or "").split())
        if len(claim) > 120:
            claim = claim[:117] + "..."
        items.append(
            {
                "id": item.get("id"),
                "status": status,
                "task_type": item.get("task_type"),
                "n": evidence.get("n"),
                "posterior": evidence.get("posterior"),
                "claim": claim,
            }
        )
    return {"path": str(path), "status_counts": status_counts, "hypotheses": items}


def _consumer_sync_hygiene_summary(state_dir: Path | None = None) -> dict:
    """Surface committed-debris findings in the check-in digest.

    Read-only and fail-soft. The ingest already measures this from the git tree it fetches anyway;
    without a line here the findings sat in a JSON artifact nobody opens daily, which is
    indistinguishable from having measured nothing. FYI only — no queue, nothing to action, and the
    judgment calls go through the auto-expiring owner-question path instead.
    """
    base = state_dir or Path(
        os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator")
    )
    path = base / "consumer-sync-artifact-ingest-report.json"
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"available": False, "reason": "no consumer-sync ingest report"}

    escalation = report.get("hygiene_escalation") or {}
    digest = escalation.get("digest") or []
    oversized = sorted(
        (
            {
                "repository": repo,
                "tracked_bytes": (row.get("hygiene") or {}).get("tracked_bytes", 0),
            }
            for repo, row in (report.get("repositories") or {}).items()
            if (row.get("hygiene") or {}).get("oversized")
        ),
        key=lambda r: -r["tracked_bytes"],
    )
    return {
        "available": True,
        "generated_at": report.get("generated_at"),
        "artifact": report.get("artifact_name"),
        "repositories_measured": len(report.get("repositories") or {}),
        "untrackable_bytes": escalation.get("untrackable_bytes", 0),
        "untrackable_findings": len(digest),
        "top_findings": digest[:5],
        "oversized_repositories": oversized,
        "open_owner_questions": len(escalation.get("questions") or []),
    }


def mining_coverage(window_days: int = 30, *, conn=None) -> dict:
    """Per-agent answer to "is the miner working, and on how much of the target?".

    An up/down signal is not enough. The miner can be perfectly healthy while covering one seat out
    of six, and that looks identical to full health if the only number reported is "it ran". This
    reports, per agent that actually did work: whether a profile exists, whether that profile's
    model identity can ever be RESOLVED (an `agy:`/`cursor:`/`vibe:` routing tag cannot), and how
    many of its worker attempts actually resolved.

    Verdicts, in the order they block:
      * ``no_runs``              - the seat did no work in the window; nothing to mine either way.
      * ``no_profile``           - no registered profile, so no worker attempt can ever be written.
      * ``model_not_reportable`` - profile exists, but the adapter reports a routing tag, so the
                                   attempt completes unresolved and the work stays unminable.
      * ``no_worker_attempt``    - profile and a reportable model, but nothing recorded an attempt.
      * ``attempts_unresolved``  - worker attempts exist, none carry a resolved model. Usually the
                                   seat's CLI keeps no per-session log; see
                                   `adapters.NO_SESSION_LOG_AGENTS` for the named reason.
      * ``minable``              - at least one resolved worker attempt exists.
    """
    close = conn is None
    c = conn or feedback._conn()
    cutoff = int(time.time()) - max(1, int(window_days)) * 86400
    try:
        runs = dict(c.execute(
            "SELECT agent, COUNT(*) FROM runs WHERE ts>=? AND agent IS NOT NULL "
            "AND agent<>'none' GROUP BY agent", (cutoff,)).fetchall())
        attempts = {}
        for agent, total, resolved in c.execute(
            "SELECT r.agent, COUNT(*), SUM(CASE WHEN ea.resolved_model IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM execution_attempts ea JOIN runs r ON r.run_id=ea.run_id "
            "WHERE ea.operation_role='worker' AND r.ts>=? GROUP BY r.agent", (cutoff,)).fetchall():
            attempts[agent] = (int(total or 0), int(resolved or 0))
    finally:
        if close:
            c.close()

    agents = sorted(set(runs) | set(attempts) | {
        p["agent"] for p in execution_profiles.PROFILE_REGISTRY.values()})
    rows, minable = {}, []
    for agent in agents:
        profiles = execution_profiles.profiles_for_agent(agent)
        total, resolved = attempts.get(agent, (0, 0))
        # A routing tag can never become resolved identity, so the profile cannot help this seat.
        reportable = bool(profiles) and not any(
            feedback.SYNTHETIC_ADAPTER_MODEL_RE.match(str(p["requested_model"]))
            for p in profiles)
        if resolved:
            verdict = "minable"
        elif not profiles:
            verdict = "no_profile"
        elif not reportable:
            verdict = "model_not_reportable"
        elif not runs.get(agent):
            verdict = "no_runs"
        elif total:
            # ATTEMPTS EXIST BUT NONE RESOLVED — a different fault from "nothing recorded an
            # attempt", and the old wording reported cursor as `no_worker_attempt` while it held 22
            # of them. That sends a reader to look for a dispatch bug when the real answer is that
            # the seat's CLI leaves no per-session log to read a model from. A wrong verdict is
            # worse than a missing one: it is confidently actionable in the wrong direction.
            verdict = "attempts_unresolved"
        else:
            verdict = "no_worker_attempt"
        if verdict == "minable":
            minable.append(agent)
        rows[agent] = {
            "runs": int(runs.get(agent, 0)),
            "profiles": len(profiles),
            "model_reportable": reportable,
            "worker_attempts": total,
            "resolved_worker_attempts": resolved,
            "verdict": verdict,
        }
    working = [a for a in agents if runs.get(a)]
    blocked = {a: r["verdict"] for a, r in rows.items() if r["verdict"] not in ("minable", "no_runs")}
    return {
        "window_days": int(window_days),
        "agents": rows,
        "minable_agents": sorted(minable),
        "working_agents": sorted(working),
        # The pair that makes a subset visible: how many seats CAN be mined out of how many are
        # actually doing work. "1 of 6" is a coverage problem; "it ran" hides it.
        "coverage": f"{len(minable)} of {len(working)} working agents minable",
        "blocked": blocked,
    }


def build_report(
    window_days: int = 90,
    min_gap_recurrence: int = 3,
    *,
    route_table=None,
    hypotheses_path: Path | None = None,
    features_path: Path | None = None,
    capabilities_path: Path | None = None,
    keepalive_corpus_path: Path | None = None,
    redirect_corpus_path: Path | None = None,
    model_profile_trial_path: Path | None = None,
    model_profile_qualification_path: Path | None = None,
    langsmith_artifact_health=_LANGSMITH_ARTIFACT_HEALTH_UNSET,
    probe_langsmith_artifacts: bool = True,
) -> dict:
    route_weights = _route_weight_summary(route_table=route_table)
    pattern_state_dir = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    pattern_status_path = pattern_state_dir / "pattern-miner-status.json"
    pattern_inventory_path = pattern_state_dir / "pattern-miner-inventory.json"
    try:
        pattern_status = json.loads(pattern_status_path.read_text()) if pattern_status_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        pattern_status = {}
    try:
        pattern_inventory = json.loads(pattern_inventory_path.read_text()) if pattern_inventory_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        pattern_inventory = {}
    try:
        range_rollout = json.loads((pattern_state_dir / "range-rollout.json").read_text())
    except (OSError, json.JSONDecodeError):
        range_rollout = {}
    runtime_flow = runtime_ac_flow_monitor.build_report(
        feedback.DB_PATH,
        runtime_ac_flow_monitor.DEFAULT_CRON_LOG,
        lookback_hours=min(window_days * 24, 24 * 30),
        min_closer_proxy=1,
        sample_limit=5,
    )
    return {
        "generated_at": int(time.time()),
        "read_only": True,
        "consumer_sync_hygiene": _consumer_sync_hygiene_summary(),
        "db_path": str(feedback.DB_PATH),
        "window_days": window_days,
        "min_gap_recurrence": min_gap_recurrence,
        "dataset": {
            "table_counts": _table_counts(),
            "route_weights_version": route_weights["latest_version"],
            "previous_route_weights_version": route_weights["previous_version"],
        },
        "route_weights": {"tasks": route_weights["tasks"]},
        "execution_profiles": feedback.profile_routing_summary(),
        "role_activation": feedback.role_activation_metrics(),
        "range_rollout": range_rollout,
        "runtime_ac_flow": runtime_flow,
        "model_profile_trial": model_profile_trial.build_report(
            model_profile_trial_path
        ),
        "model_profile_transport_qualification": (
            model_profile_trial_bridge.build_qualification_report(
                model_profile_qualification_path
            )
        ),
        # Coverage sits next to the miner status it qualifies: the status says whether the miner
        # RAN, the coverage says how much of the fleet it could ever cover. Reporting one without
        # the other is how "healthy" and "healthy on one seat of six" became indistinguishable.
        "mining_coverage": mining_coverage(window_days),
        "pattern_miner": {
            "status": pattern_status,
            "inventory": {
                "emitted_candidate_count": pattern_inventory.get("emitted_candidate_count", 0),
                "expired_candidate_count": pattern_inventory.get("expired_candidate_count", 0),
                "tombstone_count": len(pattern_inventory.get("tombstones") or []),
                "next_actions": pattern_inventory.get("next_actions") or [],
            },
        },
        "experiments": _experiment_identity_summary(window_days),
        "exploration_policy": exploration_review.build_report(
            route_table=route_table or router.ROUTE_TABLE,
            version=route_weights["latest_version"],
        ),
        "exploration_evidence_plan": exploration_evidence_plan.build_plan(
            route_table=route_table or router.ROUTE_TABLE,
        ),
        "exploration_backfill_plan": exploration_backfill.build_plan(
            route_table=route_table or router.ROUTE_TABLE,
        ),
        "outcomes": _outcome_summary(window_days),
        "production_flow": _production_flow_summary(window_days),
        "process_improvement": _process_improvement_summary(window_days),
        "keepalive_supervisor": _keepalive_supervisor_summary(
            keepalive_corpus_path,
            redirect_corpus_path,
        ),
        "costs_traces": _cost_trace_summary(
            window_days,
            langsmith_artifact_health=langsmith_artifact_health,
            probe_langsmith_artifacts=probe_langsmith_artifacts,
        ),
        "research_subjects": research_subjects.summary(window_days=window_days),
        "judge_reliability": judge_reliability.summarize(window_days=window_days),
        "human_calibration": human_calibration.summarize(window_days=window_days),
        # Objective anchors are emitted by experiment followup. This state is
        # diagnostic only and never creates an owner scoring queue.
        "human_calibration_queue": human_calibration.pending_queue(window_days=window_days),
        # item 16h: FYI-only — agents already proceeded on their defaults; answering merely
        # course-corrects future work. Unanswered questions auto-ratify at expiry (no backlog).
        "owner_questions": feedback.open_owner_questions(limit=10),
        "evidence": _evidence_summary(window_days, min_gap_recurrence),
        "dry_seams": dry_seam_audit.audit_dry_seams(
            window_days=window_days,
            route_table=route_table or router.ROUTE_TABLE,
        ),
        "hypotheses": _hypothesis_summary(hypotheses_path),
        "features": features.summary(features_path or features.REG, create=False),
        "capabilities": capabilities.summary(
            capabilities_path or capabilities.REG, create=False
        ),
    }


def _fmt_num(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.3f}"


def _fmt_delta(value) -> str:
    if value is None:
        return "n/a"
    if value > 0:
        return f"+{value}"
    return str(value)


def format_human(report: dict) -> str:
    lines = []
    dataset = report["dataset"]
    lines.append(
        f"periodic_report: db={report['db_path']} "
        f"route_weights_version={dataset['route_weights_version'] or 'none'} "
        f"window_days={report['window_days']}"
    )
    count_bits = [
        f"{table}={count}" for table, count in dataset["table_counts"].items() if count
    ]
    lines.append(f"dataset: {' '.join(count_bits) if count_bits else 'no rows yet'}")

    experiments = report.get("experiments") or {}
    lines.append(
        "experiments: "
        f"arms={len(experiments.get('implementation_arms') or [])} "
        f"evaluators={len(experiments.get('evaluator_identities') or [])} "
        f"missing_arm_outcomes={len(experiments.get('missing_arm_outcomes') or [])}"
    )
    for arm in experiments.get("implementation_arms") or []:
        mean = arm.get("mean_score")
        mean_text = f"{mean:.2f}" if mean is not None else "incomplete"
        lines.append(
            f"  {arm['experiment_id']}:{arm['arm_id']} "
            f"members={','.join(arm['members'])} agents={','.join(arm['agents'])} "
            f"profiles={','.join(arm['profiles']) or '-'} "
            f"n={arm['evaluation_observations']} mean={mean_text} "
            f"member_mean={arm['mean_member_evaluation_score']:.2f}"
        )
    for missing in experiments.get("missing_arm_outcomes") or []:
        lines.append(
            f"  missing: {missing['experiment_id']}:{missing['arm_id']} "
            f"members={','.join(missing['members'])} reason={missing['reason']}"
        )

    lines.append("route_weights (read-only; negative delta means the agent rose):")
    for task in report["route_weights"]["tasks"]:
        suffix = ""
        if task.get("diverges_from_prior"):
            suffix = " DIVERGED"
        elif task.get("cold_start"):
            suffix = " COLD_START"
        lines.append(f"  {task['task_type']}:{suffix}")
        lines.append(f"    prior:   {' > '.join(task['prior_order'])}")
        learned = task.get("learned_order") or []
        if not learned:
            if task.get("note"):
                lines.append(f"    note: {task['note']}")
            continue
        lines.append(f"    learned: {' > '.join(learned)}")
        if task.get("previous_order"):
            lines.append(f"    previous:{' > '.join(task['previous_order'])}")
        if task.get("note"):
            lines.append(f"    note: {task['note']}")
        for row in task.get("rows", []):
            lines.append(
                f"    {row['agent']:<11} rank={row['learned_rank']:>2} "
                f"d_prior={_fmt_delta(row['prior_rank_delta']):>4} "
                f"d_prev={_fmt_delta(row['previous_rank_delta']):>4} "
                f"posterior={_fmt_num(row['posterior']):>5} "
                f"score={_fmt_num(row['score']):>5} "
                f"n_obs={_fmt_num(row['n_obs']):>3}"
            )

    exploration = report.get("exploration_policy") or {}
    recorded = exploration.get("recorded_exploration_evidence") or {}
    lines.append(
        f"exploration policy: default={exploration.get('current_default')} "
        f"status={exploration.get('status')} "
        f"recommendation={exploration.get('recommendation')} "
        f"ready_tasks={exploration.get('ready_task_count')}/{exploration.get('task_count')} "
        f"zero_cell_rate={_fmt_num(exploration.get('zero_observation_cell_rate'))} "
        f"direct_ready={recorded.get('ready_for_direct_comparison')}"
    )
    if exploration.get("reason"):
        lines.append(f"  reason: {exploration['reason']}")
    if recorded:
        lines.append(
            f"  recorded exploration: instrumented={recorded.get('instrumented_runs')} "
            f"exploration_outcomes={recorded.get('outcome_exploration_runs')}"
        )
    acquisition = report.get("exploration_evidence_plan") or {}
    if acquisition:
        deficits = {
            row.get("mode"): row.get("remaining_outcome_runs")
            for row in acquisition.get("direct_mode_deficits") or []
        }
        candidates = [
            row
            for row in acquisition.get("candidate_task_types") or []
            if row.get("recommended")
        ]
        lines.append(
            f"exploration acquisition: stage={acquisition.get('stage')} "
            f"epsilon_remaining={deficits.get('epsilon-greedy')} "
            f"thompson_remaining={deficits.get('thompson-hybrid')} "
            f"candidate_task_types={len(candidates)}"
        )
        if acquisition.get("next_action"):
            lines.append(f"  next: {acquisition['next_action']}")
    backfill = report.get("exploration_backfill_plan") or {}
    if backfill:
        lines.append(
            f"exploration backfill: status={backfill.get('status')} "
            f"eligible={backfill.get('active_backfill_eligible')} "
            f"missing_cells={backfill.get('missing_cell_count')} "
            f"planned_jobs={len(backfill.get('planned_jobs') or [])}"
        )
        if backfill.get("next_action"):
            lines.append(f"  next: {backfill['next_action']}")

    outcomes = report["outcomes"]
    rollup = outcomes.get("rollup") or {}
    coverage = rollup.get("outcome_coverage")
    durable_rate = rollup.get("durable_success_rate")
    merged_rate = rollup.get("merged_rate")
    lines.append(
        f"all-run outcome rows diagnostic ({outcomes['window_days']}d, total={outcomes['total']}): "
        f"coverage={_fmt_num(coverage) if coverage is not None else 'n/a'} "
        f"merged_rate={_fmt_num(merged_rate) if merged_rate is not None else 'n/a'} "
        f"durable_success_rate={_fmt_num(durable_rate) if durable_rate is not None else 'n/a'}"
    )
    production_flow = report.get("production_flow") or {}
    lines.append(
        f"PRODUCTION outcome coverage: {_fmt_num(production_flow.get('outcome_coverage'))} "
        f"({production_flow.get('production_outcomes', 0)}/{production_flow.get('production_runs', 0)}); "
        f"status={production_flow.get('status')} "
        f"recent_{production_flow.get('recent_days', 7)}d_runs="
        f"{production_flow.get('recent_production_runs', 0)} "
        f"recent_{production_flow.get('recent_days', 7)}d_outcomes="
        f"{production_flow.get('recent_production_outcomes', 0)} "
        f"window_runs={production_flow.get('production_runs', 0)} "
        f"latest_run_age_days={production_flow.get('latest_run_age_days')}"
    )
    if production_flow.get("recommendation"):
        lines.append(f"  next: {production_flow['recommendation']}")
    for row in outcomes["by_task_agent_verdict"]:
        verdict = row["adjudicated_verdict"] or "-"
        durability = row["durability"] or "-"
        lines.append(
            f"  {row['task_type']}/{row['agent']} {verdict}/{durability} count={row['count']}"
        )

    process = report.get("process_improvement") or {}
    lines.append(
        f"process improvement ({process.get('window_days', report['window_days'])}d): "
        f"signals={len(process.get('signals') or [])} "
        f"suppressed={len(process.get('suppressed_process_failures') or [])} "
        f"non_durable_issue_runs={len(process.get('non_durable_issue_runs') or [])} "
        f"reviewed_issue_failures={len(process.get('reviewed_issue_failures') or [])}"
    )
    for row in process.get("work_type_rollup") or []:
        lines.append(
            f"  {row['work_type']}: runs={row['runs']} outcomes={row['outcomes']} "
            f"durable_success_rate={_fmt_num(row['durable_success_rate'])} "
            f"failure_rate={_fmt_num(row['durability_failure_rate'])}"
        )
    for signal in process.get("signals") or []:
        lines.append(
            f"  [{signal['severity']}] {signal['work_type']} failures={signal['failure_count']}: "
            f"{signal['recommendation']}"
        )
    for item in (process.get("non_durable_issue_runs") or [])[:5]:
        lines.append(
            f"  issue failure: {item['target']} {item['agent']} "
            f"{item['durability']} run={item['run_id']}"
        )

    supervisor = report.get("keepalive_supervisor") or {}
    sup_summary = supervisor.get("summary") or {}
    thresholds = supervisor.get("thresholds") or {}
    lines.append(
        f"keepalive supervisor gate: status={supervisor.get('status')} "
        f"labeled={sup_summary.get('labeled_outcomes', 0)}/{thresholds.get('labeled_outcomes')} "
        f"failures={sup_summary.get('failure_outcomes', 0)}/{thresholds.get('failure_outcomes')} "
        f"meaningful_disagreements={sup_summary.get('meaningful_disagreements', 0)}/"
        f"{thresholds.get('meaningful_disagreements')}"
    )
    stage2 = supervisor.get("stage2_proposal_corpus") or {}
    stage2_summary = stage2.get("summary") or {}
    lines.append(
        f"  stage2 proposals: status={stage2.get('status')} "
        f"valid={stage2_summary.get('valid_proposals', 0)}/{stage2_summary.get('readiness_target')} "
        f"linked={stage2_summary.get('synced_role_outcomes', 0)}/{stage2_summary.get('linked_outcome_target')} "
        f"disagreement_links={stage2_summary.get('linked_disagreements', 0)}/"
        f"{stage2_summary.get('disagreement_outcome_target')} "
        f"ready_for_supervised_apply={stage2.get('ready_for_supervised_apply')}"
    )
    lines.append(f"  recommendation: {supervisor.get('recommendation')}")

    costs = report["costs_traces"]
    lines.append(f"costs ({costs['window_days']}d):")
    if costs["costs_by_source"]:
        for row in costs["costs_by_source"]:
            lines.append(
                f"  {row['source'] or '-'} n={row['count']} "
                f"tokens={row['tokens_in']}+{row['tokens_out']} "
                f"cost=${row['cost_usd']:.4f} avg_latency={row['avg_latency_s']:.2f}s"
            )
    else:
        lines.append("  none")

    if costs["trace_status_counts"] or costs["traces_by_source"]:
        lines.append(f"traces ({costs['window_days']}d):")
        for row in costs["trace_status_counts"]:
            lines.append(f"  status={row['status'] or '-'} count={row['count']}")
        for row in costs["traces_by_source"]:
            lines.append(
                f"  source={row['source'] or '-'} n={row['count']} "
                f"cost=${row['cost_usd']:.4f} avg_latency={row['avg_latency_s']:.2f}s"
            )
    artifact_health = costs.get("langsmith_artifact_distribution") or {}
    if artifact_health:
        lines.append(
            "langsmith artifacts: "
            f"status={artifact_health.get('status')} "
            f"per_repo={artifact_health.get('per_repo_artifacts_found')}/"
            f"{artifact_health.get('expected_repos', artifact_health.get('registered_repos'))} "
            f"registered={artifact_health.get('registered_repos')} "
            f"exempted={artifact_health.get('exempted_repos', 0)} "
            f"missing_with_runs={artifact_health.get('missing_expected_with_recent_runs', 0)} "
            f"rollup={'yes' if artifact_health.get('rollup_artifact_found') else 'no'}"
        )
        if artifact_health.get("recommendation"):
            lines.append(f"  recommendation: {artifact_health['recommendation']}")
    langsmith_telemetry = costs.get("langsmith_telemetry") or {}
    if langsmith_telemetry:
        lines.append(
            "langsmith telemetry: "
            f"status={langsmith_telemetry.get('status')} "
            f"cost_rows={langsmith_telemetry.get('cost_rows')} "
            f"trace_rows={langsmith_telemetry.get('trace_rows')}"
        )
        if langsmith_telemetry.get("recommendation"):
            lines.append(f"  recommendation: {langsmith_telemetry['recommendation']}")
    provenance = costs.get("worker_model_provenance") or {}
    if provenance:
        eligible_workers = provenance.get("eligible_worker_runs")
        lines.append(
            "worker model provenance: "
            f"requested={provenance.get('requested_worker_runs')}/"
            f"{eligible_workers} "
            f"resolved={provenance.get('resolved_worker_runs')}/"
            f"{eligible_workers} "
            f"unknown={provenance.get('unknown_worker_runs')} "
            f"excluded_nonworker={provenance.get('excluded_nonworker_runs')} "
            f"worker_evaluator_overlap={provenance.get('worker_evaluator_role_overlap_runs')} "
            f"resolved_model_collisions="
            f"{provenance.get('worker_evaluator_resolved_model_collision_runs')} "
            f"legacy_nonworker_collisions="
            f"{provenance.get('legacy_worker_nonworker_model_collision_runs')} "
            f"unmigrated_traces={provenance.get('unmigrated_legacy_trace_rows')}"
        )
    capability_report = report.get("capabilities") or {}
    lines.append(
        "capability lifecycle: "
        f"total={capability_report.get('total', 0)} "
        f"states={json.dumps(capability_report.get('counts_by_status') or {}, sort_keys=True)} "
        f"invalid_active_edges={len(capability_report.get('active_without_edges') or [])}"
    )
    for capability_id, cap in sorted((capability_report.get("capabilities") or {}).items()):
        last_outcome = max(
            (
                int(event.get("timestamp") or 0)
                for event in cap.get("event_history") or []
                if event.get("type") == "outcome"
            ),
            default=0,
        ) or None
        lines.append(
            f"  {capability_id}: state={cap.get('status')} "
            f"liveness={capabilities.classify_liveness(cap)} "
            f"gate={cap.get('gate_reason') or '-'} match={cap.get('last_match')} "
            f"invocation={cap.get('last_invocation')} outcome={last_outcome} "
            f"expiry={cap.get('expiry')} next={cap.get('next_transition')} "
            f"rollback={json.dumps(cap.get('rollback'), sort_keys=True)} "
            f"upstream={cap.get('predecessor')} successor={cap.get('successor')}"
        )
    profile_report = report.get("execution_profiles") or {}
    lines.append(
        "execution profiles: "
        f"ready={profile_report.get('ready_profiles', 0)} "
        f"cold={profile_report.get('cold_starts', 0)} "
        f"decisions={profile_report.get('routing_decisions', 0)} "
        f"mean_propensity={profile_report.get('mean_assignment_probability', 0):.3f} "
        f"shared_pool_burn={json.dumps(profile_report.get('shared_pool_burn') or {}, sort_keys=True)} "
        f"v2_reads={profile_report.get('profile_weight_reads_enabled', False)}"
    )
    for profile in profile_report.get("profiles") or []:
        lines.append(
            f"  {profile.get('profile_id')}: model={profile.get('requested_model')} "
            f"coverage={profile.get('resolved_model_coverage', 0):.3f} "
            f"fallback={profile.get('fallback_rate', 0):.3f} "
            f"evidence_age_days={_fmt_num(profile.get('evidence_age_days'))}"
        )
    trial_report = report.get("model_profile_trial") or {}
    lines.append(
        "model profile trial: "
        f"status={trial_report.get('status', 'not_run')} "
        f"lifecycle={trial_report.get('lifecycle', 'shadow')} "
        f"attempts={trial_report.get('attempt_count', 0)} "
        f"source_unchanged={(trial_report.get('source_integrity') or {}).get('unchanged')} "
        f"shared_pool_debit={json.dumps(trial_report.get('shared_pool_debit') or {}, sort_keys=True)} "
        f"learning={trial_report.get('learning_enabled', False)}"
    )
    qualification = report.get("model_profile_transport_qualification") or {}
    lines.append(
        "model profile transport qualification: "
        f"status={qualification.get('status', 'not_qualified')} "
        f"transport_contract={qualification.get('transport_contract_qualified', False)} "
        f"provider_identity={qualification.get('provider_identity_status', 'unavailable_unclaimed')} "
        f"learning={qualification.get('learning_enabled', False)} "
        f"quality_weights={qualification.get('quality_weight_updates_allowed', False)}"
    )

    subjects = report.get("research_subjects") or {}
    lines.append(
        f"research subjects ({subjects.get('window_days', report['window_days'])}d): "
        f"registered={subjects.get('registered_subjects', 0)} "
        f"independent={subjects.get('independent_subjects', 0)} "
        f"unevaluated_backlog={subjects.get('unevaluated_backlog', 0)} "
        f"duplicate_rejections={subjects.get('duplicate_rejections', 0)} "
        f"production_collisions={subjects.get('research_production_collisions', 0)} "
        f"effective_n={subjects.get('effective_sample_count', 0)}"
    )
    if subjects.get("true_task_type_distribution"):
        task_bits = " ".join(
            f"{task_type}={count}"
            for task_type, count in sorted(
                subjects["true_task_type_distribution"].items()
            )
        )
        lines.append(f"  true task types: {task_bits}")
    range_state = report.get("range_rollout") or {}
    lines.append(
        f"range rollout: eligible={range_state.get('eligible')} "
        f"blocked={json.dumps(range_state.get('blocked_reasons') or [])} "
        f"claims={json.dumps(range_state.get('claimed_by') or {}, sort_keys=True)} "
        f"capacity_rejections={len(range_state.get('capacity_rejections') or [])}"
    )
    runtime_state = report.get("runtime_ac_flow") or {}
    lines.append(
        f"runtime-ac flow: status={runtime_state.get('status')} "
        f"live_firing={runtime_state.get('runtime_ac_live_firing')} "
        f"required={runtime_state.get('required_event_denominator')} "
        f"executed={runtime_state.get('executed_gate_numerator')} "
        f"closer_proxy_diagnostic={runtime_state.get('closer_proxy_present')} "
        f"reason={runtime_state.get('reason') or ((runtime_state.get('alerts') or [{}])[0].get('message'))} "
        f"next={json.dumps(runtime_state.get('actions') or [])}"
    )
    compiler = report.get("pattern_miner") or {}
    # `.get('status').get('status')` printed None forever: the miner's status artifact has no
    # `status` key. `mining_health` is the field that actually says what happened.
    health = (compiler.get("status") or {}).get("mining_health") or {}
    lines.append(
        f"pattern compiler: state={health.get('state', 'unknown')} "
        f"[{health.get('summary', 'no summary')}] "
        f"candidates={(compiler.get('inventory') or {}).get('emitted_candidate_count', 0)} "
        f"expired={(compiler.get('inventory') or {}).get('expired_candidate_count', 0)} "
        f"tombstones={(compiler.get('inventory') or {}).get('tombstone_count', 0)} "
        f"next={json.dumps((compiler.get('inventory') or {}).get('next_actions') or [])}"
    )
    cov = report.get("mining_coverage") or {}
    if cov:
        lines.append(f"mining coverage ({cov.get('window_days')}d): {cov.get('coverage')}"
                     + (f" | minable: {', '.join(cov['minable_agents'])}" if cov.get("minable_agents") else ""))
        for agent, why in sorted((cov.get("blocked") or {}).items()):
            row = (cov.get("agents") or {}).get(agent) or {}
            lines.append(f"  {agent:<9} {why:<22} runs={row.get('runs', 0)} "
                         f"worker={row.get('resolved_worker_attempts', 0)}/{row.get('worker_attempts', 0)}")
    if subjects.get("rejections_by_reason"):
        rejection_bits = " ".join(
            f"{reason}={count}"
            for reason, count in sorted(subjects["rejections_by_reason"].items())
        )
        lines.append(f"  subject rejections: {rejection_bits}")

    reliability = report.get("judge_reliability") or {}
    lines.append(
        f"judge reliability ({reliability.get('window_days', report['window_days'])}d): "
        f"judges={reliability.get('judge_count', 0)} ready={reliability.get('ready_judge_count', 0)}"
    )
    for name, row in sorted((reliability.get("judges") or {}).items()):
        status = "ready" if row.get("ready") else "not-ready"
        lines.append(
            f"  {name:<11} {status:<9} weight={_fmt_num(row.get('weight')):>5} "
            f"comparisons={_fmt_num(row.get('comparisons')):>3} "
            f"mae={_fmt_num(row.get('mean_abs_error')):>5}"
        )

    calibration = report.get("human_calibration") or {}
    lines.append(
        f"human calibration ({calibration.get('window_days', report['window_days'])}d): "
        f"status={calibration.get('status')} ready={calibration.get('ready')} "
        f"anchors={calibration.get('structured_anchor_count', 0)} "
        f"pairs={calibration.get('matched_pair_count', 0)}"
    )
    if calibration.get("recommendation"):
        lines.append(f"  recommendation: {calibration['recommendation']}")
    queue = report.get("human_calibration_queue") or {}
    queue_items = queue.get("items") or []
    if queue_items:
        lines.append(
            f"  OBJECTIVE ANCHOR STATE ({len(queue_items)} of {queue.get('pending_total', 0)} pending): "
            f"owner_action_required={queue.get('owner_action_required', False)}"
        )
        for item in queue_items:
            scores = " ".join(
                f"{k}={v:g}" for k, v in sorted((item.get("judge_scores") or {}).items())
            )
            lines.append(
                f"    {item.get('experiment_id')}:{item.get('implementer')} "
                f"target={item.get('target') or 'n/a'} judges[{scores}] "
                f"spread={item.get('judge_spread')} status={item.get('status')}"
            )
            lines.append(f"      next={item.get('next_transition')}")
    owner_questions = report.get("owner_questions") or []
    if owner_questions:
        lines.append(
            f"  OWNER QUESTIONS ({len(owner_questions)} open, FYI-only — work already proceeded "
            "on the stated defaults; unanswered questions auto-adopt them at expiry)"
        )
        for q in owner_questions:
            lines.append(
                f"    [{q.get('question_id')}] ({q.get('repo') or q.get('target') or 'fleet'}) "
                f"{q.get('question')} | default: {q.get('default_action')}"
            )
            lines.append(
                f"      answer: python3 feedback.py answer {q.get('question_id')} \"<answer>\""
            )

    evidence = report["evidence"]
    type_counts = evidence["evidence_types"]["counts_by_status"]
    lines.append(
        f"evidence ({evidence['window_days']}d): "
        f"open_gap_kinds={len(evidence['open_gaps_by_recurrence'])} "
        f"proposals={len(evidence['proposals'])} "
        f"active_types={type_counts.get('active', 0)} "
        f"retired_types={type_counts.get('retired', 0)}"
    )
    for row in evidence["open_gaps_by_recurrence"][:5]:
        lines.append(f"  gap x{row['recurrence']}: {row['gap']}")
    for row in evidence["proposals"]:
        lines.append(f"  proposal: {row['gap']} (recurrence={row['recurrence']})")
    schema_growth = evidence.get("schema_growth") or {}
    lines.append(
        f"evidence schema growth: status={schema_growth.get('status')} "
        f"clustered_proposals={schema_growth.get('clustered_proposal_count', 0)}"
    )
    active_review = schema_growth.get("active_type_review") or {}
    lines.append(
        f"evidence active-type review: active={active_review.get('active_count', 0)} "
        f"prune_candidates={active_review.get('prune_candidate_count', 0)}"
    )
    for row in (schema_growth.get("clustered_proposals") or [])[:5]:
        lines.append(
            f"  clustered proposal: {row['name']} "
            f"(recurrence={row['recurrence']}, refs={row['ref_count']})"
        )
    for row in (active_review.get("active") or [])[:5]:
        lines.append(
            f"  active evidence type: {row['name']} status={row['status']} "
            f"influence={row['influence']} age_days={row['age_days']}"
        )

    dry_seams = report["dry_seams"]
    seam_counts = dry_seams.get("status_counts") or {}
    lines.append(
        f"dry seams: overall={dry_seams['overall']} "
        f"fail={seam_counts.get('fail', 0)} warn={seam_counts.get('warn', 0)} "
        f"findings={len(dry_seams.get('findings') or [])}"
    )
    lineage = dry_seams.get("completion_event_health") or {}
    lines.append(
        "completion lineage: "
        f"events={lineage.get('total', 0)} complete={lineage.get('complete', 0)} "
        f"durable={lineage.get('durable', 0)} redacted={lineage.get('redacted', 0)} "
        f"rejected={lineage.get('rejected', 0)} orphans={lineage.get('orphan_edges', 0)} "
        f"accepted_linked={lineage.get('accepted_influence_linked', 0)}"
    )
    for role_name, metrics in (report.get("role_activation") or {}).get("roles", {}).items():
        lines.append(
            f"role {role_name}: matched={metrics.get('matched', 0)} "
            f"invoked={metrics.get('invoked', 0)} accepted={metrics.get('accepted', 0)} "
            f"rejected={metrics.get('rejected', 0)} linked={metrics.get('linked', 0)} "
            f"durable={metrics.get('durable', 0)} readiness={metrics.get('evidence_readiness')}"
        )
    outcome_gaps = dry_seams.get("outcome_gap_summary") or {}
    if outcome_gaps.get("total_runs_without_outcome"):
        lines.append(
            "outcome gaps: "
            f"total={outcome_gaps.get('total_runs_without_outcome', 0)} "
            f"actionable={outcome_gaps.get('actionable_runs_without_outcome', 0)} "
            f"advisory_or_unlinked={outcome_gaps.get('advisory_or_expected_unlinked', 0)}"
        )
        for row in (outcome_gaps.get("categories") or [])[:5]:
            lines.append(
                f"  outcome gap: {row['category']} "
                f"count={row['count']} actionable={row['actionable']}"
            )
    for item in (dry_seams.get("findings") or [])[:8]:
        lines.append(f"  [{item['status']}] {item['sink']}: {item['finding']}")

    feature_report = report.get("features") or {}
    feature_counts = feature_report.get("counts_by_maturity") or {}
    lines.append(
        f"features: total={feature_report.get('total', 0)} "
        f"ad-hoc={feature_counts.get('ad-hoc', 0)} "
        f"reused={feature_counts.get('reused', 0)} "
        f"hardened={feature_counts.get('hardened', 0)} "
        f"promotion_candidates={len(feature_report.get('promotion_candidates') or [])}"
    )
    for item in (feature_report.get("promotion_candidates") or [])[:5]:
        lines.append(f"  promote: {item['name']} x{item['count']} — {item['problem']}")

    approval = report.get("evidence_approval")
    if approval:
        mode = "applied" if approval.get("applied") else "preview"
        lines.append(
            f"evidence_approval ({mode}): name={approval['name']} "
            f"gap={approval['gap']!r} recurrence={approval['recurrence']}/"
            f"{approval['min_recurrence']} eligible={approval['eligible']} "
            f"gaps_marked={approval['gaps_marked']} "
            f"type_status={approval['evidence_type_status']}"
        )
        if approval.get("blocked_reason"):
            lines.append(f"  blocked: {approval['blocked_reason']}")

    hypotheses = report["hypotheses"]
    status_bits = " ".join(
        f"{status}={count}"
        for status, count in sorted(hypotheses["status_counts"].items())
    )
    lines.append(f"hypotheses ({status_bits or 'none'}):")
    for row in hypotheses["hypotheses"]:
        lines.append(f"  {row['id']} [{row['status']}] {row['claim']}")

    return "\n".join(lines)


def _seed_selftest_data(route_table: dict, now: int) -> Path:
    priors = priors_from_route_table(route_table)
    with feedback._conn() as c:
        for agent, prior in priors["implement"].items():
            score = {"prior_top": 0.9, "evidence_wins": 0.8, "also_ran": 0.7}[agent]
            c.execute(
                "INSERT INTO route_weights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    now,
                    "implement",
                    agent,
                    prior,
                    prior,
                    0,
                    None,
                    None,
                    score,
                    "fixture v1",
                    now - 86400,
                    now,
                ),
            )

    for i in range(4):
        run_id = f"win-{i}"
        feedback.record_run(
            run_id, "o/r#1", "implement", "evidence_wins", ts=now - 3600
        )
        feedback.record_outcome(
            run_id, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
        feedback.record_cost(
            run_id,
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.5,
            latency_s=2.0,
            source="ledger",
        )
        feedback.record_execution_trace(
            run_id,
            trace_id=f"tr-{i}",
            provider="openai",
            model="gpt-worker-test",
            operation="implement",
            operation_role="worker",
            profile_id="openai/gpt-worker-test/default",
            requested_provider="openai",
            requested_model="gpt-worker-test",
            resolved_model="gpt-worker-test",
            status="success",
            cost_usd=0.5,
            latency_s=2.0,
            source="langsmith",
        )
    feedback.record_execution_trace(
        "win-0",
        trace_id="tr-win-0-evaluator",
        provider="anthropic",
        model="claude-evaluator-test",
        operation="evaluate_pr_compare",
        status="success",
        source="langsmith",
    )
    for i in range(2):
        run_id = f"lose-{i}"
        feedback.record_run(run_id, "o/r#2", "implement", "prior_top", ts=now - 7200)
        feedback.record_outcome(
            run_id, adjudicated_verdict="PASS", merged=True, durability="reverted"
        )
        feedback.record_cost(run_id, cost_usd=2.0, latency_s=5.0, source="langsmith")
    feedback.record_run(
        "process-renovate-reverted",
        "o/r#20",
        "implement",
        "none",
        pr_number=20,
        ts=now - 1800,
        source="keepalive",
        assignment="none",
        work_type="renovate",
    )
    feedback.record_outcome(
        "process-renovate-reverted",
        adjudicated_verdict="PASS",
        merged=True,
        durability="reverted",
        notes="fixture dependency loop reverted",
    )
    feedback.record_run(
        "process-sync-abandoned",
        "o/r#21",
        "implement",
        "none",
        pr_number=21,
        ts=now - 1200,
        source="keepalive",
        assignment="none",
        work_type="sync",
    )
    feedback.record_outcome(
        "process-sync-abandoned",
        adjudicated_verdict="",
        merged=False,
        durability="abandoned",
        notes="fixture sync loop abandoned",
    )
    feedback.record_run(
        "process-sync-duplicate",
        "o/r#22",
        "implement",
        "none",
        pr_number=22,
        ts=now - 900,
        source="keepalive",
        assignment="none",
        work_type="sync",
    )
    feedback.record_outcome(
        "process-sync-duplicate",
        adjudicated_verdict="",
        merged=False,
        durability="abandoned",
        notes=(
            "remote keepalive PR closed unmerged; "
            "process_ignore=duplicate_or_superseded"
        ),
    )
    feedback.record_run(
        "issue-reviewed",
        "o/r#23",
        "implement",
        "codex",
        pr_number=23,
        ts=now - 600,
        source="keepalive",
        assignment="assigned",
        work_type="issue",
    )
    feedback.record_outcome(
        "issue-reviewed",
        adjudicated_verdict="FAIL",
        merged=False,
        durability="abandoned",
        notes="fixture reviewed issue failure; issue_review=duplicate_or_superseded",
    )

    feedback.record_evaluation("exp-self", "evidence_wins", "judge-a", 9.0)
    for _ in range(3):
        feedback.record_evidence_gap(
            "exp-gap", "judge-a", "need test-execution output to judge"
        )
    feedback.record_evidence_type("test_run_output", "already approved")
    feedback.record_evidence_type("unused_field", "speculative")
    feedback.bump_evidence_influence("test_run_output")
    feedback.prune_dead_evidence(min_influence=1)
    subject = research_subjects.subject_identity(
        "o/r#subject-report",
        "testgen",
        "Generate robust tests",
        "abc123",
        ["codex", "cursor"],
    )
    research_subjects.record_subject(
        subject,
        lifecycle="active",
        exp_id="subject-report-exp",
        now=now,
    )
    research_subjects.record_event(
        "rejected",
        identity=subject,
        reason="subject_cooldown",
        ts=now,
    )
    research_subjects.record_event(
        "rejected",
        identity=subject,
        reason="production_reserved",
        ts=now,
    )

    with feedback._conn() as c:
        for agent, score in [
            ("evidence_wins", 1.1),
            ("prior_top", 0.9),
            ("also_ran", 0.7),
        ]:
            prior = priors["implement"][agent]
            c.execute(
                "INSERT INTO route_weights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    2,
                    now,
                    "implement",
                    agent,
                    prior,
                    prior + 0.05,
                    4,
                    0.75,
                    0.5,
                    score,
                    "fixture v2",
                    now - 86400,
                    now,
                ),
            )

    hypotheses_path = feedback.DB_PATH.parent / "hypotheses.json"
    hypotheses_path.write_text(
        json.dumps(
            [
                {
                    "id": "H-test",
                    "claim": "evidence_wins beats prior_top in fixture data",
                    "task_type": "implement",
                    "evidence": {"n": 6, "posterior": 0.8, "status": "accumulating"},
                }
            ]
        )
    )
    return hypotheses_path


def _seed_keepalive_supervisor_corpus(path: Path) -> None:
    for idx in range(25):
        keepalive_shadow.record(
            {
                "target": f"o/r#{100 + idx}",
                "keepalive_blunt": "continue",
                "shadow_action": "wait",
                "disagreement": False,
                "outcome": "durable",
            },
            path,
        )
    for idx in range(3):
        keepalive_shadow.record(
            {
                "target": f"o/r#{200 + idx}",
                "keepalive_blunt": "switch-agent",
                "shadow_action": "inspect",
                "disagreement": True,
                "outcome": "needs_human",
            },
            path,
        )
    for idx in range(2):
        keepalive_shadow.record(
            {
                "target": f"o/r#{300 + idx}",
                "keepalive_blunt": "continue",
                "shadow_action": "wait",
                "disagreement": False,
                "outcome": "closed_unmerged",
            },
            path,
        )


def _seed_redirect_stage2_corpus(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for idx in range(redirect_shadow.READINESS_TARGET):
        role_run_id = (
            f"role:redirect:fixture:{idx}"
            if idx < redirect_shadow.LINKED_OUTCOME_TARGET
            else None
        )
        rows.append(
            {
                "kind": "redirect_proposal",
                "schema_version": redirect_shadow.SCHEMA_VERSION,
                "entry_id": f"stage2-proposal-{idx}",
                "target": f"o/r#{400 + idx}",
                "role_run_id": role_run_id,
                "source": "live-dispatch",
                "valid_proposal": True,
                "errors": [],
                "backend": "fixture",
                "baseline_action": "wait",
                "proposal_action": "redirect" if role_run_id else "wait",
                "plan_action": "redirect" if role_run_id else "wait",
                "disagreement": bool(role_run_id),
                "report": {"state": "stalled"},
            }
        )
        if role_run_id:
            rows.append(
                {
                    "kind": "redirect_outcome_link",
                    "schema_version": redirect_shadow.SCHEMA_VERSION,
                    "entry_id": f"stage2-link-{idx}",
                    "role_run_id": role_run_id,
                    "influenced_run_id": f"downstream-{idx}",
                    "accepted": True,
                    "link_result": {"synced": True},
                    "role_outcome": {"success": True},
                    "downstream_outcome": {"success": True},
                }
            )
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _seed_blocked_redirect_stage2_corpus(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for idx in range(redirect_shadow.READINESS_TARGET):
        role_run_id = f"role:redirect:blocked:{idx}"
        rows.append(
            {
                "kind": "redirect_proposal",
                "schema_version": redirect_shadow.SCHEMA_VERSION,
                "entry_id": f"stage2-blocked-proposal-{idx}",
                "target": f"o/r#{600 + idx}",
                "role_run_id": role_run_id,
                "source": redirect_shadow.HISTORICAL_SOURCE,
                "valid_proposal": True,
                "errors": [],
                "backend": "fixture",
                "baseline_action": "collect",
                "proposal_action": "collect",
                "plan_action": "collect",
                "disagreement": False,
                "report": {"state": "exited"},
            }
        )
        if idx < redirect_shadow.LINKED_OUTCOME_TARGET:
            rows.append(
                {
                    "kind": "redirect_historical_outcome_link",
                    "schema_version": redirect_shadow.SCHEMA_VERSION,
                    "entry_id": f"stage2-blocked-historical-link-{idx}",
                    "role_run_id": role_run_id,
                    "target": f"o/r#{600 + idx}",
                    "counterfactual": True,
                    "not_role_learning": True,
                    "historical_outcome": {"success": True, "durability": "durable"},
                }
            )
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _selftest() -> None:
    import shutil

    old_db_path = feedback.DB_PATH
    temp_dir = Path(tempfile.mkdtemp(prefix="periodic-report-selftest-"))
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
        hypotheses_path = _seed_selftest_data(route_table, now)
        keepalive_corpus_path = temp_dir / "keepalive-shadow.jsonl"
        _seed_keepalive_supervisor_corpus(keepalive_corpus_path)
        redirect_corpus_path = temp_dir / "redirect-shadow.jsonl"
        _seed_redirect_stage2_corpus(redirect_corpus_path)
        features_path = temp_dir / "features.json"
        features.record_use(
            "fixture-reflection",
            "task-1",
            "capture reusable task-end patterns",
            features_path,
        )
        features.record_use("fixture-reflection", "task-2", path=features_path)
        features.record_use("fixture-reflection", "task-3", path=features_path)
        artifact_health = {
            "schema_version": langsmith_fetch.ARTIFACT_DISTRIBUTION_SCHEMA_VERSION,
            "status": "rollup_only",
            "registry": str(langsmith_fetch.DEFAULT_REGISTRY),
            "registered_repos": 3,
            "expected_repos": 2,
            "exempted_repos": 1,
            "visible_artifacts_found": 0,
            "per_repo_artifacts_found": 0,
            "per_repo_artifacts_missing": 2,
            "per_repo_coverage": 0.0,
            "missing_repos": [
                {
                    "repo": "stranske/One",
                    "artifact_name": langsmith_fetch.DEFAULT_ARTIFACT_NAME,
                    "artifact_expected": True,
                },
                {
                    "repo": "stranske/Two",
                    "artifact_name": langsmith_fetch.DEFAULT_ARTIFACT_NAME,
                    "artifact_expected": True,
                },
            ],
            "exempted_missing_repos": [
                {
                    "repo": "stranske/DirectOnly",
                    "artifact_name": langsmith_fetch.DEFAULT_ARTIFACT_NAME,
                    "artifact_expected": False,
                    "rollout_status": "covered-via-langsmith-direct",
                    "artifact_expectation_reason": "rollout_status=covered-via-langsmith-direct",
                }
            ],
            "rollup_artifact_found": True,
            "rollup_repo": langsmith_fetch.DEFAULT_ROLLUP_REPO,
            "rollup_prefix": langsmith_fetch.DEFAULT_ROLLUP_PREFIX,
            "rollup_artifact": {
                "name": "langsmith-fleet-rollup-fixture",
                "id": 42,
                "updated_at": "2026-06-23T00:00:00Z",
            },
            "error_count": 0,
            "error_samples": [],
            "recommendation": "fixture rollup path is visible while per-repo artifacts are dry",
        }

        report = build_report(
            window_days=90,
            min_gap_recurrence=3,
            route_table=route_table,
            hypotheses_path=hypotheses_path,
            features_path=features_path,
            keepalive_corpus_path=keepalive_corpus_path,
            redirect_corpus_path=redirect_corpus_path,
            langsmith_artifact_health=artifact_health,
        )
        task = report["route_weights"]["tasks"][0]
        assert task["learned_order"][0] == "evidence_wins", task
        assert task["diverges_from_prior"] is True, task
        assert task["rows"][0]["previous_rank_delta"] is not None, task
        assert report["outcomes"]["total"] == 10, report["outcomes"]
        assert report["outcomes"]["rollup"]["runs_total"] == 10, report["outcomes"]
        assert report["outcomes"]["rollup"]["outcome_coverage"] == 1.0, report[
            "outcomes"
        ]
        assert report["production_flow"]["status"] == "flowing", report[
            "production_flow"
        ]
        assert report["production_flow"]["recent_production_runs"] == 10, report[
            "production_flow"
        ]
        assert report["outcomes"]["rollup"]["merged_rate"] == 7 / 10, report["outcomes"]
        assert report["outcomes"]["rollup"]["durable_success_count"] == 4, report[
            "outcomes"
        ]
        assert report["outcomes"]["by_source_assignment"], report["outcomes"]
        process = report["process_improvement"]
        process_by_type = {row["work_type"]: row for row in process["work_type_rollup"]}
        assert process_by_type["renovate"]["durability_failures"] == 1, process
        assert process_by_type["sync"]["durability_failures"] == 1, process
        assert process["non_agent_by_work_type"], process
        process_signals = {row["work_type"]: row for row in process["signals"]}
        assert process_signals["renovate"]["severity"] == "HIGH", process
        assert process_signals["sync"]["severity"] == "MED", process
        assert len(process["suppressed_process_failures"]) == 1, process
        assert (
            process["suppressed_process_failures"][0]["reason"]
            == "duplicate_or_superseded"
        ), process
        assert len(process["non_durable_issue_runs"]) == 2, process
        assert len(process["reviewed_issue_failures"]) == 1, process
        assert (
            process["reviewed_issue_failures"][0]["reason"]
            == "duplicate_or_superseded"
        ), process
        supervisor = report["keepalive_supervisor"]
        assert supervisor["status"] == "armed_for_layered_ab_review", supervisor
        assert supervisor["live_supervisor_allowed"] is False, supervisor
        assert supervisor["summary"]["failure_outcomes"] == 5, supervisor
        assert supervisor["summary"]["meaningful_disagreements"] == 3, supervisor
        assert (
            supervisor["stage2_proposal_corpus"]["status"]
            == "ready_for_supervised_apply_review"
        ), supervisor
        assert (
            supervisor["stage2_proposal_corpus"]["ready_for_supervised_apply"] is True
        ), supervisor
        blocked_redirect_corpus = temp_dir / "redirect-stage2-blocked.jsonl"
        empty_keepalive_corpus = temp_dir / "keepalive-shadow-empty.jsonl"
        empty_keepalive_corpus.write_text("", encoding="utf-8")
        _seed_blocked_redirect_stage2_corpus(blocked_redirect_corpus)
        blocked_stage2 = _redirect_stage2_summary(
            blocked_redirect_corpus, empty_keepalive_corpus
        )
        assert blocked_stage2["status"] == "waiting_for_candidates", blocked_stage2
        assert (
            blocked_stage2["ready_for_historical_replay_analysis"] is False
        ), blocked_stage2
        assert blocked_stage2["historical_candidates_remaining"] == 0, blocked_stage2
        assert blocked_stage2["calibration_candidates_remaining"] == 0, blocked_stage2
        assert report["costs_traces"]["costs_by_source"], report["costs_traces"]
        assert report["costs_traces"]["trace_status_counts"], report["costs_traces"]
        subject_report = report["research_subjects"]
        assert subject_report["registered_subjects"] == 1, subject_report
        assert subject_report["true_task_type_distribution"] == {"testgen": 1}, subject_report
        assert subject_report["duplicate_rejections"] == 1, subject_report
        assert subject_report["research_production_collisions"] == 1, subject_report
        provenance = report["costs_traces"]["worker_model_provenance"]
        assert provenance["requested_worker_runs"] == 4, provenance
        assert provenance["resolved_worker_runs"] == 4, provenance
        assert provenance["unknown_worker_runs"] == 6, provenance
        assert provenance["worker_evaluator_role_overlap_runs"] == 1, provenance
        assert (
            report["costs_traces"]["langsmith_artifact_distribution"]["status"]
            == "rollup_only"
        ), report["costs_traces"]
        assert (
            report["costs_traces"]["langsmith_telemetry"]["status"] == "flowing"
        ), report["costs_traces"]
        assert report["judge_reliability"]["judge_count"] >= 1, report[
            "judge_reliability"
        ]
        assert any(
            p["recurrence"] >= 3 for p in report["evidence"]["proposals"]
        ), report["evidence"]
        assert (
            report["evidence"]["evidence_types"]["counts_by_status"]["active"] == 1
        ), report["evidence"]
        assert (
            report["evidence"]["evidence_types"]["counts_by_status"]["retired"] == 1
        ), report["evidence"]
        assert report["dry_seams"]["overall"] in {"pass", "warn", "fail"}, report[
            "dry_seams"
        ]
        assert "outcome_gap_summary" in report["dry_seams"], report["dry_seams"]
        assert report["hypotheses"]["status_counts"]["accumulating"] == 1, report[
            "hypotheses"
        ]
        assert (
            report["features"]["promotion_candidates"][0]["name"]
            == "fixture-reflection"
        ), report["features"]
        assert "evidence_wins" in format_human(report)
        assert "judge reliability" in format_human(report)
        assert "features:" in format_human(report)
        assert "dry seams:" in format_human(report)
        assert "PRODUCTION outcome coverage:" in format_human(report)
        assert "exploration backfill:" in format_human(report)
        human = format_human(report)
        assert "langsmith artifacts: status=rollup_only" in human, human
        assert "langsmith telemetry: status=flowing" in human, human
        assert "worker model provenance: requested=4/10 resolved=4/10 unknown=6" in human, human
        assert "research subjects (90d): registered=1 independent=1" in human, human

        snapshot_path = temp_dir / "snapshot.json"
        feedback.snapshot_json(snapshot_path)
        snapshot_report = build_report_from_snapshot(
            snapshot_path,
            window_days=90,
            min_gap_recurrence=3,
            route_table=route_table,
            hypotheses_path=hypotheses_path,
            features_path=features_path,
            keepalive_corpus_path=keepalive_corpus_path,
            redirect_corpus_path=redirect_corpus_path,
            langsmith_artifact_health=artifact_health,
        )
        assert snapshot_report["dataset"]["table_counts"]["runs"] == 10, snapshot_report
        assert snapshot_report["dataset"]["table_counts"]["execution_attempts"] == 5, snapshot_report

        gap_text = "need test-execution output to judge"
        snapshot_preview = build_report_from_snapshot(
            snapshot_path,
            window_days=90,
            min_gap_recurrence=3,
            route_table=route_table,
            hypotheses_path=hypotheses_path,
            keepalive_corpus_path=keepalive_corpus_path,
            redirect_corpus_path=redirect_corpus_path,
            langsmith_artifact_health=artifact_health,
        )
        assert snapshot_preview["evidence"]["proposals"], snapshot_preview["evidence"]
        preview = feedback.approve_evidence_type(
            "cli_preview_type",
            gap_text,
            min_recurrence=3,
            window_days=90,
            apply=False,
        )
        assert preview["eligible"] and not preview["applied"], preview
        preview_report = build_report(
            window_days=90,
            min_gap_recurrence=3,
            route_table=route_table,
            hypotheses_path=hypotheses_path,
            keepalive_corpus_path=keepalive_corpus_path,
            redirect_corpus_path=redirect_corpus_path,
            langsmith_artifact_health=artifact_health,
        )
        preview_report["evidence_approval"] = preview
        assert "evidence_approval (preview)" in format_human(preview_report)
        with feedback._conn() as c:
            still_open = c.execute(
                "SELECT COUNT(*) FROM evidence_gaps WHERE gap=? AND status='open'",
                (gap_text,),
            ).fetchone()[0]
        assert still_open == 3, still_open

        applied = feedback.approve_evidence_type(
            "cli_apply_type",
            gap_text,
            rationale="selftest apply",
            min_recurrence=3,
            window_days=90,
            apply=True,
        )
        assert applied["applied"] and applied["gaps_marked"] == 3, applied
        with feedback._conn() as c:
            approved = c.execute(
                "SELECT COUNT(*) FROM evidence_gaps WHERE gap=? AND status='approved'",
                (gap_text,),
            ).fetchone()[0]
            active = c.execute(
                "SELECT status FROM evidence_types WHERE name='cli_apply_type'"
            ).fetchone()[0]
        assert approved == 3 and active == "active", (approved, active)

        import contextlib
        import io

        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(
                io.StringIO()
            ):
                main(
                    [
                        "--snapshot-json",
                        str(snapshot_path),
                        "--approve-evidence-type",
                        "snapshot_blocked",
                        "--from-gap",
                        gap_text,
                        "--apply",
                    ]
                )
            raise AssertionError("expected --apply with --snapshot-json to fail")
        except SystemExit as exc:
            assert exc.code != 0, exc

        # --- mining coverage: an up/down signal cannot show a one-seat-of-six miner ---
        cov = mining_coverage(30)
        assert set(cov) >= {"agents", "minable_agents", "working_agents", "coverage", "blocked"}, cov
        # Every registered agent appears, so a seat can never be silently uncovered.
        for agent in {p["agent"] for p in execution_profiles.PROFILE_REGISTRY.values()}:
            assert agent in cov["agents"], (agent, sorted(cov["agents"]))
        # Every seat's model is now named from the seat's own authority, so NONE may be reported as
        # an unidentifiable routing tag. If a seat regresses to a tag this fails, because that would
        # silently reclassify "needs tracing" as "can never be identified" -- the exact conflation
        # that made a one-seat miner look like a fleet-wide impossibility.
        for agent in ("codex", "claude", "gemini", "cursor", "vibe", "aider"):
            row = cov["agents"][agent]
            assert row["model_reportable"] is True, (agent, row)
            assert row["verdict"] != "model_not_reportable", (agent, row)
        # The headline must state a FRACTION. "it ran" is what hid a dead miner for 43 days.
        assert " of " in cov["coverage"] and "minable" in cov["coverage"], cov["coverage"]
        assert len(cov["minable_agents"]) <= len(cov["working_agents"]), cov
        # A blocked seat must always carry a reason -- a blocked entry with no verdict is silence.
        assert all(bool(reason) for reason in (cov["blocked"] or {}).values()), cov["blocked"]
        # CONSTRUCTED, not sampled. This block reads whatever the ambient DB holds, which on a
        # fresh machine is nothing -- so asserting over it passed even with the fix removed, the
        # vacuous shape this repo keeps re-growing. Build the exact state instead.
        import tempfile as _tf
        import time as _t

        _probe = feedback.DB_PATH
        try:
            _tmpdir = _tf.mkdtemp(prefix="orch-cov-verdict-")
            feedback.DB_PATH = Path(_tmpdir) / "coverage-verdicts.db"
            feedback.record_run("cov-unres", "offload:/tmp/x", "offload", "cursor")
            feedback.record_execution_attempt(
                "cov-unres", attempt_id="attempt:profile:cov-unres", operation_role="worker",
                profile_id="cursor-composer-2.5", requested_provider="cursor",
                requested_model="composer-2.5", status="unresolved",
                source="orchestrator-profile-decision", started_ts=int(_t.time()),
            )
            built = mining_coverage(30)["agents"]["cursor"]
            # 22 worker attempts once reported as `no_worker_attempt`, sending the reader after a
            # dispatch bug when the real answer is the seat's CLI keeps no log to read a model from.
            # A wrong verdict is worse than a missing one: confidently actionable, in the wrong
            # direction.
            assert built["worker_attempts"] == 1, built
            assert built["resolved_worker_attempts"] == 0, built
            assert built["verdict"] == "attempts_unresolved", built
        finally:
            feedback.DB_PATH = _probe

        # A VERDICT MUST NOT CONTRADICT ITS OWN COUNTS. `no_worker_attempt` was reported for a seat
        # holding 22 worker attempts, which sends the reader hunting a dispatch bug when the real
        # answer is that the seat's CLI leaves no log to read a model from. A wrong verdict is worse
        # than a missing one -- it is confidently actionable in the wrong direction.
        for agent, row in cov["agents"].items():
            if row["verdict"] == "no_worker_attempt":
                assert row["worker_attempts"] == 0, (agent, row)
            if row["verdict"] == "attempts_unresolved":
                assert row["worker_attempts"] > 0 and row["resolved_worker_attempts"] == 0, (agent, row)
            if row["verdict"] == "minable":
                assert row["resolved_worker_attempts"] > 0, (agent, row)

        print("periodic_report.py selftest: OK (incl. mining coverage)")
    finally:
        feedback.DB_PATH = old_db_path
        shutil.rmtree(temp_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only periodic feedback dataset report."
    )
    parser.add_argument("--window-days", type=_positive_int, default=90)
    parser.add_argument("--min-gap-recurrence", type=_positive_int, default=3)
    parser.add_argument(
        "--json", action="store_true", help="print JSON instead of human text"
    )
    parser.add_argument(
        "--snapshot-json", type=Path, help="read feedback.snapshot_json() output"
    )
    parser.add_argument(
        "--no-langsmith-artifact-probe",
        action="store_true",
        help="skip the live read-only GitHub artifact distribution probe",
    )
    parser.add_argument("--selftest", action="store_true", help="run offline selftest")
    parser.add_argument(
        "--approve-evidence-type",
        metavar="NAME",
        help="preview or apply approval of a proposed evidence type",
    )
    parser.add_argument(
        "--from-gap",
        metavar="GAP",
        help="exact evidence gap text that motivated the proposed type",
    )
    parser.add_argument(
        "--rationale", default="", help="rationale stored on the evidence type"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mutate the live DB when approving an evidence type; default is preview",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    if args.approve_evidence_type and not args.from_gap:
        parser.error("--approve-evidence-type requires --from-gap")
    if args.apply and args.snapshot_json:
        parser.error(
            "--apply cannot be used with --snapshot-json; snapshot mode is read-only"
        )

    if args.snapshot_json:
        snapshot_path = args.snapshot_json.resolve()
        report = build_report_from_snapshot(
            snapshot_path,
            window_days=args.window_days,
            min_gap_recurrence=args.min_gap_recurrence,
            probe_langsmith_artifacts=False,
        )
    else:
        report = build_report(
            window_days=args.window_days,
            min_gap_recurrence=args.min_gap_recurrence,
            probe_langsmith_artifacts=not args.no_langsmith_artifact_probe,
        )
    if args.approve_evidence_type:
        if args.snapshot_json:
            original_db = feedback.DB_PATH
            temp_db = _load_snapshot_into_temp_db(args.snapshot_json.resolve())
            try:
                feedback.DB_PATH = temp_db
                report["evidence_approval"] = feedback.approve_evidence_type(
                    args.approve_evidence_type,
                    args.from_gap,
                    rationale=args.rationale,
                    min_recurrence=args.min_gap_recurrence,
                    window_days=args.window_days,
                    apply=False,
                )
            finally:
                feedback.DB_PATH = original_db
                temp_db.unlink(missing_ok=True)
        else:
            try:
                report["evidence_approval"] = feedback.approve_evidence_type(
                    args.approve_evidence_type,
                    args.from_gap,
                    rationale=args.rationale,
                    min_recurrence=args.min_gap_recurrence,
                    window_days=args.window_days,
                    apply=args.apply,
                )
            except ValueError as exc:
                parser.error(str(exc))
        report["read_only"] = not report["evidence_approval"].get("applied", False)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        text = format_human(report)
        if args.snapshot_json:
            text += f"\nsource: snapshot {report['snapshot_path']}"
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
