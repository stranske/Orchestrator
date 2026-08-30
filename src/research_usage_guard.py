#!/usr/bin/env python3
"""Deterministic, local admission control for optional research followups.

The guard makes no network or model calls. It records every research opportunity before dispatch,
deduplicates stable inputs across ticks, enforces rolling request/prompt budgets, blocks repeated
subject spikes, and emits an operator-readable anomaly report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import capabilities
import feedback
import research_subjects
import spec_provenance

DEFAULT_MAX_EVAL_CALLS_24H = 20
DEFAULT_MAX_PROMPT_BYTES_24H = 2_000_000
DEFAULT_MAX_EVAL_CALLS_1H = 8
DEFAULT_MAX_PROMPT_BYTES_1H = 1_000_000
DEFAULT_MAX_SUBJECT_SHARE_24H = 0.30
DEFAULT_MAX_CONSECUTIVE_SUBJECT = 2
DEFAULT_STALE_DISPATCH_HOURS = 2
ACTIVE_ALERT_WINDOW_HOURS = 24
DEFAULT_MAX_PANELS_PER_SUBJECT_24H = 2
DEFAULT_MAX_UNATTENDED_PANEL_WIDTH = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_usage_opportunities (
  opportunity_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  exp_id TEXT NOT NULL,
  repo TEXT NOT NULL,
  subject TEXT NOT NULL,
  signature TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  estimated_prompt_bytes INTEGER NOT NULL,
  estimated_prompt_tokens INTEGER NOT NULL,
  evaluator_count INTEGER NOT NULL,
  evaluator_agents_json TEXT NOT NULL DEFAULT '[]',
  is_manual INTEGER NOT NULL,
  is_missing_spec INTEGER NOT NULL,
  terminal_outcome TEXT NOT NULL DEFAULT 'unknown',
  completed_at INTEGER,
  alerts_json TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_usage_opp_ts ON research_usage_opportunities(ts);
CREATE INDEX IF NOT EXISTS idx_research_usage_opp_sig ON research_usage_opportunities(signature);
CREATE INDEX IF NOT EXISTS idx_research_usage_opp_subj ON research_usage_opportunities(subject);
"""

SCHEMA_MIGRATIONS = {
    "evaluator_agents_json": "TEXT NOT NULL DEFAULT '[]'",
    "terminal_outcome": "TEXT NOT NULL DEFAULT 'unknown'",
    "completed_at": "INTEGER",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the local ledger and add forward-compatible columns to earlier drafts."""

    conn.executescript(SCHEMA)
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(research_usage_opportunities)")
    }
    for name, declaration in SCHEMA_MIGRATIONS.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE research_usage_opportunities ADD COLUMN {name} {declaration}"
            )
    conn.commit()


def subject_of_experiment(exp_id: str) -> str:
    """Recover the correlated subject key from legacy ``tick-<ts>-<subject>`` IDs."""

    parts = str(exp_id).split("-", 2)
    if len(parts) == 3 and parts[0] == "tick" and parts[1].isdigit():
        return parts[2]
    return str(exp_id)


def compute_followup_signature(
    repo: str,
    spec_text: str | None,
    base_ref_or_sha: str | None,
    candidate_diffs: dict[str, str] | None = None,
) -> str:
    """Hash repository, normalized spec, base, and sorted candidate diff hashes."""

    normalized_spec = research_subjects.normalize_spec(spec_text or "")
    spec_hash = hashlib.sha256(normalized_spec.encode("utf-8")).hexdigest()
    repo_key = str(repo or "").strip().lower()
    base_key = str(base_ref_or_sha or "").strip().lower()
    candidate_hashes = []
    for member_id in sorted(candidate_diffs or {}):
        diff_content = (candidate_diffs or {})[member_id] or ""
        diff_hash = hashlib.sha256(diff_content.encode("utf-8")).hexdigest()
        candidate_hashes.append(f"{member_id}:{diff_hash}")
    payload = f"{repo_key}|{spec_hash}|{base_key}|{','.join(candidate_hashes)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_prompt_bytes_and_tokens(
    spec_text: str | None,
    candidate_diffs: dict[str, str] | None,
    evaluator_count: int,
) -> tuple[int, int]:
    """Conservatively estimate UTF-8 prompt bytes and tokens across evaluators."""

    spec_bytes = len(str(spec_text or "").encode("utf-8"))
    diff_bytes = sum(
        len(str(diff or "")[:100_000].encode("utf-8")) for diff in (candidate_diffs or {}).values()
    )
    per_evaluator_bytes = spec_bytes + diff_bytes + 2_000
    total_bytes = per_evaluator_bytes * max(1, evaluator_count)
    return total_bytes, (total_bytes + 3) // 4


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def observe_recent_research_usage(
    *,
    conn: sqlite3.Connection,
    window_days: int,
    now: int,
    experiment_dir: Path | None = None,
) -> dict[str, Any]:
    """Audit recorded evaluator runs for bypasses and atypical panel concentration.

    This read-side detector is intentionally independent of the admission ledger: it still catches
    an evaluator path that bypasses the guard or predates deployment.
    """

    empty = {
        "telemetry_available": True,
        "panel_count": 0,
        "evaluator_calls": 0,
        "missing_spec_panel_count": 0,
        "missing_spec_evaluator_calls": 0,
        "wide_panel_count": 0,
        "top_subjects": [],
        "alerts": [],
        "active_alert_count": 0,
    }
    if not _table_exists(conn, "runs"):
        return {
            **empty,
            "telemetry_available": False,
            "alerts": [
                {
                    "type": "observed_usage_telemetry_unavailable",
                    "class": "observability",
                    "reason": "feedback runs table is missing",
                    "active_panel_count": 1,
                }
            ],
            "active_alert_count": 1,
        }

    config_alerts: list[dict[str, Any]] = []

    def observed_limit(name: str, default: int) -> int:
        try:
            return _int_limit(os.environ, name, default)
        except ValueError as exc:
            config_alerts.append(
                {
                    "type": "observed_usage_config_invalid",
                    "class": "observability",
                    "detail": str(exc),
                    "setting": name,
                    "active_panel_count": 1,
                }
            )
            return default

    max_subject_panels = observed_limit(
        "ORCH_GUARD_MAX_PANELS_PER_SUBJECT_24H", DEFAULT_MAX_PANELS_PER_SUBJECT_24H
    )
    max_panel_width = observed_limit(
        "ORCH_GUARD_MAX_UNATTENDED_PANEL_WIDTH", DEFAULT_MAX_UNATTENDED_PANEL_WIDTH
    )
    since = now - max(1, int(window_days)) * 86_400
    rows = conn.execute(
        "SELECT experiment_id,agent,ts FROM runs "
        "WHERE ts>=? AND task_type='review' "
        "AND experiment_id LIKE 'tick-%'",
        (since,),
    ).fetchall()
    if not rows:
        return {
            **empty,
            "alerts": config_alerts,
            "active_alert_count": len(config_alerts),
        }

    panels: dict[str, dict[str, Any]] = {}
    for experiment_id, agent, ts in rows:
        exp_id = str(experiment_id)
        panel = panels.setdefault(
            exp_id,
            {
                "subject": subject_of_experiment(exp_id),
                "calls": 0,
                "agents": set(),
                "last_ts": 0,
                "missing_spec": False,
            },
        )
        panel["calls"] += 1
        panel["agents"].add(str(agent or "unknown"))
        panel["last_ts"] = max(int(panel["last_ts"]), int(ts or 0))

    root = experiment_dir or Path(
        os.environ.get("ORCH_EXP_DIR", Path(__file__).resolve().parent / "experiments")
    )
    for exp_id, panel in panels.items():
        directory = root / exp_id
        try:
            meta = json.loads((directory / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            meta = {}
        try:
            spec_text = (directory / "spec.md").read_text(errors="replace")
        except OSError:
            spec_text = ""
        panel["missing_spec"] = spec_provenance.is_missing_spec(meta, spec_text)

    subject_panels: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for panel in panels.values():
        subject_panels[str(panel["subject"])].append(panel)
    active_since = now - ACTIVE_ALERT_WINDOW_HOURS * 3_600
    missing_panels = [panel for panel in panels.values() if panel["missing_spec"]]
    wide_panels = [panel for panel in panels.values() if len(panel["agents"]) > max_panel_width]
    alerts: list[dict[str, Any]] = list(config_alerts)

    if missing_panels:
        alerts.append(
            {
                "type": "observed_missing_spec_judge_calls",
                "class": "anomaly",
                "panel_count": len(missing_panels),
                "evaluator_calls": sum(int(panel["calls"]) for panel in missing_panels),
                "active_panel_count": sum(
                    int(panel["last_ts"] >= active_since) for panel in missing_panels
                ),
            }
        )
    if wide_panels:
        alerts.append(
            {
                "type": "observed_wide_research_panels",
                "class": "anomaly",
                "panel_count": len(wide_panels),
                "max_width": max(len(panel["agents"]) for panel in wide_panels),
                "limit": max_panel_width,
                "active_panel_count": sum(
                    int(panel["last_ts"] >= active_since) for panel in wide_panels
                ),
            }
        )
    for subject, subject_rows in sorted(subject_panels.items()):
        active_rows = [panel for panel in subject_rows if panel["last_ts"] >= active_since]
        if len(subject_rows) > max_subject_panels:
            alerts.append(
                {
                    "type": "observed_repeated_subject_panels",
                    "class": "anomaly",
                    "subject": subject,
                    "panel_count": len(subject_rows),
                    "limit": max_subject_panels,
                    "active_panel_count": len(active_rows),
                }
            )

    return {
        "telemetry_available": True,
        "panel_count": len(panels),
        "evaluator_calls": len(rows),
        "missing_spec_panel_count": len(missing_panels),
        "missing_spec_evaluator_calls": sum(int(panel["calls"]) for panel in missing_panels),
        "wide_panel_count": len(wide_panels),
        "top_subjects": sorted(
            ((subject, len(subject_rows)) for subject, subject_rows in subject_panels.items()),
            key=lambda item: (-item[1], item[0]),
        )[:10],
        "alerts": alerts,
        "active_alert_count": sum(int(alert.get("active_panel_count", 0) > 0) for alert in alerts),
    }


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_guard_bypassed(is_manual: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """Return whether a supervised caller explicitly bypassed optional-research budgets."""

    environ = os.environ if env is None else env
    return is_manual or _truthy(environ.get("ORCH_RESEARCH_USAGE_BYPASS"))


def _int_limit(environ: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(environ.get(key, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer guard setting: {key}") from exc
    if value < 0:
        raise ValueError(f"negative guard setting: {key}")
    return value


def _float_limit(environ: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(environ.get(key, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric guard setting: {key}") from exc
    if not 0 <= value <= 1:
        raise ValueError(f"guard setting outside [0,1]: {key}")
    return value


def assess_and_record_opportunity(
    *,
    exp_id: str,
    repo: str,
    subject: str,
    spec_text: str | None,
    base_sha: str | None,
    candidate_diffs: dict[str, str] | None = None,
    evaluator_count: int = 1,
    evaluator_agents: Sequence[str] | None = None,
    is_missing_spec: bool = False,
    is_manual: bool = False,
    env: Mapping[str, str] | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Persist one opportunity and decide whether optional model dispatch is permitted."""

    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    current_ts = int(time.time() if now is None else now)
    environ = os.environ if env is None else env
    normalized_subject = str(subject or subject_of_experiment(exp_id))
    agents = [str(agent).strip() for agent in (evaluator_agents or []) if str(agent).strip()]
    if agents:
        evaluator_count = len(agents)
    evaluator_count = max(1, int(evaluator_count))
    signature = compute_followup_signature(repo, spec_text, base_sha, candidate_diffs)
    est_bytes, est_tokens = estimate_prompt_bytes_and_tokens(
        spec_text, candidate_diffs, evaluator_count
    )
    bypassed = is_guard_bypassed(is_manual, environ)

    # Missing specifications are never model-evaluable, even under a manual budget bypass.
    if is_missing_spec:
        result = _save_opportunity(
            db,
            current_ts,
            exp_id,
            repo,
            normalized_subject,
            signature,
            "missing-spec-objective-only",
            "missing_spec_recovered",
            0,
            0,
            0,
            [],
            is_manual,
            True,
            [],
            False,
            bypassed,
        )
        if close:
            db.close()
        return result

    if bypassed:
        result = _save_opportunity(
            db,
            current_ts,
            exp_id,
            repo,
            normalized_subject,
            signature,
            "admitted",
            "manual_override_bypass",
            est_bytes,
            est_tokens,
            evaluator_count,
            agents,
            is_manual,
            False,
            [],
            True,
            True,
        )
        if close:
            db.close()
        return result

    # The shell default is defensive, but this in-process check is the actual dispatch invariant.
    if not _truthy(environ.get("ORCH_RESEARCH_ARM", "0")):
        result = _save_opportunity(
            db,
            current_ts,
            exp_id,
            repo,
            normalized_subject,
            signature,
            "deferred",
            "research_arm_disabled",
            est_bytes,
            est_tokens,
            evaluator_count,
            agents,
            False,
            False,
            [],
            False,
            False,
        )
        if close:
            db.close()
        return result

    # Fail closed across restarts. Once admitted, an opportunity may have spent model capacity
    # even when the caller ultimately records ``failed``. Do not repeat immutable inputs
    # automatically; a supervised manual bypass is the explicit recovery path.
    duplicate = db.execute(
        "SELECT terminal_outcome FROM research_usage_opportunities "
        "WHERE signature=? AND decision='admitted' LIMIT 1",
        (signature,),
    ).fetchone()
    if duplicate:
        result = _save_opportunity(
            db,
            current_ts,
            exp_id,
            repo,
            normalized_subject,
            signature,
            "duplicate",
            f"duplicate_signature_{duplicate[0]}",
            est_bytes,
            est_tokens,
            evaluator_count,
            agents,
            False,
            False,
            [],
            False,
            False,
        )
        if close:
            db.close()
        return result

    since_24h = current_ts - 86_400
    since_1h = current_ts - 3_600
    rows_24h = db.execute(
        "SELECT subject,evaluator_count,estimated_prompt_bytes "
        "FROM research_usage_opportunities WHERE ts>=? AND decision='admitted'",
        (since_24h,),
    ).fetchall()
    rows_1h = db.execute(
        "SELECT evaluator_count,estimated_prompt_bytes "
        "FROM research_usage_opportunities WHERE ts>=? AND decision='admitted'",
        (since_1h,),
    ).fetchall()

    try:
        max_calls_24h = _int_limit(
            environ, "ORCH_GUARD_MAX_EVAL_CALLS_24H", DEFAULT_MAX_EVAL_CALLS_24H
        )
        max_bytes_24h = _int_limit(
            environ, "ORCH_GUARD_MAX_PROMPT_BYTES_24H", DEFAULT_MAX_PROMPT_BYTES_24H
        )
        max_calls_1h = _int_limit(
            environ, "ORCH_GUARD_MAX_EVAL_CALLS_1H", DEFAULT_MAX_EVAL_CALLS_1H
        )
        max_bytes_1h = _int_limit(
            environ, "ORCH_GUARD_MAX_PROMPT_BYTES_1H", DEFAULT_MAX_PROMPT_BYTES_1H
        )
        max_subject_share = _float_limit(
            environ, "ORCH_GUARD_MAX_SUBJECT_SHARE_24H", DEFAULT_MAX_SUBJECT_SHARE_24H
        )
        max_consecutive = _int_limit(
            environ,
            "ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT",
            DEFAULT_MAX_CONSECUTIVE_SUBJECT,
        )
    except ValueError as exc:
        config_alerts: list[dict[str, Any]] = [
            {"type": "guard_config_invalid", "class": "anomaly", "detail": str(exc)}
        ]
        result = _save_opportunity(
            db,
            current_ts,
            exp_id,
            repo,
            normalized_subject,
            signature,
            "blocked_by_anomaly",
            "guard_config_invalid",
            est_bytes,
            est_tokens,
            evaluator_count,
            agents,
            False,
            False,
            config_alerts,
            False,
            False,
        )
        if close:
            db.close()
        return result

    recent_subjects = db.execute(
        "SELECT subject FROM research_usage_opportunities "
        "WHERE ts>=? AND decision='admitted' ORDER BY ts DESC,opportunity_id DESC LIMIT ?",
        (since_24h, max(1, max_consecutive)),
    ).fetchall()
    calls_24h = sum(int(row[1]) for row in rows_24h)
    bytes_24h = sum(int(row[2]) for row in rows_24h)
    calls_1h = sum(int(row[0]) for row in rows_1h)
    bytes_1h = sum(int(row[1]) for row in rows_1h)
    subject_count_24h = sum(1 for row in rows_24h if row[0] == normalized_subject)
    consecutive_count = 0
    for row in recent_subjects:
        if row[0] != normalized_subject:
            break
        consecutive_count += 1

    alerts: list[dict[str, Any]] = []
    if calls_24h + evaluator_count > max_calls_24h:
        alerts.append(
            {
                "type": "evaluator_call_limit_exceeded",
                "class": "budget",
                "limit": max_calls_24h,
                "current": calls_24h,
                "requested": evaluator_count,
            }
        )
    if bytes_24h + est_bytes > max_bytes_24h:
        alerts.append(
            {
                "type": "prompt_byte_limit_exceeded",
                "class": "budget",
                "limit": max_bytes_24h,
                "current": bytes_24h,
                "requested": est_bytes,
            }
        )
    if calls_1h + evaluator_count > max_calls_1h:
        alerts.append(
            {
                "type": "evaluator_call_spike",
                "class": "anomaly",
                "limit": max_calls_1h,
                "current": calls_1h,
                "requested": evaluator_count,
            }
        )
    if bytes_1h + est_bytes > max_bytes_1h:
        alerts.append(
            {
                "type": "prompt_byte_spike",
                "class": "anomaly",
                "limit": max_bytes_1h,
                "current": bytes_1h,
                "requested": est_bytes,
            }
        )
    if max_consecutive and consecutive_count >= max_consecutive:
        alerts.append(
            {
                "type": "repeated_subject_spike",
                "class": "anomaly",
                "subject": normalized_subject,
                "consecutive": consecutive_count,
                "limit": max_consecutive,
            }
        )
    elif len(rows_24h) >= 3:
        projected_share = (subject_count_24h + 1) / (len(rows_24h) + 1)
        if projected_share > max_subject_share:
            alerts.append(
                {
                    "type": "repeated_subject_share_spike",
                    "class": "anomaly",
                    "subject": normalized_subject,
                    "share": round(projected_share, 4),
                    "limit": max_subject_share,
                }
            )

    if any(alert["class"] == "anomaly" for alert in alerts):
        decision = "blocked_by_anomaly"
    elif alerts:
        decision = "blocked_by_limit"
    else:
        decision = "admitted"
    eligible = decision == "admitted"
    result = _save_opportunity(
        db,
        current_ts,
        exp_id,
        repo,
        normalized_subject,
        signature,
        decision,
        alerts[0]["type"] if alerts else "admitted",
        est_bytes,
        est_tokens,
        evaluator_count,
        agents,
        False,
        False,
        alerts,
        eligible,
        False,
    )
    if close:
        db.close()
    return result


def _save_opportunity(
    db: sqlite3.Connection,
    ts: int,
    exp_id: str,
    repo: str,
    subject: str,
    signature: str,
    decision: str,
    reason: str | None,
    estimated_bytes: int,
    estimated_tokens: int,
    evaluator_count: int,
    evaluator_agents: Sequence[str],
    is_manual: bool,
    is_missing_spec: bool,
    alerts: list[dict[str, Any]],
    eligible: bool,
    bypassed: bool,
) -> dict[str, Any]:
    # Every scheduler opportunity is a denominator row. A content-derived key would collapse two
    # same-second duplicate observations, which makes load measurement look better than reality.
    opportunity_id = f"opp:{uuid.uuid4().hex}"
    terminal_outcome = "dispatching" if eligible else decision
    completed_at = None if eligible else ts
    db.execute(
        "INSERT INTO research_usage_opportunities "
        "(opportunity_id,ts,exp_id,repo,subject,signature,decision,reason,"
        "estimated_prompt_bytes,estimated_prompt_tokens,evaluator_count,"
        "evaluator_agents_json,is_manual,is_missing_spec,terminal_outcome,completed_at,"
        "alerts_json,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            opportunity_id,
            ts,
            exp_id,
            repo,
            subject,
            signature,
            decision,
            reason,
            estimated_bytes,
            estimated_tokens,
            evaluator_count,
            json.dumps(list(evaluator_agents)),
            int(is_manual),
            int(is_missing_spec),
            terminal_outcome,
            completed_at,
            json.dumps(alerts) if alerts else None,
            json.dumps({"bypassed": bypassed, "eligible": eligible}),
        ),
    )
    db.commit()
    try:
        capabilities.production_heartbeat(
            "research-usage-guard",
            "invocation",
            ref=exp_id,
            metadata={"decision": decision, "eligible": eligible, "subject": subject},
        )
        capabilities.production_heartbeat(
            "research-usage-guard",
            "success",
            ref=opportunity_id,
            metadata={"decision": decision, "terminal_outcome": terminal_outcome},
        )
    except Exception:
        # Capability credit is secondary telemetry; it must never change the admission decision.
        pass
    return {
        "opportunity_id": opportunity_id,
        "ts": ts,
        "exp_id": exp_id,
        "repo": repo,
        "subject": subject,
        "signature": signature,
        "decision": decision,
        "reason": reason,
        "eligible": eligible,
        "bypassed": bypassed,
        "estimated_prompt_bytes": estimated_bytes,
        "estimated_prompt_tokens": estimated_tokens,
        "evaluator_count": evaluator_count,
        "evaluator_agents": list(evaluator_agents),
        "terminal_outcome": terminal_outcome,
        "alerts": alerts,
    }


def update_opportunity_outcome(
    opportunity_id: str,
    outcome: str,
    *,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
) -> None:
    """Finish an admitted denominator row after the evaluator process returns or fails."""

    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    completed_at = int(time.time() if now is None else now)
    db.execute(
        "UPDATE research_usage_opportunities SET terminal_outcome=?,completed_at=? "
        "WHERE opportunity_id=?",
        (str(outcome), completed_at, opportunity_id),
    )
    db.commit()
    if close:
        db.close()


def generate_usage_report(
    *,
    conn: sqlite3.Connection | None = None,
    window_days: int = 7,
    now: int | None = None,
) -> dict[str, Any]:
    """Generate a seven-day report whose health reflects active 24-hour blocks."""

    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    current_ts = int(time.time() if now is None else now)
    since = current_ts - max(1, int(window_days)) * 86_400
    active_since = current_ts - ACTIVE_ALERT_WINDOW_HOURS * 3_600
    rows = db.execute(
        "SELECT opportunity_id,ts,exp_id,repo,subject,signature,decision,reason,"
        "estimated_prompt_bytes,estimated_prompt_tokens,evaluator_count,"
        "evaluator_agents_json,terminal_outcome,alerts_json "
        "FROM research_usage_opportunities WHERE ts>=? ORDER BY ts DESC",
        (since,),
    ).fetchall()

    decision_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    alerts_by_type: Counter[str] = Counter()
    subject_counts: Counter[str] = Counter()
    evaluator_counts: Counter[str] = Counter()
    total_bytes = 0
    total_tokens = 0
    total_eval_calls = 0
    active_anomalies = 0
    active_budget_blocks = 0
    stale_dispatches = 0
    recent_opportunities: list[dict[str, Any]] = []

    for row in rows:
        (
            opportunity_id,
            ts,
            exp_id,
            repo,
            subject,
            signature,
            decision,
            reason,
            estimated_bytes,
            estimated_tokens,
            evaluator_count,
            evaluator_agents_json,
            terminal_outcome,
            alerts_json,
        ) = row
        decision_counts[str(decision)] += 1
        outcome_counts[str(terminal_outcome)] += 1
        subject_counts[str(subject)] += 1
        try:
            evaluator_agents = json.loads(evaluator_agents_json or "[]")
        except (TypeError, json.JSONDecodeError):
            evaluator_agents = []
        if decision == "admitted":
            total_bytes += int(estimated_bytes)
            total_tokens += int(estimated_tokens)
            total_eval_calls += int(evaluator_count)
            evaluator_counts.update(str(agent) for agent in evaluator_agents)
        if alerts_json:
            try:
                alerts = json.loads(alerts_json)
            except (TypeError, json.JSONDecodeError):
                alerts = []
            for alert in alerts:
                alerts_by_type[str(alert.get("type", "unknown"))] += 1
        if int(ts) >= active_since:
            active_anomalies += int(decision == "blocked_by_anomaly")
            active_budget_blocks += int(decision == "blocked_by_limit")
        if terminal_outcome == "dispatching" and int(ts) < (
            current_ts - DEFAULT_STALE_DISPATCH_HOURS * 3_600
        ):
            stale_dispatches += 1
        if len(recent_opportunities) < 20:
            recent_opportunities.append(
                {
                    "opportunity_id": opportunity_id,
                    "ts": ts,
                    "exp_id": exp_id,
                    "repo": repo,
                    "subject": subject,
                    "signature": signature,
                    "decision": decision,
                    "reason": reason,
                    "terminal_outcome": terminal_outcome,
                    "estimated_prompt_bytes": estimated_bytes,
                    "evaluator_count": evaluator_count,
                    "evaluator_agents": evaluator_agents,
                }
            )

    observed_usage = observe_recent_research_usage(
        conn=db,
        window_days=window_days,
        now=current_ts,
    )
    health_status = "OK"
    if active_anomalies:
        health_status = "ANOMALY_BLOCKED"
    elif active_budget_blocks:
        health_status = "BUDGET_BLOCKED"
    elif observed_usage["active_alert_count"]:
        health_status = (
            "OBSERVED_ANOMALY"
            if observed_usage["telemetry_available"]
            else "OBSERVABILITY_UNAVAILABLE"
        )
    elif stale_dispatches:
        health_status = "STALE_DISPATCH"
    report = {
        "generated_at": current_ts,
        "window_days": window_days,
        "active_alert_window_hours": ACTIVE_ALERT_WINDOW_HOURS,
        "health_status": health_status,
        "active_anomaly_blocks": active_anomalies,
        "active_budget_blocks": active_budget_blocks,
        "stale_dispatching_opportunities": stale_dispatches,
        "total_opportunities": len(rows),
        "decision_counts": dict(sorted(decision_counts.items())),
        "terminal_outcome_counts": dict(sorted(outcome_counts.items())),
        "total_admitted_prompt_bytes": total_bytes,
        "total_admitted_prompt_tokens": total_tokens,
        "total_admitted_evaluator_calls": total_eval_calls,
        "alerts_summary": dict(sorted(alerts_by_type.items())),
        "top_subjects": subject_counts.most_common(10),
        "evaluator_call_estimates": dict(sorted(evaluator_counts.items())),
        "observed_research_usage": observed_usage,
        "recent_opportunities": recent_opportunities,
    }
    if close:
        db.close()
    return report


def write_usage_report(
    output_path: Path | str | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Write the structured local report under the configured state root."""

    report = generate_usage_report(conn=conn)
    if output_path is None:
        state_dir = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
        output_path = state_dir / "research-usage-report.json"
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        capabilities.production_heartbeat(
            "research-usage-guard",
            "success",
            ref="daily-report",
            metadata={"health_status": report["health_status"], "output": str(path)},
        )
    except Exception:
        pass
    return report


def _selftest() -> None:
    db = sqlite3.connect(":memory:")
    ensure_schema(db)
    now = 1_700_000_000
    enabled = {"ORCH_RESEARCH_ARM": "1"}
    sig1 = compute_followup_signature("owner/repo", "spec content", "abc123", {"a": "diff-a"})
    sig2 = compute_followup_signature("owner/repo", "spec content", "abc123", {"a": "diff-a"})
    assert sig1 == sig2
    assert sig1 != compute_followup_signature(
        "owner/repo", "spec content v2", "abc123", {"a": "diff-a"}
    )
    byte_count, token_count = estimate_prompt_bytes_and_tokens("spec", {"a": "diff"}, 2)
    assert byte_count > 0 and token_count == (byte_count + 3) // 4

    admitted = assess_and_record_opportunity(
        exp_id="exp-1",
        repo="owner/repo",
        subject="subj-1",
        spec_text="spec text",
        base_sha="sha1",
        candidate_diffs={"codex": "diff1"},
        evaluator_agents=["vibe"],
        env=enabled,
        conn=db,
        now=now,
    )
    assert admitted["eligible"] and admitted["decision"] == "admitted"
    update_opportunity_outcome(admitted["opportunity_id"], "completed", conn=db, now=now + 1)
    duplicate = assess_and_record_opportunity(
        exp_id="exp-2",
        repo="owner/repo",
        subject="subj-1",
        spec_text="spec text",
        base_sha="sha1",
        candidate_diffs={"codex": "diff1"},
        evaluator_agents=["vibe"],
        env=enabled,
        conn=db,
        now=now + 10,
    )
    assert duplicate["decision"] == "duplicate"
    missing = assess_and_record_opportunity(
        exp_id="exp-3",
        repo="owner/repo",
        subject="subj-2",
        spec_text="stub",
        base_sha="sha2",
        is_missing_spec=True,
        env=enabled,
        conn=db,
        now=now + 20,
    )
    assert missing["decision"] == "missing-spec-objective-only"
    deferred = assess_and_record_opportunity(
        exp_id="exp-off",
        repo="owner/repo",
        subject="subj-off",
        spec_text="spec",
        base_sha="sha-off",
        conn=db,
        now=now + 25,
    )
    assert deferred["decision"] == "deferred"

    tight = {
        "ORCH_RESEARCH_ARM": "1",
        "ORCH_GUARD_MAX_EVAL_CALLS_24H": "2",
        "ORCH_GUARD_MAX_EVAL_CALLS_1H": "2",
        "ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT": "2",
    }
    blocked = assess_and_record_opportunity(
        exp_id="exp-4",
        repo="owner/repo",
        subject="subj-2",
        spec_text="spec 4",
        base_sha="sha4",
        candidate_diffs={"codex": "diff4"},
        evaluator_agents=["vibe", "aider"],
        env=tight,
        conn=db,
        now=now + 30,
    )
    assert not blocked["eligible"] and blocked["decision"] in {
        "blocked_by_anomaly",
        "blocked_by_limit",
    }
    manual = assess_and_record_opportunity(
        exp_id="exp-5",
        repo="owner/repo",
        subject="subj-2",
        spec_text="spec 5",
        base_sha="sha5",
        evaluator_agents=["vibe"],
        is_manual=True,
        env=tight,
        conn=db,
        now=now + 40,
    )
    assert manual["eligible"] and manual["bypassed"]
    report = generate_usage_report(conn=db, now=now + 50)
    assert report["total_opportunities"] == 6
    assert report["health_status"] in {"ANOMALY_BLOCKED", "BUDGET_BLOCKED"}
    db.close()
    print("research_usage_guard.py selftest: OK")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("cmd", nargs="?", default="report", choices=["report", "selftest"])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", help="write report JSON to this path")
    parser.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="return non-zero whenever report health is not OK",
    )
    args = parser.parse_args(argv)
    if args.selftest or args.cmd == "selftest":
        _selftest()
        return 0
    report = (
        write_usage_report(Path(args.write_report))
        if args.write_report
        else generate_usage_report()
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Usage Guard Report — Health: {report['health_status']}")
        print(f"Total Opportunities ({report['window_days']}d): {report['total_opportunities']}")
        print(f"Decisions: {report['decision_counts']}")
        print(f"Admitted Prompt Bytes: {report['total_admitted_prompt_bytes']}")
        print(f"Admitted Evaluator Calls: {report['total_admitted_evaluator_calls']}")
        if report["alerts_summary"]:
            print(f"Alerts: {report['alerts_summary']}")
    return 2 if args.fail_on_alert and report["health_status"] != "OK" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
