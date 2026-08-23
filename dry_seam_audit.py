#!/usr/bin/env python3
"""dry_seam_audit.py - flag Orchestrator sinks that look wired but not alive.

The failure mode this guards against is declaring a pipeline "done" because
code, tests, schedules, or docs exist while the durable sink has zero or stale
real rows. This module is read-only: it queries the Brain and reports empty
sinks, stale sinks, join gaps, and route cells that are still running on priors.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import feedback

TABLE_TS_COLUMNS = {
    "runs": "ts",
    "outcomes": "durability_checked_ts",
    "costs": "pulled_ts",
    "execution_traces": "pulled_ts",
    "completion_events": "updated_ts",
    "influence_edges": "created_ts",
    "route_weights": "ts",
    "evaluations": "ts",
    "human_calibration": "ts",
    "evidence_gaps": "ts",
    "evidence_types": "added_ts",
}
CORE_SINKS = {"runs", "outcomes", "costs", "execution_traces", "route_weights"}
GROWTH_SINKS = {"human_calibration", "evidence_gaps", "evidence_types"}


def _now() -> int:
    return int(time.time())


def _route_priors(route_table: dict[str, Any] | None = None) -> dict[str, list[str]]:
    if route_table is None:
        import router

        route_table = router.ROUTE_TABLE
    priors: dict[str, list[str]] = {}
    for task_type, spec in route_table.items():
        agents = []
        for row in spec.get("agents") or []:
            agent = row.get("agent") if isinstance(row, dict) else None
            if agent and agent not in agents:
                agents.append(agent)
        priors[task_type] = agents
    return priors


def _table_counts(c: sqlite3.Connection) -> dict[str, int]:
    return {
        table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLE_TS_COLUMNS
    }


def _recent_counts(c: sqlite3.Connection, since: int) -> dict[str, int]:
    recent = {}
    for table, ts_col in TABLE_TS_COLUMNS.items():
        recent[table] = c.execute(
            f"SELECT COUNT(*) FROM {table} WHERE COALESCE({ts_col},0)>=?",
            (since,),
        ).fetchone()[0]
    return recent


def _finding(
    findings: list[dict[str, Any]],
    *,
    sink: str,
    status: str,
    finding: str,
    evidence: dict[str, Any],
    recommendation: str,
) -> None:
    findings.append(
        {
            "sink": sink,
            "status": status,
            "finding": finding,
            "evidence": evidence,
            "recommendation": recommendation,
        }
    )


def _classify_outcome_gap(row: dict[str, Any]) -> tuple[str, str, bool, str]:
    mode = row.get("mode") or ""
    task_type = row.get("task_type") or ""
    source = row.get("source") or ""
    experiment_id = row.get("experiment_id") or ""
    run_id = row.get("run_id") or ""
    target = row.get("target") or ""
    if mode == "offload" or task_type == "offload":
        return (
            "offload_no_production_outcome",
            "Synchronous offload rows are execution/cost evidence; they only need an outcome if promoted into a downstream run.",
            False,
            "",
        )
    if mode == "role" or task_type.startswith("role:"):
        return (
            "role_shadow_needs_link_if_used",
            "Role-shadow rows need roles.py/redirect_shadow.py outcome linking only when the advice was accepted or applied.",
            False,
            "link accepted role advice to downstream outcomes when it influenced a run",
        )
    if experiment_id or "[exp " in target or ":eval:" in run_id:
        return (
            "experiment_evidence_not_production_outcome",
            "A/B experiment rows learn through evaluations; only synthesized/applied follow-up work gets production outcomes.",
            False,
            "",
        )
    if task_type in {"review", "ux_review", "synthesize"}:
        return (
            "advisory_no_production_outcome",
            "Advisory review/synthesis rows should be linked only if they influenced downstream work.",
            False,
            "link accepted advice to a downstream run when applicable",
        )
    if (
        mode in {"remote", "local"}
        or (task_type == "implement" and "#" in target and source == "orchestrator_local")
        or (mode in {"composer", "full"} and "#" in target and source == "orchestrator_local")
    ):
        return (
            "outcome_ingest_candidate",
            "Delegated implementation work should get an outcome row from PR/branch state.",
            True,
            "run outcomes.py --mode both (or --mode local/remote) for delegated runs, then inspect skipped_details for open PR waits vs missing branch/PR joins",
        )
    return (
        "unclassified_outcome_gap",
        "No outcome row exists and the run type is not recognized as advisory/offload/experiment.",
        True,
        "inspect the run and either record/link an outcome or mark the source as advisory",
    )


def outcome_gap_summary(
    *,
    window_days: int = 90,
    generated_at: int | None = None,
    example_limit: int = 5,
) -> dict[str, Any]:
    generated_at = generated_at or _now()
    since = generated_at - window_days * 86400
    with feedback._conn() as c:
        c.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in c.execute(
                "SELECT r.run_id, r.ts, r.target, COALESCE(r.task_type,'') task_type, "
                "COALESCE(r.agent,'') agent, COALESCE(r.mode,'') mode, "
                "COALESCE(r.source,'') source, COALESCE(r.assignment,'') assignment, "
                "COALESCE(r.experiment_id,'') experiment_id, r.pr_number, "
                "COALESCE(r.role_name,'') role_name "
                "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                "WHERE r.ts>=? AND o.run_id IS NULL ORDER BY r.ts DESC",
                (since,),
            ).fetchall()
        ]
    categories: dict[str, dict[str, Any]] = {}
    for row in rows:
        category, meaning, actionable, recommendation = _classify_outcome_gap(row)
        item = categories.setdefault(
            category,
            {
                "category": category,
                "count": 0,
                "actionable": actionable,
                "meaning": meaning,
                "recommendation": recommendation,
                "examples": [],
            },
        )
        item["count"] += 1
        if len(item["examples"]) < example_limit:
            item["examples"].append(
                {
                    "run_id": row["run_id"],
                    "target": row["target"],
                    "task_type": row["task_type"],
                    "agent": row["agent"],
                    "mode": row["mode"],
                    "source": row["source"],
                    "age_days": max(0, (generated_at - int(row["ts"] or generated_at)) // 86400),
                }
            )
    category_rows = sorted(categories.values(), key=lambda row: (-row["count"], row["category"]))
    actionable_count = sum(row["count"] for row in category_rows if row["actionable"])
    if not rows:
        recommendation = "No recent runs are missing outcome rows."
    elif actionable_count:
        recommendation = (
            "Run outcome ingest/linking for actionable categories; do not treat "
            "offload/advisory rows as production failures."
        )
    else:
        recommendation = (
            "No outcome-ingest action is currently needed; remaining rows are "
            "offload/advisory/experiment/role-shadow evidence and should only be "
            "linked if their advice or output influenced downstream production work."
        )
    return {
        "window_days": window_days,
        "total_runs_without_outcome": len(rows),
        "actionable_runs_without_outcome": actionable_count,
        "advisory_or_expected_unlinked": len(rows) - actionable_count,
        "categories": category_rows,
        "recommendation": recommendation,
    }


def audit_dry_seams(
    *,
    window_days: int = 90,
    stale_days: int = 14,
    route_table: dict[str, Any] | None = None,
    generated_at: int | None = None,
    capabilities_path: Path | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or _now()
    since = generated_at - window_days * 86400
    stale_since = generated_at - stale_days * 86400
    findings: list[dict[str, Any]] = []

    with feedback._conn() as c:
        counts = _table_counts(c)
        recent = _recent_counts(c, since)
        fresh = _recent_counts(c, stale_since)
        completion_health = feedback.completion_event_health(conn=c)

        for table in CORE_SINKS:
            if counts[table] == 0:
                _finding(
                    findings,
                    sink=table,
                    status="fail",
                    finding=f"{table} has zero rows",
                    evidence={"count": 0},
                    recommendation=(
                        "Treat the producer as not live until the sink contains a real row from a real upstream event."
                    ),
                )
            elif fresh[table] == 0:
                _finding(
                    findings,
                    sink=table,
                    status="warn",
                    finding=f"{table} has rows but none in the last {stale_days} day(s)",
                    evidence={"total": counts[table], f"recent_{stale_days}d": 0},
                    recommendation="Confirm the producer cadence still runs and writes current rows.",
                )

        for table in GROWTH_SINKS:
            if counts[table] == 0:
                if table == "human_calibration":
                    _finding(
                        findings,
                        sink=table,
                        status="info",
                        finding=f"{table} is data-gated and empty",
                        evidence={"count": 0},
                        recommendation=(
                            "Record real structured human score anchors with "
                            "human_calibration.py --record-anchor when human scores are "
                            "available; do not synthesize anchors just to fill the sink."
                        ),
                    )
                    continue
                _finding(
                    findings,
                    sink=table,
                    status="warn",
                    finding=f"{table} is wired but empty",
                    evidence={"count": 0},
                    recommendation=(
                        "If this feature is considered implemented, seed it from a real calibration/gap event or mark the flow not-yet-live."
                    ),
                )

        if counts["runs"] and completion_health["total"] == 0:
            _finding(
                findings,
                sink="completion_events",
                status="fail",
                finding="runs exist without completion-event envelopes",
                evidence=completion_health,
                recommendation="Repair the normal record_run/attempt/outcome event hooks before mining methods.",
            )
        if completion_health["rejected"]:
            _finding(
                findings,
                sink="completion_events",
                status="warn",
                finding="completion events were rejected by the safe-envelope validator",
                evidence=completion_health,
                recommendation="Inspect rejection_codes; update the producer to use IDs/hashes and the canonical nested fields.",
            )
        if completion_health["orphan_edges"]:
            _finding(
                findings,
                sink="influence_edges",
                status="fail",
                finding="influence edges reference missing source or target events",
                evidence=completion_health,
                recommendation="Repair dispatch-time influence stamping before using lineage for learning.",
            )
        if completion_health["accepted_influence_missing_outcome"]:
            _finding(
                findings,
                sink="influence_edges",
                status="warn",
                finding="accepted influences have no propagated outcome/durability yet",
                evidence=completion_health,
                recommendation="Run normal outcome/durability ingest; no manual role link command is required.",
            )

        costs_without_runs = c.execute(
            "SELECT COUNT(*) FROM costs co LEFT JOIN runs r ON co.run_id=r.run_id WHERE r.run_id IS NULL"
        ).fetchone()[0]
        if costs_without_runs:
            _finding(
                findings,
                sink="costs",
                status="fail",
                finding="cost rows exist without matching runs",
                evidence={"orphan_cost_rows": costs_without_runs},
                recommendation="Fix the run_id join before using costs in routing.",
            )

        traces_without_runs = c.execute(
            "SELECT COUNT(*) FROM execution_traces et LEFT JOIN runs r ON et.run_id=r.run_id WHERE r.run_id IS NULL"
        ).fetchone()[0]
        if traces_without_runs:
            _finding(
                findings,
                sink="execution_traces",
                status="fail",
                finding="trace rows exist without matching runs",
                evidence={"orphan_trace_rows": traces_without_runs},
                recommendation="Fix trace-to-run joining before treating trace coverage as live.",
            )

        outcome_gaps = outcome_gap_summary(window_days=window_days, generated_at=generated_at)
        if outcome_gaps["total_runs_without_outcome"]:
            status = "warn" if outcome_gaps["actionable_runs_without_outcome"] else "info"
            _finding(
                findings,
                sink="outcomes",
                status=status,
                finding="recent runs have no outcome rows",
                evidence={
                    "runs_without_outcome": outcome_gaps["total_runs_without_outcome"],
                    "actionable_runs_without_outcome": outcome_gaps[
                        "actionable_runs_without_outcome"
                    ],
                    "window_days": window_days,
                    "categories": [
                        {
                            "category": row["category"],
                            "count": row["count"],
                            "actionable": row["actionable"],
                        }
                        for row in outcome_gaps["categories"]
                    ],
                },
                recommendation=outcome_gaps["recommendation"],
            )

        langsmith_costs = c.execute(
            "SELECT COUNT(*) FROM costs WHERE source='langsmith'"
        ).fetchone()[0]
        if counts["runs"] and langsmith_costs == 0:
            _finding(
                findings,
                sink="costs",
                status="fail",
                finding="no LangSmith cost rows are joined to Brain runs",
                evidence={"runs": counts["runs"], "langsmith_cost_rows": 0},
                recommendation="Run langsmith_direct.py --ingest and verify non-zero costs(source=langsmith).",
            )

        trace_run_without_cost = c.execute(
            "SELECT COUNT(DISTINCT et.run_id) FROM execution_traces et "
            "LEFT JOIN costs co ON et.run_id=co.run_id "
            "WHERE et.source='langsmith' AND co.run_id IS NULL"
        ).fetchone()[0]
        if trace_run_without_cost:
            _finding(
                findings,
                sink="execution_traces",
                status="warn",
                finding="LangSmith traces exist for runs that have no cost row",
                evidence={"trace_runs_without_cost": trace_run_without_cost},
                recommendation="Confirm whether the trace rows carry zero-cost calls or the cost aggregation dropped them.",
            )

        latest_version = c.execute("SELECT COALESCE(MAX(version),0) FROM route_weights").fetchone()[
            0
        ]
        zero_obs_cells: list[dict[str, Any]] = []
        missing_cells: list[dict[str, str]] = []
        if latest_version:
            existing = {
                (task_type, agent): n_obs
                for task_type, agent, n_obs in c.execute(
                    "SELECT task_type, agent, n_obs FROM route_weights WHERE version=?",
                    (latest_version,),
                ).fetchall()
            }
            for task_type, agents in _route_priors(route_table).items():
                for agent in agents:
                    key = (task_type, agent)
                    if key not in existing:
                        missing_cells.append({"task_type": task_type, "agent": agent})
                    elif int(existing[key] or 0) == 0:
                        zero_obs_cells.append({"task_type": task_type, "agent": agent})
        elif counts["runs"]:
            _finding(
                findings,
                sink="route_weights",
                status="fail",
                finding="runs exist but no route_weights version has been written",
                evidence={"runs": counts["runs"]},
                recommendation="Run relearn_report.py after outcomes/costs are live.",
            )

        if zero_obs_cells or missing_cells:
            _finding(
                findings,
                sink="route_weights",
                status="warn",
                finding="routing cells are still prior-only",
                evidence={
                    "latest_version": latest_version,
                    "zero_observation_cells": len(zero_obs_cells),
                    "missing_cells": len(missing_cells),
                    "examples": [*zero_obs_cells[:8], *missing_cells[:4]],
                },
                recommendation="Schedule exploration/production runs for high-value zero-observation cells before trusting learned order.",
            )

    capability_lifecycle = {
        "path": str(capabilities_path) if capabilities_path else None,
        "total": 0,
        "counts_by_status": {},
        "classifications": {},
    }
    try:
        import capabilities

        cap_report = capabilities.summary(
            capabilities_path or capabilities.REG,
            create=False,
        )
        capability_lifecycle.update(
            {
                "path": cap_report.get("path"),
                "total": cap_report.get("total", 0),
                "counts_by_status": cap_report.get("counts_by_status", {}),
            }
        )
        classification_status = {
            "deliberately_gated": "info",
            "no_matching_work": "info",
            "wired_but_dry": "warn",
            "matched_not_invoked": "warn",
            "invoked_without_outcomes": "fail",
            "stale_active": "warn",
            "retired": "info",
            "superseded": "info",
        }
        for name, cap in cap_report.get("capabilities", {}).items():
            classification = capabilities.classify_liveness(
                cap,
                now=generated_at,
                stale_days=stale_days,
            )
            capability_lifecycle["classifications"][name] = classification
            severity = classification_status.get(classification)
            if severity:
                _finding(
                    findings,
                    sink="capabilities",
                    status=severity,
                    finding=f"{name}: {classification}",
                    evidence={
                        "state": cap.get("status"),
                        "last_match": cap.get("last_match"),
                        "last_invocation": cap.get("last_invocation"),
                        "last_success": cap.get("last_success"),
                        "outcome_links": list(cap.get("outcome_links") or []),
                        "gate_reason": cap.get("gate_reason"),
                    },
                    recommendation=(
                        "Allow the ledger's expiry/next-transition policy to proceed."
                        if classification
                        in {"deliberately_gated", "no_matching_work", "retired", "superseded"}
                        else "Repair the exact missing lifecycle edge before promotion."
                    ),
                )
    except Exception as exc:
        capability_lifecycle["error"] = str(exc)

    status_counts: dict[str, int] = {}
    for item in findings:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    overall = (
        "fail" if status_counts.get("fail") else "warn" if status_counts.get("warn") else "pass"
    )
    return {
        "generated_at": generated_at,
        "db_path": str(feedback.DB_PATH),
        "window_days": window_days,
        "stale_days": stale_days,
        "overall": overall,
        "status_counts": status_counts,
        "counts": counts,
        "recent_counts": recent,
        "outcome_gap_summary": outcome_gaps,
        "completion_event_health": completion_health,
        "capability_lifecycle": capability_lifecycle,
        "findings": findings,
    }


def format_human(report: dict[str, Any]) -> str:
    lines = [
        f"dry_seam_audit: overall={report['overall']} db={report['db_path']} "
        f"window_days={report['window_days']} stale_days={report['stale_days']}",
    ]
    counts = " ".join(f"{key}={value}" for key, value in report["counts"].items() if value)
    lines.append(f"counts: {counts or 'no rows'}")
    lineage = report.get("completion_event_health") or {}
    if lineage:
        lines.append(
            "completion events: "
            f"total={lineage.get('total', 0)} complete={lineage.get('complete', 0)} "
            f"redacted={lineage.get('redacted', 0)} rejected={lineage.get('rejected', 0)} "
            f"orphans={lineage.get('orphan_edges', 0)} "
            f"accepted_linked={lineage.get('accepted_influence_linked', 0)}"
        )
    if not report["findings"]:
        lines.append("findings: none")
        return "\n".join(lines)
    gaps = report.get("outcome_gap_summary") or {}
    if gaps.get("total_runs_without_outcome"):
        lines.append(
            "outcome gaps: "
            f"total={gaps.get('total_runs_without_outcome', 0)} "
            f"actionable={gaps.get('actionable_runs_without_outcome', 0)} "
            f"advisory_or_unlinked={gaps.get('advisory_or_expected_unlinked', 0)}"
        )
        for row in (gaps.get("categories") or [])[:5]:
            lines.append(
                f"  {row['category']}: count={row['count']} actionable={row['actionable']}"
            )
    lines.append("findings:")
    for item in report["findings"]:
        lines.append(f"- [{item['status']}] {item['sink']}: {item['finding']}")
        lines.append(f"  evidence: {json.dumps(item['evidence'], sort_keys=True)}")
        lines.append(f"  next: {item['recommendation']}")
    return "\n".join(lines)


def _selftest() -> None:
    import shutil

    old_db = feedback.DB_PATH
    temp_dir = Path(tempfile.mkdtemp(prefix="dry-seam-audit-selftest-"))
    feedback.DB_PATH = temp_dir / "orchestrator.db"
    isolated_capabilities = temp_dir / "missing-capabilities.json"
    try:
        empty = audit_dry_seams(
            route_table={"implement": {"agents": [{"agent": "codex"}]}},
            capabilities_path=isolated_capabilities,
        )
        assert empty["overall"] == "fail", empty
        assert any(f["sink"] == "runs" and f["status"] == "fail" for f in empty["findings"]), empty

        now = int(time.time())
        feedback.record_run("run-1", "owner/repo#1", "implement", "codex", ts=now)
        feedback.record_outcome("run-1", adjudicated_verdict="PASS", durability="durable")
        feedback.record_cost("run-1", tokens_in=10, tokens_out=5, cost_usd=0.01, source="langsmith")
        feedback.record_execution_trace(
            "run-1", trace_id="tr-1", status="success", source="langsmith"
        )
        feedback.record_evidence_gap("exp", "judge", "need live sink row")
        feedback.record_evidence_type("live_sink_row", "selftest")
        feedback.record_human_calibration("owner/repo#1", "pass", "selftest")
        with feedback._conn() as c:
            c.execute(
                "INSERT INTO route_weights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    now,
                    "implement",
                    "codex",
                    0.8,
                    0.85,
                    1,
                    1.0,
                    0.01,
                    0.85,
                    "selftest",
                    now - 10,
                    now,
                ),
            )
        live = audit_dry_seams(
            route_table={"implement": {"agents": [{"agent": "codex"}]}},
            capabilities_path=isolated_capabilities,
        )
        assert live["overall"] == "pass", live
        assert "overall=pass" in format_human(live)
        feedback.record_run(
            "offload-no-outcome",
            "local review",
            "offload",
            "cursor",
            mode="offload",
            source="orchestrator_local",
            ts=now,
        )
        advisory = audit_dry_seams(
            route_table={"implement": {"agents": [{"agent": "codex"}]}},
            capabilities_path=isolated_capabilities,
        )
        assert advisory["overall"] == "pass", advisory
        assert advisory["outcome_gap_summary"]["actionable_runs_without_outcome"] == 0, advisory
        assert (
            "No outcome-ingest action" in advisory["outcome_gap_summary"]["recommendation"]
        ), advisory
        feedback.record_run(
            "remote-missing-outcome",
            "owner/repo#2",
            "implement",
            "codex",
            mode="remote",
            source="orchestrator_remote",
            ts=now,
        )
        unresolved = audit_dry_seams(
            route_table={"implement": {"agents": [{"agent": "codex"}]}},
            capabilities_path=isolated_capabilities,
        )
        assert unresolved["overall"] == "warn", unresolved
        assert unresolved["outcome_gap_summary"]["actionable_runs_without_outcome"] == 1, unresolved
        text = format_human(unresolved)
        assert "outcome gaps:" in text and "outcome_ingest_candidate" in text, text

        import capabilities

        cap_path = temp_dir / "capabilities.json"
        unmatched = capabilities._blank_capability("unmatched")
        unmatched.update({"status": "wired", "entrypoint": "worker.py"})
        matched = capabilities._blank_capability("matched")
        matched.update({"status": "wired", "entrypoint": "worker.py", "last_match": now})
        capabilities.save({"unmatched": unmatched, "matched": matched}, cap_path)
        cap_audit = audit_dry_seams(
            route_table={"implement": {"agents": [{"agent": "codex"}]}},
            generated_at=now,
            capabilities_path=cap_path,
        )
        classifications = cap_audit["capability_lifecycle"]["classifications"]
        assert classifications["unmatched"] == "no_matching_work", classifications
        assert classifications["matched"] == "matched_not_invoked", classifications
        print("dry_seam_audit.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(temp_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for dry Orchestrator data seams.")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--stale-days", type=int, default=14)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    report = audit_dry_seams(window_days=args.window_days, stale_days=args.stale_days)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(format_human(report))
    return 0 if report["overall"] != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
