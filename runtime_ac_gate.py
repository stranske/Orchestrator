#!/usr/bin/env python3
"""Shared runtime-AC gate enforcement for closer PR paths.

This module owns the common requirement detection and active gate execution used
by both autonomous ticks and terminal merge guards. Specs live on local disk by
default so the Dropbox code checkout never holds runtime state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import feedback
import capabilities
import runtime_ac

RUNTIME_AC_REQUIRED_LABELS = {
    "runtime-ac",
    "runtime-verification",
    "acceptance-criteria",
    "verification-spec",
    "verification-plan",
    "ac-checks",
    "runtime-checks",
}
DEFAULT_RUNTIME_AC_SPEC_DIR = Path.home() / ".codex" / "orchestrator" / "runtime-ac"
DEFAULT_BACKLOG_JSON = (
    Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff")) / "backlog.json"
)
DEFAULT_CRON_LOG = (
    Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
    / "orchestrator-cron.log"
)


def target_slug(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", target).strip("_")


def spec_path(target: str, *, spec_dir: str | Path | None = None, env: dict | None = None) -> Path:
    source_env = os.environ if env is None else env
    root = Path(spec_dir or source_env.get("ORCH_RUNTIME_AC_SPEC_DIR", DEFAULT_RUNTIME_AC_SPEC_DIR))
    return root / f"{target_slug(target)}.json"


def _all_label_names(item: dict) -> list[str]:
    """The item's own labels PLUS its source issue's labels.

    The gate is closer-only and closer items carry PR labels; verification-relevant metadata lives
    on the source ISSUE. Verified 2026-08-20: 0 PRs in the fleet carry a `risk:*` label, and none
    carries a runtime-AC label either, so reading `labels` alone made the gate unreachable by
    anything except a pre-existing spec file. `backlog.build_backlog` attaches `source_labels`.
    """
    out = []
    for key in ("labels", "source_labels"):
        for label in item.get(key) or []:
            out.append(str(label.get("name") if isinstance(label, dict) else label).strip())
    return out


def required(item: dict, path: Path) -> bool:
    labels = {label.lower() for label in _all_label_names(item)}
    normalized = labels | {label.split(":", 1)[1].strip() for label in labels if ":" in label}
    return bool(normalized & RUNTIME_AC_REQUIRED_LABELS) or path.exists()


def eligibility(item: dict, path: Path) -> dict:
    """Return exact, target-local evidence that makes a closer gate eligible."""
    raw_labels = _all_label_names(item)
    matched_labels = []
    for raw in raw_labels:
        lowered = raw.lower()
        normalized = lowered.split(":", 1)[1].strip() if ":" in lowered else lowered
        if normalized in RUNTIME_AC_REQUIRED_LABELS:
            matched_labels.append(raw)
    by_label = bool(matched_labels)
    by_spec = path.exists()
    source = (
        "label+spec"
        if by_label and by_spec
        else "label" if by_label else "spec" if by_spec else "none"
    )
    refs = list(matched_labels)
    if by_spec:
        refs.append(str(path))
    return {
        "required": by_label or by_spec,
        "source": source,
        "refs": refs,
    }


def spec_sha256(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _latest_closer_run(target: str, latest_run_fn=None) -> tuple[str | None, str | None]:
    lookup = latest_run_fn or feedback.latest_run_id_for_target
    try:
        return lookup(target, mode="remote"), None
    except Exception as exc:
        return None, str(exc)


def _record_gate_event(
    *,
    target: str,
    status: str,
    required_gate: bool,
    dry_run: bool,
    eligibility_info: dict,
    path: Path,
    blocks: bool,
    closer_run_id: str | None,
    terminal_reason: str | None = None,
    verifier_run_id: str | None = None,
    verifier_verdict: str | None = None,
    materialization_source: str | None = None,
    materialization_status: str | None = None,
    materialization_run_id: str | None = None,
) -> dict:
    """Write gate telemetry fail-open so observability never changes gate safety."""
    try:
        return feedback.record_runtime_ac_gate_event(
            target=target,
            gate_status=status,
            required=required_gate,
            dry_run=dry_run,
            eligibility_source=eligibility_info.get("source") or "none",
            eligibility_refs=eligibility_info.get("refs") or [],
            spec_path=str(path),
            spec_hash=spec_sha256(path),
            spec_path_matches_target=path == spec_path(target, spec_dir=path.parent),
            blocking=blocks,
            terminal_reason=terminal_reason,
            closer_run_id=closer_run_id,
            verifier_run_id=verifier_run_id,
            verifier_verdict=verifier_verdict,
            materialization_source=materialization_source,
            materialization_status=materialization_status,
            materialization_run_id=materialization_run_id,
        )
    except Exception as exc:
        return {"recorded": False, "error": str(exc)}


def env_flag(env: dict, name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def env_int(env: dict, name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return default


def gate_status(
    item: dict,
    *,
    dry_run: bool,
    env: dict | None = None,
    spec_dir: str | Path | None = None,
    run_fn=None,
    latest_run_fn=None,
    record_fn=None,
) -> dict | None:
    """Return and persist runtime-AC gate truth for closer PRs.

    The gate remains a hard opt-in safety gate: a required active closer blocks
    on a missing spec, disabled execution, errors, or any verdict except PASS.
    """
    if item.get("lane") != "closer":
        return None
    env = os.environ if env is None else env
    target = str(item.get("target") or "")
    if not target:
        return None
    path = spec_path(target, spec_dir=spec_dir, env=env)
    eligibility_info = eligibility(item, path)
    closer_run_id, closer_lookup_error = _latest_closer_run(target, latest_run_fn)
    if not eligibility_info["required"]:
        _record_gate_event(
            target=target,
            status="skipped",
            required_gate=False,
            dry_run=dry_run,
            eligibility_info=eligibility_info,
            path=path,
            blocks=False,
            closer_run_id=closer_run_id,
            terminal_reason="closer_not_runtime_ac_eligible",
        )
        return None

    base = {
        "target": target,
        "spec_path": str(path),
        "spec_hash": spec_sha256(path),
        "required": True,
        "eligibility_source": eligibility_info["source"],
        "eligibility_refs": eligibility_info["refs"],
        "closer_run_id": closer_run_id,
    }
    if closer_lookup_error:
        base["closer_run_lookup_error"] = closer_lookup_error
    capabilities.production_heartbeat(
        "runtime-ac-checks",
        "match",
        ref=target,
        metadata={
            "spec_path": str(path),
            "spec_hash": base["spec_hash"],
            "eligibility_source": eligibility_info["source"],
        },
    )
    if dry_run:
        if path.exists():
            result = {**base, "status": "planned", "blocks": False}
            result["gate_event"] = _record_gate_event(
                target=target,
                status="planned",
                required_gate=True,
                dry_run=True,
                eligibility_info=eligibility_info,
                path=path,
                blocks=False,
                closer_run_id=closer_run_id,
                terminal_reason="dry_run_plan_only",
            )
            return result
        result = {
            **base,
            "status": "missing_spec",
            "blocks": False,
            "detail": "runtime AC is required, but no verification spec exists yet",
        }
        result["gate_event"] = _record_gate_event(
            target=target,
            status="missing_spec",
            required_gate=True,
            dry_run=True,
            eligibility_info=eligibility_info,
            path=path,
            blocks=False,
            closer_run_id=closer_run_id,
            terminal_reason="required_spec_not_materialized",
        )
        return result

    if not path.exists():
        result = {
            **base,
            "status": "missing_spec",
            "blocks": True,
            "detail": "runtime AC is required before this closer can proceed",
        }
        result["gate_event"] = _record_gate_event(
            target=target,
            status="missing_spec",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason="required_spec_not_materialized",
        )
        return result
    if not env_flag(env, "ORCH_RUN_RUNTIME_AC"):
        result = {
            **base,
            "status": "required_but_not_run",
            "blocks": True,
            "detail": "set ORCH_RUN_RUNTIME_AC=1 to execute the runtime AC gate for required closer PRs",
        }
        result["gate_event"] = _record_gate_event(
            target=target,
            status="required",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason="runtime_ac_execution_disabled",
        )
        return result

    run_fn = run_fn or runtime_ac.run_verification
    record_fn = record_fn or runtime_ac.record_gate_verdict
    try:
        spec = runtime_ac.parse_spec_json(path.read_text(encoding="utf-8"))
        declared_target = str((spec.get("verification") or {}).get("target") or "").strip()
        if declared_target and declared_target != target:
            raise ValueError(
                f"runtime AC spec target {declared_target!r} does not match closer target {target!r}"
            )
        capabilities.production_heartbeat(
            "runtime-ac-checks",
            "invocation",
            ref=target,
            metadata={"spec_path": str(path), "spec_hash": spec_sha256(path)},
        )
        run = run_fn(
            spec,
            confirm_run=True,
            allow_command_checks=env_flag(env, "ORCH_RUNTIME_AC_ALLOW_COMMANDS"),
            timeout=env_int(env, "ORCH_RUNTIME_AC_TIMEOUT", 120),
        )
        gate = run["gate"]
        verdict = gate.get("verdict")
        result = {
            **base,
            "status": "executed",
            "blocks": verdict != "PASS",
            "verdict": verdict,
            "verifier_verdict": gate.get("verifier_verdict"),
            "pass_ratio": gate.get("pass_ratio"),
            "result_count": gate.get("result_count"),
            "blocking": gate.get("blocking") or [],
            "needs_review": gate.get("needs_review") or [],
            "run_id": closer_run_id,
            "verifier_run_id": gate.get("verification_id"),
        }
        run_id = closer_run_id
        try:
            if run_id:
                result["feedback"] = record_fn(run_id, gate)
            else:
                result["feedback"] = {
                    "recorded": False,
                    "reason": "no remote run_id found for target",
                }
        except Exception as feedback_exc:
            result["feedback"] = {"recorded": False, "error": str(feedback_exc)}
        feedback_result = result.get("feedback")
        feedback_recorded = bool(run_id) and not (
            isinstance(feedback_result, dict) and feedback_result.get("recorded") is False
        )
        if verdict == "PASS" and feedback_recorded:
            capabilities.production_heartbeat(
                "runtime-ac-checks",
                "success",
                ref=run_id,
                metadata={"spec_path": str(path), "verdict": verdict},
            )
            capabilities.production_heartbeat(
                "runtime-ac-checks",
                "outcome",
                ref=run_id,
                metadata={"sink": "feedback.outcomes"},
            )
        result["gate_event"] = _record_gate_event(
            target=target,
            status="executed",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=verdict != "PASS",
            closer_run_id=run_id,
            verifier_run_id=gate.get("verification_id"),
            verifier_verdict=verdict,
            terminal_reason=(
                None if verdict == "PASS" else f"verifier_{str(verdict or 'unknown').lower()}"
            ),
        )
        return result
    except Exception as exc:
        result = {**base, "status": "failed", "blocks": True, "error": str(exc)}
        result["gate_event"] = _record_gate_event(
            target=target,
            status="error",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason="gate_execution_error",
        )
        return result


def materialize_range_spec(
    target: str,
    source: dict | str | Path,
    *,
    spec_dir: str | Path | None = None,
    producer_run_id: str | None = None,
) -> dict:
    """Validate and atomically install a range-lane spec at the gate's exact path.

    Target/repository attribution is fail-closed.  A Workflows spec can never be
    installed for a Pension target, even when the JSON itself is otherwise valid.
    Every terminal result is retained in the structured gate-event plane.
    """
    target = str(target or "").strip()
    path = spec_path(target, spec_dir=spec_dir)
    closer_run_id, lookup_error = _latest_closer_run(target)
    materialization_source = "inline"
    try:
        if not target or "#" not in target:
            raise ValueError("materialization target must be an exact owner/repo#number reference")
        if isinstance(source, dict):
            spec = json.loads(json.dumps(source))
        else:
            source_path = Path(source)
            materialization_source = str(source_path)
            spec = runtime_ac.parse_spec_json(source_path.read_text(encoding="utf-8"))

        verification = spec.get("verification") or {}
        declared_target = str(verification.get("target") or "").strip()
        declared_repo = str(verification.get("repo") or "").strip()
        target_repo = target.split("#", 1)[0]
        if declared_target != target:
            raise ValueError(
                f"spec_target_mismatch: expected {target!r}, found {declared_target or '<missing>'!r}"
            )
        if declared_repo and declared_repo != target_repo:
            raise ValueError(
                f"spec_repo_mismatch: expected {target_repo!r}, found {declared_repo!r}"
            )
        errors = runtime_ac.validate_spec(spec)
        if errors:
            raise ValueError("invalid_runtime_ac_spec: " + "; ".join(errors))

        encoded = json.dumps(spec, indent=2, sort_keys=True) + "\n"
        expected_hash = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        installed_hash = spec_sha256(path)
        if installed_hash != expected_hash:
            raise RuntimeError("installed_spec_hash_mismatch")

        eligibility_info = {"source": "spec", "refs": [str(path)]}
        event = _record_gate_event(
            target=target,
            status="materialized",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=False,
            closer_run_id=closer_run_id,
            terminal_reason=None,
            materialization_source=materialization_source,
            materialization_status="installed",
            materialization_run_id=producer_run_id,
        )
        return {
            "status": "materialized",
            "target": target,
            "spec_path": str(path),
            "spec_hash": installed_hash,
            "closer_run_id": closer_run_id,
            "closer_run_lookup_error": lookup_error,
            "producer_run_id": producer_run_id,
            "gate_event": event,
        }
    except Exception as exc:
        reason = str(exc)
        event = _record_gate_event(
            target=target or "invalid-target",
            status="materialization_failed",
            required_gate=True,
            dry_run=False,
            eligibility_info={"source": "none", "refs": []},
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason=reason,
            materialization_source=materialization_source,
            materialization_status="not_installed",
            materialization_run_id=producer_run_id,
        )
        return {
            "status": "materialization_failed",
            "target": target,
            "spec_path": str(path),
            "terminal_reason": reason,
            "producer_run_id": producer_run_id,
            "gate_event": event,
        }


def _active_preflight(status: dict, env: dict) -> dict:
    if status.get("status") == "missing_spec":
        return {
            "status": "missing_spec",
            "blocks_active": True,
            "next_action": "create a runtime AC spec at the reported spec_path",
        }
    if status.get("status") == "planned" and not env_flag(env, "ORCH_RUN_RUNTIME_AC"):
        return {
            "status": "requires_env",
            "blocks_active": True,
            "next_action": "set ORCH_RUN_RUNTIME_AC=1 before active closer/merge execution",
        }
    if status.get("status") == "planned":
        return {
            "status": "ready_to_execute",
            "blocks_active": False,
            "next_action": "active execution would run the runtime AC spec",
        }
    return {
        "status": status.get("status") or "unknown",
        "blocks_active": bool(status.get("blocks")),
        "next_action": status.get("detail") or status.get("error") or "",
    }


def scan_items(
    items: list[dict],
    *,
    env: dict | None = None,
    spec_dir: str | Path | None = None,
) -> dict:
    """Read-only scan of backlog/closer items for runtime-AC gate coverage."""
    env = os.environ if env is None else env
    required_rows: list[dict] = []
    status_counts: dict[str, int] = {}
    active_blockers = 0
    for item in items:
        status = gate_status(item, dry_run=True, env=env, spec_dir=spec_dir)
        if not status:
            continue
        preflight = _active_preflight(status, env)
        if preflight.get("blocks_active"):
            active_blockers += 1
        status_counts[status["status"]] = status_counts.get(status["status"], 0) + 1
        required_rows.append(
            {
                "target": item.get("target"),
                "task_type": item.get("task_type"),
                "lane": item.get("lane"),
                "labels": item.get("labels") or [],
                "status": status,
                "active_preflight": preflight,
            }
        )
    if not required_rows:
        recommendation = "No current backlog closer item requires runtime AC."
    elif active_blockers:
        recommendation = "Resolve missing specs or active-run environment before expecting runtime AC gates to fire."
    else:
        recommendation = (
            "Runtime AC-required backlog items are ready for active gate execution when selected."
        )
    return {
        "read_only": True,
        "items_checked": len(items),
        "required_count": len(required_rows),
        "status_counts": status_counts,
        "active_blocker_count": active_blockers,
        "env": {
            "ORCH_RUN_RUNTIME_AC": env_flag(env, "ORCH_RUN_RUNTIME_AC"),
            "ORCH_RUNTIME_AC_ALLOW_COMMANDS": env_flag(env, "ORCH_RUNTIME_AC_ALLOW_COMMANDS"),
            "ORCH_RUNTIME_AC_TIMEOUT": env_int(env, "ORCH_RUNTIME_AC_TIMEOUT", 120),
        },
        "spec_dir": str(
            spec_dir or env.get("ORCH_RUNTIME_AC_SPEC_DIR", DEFAULT_RUNTIME_AC_SPEC_DIR)
        ),
        "required": required_rows,
        "recommendation": recommendation,
    }


def load_backlog_items(
    path: str | Path = DEFAULT_BACKLOG_JSON,
) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], str(exc)
    items = data.get("items") if isinstance(data, dict) else []
    return [item for item in items or [] if isinstance(item, dict)], None


def format_scan(summary: dict) -> str:
    lines = [
        "runtime_ac_gate scan: "
        f"required={summary['required_count']} checked={summary['items_checked']} "
        f"active_blockers={summary['active_blocker_count']}",
        f"source={summary.get('source', DEFAULT_BACKLOG_JSON)}",
        f"spec_dir={summary['spec_dir']}",
        f"recommendation: {summary['recommendation']}",
    ]
    if summary.get("load_error"):
        lines.append(f"load_error: {summary['load_error']}")
    for row in summary.get("required") or []:
        status = row["status"]
        preflight = row["active_preflight"]
        lines.append(
            f"  {row['target']}: status={status.get('status')} "
            f"active_preflight={preflight.get('status')} spec={status.get('spec_path')}"
        )
    return "\n".join(lines)


def _scan_cron_runtime_ac_gates(path: str | Path) -> tuple[dict, str | None]:
    """Extract archived runtime-AC gate events from the local orchestrator cron log."""
    log_path = Path(path)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": str(log_path),
            "exists": False,
            "event_count": 0,
            "target_count": 0,
            "status_counts": {},
            "targets": [],
        }, str(exc)

    events: list[dict] = []
    for block in re.findall(r'"runtime_ac_gates"\s*:\s*\[(.*?)\]', text, re.DOTALL):
        for match in re.finditer(
            r'\{.*?"target"\s*:\s*"([^"]+)".*?'
            r'"spec_path"\s*:\s*"([^"]+)".*?'
            r'"status"\s*:\s*"([^"]+)".*?\}',
            block,
            re.DOTALL,
        ):
            target, spec, status = match.groups()
            events.append({"target": target, "spec_path": spec, "status": status})

    status_counts: dict[str, int] = {}
    targets: dict[str, dict] = {}
    for event in events:
        status = event["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        row = targets.setdefault(
            event["target"],
            {
                "target": event["target"],
                "event_count": 0,
                "statuses": {},
                "latest_status": status,
                "latest_spec_path": event["spec_path"],
            },
        )
        row["event_count"] += 1
        row["statuses"][status] = row["statuses"].get(status, 0) + 1
        row["latest_status"] = status
        row["latest_spec_path"] = event["spec_path"]

    return {
        "path": str(log_path),
        "exists": True,
        "event_count": len(events),
        "target_count": len(targets),
        "status_counts": status_counts,
        "targets": sorted(targets.values(), key=lambda row: row["target"]),
    }, None


def _scan_db_runtime_ac_rows(db_path: str | Path) -> tuple[dict, str | None]:
    """Summarize retained runtime-AC evidence from the feedback DB."""
    path = Path(db_path)
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "runtime_ac_runs": 0,
            "production_runs": 0,
            "production_outcomes": 0,
            "evaluation_runs": 0,
            "recent_production": [],
        }, f"{path} does not exist"
    try:
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT r.run_id, r.target, r.agent, r.mode, r.source, r.experiment_id,
                   o.verifier_verdict, o.adjudicated_verdict, o.merged,
                   o.durability, o.notes
            FROM runs r
            LEFT JOIN outcomes o ON o.run_id = r.run_id
            WHERE r.task_type = 'runtime_ac'
               OR o.verifier_verdict LIKE '%RUNTIME_AC%'
               OR o.notes LIKE '%runtime%AC%'
            ORDER BY r.ts DESC
            """).fetchall()
    except sqlite3.Error as exc:
        return {
            "path": str(path),
            "exists": True,
            "runtime_ac_runs": 0,
            "production_runs": 0,
            "production_outcomes": 0,
            "evaluation_runs": 0,
            "recent_production": [],
        }, str(exc)
    finally:
        try:
            c.close()
        except Exception:
            pass

    production: list[sqlite3.Row] = []
    evaluation = 0
    for row in rows:
        target = str(row["target"] or "")
        if " [exp " in target or str(row["run_id"] or "").startswith("backfill-"):
            evaluation += 1
        else:
            production.append(row)

    recent = []
    for row in production[:10]:
        recent.append(
            {
                "run_id": row["run_id"],
                "target": row["target"],
                "agent": row["agent"],
                "mode": row["mode"],
                "source": row["source"],
                "verifier_verdict": row["verifier_verdict"],
                "adjudicated_verdict": row["adjudicated_verdict"],
                "merged": bool(row["merged"]) if row["merged"] is not None else None,
                "durability": row["durability"],
                "notes": row["notes"],
            }
        )

    return {
        "path": str(path),
        "exists": True,
        "runtime_ac_runs": len(rows),
        "production_runs": len(production),
        "production_outcomes": sum(1 for row in production if row["adjudicated_verdict"]),
        "evaluation_runs": evaluation,
        "recent_production": recent,
    }, None


def scan_history(
    *,
    log_path: str | Path = DEFAULT_CRON_LOG,
    db_path: str | Path = feedback.DB_PATH,
) -> dict:
    """Read-only history scan for runtime-AC traffic that is not in current backlog."""
    cron, cron_error = _scan_cron_runtime_ac_gates(log_path)
    db, db_error = _scan_db_runtime_ac_rows(db_path)
    archived_gate_targets = int(cron.get("target_count") or 0)
    production_rows = int(db.get("production_runs") or 0)
    if archived_gate_targets or production_rows:
        recommendation = (
            "Archived runtime-AC traffic exists; use this history for evidence/backfill review "
            "instead of waiting only for current backlog closer labels."
        )
    else:
        recommendation = (
            "No archived runtime-AC traffic was found; wait for a spec/label-backed closer or "
            "create an explicit spec-backed exercise."
        )
    return {
        "read_only": True,
        "history_available": bool(archived_gate_targets or production_rows),
        "archived_gate_targets": archived_gate_targets,
        "archived_gate_events": int(cron.get("event_count") or 0),
        "db_production_rows": production_rows,
        "db_evaluation_rows": int(db.get("evaluation_runs") or 0),
        "cron": cron,
        "db": db,
        "errors": {
            "cron": cron_error,
            "db": db_error,
        },
        "recommendation": recommendation,
    }


def format_history(summary: dict) -> str:
    lines = [
        "runtime_ac_gate history: "
        f"history_available={summary['history_available']} "
        f"archived_gate_targets={summary['archived_gate_targets']} "
        f"db_production_rows={summary['db_production_rows']} "
        f"db_evaluation_rows={summary['db_evaluation_rows']}",
        f"cron_log={summary['cron']['path']}",
        f"feedback_db={summary['db']['path']}",
        f"recommendation: {summary['recommendation']}",
    ]
    for target in (summary.get("cron", {}).get("targets") or [])[:8]:
        lines.append(
            f"  archived {target['target']}: events={target['event_count']} "
            f"latest_status={target['latest_status']} spec={target['latest_spec_path']}"
        )
    for row in (summary.get("db", {}).get("recent_production") or [])[:8]:
        lines.append(
            f"  db {row['target']}: agent={row['agent']} "
            f"verdict={row.get('adjudicated_verdict') or row.get('verifier_verdict')} "
            f"durability={row.get('durability')}"
        )
    return "\n".join(lines)


def exercise_gate() -> dict:
    """Run a real, non-mutating runtime-AC gate over a temporary command spec.

    This is an operator smoke for the active gate path when no current closer item
    requires runtime AC. It executes a harmless Python command check, refuses to
    patch an outcome by returning no run id, records its structured gate event,
    and removes the temporary spec on exit.
    """
    with tempfile.TemporaryDirectory(prefix="runtime-ac-gate-exercise-") as tmp:
        target = "stranske/Workflows#303"
        item = {
            "target": target,
            "task_type": "runtime_ac",
            "lane": "closer",
            "labels": ["runtime-ac"],
            "title": "Runtime AC gate exercise",
        }
        path = spec_path(target, spec_dir=tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        command = f"{sys.executable} -c 'print(\"ok\")'"
        path.write_text(
            json.dumps(_command_spec(str(Path(__file__).resolve().parent), command)),
            encoding="utf-8",
        )
        gate = gate_status(
            item,
            dry_run=False,
            env={
                "ORCH_RUN_RUNTIME_AC": "1",
                "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1",
                "ORCH_RUNTIME_AC_TIMEOUT": "30",
            },
            spec_dir=tmp,
            latest_run_fn=lambda target, mode=None: None,
        )
        passed = (
            gate.get("status") == "executed"
            and gate.get("verdict") == "PASS"
            and not gate.get("blocks")
        )
        return {
            "exercise": "runtime_ac_gate",
            "read_only_outcomes": True,
            "writes_structured_gate_event": True,
            "mutates_repos": False,
            "temporary_spec_removed": True,
            "target": target,
            "status": "pass" if passed else "fail",
            "gate": gate,
        }


def format_exercise(report: dict) -> str:
    gate = report.get("gate") or {}
    feedback_report = gate.get("feedback") or {}
    return "\n".join(
        [
            "runtime_ac_gate exercise: "
            f"status={report.get('status')} verdict={gate.get('verdict')} "
            f"blocks={gate.get('blocks')}",
            f"target={report.get('target')}",
            f"result_count={gate.get('result_count')} pass_ratio={gate.get('pass_ratio')}",
            "feedback: "
            f"recorded={feedback_report.get('recorded')} "
            f"reason={feedback_report.get('reason', '')}",
            "mutates_repos=false temporary_spec_removed=true",
        ]
    )


def _command_spec(worktree: str, command: str) -> dict:
    return {
        "verification": {
            "id": "gate-command-runtime-ac",
            "title": "Gate command runtime AC",
            "target": "stranske/Workflows#303",
            "goal": "Verify shared runtime AC gate command execution.",
            "risk_level": "low",
        },
        "runtime_context": {"worktree": worktree},
        "acceptance_criteria": [
            {
                "id": "AC1",
                "statement": "The check prints ok.",
                "evidence_required": ["command_output"],
                "checks": [
                    {
                        "id": "AC1-CMD",
                        "type": "command",
                        "name": "Print ok",
                        "command": command,
                        "expected": "contains",
                        "contains": "ok",
                    }
                ],
            }
        ],
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": False,
            "fail_on_missing_checks": True,
            "required_check_ids": ["AC1-CMD"],
            "min_pass_ratio": 1.0,
        },
    }


def _selftest() -> None:
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="runtime-ac-gate-") as tmp:
        old_feedback_db = feedback.DB_PATH
        feedback.DB_PATH = Path(tmp) / "feedback" / "orchestrator.db"
        item = {
            "target": "stranske/Workflows#303",
            "task_type": "implement",
            "lane": "closer",
            "labels": ["runtime-ac"],
            "title": "Runtime-sensitive merge",
        }
        fixture_spec = runtime_ac._valid_spec()
        fixture_spec["verification"]["target"] = item["target"]
        fixture_spec["verification"]["repo"] = item["target"].split("#", 1)[0]
        path = spec_path(item["target"], spec_dir=tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixture_spec), encoding="utf-8")
        missing_path = Path(tmp) / "missing.json"
        assert "/" not in target_slug("../owner/repo#5")
        assert required({"labels": ["Type: Runtime-AC"]}, missing_path) is True
        assert required({"labels": ["AC-CHECKS"]}, missing_path) is True
        with tempfile.TemporaryDirectory(prefix="runtime-ac-env-") as env_tmp:
            env_path = spec_path(item["target"], env={"ORCH_RUNTIME_AC_SPEC_DIR": env_tmp})
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(json.dumps(fixture_spec), encoding="utf-8")
            env_planned = gate_status(item, dry_run=True, env={"ORCH_RUNTIME_AC_SPEC_DIR": env_tmp})
            assert env_planned["spec_path"] == str(env_path), env_planned

        planned = gate_status(item, dry_run=True, spec_dir=tmp)
        assert planned["status"] == "planned" and planned["blocks"] is False, planned
        assert (
            gate_status(
                {**item, "target": "stranske/Workflows#999", "labels": []},
                dry_run=False,
                spec_dir=tmp,
            )
            is None
        )
        assert gate_status({**item, "lane": "opener"}, dry_run=True, spec_dir=tmp) is None
        missing = gate_status(
            {**item, "target": "stranske/Workflows#304"},
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
        )
        assert missing["status"] == "missing_spec" and missing["blocks"] is True, missing
        disabled = gate_status(item, dry_run=False, env={}, spec_dir=tmp)
        assert disabled["status"] == "required_but_not_run" and disabled["blocks"] is True, disabled
        path.write_text("{not json", encoding="utf-8")
        malformed = gate_status(item, dry_run=False, env={"ORCH_RUN_RUNTIME_AC": "1"}, spec_dir=tmp)
        assert malformed["status"] == "failed" and malformed["blocks"] is True, malformed

        recorded = []
        path.write_text(json.dumps(fixture_spec), encoding="utf-8")

        def fake_pass(spec, **kwargs):
            assert kwargs["confirm_run"] is True, kwargs
            return {
                "gate": {
                    "verification_id": "fixture",
                    "verdict": "PASS",
                    "verifier_verdict": "PASS_RUNTIME_AC",
                    "pass_ratio": 1.0,
                    "result_count": 1,
                    "blocking": [],
                    "needs_review": [],
                }
            }

        def fake_fail(spec, **kwargs):
            return {
                "gate": {
                    "verification_id": "fixture",
                    "verdict": "FAIL",
                    "verifier_verdict": "FAIL_RUNTIME_AC",
                    "pass_ratio": 0.0,
                    "result_count": 1,
                    "blocking": [{"check_id": "AC1", "status": "FAIL"}],
                    "needs_review": [],
                }
            }

        def fake_needs_review(spec, **kwargs):
            return {
                "gate": {
                    "verification_id": "fixture-review",
                    "verdict": "NEEDS_REVIEW",
                    "verifier_verdict": "NEEDS_REVIEW_RUNTIME_AC",
                    "pass_ratio": 0.5,
                    "result_count": 1,
                    "blocking": [],
                    "needs_review": [{"check_id": "AC1", "status": "NEEDS_REVIEW"}],
                }
            }

        passed = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_TIMEOUT": "15"},
            spec_dir=tmp,
            run_fn=fake_pass,
            latest_run_fn=lambda target, mode=None: "remote-run-303",
            record_fn=lambda run_id, gate: recorded.append((run_id, gate["verdict"]))
            or {"recorded": True},
        )
        assert passed["status"] == "executed" and passed["blocks"] is False, passed
        assert passed["run_id"] == "remote-run-303" and recorded == [
            ("remote-run-303", "PASS")
        ], passed
        feedback_error = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_pass,
            latest_run_fn=lambda target, mode=None: (_ for _ in ()).throw(RuntimeError("db busy")),
        )
        assert (
            feedback_error["status"] == "executed" and feedback_error["blocks"] is False
        ), feedback_error
        assert feedback_error["feedback"]["recorded"] is False, feedback_error
        failed = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_fail,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert failed["status"] == "executed" and failed["blocks"] is True, failed
        assert failed["feedback"]["recorded"] is False, failed
        needs_review = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_needs_review,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert (
            needs_review["verdict"] == "NEEDS_REVIEW" and needs_review["blocks"] is True
        ), needs_review

        structured = feedback.runtime_ac_gate_events(limit=100)
        statuses = {row.get("gate_status") for row in structured}
        verdicts = {row.get("verifier_verdict") for row in structured}
        assert {
            "required",
            "planned",
            "missing_spec",
            "skipped",
            "error",
            "executed",
        } <= statuses, structured
        assert {"PASS", "NEEDS_REVIEW", "FAIL"} <= verdicts, structured
        assert all(row.get("spec_path_matches_target") is True for row in structured), structured

        materialized_target = "stranske/Pension-Data#703"
        materialized_spec = json.loads(json.dumps(fixture_spec))
        materialized_spec["verification"]["target"] = materialized_target
        materialized_spec["verification"]["repo"] = "stranske/Pension-Data"
        materialized = materialize_range_spec(
            materialized_target,
            materialized_spec,
            spec_dir=tmp,
            producer_run_id="range-run-703",
        )
        assert materialized["status"] == "materialized", materialized
        materialized_item = {
            "target": materialized_target,
            "task_type": "implement",
            "lane": "closer",
            "labels": [],
        }
        next_gate = gate_status(
            materialized_item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_pass,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert next_gate["spec_path"] == materialized["spec_path"], next_gate
        assert next_gate["spec_hash"] == materialized["spec_hash"], (next_gate, materialized)
        wrong_target = materialize_range_spec(
            "stranske/Pension-Data#704",
            fixture_spec,
            spec_dir=tmp,
        )
        assert wrong_target["status"] == "materialization_failed", wrong_target
        assert "spec_target_mismatch" in wrong_target["terminal_reason"], wrong_target
        assert not spec_path("stranske/Pension-Data#704", spec_dir=tmp).exists()

        command = f"{sys.executable} -c 'print(\"ok\")'"
        path.write_text(
            json.dumps(_command_spec(str(Path(__file__).resolve().parent), command)),
            encoding="utf-8",
        )
        integrated = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1"},
            spec_dir=tmp,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert integrated["status"] == "executed" and integrated["verdict"] == "PASS", integrated
        assert integrated["blocks"] is False, integrated

        scan_item = {
            "target": item["target"],
            "task_type": "implement",
            "lane": "closer",
            "labels": ["runtime-ac"],
        }
        ignored = {
            "target": "stranske/Workflows#305",
            "task_type": "implement",
            "lane": "opener",
            "labels": ["runtime-ac"],
        }
        scan = scan_items([scan_item, ignored], env={}, spec_dir=tmp)
        assert scan["required_count"] == 1 and scan["items_checked"] == 2, scan
        assert scan["status_counts"]["planned"] == 1, scan
        assert scan["required"][0]["active_preflight"]["status"] == "requires_env", scan
        scan_ready = scan_items([scan_item], env={"ORCH_RUN_RUNTIME_AC": "1"}, spec_dir=tmp)
        assert (
            scan_ready["required"][0]["active_preflight"]["status"] == "ready_to_execute"
        ), scan_ready
        missing_scan = scan_items(
            [{**scan_item, "target": "stranske/Workflows#306"}],
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
        )
        assert missing_scan["status_counts"]["missing_spec"] == 1, missing_scan
        assert missing_scan["active_blocker_count"] == 1, missing_scan
        malformed_backlog = Path(tmp) / "bad-backlog.json"
        malformed_backlog.write_text("{not json", encoding="utf-8")
        loaded_items, load_error = load_backlog_items(malformed_backlog)
        assert loaded_items == [] and load_error, (loaded_items, load_error)
        missing_items, missing_error = load_backlog_items(Path(tmp) / "missing.json")
        assert missing_items == [] and missing_error, (missing_items, missing_error)

        history_log = Path(tmp) / "cron.log"
        history_log.write_text(
            """
            {
              "runtime_ac_gates": [
                {
                  "target": "stranske/Workflows#2479",
                  "spec_path": "/tmp/stranske__Workflows__2479.json",
                  "status": "missing_spec",
                  "blocks": true
                }
              ]
            }
            {
              "runtime_ac_gates": []
            }
            """,
            encoding="utf-8",
        )
        history = scan_history(log_path=history_log, db_path=Path(tmp) / "missing.db")
        assert history["history_available"] is True, history
        assert history["archived_gate_targets"] == 1, history
        assert history["cron"]["targets"][0]["target"] == "stranske/Workflows#2479"
        assert "runtime_ac_gate history:" in format_history(history), history

        exercise = exercise_gate()
        assert exercise["status"] == "pass", exercise
        assert exercise["mutates_repos"] is False, exercise
        assert exercise["gate"]["feedback"]["recorded"] is False, exercise
        assert "runtime_ac_gate exercise:" in format_exercise(exercise), exercise

        feedback.DB_PATH = old_feedback_db

    print(
        "runtime_ac_gate.py selftest: OK (requirement detection, active gate, "
        "feedback patch, command integration, backlog scan, history scan)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Runtime AC gate helpers.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--scan-backlog",
        nargs="?",
        const=str(DEFAULT_BACKLOG_JSON),
        help="read-only scan of backlog.json for runtime-AC-required closer items",
    )
    parser.add_argument(
        "--exercise",
        action="store_true",
        help="run a non-mutating active runtime-AC gate exercise with a temporary command spec",
    )
    parser.add_argument(
        "--scan-history",
        action="store_true",
        help="read-only scan of local archived runtime-AC gate and feedback evidence",
    )
    parser.add_argument(
        "--materialize-range-spec",
        type=Path,
        help="validate and atomically install a range-lane runtime-AC JSON artifact",
    )
    parser.add_argument(
        "--target",
        help="exact owner/repo#number target required with --materialize-range-spec",
    )
    parser.add_argument(
        "--producer-run-id",
        help="optional range-lane producer run id retained with materialization output",
    )
    parser.add_argument(
        "--cron-log",
        type=Path,
        default=DEFAULT_CRON_LOG,
        help="cron log to scan with --scan-history",
    )
    parser.add_argument(
        "--feedback-db",
        type=Path,
        default=feedback.DB_PATH,
        help="feedback DB to scan with --scan-history",
    )
    parser.add_argument("--spec-dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.materialize_range_spec:
        if not args.target:
            parser.error("--materialize-range-spec requires --target")
        report = materialize_range_spec(
            args.target,
            args.materialize_range_spec,
            spec_dir=args.spec_dir,
            producer_run_id=args.producer_run_id,
        )
        print(json.dumps(report, indent=2) if args.as_json else json.dumps(report, indent=2))
        return 0 if report.get("status") == "materialized" else 1
    if args.exercise:
        report = exercise_gate()
        print(json.dumps(report, indent=2) if args.as_json else format_exercise(report))
        return 0 if report.get("status") == "pass" else 1
    if args.scan_history:
        summary = scan_history(log_path=args.cron_log, db_path=args.feedback_db)
        print(json.dumps(summary, indent=2) if args.as_json else format_history(summary))
        return 0
    if args.scan_backlog:
        items, load_error = load_backlog_items(args.scan_backlog)
        summary = scan_items(items, spec_dir=args.spec_dir)
        summary["source"] = str(args.scan_backlog)
        if load_error:
            summary["load_error"] = load_error
            summary["recommendation"] = (
                "Backlog could not be loaded; refresh the handoff backlog snapshot before expecting gates to fire."
            )
        print(json.dumps(summary, indent=2) if args.as_json else format_scan(summary))
        return 0
    parser.print_usage()
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
