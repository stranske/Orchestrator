#!/usr/bin/env python3
"""Durable subject identity and acquisition controls for Orchestrator research.

The tables live in feedback._conn()'s SQLite database so subject selection is
joined to the existing Brain without inventing identities for legacy runs. This
module owns the additive schema while feedback.py is integrated independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import feedback

DEFAULT_COOLDOWN_HOURS = int(os.environ.get("ORCH_RESEARCH_SUBJECT_COOLDOWN_HOURS", "168"))
DEFAULT_UNEVALUATED_CAP = int(os.environ.get("ORCH_RESEARCH_UNEVALUATED_CAP", "25"))
DEFAULT_PER_SUBJECT_CAP = int(os.environ.get("ORCH_RESEARCH_PER_SUBJECT_CAP", "1"))
# THE EVALUATION WINDOW, DEFINED ONCE AND CONSUMED TWICE. `exp_abcd.followup` defaults its
# `max_age_days` to this value, and `unevaluated_experiment_ids` bounds its count by it, so the
# window the cap MEASURES and the window the drain can REACH are the same number by construction.
#
# They were not, and that deadlocked the whole research arm for five weeks. The cap counted every
# experiment lacking an `evaluations` row over ALL TIME (a raw Brain query), while `followup` could
# only ever pick up experiments younger than 14 days that still had their on-disk artifacts. So an
# experiment that aged out, or whose directory was reclaimed, counted against the cap FOREVER with
# no path to leave it. Measured 2026-08-21: 128 unevaluated against a cap of 25, **0 of them within
# 30 days** (range 50.9–67.5 days old) and **0 with an on-disk directory**, so the drain was 0 and
# the block was permanent. The research arm therefore planned nothing from ~2026-07-15 onward —
# which is exactly when objective anchors stopped (last anchor 2026-07-15 21:44) and why
# `human_calibration` has taken no new row since.
#
# Seventh instance of this project's signature bug: a gate whose clear path is blocked by the very
# thing it measures. The fix is NOT to weaken the cap — a genuinely pending experiment still counts,
# and that is what the cap is for.
EVALUABLE_WINDOW_DAYS = int(os.environ.get("ORCH_RESEARCH_EVALUABLE_WINDOW_DAYS", "14"))
OPEN_LIFECYCLES = {"planned", "active", "evaluable"}
LIFECYCLES = OPEN_LIFECYCLES | {"evaluated", "failed", "skipped"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_subjects (
  subject_id TEXT PRIMARY KEY,
  subject_family_id TEXT NOT NULL,
  canonical_target TEXT NOT NULL,
  task_type TEXT NOT NULL,
  spec_hash TEXT NOT NULL,
  base_sha TEXT,
  arm_set_hash TEXT NOT NULL,
  arms_json TEXT NOT NULL,
  profiles_json TEXT,
  lifecycle TEXT NOT NULL,
  exp_id TEXT,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  cooldown_until INTEGER,
  evaluable_ts INTEGER,
  evaluated_ts INTEGER,
  last_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_subjects_family_lifecycle
  ON research_subjects(subject_family_id, lifecycle, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_research_subjects_target
  ON research_subjects(canonical_target, task_type);
CREATE TABLE IF NOT EXISTS research_subject_experiments (
  exp_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  subject_family_id TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  cooldown_until INTEGER,
  last_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_subject_experiments_subject
  ON research_subject_experiments(subject_id, lifecycle, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_research_subject_experiments_family
  ON research_subject_experiments(subject_family_id, lifecycle);
CREATE TABLE IF NOT EXISTS research_subject_events (
  event_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  subject_id TEXT,
  subject_family_id TEXT,
  canonical_target TEXT,
  task_type TEXT,
  decision TEXT NOT NULL,
  reason TEXT,
  exp_id TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_subject_events_decision_ts
  ON research_subject_events(decision, ts);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def canonical_target(target: str) -> str:
    text = str(target or "").strip()
    if "#" not in text:
        return text.lower()
    repo, number = text.rsplit("#", 1)
    return f"{repo.strip().lower()}#{number.strip()}"


def normalize_spec(spec: str | None) -> str:
    lines = []
    for line in str(spec or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = re.sub(r"[ \t]+", " ", line.strip())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def subject_identity(
    target: str,
    task_type: str,
    spec: str | None,
    base_sha: str | None,
    arms: list[str] | tuple[str, ...],
    profiles: dict | list | None = None,
) -> dict:
    return subject_identity_from_hash(
        target,
        task_type,
        _hash(normalize_spec(spec)),
        base_sha,
        arms,
        profiles,
    )


def subject_identity_from_hash(
    target: str,
    task_type: str,
    spec_hash: str,
    base_sha: str | None,
    arms: list[str] | tuple[str, ...],
    profiles: dict | list | None = None,
) -> dict:
    """Canonical identity when safe event producers retain only a spec hash."""
    target_key = canonical_target(target)
    task_key = str(task_type or "implement").strip().lower()
    spec_hash = str(spec_hash or "").strip().lower()
    if spec_hash.startswith("sha256:"):
        spec_hash = spec_hash.split(":", 1)[1]
    if not re.fullmatch(r"[a-f0-9]{64}", spec_hash):
        raise ValueError("spec_hash must be a SHA-256 hex digest")
    base_key = str(base_sha or "unknown").strip().lower() or "unknown"
    arm_set = sorted({str(arm).strip().lower() for arm in arms if str(arm).strip()})
    profile_value = profiles or {}
    profile_json = json.dumps(profile_value, sort_keys=True, separators=(",", ":"))
    arm_payload = json.dumps(
        {"arms": arm_set, "profiles": profile_value},
        sort_keys=True,
        separators=(",", ":"),
    )
    arm_set_hash = _hash(arm_payload)
    family_payload = "|".join((target_key, task_key, spec_hash, base_key))
    family_id = f"subject-family:{_hash(family_payload)[:24]}"
    subject_payload = f"{family_payload}|{arm_set_hash}"
    return {
        "subject_id": f"subject:{_hash(subject_payload)[:24]}",
        "subject_family_id": family_id,
        "canonical_target": target_key,
        "task_type": task_key,
        "spec_hash": spec_hash,
        "base_sha": None if base_key == "unknown" else base_key,
        "arm_set_hash": arm_set_hash,
        "arms": arm_set,
        "arms_json": json.dumps(arm_set, separators=(",", ":")),
        "profiles_json": profile_json if profile_value else None,
    }


def completion_observation_id(
    subject_id: str | None, run_id: str, canonical_attempt_id: str | None
) -> str:
    """Stable across phase events; changes when subject/run/worker attempt changes."""
    return "sha256:" + _hash(
        f"{subject_id}|{run_id}|{canonical_attempt_id or 'unresolved'}"
    )


def unevaluated_experiment_ids(
    conn: sqlite3.Connection,
    *,
    window_days: int | None = None,
    now: float | None = None,
) -> set[str]:
    """Experiments still PENDING evaluation — lacking evaluation rows AND still reachable.

    Bounded by `EVALUABLE_WINDOW_DAYS` (see the constant for the deadlock this fixes): an
    experiment older than the window can never be picked up by `exp_abcd.followup`, so counting it
    as pending measures something unreachable and latches the cap shut.

    FAILS SAFE toward the cap: an experiment whose newest run carries NO timestamp has unknown age
    and is COUNTED. Unknown age must not silently unblock the arm — the conservative direction here
    is to keep the cap honest, not to widen it.
    """
    window = EVALUABLE_WINDOW_DAYS if window_days is None else int(window_days)
    cutoff = (time.time() if now is None else float(now)) - max(0, window) * 86400.0
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT r.experiment_id, MAX(r.ts) FROM runs r "
                "WHERE r.experiment_id IS NOT NULL AND r.experiment_id<>'' "
                "AND NOT EXISTS (SELECT 1 FROM evaluations e "
                "WHERE e.experiment_id=r.experiment_id) "
                "GROUP BY r.experiment_id"
            ).fetchall()
            if row[1] is None or float(row[1]) >= cutoff
        }
    except (sqlite3.Error, TypeError, ValueError):
        return set()


def _effective_lifecycle(conn: sqlite3.Connection, row: tuple) -> str:
    lifecycle, exp_id = str(row[0]), row[1]
    if exp_id:
        evaluated = conn.execute(
            "SELECT 1 FROM evaluations WHERE experiment_id=? LIMIT 1", (exp_id,)
        ).fetchone()
        if evaluated:
            return "evaluated"
    return lifecycle


def prior_experiment_count(
    identity: dict, *, conn: sqlite3.Connection | None = None
) -> int:
    """Independent subject-selection history, separate from quality outcomes."""
    db = conn or feedback._conn()
    close = conn is None
    if not _table_exists(db, "research_subject_experiments"):
        if close:
            db.close()
        return 0
    if identity.get("base_sha") is None:
        count = db.execute(
            "SELECT COUNT(*) FROM research_subject_experiments x "
            "JOIN research_subjects s ON s.subject_id=x.subject_id "
            "WHERE s.canonical_target=? AND s.task_type=? AND s.spec_hash=?",
            (
                identity["canonical_target"],
                identity["task_type"],
                identity["spec_hash"],
            ),
        ).fetchone()[0]
    else:
        count = db.execute(
            "SELECT COUNT(*) FROM research_subject_experiments WHERE subject_family_id=?",
            (identity["subject_family_id"],),
        ).fetchone()[0]
    if close:
        db.close()
    return int(count or 0)


def assess_candidate(
    *,
    target: str,
    task_type: str,
    spec: str | None,
    base_sha: str | None,
    arms: list[str] | tuple[str, ...],
    profiles: dict | list | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
    unevaluated_cap: int = DEFAULT_UNEVALUATED_CAP,
    per_subject_cap: int = DEFAULT_PER_SUBJECT_CAP,
) -> dict:
    """Read-only admission decision with explicit, machine-readable blockers."""
    identity = subject_identity(target, task_type, spec, base_sha, arms, profiles)
    db = conn or feedback._conn()
    close = conn is None
    now = int(now or time.time())
    try:
        backlog_ids = unevaluated_experiment_ids(db)
        backlog_count = len(backlog_ids)
        if max(0, int(unevaluated_cap)) <= backlog_count:
            return {
                **identity,
                "eligible": False,
                "reason": "unevaluated_backlog_cap",
                "unevaluated_backlog": backlog_count,
                "unevaluated_cap": max(0, int(unevaluated_cap)),
            }
        if not _table_exists(db, "research_subjects"):
            return {
                **identity,
                "eligible": True,
                "reason": "admitted",
                "unevaluated_backlog": backlog_count,
                "unevaluated_cap": max(0, int(unevaluated_cap)),
            }
        if identity.get("base_sha") is None:
            exact = db.execute(
                "SELECT x.lifecycle,x.exp_id,x.cooldown_until FROM research_subject_experiments x "
                "JOIN research_subjects s ON s.subject_id=x.subject_id "
                "WHERE s.canonical_target=? AND s.task_type=? AND s.spec_hash=? "
                "AND s.arm_set_hash=? ORDER BY x.updated_ts DESC LIMIT 1",
                (
                    identity["canonical_target"],
                    identity["task_type"],
                    identity["spec_hash"],
                    identity["arm_set_hash"],
                ),
            ).fetchone()
        else:
            exact = db.execute(
                "SELECT lifecycle,exp_id,cooldown_until FROM research_subject_experiments "
                "WHERE subject_id=? ORDER BY updated_ts DESC LIMIT 1",
                (identity["subject_id"],),
            ).fetchone()
        if exact:
            lifecycle = _effective_lifecycle(db, exact[:2])
            if lifecycle in OPEN_LIFECYCLES:
                return {
                    **identity,
                    "eligible": False,
                    "reason": f"subject_{lifecycle}",
                    "existing_exp_id": exact[1],
                    "unevaluated_backlog": backlog_count,
                    "unevaluated_cap": max(0, int(unevaluated_cap)),
                }
            if int(exact[2] or 0) > now:
                return {
                    **identity,
                    "eligible": False,
                    "reason": "subject_cooldown",
                    "cooldown_until": int(exact[2]),
                    "existing_exp_id": exact[1],
                    "unevaluated_backlog": backlog_count,
                    "unevaluated_cap": max(0, int(unevaluated_cap)),
                }
        family_open = 0
        if identity.get("base_sha") is None:
            family_rows = db.execute(
                "SELECT x.lifecycle,x.exp_id FROM research_subject_experiments x "
                "JOIN research_subjects s ON s.subject_id=x.subject_id "
                "WHERE s.canonical_target=? AND s.task_type=? AND s.spec_hash=?",
                (
                    identity["canonical_target"],
                    identity["task_type"],
                    identity["spec_hash"],
                ),
            ).fetchall()
        else:
            family_rows = db.execute(
                "SELECT lifecycle,exp_id FROM research_subject_experiments "
                "WHERE subject_family_id=?",
                (identity["subject_family_id"],),
            ).fetchall()
        for lifecycle, exp_id in family_rows:
            if _effective_lifecycle(db, (lifecycle, exp_id)) in OPEN_LIFECYCLES:
                family_open += 1
        if family_open >= max(1, int(per_subject_cap)):
            return {
                **identity,
                "eligible": False,
                "reason": "subject_backlog_cap",
                "subject_open_backlog": family_open,
                "per_subject_cap": max(1, int(per_subject_cap)),
                "unevaluated_backlog": backlog_count,
                "unevaluated_cap": max(0, int(unevaluated_cap)),
            }
        return {
            **identity,
            "eligible": True,
            "reason": "admitted",
            "unevaluated_backlog": backlog_count,
            "unevaluated_cap": max(0, int(unevaluated_cap)),
        }
    finally:
        if close:
            db.close()


def record_event(
    decision: str,
    *,
    identity: dict | None = None,
    target: str | None = None,
    task_type: str | None = None,
    reason: str | None = None,
    exp_id: str | None = None,
    metadata: dict | None = None,
    conn: sqlite3.Connection | None = None,
    ts: int | None = None,
) -> str:
    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    ts = int(ts or time.time())
    identity = identity or {}
    target_key = identity.get("canonical_target") or canonical_target(target or "")
    task_key = identity.get("task_type") or str(task_type or "implement")
    raw = "|".join(
        str(value or "")
        for value in (
            ts,
            decision,
            identity.get("subject_id"),
            target_key,
            task_key,
            reason,
            exp_id,
            json.dumps(metadata or {}, sort_keys=True),
        )
    )
    event_id = f"subject-event:{_hash(raw)[:24]}"
    db.execute(
        "INSERT OR REPLACE INTO research_subject_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            ts,
            identity.get("subject_id"),
            identity.get("subject_family_id"),
            target_key or None,
            task_key or None,
            str(decision),
            reason,
            exp_id,
            json.dumps(metadata, sort_keys=True) if metadata else None,
        ),
    )
    db.commit()
    if close:
        db.close()
    return event_id


def record_subject(
    identity: dict,
    *,
    lifecycle: str,
    exp_id: str,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
    reason: str | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
) -> None:
    lifecycle = str(lifecycle)
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"invalid research subject lifecycle: {lifecycle}")
    db = conn or feedback._conn()
    close = conn is None
    ensure_schema(db)
    now = int(now or time.time())
    cooldown_until = now + max(0, int(cooldown_hours)) * 3600
    existing = db.execute(
        "SELECT created_ts FROM research_subjects WHERE subject_id=?",
        (identity["subject_id"],),
    ).fetchone()
    db.execute(
        "INSERT OR REPLACE INTO research_subjects "
        "(subject_id,subject_family_id,canonical_target,task_type,spec_hash,base_sha," 
        "arm_set_hash,arms_json,profiles_json,lifecycle,exp_id,created_ts,updated_ts," 
        "cooldown_until,evaluable_ts,evaluated_ts,last_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity["subject_id"],
            identity["subject_family_id"],
            identity["canonical_target"],
            identity["task_type"],
            identity["spec_hash"],
            identity.get("base_sha"),
            identity["arm_set_hash"],
            identity["arms_json"],
            identity.get("profiles_json"),
            lifecycle,
            exp_id,
            int(existing[0]) if existing else now,
            now,
            cooldown_until,
            now if lifecycle == "evaluable" else None,
            now if lifecycle == "evaluated" else None,
            reason,
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO research_subject_experiments "
        "(exp_id,subject_id,subject_family_id,lifecycle,created_ts,updated_ts," 
        "cooldown_until,last_reason) VALUES (?,?,?,?,?,?,?,?)",
        (
            exp_id,
            identity["subject_id"],
            identity["subject_family_id"],
            lifecycle,
            now,
            now,
            cooldown_until,
            reason,
        ),
    )
    record_event(
        "launched" if lifecycle in OPEN_LIFECYCLES else lifecycle,
        identity=identity,
        reason=reason,
        exp_id=exp_id,
        conn=db,
        ts=now,
    )
    db.commit()
    if close:
        db.close()


def mark_lifecycle(
    exp_id: str,
    lifecycle: str,
    *,
    reason: str | None = None,
    conn: sqlite3.Connection | None = None,
    now: int | None = None,
) -> bool:
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"invalid research subject lifecycle: {lifecycle}")
    db = conn or feedback._conn()
    close = conn is None
    if not _table_exists(db, "research_subject_experiments"):
        if close:
            db.close()
        return False
    now = int(now or time.time())
    db.execute(
        "UPDATE research_subjects SET lifecycle=?,updated_ts=?,last_reason=?," 
        "evaluable_ts=CASE WHEN ?='evaluable' THEN ? ELSE evaluable_ts END," 
        "evaluated_ts=CASE WHEN ?='evaluated' THEN ? ELSE evaluated_ts END "
        "WHERE exp_id=?",
        (lifecycle, now, reason, lifecycle, now, lifecycle, now, exp_id),
    )
    db.execute(
        "UPDATE research_subject_experiments SET lifecycle=?,updated_ts=?,last_reason=? "
        "WHERE exp_id=?",
        (lifecycle, now, reason, exp_id),
    )
    changed = db.execute("SELECT changes()").fetchone()[0] > 0
    if changed:
        row = db.execute(
            "SELECT s.subject_id,s.subject_family_id,s.canonical_target,s.task_type "
            "FROM research_subject_experiments x JOIN research_subjects s "
            "ON s.subject_id=x.subject_id WHERE x.exp_id=?",
            (exp_id,),
        ).fetchone()
        identity = {
            "subject_id": row[0],
            "subject_family_id": row[1],
            "canonical_target": row[2],
            "task_type": row[3],
        }
        record_event(lifecycle, identity=identity, reason=reason, exp_id=exp_id, conn=db, ts=now)
    db.commit()
    if close:
        db.close()
    return changed


def effective_evidence_weights(
    *, conn: sqlite3.Connection | None = None, task_type: str | None = None
) -> dict[str, float]:
    """Return weights for explicitly linked research runs; legacy rows are omitted."""
    db = conn or feedback._conn()
    close = conn is None
    if not _table_exists(db, "research_subjects"):
        if close:
            db.close()
        return {}
    query = (
        "SELECT r.run_id,r.agent,x.subject_family_id FROM runs r "
        "JOIN research_subject_experiments x ON x.exp_id=r.experiment_id "
        "WHERE r.experiment_id IS NOT NULL"
    )
    params: list = []
    if task_type is not None:
        query += " AND r.task_type=?"
        params.append(task_type)
    rows = db.execute(query, params).fetchall()
    by_agent_subject: dict[tuple[str, str], list[str]] = defaultdict(list)
    for run_id, agent, family_id in rows:
        by_agent_subject[(str(agent or "unknown"), str(family_id))].append(str(run_id))
    weights = {
        run_id: 1.0 / len(run_ids)
        for run_ids in by_agent_subject.values()
        for run_id in run_ids
    }
    if close:
        db.close()
    return weights


def summary(
    *, conn: sqlite3.Connection | None = None, window_days: int = 90, now: int | None = None
) -> dict:
    db = conn or feedback._conn()
    close = conn is None
    now = int(now or time.time())
    since = now - max(1, int(window_days)) * 86400
    unevaluated_ids = unevaluated_experiment_ids(db)
    if not _table_exists(db, "research_subjects"):
        if close:
            db.close()
        return {
            "window_days": window_days,
            "registered_subjects": 0,
            "independent_subjects": 0,
            "unevaluated_backlog": len(unevaluated_ids),
            "unevaluated_cap": DEFAULT_UNEVALUATED_CAP,
            "unevaluated_backlog_cap_reached": len(unevaluated_ids)
            >= DEFAULT_UNEVALUATED_CAP,
            "lifecycle_counts": {},
            "true_task_type_distribution": {},
            "duplicate_rejections": 0,
            "rejections_by_reason": {},
            "research_production_collisions": 0,
            "effective_sample_count": 0.0,
            "registered_run_count": 0,
        }
    rows = db.execute(
        "SELECT subject_family_id,task_type FROM research_subjects"
    ).fetchall()
    experiment_rows = db.execute(
        "SELECT subject_family_id,lifecycle,exp_id FROM research_subject_experiments"
    ).fetchall()
    lifecycle_counts: Counter = Counter()
    task_counts: Counter = Counter()
    families = set()
    for family_id, task_type in rows:
        families.add(str(family_id))
        task_counts[str(task_type)] += 1
    for _family_id, lifecycle, exp_id in experiment_rows:
        lifecycle_counts[_effective_lifecycle(db, (lifecycle, exp_id))] += 1
    event_rows = db.execute(
        "SELECT decision,COALESCE(reason,'') FROM research_subject_events WHERE ts>=?",
        (since,),
    ).fetchall()
    rejection_reasons = Counter(
        reason for decision, reason in event_rows if decision == "rejected"
    )
    duplicate_reasons = {
        "duplicate_candidate_in_plan",
        "subject_active",
        "subject_evaluable",
        "subject_planned",
        "subject_cooldown",
        "subject_backlog_cap",
    }
    weights = effective_evidence_weights(conn=db)
    result = {
        "window_days": window_days,
        "registered_subjects": len(rows),
        "registered_experiments": len(experiment_rows),
        "independent_subjects": len(families),
        "unevaluated_backlog": len(unevaluated_ids),
        "unevaluated_cap": DEFAULT_UNEVALUATED_CAP,
        "unevaluated_backlog_cap_reached": len(unevaluated_ids)
        >= DEFAULT_UNEVALUATED_CAP,
        "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
        "true_task_type_distribution": dict(sorted(task_counts.items())),
        "duplicate_rejections": sum(
            count
            for reason, count in rejection_reasons.items()
            if reason in duplicate_reasons
        ),
        "rejections_by_reason": dict(sorted(rejection_reasons.items())),
        "research_production_collisions": sum(
            1
            for decision, reason in event_rows
            if decision == "rejected" and reason == "production_reserved"
        ),
        "effective_sample_count": round(sum(weights.values()), 6),
        "registered_run_count": len(weights),
    }
    if close:
        db.close()
    return result


def _selftest() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(feedback.SCHEMA)
    feedback._migrate_schema(conn)
    ensure_schema(conn)
    now = 2_000_000_000
    one = subject_identity("Owner/Repo#1", "testgen", "A  spec\n", "ABC", ["codex", "cursor"])
    same = subject_identity("owner/repo#1", "testgen", "A spec", "abc", ["cursor", "codex"])
    assert one["subject_id"] == same["subject_id"], (one, same)
    first = assess_candidate(
        target="Owner/Repo#1",
        task_type="testgen",
        spec="A spec",
        base_sha="abc",
        arms=["cursor", "codex"],
        conn=conn,
        now=now,
        unevaluated_cap=99,
    )
    assert first["eligible"] and first["reason"] == "admitted", first

    # THE UNEVALUATED CAP MUST COUNT ONLY WHAT THE DRAIN CAN REACH. Regression for the deadlock
    # measured 2026-08-21: 128 unevaluated experiments (all 50.9-67.5 days old, none with on-disk
    # artifacts) held the cap of 25 shut permanently, because `followup` can only pick up
    # experiments inside EVALUABLE_WINDOW_DAYS. The cap counted over all time, so the number could
    # never fall and the research arm planned nothing for five weeks.
    cap_now = now
    conn.execute(
        "INSERT INTO runs (run_id,ts,agent,task_type,target,experiment_id) VALUES (?,?,?,?,?,?)",
        ("run-fresh", cap_now - 2 * 86400, "codex", "testgen", "Owner/Repo#9", "exp-fresh"),
    )
    conn.execute(
        "INSERT INTO runs (run_id,ts,agent,task_type,target,experiment_id) VALUES (?,?,?,?,?,?)",
        ("run-stale", cap_now - 60 * 86400, "codex", "testgen", "Owner/Repo#8", "exp-stale"),
    )
    conn.execute(
        "INSERT INTO runs (run_id,ts,agent,task_type,target,experiment_id) VALUES (?,?,?,?,?,?)",
        ("run-nots", None, "codex", "testgen", "Owner/Repo#7", "exp-nots"),
    )
    pending = unevaluated_experiment_ids(conn, now=cap_now)
    # The 2-day-old one is genuinely pending and MUST still count — the cap is not being weakened.
    assert "exp-fresh" in pending, pending
    # The 60-day-old one is past the drain's reach; counting it is what latched the gate.
    assert "exp-stale" not in pending, pending
    # FAIL SAFE: unknown age counts, so a missing timestamp can never silently unblock the arm.
    assert "exp-nots" in pending, pending
    # ...and an evaluated experiment leaves the count by the original path, unchanged.
    conn.execute(
        "INSERT INTO evaluations (experiment_id,implementer,evaluator,score,ts) VALUES (?,?,?,?,?)",
        ("exp-fresh", "codex", "cursor", 7.0, cap_now),
    )
    assert "exp-fresh" not in unevaluated_experiment_ids(conn, now=cap_now), "evaluated must clear"
    # THE TWO WINDOWS ARE ONE NUMBER BY CONSTRUCTION, not by comment: followup defaults to it.
    import exp_abcd as _exp_abcd
    import inspect as _inspect
    assert (_inspect.signature(_exp_abcd.followup).parameters["max_age_days"].default
            == EVALUABLE_WINDOW_DAYS), "followup window drifted from the cap window"
    record_subject(one, lifecycle="active", exp_id="exp-one", conn=conn, now=now)
    second = assess_candidate(
        target="owner/repo#1",
        task_type="testgen",
        spec="A spec",
        base_sha="abc",
        arms=["codex", "cursor"],
        conn=conn,
        now=now + 1,
        unevaluated_cap=99,
    )
    assert not second["eligible"] and second["reason"] == "subject_active", second

    identities = [one]
    for index in (2, 3):
        identity = subject_identity(
            f"owner/repo#{index}", "testgen", f"spec {index}", "abc", ["codex"]
        )
        identities.append(identity)
        record_subject(identity, lifecycle="evaluated", exp_id=f"exp-{index}", conn=conn, now=now)
    for index in range(20):
        conn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"corr-{index}", now, "o/r#1", "testgen", "codex", "exp-one", "experimental"),
        )
    for index in (2, 3):
        conn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"ind-{index}", now, f"o/r#{index}", "testgen", "codex", f"exp-{index}", "experimental"),
        )
        conn.execute(
            "INSERT INTO evaluations (experiment_id,implementer,evaluator,score,rank,verdict,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"exp-{index}", "codex", "judge", 8.0, 1, None, now),
        )
    weights = effective_evidence_weights(conn=conn, task_type="testgen")
    assert len(weights) == 22 and abs(sum(weights.values()) - 3.0) < 1e-9, weights
    # A legacy experiment remains usable elsewhere but receives no invented subject mapping.
    conn.execute(
        "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
        "VALUES ('legacy',?,?,?,?,?,?)",
        (now, "o/r#legacy", "testgen", "codex", "legacy-exp", "experimental"),
    )
    assert "legacy" not in effective_evidence_weights(conn=conn)
    report = summary(conn=conn, now=now)
    assert report["effective_sample_count"] == 3.0, report
    assert report["registered_run_count"] == 22, report
    conn.close()
    print(
        "research_subjects.py selftest: OK (canonical identity, active/cooldown gate, "
        "legacy-safe provenance, independent-subject effective sample count)"
    )


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        _selftest()
    else:
        print(json.dumps(summary(), indent=2, sort_keys=True))
