#!/usr/bin/env python3
"""Read-only monitor for structured runtime-AC gate flow and follow-through.

Live firing and the alert denominator come exclusively from gate observations
written by ``runtime_ac_gate.gate_status`` into ``feedback.completion_events``.
The old cron-log scan is retained only as unattributed archival context; it can
never create a live alert or attach a spec path to a target.
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
from typing import Any


DEFAULT_DB = Path.home() / ".codex/orchestrator/feedback/orchestrator.db"
DEFAULT_CRON_LOG = Path.home() / ".codex/handoff/orchestrator-cron.log"
DEFAULT_REPORT = Path.home() / ".codex/orchestrator/runtime-ac-flow-monitor.json"

PRODUCTION_RUNTIME_AC = (
    "r.task_type = 'runtime_ac' "
    "and r.run_id not like 'backfill-%' "
    "and r.target not like '%[exp %'"
)
EXPERIMENT_RUNTIME_AC = (
    "r.task_type = 'runtime_ac' "
    "and (r.run_id like 'backfill-%' or r.target like '%[exp %')"
)
# Retained as a diagnostic only. It is explicitly not the live-flow denominator.
CLOSER_PROXY = (
    "r.target like '%#%' "
    "and coalesce(r.task_type, '') != 'role:redirect' "
    "and (r.source = 'keepalive' or r.mode = 'remote')"
)
ACTIVE_REQUIRED_STATUSES = {"required", "missing_spec", "executed", "skipped", "error"}


def _coerce_int(value: Any) -> int:
    return int(value or 0)


def _query_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _query_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    return [{key: row[key] for key in row.keys()} for row in cur.fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _stats(conn: sqlite3.Connection, where_clause: str, cutoff_ts: int) -> dict[str, int]:
    if not _table_exists(conn, "runs") or not _table_exists(conn, "outcomes"):
        return {
            "total": 0, "in_window": 0, "outcomes": 0, "merged": 0,
            "pending": 0, "durable": 0, "durability_failures": 0,
        }
    row = _query_one(
        conn,
        f"""
        select count(*) total,
          sum(case when r.ts >= ? then 1 else 0 end) in_window,
          sum(case when o.run_id is not null then 1 else 0 end) outcomes,
          sum(case when o.merged = 1 then 1 else 0 end) merged,
          sum(case when o.durability = 'pending' then 1 else 0 end) pending,
          sum(case when o.durability = 'durable' then 1 else 0 end) durable,
          sum(case when o.durability in ('reverted','reworked','reopened','broke_later') then 1 else 0 end) durability_failures
        from runs r left join outcomes o on o.run_id=r.run_id
        where {where_clause}
        """,
        (cutoff_ts,),
    )
    return {key: _coerce_int(value) for key, value in row.items()}


def _sample_rows(
    conn: sqlite3.Connection, where_clause: str, cutoff_ts: int, limit: int
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "runs") or not _table_exists(conn, "outcomes"):
        return []
    return _query_all(
        conn,
        f"""
        select r.run_id,r.target,r.agent,r.mode,r.source,r.ts,
          o.adjudicated_verdict,o.merged,o.durability,
          substr(coalesce(o.notes,''),1,140) notes
        from runs r left join outcomes o on o.run_id=r.run_id
        where {where_clause} and r.ts>=?
        order by r.ts desc limit ?
        """,
        (cutoff_ts, limit),
    )


def _target_slug(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", target).strip("_")


def _event_path_matches_target(event: dict[str, Any]) -> bool:
    path_value = str(event.get("spec_path") or "")
    target = str(event.get("target") or "")
    if not path_value or not target:
        return event.get("gate_status") in {"missing_spec", "skipped", "materialization_failed"}
    expected = Path(path_value).parent / f"{_target_slug(target)}.json"
    return Path(path_value) == expected and event.get("spec_path_matches_target") is not False


def _load_gate_events(
    conn: sqlite3.Connection, cutoff_ts: int, sample_limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not _table_exists(conn, "completion_events"):
        return [], {
            "available": False,
            "reason": "completion_events table is unavailable; run the feedback migration",
        }
    rows = _query_all(
        conn,
        "SELECT event_id,run_id,status,validation_status,payload_json,created_ts,updated_ts "
        "FROM completion_events WHERE producer='runtime_ac_gate' AND updated_ts>=? "
        "ORDER BY updated_ts DESC,event_id DESC LIMIT ?",
        (cutoff_ts, max(1000, sample_limit * 50)),
    )
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            gate = payload.get("runtime_ac_gate") or {}
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(gate, dict):
            continue
        event = {
            "event_id": row["event_id"],
            "event_run_id": row["run_id"],
            "completion_status": row["status"],
            "validation_status": row["validation_status"],
            "created_ts": row["created_ts"],
            "updated_ts": row["updated_ts"],
            **gate,
        }
        closer_run_id = event.get("closer_run_id")
        if closer_run_id and _table_exists(conn, "outcomes"):
            outcome = conn.execute(
                "SELECT COALESCE(adjudicated_verdict,verifier_verdict),merged,durability "
                "FROM outcomes WHERE run_id=?",
                (closer_run_id,),
            ).fetchone()
            if outcome:
                event.update(
                    {
                        "downstream_verdict": outcome[0],
                        "downstream_merged": bool(outcome[1]) if outcome[1] is not None else None,
                        "downstream_durability": outcome[2],
                    }
                )
        event["target_spec_attribution_valid"] = _event_path_matches_target(event)
        events.append(event)
    return events, {"available": True, "event_count": len(events)}


def _summarize_gate_events(events: list[dict[str, Any]], sample_limit: int) -> dict[str, Any]:
    active_required = [
        row for row in events
        if bool(row.get("required"))
        and not bool(row.get("dry_run"))
        and row.get("gate_status") in ACTIVE_REQUIRED_STATUSES
    ]
    executed = [row for row in active_required if row.get("gate_status") == "executed"]
    ineligible = [row for row in events if not bool(row.get("required"))]
    missing = [row for row in active_required if row.get("gate_status") == "missing_spec"]
    errors = [row for row in active_required if row.get("gate_status") == "error"]
    skipped = [row for row in active_required if row.get("gate_status") == "skipped"]
    attribution_mismatches = [row for row in events if not row["target_spec_attribution_valid"]]
    materialized = [row for row in events if row.get("gate_status") == "materialized"]
    materialization_failed = [
        row for row in events if row.get("gate_status") == "materialization_failed"
    ]
    verdict_counts = {"PASS": 0, "NEEDS_REVIEW": 0, "FAIL": 0, "UNKNOWN": 0}
    for row in executed:
        verdict = str(row.get("verifier_verdict") or "UNKNOWN").upper()
        verdict_counts[verdict if verdict in verdict_counts else "UNKNOWN"] += 1
    downstream = {
        "joined": sum(row.get("downstream_verdict") is not None for row in active_required),
        "merged": sum(row.get("downstream_merged") is True for row in active_required),
        "durable": sum(row.get("downstream_durability") == "durable" for row in active_required),
        "pending": sum(row.get("downstream_durability") == "pending" for row in active_required),
    }
    return {
        "event_count": len(events),
        "required_active_count": len(active_required),
        "executed_count": len(executed),
        "ineligible_count": len(ineligible),
        "missing_spec_count": len(missing),
        "error_count": len(errors),
        "execution_skipped_count": len(skipped),
        "materialized_count": len(materialized),
        "materialization_failed_count": len(materialization_failed),
        "attribution_mismatch_count": len(attribution_mismatches),
        "verdict_counts": verdict_counts,
        "downstream": downstream,
        "required_recent": active_required[:sample_limit],
        "ineligible_recent": ineligible[:sample_limit],
        "missing_spec_recent": missing[:sample_limit],
        "materialization_failed_recent": materialization_failed[:sample_limit],
        "attribution_mismatch_recent": attribution_mismatches[:sample_limit],
    }


def _scan_archival_cron_history(cron_log: Path) -> dict[str, Any]:
    """Return unattributed counts only; never use legacy text as live evidence."""
    if not cron_log.exists():
        return {
            "archival_only": True,
            "used_for_alerts": False,
            "used_for_attribution": False,
            "path": str(cron_log),
            "available": False,
            "status_counts": {},
        }
    text = cron_log.read_text(errors="replace")
    statuses: dict[str, int] = {}
    for status in re.findall(r'"status"\s*:\s*"([^"\n]+)"', text):
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "archival_only": True,
        "used_for_alerts": False,
        "used_for_attribution": False,
        "path": str(cron_log),
        "available": True,
        "runtime_ac_gate_blocks": text.count('"runtime_ac_gates"'),
        "status_counts": statuses,
    }


def build_report(
    db_path: Path,
    cron_log: Path,
    lookback_hours: int,
    min_closer_proxy: int,
    sample_limit: int,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Build live flow truth; ``min_closer_proxy`` is a compatibility alias.

    The threshold now applies to required active gate events. Closer proxy rows
    are reported for context only and can never trigger zero-flow.
    """
    now = int(now_ts if now_ts is not None else time.time())
    cutoff_ts = now - (lookback_hours * 3600)
    report: dict[str, Any] = {
        "status": "ok",
        "generated_ts": now,
        "lookback_hours": lookback_hours,
        "cutoff_ts": cutoff_ts,
        "db_path": str(db_path),
        "alerts": [],
        "actions": [],
    }
    if not db_path.exists():
        report.update(
            {
                "status": "blocked",
                "reason": "feedback DB not found",
                "actions": [f"Locate or create the authoritative feedback DB at {db_path}."],
            }
        )
        return report

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        events, event_plane = _load_gate_events(conn, cutoff_ts, sample_limit)
        gate_summary = _summarize_gate_events(events, sample_limit)
        production = _stats(conn, PRODUCTION_RUNTIME_AC, cutoff_ts)
        experiments = _stats(conn, EXPERIMENT_RUNTIME_AC, cutoff_ts)
        closer_proxy = _stats(conn, CLOSER_PROXY, cutoff_ts)
        production_recent = _sample_rows(conn, PRODUCTION_RUNTIME_AC, cutoff_ts, sample_limit)
        closer_recent = _sample_rows(conn, CLOSER_PROXY, cutoff_ts, sample_limit)
    finally:
        conn.close()

    threshold = max(1, int(min_closer_proxy))
    eligible_required_count = gate_summary["required_active_count"]
    executed_count = gate_summary["executed_count"]
    runtime_ac_live_firing = executed_count > 0
    eligible_event_present = eligible_required_count >= threshold
    zero_flow_alert = eligible_event_present and not runtime_ac_live_firing
    missing_spec_alert = gate_summary["missing_spec_count"] > 0
    attribution_alert = gate_summary["attribution_mismatch_count"] > 0
    materialization_alert = gate_summary["materialization_failed_count"] > 0
    experiment_rows_without_outcomes = max(0, experiments["total"] - experiments["outcomes"])

    if not event_plane.get("available"):
        report["alerts"].append(
            {
                "key": "runtime_ac_event_plane_unavailable",
                "message": event_plane.get("reason"),
            }
        )
        report["actions"].append("Run the additive feedback migration before evaluating runtime-AC flow.")
    if zero_flow_alert:
        report["alerts"].append(
            {
                "key": "runtime_ac_zero_live_fire",
                "message": (
                    "Required active runtime-AC gate events exist in the lookback window, "
                    "but none executed."
                ),
            }
        )
        report["actions"].append(
            "Resolve the exact missing-spec, disabled-execution, or gate-error reasons on required closers."
        )
    if missing_spec_alert:
        report["alerts"].append(
            {
                "key": "runtime_ac_missing_spec",
                "message": "Structured required gate events contain missing_spec states.",
            }
        )
        targets = ", ".join(
            str(row.get("target")) for row in gate_summary["missing_spec_recent"][:3]
        )
        report["actions"].append(f"Materialize runtime-AC specs at the recorded exact paths: {targets}.")
    if attribution_alert:
        report["alerts"].append(
            {
                "key": "runtime_ac_target_spec_mismatch",
                "message": "A structured event has target/spec attribution that does not match its canonical path.",
            }
        )
        report["actions"].append("Quarantine mismatched target/spec events; do not count or execute them.")
    if materialization_alert:
        report["alerts"].append(
            {
                "key": "runtime_ac_materialization_failed",
                "message": "A range-lane runtime-AC artifact reached a terminal non-installed state.",
            }
        )
        report["actions"].append("Inspect the recorded materialization terminal_reason and regenerate the exact-target spec.")
    if gate_summary["verdict_counts"]["FAIL"] or gate_summary["verdict_counts"]["NEEDS_REVIEW"]:
        report["alerts"].append(
            {
                "key": "runtime_ac_non_pass_verdict",
                "message": "Executed runtime-AC gates include blocking FAIL or NEEDS_REVIEW verdicts.",
            }
        )
    if gate_summary["downstream"]["pending"]:
        report["actions"].append("Run the durability sweep after the grace window for joined runtime-AC outcomes.")
    if experiment_rows_without_outcomes:
        report["actions"].append(
            "Evaluate experiment runtime-AC rows through the experiment path; they are not live gate firing."
        )
    if not eligible_event_present:
        report["actions"].append(
            "No required active gate event met the lookback threshold; ineligible closer traffic is intentionally ignored."
        )
    if report["alerts"]:
        report["status"] = "attention"

    archival = _scan_archival_cron_history(cron_log)
    report.update(
        {
            "runtime_ac_live_firing": runtime_ac_live_firing,
            "eligible_event_present": eligible_event_present,
            "eligible_required_count": eligible_required_count,
            "required_event_denominator": eligible_required_count,
            "executed_gate_numerator": executed_count,
            "closer_proxy_present": closer_proxy["in_window"] >= threshold,
            "closer_proxy_is_diagnostic_only": True,
            "zero_flow_alert": zero_flow_alert,
            "missing_spec_alert": missing_spec_alert,
            "target_spec_attribution_alert": attribution_alert,
            "materialization_alert": materialization_alert,
            "structured_gate_events": gate_summary,
            "event_plane": event_plane,
            "gate_history": {
                "source": "completion_events",
                "required_active_count": eligible_required_count,
                "executed_count": executed_count,
                "missing_spec_events": gate_summary["missing_spec_count"],
            },
            "legacy_gate_history": archival,
            "production_runtime_ac": production,
            "experiment_runtime_ac": {
                **experiments,
                "rows_without_outcomes": experiment_rows_without_outcomes,
            },
            "closer_proxy": closer_proxy,
            "production_recent": production_recent,
            "closer_proxy_recent": closer_recent,
        }
    )
    return report


def _print_human(report: dict[str, Any]) -> None:
    print(f"runtime_ac_flow_monitor: {report['status']}")
    print(f"lookback_hours: {report['lookback_hours']}")
    print(f"runtime_ac_live_firing: {report.get('runtime_ac_live_firing')}")
    print(
        "required_gate_flow: "
        f"denominator={report.get('required_event_denominator', 0)} "
        f"executed={report.get('executed_gate_numerator', 0)}"
    )
    print(f"zero_flow_alert: {report.get('zero_flow_alert')}")
    print(f"missing_spec_alert: {report.get('missing_spec_alert')}")
    print(
        "closer_proxy_diagnostic: "
        f"present={report.get('closer_proxy_present')} used_as_denominator=false"
    )
    for alert in report.get("alerts") or []:
        print(f"- alert {alert['key']}: {alert['message']}")
    for action in report.get("actions") or []:
        print(f"- action: {action}")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table runs (
          run_id text primary key, ts integer, target text, task_type text,
          agent text, mode text, reasoning_level text, model text,
          decomposition text, rationale text, pr_number integer, experiment_id text,
          source text, assignment text, work_type text, role_name text, routing_metadata text
        );
        create table outcomes (
          run_id text primary key, verifier_verdict text, adjudicated_verdict text,
          merged integer, ci_status text, durability text default 'pending',
          durability_checked_ts integer, notes text, influenced_by_run_id text
        );
        create table completion_events (
          event_id text primary key, schema_version integer, run_id text, attempt_id text,
          event_type text, phase text, producer text, status text, validation_status text,
          payload_json text, content_hash text, redaction_count integer,
          created_ts integer, updated_ts integer
        );
        """
    )


def _insert_gate_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    now: int,
    target: str,
    required: bool,
    status: str,
    dry_run: bool = False,
    spec_path: str | None = None,
    verdict: str | None = None,
) -> None:
    path = spec_path or f"/tmp/{_target_slug(target)}.json"
    gate = {
        "schema_version": 1,
        "gate_event_id": event_id,
        "target": target,
        "required": required,
        "dry_run": dry_run,
        "eligibility_source": "label" if required else "none",
        "eligibility_refs": ["runtime-ac"] if required else [],
        "spec_path": path,
        "spec_hash": None,
        "spec_path_matches_target": Path(path).name == f"{_target_slug(target)}.json",
        "gate_status": status,
        "blocking": required and status != "executed",
        "closer_run_id": None,
        "verifier_run_id": "fixture" if status == "executed" else None,
        "verifier_verdict": verdict,
    }
    conn.execute(
        "INSERT INTO completion_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id, 1, f"gate:{event_id}", None, "verification", "verification",
            "runtime_ac_gate", "pass" if verdict == "PASS" else "fail",
            "accepted", json.dumps({"runtime_ac_gate": gate}), "hash", 0, now, now,
        ),
    )


def _run_selftest() -> None:
    now = 2_000_000
    with tempfile.TemporaryDirectory(prefix="runtime-ac-flow-") as tmp:
        root = Path(tmp)
        db_path = root / "orchestrator.db"
        log_path = root / "orchestrator-cron.log"
        conn = sqlite3.connect(db_path)
        try:
            _create_schema(conn)
            for index in range(100):
                _insert_gate_event(
                    conn,
                    event_id=f"ineligible-{index}",
                    now=now - index,
                    target=f"owner/repo#{index + 1}",
                    required=False,
                    status="skipped",
                )
            conn.commit()
        finally:
            conn.close()
        log_path.write_text(
            '{"runtime_ac_gates":[{"target":"wrong/repo#1","status":"missing_spec"}]}\n'
        )
        ineligible = build_report(db_path, log_path, 72, 1, 5, now_ts=now)
        assert ineligible["required_event_denominator"] == 0, ineligible
        assert ineligible["zero_flow_alert"] is False, (
            "ineligible closer traffic triggered zero-flow alert",
            ineligible,
        )
        assert ineligible["legacy_gate_history"]["used_for_alerts"] is False, ineligible
        assert ineligible["missing_spec_alert"] is False, ineligible

        conn = sqlite3.connect(db_path)
        try:
            _insert_gate_event(
                conn,
                event_id="required-missing",
                now=now,
                target="owner/repo#500",
                required=True,
                status="missing_spec",
            )
            conn.commit()
        finally:
            conn.close()
        missing = build_report(db_path, log_path, 72, 1, 5, now_ts=now)
        assert missing["required_event_denominator"] == 1, missing
        assert missing["zero_flow_alert"] is True, missing
        assert missing["missing_spec_alert"] is True, missing

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM completion_events WHERE event_id='required-missing'")
            _insert_gate_event(
                conn,
                event_id="required-pass",
                now=now,
                target="owner/repo#500",
                required=True,
                status="executed",
                verdict="PASS",
            )
            conn.commit()
        finally:
            conn.close()
        fired = build_report(db_path, log_path, 72, 1, 5, now_ts=now)
        assert fired["runtime_ac_live_firing"] is True, fired
        assert fired["zero_flow_alert"] is False, fired
        assert fired["structured_gate_events"]["verdict_counts"]["PASS"] == 1, fired

    print(
        "runtime_ac_flow_monitor.py selftest: OK "
        "(structured required denominator, ineligible exclusion, archival-only legacy history)"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="authoritative feedback DB path")
    parser.add_argument("--cron-log", default=str(DEFAULT_CRON_LOG), help="archival cron log path")
    parser.add_argument("--lookback-hours", type=int, default=72, help="live-flow lookback window")
    parser.add_argument(
        "--min-required-events",
        "--min-closer-proxy",
        dest="min_required_events",
        type=int,
        default=1,
        help="minimum required active gate events before zero-flow alert",
    )
    parser.add_argument("--sample-limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", nargs="?", const=str(DEFAULT_REPORT))
    parser.add_argument("--fail-on-alert", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.selftest:
        _run_selftest()
        return 0
    report = build_report(
        Path(os.path.expanduser(args.db)),
        Path(os.path.expanduser(args.cron_log)),
        args.lookback_hours,
        args.min_required_events,
        args.sample_limit,
    )
    if args.write_report:
        _write_report(Path(os.path.expanduser(args.write_report)), report)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _format_human(report))
    if args.fail_on_alert and report["status"] == "attention":
        return 2
    return 1 if report["status"] == "blocked" else 0


def _format_human(report: dict[str, Any]) -> str:
    lines = []
    # Reuse the tested printer without capturing stdout in normal code.
    lines.append(f"runtime_ac_flow_monitor: {report['status']}")
    lines.append(f"lookback_hours: {report['lookback_hours']}")
    lines.append(
        "required_gate_flow: "
        f"denominator={report.get('required_event_denominator', 0)} "
        f"executed={report.get('executed_gate_numerator', 0)}"
    )
    lines.append(f"runtime_ac_live_firing: {report.get('runtime_ac_live_firing')}")
    lines.append(f"zero_flow_alert: {report.get('zero_flow_alert')}")
    lines.extend(f"alert {row['key']}: {row['message']}" for row in report.get("alerts") or [])
    lines.extend(f"action: {row}" for row in report.get("actions") or [])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
