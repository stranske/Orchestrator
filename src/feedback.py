#!/usr/bin/env python3
"""feedback.py — the orchestrator's LEARNING STORE (the long-term feedback loop's spine).

The orchestrator only gets better if every decision and its consequences are RETAINED and
re-examined as the dataset grows — not processed once and forgotten. This is that store.

Three data planes, joined by run_id / PR (design doc: PR #2350):
  1. DECISIONS  — what the orchestrator chose, and why (table `runs`).
  2. EXECUTION  — cost/tokens/latency + trace refs, pulled from LangSmith (tables `costs`,
                  `execution_traces`).
  3. OUTCOME    — did it succeed *to the user's goal* (table `outcomes`): the AC-anchored
                  verifier verdict (adjudicated), merged?, and — the un-gameable part —
                  DURABILITY, updated DAYS LATER (reverted / reworked / reopened / broke).
Plus `route_weights` (versioned learned priors → posteriors, so changes to the orchestrator
are catalogued and their effect attributable) and `evaluations` (the A/B/C/D cross-eval
matrix) and `human_calibration` (periodic human ground-truth that re-anchors the proxy).

LEARNING: `relearn()` does a Beta-Binomial prior→posterior update per (task_type, agent):
the hand-set route table is the PRIOR; accumulating verified-durable outcomes move the
posterior; with little data the prior dominates, as the dataset develops evidence wins.
Routing score = posterior_success / cost_per_success (capacity-per-verified-success).

SQLite at ~/.codex/orchestrator/feedback/orchestrator.db. `--selftest` runs offline.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import execution_profiles

ORCH = Path(__file__).resolve().parent
# The live SQLite store stays on LOCAL disk — Dropbox can corrupt a DB written mid-sync. The CODE lives
# in Code/Orchestrator (Dropbox); snapshot_json() writes a reviewable copy of the dataset INTO the project.
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator"))
DB_PATH = Path(os.environ.get("ORCH_FEEDBACK_DB", LOCAL_RUNTIME / "feedback" / "orchestrator.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, ts INTEGER, target TEXT, task_type TEXT,
  agent TEXT, mode TEXT, reasoning_level TEXT, model TEXT,
  decomposition TEXT, rationale TEXT, pr_number INTEGER, experiment_id TEXT,
  source TEXT, assignment TEXT, work_type TEXT, role_name TEXT,
  routing_metadata TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  run_id TEXT PRIMARY KEY, verifier_verdict TEXT, adjudicated_verdict TEXT,
  merged INTEGER, ci_status TEXT,
  durability TEXT DEFAULT 'pending',   -- pending|durable|reverted|reworked|reopened|broke_later
  durability_checked_ts INTEGER, notes TEXT, influenced_by_run_id TEXT
);
CREATE TABLE IF NOT EXISTS costs (
  run_id TEXT PRIMARY KEY, tokens_in INTEGER, tokens_out INTEGER,
  cost_usd REAL, latency_s REAL, source TEXT, pulled_ts INTEGER
);
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_key TEXT PRIMARY KEY, run_id TEXT, trace_id TEXT, trace_url TEXT,
  provider TEXT, model TEXT, operation TEXT, status TEXT,
  latency_s REAL, cost_usd REAL, source TEXT, raw_ref TEXT, pulled_ts INTEGER
);
CREATE TABLE IF NOT EXISTS execution_attempts (
  attempt_id TEXT PRIMARY KEY, run_id TEXT, attempt_ordinal INTEGER DEFAULT 1,
  operation_role TEXT NOT NULL DEFAULT 'unknown', profile_id TEXT,
  requested_provider TEXT, requested_model TEXT,
  selected_model TEXT, reported_model TEXT,
  resolved_provider TEXT, resolved_model TEXT, fallback_reason TEXT,
  runner_version TEXT, cli_version TEXT,
  status TEXT, tokens_in INTEGER, tokens_out INTEGER,
  latency_s REAL, cost_usd REAL, trace_key TEXT, source TEXT, raw_ref TEXT,
  started_ts INTEGER, completed_ts INTEGER, recorded_ts INTEGER
);
-- Safe, versioned method evidence.  This extends the Brain's run/attempt/outcome
-- planes; it is intentionally not a second feature or telemetry registry.
CREATE TABLE IF NOT EXISTS completion_events (
  event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
  run_id TEXT NOT NULL, attempt_id TEXT,
  capability_id TEXT, capability_version_id TEXT,
  event_type TEXT NOT NULL, phase TEXT NOT NULL, producer TEXT NOT NULL,
  status TEXT, validation_status TEXT NOT NULL DEFAULT 'accepted',
  payload_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  redaction_count INTEGER NOT NULL DEFAULT 0,
  created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS influence_edges (
  edge_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
  source_event_id TEXT, source_run_id TEXT,
  target_event_id TEXT, target_run_id TEXT NOT NULL,
  capability_id TEXT, capability_version_id TEXT,
  influence_type TEXT NOT NULL, influence_id TEXT NOT NULL,
  accepted INTEGER NOT NULL, counterfactual INTEGER NOT NULL DEFAULT 0,
  acceptance_gate_id TEXT,
  outcome_verdict TEXT, merged INTEGER, durability TEXT,
  created_ts INTEGER NOT NULL, propagated_ts INTEGER,
  metadata_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_weights (
  version INTEGER, ts INTEGER, task_type TEXT, agent TEXT,
  prior REAL, posterior REAL, n_obs INTEGER, success_rate REAL,
  cost_per_success REAL, score REAL, rationale TEXT, win_start INTEGER, win_end INTEGER,
  PRIMARY KEY (version, task_type, agent)
);
CREATE TABLE IF NOT EXISTS evaluations (
  experiment_id TEXT, implementer TEXT, evaluator TEXT,
  score REAL, rank INTEGER, verdict TEXT, ts INTEGER,
  PRIMARY KEY (experiment_id, implementer, evaluator)
);
-- Additive causal evaluation identity.  The legacy table remains the agent-level
-- compatibility/learning projection; v2 retains the exact arm, member, profile,
-- and evaluator attempt that produced the observation.
CREATE TABLE IF NOT EXISTS evaluations_v2 (
  experiment_id TEXT,
  implementer_arm_id TEXT, implementer_member_id TEXT,
  implementer_profile_id TEXT, implementation_agent TEXT,
  evaluator_id TEXT, evaluator_arm_id TEXT, evaluator_profile_id TEXT,
  evaluator_agent TEXT,
  score REAL, rank INTEGER, verdict TEXT, ts INTEGER,
  PRIMARY KEY (experiment_id, implementer_member_id, evaluator_id)
);
CREATE TABLE IF NOT EXISTS human_calibration (
  ts INTEGER, ref TEXT, human_verdict TEXT, note TEXT
);
-- GROWTH layer: the evaluation process itself learns what data it lacked.
CREATE TABLE IF NOT EXISTS evidence_gaps (        -- "what would have let me judge better?"
  ts INTEGER, ref TEXT, evaluator TEXT, gap TEXT, status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS evidence_types (       -- versioned registry of what we capture; prunable
  name TEXT PRIMARY KEY, added_ts INTEGER, influence INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active', rationale TEXT
);
-- item 16f (2026-07-08): per-run CLI resume identifiers, harvested from run logs at reconcile
-- time. A crash / quota cutoff / kill can then RESUME the session mid-task (claude --resume,
-- codex exec resume, ...) instead of re-dispatching cold — the durable-execution lesson from the
-- audit's R1/R2 field survey, translated to CLI fleets.
CREATE TABLE IF NOT EXISTS resume_tokens (
  run_id TEXT PRIMARY KEY, agent TEXT, kind TEXT, token TEXT,
  cwd TEXT, captured_ts INTEGER
);
-- item 16h (2026-07-08): interrupt-as-data owner questions. Agents record a product-level
-- question WITH a default and KEEP WORKING with that default (never pause/burn quota waiting);
-- unanswered questions auto-ratify their default at expiry (owner constraint: nothing may
-- accumulate a backlog). Answers feed future dispatch prompts for the same repo/target.
CREATE TABLE IF NOT EXISTS owner_questions (
  question_id TEXT PRIMARY KEY, ts INTEGER, run_id TEXT, target TEXT, repo TEXT,
  question TEXT, default_action TEXT, options TEXT,
  expires_ts INTEGER, status TEXT DEFAULT 'open',   -- open|answered|expired_default
  answer TEXT, answered_ts INTEGER
);
"""

# Effort sensitivity for the quality-weighted score. Multipliers are exponential so zero cost/tokens/latency
# remain the best case (1.0) and sparse telemetry degrades gracefully. Dollar cost is stored separately in
# route_weights.cost_per_success for backward-compatible reporting; token/latency inputs are retained in the
# route_weights rationale.
LAMBDA_COST = 0.15
LAMBDA_TOKEN_MTOK = 0.03
LAMBDA_LATENCY_MIN = 0.01
QUALITY_MAX = 10.0  # rubric scale; reward is normalized to [0,1] as score/QUALITY_MAX

# Prior strength (pseudo-observations) — how many real outcomes it takes for evidence to
# overtake the hand-set prior. Higher = more conservative (trust the prior longer).
PRIOR_STRENGTH = 8.0
# Floor ($/success) for the score division in relearn(): a near-zero measured cost_per_success
# must not catapult an arm x100 past its peers (2026-07-03 audit F1) — cheap stays cheap, bounded.
CPS_FLOOR = 0.01
# Recency half-life (days) for relearn_quality evidence weights — a stale outcome must not vote
# with yesterday's strength (agents get silent model/prompt bumps; without decay an agent keeps
# winning on old glory — audit item 16a / R3 routing survey). Complements the model-supersession
# discount, which only fires when the model IDENTITY visibly changed. Override with
# ORCH_RELEARN_HALF_LIFE_DAYS; <=0 disables decay.
DEFAULT_RELEARN_HALF_LIFE_DAYS = 30.0
VERIFIER_FAILURES = {
    "FAIL_HOLLOW",
    "FAIL_BROKEN",
    "FAIL_RUNTIME_AC",
    "ERROR_RUNTIME_AC",
}
SUPERSEDED_MODEL_WEIGHT = 0.5
SUCCESSFUL_ATTEMPT_STATUSES = {
    "complete",
    "completed",
    "merged",
    "ok",
    "pass",
    "passed",
    "succeeded",
    "success",
}
VALID_OPERATION_ROLES = {
    "worker",
    "evaluator",
    "verifier",
    "synthesizer",
    "role_backend",
    "replay",
    "unknown",
}
SYNTHETIC_ADAPTER_MODEL_RE = re.compile(
    r"^(?:claude|codex|cursor|gemini|vibe|agy|aider):",
    re.IGNORECASE,
)
LEGACY_EVALUATOR_OPERATIONS = {
    "evaluate",
    "evaluate_pr",
    "evaluate_pr_compare",
    "evaluator",
    "judge",
}
LEGACY_VERIFIER_OPERATIONS = {
    "verify",
    "verifier",
    "runtime_ac_verify",
}
LEGACY_SYNTHESIZER_OPERATIONS = {"summarize", "synthesize", "synthesizer"}
LEGACY_WORKER_OPERATIONS = {"implement", "implementation", "worker", "worker_dispatch"}

COMPLETION_EVENT_SCHEMA_VERSION = 1
MAX_COMPLETION_EVENT_BYTES = 12 * 1024
MAX_COMPLETION_LIST_ITEMS = 32
MAX_COMPLETION_STRING_CHARS = 256
MAX_COMPLETION_ARTIFACTS = 16
COMPLETION_PAYLOAD_FIELDS = {
    "changed_path_classes",
    "command_ids",
    "test_ids",
    "result_hashes",
    "capability_ids",
    "role_ids",
    "skill_ids",
    "workflow_ids",
    "acceptance_gate_ids",
    "retry_sequence",
    "root_cause_ids",
    "artifact_refs",
    "verification",
    "delivery",
    "durability",
    "panel_ids",
    "adjudication_id",
    "skill",
    "runtime_ac_gate",
    "result",
    "redacted_fields",
    "rejection_codes",
}
SENSITIVE_COMPLETION_FIELD_RE = re.compile(
    r"(?:^|_)(?:token|secret|password|credential|raw_prompt|prompt|document|content|output)(?:$|_)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+[a-z0-9._~+/-]{8,}|(?:gh[opurs]_|sk-|api[_-]?key)[a-z0-9._-]{8,})",
    re.IGNORECASE,
)
VALID_INFLUENCE_TYPES = {
    "role",
    "skill",
    "workflow",
    "capability",
    "verifier",
    "experiment",
}
VALID_COMPLETION_EVENT_TYPES = {
    "decision",
    "attempt",
    "completion",
    "verification",
    "delivery",
    "durability",
    "role",
    "skill",
    "workflow",
    "panel",
}
VALID_COMPLETION_PHASES = {
    "trigger",
    "decision",
    "execution",
    "artifact",
    "verification",
    "delivery",
    "outcome",
    "durability",
}
VALID_COMPLETION_PRODUCERS = {
    "feedback.record_run",
    "feedback.execution_attempt",
    "feedback.record_outcome",
    "feedback.record_role_run",
    "feedback.record_skill_invocation",
    "dispatcher",
    "exp_abcd",
    "roles",
    "adversarial",
    "tick",
    "outcomes",
    "keepalive_outcomes",
    "ledger_reconcile",
    "local_verify",
    "runtime_ac",
    "runtime_ac_gate",
    "langsmith",
    "ccusage",
    "ledger",
    "orchestrator",
    "orchestrator_local",
    "orchestrator_remote",
    "keepalive",
    "workflow",
    "selftest",
    "other",
}
VALID_COMPLETION_STATUSES = {
    "recorded",
    "running",
    "pending",
    "succeeded",
    "failed",
    "pass",
    "fail",
    "needs_review",
    "merged",
    "durable",
    "reverted",
    "reworked",
    "reopened",
    "broke_later",
    "abandoned",
    "planned",
    "skipped",
    "error",
    "unknown",
}
COMPLETION_NESTED_FIELDS = {
    "verification": {
        "verifier_verdict",
        "adjudicated_verdict",
        "verifier_ids",
        "result_hashes",
    },
    "delivery": {
        "pr_number",
        "target_id",
        "task_type",
        "experiment_id",
        "merged",
        "ci_status",
    },
    "durability": {"status", "checked_ts"},
    "result": {
        "outcome_verdict",
        "influence_type",
        "influence_id",
        "influenced_run_id",
        "operation_role",
        "status",
        "trace_key_hash",
        "action_id",
        "decision_source_id",
        "backend_run_id",
        "proposal_hash",
        "notes_hash",
        "version_hash",
        "selector_status",
        "selector_reason_id",
        "matched",
        "invoked",
        "accepted",
        "disagreement",
        "profile_fit_id",
        "evidence_readiness_id",
    },
    "skill": {"skill_id", "version_hash", "phase", "result", "accepted"},
    "runtime_ac_gate": {
        "schema_version",
        "gate_event_id",
        "target",
        "required",
        "dry_run",
        "eligibility_source",
        "eligibility_refs",
        "spec_path",
        "spec_hash",
        "spec_path_matches_target",
        "gate_status",
        "blocking",
        "terminal_reason",
        "closer_run_id",
        "verifier_run_id",
        "verifier_verdict",
        "downstream_verdict",
        "downstream_merged",
        "downstream_durability",
        "materialization_source",
        "materialization_status",
        "materialization_run_id",
    },
}
COMPLETION_RETRY_FIELDS = {
    "attempt_ordinal",
    "operation_role",
    "profile_id",
    "requested_provider",
    "requested_model",
    "selected_model",
    "reported_model",
    "resolved_provider",
    "resolved_model",
    "fallback_reason_id",
    "runner_version",
    "cli_version",
    "status",
}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.executescript(SCHEMA)
    _migrate_schema(c)
    # Additive v2 profile tables live in the same durable Brain.  Import lazily
    # to keep execution_profiles independent from this connection factory.
    execution_profiles.ensure_schema(c)
    c.commit()
    return c


def _migrate_schema(c: sqlite3.Connection) -> None:
    """Guarded migrations for existing local feedback stores."""
    cols = {row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()}
    if "model" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN model TEXT")
    if "source" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN source TEXT")
    if "assignment" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN assignment TEXT")
    if "work_type" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN work_type TEXT")
    if "role_name" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN role_name TEXT")
    if "routing_metadata" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN routing_metadata TEXT")
    tables = {
        row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "outcomes" in tables:
        outcome_cols = {row[1] for row in c.execute("PRAGMA table_info(outcomes)").fetchall()}
        if "influenced_by_run_id" not in outcome_cols:
            c.execute("ALTER TABLE outcomes ADD COLUMN influenced_by_run_id TEXT")
        if "failure_class" not in outcome_cols:
            # 2026-07-08 audit item 9 (two-tier enum): transient-infra != task failure.
            # 'transient_infra' rows are EXCLUDED from route-weight learning so an agent killed
            # by the environment (signal-killed wrapper, env crash) never trains as a capability
            # FAIL. Written by mark_transient_infra() from done-marker rc evidence.
            c.execute("ALTER TABLE outcomes ADD COLUMN failure_class TEXT")
    if "execution_attempts" in tables:
        attempt_cols = {
            row[1] for row in c.execute("PRAGMA table_info(execution_attempts)").fetchall()
        }
        attempt_columns = {
            "run_id": "TEXT",
            "attempt_ordinal": "INTEGER DEFAULT 1",
            "operation_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "profile_id": "TEXT",
            "requested_provider": "TEXT",
            "requested_model": "TEXT",
            "selected_model": "TEXT",
            "reported_model": "TEXT",
            "resolved_provider": "TEXT",
            "resolved_model": "TEXT",
            "fallback_reason": "TEXT",
            "runner_version": "TEXT",
            "cli_version": "TEXT",
            "status": "TEXT",
            "tokens_in": "INTEGER",
            "tokens_out": "INTEGER",
            "latency_s": "REAL",
            "cost_usd": "REAL",
            "trace_key": "TEXT",
            "source": "TEXT",
            "raw_ref": "TEXT",
            "started_ts": "INTEGER",
            "completed_ts": "INTEGER",
            "recorded_ts": "INTEGER",
        }
        for name, declaration in attempt_columns.items():
            if name not in attempt_cols:
                c.execute(f"ALTER TABLE execution_attempts ADD COLUMN {name} {declaration}")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_attempts_run_role "
            "ON execution_attempts(run_id, operation_role, status)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_attempts_profile "
            "ON execution_attempts(profile_id, resolved_provider, resolved_model)"
        )
    if "evaluations_v2" in tables:
        v2_cols = {row[1] for row in c.execute("PRAGMA table_info(evaluations_v2)").fetchall()}
        required_v2 = {
            "experiment_id",
            "implementer_arm_id",
            "implementer_member_id",
            "implementer_profile_id",
            "implementation_agent",
            "evaluator_id",
            "evaluator_arm_id",
            "evaluator_profile_id",
            "evaluator_agent",
            "score",
            "rank",
            "verdict",
            "ts",
        }
        if not required_v2.issubset(v2_cols):
            # An early local prototype used arm-only keys and could overwrite two
            # members of one parallel arm. Preserve those rows under conservative
            # legacy member identities while rebuilding the exact schema.
            c.execute("DROP TABLE IF EXISTS evaluations_v2_rebuild")
            c.execute(
                "CREATE TABLE evaluations_v2_rebuild ("
                "experiment_id TEXT, implementer_arm_id TEXT, implementer_member_id TEXT, "
                "implementer_profile_id TEXT, implementation_agent TEXT, "
                "evaluator_id TEXT, evaluator_arm_id TEXT, evaluator_profile_id TEXT, "
                "evaluator_agent TEXT, score REAL, rank INTEGER, verdict TEXT, ts INTEGER, "
                "PRIMARY KEY (experiment_id, implementer_member_id, evaluator_id))"
            )

            def source_col(*names: str, fallback: str = "NULL") -> str:
                return next((name for name in names if name in v2_cols), fallback)

            old_count = c.execute("SELECT COUNT(*) FROM evaluations_v2").fetchone()[0]
            experiment_expr = source_col("experiment_id", fallback="'legacy-experiment:' || rowid")
            arm_expr = source_col(
                "implementer_arm_id", "implementer_arm", fallback="'legacy-arm:' || rowid"
            )
            member_source = source_col(
                "implementer_member_id", "arm_id", "implementer_arm_id", "implementer_arm"
            )
            member_expr = f"COALESCE({member_source}, 'legacy-member:' || rowid)"
            profile_expr = source_col("implementer_profile_id", "profile_id")
            implementation_expr = source_col("implementation_agent")
            evaluator_source = source_col("evaluator_id", "evaluator_arm")
            evaluator_expr = f"COALESCE({evaluator_source}, 'legacy-evaluator:' || rowid)"
            evaluator_arm_expr = source_col("evaluator_arm_id", "evaluator_arm")
            evaluator_profile_expr = source_col("evaluator_profile_id")
            evaluator_agent_expr = source_col("evaluator_agent")
            score_expr = source_col("score")
            rank_expr = source_col("rank")
            verdict_expr = source_col("verdict")
            ts_expr = source_col("ts")
            c.execute(
                "INSERT INTO evaluations_v2_rebuild "
                "(experiment_id, implementer_arm_id, implementer_member_id, "
                "implementer_profile_id, implementation_agent, evaluator_id, "
                "evaluator_arm_id, evaluator_profile_id, evaluator_agent, "
                "score, rank, verdict, ts) "
                f"SELECT {experiment_expr}, {arm_expr}, {member_expr}, "
                f"{profile_expr}, {implementation_expr}, {evaluator_expr}, "
                f"{evaluator_arm_expr}, {evaluator_profile_expr}, {evaluator_agent_expr}, "
                f"{score_expr}, {rank_expr}, {verdict_expr}, {ts_expr} FROM evaluations_v2"
            )
            new_count = c.execute("SELECT COUNT(*) FROM evaluations_v2_rebuild").fetchone()[0]
            if new_count != old_count:
                raise RuntimeError(
                    "evaluations_v2 migration refused row loss: "
                    f"before={old_count} after={new_count}"
                )
            c.execute("DROP TABLE evaluations_v2")
            c.execute("ALTER TABLE evaluations_v2_rebuild RENAME TO evaluations_v2")
    if "completion_events" in tables:
        event_cols = {
            row[1] for row in c.execute("PRAGMA table_info(completion_events)").fetchall()
        }
        for name in ("capability_id", "capability_version_id"):
            if name not in event_cols:
                c.execute(f"ALTER TABLE completion_events ADD COLUMN {name} TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_completion_events_run_phase "
            "ON completion_events(run_id,phase,validation_status,updated_ts)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_completion_events_attempt "
            "ON completion_events(attempt_id,event_type,phase)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_completion_events_capability "
            "ON completion_events(capability_id,capability_version_id,run_id,phase)"
        )
    if "influence_edges" in tables:
        edge_cols = {row[1] for row in c.execute("PRAGMA table_info(influence_edges)").fetchall()}
        for name in ("capability_id", "capability_version_id"):
            if name not in edge_cols:
                c.execute(f"ALTER TABLE influence_edges ADD COLUMN {name} TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_influence_edges_target "
            "ON influence_edges(target_run_id,accepted,propagated_ts)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_influence_edges_source "
            "ON influence_edges(source_run_id,influence_type,influence_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_influence_edges_capability "
            "ON influence_edges(capability_id,capability_version_id,target_run_id,accepted)"
        )
    c.execute(
        "UPDATE runs SET assignment = CASE WHEN source='keepalive' THEN 'assigned' "
        "ELSE 'experimental' END WHERE assignment IS NULL"
    )


def _derive_source(run_id: str, mode: str | None, source: str | None = None) -> str | None:
    if source:
        return source
    rid = run_id or ""
    if rid.startswith("keepalive:"):
        return "keepalive"
    if mode == "remote" or rid.startswith("remote:"):
        return "orchestrator_remote"
    if mode in {"local", "offload", "composer", "full"}:
        return "orchestrator_local"
    return None


def _derive_assignment(source: str | None, assignment: str | None = None) -> str:
    if assignment:
        return assignment
    return "assigned" if source == "keepalive" else "experimental"


def _json_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if not value:
        return None
    return json.dumps(value, sort_keys=True)


def _completion_hash(value) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _bounded_completion_string(value, redacted: list[str], path: str) -> str:
    text = str(value or "")
    if SECRET_VALUE_RE.search(text):
        redacted.append(path)
        return "[REDACTED]"
    if len(text) > MAX_COMPLETION_STRING_CHARS:
        redacted.append(f"{path}:truncated")
        return text[:MAX_COMPLETION_STRING_CHARS]
    return text


def _sanitize_completion_value(value, redacted: list[str], path: str):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_completion_string(value, redacted, path)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if len(items) > MAX_COMPLETION_LIST_ITEMS:
            redacted.append(f"{path}:bounded")
        return [
            _sanitize_completion_value(item, redacted, f"{path}[{index}]")
            for index, item in enumerate(items[:MAX_COMPLETION_LIST_ITEMS])
        ]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if SENSITIVE_COMPLETION_FIELD_RE.search(name):
                redacted.append(child_path)
                continue
            clean[_bounded_completion_string(name, redacted, child_path)] = (
                _sanitize_completion_value(item, redacted, child_path)
            )
        return clean
    return _bounded_completion_string(value, redacted, path)


def _safe_identifier(value, redacted: list[str], path: str) -> str:
    text = _bounded_completion_string(value, redacted, path).strip()
    if text == "[REDACTED]":
        return text
    # IDs may contain pytest path separators, but commands/prose must be retained
    # only by hash so the event cannot become an unbounded command-output sink.
    if not re.fullmatch(r"[A-Za-z0-9_.:/#@+-]{1,192}", text):
        redacted.append(f"{path}:hashed")
        return _completion_hash(text)
    return text


def _sanitize_artifact_refs(value, redacted: list[str]) -> list[dict]:
    rows = list(value or []) if isinstance(value, (list, tuple)) else [value]
    if len(rows) > MAX_COMPLETION_ARTIFACTS:
        redacted.append("artifact_refs:bounded")
    result = []
    for index, row in enumerate(rows[:MAX_COMPLETION_ARTIFACTS]):
        if isinstance(row, str):
            result.append({"artifact_id": _completion_hash(row), "kind": "reference"})
            redacted.append(f"artifact_refs[{index}]:hashed")
            continue
        if not isinstance(row, dict):
            result.append({"artifact_id": _completion_hash(row), "kind": "reference"})
            redacted.append(f"artifact_refs[{index}]:hashed")
            continue
        allowed = {"artifact_id", "kind", "content_hash", "ref_class"}
        unsafe = sorted(set(row) - allowed)
        if unsafe:
            redacted.extend(f"artifact_refs[{index}].{key}" for key in unsafe)
        clean: dict[str, Any] = {}
        for key in sorted(allowed & set(row)):
            item = row.get(key)
            if item in (None, ""):
                continue
            if key == "content_hash":
                text = str(item)
                clean[key] = (
                    text
                    if re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", text)
                    else _completion_hash(text)
                )
            else:
                clean[key] = _safe_identifier(item, redacted, f"artifact_refs[{index}].{key}")
        if "artifact_id" not in clean:
            clean["artifact_id"] = _completion_hash(row)
        result.append(clean)
    return result


def _sanitize_result_hashes(value, redacted: list[str], path: str) -> dict:
    if not isinstance(value, dict):
        redacted.append(f"{path}:hashed")
        return {"result": _completion_hash(value)}
    result = {}
    for index, (key, item) in enumerate(sorted(value.items(), key=lambda pair: str(pair[0]))):
        if index >= MAX_COMPLETION_LIST_ITEMS:
            redacted.append(f"{path}:bounded")
            break
        name = _safe_identifier(key, redacted, f"{path}.key")
        text = str(item or "")
        result[name] = (
            text if re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", text) else _completion_hash(item)
        )
    return result


def _sanitize_completion_payload(payload: dict | None) -> tuple[dict, str, int]:
    raw = dict(payload or {})
    redacted: list[str] = []
    rejection_codes = []
    clean: dict[str, Any] = {}
    for key, value in sorted(raw.items()):
        name = str(key)
        if name not in COMPLETION_PAYLOAD_FIELDS:
            if SENSITIVE_COMPLETION_FIELD_RE.search(name):
                redacted.append(name)
            else:
                rejection_codes.append(f"unknown_field:{name}")
            continue
        if name == "artifact_refs":
            clean[name] = _sanitize_artifact_refs(value, redacted)
        elif name == "result_hashes":
            values = list(value or []) if isinstance(value, (list, tuple, set)) else [value]
            if len(values) > MAX_COMPLETION_LIST_ITEMS:
                redacted.append("result_hashes:bounded")
            clean[name] = []
            for index, item in enumerate(values[:MAX_COMPLETION_LIST_ITEMS]):
                text = str(item or "")
                clean[name].append(
                    text
                    if re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", text)
                    else _completion_hash(item)
                )
        elif name in COMPLETION_NESTED_FIELDS:
            if not isinstance(value, dict):
                rejection_codes.append(f"invalid_object:{name}")
                continue
            unknown_nested = sorted(set(value) - COMPLETION_NESTED_FIELDS[name])
            if unknown_nested:
                rejection_codes.extend(f"unknown_field:{name}.{item}" for item in unknown_nested)
                continue
            nested = _sanitize_completion_value(value, redacted, name)
            if name == "verification" and "result_hashes" in nested:
                nested["result_hashes"] = _sanitize_result_hashes(
                    nested["result_hashes"], redacted, "verification.result_hashes"
                )
            for nested_key in ("trace_key_hash", "proposal_hash", "notes_hash", "version_hash"):
                if name == "result" and nested.get(nested_key):
                    text = str(nested[nested_key])
                    nested[nested_key] = (
                        text
                        if re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", text)
                        else _completion_hash(text)
                    )
            clean[name] = nested
        elif name == "retry_sequence":
            rows = list(value or []) if isinstance(value, (list, tuple)) else [value]
            if len(rows) > MAX_COMPLETION_LIST_ITEMS:
                redacted.append("retry_sequence:bounded")
            clean_rows = []
            for index, row in enumerate(rows[:MAX_COMPLETION_LIST_ITEMS]):
                if not isinstance(row, dict):
                    rejection_codes.append(f"invalid_object:retry_sequence[{index}]")
                    continue
                unknown_nested = sorted(set(row) - COMPLETION_RETRY_FIELDS)
                if unknown_nested:
                    rejection_codes.extend(
                        f"unknown_field:retry_sequence[{index}].{item}" for item in unknown_nested
                    )
                    continue
                clean_rows.append(
                    _sanitize_completion_value(row, redacted, f"retry_sequence[{index}]")
                )
            clean[name] = clean_rows
        elif name in {
            "changed_path_classes",
            "command_ids",
            "test_ids",
            "capability_ids",
            "role_ids",
            "skill_ids",
            "workflow_ids",
            "acceptance_gate_ids",
            "root_cause_ids",
            "panel_ids",
        }:
            values = list(value or []) if isinstance(value, (list, tuple, set)) else [value]
            if len(values) > MAX_COMPLETION_LIST_ITEMS:
                redacted.append(f"{name}:bounded")
            clean[name] = [
                _safe_identifier(item, redacted, f"{name}[{index}]")
                for index, item in enumerate(values[:MAX_COMPLETION_LIST_ITEMS])
                if item not in (None, "")
            ]
        else:
            clean[name] = _sanitize_completion_value(value, redacted, name)
    if rejection_codes:
        return (
            {
                "rejection_codes": rejection_codes[:MAX_COMPLETION_LIST_ITEMS],
                "redacted_fields": sorted(set(redacted))[:MAX_COMPLETION_LIST_ITEMS],
            },
            "rejected",
            len(redacted),
        )
    if redacted:
        clean["redacted_fields"] = sorted(set(redacted))[:MAX_COMPLETION_LIST_ITEMS]
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_COMPLETION_EVENT_BYTES:
        return (
            {
                "rejection_codes": ["event_size_limit"],
                "redacted_fields": sorted(set(redacted))[:MAX_COMPLETION_LIST_ITEMS],
            },
            "rejected",
            len(redacted),
        )
    return clean, ("redacted" if redacted else "accepted"), len(redacted)


def _normalize_completion_producer(producer: str) -> str:
    value = str(producer or "").strip().lower().replace("-", "_")
    aliases = {
        "feedback.execution_attempt": "feedback.execution_attempt",
        "feedback.record_run": "feedback.record_run",
        "feedback.record_outcome": "feedback.record_outcome",
        "feedback.record_role_run": "feedback.record_role_run",
        "feedback.record_skill_invocation": "feedback.record_skill_invocation",
    }
    if value in aliases:
        return aliases[value]
    if value in VALID_COMPLETION_PRODUCERS:
        return value
    if value.startswith("langsmith"):
        return "langsmith"
    if value.startswith("ledger"):
        return "ledger"
    if value.startswith("keepalive"):
        return "keepalive"
    return "other"


def _normalize_completion_status(status: str | None) -> str:
    value = str(status or "").strip().lower().replace("-", "_")
    if value in VALID_COMPLETION_STATUSES:
        return value
    if value in SUCCESSFUL_ATTEMPT_STATUSES or value.startswith("pass"):
        return "succeeded" if value not in {"pass", "passed"} else "pass"
    if value in VERIFIER_FAILURES or value.startswith(("fail", "error")):
        return "fail"
    if value in {"blocked", "block"}:
        return "fail"
    if value in {"concerns", "needsreview", "needs_review"}:
        return "needs_review"
    return "unknown" if value else "recorded"


def _completion_event_id(
    run_id: str,
    event_type: str,
    phase: str,
    producer: str,
    attempt_id: str | None = None,
    capability_id: str | None = None,
    capability_version_id: str | None = None,
) -> str:
    identity = "|".join(
        str(item or "")
        for item in (
            COMPLETION_EVENT_SCHEMA_VERSION,
            run_id,
            attempt_id,
            event_type,
            phase,
            producer,
            capability_id,
            capability_version_id,
        )
    )
    return "event:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _merge_completion_payload(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    for key, value in incoming.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            combined = merged[key] + value
            seen, unique = set(), []
            for item in combined:
                marker = json.dumps(item, sort_keys=True, default=str)
                if marker not in seen:
                    seen.add(marker)
                    unique.append(item)
            merged[key] = unique[:MAX_COMPLETION_LIST_ITEMS]
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _latest_completion_event_id(c: sqlite3.Connection, run_id: str) -> str | None:
    row = c.execute(
        "SELECT event_id FROM completion_events WHERE run_id=? "
        "ORDER BY CASE phase WHEN 'durability' THEN 5 WHEN 'outcome' THEN 4 "
        "WHEN 'verification' THEN 3 WHEN 'execution' THEN 2 ELSE 1 END DESC, "
        "updated_ts DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return row[0] if row else None


def _record_completion_event_in_conn(
    c: sqlite3.Connection,
    run_id: str,
    *,
    event_type: str,
    phase: str,
    producer: str,
    attempt_id: str | None = None,
    capability_id: str | None = None,
    capability_version_id: str | None = None,
    status: str | None = None,
    payload: dict | None = None,
    event_id: str | None = None,
    timestamp: int | None = None,
) -> dict:
    if not run_id or not event_type or not phase or not producer:
        raise ValueError("completion event requires run_id, event_type, phase, and producer")
    event_type = str(event_type).strip().lower().replace("-", "_")
    phase = str(phase).strip().lower().replace("-", "_")
    if event_type not in VALID_COMPLETION_EVENT_TYPES:
        raise ValueError(f"invalid completion event_type={event_type!r}")
    if phase not in VALID_COMPLETION_PHASES:
        raise ValueError(f"invalid completion phase={phase!r}")
    producer = _normalize_completion_producer(producer)
    status = _normalize_completion_status(status)
    if len(str(run_id)) > 512 or (attempt_id and len(str(attempt_id)) > 512):
        raise ValueError("completion event identity exceeds 512 characters")
    if bool(capability_id) != bool(capability_version_id):
        raise ValueError("completion capability lineage requires both identity and version")
    event_id = event_id or _completion_event_id(
        run_id,
        event_type,
        phase,
        producer,
        attempt_id,
        capability_id,
        capability_version_id,
    )
    prior = c.execute(
        "SELECT payload_json,created_ts,capability_id,capability_version_id "
        "FROM completion_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if prior and (prior[2], prior[3]) != (capability_id, capability_version_id):
        raise ValueError(f"immutable capability identity changed for {event_id}")
    incoming, validation, redactions = _sanitize_completion_payload(payload)
    if prior and validation != "rejected":
        try:
            incoming = _merge_completion_payload(json.loads(prior[0]), incoming)
            incoming, validation, redactions = _sanitize_completion_payload(incoming)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    encoded = json.dumps(incoming, sort_keys=True, separators=(",", ":"), default=str)
    content_hash = _completion_hash(encoded)
    now = int(timestamp or time.time())
    created = int(prior[1]) if prior else now
    c.execute(
        "INSERT OR REPLACE INTO completion_events "
        "(event_id,schema_version,run_id,attempt_id,capability_id,capability_version_id,"
        "event_type,phase,producer,status,"
        "validation_status,payload_json,content_hash,redaction_count,created_ts,updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            COMPLETION_EVENT_SCHEMA_VERSION,
            run_id,
            attempt_id,
            capability_id,
            capability_version_id,
            event_type,
            phase,
            producer,
            status,
            validation,
            encoded,
            content_hash,
            redactions,
            created,
            now,
        ),
    )
    c.execute(
        "UPDATE influence_edges SET target_event_id=? "
        "WHERE target_run_id=? AND target_event_id IS NULL",
        (event_id, run_id),
    )
    return {
        "event_id": event_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "validation_status": validation,
        "content_hash": content_hash,
        "bytes": len(encoded.encode("utf-8")),
    }


def record_completion_event(
    run_id: str,
    *,
    event_type: str = "completion",
    phase: str = "execution",
    producer: str = "orchestrator",
    attempt_id: str | None = None,
    capability_id: str | None = None,
    capability_version_id: str | None = None,
    status: str | None = None,
    payload: dict | None = None,
    event_id: str | None = None,
    timestamp: int | None = None,
) -> dict:
    """Persist one bounded/redacted event in the existing feedback Brain."""
    # ~120 completion events/day; coalesced daily for the same reason as feedback-store. Placed on
    # the PUBLIC entrypoint rather than `_record_completion_event_in_conn`, so the internal helper
    # (called several times inside a single record_run) stays free of ledger work.
    _capability_daily_heartbeat(
        "completion-event-lineage", "invocation", ref="record_completion_event"
    )
    with _conn() as c:
        return _record_completion_event_in_conn(
            c,
            run_id,
            event_type=event_type,
            phase=phase,
            producer=producer,
            attempt_id=attempt_id,
            capability_id=capability_id,
            capability_version_id=capability_version_id,
            status=status,
            payload=payload,
            event_id=event_id,
            timestamp=timestamp,
        )


RUNTIME_AC_GATE_SCHEMA_VERSION = 1
RUNTIME_AC_GATE_STATUSES = {
    "required",
    "planned",
    "missing_spec",
    "executed",
    "skipped",
    "error",
    "materialized",
    "materialization_failed",
}
RUNTIME_AC_ELIGIBILITY_SOURCES = {"none", "label", "spec", "label+spec"}


def _runtime_ac_completion_status(gate_status: str, verifier_verdict: str | None) -> str:
    if gate_status == "executed":
        verdict = str(verifier_verdict or "").strip().upper()
        return {"PASS": "pass", "NEEDS_REVIEW": "needs_review", "FAIL": "fail"}.get(
            verdict, "unknown"
        )
    return {
        "required": "pending",
        "planned": "planned",
        "missing_spec": "fail",
        "skipped": "skipped",
        "error": "error",
        "materialized": "succeeded",
        "materialization_failed": "fail",
    }[gate_status]


def _runtime_ac_event_id(target: str, gate_status: str, timestamp_ns: int) -> str:
    identity = (
        f"runtime-ac-gate-v{RUNTIME_AC_GATE_SCHEMA_VERSION}|{target}|{gate_status}|{timestamp_ns}"
    )
    return "event:runtime-ac-gate:" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _record_runtime_ac_gate_event_in_conn(
    c: sqlite3.Connection,
    *,
    target: str,
    gate_status: str,
    required: bool,
    dry_run: bool,
    eligibility_source: str = "none",
    eligibility_refs: list[str] | tuple[str, ...] | None = None,
    spec_path: str | None = None,
    spec_hash: str | None = None,
    spec_path_matches_target: bool | None = None,
    blocking: bool = False,
    terminal_reason: str | None = None,
    closer_run_id: str | None = None,
    verifier_run_id: str | None = None,
    verifier_verdict: str | None = None,
    materialization_source: str | None = None,
    materialization_status: str | None = None,
    materialization_run_id: str | None = None,
    event_id: str | None = None,
    timestamp: int | None = None,
) -> dict:
    """Persist one exact runtime-AC gate observation in the canonical event plane.

    This is deliberately an extension of ``completion_events`` rather than a
    second telemetry log.  The closer run id is the durable join to delivery and
    durability outcomes; those late-arriving fields are read back below.
    """
    target = str(target or "").strip()
    gate_status = str(gate_status or "").strip().lower()
    eligibility_source = str(eligibility_source or "none").strip().lower()
    if not target:
        raise ValueError("runtime-AC gate event requires target")
    if gate_status not in RUNTIME_AC_GATE_STATUSES:
        raise ValueError(f"invalid runtime-AC gate status={gate_status!r}")
    if eligibility_source not in RUNTIME_AC_ELIGIBILITY_SOURCES:
        raise ValueError(f"invalid runtime-AC eligibility source={eligibility_source!r}")
    if spec_hash and not re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", str(spec_hash)):
        raise ValueError("runtime-AC spec_hash must be a SHA-256 digest")
    normalized_verdict = str(verifier_verdict or "").strip().upper() or None
    if gate_status == "executed" and normalized_verdict not in {
        "PASS",
        "NEEDS_REVIEW",
        "FAIL",
    }:
        raise ValueError("executed runtime-AC event requires PASS/NEEDS_REVIEW/FAIL verdict")

    downstream = None
    if closer_run_id:
        downstream = c.execute(
            "SELECT COALESCE(adjudicated_verdict,verifier_verdict),merged,durability "
            "FROM outcomes WHERE run_id=?",
            (closer_run_id,),
        ).fetchone()
    event_id = event_id or _runtime_ac_event_id(target, gate_status, time.time_ns())
    event_run_id = str(
        closer_run_id or f"runtime-ac-gate:{hashlib.sha256(target.encode()).hexdigest()[:24]}"
    )
    gate_payload = {
        "schema_version": RUNTIME_AC_GATE_SCHEMA_VERSION,
        "gate_event_id": event_id,
        "target": target,
        "required": bool(required),
        "dry_run": bool(dry_run),
        "eligibility_source": eligibility_source,
        "eligibility_refs": list(eligibility_refs or []),
        "spec_path": str(spec_path) if spec_path else None,
        "spec_hash": str(spec_hash) if spec_hash else None,
        "spec_path_matches_target": spec_path_matches_target,
        "gate_status": gate_status,
        "blocking": bool(blocking),
        "terminal_reason": terminal_reason,
        "closer_run_id": closer_run_id,
        "verifier_run_id": verifier_run_id,
        "verifier_verdict": normalized_verdict,
        "downstream_verdict": downstream[0] if downstream else None,
        "downstream_merged": (
            bool(downstream[1]) if downstream and downstream[1] is not None else None
        ),
        "downstream_durability": downstream[2] if downstream else None,
        "materialization_source": materialization_source,
        "materialization_status": materialization_status,
        "materialization_run_id": materialization_run_id,
    }
    event = _record_completion_event_in_conn(
        c,
        event_run_id,
        event_type="verification",
        phase="verification",
        producer="runtime_ac_gate",
        status=_runtime_ac_completion_status(gate_status, normalized_verdict),
        payload={
            "acceptance_gate_ids": [event_id],
            "runtime_ac_gate": gate_payload,
        },
        event_id=event_id,
        timestamp=timestamp,
    )
    return {**event, "runtime_ac_gate": gate_payload}


def record_runtime_ac_gate_event(**kwargs) -> dict:
    """Public writer for a structured runtime-AC gate event."""
    with _conn() as c:
        return _record_runtime_ac_gate_event_in_conn(c, **kwargs)


def runtime_ac_gate_events(*, cutoff_ts: int = 0, limit: int = 1000) -> list[dict]:
    """Read structured gate events with the latest joined closer outcome."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT event_id,run_id,status,validation_status,payload_json,created_ts,updated_ts "
            "FROM completion_events WHERE producer='runtime_ac_gate' AND updated_ts>=? "
            "ORDER BY updated_ts DESC,event_id DESC LIMIT ?",
            (int(cutoff_ts), max(1, int(limit))),
        ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                gate = dict(payload.get("runtime_ac_gate") or {})
            except (TypeError, json.JSONDecodeError):
                continue
            closer_run_id = gate.get("closer_run_id")
            if closer_run_id:
                outcome = c.execute(
                    "SELECT COALESCE(adjudicated_verdict,verifier_verdict),merged,durability "
                    "FROM outcomes WHERE run_id=?",
                    (closer_run_id,),
                ).fetchone()
                if outcome:
                    gate.update(
                        {
                            "downstream_verdict": outcome[0],
                            "downstream_merged": (
                                bool(outcome[1]) if outcome[1] is not None else None
                            ),
                            "downstream_durability": outcome[2],
                        }
                    )
            events.append(
                {
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "validation_status": row["validation_status"],
                    "created_ts": row["created_ts"],
                    "updated_ts": row["updated_ts"],
                    **gate,
                }
            )
        return events


def _capability_daily_heartbeat(capability_id: str, event_type: str, **kw) -> None:
    """Record that an always-on capability was exercised today.

    Lazy import because `capabilities` imports THIS module — a top-level import would be circular.
    Never raises: the Brain's write path must not be stoppable by a capability-ledger fault. Daily
    coalescing keeps a hot path from growing the ledger without bound (see daily_heartbeat).
    """
    try:
        import capabilities

        capabilities.daily_heartbeat(capability_id, event_type, **kw)
    except Exception:
        pass


def _resolve_capability_versions(capability_ids: list[str]) -> list[str]:
    """Ledger version id per capability, or [] unless EVERY one resolves.

    All-or-nothing on purpose: the influence-edge writer requires identity and version together,
    and a partial list would silently misalign ids with versions. Returns [] rather than guessing
    so an unversioned capability degrades to "unattributed", never to a wrong attribution.
    """
    try:
        import capabilities

        ledger = capabilities.load()
    except Exception:
        return []
    versions = [
        str((ledger.get(cid) or {}).get("capability_version_id") or "") for cid in capability_ids
    ]
    return versions if all(versions) else []


def _record_influence_edge_in_conn(
    c: sqlite3.Connection,
    *,
    target_run_id: str,
    influence_type: str,
    influence_id: str,
    accepted: bool,
    source_run_id: str | None = None,
    source_event_id: str | None = None,
    target_event_id: str | None = None,
    capability_id: str | None = None,
    capability_version_id: str | None = None,
    acceptance_gate_id: str | None = None,
    metadata: dict | None = None,
    allow_unlinked: bool = False,
) -> dict:
    kind = str(influence_type or "").strip().lower()
    if kind not in VALID_INFLUENCE_TYPES:
        raise ValueError(f"invalid influence_type={influence_type!r}")
    if not target_run_id or not influence_id:
        raise ValueError("influence edge requires target_run_id and influence_id")
    if bool(capability_id) != bool(capability_version_id):
        raise ValueError("influence capability lineage requires both identity and version")
    source_event_id = source_event_id or (
        _latest_completion_event_id(c, source_run_id) if source_run_id else None
    )
    target_event_id = target_event_id or _latest_completion_event_id(c, target_run_id)
    # AN EDGE WITH NO TARGET ENVELOPE CAN NEVER BE CAUSAL EVIDENCE, and until now that was implicit
    # -- you only learned it by reading `capability_causal_evidence`'s `consumed` guard. The sibling
    # `record_capability_consumption` already RAISES on this exact condition; this one inserted
    # silently, and 296 such edges accumulated (measured 2026-08-21), 202 of them created in a single
    # backfill that reported success. They were inert, but five carried PASS/durable and were 100% of
    # `offload`'s measured durable signal until the consumer guard landed.
    #
    # OPT-IN AND DECLARED, never inferred: an unlinked edge is a legitimate ASSOCIATION for advisory
    # role runs, which are written to `runs` but never emit a completion envelope. Those callers pass
    # `allow_unlinked=True` and say why. Everyone else now RAISES, so a new caller cannot create
    # orphans by accident -- which is exactly how the 202 arrived.
    if target_event_id is None and not allow_unlinked:
        raise ValueError(
            f"influence edge for target_run_id={target_run_id!r} has no completion event to point "
            "at, so it can never become causal evidence. If this target is an advisory run that "
            "legitimately emits no envelope, pass allow_unlinked=True and record why"
        )
    edge_identity = "|".join(
        str(item or "")
        for item in (
            source_event_id,
            source_run_id,
            target_run_id,
            capability_id,
            capability_version_id,
            kind,
            influence_id,
            bool(accepted),
        )
    )
    edge_id = "edge:" + hashlib.sha256(edge_identity.encode()).hexdigest()[:32]
    safe_meta, _, _ = _sanitize_completion_payload({"result": metadata or {}})
    metadata_hash = _completion_hash(safe_meta)
    now = int(time.time())
    c.execute(
        "INSERT INTO influence_edges "
        "(edge_id,schema_version,source_event_id,source_run_id,target_event_id,target_run_id,"
        "capability_id,capability_version_id,influence_type,influence_id,"
        "accepted,counterfactual,acceptance_gate_id,"
        "outcome_verdict,merged,durability,created_ts,propagated_ts,metadata_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(edge_id) DO UPDATE SET "
        "source_event_id=COALESCE(excluded.source_event_id,influence_edges.source_event_id),"
        "source_run_id=COALESCE(excluded.source_run_id,influence_edges.source_run_id),"
        "target_event_id=COALESCE(excluded.target_event_id,influence_edges.target_event_id),"
        "acceptance_gate_id=COALESCE(excluded.acceptance_gate_id,influence_edges.acceptance_gate_id),"
        "metadata_hash=excluded.metadata_hash",
        (
            edge_id,
            COMPLETION_EVENT_SCHEMA_VERSION,
            source_event_id,
            source_run_id,
            target_event_id,
            target_run_id,
            capability_id,
            capability_version_id,
            kind,
            str(influence_id),
            int(bool(accepted)),
            int(not bool(accepted)),
            acceptance_gate_id,
            None,
            None,
            None,
            now,
            None,
            metadata_hash,
        ),
    )
    return {"edge_id": edge_id, "accepted": bool(accepted), "counterfactual": not bool(accepted)}


def record_influence_edge(**kwargs) -> dict:
    with _conn() as c:
        edge = _record_influence_edge_in_conn(c, **kwargs)
        _propagate_outcome_lineage_in_conn(c, kwargs["target_run_id"])
        return edge


def record_capability_consumption(
    *,
    capability_id: str,
    capability_version_id: str,
    source_run_id: str,
    target_run_id: str,
    accepted: bool,
    producer: str = "orchestrator",
    acceptance_gate_id: str | None = None,
    attempt_id: str | None = None,
) -> dict:
    """Record a version-exact producer-to-consumer edge.

    The target must already have a completion event, so a caller cannot claim
    consumption for an unobserved downstream run. Later outcomes reconcile over
    this exact edge without rewriting it.
    """
    with _conn() as c:
        target_event_id = _latest_completion_event_id(c, target_run_id)
        if not target_event_id:
            raise ValueError(
                f"capability consumption target has no completion event: {target_run_id}"
            )
        source_event = _record_completion_event_in_conn(
            c,
            source_run_id,
            event_type="completion",
            phase="artifact",
            producer=producer,
            attempt_id=attempt_id,
            capability_id=capability_id,
            capability_version_id=capability_version_id,
            status="succeeded",
            payload={
                "capability_ids": [capability_id],
                "result": {"version_hash": capability_version_id, "accepted": accepted},
            },
        )
        edge = _record_influence_edge_in_conn(
            c,
            source_event_id=source_event["event_id"],
            source_run_id=source_run_id,
            target_event_id=target_event_id,
            target_run_id=target_run_id,
            capability_id=capability_id,
            capability_version_id=capability_version_id,
            influence_type="capability",
            influence_id=capability_version_id,
            accepted=accepted,
            acceptance_gate_id=acceptance_gate_id,
            metadata={"accepted": accepted, "version_hash": capability_version_id},
        )
        _propagate_outcome_lineage_in_conn(c, target_run_id)
        return {
            **edge,
            "source_event_id": source_event["event_id"],
            "target_event_id": target_event_id,
        }


CAPABILITY_REGRESSION_DURABILITY = {
    "reverted",
    "reworked",
    "reopened",
    "broke_later",
    "abandoned",
}


def capability_causal_evidence(
    capability_id: str,
    capability_version_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Derive lifecycle evidence from exact stored joins, never caller summaries.

    A row is consumed only when an accepted influence edge names the immutable
    capability version *and* points at an accepted target completion event for
    the same run. Outcome, durability, regression, rework, subject, cost, and
    profile-attempt lineage are then read from their authoritative tables.
    """
    if not capability_id or not capability_version_id:
        raise ValueError("capability evidence requires identity and version")
    c = conn or _conn()
    close = conn is None
    prior_row_factory = c.row_factory
    try:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ie.edge_id,ie.source_event_id,ie.source_run_id,"
            "ie.target_event_id,ie.target_run_id,ie.accepted,ie.counterfactual,"
            "ie.acceptance_gate_id,ie.created_ts,ie.propagated_ts,"
            "te.validation_status target_validation_status,te.updated_ts target_event_ts,"
            "r.target,r.task_type,r.routing_metadata,"
            "o.verifier_verdict,o.adjudicated_verdict,o.merged,o.ci_status,"
            "o.durability,o.durability_checked_ts,o.failure_class,"
            "co.tokens_in,co.tokens_out,co.cost_usd,co.latency_s "
            "FROM influence_edges ie "
            "LEFT JOIN completion_events te ON te.event_id=ie.target_event_id "
            "AND te.run_id=ie.target_run_id "
            "LEFT JOIN runs r ON r.run_id=ie.target_run_id "
            "LEFT JOIN outcomes o ON o.run_id=ie.target_run_id "
            "LEFT JOIN costs co ON co.run_id=ie.target_run_id "
            "WHERE ie.capability_id=? AND ie.capability_version_id=? "
            "ORDER BY ie.created_ts,ie.edge_id",
            (capability_id, capability_version_id),
        ).fetchall()
        evidence: list[dict] = []
        for row in rows:
            metadata = _routing_metadata_dict(row["routing_metadata"])
            causal_context = metadata.get("causal_context") or (
                (metadata.get("profile_decision") or {}).get("causal_context") or {}
            )
            subject_id = str(
                causal_context.get("subject_id")
                or metadata.get("subject_id")
                or row["target"]
                or row["target_run_id"]
            )
            attempts = [
                item[0]
                for item in c.execute(
                    "SELECT attempt_id FROM execution_attempts WHERE run_id=? "
                    "ORDER BY attempt_ordinal,attempt_id",
                    (row["target_run_id"],),
                ).fetchall()
            ]
            durability = str(row["durability"] or "").lower() or None
            verdict = str(row["adjudicated_verdict"] or row["verifier_verdict"] or "").upper()
            consumed = bool(
                row["accepted"]
                and row["target_event_id"]
                and row["target_validation_status"] == "accepted"
            )
            outcome_present = bool(
                row["verifier_verdict"] is not None
                or row["adjudicated_verdict"] is not None
                or row["merged"] is not None
                or row["durability"] is not None
            )
            terminal_outcome = bool(
                outcome_present
                and (durability not in (None, "pending") or verdict.startswith("FAIL"))
            )
            durable_success = bool(
                consumed
                and outcome_present
                and verdict.startswith("PASS")
                and row["merged"]
                and durability == "durable"
            )
            evidence.append(
                {
                    "edge_id": row["edge_id"],
                    "capability_id": capability_id,
                    "capability_version_id": capability_version_id,
                    "source_event_id": row["source_event_id"],
                    "source_run_id": row["source_run_id"],
                    "target_event_id": row["target_event_id"],
                    "target_run_id": row["target_run_id"],
                    "subject_id": subject_id,
                    "accepted_consumption": consumed,
                    "counterfactual": bool(row["counterfactual"]),
                    "acceptance_gate_id": row["acceptance_gate_id"],
                    "outcome_present": outcome_present,
                    "terminal_outcome": terminal_outcome,
                    "outcome_verdict": verdict or None,
                    "merged": bool(row["merged"]) if row["merged"] is not None else None,
                    "durability": durability,
                    "durable_success": durable_success,
                    "rework": durability == "reworked",
                    "regression": durability in CAPABILITY_REGRESSION_DURABILITY,
                    "failure_class": row["failure_class"],
                    "profile_attempt_ids": attempts,
                    "cost": {
                        "tokens_in": row["tokens_in"],
                        "tokens_out": row["tokens_out"],
                        "cost_usd": row["cost_usd"],
                        "latency_s": row["latency_s"],
                    },
                    "observed_ts": row["durability_checked_ts"]
                    or row["target_event_ts"]
                    or row["propagated_ts"]
                    or row["created_ts"],
                }
            )
        return evidence
    finally:
        if close:
            c.close()
        else:
            c.row_factory = prior_row_factory


def validate_operation_role(operation_role: str | None) -> str:
    """Return a normalized causal role or reject an unsafe producer value."""
    role = str(operation_role or "unknown").strip().lower().replace("-", "_")
    if role not in VALID_OPERATION_ROLES:
        raise ValueError(
            f"invalid operation_role={operation_role!r}; expected one of "
            f"{sorted(VALID_OPERATION_ROLES)}"
        )
    return role


# A PLACEHOLDER IS NOT AN IDENTITY. `SYNTHETIC_ADAPTER_MODEL_RE` only catches `agent:`-prefixed
# routing tags, so a function named `validate_resolved_worker_model` happily accepted `<synthetic>`,
# `unknown`, `none` and `default` -- and Claude's own transcripts really do write `"model":
# "<synthetic>"` on some turns (3 of 1,691 in one session sampled 2026-08-22). Admitting one of
# those as provenance is worse than recording nothing: it makes an unresolved attempt look resolved,
# which is the exact inversion this table exists to prevent. Two shapes are refused, both narrow so
# a real vendor id can never be caught by them: a bracketed marker, and this short literal list.
MODEL_PLACEHOLDERS = frozenset(
    {"unknown", "none", "null", "default", "n/a", "na", "-", "?", "tbd", "synthetic"}
)


def validate_resolved_worker_model(model: str | None) -> str | None:
    """Reject adapter routing tags and placeholders that are not resolved model identities."""
    value = str(model or "").strip()
    if not value:
        return None
    if SYNTHETIC_ADAPTER_MODEL_RE.match(value):
        raise ValueError(f"synthetic adapter tag is not a resolved worker model: {value}")
    bare = value.strip("<>[](){}").strip().lower()
    if value.startswith("<") or value.startswith("["):
        raise ValueError(f"bracketed marker is not a resolved worker model: {value}")
    if bare in MODEL_PLACEHOLDERS:
        raise ValueError(f"placeholder is not a resolved worker model: {value}")
    return value


def derive_operation_role(operation: str | None, operation_role: str | None = None) -> str:
    """Derive a causal role and reject producer claims that contradict known work."""
    op = str(operation or "").strip().lower().replace("-", "_")
    inferred = "unknown"
    if op in LEGACY_EVALUATOR_OPERATIONS or op.startswith("evaluate_"):
        inferred = "evaluator"
    elif op in LEGACY_VERIFIER_OPERATIONS or op.startswith("verify_"):
        inferred = "verifier"
    elif op == "replay" or op.startswith("replay_"):
        inferred = "replay"
    elif op in LEGACY_SYNTHESIZER_OPERATIONS or op.startswith("synthes"):
        inferred = "synthesizer"
    elif op.startswith("role_") or op in {"decompose", "redirect_agent"}:
        inferred = "role_backend"
    elif op in LEGACY_WORKER_OPERATIONS:
        inferred = "worker"
    if operation_role is None or not str(operation_role).strip():
        return inferred
    explicit = validate_operation_role(operation_role)
    if inferred != "unknown" and explicit != inferred:
        raise ValueError(
            f"operation_role={explicit!r} contradicts operation={operation!r} "
            f"(derived role {inferred!r})"
        )
    return explicit


def _successful_attempt_sql(alias: str = "ea") -> str:
    values = ",".join(f"'{status}'" for status in sorted(SUCCESSFUL_ATTEMPT_STATUSES))
    return f"LOWER(COALESCE({alias}.status,'')) IN ({values})"


def _run_model_expr() -> str:
    """SQL expression for the successful resolved worker model only.

    Legacy ``runs.model`` and arbitrary trace models remain readable but cannot act as
    exact worker provenance. This preserves agent-level evidence while preventing judge
    and verifier identities from entering supersession/model-drift logic.
    """
    return (
        "(SELECT ea.resolved_model FROM execution_attempts ea "
        "WHERE ea.run_id=r.run_id AND ea.operation_role='worker' "
        f"AND {_successful_attempt_sql('ea')} AND ea.resolved_model IS NOT NULL "
        "ORDER BY COALESCE(ea.completed_ts,ea.recorded_ts,0) DESC, "
        "ea.attempt_ordinal DESC, ea.attempt_id DESC LIMIT 1)"
    )


def resolved_worker_identity_for_run(
    run_id: str, *, conn: sqlite3.Connection | None = None
) -> dict | None:
    """Return the latest successful worker attempt, never evaluator/verifier identity."""
    c = conn or _conn()
    close = conn is None
    try:
        row = c.execute(
            "SELECT attempt_id, attempt_ordinal, profile_id, requested_provider, "
            "requested_model, selected_model, reported_model, resolved_provider, "
            "resolved_model, fallback_reason, runner_version, cli_version, "
            "status, trace_key, source FROM execution_attempts ea WHERE run_id=? "
            "AND operation_role='worker' AND resolved_model IS NOT NULL AND "
            f"{_successful_attempt_sql('ea')} "
            "ORDER BY COALESCE(completed_ts,recorded_ts,0) DESC, attempt_ordinal DESC, "
            "attempt_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        if close:
            c.close()
    if not row:
        return None
    keys = (
        "attempt_id",
        "attempt_ordinal",
        "profile_id",
        "requested_provider",
        "requested_model",
        "selected_model",
        "reported_model",
        "resolved_provider",
        "resolved_model",
        "fallback_reason",
        "runner_version",
        "cli_version",
        "status",
        "trace_key",
        "source",
    )
    return dict(zip(keys, row))


def resolved_worker_model_for_run(
    run_id: str, *, conn: sqlite3.Connection | None = None
) -> str | None:
    identity = resolved_worker_identity_for_run(run_id, conn=conn)
    return identity.get("resolved_model") if identity else None


def latest_worker_identity_for_agent(
    agent: str,
    *,
    task_type: str | None = None,
    evidence_only: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict | None:
    """Latest successful worker provenance, optionally restricted to evidence rows."""
    c = conn or _conn()
    close = conn is None
    clauses = [
        "r.agent=?",
        "ea.operation_role='worker'",
        "ea.resolved_model IS NOT NULL",
        _successful_attempt_sql("ea"),
    ]
    params: list = [agent]
    if task_type is not None:
        clauses.append("r.task_type=?")
        params.append(task_type)
    if evidence_only:
        clauses.append(
            "(EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id=r.run_id) "
            "OR EXISTS (SELECT 1 FROM evaluations e WHERE e.experiment_id=r.experiment_id "
            "AND e.implementer=r.agent))"
        )
    try:
        row = c.execute(
            "SELECT r.run_id, r.ts, ea.attempt_id, ea.profile_id, "
            "ea.requested_provider, ea.requested_model, ea.selected_model, "
            "ea.reported_model, ea.resolved_provider, ea.resolved_model, "
            "ea.runner_version, ea.cli_version, ea.status "
            "FROM runs r JOIN execution_attempts ea "
            "ON ea.run_id=r.run_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.ts DESC, COALESCE(ea.completed_ts,ea.recorded_ts,0) DESC, "
            "ea.attempt_ordinal DESC, ea.attempt_id DESC LIMIT 1",
            params,
        ).fetchone()
    finally:
        if close:
            c.close()
    if not row:
        return None
    keys = (
        "run_id",
        "run_ts",
        "attempt_id",
        "profile_id",
        "requested_provider",
        "requested_model",
        "selected_model",
        "reported_model",
        "resolved_provider",
        "resolved_model",
        "runner_version",
        "cli_version",
        "status",
    )
    return dict(zip(keys, row))


def _latest_model_for_agent(c: sqlite3.Connection, agent: str) -> str | None:
    identity = latest_worker_identity_for_agent(agent, conn=c)
    return identity.get("resolved_model") if identity else None


def record_run(
    run_id,
    target,
    task_type,
    agent,
    mode=None,
    reasoning_level=None,
    decomposition=None,
    rationale=None,
    pr_number=None,
    experiment_id=None,
    ts=None,
    model=None,
    source=None,
    assignment=None,
    work_type=None,
    role_name=None,
    routing_metadata=None,
    influenced_by_role_run_ids=None,
    influenced_by_skill_event_ids=None,
    influenced_by_workflow_ids=None,
    capability_ids=None,
    capability_version_ids=None,
    acceptance_gate_ids=None,
):
    with _conn() as c:
        existing = c.execute(
            "SELECT model, source, work_type, role_name, routing_metadata FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        run_model = model or (existing[0] if existing and existing[0] else None)
        run_source = _derive_source(run_id, mode, source) or (
            existing[1] if existing and existing[1] else None
        )
        run_assignment = _derive_assignment(run_source, assignment)
        run_work_type = work_type or (existing[2] if existing and existing[2] else None)
        run_role = role_name or (existing[3] if existing and existing[3] else None)
        run_routing_metadata = _json_or_none(routing_metadata) or (
            existing[4] if existing and existing[4] else None
        )
        c.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, ts, target, task_type, agent, mode, reasoning_level, model, "
            "decomposition, rationale, pr_number, experiment_id, source, assignment, work_type, role_name, "
            "routing_metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                ts or int(time.time()),
                target,
                task_type,
                agent,
                mode,
                reasoning_level,
                run_model,
                json.dumps(decomposition) if decomposition else None,
                rationale,
                pr_number,
                experiment_id,
                run_source,
                run_assignment,
                run_work_type,
                run_role,
                run_routing_metadata,
            ),
        )
        # feedback.py IS the Brain — exercised by every recorded run, never selected for a task.
        # Coalesced daily: at ~48 runs/day a per-invocation heartbeat would add ~17,500 events a
        # year to one record, and `heartbeat` scans that list on every call. (2026-08-09)
        _capability_daily_heartbeat("feedback-store", "invocation", ref="record_run")
        capability_ids = list(capability_ids or [])
        capability_version_ids = list(capability_version_ids or [])
        if capability_version_ids and len(capability_ids) != len(capability_version_ids):
            raise ValueError("capability identity/version lineage length mismatch")
        # Resolve versions from the ledger when the caller supplied ids without them. Callers
        # legitimately know WHICH capabilities they used but not the version hash; before lineage
        # adoption (2026-08-09) no capability had one, so this always fell through and attribution
        # was dropped. Never invents a version: a capability without lineage stays unresolved.
        if capability_ids and not capability_version_ids:
            capability_version_ids = _resolve_capability_versions(capability_ids)
        direct_capability_id = (
            capability_ids[0]
            if len(capability_ids) == 1 and len(capability_version_ids) == 1
            else None
        )
        direct_capability_version_id = (
            capability_version_ids[0] if len(capability_version_ids) == 1 else None
        )
        payload = {
            "capability_ids": capability_ids,
            "acceptance_gate_ids": list(acceptance_gate_ids or []),
            "role_ids": list(influenced_by_role_run_ids or []),
            "skill_ids": list(influenced_by_skill_event_ids or []),
            "workflow_ids": list(influenced_by_workflow_ids or []),
            "delivery": {
                "pr_number": pr_number,
                "target_id": target,
                "task_type": task_type,
                "experiment_id": experiment_id,
            },
        }
        _record_completion_event_in_conn(
            c,
            run_id,
            event_type="decision",
            phase="trigger",
            producer=run_source or "feedback.record_run",
            capability_id=direct_capability_id,
            capability_version_id=direct_capability_version_id,
            status="recorded",
            payload=payload,
            timestamp=ts,
        )
        event = _record_completion_event_in_conn(
            c,
            run_id,
            event_type="decision",
            phase="decision",
            producer=run_source or "feedback.record_run",
            capability_id=direct_capability_id,
            capability_version_id=direct_capability_version_id,
            status="recorded",
            payload=payload,
            timestamp=ts,
        )
        # ONE EDGE PER CAPABILITY. A work process routinely uses several capabilities, but the
        # completion event carries a single capability_id column, so `direct_capability_id` is set
        # only in the one-capability case — which meant a run declaring TWO capabilities recorded
        # attribution for NEITHER (the multi-capability case was the worst-served one, 2026-08-09).
        # Edges are the many-to-many surface, and `capability` is already a valid influence type.
        if len(capability_ids) == len(capability_version_ids):
            for cap_id, cap_version in zip(capability_ids, capability_version_ids):
                _record_influence_edge_in_conn(
                    c,
                    target_run_id=run_id,
                    target_event_id=event["event_id"],
                    influence_type="capability",
                    influence_id=str(cap_id),
                    accepted=True,
                    capability_id=str(cap_id),
                    capability_version_id=str(cap_version),
                    acceptance_gate_id=(list(acceptance_gate_ids or []) or [None])[0],
                )
        for role_run_id in influenced_by_role_run_ids or []:
            _record_influence_edge_in_conn(
                c,
                target_run_id=run_id,
                target_event_id=event["event_id"],
                influence_type="role",
                influence_id=str(role_run_id),
                source_run_id=str(role_run_id),
                accepted=True,
                acceptance_gate_id=(list(acceptance_gate_ids or []) or [None])[0],
            )
            # INHERIT the role run's capability attribution onto THIS run.
            #
            # Why this is required for measurement. A role run is advisory: it proposes and never
            # produces a PR, so no `outcomes` row is ever written for it. Its own capability edge is
            # therefore permanently unresolvable — measured 2026-08-18, all 170 capability edges
            # targeted `role:triage:*` runs and 0 carried a verdict or durability, so
            # capability_effectiveness reported `not_yet_measurable` forever with a populated
            # numerator and no denominator.
            #
            # The run that ACTS on the proposal does terminate, and `_record_outcome_in_conn`
            # already back-propagates over accepted edges. So re-attributing the capability to the
            # acting run is the whole fix: no new table, no second store, and outcome resolution
            # arrives for free on the existing path. `source_run_id` preserves the provenance chain
            # back to the proposal, so this adds a claim about WHERE the capability was used, not a
            # stronger claim than the evidence supports.
            for inherited_id, inherited_version in _capability_attribution_of(c, str(role_run_id)):
                if inherited_id in capability_ids:
                    continue  # the run declared it directly; don't double-count
                _record_influence_edge_in_conn(
                    c,
                    target_run_id=run_id,
                    target_event_id=event["event_id"],
                    influence_type="capability",
                    influence_id=str(inherited_version),
                    source_run_id=str(role_run_id),
                    accepted=True,
                    capability_id=str(inherited_id),
                    capability_version_id=str(inherited_version),
                    acceptance_gate_id=(list(acceptance_gate_ids or []) or [None])[0],
                )
        for skill_event_id in influenced_by_skill_event_ids or []:
            source = c.execute(
                "SELECT run_id FROM completion_events WHERE event_id=?", (skill_event_id,)
            ).fetchone()
            _record_influence_edge_in_conn(
                c,
                target_run_id=run_id,
                target_event_id=event["event_id"],
                influence_type="skill",
                influence_id=str(skill_event_id),
                source_run_id=source[0] if source else None,
                source_event_id=str(skill_event_id),
                accepted=True,
                acceptance_gate_id=(list(acceptance_gate_ids or []) or [None])[0],
            )
        for workflow_id in influenced_by_workflow_ids or []:
            source_run = f"workflow:{workflow_id}"
            source_event = _record_completion_event_in_conn(
                c,
                source_run,
                event_type="workflow",
                phase="trigger",
                producer="feedback.record_run",
                status="accepted",
                payload={"workflow_ids": [workflow_id]},
            )
            _record_influence_edge_in_conn(
                c,
                target_run_id=run_id,
                target_event_id=event["event_id"],
                influence_type="workflow",
                influence_id=str(workflow_id),
                source_run_id=source_run,
                source_event_id=source_event["event_id"],
                accepted=True,
                acceptance_gate_id=(list(acceptance_gate_ids or []) or [None])[0],
            )


def record_outcome(
    run_id,
    verifier_verdict=None,
    adjudicated_verdict=None,
    merged=None,
    ci_status=None,
    durability=None,
    notes=None,
    influenced_by_run_id=None,
):
    with _conn() as c:
        _record_outcome_in_conn(
            c,
            run_id,
            verifier_verdict=verifier_verdict,
            adjudicated_verdict=adjudicated_verdict,
            merged=merged,
            ci_status=ci_status,
            durability=durability,
            notes=notes,
            influenced_by_run_id=influenced_by_run_id,
        )


def mark_transient_infra(run_id: str, reason: str = "") -> bool:
    """Classify a NON-merged outcome as transient infrastructure failure (item 9 two-tier enum):
    the work never got a fair chance (agent signal-killed, environment broke), so the learners
    must not count it as a capability failure. Only non-merged rows are eligible — a merged PR is
    a real outcome regardless of infra noise around it. Idempotent; returns True when a row was
    newly classified."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE outcomes SET failure_class='transient_infra', "
            "notes=COALESCE(notes,'') || CASE WHEN ?!='' THEN ' [infra: ' || ? || ']' ELSE '' END "
            "WHERE run_id=? AND COALESCE(merged,0)=0 "
            "AND COALESCE(failure_class,'') != 'transient_infra'",
            (reason, reason, run_id),
        )
        return cur.rowcount > 0


# --- item 16f: resume-token registry ------------------------------------------------------------
RESUME_COMMANDS = {
    "claude_session": "claude --resume {token}",
    "codex_session": "codex exec resume {token}",
    "cursor_chat": "cursor-agent --resume={token}",
}


def record_resume_token(run_id, agent, kind, token, cwd=None) -> None:
    """Store a per-run CLI resume identifier (harvested from run logs by ledger_reconcile)."""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO resume_tokens VALUES (?,?,?,?,?,?)",
            (run_id, agent, kind, token, cwd, int(time.time())),
        )


def resume_hint(run_id) -> dict | None:
    """The stored resume identifier + a paste-ready resume command (None command for kinds
    without a known resume CLI — the raw token is still useful forensics)."""
    with _conn() as c:
        row = c.execute(
            "SELECT agent, kind, token, cwd, captured_ts FROM resume_tokens WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    agent, kind, token, cwd, ts = row
    cmd = RESUME_COMMANDS.get(kind)
    return {
        "run_id": run_id,
        "agent": agent,
        "kind": kind,
        "token": token,
        "cwd": cwd,
        "captured_ts": ts,
        "command": cmd.format(token=token) if cmd else None,
    }


# --- item 16g: Bradley-Terry strengths from the A/B/C duel data ----------------------------------
def bt_strengths(
    task_type: str | None = None,
    *,
    window_days: int = 120,
    min_comparisons: int = 8,
    iterations: int = 200,
) -> dict:
    """Pairwise-duel ranking (2026-07-08, item 16g). Each judge's scores WITHIN one experiment
    yield head-to-head wins (i beats j when score_i > score_j; ties skipped) — far more
    sample-efficient than independent success rates for ranking agents on identical work (the
    method behind Elo/LMArena). Standard Bradley-Terry MM fit; strengths normalized to mean 1.0.
    Data-gated: under `min_comparisons` decisive pairs returns ready=False and callers keep their
    hand-set table priors."""
    since = int(time.time()) - window_days * 86400
    with _conn() as c:
        if task_type:
            rows = c.execute(
                "SELECT e.experiment_id, e.implementer, e.evaluator, e.score FROM evaluations e "
                "JOIN runs r ON r.experiment_id=e.experiment_id AND r.agent=e.implementer "
                "WHERE e.ts>=? AND r.task_type=?",
                (since, task_type),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT experiment_id, implementer, evaluator, score FROM evaluations WHERE ts>=?",
                (since,),
            ).fetchall()
    by_judge: dict = {}
    for exp, impl, evaluator, score in rows:
        try:
            by_judge.setdefault((str(exp), str(evaluator)), {})[str(impl)] = float(score)
        except (TypeError, ValueError):
            continue
    wins: dict = {}
    agents: set = set()
    comparisons = 0
    for scores in by_judge.values():
        impls = sorted(scores)
        agents.update(impls)
        for i, a in enumerate(impls):
            for b in impls[i + 1 :]:
                if scores[a] > scores[b]:
                    wins[(a, b)] = wins.get((a, b), 0) + 1
                    comparisons += 1
                elif scores[b] > scores[a]:
                    wins[(b, a)] = wins.get((b, a), 0) + 1
                    comparisons += 1
    if comparisons < min_comparisons or len(agents) < 2:
        return {
            "ready": False,
            "comparisons": comparisons,
            "agents": sorted(agents),
            "task_type": task_type,
        }
    strengths = {a: 1.0 for a in agents}
    for _ in range(iterations):
        new = {}
        for a in agents:
            won = sum(wins.get((a, b), 0) for b in agents if b != a)
            denom = 0.0
            for b in agents:
                if b == a:
                    continue
                n_ab = wins.get((a, b), 0) + wins.get((b, a), 0)
                if n_ab:
                    denom += n_ab / (strengths[a] + strengths[b])
            new[a] = max((won / denom) if denom else strengths[a], 1e-6)
        mean = sum(new.values()) / len(new)
        strengths = {a: v / mean for a, v in new.items()} if mean > 0 else new
    return {
        "ready": True,
        "comparisons": comparisons,
        "task_type": task_type,
        "strengths": {a: round(v, 4) for a, v in sorted(strengths.items())},
    }


# --- item 16h: interrupt-as-data owner questions (defaults apply; nothing ever blocks) -----------
def _question_id(question: str, scope: str) -> str:
    return "q-" + hashlib.sha1(f"{scope}::{question}".encode()).hexdigest()[:16]


def record_owner_question(
    question,
    default_action,
    *,
    run_id=None,
    target=None,
    repo=None,
    options=None,
    expires_days: float = 7.0,
) -> dict:
    """Record a product-level owner question WITH the default the agent is proceeding on.
    Non-blocking by contract: work continues with the default; an unanswered question is
    auto-ratified at expiry (expire_owner_questions). Dedupe: the same question in the same
    scope stays a single row while open/answered; an expired question may be re-asked."""
    question = str(question or "").strip()
    default_action = str(default_action or "").strip()
    if not question or not default_action:
        raise ValueError("owner question requires question and default_action")
    scope = str(repo or target or "fleet")
    qid = _question_id(question, scope)
    now = int(time.time())
    with _conn() as c:
        row = c.execute("SELECT status FROM owner_questions WHERE question_id=?", (qid,)).fetchone()
        if row and row[0] in ("open", "answered"):
            return {"question_id": qid, "status": row[0], "deduped": True}
        c.execute(
            "INSERT OR REPLACE INTO owner_questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                qid,
                now,
                run_id,
                target,
                repo,
                question,
                default_action,
                json.dumps(list(options or [])),
                now + int(float(expires_days) * 86400),
                "open",
                None,
                None,
            ),
        )
    return {"question_id": qid, "status": "open", "deduped": False}


def answer_owner_question(question_id, answer) -> bool:
    with _conn() as c:
        cur = c.execute(
            "UPDATE owner_questions SET status='answered', answer=?, answered_ts=? "
            "WHERE question_id=? AND status='open'",
            (str(answer), int(time.time()), question_id),
        )
        return cur.rowcount > 0


def expire_owner_questions(now: int | None = None) -> int:
    """Auto-ratify defaults on expired open questions (owner constraint: no mounting backlog)."""
    now = int(now if now is not None else time.time())
    with _conn() as c:
        cur = c.execute(
            "UPDATE owner_questions SET status='expired_default', answered_ts=? "
            "WHERE status='open' AND expires_ts < ?",
            (now, now),
        )
        return cur.rowcount


def open_owner_questions(limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT question_id, ts, target, repo, question, default_action, expires_ts "
            "FROM owner_questions WHERE status='open' ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [
        {
            "question_id": q,
            "ts": ts,
            "target": tgt,
            "repo": rp,
            "question": qq,
            "default_action": d,
            "expires_ts": e,
        }
        for q, ts, tgt, rp, qq, d, e in rows
    ]


def owner_decisions_for(repo=None, target=None, limit: int = 5) -> list[dict]:
    """Resolved decisions for prompt injection: explicit answers, plus expiry-ratified defaults.
    Scoped to the repo/target; most recent first."""
    clauses, params = ["status IN ('answered','expired_default')"], []
    scopes = [s for s in (repo, target) if s]
    if scopes:
        placeholders = ",".join("?" for _ in scopes)
        clauses.append(f"(repo IN ({placeholders}) OR target IN ({placeholders}))")
        params.extend(scopes)
        params.extend(scopes)
    with _conn() as c:
        rows = c.execute(
            "SELECT question, default_action, status, answer FROM owner_questions "
            f"WHERE {' AND '.join(clauses)} ORDER BY COALESCE(answered_ts, ts) DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    return [
        {
            "question": q,
            "decision": (a if s == "answered" and a else d),
            "source": "owner" if s == "answered" else "default_ratified",
        }
        for q, d, s, a in rows
    ]


def _capability_attribution_of(c: sqlite3.Connection, run_id: str) -> list[tuple[str, str]]:
    """(capability_id, capability_version_id) pairs already attributed to `run_id`.

    Reads the existing edge table rather than re-deriving from prompts or entrypoints, so an
    inherited attribution can never be stronger than the attribution it came from. Returns [] on
    any failure: attribution telemetry must never break the run that is recording it.
    """
    try:
        rows = c.execute(
            "SELECT DISTINCT capability_id, capability_version_id FROM influence_edges "
            "WHERE target_run_id=? AND influence_type='capability' "
            "AND capability_id IS NOT NULL AND capability_version_id IS NOT NULL",
            (str(run_id),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(r[0], r[1]) for r in rows]


def _propagate_outcome_lineage_in_conn(c: sqlite3.Connection, target_run_id: str) -> int:
    """Back-propagate a target's current terminal state over accepted edges only."""
    outcome = c.execute(
        "SELECT verifier_verdict,adjudicated_verdict,merged,ci_status,durability,notes "
        "FROM outcomes WHERE run_id=?",
        (target_run_id,),
    ).fetchone()
    if outcome is None:
        return 0
    vv, av, merged, ci_status, durability, downstream_notes = outcome
    terminal_verdict = av or vv or ci_status
    target_event_id = _latest_completion_event_id(c, target_run_id)
    rows = c.execute(
        "SELECT edge_id,source_event_id,source_run_id,influence_type,influence_id "
        "FROM influence_edges WHERE target_run_id=? AND accepted=1",
        (target_run_id,),
    ).fetchall()
    propagated = 0
    for edge_id, source_event_id, source_run_id, influence_type, influence_id in rows:
        now = int(time.time())
        c.execute(
            "UPDATE influence_edges SET target_event_id=COALESCE(?,target_event_id),"
            "outcome_verdict=?,merged=?,durability=?,propagated_ts=? WHERE edge_id=?",
            (
                target_event_id,
                terminal_verdict,
                int(merged) if merged is not None else None,
                durability,
                now,
                edge_id,
            ),
        )
        if source_event_id:
            row = c.execute(
                "SELECT run_id,event_type,phase,producer,status,payload_json,attempt_id,"
                "capability_id,capability_version_id "
                "FROM completion_events WHERE event_id=?",
                (source_event_id,),
            ).fetchone()
            if row:
                (
                    source_event_run,
                    event_type,
                    phase,
                    producer,
                    status,
                    payload_json,
                    attempt_id,
                    capability_id,
                    capability_version_id,
                ) = row
                try:
                    source_payload = json.loads(payload_json or "{}")
                except json.JSONDecodeError:
                    source_payload = {}
                source_payload = _merge_completion_payload(
                    source_payload,
                    {
                        "result": {
                            "influenced_run_id": target_run_id,
                            "outcome_verdict": terminal_verdict,
                            "influence_type": influence_type,
                            "influence_id": influence_id,
                        },
                        "delivery": {"merged": bool(merged) if merged is not None else None},
                        "durability": {"status": durability},
                    },
                )
                _record_completion_event_in_conn(
                    c,
                    source_event_run,
                    event_type=event_type,
                    phase=phase,
                    producer=producer,
                    status=status,
                    payload=source_payload,
                    attempt_id=attempt_id,
                    capability_id=capability_id,
                    capability_version_id=capability_version_id,
                    event_id=source_event_id,
                )
        if influence_type == "role" and source_run_id:
            role_row = c.execute(
                "SELECT role_name FROM runs WHERE run_id=?", (source_run_id,)
            ).fetchone()
            if role_row and role_row[0]:
                role_notes = f"automatically influenced {target_run_id}"
                if downstream_notes:
                    role_notes += f"; downstream notes: {downstream_notes}"
                _record_outcome_in_conn(
                    c,
                    source_run_id,
                    verifier_verdict=vv,
                    adjudicated_verdict=av,
                    merged=merged,
                    ci_status=ci_status,
                    durability=durability,
                    notes=role_notes,
                    propagate_lineage=False,
                )
        propagated += 1
    accepted_role = c.execute(
        "SELECT source_run_id FROM influence_edges WHERE target_run_id=? "
        "AND accepted=1 AND influence_type='role' AND source_run_id IS NOT NULL "
        "ORDER BY created_ts,edge_id LIMIT 1",
        (target_run_id,),
    ).fetchone()
    if accepted_role:
        c.execute(
            "UPDATE outcomes SET influenced_by_run_id=COALESCE(influenced_by_run_id,?) "
            "WHERE run_id=?",
            (accepted_role[0], target_run_id),
        )
    return propagated


def _record_outcome_in_conn(
    c: sqlite3.Connection,
    run_id,
    verifier_verdict=None,
    adjudicated_verdict=None,
    merged=None,
    ci_status=None,
    durability=None,
    notes=None,
    influenced_by_run_id=None,
    propagate_lineage: bool = True,
):
    row = c.execute("SELECT run_id FROM outcomes WHERE run_id=?", (run_id,)).fetchone()
    values = [
        ("verifier_verdict", verifier_verdict),
        ("adjudicated_verdict", adjudicated_verdict),
        ("merged", merged),
        ("ci_status", ci_status),
        ("durability", durability),
        ("notes", notes),
        ("influenced_by_run_id", influenced_by_run_id),
    ]
    if row:  # late-arriving update (e.g. a durability sweep days later) — patch, don't clobber
        sets, vals = [], []
        for k, v in values:
            if v is not None:
                sets.append(f"{k}=?")
                vals.append(int(v) if isinstance(v, bool) else v)
        if durability is not None:
            sets.append("durability_checked_ts=?")
            vals.append(int(time.time()))
        if not sets:
            return
        vals.append(run_id)
        c.execute(f"UPDATE outcomes SET {','.join(sets)} WHERE run_id=?", vals)
    else:
        c.execute(
            "INSERT INTO outcomes "
            "(run_id, verifier_verdict, adjudicated_verdict, merged, ci_status, durability, "
            "durability_checked_ts, notes, influenced_by_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                verifier_verdict,
                adjudicated_verdict,
                int(merged) if merged is not None else None,
                ci_status,
                durability or "pending",
                int(time.time()) if durability else None,
                notes,
                influenced_by_run_id,
            ),
        )
    stored = c.execute(
        "SELECT verifier_verdict,adjudicated_verdict,merged,ci_status,durability,"
        "durability_checked_ts FROM outcomes WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if stored:
        vv, av, stored_merged, stored_ci, stored_durability, checked_ts = stored
        payload = {
            "verification": {
                "verifier_verdict": vv,
                "adjudicated_verdict": av,
            },
            "delivery": {
                "merged": bool(stored_merged) if stored_merged is not None else None,
                "ci_status": stored_ci,
            },
            "durability": {"status": stored_durability, "checked_ts": checked_ts},
            "result": {"outcome_verdict": av or vv},
        }
        run_delivery = c.execute(
            "SELECT target,pr_number FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if stored_merged and run_delivery and run_delivery[1] is not None:
            target, pr_number = run_delivery
            repo = str(target or "").split("#", 1)[0]
            pr_ref = f"github-pr:{repo}#{int(pr_number)}"
            _record_completion_event_in_conn(
                c,
                run_id,
                event_type="completion",
                phase="artifact",
                producer="feedback.record_outcome",
                status="merged",
                payload={
                    **payload,
                    "artifact_refs": [
                        {
                            "artifact_id": pr_ref,
                            "kind": "github-pr",
                            "content_hash": _completion_hash(pr_ref),
                            "ref_class": "delivery",
                        }
                    ],
                },
            )
        if vv is not None or av is not None:
            # NAME THE GATE. The miner refuses an episode whose verification does not say WHAT
            # passed (`unnamed_verification`) -- correctly, because "it passed" is precisely the
            # gameable label the evidence contract exists to reject. This payload carried the
            # verdicts but never the gate, so every otherwise-complete episode was ineligible.
            #
            # `acceptance_gate_ids` is the top-level field `normalize_episode` falls back to, and
            # it is named ONLY when a gate actually ran: CI is a real, checkable gate whose status
            # travels in `delivery.ci_status` in this same payload, so the claim is substantiated.
            # With no CI there is nothing to point at, so the verification stays unnamed and the
            # episode stays ineligible -- the honest outcome, not a gap to paper over with a
            # constant. (A caller-supplied gate id would be better still; this uses evidence
            # record_outcome already holds rather than inventing a parameter.)
            verification_payload: dict[str, Any] = dict(payload)
            if stored_ci:
                verification_payload["acceptance_gate_ids"] = ["ci"]
            _record_completion_event_in_conn(
                c,
                run_id,
                event_type="verification",
                phase="verification",
                producer="feedback.record_outcome",
                status=av or vv,
                payload=verification_payload,
            )
        if stored_merged is not None or stored_ci is not None:
            _record_completion_event_in_conn(
                c,
                run_id,
                event_type="delivery",
                phase="delivery",
                producer="feedback.record_outcome",
                status="merged" if stored_merged else stored_ci,
                payload=payload,
            )
        _record_completion_event_in_conn(
            c,
            run_id,
            event_type="completion",
            phase="outcome",
            producer="feedback.record_outcome",
            status=av or vv or stored_ci or "recorded",
            payload=payload,
        )
        if durability is not None:
            _record_completion_event_in_conn(
                c,
                run_id,
                event_type="durability",
                phase="durability",
                producer="feedback.record_outcome",
                status=stored_durability,
                payload=payload,
            )
    if propagate_lineage:
        _propagate_outcome_lineage_in_conn(c, run_id)


def role_task_type(role_name: str) -> str:
    """Task-type namespace for learning backend fit for an agent-role."""
    role = (role_name or "").strip().lower().replace("_", "-")
    if not role:
        raise ValueError("role_name is required")
    return f"role:{role}"


def record_role_run(
    run_id: str,
    role_name: str,
    target: str,
    agent: str,
    *,
    mode: str | None = "role",
    reasoning_level: str | None = None,
    backend_run_id: str | None = None,
    action: str | None = None,
    decision_source: str | None = None,
    proposal: dict | None = None,
    rationale: str | None = None,
    model: str | None = None,
    ts: int | None = None,
):
    """Record a role invocation as its own learnable run.

    `model` is COST TELEMETRY, not provenance. A role run is advisory (non-worker), and the value
    may be a synthetic adapter tag like `codex:assess:default`, so it must never be read as a
    provider-resolved identity for a worker attempt. Recording it closes a real gap: 450 local role
    runs carried `model=NULL`, which makes their spend unattributable -- and missing cost telemetry
    must never read as free.

    The backend's raw offload/delegate run remains separate. This role run is the
    unit that later receives the downstream outcome it influenced, giving the
    learner a per-role `(role:<name>, backend)` surface without contaminating the
    normal task-type weights.
    """
    metadata = {
        "role": role_name,
        "backend_run_id": backend_run_id,
        "action": action,
        "decision_source": decision_source,
        "proposal": proposal,
    }
    # Roles are the ONE production path that already knows which capability it is, so they are
    # where capability attribution can start. Until 2026-08-11 the id was passed only into a
    # completion-event payload and never to record_run, so no influence edge was ever tagged with
    # a capability — 81 edges, 0 tagged. `reconcile_causal_lifecycle` reads exactly those tagged
    # edges, so capability-level causal evidence stayed at zero no matter how much work ran, and
    # every gate was unliftable by construction. record_run resolves the version from the ledger
    # (all role capabilities carry lineage since adoption), so passing the id here is what makes
    # the evidence loop turn.
    role_capability_id = f"role-{str(role_name).strip().lower()}"
    # An offload PRODUCED this proposal whenever there is a backend run id, and the role's
    # capability attribution inherits onto whatever run acts on the advice — so tagging it here is
    # how the transport finally gets an outcome instead of only an invocation count. Recorded link,
    # not a guess: backend_run_id IS the offload's run. Omitted when the proposal was replayed
    # offline, because then no offload ran. (2026-08-21)
    capability_ids = [role_capability_id]
    if backend_run_id:
        capability_ids.append("offload")
    record_run(
        run_id,
        target,
        role_task_type(role_name),
        agent,
        mode=mode,
        reasoning_level=reasoning_level,
        decomposition={k: v for k, v in metadata.items() if v is not None},
        rationale=rationale or decision_source,
        ts=ts,
        role_name=role_name,
        model=model,
        capability_ids=capability_ids,
    )
    record_completion_event(
        run_id,
        event_type="role",
        phase="execution",
        producer="feedback.record_role_run",
        status=action or "recorded",
        payload={
            "role_ids": [role_name],
            "capability_ids": [f"role-{str(role_name).strip().lower()}"],
            "result": {
                "action_id": action,
                "decision_source_id": decision_source,
                "backend_run_id": backend_run_id,
                "proposal_hash": _completion_hash(proposal) if proposal else None,
            },
        },
    )


def record_role_selector_event(
    role_name: str,
    selector_status: str,
    *,
    reason: str,
    target: str | None = None,
    matched: bool = False,
    invoked: bool = False,
    accepted: bool | None = None,
    disagreement: bool = False,
    role_run_id: str | None = None,
) -> dict:
    """Record a bounded role-selector decision in the existing completion-event plane.

    Selector misses are learning evidence too: ``no_matching_work`` is intentionally
    distinct from ``matched_not_invoked`` (gate, capacity, or per-cycle cap).  The
    event contains IDs and booleans only; no prompt, issue body, or backend output is
    copied into the Brain.
    """
    role = str(role_name or "").strip().lower().replace("_", "-")
    if not role:
        raise ValueError("role_name is required")
    selector_status = str(selector_status or "").strip().lower()
    if selector_status not in {"no_matching_work", "matched_not_invoked", "invoked"}:
        raise ValueError(f"invalid role selector status={selector_status!r}")
    identity = role_run_id or (
        "role-selector:"
        + hashlib.sha256(
            f"{role}|{target or ''}|{selector_status}|{reason}|{time.time_ns()}".encode()
        ).hexdigest()[:24]
    )
    return record_completion_event(
        identity,
        event_type="role",
        phase="trigger" if not invoked else "decision",
        producer="roles",
        status="recorded",
        payload={
            "role_ids": [role],
            "capability_ids": [f"role-{role}"],
            "result": {
                "selector_status": selector_status,
                "selector_reason_id": str(reason or "unspecified")[:96],
                "matched": bool(matched),
                "invoked": bool(invoked),
                "accepted": accepted,
                "disagreement": bool(disagreement),
            },
        },
    )


def role_activation_metrics(*, conn: sqlite3.Connection | None = None) -> dict:
    """Summarize role activation, linkage, durability, and evidence readiness."""
    c = conn or _conn()
    close = conn is None
    roles = ("redirect", "prompt", "decomposer", "triage", "adjudicator")
    try:
        rows = c.execute(
            "SELECT payload_json FROM completion_events "
            "WHERE event_type='role' AND producer='roles'"
        ).fetchall()
        selector: dict[str, dict[str, int]] = {
            role: {
                "matched": 0,
                "invoked": 0,
                "accepted": 0,
                "rejected": 0,
                "disagreement": 0,
                "no_matching_work": 0,
                "matched_not_invoked": 0,
            }
            for role in roles
        }
        for (raw,) in rows:
            try:
                payload = json.loads(raw or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            role_ids = payload.get("role_ids") or []
            if not role_ids or role_ids[0] not in selector:
                continue
            out = selector[role_ids[0]]
            result = payload.get("result") or {}
            status = result.get("selector_status")
            if status in out:
                out[status] += 1
            for key in ("matched", "invoked", "disagreement"):
                if result.get(key) is True:
                    out[key] += 1
            if result.get("accepted") is True:
                out["accepted"] += 1
            elif result.get("accepted") is False:
                out["rejected"] += 1

        for role in roles:
            linked, durable = c.execute(
                "SELECT COUNT(*),SUM(CASE WHEN ie.durability='durable' THEN 1 ELSE 0 END) "
                "FROM influence_edges ie JOIN runs r ON r.run_id=ie.source_run_id "
                "WHERE ie.influence_type='role' AND ie.accepted=1 AND r.role_name=?",
                (role,),
            ).fetchone()
            role_runs = int(
                c.execute("SELECT COUNT(*) FROM runs WHERE role_name=?", (role,)).fetchone()[0]
            )
            out: dict[str, Any] = selector[role]
            out["role_runs"] = role_runs
            out["linked"] = int(linked or 0)
            out["durable"] = int(durable or 0)
            # Role-specific observations are intentionally conservative.  This is
            # promotion readiness, not a benchmark or autonomy switch.
            out["evidence_readiness"] = (
                "ready_for_review"
                if out["linked"] >= 5 and out["durable"] >= 3 and out["rejected"] >= 1
                else "collecting"
            )
            out["profile_fit"] = "observed" if role_runs >= 3 else "insufficient_role_runs"
        return {"roles": selector}
    finally:
        if close:
            c.close()


def join_role_to_outcome(
    role_run_id: str,
    influenced_run_id: str,
    *,
    accepted: bool = True,
    notes: str | None = None,
) -> dict:
    """Join a role invocation to the downstream outcome it influenced.

    `influenced_by_run_id` is stamped on the downstream outcome for auditability.
    When the downstream outcome already exists and the role's advice was accepted,
    the same outcome is mirrored onto the role run so existing `relearn_quality`
    can learn backend fit for `task_type='role:<name>'`.
    """
    if not role_run_id or not influenced_run_id:
        raise ValueError("role_run_id and influenced_run_id are required")
    with _conn() as c:
        role_row = c.execute(
            "SELECT role_name, task_type FROM runs WHERE run_id=?", (role_run_id,)
        ).fetchone()
        if not role_row or not role_row[0]:
            raise ValueError(f"{role_run_id!r} is not a recorded role run")
        edge = _record_influence_edge_in_conn(
            c,
            target_run_id=influenced_run_id,
            influence_type="role",
            influence_id=role_run_id,
            source_run_id=role_run_id,
            accepted=accepted,
            metadata={"notes_hash": _completion_hash(notes) if notes else None},
            # DECLARED UNLINKED: the influenced run may be an advisory role run, which is written to
            # `runs` but never emits a completion envelope, so there is no event to point at. That
            # is a legitimate ASSOCIATION and not causal evidence -- `capability_causal_evidence`
            # correctly refuses to consume it. Saying so here is what keeps the guard meaningful for
            # every other caller.
            allow_unlinked=True,
        )
        downstream = c.execute(
            "SELECT verifier_verdict, adjudicated_verdict, merged, ci_status, durability, notes "
            "FROM outcomes WHERE run_id=?",
            (influenced_run_id,),
        ).fetchone()
        if downstream is None:
            return {
                "role_run_id": role_run_id,
                "influenced_run_id": influenced_run_id,
                "linked": False,
                "synced": False,
                "edge_id": edge["edge_id"],
                "reason": "influenced run has no outcome yet",
            }
        if not accepted:
            return {
                "role_run_id": role_run_id,
                "influenced_run_id": influenced_run_id,
                "linked": True,
                "synced": False,
                "edge_id": edge["edge_id"],
                "reason": "role proposal was not accepted/applied",
            }
        _propagate_outcome_lineage_in_conn(c, influenced_run_id)
        if notes:
            c.execute(
                "UPDATE outcomes SET notes=? || CASE WHEN COALESCE(notes,'')<>'' "
                "THEN '; ' || notes ELSE '' END WHERE run_id=?",
                (notes, role_run_id),
            )
    return {
        "role_run_id": role_run_id,
        "influenced_run_id": influenced_run_id,
        "linked": True,
        "synced": True,
        "task_type": role_row[1],
        "edge_id": edge["edge_id"],
    }


def record_skill_invocation(
    skill_id: str,
    version_hash: str,
    *,
    phase: str = "execution",
    artifacts: list[dict] | None = None,
    influenced_run_ids: list[str] | None = None,
    result: str = "recorded",
    accepted: bool = True,
    invocation_id: str | None = None,
    acceptance_gate_id: str | None = None,
) -> dict:
    """Lightweight local skill protocol: IDs/hashes in, bounded lineage out."""
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        raise ValueError("skill_id is required")
    normalized_phase = str(phase or "execution").strip().lower().replace("-", "_")
    if normalized_phase in {"invoke", "invocation", "run"}:
        normalized_phase = "execution"
    if normalized_phase not in VALID_COMPLETION_PHASES:
        raise ValueError(f"invalid skill phase={phase!r}")
    version = str(version_hash or "").strip()
    if not re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", version):
        version = _completion_hash(version)
    invocation_id = invocation_id or (
        "skill-invocation:"
        + hashlib.sha256(f"{skill_id}|{version}|{time.time_ns()}".encode()).hexdigest()[:24]
    )
    run_id = f"skill:{skill_id}:{invocation_id.rsplit(':', 1)[-1]}"
    with _conn() as c:
        event = _record_completion_event_in_conn(
            c,
            run_id,
            event_type="skill",
            phase=normalized_phase,
            producer="feedback.record_skill_invocation",
            status=result,
            payload={
                "skill_ids": [skill_id],
                "skill": {
                    "skill_id": skill_id,
                    "version_hash": version,
                    "phase": normalized_phase,
                    "result": result,
                    "accepted": bool(accepted),
                },
                "artifact_refs": artifacts or [],
                "result": {"status": result},
            },
            event_id=invocation_id,
        )
        edges = []
        for target_run_id in influenced_run_ids or []:
            edge = _record_influence_edge_in_conn(
                c,
                target_run_id=target_run_id,
                influence_type="skill",
                influence_id=skill_id,
                source_run_id=run_id,
                source_event_id=event["event_id"],
                accepted=accepted,
                acceptance_gate_id=acceptance_gate_id,
                metadata={"version_hash": version},
            )
            _propagate_outcome_lineage_in_conn(c, target_run_id)
            edges.append(edge["edge_id"])
    return {
        "invocation_id": invocation_id,
        "event_id": event["event_id"],
        "run_id": run_id,
        "edge_ids": edges,
        "accepted": bool(accepted),
    }


def completion_event_health(*, conn: sqlite3.Connection | None = None) -> dict:
    """Compact lineage/completeness metrics shared by reports and capability miners."""
    c = conn or _conn()
    close = conn is None
    try:
        total = int(c.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0])
        complete = int(
            c.execute(
                "SELECT COUNT(DISTINCT run_id) FROM completion_events "
                "WHERE phase IN ('outcome','durability') AND validation_status='accepted'"
            ).fetchone()[0]
        )
        redacted = int(
            c.execute(
                "SELECT COUNT(*) FROM completion_events WHERE validation_status='redacted'"
            ).fetchone()[0]
        )
        rejected = int(
            c.execute(
                "SELECT COUNT(*) FROM completion_events WHERE validation_status='rejected'"
            ).fetchone()[0]
        )
        durable = int(
            c.execute(
                "SELECT COUNT(DISTINCT ce.run_id) FROM completion_events ce JOIN outcomes o "
                "ON o.run_id=ce.run_id WHERE o.durability='durable'"
            ).fetchone()[0]
        )
        accepted = int(
            c.execute("SELECT COUNT(*) FROM influence_edges WHERE accepted=1").fetchone()[0]
        )
        accepted_linked = int(
            c.execute(
                "SELECT COUNT(*) FROM influence_edges WHERE accepted=1 AND propagated_ts IS NOT NULL "
                "AND (outcome_verdict IS NOT NULL OR durability IS NOT NULL)"
            ).fetchone()[0]
        )
        orphan_edges = int(
            c.execute(
                "SELECT COUNT(*) FROM influence_edges ie WHERE "
                "(ie.target_event_id IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM completion_events ce WHERE ce.event_id=ie.target_event_id)) "
                "OR (ie.source_event_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM completion_events ce WHERE ce.event_id=ie.source_event_id))"
            ).fetchone()[0]
        )
        return {
            "schema_version": COMPLETION_EVENT_SCHEMA_VERSION,
            "total": total,
            "complete": complete,
            "redacted": redacted,
            "rejected": rejected,
            "orphan_edges": orphan_edges,
            "durable": durable,
            "accepted_influence_total": accepted,
            "accepted_influence_linked": accepted_linked,
            "accepted_influence_missing_outcome": max(0, accepted - accepted_linked),
            "accepted_influence_link_coverage": (
                round(accepted_linked / accepted, 4) if accepted else None
            ),
        }
    finally:
        if close:
            c.close()


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    return (
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        is not None
    )


def _routing_metadata_dict(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _normalized_sha256_id(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if re.fullmatch(r"sha256:[a-f0-9]{64}", text):
        return text
    if re.fullmatch(r"[a-f0-9]{64}", text):
        return f"sha256:{text}"
    return _completion_hash(text)


def completion_event_episodes(
    *,
    limit: int = 1000,
    accepted_only: bool = True,
    durable_only: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Canonical pure completion-event boundary for miners and status consumers.

    ``limit`` bounds runs, not events. Every accepted phase for those runs is
    returned and bound to one deterministic successful worker attempt. Run-level
    events keep their stored NULL attempt in SQLite but expose the canonical
    attempt in this joined envelope; evaluator/verifier models never qualify.
    """
    c = conn or _conn()
    close = conn is None
    try:
        # Instrumentation-only runs prove plumbing; they are not productive
        # completion episodes and must never seed Pattern Miner candidates.
        run_clauses = ["COALESCE(r.assignment,'experimental')<>'instrumentation'"]
        if accepted_only:
            run_clauses.append("ce.validation_status='accepted'")
        if durable_only:
            run_clauses.append(
                "EXISTS (SELECT 1 FROM outcomes o WHERE o.run_id=ce.run_id AND o.durability='durable')"
            )
        run_where = "WHERE " + (" AND ".join(run_clauses) if run_clauses else "1=1")
        run_ids = [
            row[0]
            for row in c.execute(
                "SELECT ce.run_id,MAX(ce.updated_ts) last_ts FROM completion_events ce "
                "LEFT JOIN runs r ON r.run_id=ce.run_id "
                f"{run_where} GROUP BY ce.run_id ORDER BY last_ts DESC,ce.run_id DESC LIMIT ?",
                (max(1, min(int(limit), 10000)),),
            ).fetchall()
        ]
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        event_clauses = [
            f"ce.run_id IN ({placeholders})",
            "ce.phase IN ('trigger','decision','execution','artifact','verification','outcome','durability')",
        ]
        params: list = list(run_ids)
        if accepted_only:
            event_clauses.append("ce.validation_status='accepted'")
        rows = c.execute(
            "SELECT ce.event_id,ce.schema_version,ce.run_id,ce.attempt_id,ce.event_type,"
            "ce.phase,ce.producer,ce.status,ce.validation_status,ce.payload_json,"
            "ce.content_hash,ce.redaction_count,ce.created_ts,ce.updated_ts,"
            "r.target,r.task_type,r.experiment_id,r.routing_metadata,r.agent "
            "FROM completion_events ce LEFT JOIN runs r ON r.run_id=ce.run_id WHERE "
            + " AND ".join(event_clauses)
            + " ORDER BY ce.updated_ts DESC,ce.run_id DESC,"
            "CASE ce.phase WHEN 'trigger' THEN 1 WHEN 'decision' THEN 2 "
            "WHEN 'execution' THEN 3 WHEN 'artifact' THEN 4 WHEN 'verification' THEN 5 "
            "WHEN 'delivery' THEN 6 WHEN 'outcome' THEN 7 WHEN 'durability' THEN 8 END,"
            "ce.event_id",
            params,
        ).fetchall()

        # Raw Brain rows retain multiple producer observations.  The miner
        # boundary exports exactly one canonical event per run/phase.
        def canonical_rank(row: tuple) -> tuple:
            event_type, phase, producer, status = row[4], row[5], row[6], row[7]
            producer_rank = 0
            event_rank = 0
            if phase == "execution":
                event_rank = 2 if event_type == "completion" else 1
            elif phase == "verification":
                producer_rank = {
                    "local_verify": 4,
                    "runtime_ac": 4,
                    "adversarial": 3,
                    "feedback.record_outcome": 1,
                }.get(producer, 2)
                event_rank = 2 if event_type in {"verification", "panel"} else 1
            terminal_rank = (
                1
                if status
                in {
                    "succeeded",
                    "pass",
                    "fail",
                    "needs_review",
                    "merged",
                    "durable",
                    "reverted",
                    "reworked",
                    "reopened",
                    "broke_later",
                    "abandoned",
                }
                else 0
            )
            return producer_rank, event_rank, terminal_rank, int(row[13] or 0), str(row[0])

        canonical_rows: dict[tuple[str, str], tuple] = {}
        for row in rows:
            key = (str(row[2]), str(row[5]))
            if key not in canonical_rows or canonical_rank(row) > canonical_rank(
                canonical_rows[key]
            ):
                canonical_rows[key] = row
        phase_order = {
            "trigger": 1,
            "decision": 2,
            "execution": 3,
            "artifact": 4,
            "verification": 5,
            "outcome": 6,
            "durability": 7,
        }
        rows = sorted(
            canonical_rows.values(),
            key=lambda row: (-int(row[13] or 0), str(row[2]), phase_order.get(str(row[5]), 99)),
        )
        context_by_run: dict[str, dict] = {}
        envelopes = []
        for row in rows:
            (
                event_id,
                schema_version,
                run_id,
                stored_attempt_id,
                event_type,
                phase,
                producer,
                status,
                validation_status,
                payload_json,
                content_hash,
                redaction_count,
                created_ts,
                updated_ts,
                target,
                task_type,
                experiment_id,
                routing_metadata,
                agent,
            ) = row
            context = context_by_run.get(run_id)
            if context is None:
                metadata = _routing_metadata_dict(routing_metadata)
                worker = resolved_worker_identity_for_run(run_id, conn=c)
                attempt_count = int(
                    c.execute(
                        "SELECT COUNT(*) FROM execution_attempts "
                        "WHERE run_id=? AND operation_role='worker'",
                        (run_id,),
                    ).fetchone()[0]
                )
                successful_attempt_count = int(
                    c.execute(
                        "SELECT COUNT(*) FROM execution_attempts ea WHERE run_id=? "
                        "AND operation_role='worker' AND " + _successful_attempt_sql("ea"),
                        (run_id,),
                    ).fetchone()[0]
                )
                arm = None
                if _table_exists(c, "evaluations_v2"):
                    arm = c.execute(
                        "SELECT implementer_arm_id,implementer_member_id,implementer_profile_id "
                        "FROM evaluations_v2 WHERE implementer_member_id=? "
                        "ORDER BY ts DESC,evaluator_id LIMIT 1",
                        (run_id,),
                    ).fetchone()
                arm_id = (arm[0] if arm else None) or metadata.get("arm_id")
                member_id = (arm[1] if arm else None) or metadata.get("member_id")
                profile_id = (
                    (worker or {}).get("profile_id")
                    or (arm[2] if arm else None)
                    or metadata.get("profile_id")
                    or metadata.get("selected_profile_id")
                )
                subject = None
                if (
                    experiment_id
                    and _table_exists(c, "research_subject_experiments")
                    and _table_exists(c, "research_subjects")
                ):
                    subject = c.execute(
                        "SELECT s.subject_id,s.subject_family_id,s.canonical_target,s.task_type,"
                        "s.spec_hash,s.base_sha,s.arms_json,s.profiles_json "
                        "FROM research_subject_experiments x "
                        "JOIN research_subjects s ON s.subject_id=x.subject_id WHERE x.exp_id=?",
                        (experiment_id,),
                    ).fetchone()
                canonical_target = (
                    (subject[2] if subject else None) or str(target or "").strip().lower() or None
                )
                canonical_task_type = (subject[3] if subject else None) or task_type
                spec_hash = (
                    (subject[4] if subject else None)
                    or metadata.get("normalized_spec_hash")
                    or metadata.get("spec_hash")
                )
                base_sha = (subject[5] if subject else None) or metadata.get("base_sha")
                family_id = (subject[1] if subject else None) or metadata.get("subject_family_id")
                subject_id = (subject[0] if subject else None) or metadata.get("subject_id")
                if subject:
                    try:
                        subject_arms = json.loads(subject[6] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        subject_arms = []
                    try:
                        subject_profiles = json.loads(subject[7] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        subject_profiles = {}
                else:
                    subject_arms = metadata.get("arms") or metadata.get("arm_set") or []
                    subject_profiles = metadata.get("profiles") or metadata.get("profile_set") or []
                if (
                    not subject_id
                    and canonical_target
                    and canonical_task_type
                    and spec_hash
                    and base_sha
                ):
                    normalized_spec_hash = _normalized_sha256_id(spec_hash)
                    raw_spec_hash = (
                        normalized_spec_hash.split(":", 1)[1]
                        if normalized_spec_hash and ":" in normalized_spec_hash
                        else normalized_spec_hash
                    )
                    if not experiment_id and not subject_arms and arm_id:
                        subject_arms = [arm_id]
                    if not experiment_id and not subject_profiles and profile_id:
                        subject_profiles = [profile_id]
                    # An experiment missing its canonical subject row must carry the
                    # full arm/profile set. Never derive from one individual arm.
                    if (not experiment_id and (subject_arms or subject_profiles)) or (
                        experiment_id and (metadata.get("arms") or metadata.get("profiles"))
                    ):
                        normalized_arms = sorted(
                            {str(item).strip().lower() for item in subject_arms}
                        )
                        subject_arms = normalized_arms
                        profile_value = subject_profiles or {}
                        # One canonical implementation owns subject/family IDs.
                        # Keep this local import to avoid feedback <-> subject-module
                        # import cycles during module initialization.
                        import research_subjects

                        derived_subject = research_subjects.subject_identity_from_hash(
                            canonical_target,
                            canonical_task_type,
                            str(raw_spec_hash or ""),
                            base_sha,
                            normalized_arms,
                            profile_value,
                        )
                        family_id = derived_subject["subject_family_id"]
                        subject_id = derived_subject["subject_id"]
                repository = (
                    canonical_target.rsplit("#", 1)[0]
                    if canonical_target and "#" in canonical_target
                    else canonical_target
                )
                identity_complete = bool(
                    all(
                        (
                            subject_id,
                            family_id,
                            canonical_target,
                            canonical_task_type,
                            spec_hash,
                            base_sha,
                        )
                    )
                )
                provenance_complete = bool(
                    identity_complete
                    and (profile_id or arm_id)
                    and successful_attempt_count == 1
                    and worker
                    and worker.get("resolved_provider")
                    and worker.get("resolved_model")
                )
                context = {
                    "run_id": run_id,
                    "subject_id": subject_id,
                    "family_id": family_id,
                    "canonical_target": canonical_target,
                    "repository": repository,
                    "task_type": canonical_task_type,
                    "normalized_spec_hash": _normalized_sha256_id(spec_hash),
                    "base_sha": base_sha,
                    "experiment_id": experiment_id,
                    "profile_id": profile_id,
                    "arm_id": arm_id,
                    "member_id": member_id,
                    "subject_arms": subject_arms,
                    "subject_profiles": subject_profiles,
                    "attempt_id": (worker or {}).get("attempt_id"),
                    "attempt_resolution": (
                        "resolved"
                        if successful_attempt_count == 1
                        else "ambiguous" if successful_attempt_count > 1 else "unresolved"
                    ),
                    "resolved_provider": (worker or {}).get("resolved_provider"),
                    "resolved_model": (worker or {}).get("resolved_model"),
                    "attempt_count": attempt_count,
                    "successful_attempt_count": successful_attempt_count,
                    "retry_count": max(0, attempt_count - 1),
                    "identity_complete": identity_complete,
                    "provenance_complete": provenance_complete,
                }
                context_by_run[run_id] = context
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            identity = {
                key: value
                for key, value in context.items()
                if key not in {"identity_complete", "provenance_complete"}
            }
            import research_subjects

            identity["observation_id"] = research_subjects.completion_observation_id(
                context["subject_id"],
                run_id,
                context["attempt_id"] if context["attempt_resolution"] == "resolved" else None,
            )
            envelopes.append(
                {
                    "schema": "orchestrator.completion-event-envelope",
                    "version": COMPLETION_EVENT_SCHEMA_VERSION,
                    "event": {
                        "schema_version": int(schema_version),
                        "event_id": event_id,
                        "run_id": run_id,
                        "attempt_id": context["attempt_id"],
                        "event_type": event_type,
                        "phase": phase,
                        "producer": producer,
                        "status": status,
                        "validation_status": validation_status,
                        "content_hash": content_hash,
                        "redaction_count": int(redaction_count or 0),
                        "created_ts": created_ts,
                        "updated_ts": updated_ts,
                        "payload": payload,
                    },
                    "identity": identity,
                    "identity_complete": context["identity_complete"],
                    "provenance_complete": context["provenance_complete"],
                }
            )
        return envelopes
    finally:
        if close:
            c.close()


def record_cost(run_id, tokens_in=0, tokens_out=0, cost_usd=0.0, latency_s=0.0, source="ledger"):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO costs VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                tokens_in,
                tokens_out,
                cost_usd,
                latency_s,
                source,
                int(time.time()),
            ),
        )


def record_execution_trace(
    run_id,
    trace_id=None,
    trace_url=None,
    provider=None,
    model=None,
    operation=None,
    status=None,
    latency_s=0.0,
    cost_usd=0.0,
    source="langsmith",
    raw_ref=None,
    trace_key=None,
    operation_role=None,
    profile_id=None,
    requested_provider=None,
    requested_model=None,
    selected_model=None,
    reported_model=None,
    resolved_provider=None,
    resolved_model=None,
    fallback_reason=None,
    runner_version=None,
    cli_version=None,
    attempt_ordinal=None,
    tokens_in=0,
    tokens_out=0,
    started_ts=None,
    completed_ts=None,
):
    """Retain a trace plus its causally scoped attempt provenance.

    The legacy trace row remains intact for cost/judge analysis. Worker resolution is
    written only to ``execution_attempts`` and only when the role is explicitly or
    unambiguously worker; evaluator/verifier/replay models never mutate ``runs.model``.
    """
    key_parts = [source, run_id, trace_id or operation or raw_ref or "unknown"]
    key = trace_key or ":".join(str(p) for p in key_parts)
    role = derive_operation_role(operation, operation_role)
    try:
        ordinal = int(attempt_ordinal) if attempt_ordinal is not None else None
        if ordinal is not None and ordinal < 1:
            ordinal = None
    except (TypeError, ValueError):
        ordinal = None
    with _conn() as c:
        if ordinal is None:
            existing_ordinal = c.execute(
                "SELECT attempt_ordinal FROM execution_attempts "
                "WHERE run_id=? AND trace_key=? AND operation_role=? "
                "AND COALESCE(resolved_provider,'')=COALESCE(?, '') "
                "AND COALESCE(resolved_model,'')=COALESCE(?, '') "
                "AND COALESCE(fallback_reason,'')=COALESCE(?, '') "
                "AND COALESCE(status,'')=COALESCE(?, '') "
                "ORDER BY attempt_ordinal LIMIT 1",
                (
                    run_id,
                    key,
                    role,
                    resolved_provider or provider,
                    resolved_model if role == "worker" else resolved_model or model,
                    fallback_reason,
                    status,
                ),
            ).fetchone()
            ordinal = (
                int(existing_ordinal[0])
                if existing_ordinal
                else int(
                    c.execute(
                        "SELECT COALESCE(MAX(attempt_ordinal),0)+1 "
                        "FROM execution_attempts WHERE run_id=? AND trace_key=?",
                        (run_id, key),
                    ).fetchone()[0]
                )
            )
        c.execute(
            "INSERT OR REPLACE INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                run_id,
                trace_id,
                trace_url,
                provider,
                model,
                operation,
                status,
                latency_s,
                cost_usd,
                source,
                raw_ref,
                int(time.time()),
            ),
        )
        attempt_model = resolved_model
        if role != "worker" and not attempt_model:
            attempt_model = model
        attempt_identity = hashlib.sha256(
            "|".join(
                str(value or "")
                for value in (
                    key,
                    ordinal,
                    role,
                    profile_id,
                    requested_provider,
                    requested_model,
                    selected_model,
                    reported_model,
                    resolved_provider,
                    attempt_model,
                    fallback_reason,
                    runner_version,
                    cli_version,
                )
            ).encode()
        ).hexdigest()[:24]
        _record_execution_attempt_in_conn(
            c,
            run_id=run_id,
            attempt_id=f"attempt:{attempt_identity}",
            attempt_ordinal=ordinal,
            operation_role=role,
            profile_id=profile_id,
            requested_provider=requested_provider,
            requested_model=requested_model,
            selected_model=selected_model,
            reported_model=reported_model,
            resolved_provider=resolved_provider or provider,
            resolved_model=attempt_model,
            fallback_reason=fallback_reason,
            runner_version=runner_version,
            cli_version=cli_version,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_s=latency_s,
            cost_usd=cost_usd,
            trace_key=key,
            source=source,
            raw_ref=raw_ref,
            started_ts=started_ts,
            completed_ts=completed_ts,
        )


def _record_execution_attempt_in_conn(
    c: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
    attempt_ordinal: int = 1,
    operation_role: str,
    profile_id: str | None = None,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    selected_model: str | None = None,
    reported_model: str | None = None,
    resolved_provider: str | None = None,
    resolved_model: str | None = None,
    fallback_reason: str | None = None,
    runner_version: str | None = None,
    cli_version: str | None = None,
    status: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_s: float = 0.0,
    cost_usd: float = 0.0,
    trace_key: str | None = None,
    source: str | None = None,
    raw_ref: str | None = None,
    started_ts: int | None = None,
    completed_ts: int | None = None,
    recorded_ts: int | None = None,
) -> None:
    role = validate_operation_role(operation_role)
    if role == "worker":
        validated_model = validate_resolved_worker_model(resolved_model)
    try:
        ordinal = max(1, int(attempt_ordinal or 1))
    except (TypeError, ValueError):
        ordinal = 1
    existing = c.execute(
        "SELECT run_id,attempt_ordinal,operation_role,profile_id,trace_key,resolved_model "
        "FROM execution_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if existing:
        prior_identity = existing[:5]
        next_identity = (run_id, ordinal, role, profile_id, trace_key)
        for prior, proposed in zip(prior_identity, next_identity):
            if prior is not None and proposed is not None and prior != proposed:
                raise ValueError(f"attempt identity changed for {attempt_id}")
        if existing[5] and resolved_model and existing[5] != resolved_model:
            raise ValueError(f"resolved model changed for {attempt_id}")
    c.execute(
        "INSERT OR REPLACE INTO execution_attempts "
        "(attempt_id,run_id,attempt_ordinal,operation_role,profile_id,"
        "requested_provider,requested_model,selected_model,reported_model,"
        "resolved_provider,resolved_model,fallback_reason,runner_version,cli_version,"
        "status,tokens_in,tokens_out,latency_s,cost_usd,trace_key,"
        "source,raw_ref,started_ts,completed_ts,recorded_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            run_id,
            ordinal,
            role,
            profile_id,
            requested_provider,
            requested_model,
            selected_model,
            reported_model,
            resolved_provider,
            resolved_model,
            fallback_reason,
            runner_version,
            cli_version,
            status,
            int(tokens_in or 0),
            int(tokens_out or 0),
            float(latency_s or 0.0),
            float(cost_usd or 0.0),
            trace_key,
            source,
            raw_ref,
            started_ts,
            completed_ts,
            recorded_ts or int(time.time()),
        ),
    )
    attempt_payload = {
        "retry_sequence": [
            {
                "attempt_ordinal": ordinal,
                "operation_role": role,
                "profile_id": profile_id,
                "requested_provider": requested_provider,
                "requested_model": requested_model,
                "selected_model": selected_model,
                "reported_model": reported_model,
                "resolved_provider": resolved_provider,
                "resolved_model": resolved_model,
                "fallback_reason_id": (
                    _completion_hash(fallback_reason) if fallback_reason else None
                ),
                "runner_version": runner_version,
                "cli_version": cli_version,
                "status": status,
            }
        ],
        "result": {
            "operation_role": role,
            "status": status,
            "trace_key_hash": _completion_hash(trace_key) if trace_key else None,
        },
    }
    _record_completion_event_in_conn(
        c,
        run_id,
        event_type="attempt",
        phase="execution",
        producer=source or "feedback.execution_attempt",
        attempt_id=attempt_id,
        status=status,
        payload=attempt_payload,
        timestamp=recorded_ts,
    )
    if str(status or "").strip().lower() in SUCCESSFUL_ATTEMPT_STATUSES or completed_ts:
        _record_completion_event_in_conn(
            c,
            run_id,
            event_type="completion",
            phase="execution",
            producer=source or "feedback.execution_attempt",
            attempt_id=attempt_id,
            status=status or "complete",
            payload=attempt_payload,
            timestamp=completed_ts or recorded_ts,
        )


def record_execution_attempt(
    run_id: str,
    *,
    attempt_id: str | None = None,
    attempt_ordinal: int = 1,
    operation_role: str,
    profile_id: str | None = None,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    selected_model: str | None = None,
    reported_model: str | None = None,
    resolved_provider: str | None = None,
    resolved_model: str | None = None,
    fallback_reason: str | None = None,
    runner_version: str | None = None,
    cli_version: str | None = None,
    status: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_s: float = 0.0,
    cost_usd: float = 0.0,
    trace_key: str | None = None,
    source: str = "orchestrator",
    raw_ref: str | None = None,
    started_ts: int | None = None,
    completed_ts: int | None = None,
) -> str:
    """Record a non-trace or trace-linked execution attempt additively."""
    role = validate_operation_role(operation_role)
    key = (
        attempt_id
        or hashlib.sha256(
            "|".join(
                str(value or "")
                for value in (run_id, attempt_ordinal, role, trace_key, source, raw_ref)
            ).encode()
        ).hexdigest()[:24]
    )
    key = key if str(key).startswith("attempt:") else f"attempt:{key}"
    with _conn() as c:
        _record_execution_attempt_in_conn(
            c,
            run_id=run_id,
            attempt_id=key,
            attempt_ordinal=attempt_ordinal,
            operation_role=role,
            profile_id=profile_id,
            requested_provider=requested_provider,
            requested_model=requested_model,
            selected_model=selected_model,
            reported_model=reported_model,
            resolved_provider=resolved_provider,
            resolved_model=resolved_model,
            fallback_reason=fallback_reason,
            runner_version=runner_version,
            cli_version=cli_version,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_s=latency_s,
            cost_usd=cost_usd,
            trace_key=trace_key,
            source=source,
            raw_ref=raw_ref,
            started_ts=started_ts,
            completed_ts=completed_ts,
        )
    return key


def complete_profile_attempt(
    run_id: str,
    *,
    selected_profile_id: str,
    resolved_provider: str,
    resolved_model: str,
    status: str = "complete",
    completed_ts: int | None = None,
) -> str:
    """Complete the pre-dispatch profile attempt with observed resolved identity.

    The caller must supply an actually reported resolved model.  Requested model
    is never copied into resolved identity, and the existing attempt row is
    updated rather than creating a parallel identity.
    """
    if not str(resolved_model or "").strip():
        raise ValueError("profile completion requires actually reported resolved_model")
    # A SEPARATE BINDING, not a reassignment: `validate_resolved_worker_model` returns None for a
    # rejected adapter tag, and the write below must still see that None. Reassigning `resolved_model`
    # (typed `str`) widened it, and coercing the None away — the first fix attempted here — turned a
    # deliberate refusal into an empty string, which broke three provenance tests.
    validated_model: str | None = validate_resolved_worker_model(resolved_model)
    attempt_id = f"attempt:profile:{run_id}"
    with _conn() as c:
        row = c.execute(
            "SELECT profile_id FROM execution_attempts WHERE attempt_id=? AND run_id=? "
            "AND operation_role='worker'",
            (attempt_id, run_id),
        ).fetchone()
        if not row:
            raise ValueError(f"missing selected profile attempt for {run_id}")
        if row[0] != selected_profile_id:
            raise ValueError(f"selected profile changed for {run_id}")
        c.execute(
            # CLEAR THE FALLBACK REASON. An attempt that resolves did not fall back, but this UPDATE
            # left the earlier `resolved_model_not_reported_*` string in place -- so
            # `resolved_model_coverage` reported codex-5.6-terra-high at coverage 1.00 AND
            # fallback_rate 1.00 simultaneously, because the fallback SUM counts
            # `fallback_reason IS NOT NULL`. A fully-resolved profile reading as 100% fallback is a
            # metric that contradicts itself, and it appeared the moment a late sweep started
            # resolving attempts that had already been closed unresolved.
            "UPDATE execution_attempts SET resolved_provider=?,resolved_model=?,status=?,"
            "completed_ts=?,fallback_reason=NULL "
            "WHERE attempt_id=?",
            (
                resolved_provider,
                validated_model,
                status,
                int(completed_ts or time.time()),
                attempt_id,
            ),
        )
    return attempt_id


def complete_profile_attempt_unresolved(
    run_id: str,
    *,
    selected_profile_id: str,
    fallback_reason: str = "resolved_model_not_reported",
    status: str = "unresolved",
    completed_ts: int | None = None,
) -> str:
    """Close a selected profile attempt without inventing resolved identity.

    Some CLIs do not report the provider-resolved model in their bounded session
    output.  Such an attempt is still terminal telemetry: retain the requested
    profile, record an explicit fallback/unresolved reason, and leave both
    resolved identity columns NULL.  A later provider trace may enrich the row,
    but requested identity is never copied into resolved identity.
    """
    reason = str(fallback_reason or "").strip()
    if not reason:
        raise ValueError("unresolved profile completion requires fallback_reason")
    attempt_id = f"attempt:profile:{run_id}"
    with _conn() as c:
        row = c.execute(
            "SELECT profile_id,resolved_model FROM execution_attempts "
            "WHERE attempt_id=? AND run_id=? AND operation_role='worker'",
            (attempt_id, run_id),
        ).fetchone()
        if not row:
            raise ValueError(f"missing selected profile attempt for {run_id}")
        if row[0] != selected_profile_id:
            raise ValueError(f"selected profile changed for {run_id}")
        if row[1]:
            return attempt_id
        c.execute(
            "UPDATE execution_attempts SET resolved_provider=NULL,resolved_model=NULL,"
            "fallback_reason=?,status=?,completed_ts=? WHERE attempt_id=?",
            (reason, status, int(completed_ts or time.time()), attempt_id),
        )
    return attempt_id


def migrate_legacy_execution_attempts(
    *, apply: bool = False, conn: sqlite3.Connection | None = None
) -> dict:
    """Classify legacy traces conservatively without rewriting trace or run rows.

    Known evaluator/verifier/replay operations become non-worker attempts. Ambiguous
    operations are retained as ``unknown`` attempts, so they remain reportable but can
    never resolve worker identity. Re-running the migration is idempotent by trace key.
    """
    c = conn or _conn()
    close = conn is None
    before_traces = c.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0]
    before_runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    rows = c.execute(
        "SELECT et.trace_key,et.run_id,et.provider,et.model,et.operation,et.status,"
        "et.latency_s,et.cost_usd,et.source,et.raw_ref,et.pulled_ts,r.model,r.agent "
        "FROM execution_traces et LEFT JOIN runs r ON r.run_id=et.run_id "
        "WHERE NOT EXISTS (SELECT 1 FROM execution_attempts ea "
        "WHERE ea.trace_key=et.trace_key) ORDER BY et.trace_key"
    ).fetchall()
    by_role = {role: 0 for role in sorted(VALID_OPERATION_ROLES)}
    by_operation: dict[str, int] = {}
    collision_runs: set[str] = set()
    collision_runs_by_agent: dict[str, set[str]] = {}
    worker_like_left_unresolved = 0
    for (
        trace_key,
        run_id,
        provider,
        model,
        operation,
        status,
        latency_s,
        cost_usd,
        source,
        raw_ref,
        pulled_ts,
        legacy_run_model,
        run_agent,
    ) in rows:
        derived_role = derive_operation_role(operation)
        # Legacy rows did not carry a producer-validated operation_role. Even a
        # worker-like operation name is insufficient causal proof after the fact.
        if derived_role == "worker":
            role = "unknown"
            worker_like_left_unresolved += 1
        else:
            role = derived_role
        by_role[role] += 1
        operation_key = str(operation or "unknown")
        by_operation[operation_key] = by_operation.get(operation_key, 0) + 1
        if role in {"evaluator", "verifier", "replay"} and model and legacy_run_model == model:
            collision_runs.add(str(run_id))
            agent_key = str(run_agent or "unknown")
            collision_runs_by_agent.setdefault(agent_key, set()).add(str(run_id))
        if apply:
            _record_execution_attempt_in_conn(
                c,
                run_id=run_id,
                attempt_id=f"attempt:{trace_key}:1",
                attempt_ordinal=1,
                operation_role=role,
                resolved_provider=provider,
                resolved_model=model,
                status=status,
                latency_s=latency_s,
                cost_usd=cost_usd,
                trace_key=trace_key,
                source=source,
                raw_ref=raw_ref,
                completed_ts=pulled_ts,
                recorded_ts=pulled_ts,
            )
    if apply:
        c.commit()
    after_traces = c.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0]
    after_runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    attempts_after = c.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0]
    if close:
        c.close()
    return {
        "applied": bool(apply),
        "legacy_trace_rows_examined": len(rows),
        "classified_non_worker": sum(
            by_role[role]
            for role in ("evaluator", "verifier", "synthesizer", "role_backend", "replay")
        ),
        "ambiguous_worker_identity_unresolved": by_role["unknown"],
        "worker_like_operations_left_unresolved": worker_like_left_unresolved,
        "by_operation_role": {key: value for key, value in by_role.items() if value},
        "by_operation": dict(sorted(by_operation.items())),
        "legacy_worker_nonworker_model_collision_runs": len(collision_runs),
        "legacy_worker_nonworker_model_collision_run_ids": sorted(collision_runs)[:50],
        "legacy_worker_nonworker_model_collision_runs_by_agent": {
            agent: len(run_ids) for agent, run_ids in sorted(collision_runs_by_agent.items())
        },
        "trace_rows_before": before_traces,
        "trace_rows_after": after_traces,
        "run_rows_before": before_runs,
        "run_rows_after": after_runs,
        "attempt_rows_after": attempts_after,
        "legacy_rows_preserved": before_traces == after_traces and before_runs == after_runs,
    }


def worker_model_provenance_summary(window_days: int = 90) -> dict:
    """Coverage/collision summary for operator reports and dashboard health."""
    since = int(time.time()) - int(window_days) * 86400
    success_sql = _successful_attempt_sql("ea")
    with _conn() as c:
        runs_total = c.execute("SELECT COUNT(*) FROM runs WHERE ts>=?", (since,)).fetchone()[0]
        worker_eligible_sql = (
            "r.ts>=? AND COALESCE(r.role_name,'') IN ('','worker') "
            "AND LOWER(COALESCE(r.task_type,'')) NOT LIKE 'role:%' "
            "AND LOWER(COALESCE(r.task_type,'')) NOT LIKE 'evaluate%' "
            "AND LOWER(COALESCE(r.task_type,'')) NOT LIKE 'verify%' "
            "AND LOWER(COALESCE(r.task_type,'')) NOT LIKE 'replay%' "
            "AND LOWER(COALESCE(r.task_type,'')) NOT LIKE 'synthes%'"
        )
        eligible_worker_runs = c.execute(
            f"SELECT COUNT(*) FROM runs r WHERE {worker_eligible_sql}", (since,)
        ).fetchone()[0]
        worker_attempts = c.execute(
            "SELECT COUNT(*) FROM execution_attempts ea JOIN runs r ON r.run_id=ea.run_id "
            f"WHERE {worker_eligible_sql} AND ea.operation_role='worker'",
            (since,),
        ).fetchone()[0]
        requested_runs = c.execute(
            "SELECT COUNT(DISTINCT r.run_id) FROM runs r JOIN execution_attempts ea "
            f"ON ea.run_id=r.run_id WHERE {worker_eligible_sql} "
            "AND ea.operation_role='worker' "
            "AND (ea.profile_id IS NOT NULL OR ea.requested_provider IS NOT NULL "
            "OR ea.requested_model IS NOT NULL)",
            (since,),
        ).fetchone()[0]
        resolved_runs = c.execute(
            "SELECT COUNT(DISTINCT r.run_id) FROM runs r JOIN execution_attempts ea "
            f"ON ea.run_id=r.run_id WHERE {worker_eligible_sql} "
            "AND ea.operation_role='worker' "
            f"AND {success_sql} AND ea.resolved_model IS NOT NULL",
            (since,),
        ).fetchone()[0]
        role_rows = c.execute(
            "SELECT ea.operation_role,COUNT(*) FROM execution_attempts ea "
            "JOIN runs r ON r.run_id=ea.run_id WHERE r.ts>=? "
            "GROUP BY ea.operation_role ORDER BY ea.operation_role",
            (since,),
        ).fetchall()
        role_overlap = c.execute(
            "SELECT COUNT(*) FROM (SELECT r.run_id FROM runs r "
            "JOIN execution_attempts w ON w.run_id=r.run_id AND w.operation_role='worker' "
            "JOIN execution_attempts e ON e.run_id=r.run_id AND e.operation_role='evaluator' "
            "WHERE r.ts>=? GROUP BY r.run_id)",
            (since,),
        ).fetchone()[0]
        resolved_model_collision = c.execute(
            "SELECT COUNT(*) FROM (SELECT r.run_id FROM runs r "
            "JOIN execution_attempts w ON w.run_id=r.run_id AND w.operation_role='worker' "
            "JOIN execution_attempts e ON e.run_id=r.run_id AND e.operation_role='evaluator' "
            "WHERE r.ts>=? AND w.resolved_model IS NOT NULL "
            "AND w.resolved_model=e.resolved_model GROUP BY r.run_id)",
            (since,),
        ).fetchone()[0]
        legacy_collision = c.execute(
            "SELECT COUNT(*) FROM (SELECT r.run_id FROM runs r "
            "JOIN execution_attempts e ON e.run_id=r.run_id "
            "AND e.operation_role IN ('evaluator','verifier','replay') "
            "WHERE r.ts>=? AND r.model IS NOT NULL AND r.model=e.resolved_model "
            "AND NOT EXISTS (SELECT 1 FROM execution_attempts w WHERE w.run_id=r.run_id "
            "AND w.operation_role='worker' AND w.resolved_model IS NOT NULL AND "
            f"{_successful_attempt_sql('w')}) GROUP BY r.run_id)",
            (since,),
        ).fetchone()[0]
    migration_preview = migrate_legacy_execution_attempts(apply=False)
    unknown_runs = max(0, eligible_worker_runs - resolved_runs)
    denominator = max(1, eligible_worker_runs)
    legacy_collision = max(
        legacy_collision,
        int(migration_preview.get("legacy_worker_nonworker_model_collision_runs") or 0),
    )
    return {
        "window_days": window_days,
        "runs_total": runs_total,
        "eligible_worker_runs": eligible_worker_runs,
        "excluded_nonworker_runs": max(0, runs_total - eligible_worker_runs),
        "worker_attempts": worker_attempts,
        "requested_worker_runs": requested_runs,
        "resolved_worker_runs": resolved_runs,
        "unknown_worker_runs": unknown_runs,
        "requested_worker_coverage": requested_runs / denominator if eligible_worker_runs else None,
        "resolved_worker_coverage": resolved_runs / denominator if eligible_worker_runs else None,
        "unknown_worker_coverage": unknown_runs / denominator if eligible_worker_runs else None,
        "attempts_by_operation_role": dict(role_rows),
        "worker_evaluator_role_overlap_runs": role_overlap,
        "worker_evaluator_resolved_model_collision_runs": resolved_model_collision,
        "legacy_worker_nonworker_model_collision_runs": legacy_collision,
        "unmigrated_legacy_trace_rows": int(
            migration_preview.get("legacy_trace_rows_examined") or 0
        ),
        "legacy_migration_complete": not bool(migration_preview.get("legacy_trace_rows_examined")),
    }


def record_evaluation(experiment_id, implementer, evaluator, score, rank=None, verdict=None):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO evaluations VALUES (?,?,?,?,?,?,?)",
            (
                experiment_id,
                implementer,
                evaluator,
                score,
                rank,
                json.dumps(verdict) if verdict else None,
                int(time.time()),
            ),
        )


def record_evaluation_v2(
    *,
    experiment_id,
    implementer_arm_id,
    implementer_member_id,
    implementation_agent,
    evaluator_id,
    evaluator_agent,
    score,
    rank=None,
    verdict=None,
    implementer_profile_id=None,
    evaluator_arm_id=None,
    evaluator_profile_id=None,
):
    """Record one causally identified evaluation alongside the legacy projection."""
    if not all(
        str(value or "").strip()
        for value in (
            experiment_id,
            implementer_arm_id,
            implementer_member_id,
            implementation_agent,
            evaluator_id,
            evaluator_agent,
        )
    ):
        raise ValueError("evaluations_v2 requires exact experiment/member/evaluator identity")
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO evaluations_v2 "
            "(experiment_id, implementer_arm_id, implementer_member_id, "
            "implementer_profile_id, implementation_agent, evaluator_id, "
            "evaluator_arm_id, evaluator_profile_id, evaluator_agent, "
            "score, rank, verdict, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                experiment_id,
                implementer_arm_id,
                implementer_member_id,
                implementer_profile_id,
                implementation_agent,
                evaluator_id,
                evaluator_arm_id,
                evaluator_profile_id,
                evaluator_agent,
                score,
                rank,
                json.dumps(verdict) if verdict else None,
                int(time.time()),
            ),
        )


def record_human_calibration(ref, human_verdict, note=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO human_calibration VALUES (?,?,?,?)",
            (int(time.time()), ref, human_verdict, note),
        )


def _is_success(durability: str, adjudicated: str | None, verifier: str | None = None) -> bool:
    """Goal-success = a PASS verdict that DURABLY held (the un-gameable part). A merge that
    reverts/reworks/reopens later is a failure the verdict missed. A verifier failure such
    as FAIL_HOLLOW is also a failure even if the PR later merged."""
    if (verifier or "").upper() in VERIFIER_FAILURES:
        return False
    if durability in ("reverted", "reworked", "reopened", "broke_later", "abandoned"):
        return False
    if durability == "durable":
        return True
    # still 'pending' durability: provisionally credit a PASS, but it's not yet confirmed
    return (adjudicated or "").upper() == "PASS"


def relearn(task_type_priors: dict, window_days: int = 90) -> int:
    """Re-estimate per-(task_type, agent) weights from retained outcomes+costs and write a NEW
    versioned route_weights row set. Beta-Binomial: posterior_success = (k*prior + successes)/(k+n);
    score = posterior_success / effective_cost_per_success. Low n -> prior dominates.
    `task_type_priors` = {task_type: {agent: prior_success_in_[0,1]}}. Returns the new version.

    Cost imputation (2026-07-03 audit F1): cost telemetry is sparse (killed ledger completions,
    starved remote artifact chain), and the old `score = post/cps if cps else post` treated a
    MISSING cost as FREE — the only measured cell (implement/codex) scored lowest in its row while
    unmeasured peers kept their raw posteriors. Absence of data must not beat presence of data:
    a cell with no measured cost is imputed from (a) the agent's own measured cps across task
    types, else (b) the global median measured cell cps. Only when NOTHING is measured anywhere
    does score fall back to the raw posterior (a row-constant divisor cannot change ranking).
    The stored cost_per_success column stays the MEASURED value (never fabricated); the rationale
    records cps_src so every weight stays auditable.
    """
    now = int(time.time())
    since = now - window_days * 86400
    with _conn() as c:
        ver = (c.execute("SELECT COALESCE(MAX(version),0) FROM route_weights").fetchone()[0]) + 1
        # Pass 1: per-cell outcome + measured-cost stats. Measured = cost_usd > 0 only — the $0
        # ledger rows are the killed-completion class (audit F2), not evidence of free work.
        cells: dict[tuple[str, str], dict] = {}
        for task_type, priors in task_type_priors.items():
            for agent, prior in priors.items():
                rows = c.execute(
                    "SELECT o.durability, o.adjudicated_verdict, o.verifier_verdict, co.cost_usd "
                    "FROM runs r JOIN outcomes o ON r.run_id=o.run_id LEFT JOIN costs co ON r.run_id=co.run_id "
                    "WHERE r.task_type=? AND r.agent=? AND r.ts>=? "
                    "AND COALESCE(r.assignment,'experimental')='experimental' "
                    "AND COALESCE(o.failure_class,'') != 'transient_infra'",  # item 9: infra != capability
                    (task_type, agent, since),
                ).fetchall()
                n = len(rows)
                succ = sum(1 for d, a, v, _ in rows if _is_success(d, a, v))
                measured = [cu for d, a, v, cu in rows if _is_success(d, a, v) and cu and cu > 0]
                cells[(task_type, agent)] = {
                    "prior": prior,
                    "n": n,
                    "succ": succ,
                    "m_cost": sum(measured),
                    "m_succ": len(measured),
                }
        # Imputation pools: the agent's own measured cps (mean over its measured cells), then the
        # global median of measured cell cps.
        agent_cps: dict[str, list[float]] = {}
        cell_cps: list[float] = []
        for (_task_type, agent), s in cells.items():
            if s["m_succ"]:
                cps_cell = s["m_cost"] / s["m_succ"]
                agent_cps.setdefault(agent, []).append(cps_cell)
                cell_cps.append(cps_cell)
        agent_global = {a: sum(v) / len(v) for a, v in agent_cps.items()}
        global_median = sorted(cell_cps)[len(cell_cps) // 2] if cell_cps else None
        # Pass 2: score with the effective cps and write the version.
        for (task_type, agent), s in cells.items():
            post = (PRIOR_STRENGTH * s["prior"] + s["succ"]) / (PRIOR_STRENGTH + s["n"])
            cps = (s["m_cost"] / s["m_succ"]) if s["m_succ"] else None
            if cps is not None:
                cps_eff, cps_src = cps, "measured"
            elif agent in agent_global:
                cps_eff, cps_src = agent_global[agent], "agent_global"
            elif global_median is not None:
                cps_eff, cps_src = global_median, "global_median"
            else:
                cps_eff, cps_src = None, "none"
            score = (
                (post / max(cps_eff, CPS_FLOOR)) if cps_eff else post
            )  # capacity-per-verified-success; imputed when unmeasured, raw post only if NOTHING measured
            c.execute(
                "INSERT OR REPLACE INTO route_weights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ver,
                    now,
                    task_type,
                    agent,
                    s["prior"],
                    post,
                    s["n"],
                    (s["succ"] / s["n"]) if s["n"] else None,
                    cps,
                    score,
                    f"k={PRIOR_STRENGTH} n={s['n']} succ={s['succ']} cps_src={cps_src}",
                    since,
                    now,
                ),
            )
        return ver


def current_weights(task_type: str, version: int | None = None) -> list[dict]:
    """Learned routing order for a task_type (descending score). The router consults this;
    the hand-set ROUTE_TABLE remains the prior/floor. Empty until relearn() has run."""
    with _conn() as c:
        if version is None:
            version = c.execute("SELECT COALESCE(MAX(version),0) FROM route_weights").fetchone()[0]
        rows = c.execute(
            "SELECT agent, posterior, score, n_obs FROM route_weights "
            "WHERE version=? AND task_type=? ORDER BY score DESC",
            (version, task_type),
        ).fetchall()
        return [{"agent": a, "posterior": p, "score": s, "n_obs": n} for a, p, s, n in rows]


def runs_needing_outcome(mode: str | None = None) -> list:
    """Runs with no recorded outcome yet, OR whose durability is still 'pending' — the work list for the
    outcome/durability sweep (gate #3). Filter by mode (e.g. 'remote' for keepalive-delegated runs).
    """
    with _conn() as c:
        q = (
            "SELECT r.run_id, r.target FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE (o.run_id IS NULL OR o.durability='pending')"
        )
        params = []
        if mode:
            q += " AND r.mode=?"
            params.append(mode)
        return [{"run_id": rid, "target": t} for rid, t in c.execute(q, params).fetchall()]


def latest_run_id_for_target(target: str, mode: str | None = None) -> str | None:
    """Return the most recent run_id recorded for a target, optionally scoped by mode."""
    with _conn() as c:
        q = "SELECT run_id FROM runs WHERE target=?"
        params: list = [target]
        if mode:
            q += " AND mode=?"
            params.append(mode)
        q += " ORDER BY ts DESC LIMIT 1"
        row = c.execute(q, params).fetchone()
    return row[0] if row else None


def snapshot_json(path=None) -> dict:
    """Dump the live store to a human-readable JSON snapshot for the Code/Orchestrator project. The live
    SQLite stays on local disk (Dropbox-safe); THIS is the reviewable, version-controllable copy of the
    growing dataset. Defaults to <project>/data/feedback-snapshot.json."""
    path = Path(path or ORCH / "data" / "feedback-snapshot.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = [
        "runs",
        "outcomes",
        "costs",
        "execution_traces",
        "execution_attempts",
        "completion_events",
        "influence_edges",
        "route_weights",
        "execution_profiles",
        "capacity_pools",
        "execution_profile_pools",
        "routing_decisions_v2",
        "route_weights_v2",
        "evaluations",
        "evaluations_v2",
        "human_calibration",
        "evidence_gaps",
        "evidence_types",
    ]
    dump = {}
    with _conn() as c:
        c.row_factory = sqlite3.Row
        for t in tables:
            dump[t] = [dict(r) for r in c.execute(f"SELECT * FROM {t}").fetchall()]
    path.write_text(json.dumps(dump, indent=2, default=str))
    return {"path": str(path), "rows": {t: len(v) for t, v in dump.items()}}


def record_profile_decision(envelope: dict) -> str:
    """Persist one replayable profile-selection envelope in the additive v2 plane."""
    import execution_profiles

    with _conn() as c:
        execution_profiles.record_decision(c, envelope)
    return str(envelope["decision_id"])


def attach_profile_attempt_to_decision(decision_id: str, attempt_id: str) -> list[str]:
    """Persist the real profile-attempt ID on its replayable route decision."""
    import execution_profiles

    with _conn() as c:
        return execution_profiles.attach_profile_attempt(c, decision_id, attempt_id)


def relearn_profiles(task_type_priors: dict) -> int:
    """Write shadow-only profile posteriors without modifying v1 route weights."""
    import execution_profiles

    with _conn() as c:
        return execution_profiles.relearn_route_weights_v2(c, task_type_priors)


def profile_routing_summary() -> dict:
    import execution_profiles

    with _conn() as c:
        return execution_profiles.report(c)


def record_evidence_gap(ref: str, evaluator: str, gap: str):
    """An evaluator declares what it lacked to judge better. The raw material for schema growth."""
    with _conn() as c:
        c.execute(
            "INSERT INTO evidence_gaps VALUES (?,?,?,?,?)",
            (int(time.time()), ref, evaluator, gap, "open"),
        )


def propose_evidence_changes(min_recurrence: int = 3, window_days: int = 120) -> list[dict]:
    """Aggregate open evidence-gaps; a gap recurring >= threshold becomes a proposed new evidence type.
    This is the 'identify what data was needed -> add to reporting' step (human approves before schema
    migration via record_evidence_type)."""
    since = int(time.time()) - window_days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT gap, COUNT(*) n FROM evidence_gaps WHERE status='open' AND ts>=? "
            "GROUP BY gap HAVING n>=? ORDER BY n DESC",
            (since, min_recurrence),
        ).fetchall()
    return [{"gap": g, "recurrence": n, "proposal": f"add evidence type for: {g}"} for g, n in rows]


def record_evidence_type(name: str, rationale: str = ""):
    """Approve a new evidence type into the (versioned) registry — the schema GROWS here."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO evidence_types VALUES (?,?,?,?,?)",
            (name, int(time.time()), 0, "active", rationale),
        )


def active_evidence_types() -> list[dict]:
    """Return active evidence-type registry rows for evaluator prompt contracts."""
    with _conn() as c:
        rows = c.execute(
            "SELECT name, added_ts, influence, rationale FROM evidence_types "
            "WHERE status='active' ORDER BY name"
        ).fetchall()
    return [
        {
            "name": name,
            "added_ts": added_ts,
            "influence": influence,
            "rationale": rationale,
        }
        for name, added_ts, influence, rationale in rows
    ]


def normalize_evidence_type_citations(raw) -> list[str]:
    """Normalize reviewer-supplied evidence-type citations without trusting them."""
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            name = value.get("name") or value.get("evidence_type") or value.get("type") or ""
        else:
            name = str(value)
        name = " ".join(str(name).strip().split())
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name[:160])
    return out


def record_evidence_type_citations(raw) -> list[str]:
    """Increment influence for cited active evidence types and ignore unknown names."""
    names = normalize_evidence_type_citations(raw)
    if not names:
        return []
    with _conn() as c:
        active = {
            row[0]
            for row in c.execute("SELECT name FROM evidence_types WHERE status='active'").fetchall()
        }
        applied: list[str] = []
        for name in names:
            if name not in active:
                continue
            c.execute(
                "UPDATE evidence_types SET influence=influence+1 WHERE name=?",
                (name,),
            )
            applied.append(name)
    return applied


def approve_evidence_type(
    name: str,
    gap: str,
    *,
    rationale: str = "",
    min_recurrence: int = 3,
    window_days: int = 120,
    apply: bool = False,
) -> dict:
    """Preview or apply approval of a recurring evidence gap as an evidence type.

    The periodic report surfaces recurring evidence gaps as schema-growth proposals.
    This helper is the approval step: preview by default; with apply=True, insert or
    reactivate the evidence type and mark matching open gap rows as approved. The
    write path is recurrence-gated so a one-off evaluator complaint cannot silently
    become a new captured evidence type.
    """
    name = (name or "").strip()
    gap = (gap or "").strip()
    if not name:
        raise ValueError("evidence type name is required")
    if not gap:
        raise ValueError("evidence gap text is required")

    now = int(time.time())
    since = now - window_days * 86400
    with _conn() as c:
        if apply:
            c.execute("BEGIN IMMEDIATE")
        recurrence = c.execute(
            "SELECT COUNT(*) FROM evidence_gaps WHERE gap=? AND status='open' AND ts>=?",
            (gap, since),
        ).fetchone()[0]
        eligible = recurrence >= min_recurrence
        existing = c.execute(
            "SELECT status, rationale FROM evidence_types WHERE name=?", (name,)
        ).fetchone()
        result = {
            "name": name,
            "gap": gap,
            "recurrence": recurrence,
            "min_recurrence": min_recurrence,
            "window_days": window_days,
            "eligible": eligible,
            "applied": False,
            "preview": not apply,
            "gaps_marked": 0,
            "rationale": rationale or None,
            "evidence_type_status": existing[0] if existing else "new",
        }
        if not apply:
            return result
        if not eligible:
            result["blocked_reason"] = f"recurrence {recurrence} < min_recurrence {min_recurrence}"
            return result
        if existing:
            if existing[0] == "retired":
                c.execute(
                    "UPDATE evidence_types SET status='active', rationale=? WHERE name=?",
                    (rationale or existing[1] or "", name),
                )
                result["evidence_type_status"] = "reactivated"
            else:
                if rationale:
                    c.execute(
                        "UPDATE evidence_types SET rationale=? WHERE name=?",
                        (rationale, name),
                    )
                result["evidence_type_status"] = "already_active"
        else:
            c.execute(
                "INSERT INTO evidence_types VALUES (?,?,?,?,?)",
                (name, now, 0, "active", rationale),
            )
            result["evidence_type_status"] = "active"
        cur = c.execute(
            "UPDATE evidence_gaps SET status='approved' WHERE gap=? AND status='open' AND ts>=?",
            (gap, since),
        )
        result["gaps_marked"] = cur.rowcount
    result["applied"] = True
    result["preview"] = False
    return result


def bump_evidence_influence(name: str):
    """An evidence type was actually CITED in a verdict — track influence so dead weight can be pruned."""
    with _conn() as c:
        c.execute("UPDATE evidence_types SET influence=influence+1 WHERE name=?", (name,))


def prune_dead_evidence(min_influence: int = 1) -> list[str]:
    """Evidence types never cited get retired — the dataset stays lean, not hoarded. Returns retired names."""
    with _conn() as c:
        dead = [
            r[0]
            for r in c.execute(
                "SELECT name FROM evidence_types WHERE status='active' AND influence < ?",
                (min_influence,),
            ).fetchall()
        ]
        c.executemany(
            "UPDATE evidence_types SET status='retired' WHERE name=?",
            [(n,) for n in dead],
        )
    return dead


def _has_outcome_evidence(
    durability: str | None, adjudicated: str | None, verifier: str | None
) -> bool:
    if verifier is not None or adjudicated is not None:
        return True
    return durability in {
        "durable",
        "reverted",
        "reworked",
        "reopened",
        "broke_later",
        "abandoned",
    }


def relearn_quality(task_type_priors: dict, window_days: int = 120) -> int:
    """Quality-MAGNITUDE learner (fixes the binary-label + free-agent-cost flaws). Per (task_type, agent):
    reward q is one observation per run: mean(eval_score)/QUALITY_MAX when a run has cross-eval scores,
    otherwise 1.0/0.0 from production outcome+durability when a run has outcome evidence. This lets real
    production outcomes shape routing without double-counting evaluated runs or multi-reviewer panels.
    posterior = (k·prior + Σq)/(k + n); score = posterior times conservative effort multipliers
    over mean MEASURED cost, tokens, and latency (AVG(NULLIF(x,0)) — $0/0-token rows are the
    killed-completion class, audit F2, not evidence of free effort). Unmeasured effort is IMPUTED
    (agent's own measured mean across task types, else the global median cell value) so silence
    cannot earn the best multiplier (2026-07-03 audit F1); a metric measured NOWHERE contributes a
    row-constant zero penalty (cannot change ranking), which also keeps cold-start behavior intact.

    Recency decay (audit item 16a): each evidence weight is halved every
    ORCH_RELEARN_HALF_LIFE_DAYS (default 30) so fresh outcomes dominate stale ones — agents get
    silent model/prompt upgrades the supersession discount can't see. <=0 disables decay.

    If the latest known model/mode for an agent differs from the model that produced an older observation,
    that superseded evidence is discounted. Unknown/NULL models are treated as current for backward
    compatibility with pre-migration rows.
    """
    now = int(time.time())
    since = now - window_days * 86400
    try:
        half_life = float(
            os.environ.get("ORCH_RELEARN_HALF_LIFE_DAYS", "") or DEFAULT_RELEARN_HALF_LIFE_DAYS
        )
    except ValueError:
        half_life = DEFAULT_RELEARN_HALF_LIFE_DAYS
    with _conn() as c:
        ver = (c.execute("SELECT COALESCE(MAX(version),0) FROM route_weights").fetchone()[0]) + 1
        # Pass 1: per-cell quality evidence + MEASURED-only effort telemetry.
        cells: dict[tuple[str, str], dict] = {}
        for task_type, priors in task_type_priors.items():
            try:
                import research_subjects

                subject_weights = research_subjects.effective_evidence_weights(
                    conn=c, task_type=task_type
                )
            except Exception:
                subject_weights = {}
            for agent, prior in priors.items():
                current_model = _latest_model_for_agent(c, agent)
                model_expr = _run_model_expr()
                exact_scores = {
                    (experiment_id, member_id): score / QUALITY_MAX
                    for experiment_id, member_id, score in c.execute(
                        "SELECT experiment_id,implementer_member_id,AVG(score) "
                        "FROM evaluations_v2 WHERE implementation_agent=? "
                        "GROUP BY experiment_id,implementer_member_id",
                        (agent,),
                    ).fetchall()
                }
                legacy_scores = {
                    experiment_id: score / QUALITY_MAX
                    for experiment_id, score in c.execute(
                        "SELECT experiment_id,AVG(score) FROM evaluations "
                        "WHERE implementer=? GROUP BY experiment_id",
                        (agent,),
                    ).fetchall()
                }
                eval_q_by_run = {}
                for run_id, experiment_id, raw_metadata, run_model in c.execute(
                    f"SELECT r.run_id,r.experiment_id,r.routing_metadata,{model_expr} "
                    "FROM runs r WHERE r.task_type=? AND r.agent=? AND r.ts>=? "
                    "AND COALESCE(r.assignment,'experimental')='experimental' "
                    "AND r.experiment_id IS NOT NULL",
                    (task_type, agent, since),
                ).fetchall():
                    try:
                        metadata = json.loads(raw_metadata) if raw_metadata else {}
                    except (TypeError, json.JSONDecodeError):
                        metadata = {}
                    member_id = metadata.get("experiment_member_id")
                    score = (
                        exact_scores.get((experiment_id, str(member_id)))
                        if member_id
                        else legacy_scores.get(experiment_id)
                    )
                    if score is not None:
                        eval_q_by_run[run_id] = (score, run_model)
                weighted_qs = []
                outcome_n = 0
                raw_n = 0
                subject_n_eff = 0.0
                superseded_n = 0
                evidence_run_ids: list[str] = []
                for (
                    run_id,
                    durability,
                    adjudicated,
                    verifier,
                    run_model,
                    run_ts,
                    failure_class,
                ) in c.execute(
                    f"SELECT r.run_id, o.durability, o.adjudicated_verdict, o.verifier_verdict, {model_expr}, r.ts, o.failure_class "
                    "FROM runs r LEFT JOIN outcomes o ON r.run_id=o.run_id "
                    "WHERE r.task_type=? AND r.agent=? AND r.ts>=? "
                    "AND COALESCE(r.assignment,'experimental')='experimental'",
                    (task_type, agent, since),
                ).fetchall():
                    if run_id in eval_q_by_run:
                        # eval scores stay usable even on infra-classified runs (judges scored
                        # the arm's real diff); only the OUTCOME label below is infra noise.
                        q, run_model = eval_q_by_run[run_id]
                        raw_n += 1
                    elif str(failure_class or "") == "transient_infra":
                        continue  # item 9: infra death is not capability evidence
                    elif _has_outcome_evidence(durability, adjudicated, verifier):
                        q = 1.0 if _is_success(durability, adjudicated, verifier) else 0.0
                        outcome_n += 1
                        raw_n += 1
                    else:
                        continue
                    subject_weight = float(subject_weights.get(run_id, 1.0))
                    subject_n_eff += subject_weight
                    weight = subject_weight
                    if current_model and run_model and run_model != current_model:
                        weight *= SUPERSEDED_MODEL_WEIGHT
                        superseded_n += 1
                    if half_life > 0 and run_ts:
                        # Recency decay: evidence half-lives so fresh outcomes dominate stale ones
                        # (item 16a). Applies ON TOP of the supersession discount. DAY granularity:
                        # sub-day age is noise against a 30d half-life, and integer days keep two
                        # same-day relearn calls byte-identical (second-granularity decay made
                        # strict-equality selftests flaky across a second boundary).
                        age_days = max(0, (now - int(run_ts)) // 86400)
                        weight *= 0.5 ** (age_days / half_life)
                    weighted_qs.append((q, weight))
                    evidence_run_ids.append(run_id)
                n_eff = sum(w for _, w in weighted_qs)
                sq = sum(q * w for q, w in weighted_qs)
                post = (PRIOR_STRENGTH * prior + sq) / (PRIOR_STRENGTH + n_eff)
                if evidence_run_ids:
                    placeholders = ",".join("?" for _ in evidence_run_ids)
                    # NULLIF: average only MEASURED metrics — $0/0-token/0-latency rows are the
                    # killed-completion class (audit F2), not evidence of free effort.
                    costrow = c.execute(
                        "SELECT AVG(NULLIF(cost_usd,0)), AVG(NULLIF(tokens_in + tokens_out,0)), "
                        "AVG(NULLIF(latency_s,0)) "
                        f"FROM costs WHERE run_id IN ({placeholders})",
                        evidence_run_ids,
                    ).fetchone()
                else:
                    costrow = None
                cells[(task_type, agent)] = {
                    "prior": prior,
                    "post": post,
                    "raw_n": raw_n,
                    "subject_n_eff": subject_n_eff,
                    "n_eff": n_eff,
                    "sq": sq,
                    "eval_runs": len(eval_q_by_run),
                    "outcome_n": outcome_n,
                    "superseded_n": superseded_n,
                    "cost": costrow[0] if costrow else None,
                    "tokens": costrow[1] if costrow else None,
                    "latency": costrow[2] if costrow else None,
                }
        # Imputation pools per metric (2026-07-03 audit F1): an agent with NO measured effort must
        # not earn the best multiplier by silence. Effective effort = measured, else the agent's own
        # measured mean across task types, else the global median cell value. A metric measured
        # NOWHERE contributes a row-constant zero (cannot change ranking) — cold start unchanged.
        metrics = ("cost", "tokens", "latency")
        agent_pool: dict[str, dict[str, list[float]]] = {m: {} for m in metrics}
        global_pool: dict[str, list[float]] = {m: [] for m in metrics}
        for (_task_type, pool_agent), s in cells.items():
            for m in metrics:
                if s[m]:
                    agent_pool[m].setdefault(pool_agent, []).append(float(s[m]))
                    global_pool[m].append(float(s[m]))
        agent_mean = {m: {a: sum(v) / len(v) for a, v in agent_pool[m].items()} for m in metrics}
        global_median = {
            m: (sorted(global_pool[m])[len(global_pool[m]) // 2] if global_pool[m] else None)
            for m in metrics
        }

        def _effective(cell_agent: str, s: dict, m: str) -> tuple[float | None, str]:
            if s[m]:
                return float(s[m]), "m"
            if cell_agent in agent_mean[m]:
                return agent_mean[m][cell_agent], "a"
            if global_median[m] is not None:
                return global_median[m], "g"
            return 0.0, "0"

        # Pass 2: score with effective effort and write the version.
        for (task_type, agent), s in cells.items():
            cost, cost_src = _effective(agent, s, "cost")
            mean_tokens, tokens_src = _effective(agent, s, "tokens")
            mean_latency_s, latency_src = _effective(agent, s, "latency")
            effort_penalty = (
                LAMBDA_COST * cost
                + LAMBDA_TOKEN_MTOK * (mean_tokens / 1_000_000.0)
                + LAMBDA_LATENCY_MIN * (mean_latency_s / 60.0)
            )
            score = s["post"] * math.exp(-effort_penalty)
            c.execute(
                "INSERT OR REPLACE INTO route_weights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ver,
                    now,
                    task_type,
                    agent,
                    s["prior"],
                    s["post"],
                    int(round(s["subject_n_eff"])),
                    (s["sq"] / s["n_eff"]) if s["n_eff"] else None,
                    s["cost"] if s["cost"] else 0.0,  # column stays MEASURED (never fabricated)
                    score,
                    (
                        f"quality+outcome k={PRIOR_STRENGTH} n={int(round(s['subject_n_eff']))} "
                        f"raw_run_n={s['raw_n']} independent_subject_n={s['subject_n_eff']:.1f} "
                        f"eff_n={s['n_eff']:.1f} "
                        f"eval_runs={s['eval_runs']} outcome_runs={s['outcome_n']} "
                        f"superseded_model_runs={s['superseded_n']} meanq={(s['sq']/s['n_eff']) if s['n_eff'] else 0:.2f} "
                        f"mean_cost={cost:.4f} mean_tokens={mean_tokens:.0f} "
                        f"mean_latency_s={mean_latency_s:.1f} effort_penalty={effort_penalty:.4f} "
                        f"effort_src={cost_src}{tokens_src}{latency_src} half_life_d={half_life:g}"
                    ),
                    since,
                    now,
                ),
            )
        return ver


def _selftest():
    import tempfile

    import env_prereq  # imported here: this module is env_prereq's own dep

    tmp = tempfile.mkdtemp(prefix="feedback-selftest-")
    gaps: list[str] = []
    global DB_PATH
    DB_PATH = Path(tmp) / "t.db"
    try:
        legacy = sqlite3.connect(":memory:")
        try:
            legacy.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts INTEGER, source TEXT)")
            legacy.execute("CREATE TABLE outcomes (run_id TEXT PRIMARY KEY)")
            legacy.execute(
                "INSERT INTO runs (run_id, ts, source) VALUES ('old-keepalive', 1, 'keepalive')"
            )
            legacy.execute(
                "INSERT INTO runs (run_id, ts, source) VALUES ('old-router', 2, 'orchestrator_remote')"
            )
            _migrate_schema(legacy)
            migrated = dict(
                legacy.execute("SELECT run_id, assignment FROM runs ORDER BY run_id").fetchall()
            )
            assert migrated == {
                "old-keepalive": "assigned",
                "old-router": "experimental",
            }, migrated
            before = [row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()].count(
                "assignment"
            )
            before_work_type = [
                row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()
            ].count("work_type")
            before_role_name = [
                row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()
            ].count("role_name")
            before_routing_metadata = [
                row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()
            ].count("routing_metadata")
            before_influenced = [
                row[1] for row in legacy.execute("PRAGMA table_info(outcomes)").fetchall()
            ].count("influenced_by_run_id")
            _migrate_schema(legacy)
            after = [row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()].count(
                "assignment"
            )
            after_work_type = [
                row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()
            ].count("work_type")
            after_role_name = [
                row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()
            ].count("role_name")
            after_routing_metadata = [
                row[1] for row in legacy.execute("PRAGMA table_info(runs)").fetchall()
            ].count("routing_metadata")
            after_influenced = [
                row[1] for row in legacy.execute("PRAGMA table_info(outcomes)").fetchall()
            ].count("influenced_by_run_id")
            assert before == 1 and after == 1, (before, after)
            assert before_work_type == 1 and after_work_type == 1, (
                before_work_type,
                after_work_type,
            )
            assert before_role_name == 1 and after_role_name == 1, (
                before_role_name,
                after_role_name,
            )
            assert before_routing_metadata == 1 and after_routing_metadata == 1, (
                before_routing_metadata,
                after_routing_metadata,
            )
            assert before_influenced == 1 and after_influenced == 1, (
                before_influenced,
                after_influenced,
            )
        finally:
            legacy.close()

        priors = {"implement": {"claude": 0.7, "cursor": 0.5}}
        # No data yet -> relearn yields the PRIOR (posterior == prior).
        v0 = relearn(priors)
        w0 = {x["agent"]: x for x in current_weights("implement", v0)}
        assert abs(w0["claude"]["posterior"] - 0.7) < 1e-9 and w0["claude"]["n_obs"] == 0, w0

        # Feed evidence that contradicts the prior: cursor succeeds durably + cheap; claude reverts.
        for i in range(12):
            rid = f"r{i}"
            record_run(rid, f"o/r#{i}", "implement", "cursor" if i % 2 else "claude")
            if i % 2:  # cursor: durable success, cheap
                record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
                record_cost(rid, cost_usd=1.0)
            else:  # claude: merged but reverted later (durability catches the verdict-miss)
                record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="reverted")
                record_cost(rid, cost_usd=5.0)
        v1 = relearn(priors)
        w1 = {x["agent"]: x for x in current_weights("implement", v1)}
        # cursor's posterior should rise toward its durable-success; claude's should fall (reverts).
        assert w1["cursor"]["posterior"] > w1["claude"]["posterior"], w1
        # and the learned order now puts cursor first DESPITE the prior favoring claude — evidence won.
        assert current_weights("implement", v1)[0]["agent"] == "cursor", current_weights(
            "implement", v1
        )

        # F1 regression (2026-07-03 audit): a MISSING cost must not beat a MEASURED one. Two agents
        # with IDENTICAL outcomes; only one has cost rows. The old formula scored the unmeasured
        # agent at its raw posterior (missing == free) and divided the measured one — here they must
        # come out identical (the unmeasured cell is imputed from the global median, which equals the
        # measured cell's cps in this two-agent setup), with provenance recorded in the rationale.
        for i in range(6):
            for imp_agent in ("paid", "dark"):
                rid = f"imp-{imp_agent}-{i}"
                record_run(rid, f"o/imp#{imp_agent}{i}", "imputetest", imp_agent)
                record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
                if imp_agent == "paid":
                    record_cost(rid, cost_usd=2.0)
        vi = relearn({"imputetest": {"paid": 0.5, "dark": 0.5}})
        wi = {x["agent"]: x for x in current_weights("imputetest", vi)}
        assert abs(wi["paid"]["score"] - wi["dark"]["score"]) < 1e-9, wi
        with _conn() as c:
            srcs = dict(
                c.execute(
                    "SELECT agent, rationale FROM route_weights WHERE version=? AND task_type='imputetest'",
                    (vi,),
                ).fetchall()
            )
        assert "cps_src=measured" in srcs["paid"], srcs
        assert "cps_src=global_median" in srcs["dark"], srcs

        # Verifier failures override plausible merge/durability labels so hollow tests do not train as success.
        record_run("hollow", "o/r#hollow", "verifytest", "cursor")
        record_outcome(
            "hollow",
            verifier_verdict="FAIL_HOLLOW",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        vh = relearn({"verifytest": {"cursor": 0.5}})
        hollow_weight = current_weights("verifytest", vh)[0]
        assert hollow_weight["n_obs"] == 1 and hollow_weight["posterior"] < 0.5, hollow_weight

        record_run("runtime-ac-fail", "o/r#runtime-ac", "verifytest", "cursor")
        record_outcome(
            "runtime-ac-fail",
            verifier_verdict="FAIL_RUNTIME_AC",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        vr = relearn({"verifytest": {"cursor": 0.5}})
        runtime_weight = current_weights("verifytest", vr)[0]
        assert runtime_weight["n_obs"] == 2 and runtime_weight["posterior"] < 0.5, runtime_weight
        record_run("older-target", "o/r#target", "implement", "cursor", mode="remote", ts=100)
        record_run("newer-target", "o/r#target", "implement", "codex", mode="remote", ts=200)
        record_run("local-target", "o/r#target", "implement", "vibe", mode="local", ts=300)
        assert latest_run_id_for_target("o/r#target") == "local-target"
        assert latest_run_id_for_target("o/r#target", mode="remote") == "newer-target"
        with _conn() as c:
            sources = {
                rid: (source, assignment)
                for rid, source, assignment in c.execute(
                    "SELECT run_id, source, assignment FROM runs WHERE run_id IN "
                    "('older-target','newer-target','local-target')"
                ).fetchall()
            }
        assert sources == {
            "older-target": ("orchestrator_remote", "experimental"),
            "newer-target": ("orchestrator_remote", "experimental"),
            "local-target": ("orchestrator_local", "experimental"),
        }, sources
        record_run("work-type-run", "o/r#work-type", "implement", "codex", work_type="sync")
        record_run("work-type-run", "o/r#work-type", "implement", "codex", mode="remote")
        with _conn() as c:
            work_type_row = c.execute(
                "SELECT work_type, mode FROM runs WHERE run_id='work-type-run'"
            ).fetchone()
        assert work_type_row == ("sync", "remote"), work_type_row
        record_run(
            "routing-meta-run",
            "o/r#routing-meta",
            "implement",
            "codex",
            routing_metadata={
                "source": "router_assignment",
                "exploration": True,
                "exploration_mode": "thompson-hybrid",
            },
        )
        record_run("routing-meta-run", "o/r#routing-meta", "implement", "codex", mode="local")
        with _conn() as c:
            assignment, routing_metadata = c.execute(
                "SELECT assignment, routing_metadata FROM runs WHERE run_id='routing-meta-run'"
            ).fetchone()
        assert assignment == "experimental", assignment
        decoded_routing = json.loads(routing_metadata)
        assert decoded_routing["exploration"] is True, decoded_routing
        assert decoded_routing["exploration_mode"] == "thompson-hybrid", decoded_routing

        # late-arriving durability update patches an existing outcome (days-later sweep)
        record_run("late", "o/r#99", "implement", "vibe")
        record_outcome("late", adjudicated_verdict="PASS")
        record_outcome("late", durability="reverted")  # patch, not clobber
        with _conn() as c:
            d, a = c.execute(
                "SELECT durability, adjudicated_verdict FROM outcomes WHERE run_id='late'"
            ).fetchone()
        assert d == "reverted" and a == "PASS", (d, a)

        # A/B/C/D cross-eval matrix + human calibration round-trip
        record_evaluation("exp1", "codex", "claude", 0.9, rank=1)
        record_human_calibration("exp1", "codex-best", "matches my goal")
        record_execution_trace(
            "trace-run",
            trace_id="tr-1",
            trace_url="https://smith.langchain.com/r/tr-1",
            provider="openai",
            model="gpt-test",
            operation="verify",
            status="success",
            latency_s=1.2,
            cost_usd=0.04,
            raw_ref="fixture.ndjson:1",
        )
        with _conn() as c:
            trace = c.execute(
                "SELECT trace_id, latency_s, cost_usd FROM execution_traces "
                "WHERE run_id='trace-run'"
            ).fetchone()
        assert trace == ("tr-1", 1.2, 0.04), trace
        record_execution_trace("trace-before-run", model="trace-model", operation="dispatch")
        record_run("trace-before-run", "o/r#trace", "implement", "codex")
        record_run("trace-before-run", "o/r#trace", "implement", "codex", mode="remote")
        record_run("direct-model", "o/r#direct", "implement", "claude", model="claude-test")
        with _conn() as c:
            reconciled = c.execute(
                "SELECT model, mode FROM runs WHERE run_id='trace-before-run'"
            ).fetchone()
            direct_model = c.execute(
                "SELECT model FROM runs WHERE run_id='direct-model'"
            ).fetchone()[0]
        assert reconciled == (None, "remote") and direct_model == "claude-test", (
            reconciled,
            direct_model,
        )

        # test_evaluator_trace_cannot_resolve_worker_model: exact worker identity is causal,
        # not "the latest model seen near this run". Both judge models remain retained.
        record_run("evaluator-contamination", "o/r#judge", "implement", "codex")
        record_execution_trace(
            "evaluator-contamination",
            trace_id="judge-claude",
            provider="anthropic",
            model="claude-sonnet-4-6",
            operation="evaluate_pr_compare",
            status="success",
        )
        record_execution_trace(
            "evaluator-contamination",
            trace_id="judge-gpt",
            provider="openai",
            model="gpt-5.6",
            operation="evaluate_pr_compare",
            status="success",
        )
        assert (
            resolved_worker_model_for_run("evaluator-contamination") is None
        ), "evaluator trace resolved worker model"
        with _conn() as c:
            roles = c.execute(
                "SELECT DISTINCT operation_role FROM execution_attempts "
                "WHERE run_id='evaluator-contamination'"
            ).fetchall()
            legacy_model = c.execute(
                "SELECT model FROM runs WHERE run_id='evaluator-contamination'"
            ).fetchone()[0]
        assert roles == [("evaluator",)] and legacy_model is None, (roles, legacy_model)

        # Conservative migration smoke: preserve runs/traces, classify known evaluator
        # operation, and report the legacy model collision without destructive relabeling.
        record_run(
            "legacy-contaminated",
            "o/r#legacy",
            "implement",
            "codex",
            model="claude-sonnet-4-6",
        )
        with _conn() as c:
            c.execute(
                "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy:judge",
                    "legacy-contaminated",
                    "legacy-judge",
                    None,
                    "anthropic",
                    "claude-sonnet-4-6",
                    "evaluate_pr_compare",
                    "success",
                    1.0,
                    0.1,
                    "legacy",
                    "legacy.ndjson:1",
                    int(time.time()),
                ),
            )
        migration_preview = migrate_legacy_execution_attempts(apply=False)
        assert migration_preview["legacy_trace_rows_examined"] == 1, migration_preview
        migration = migrate_legacy_execution_attempts(apply=True)
        assert migration["classified_non_worker"] == 1, migration
        assert migration["legacy_worker_nonworker_model_collision_runs"] == 1, migration
        assert migration["legacy_rows_preserved"] is True, migration
        assert resolved_worker_model_for_run("legacy-contaminated") is None

        # GROWTH: evidence-gap -> proposal -> register -> influence -> prune
        for _ in range(3):
            record_evidence_gap("expX", "claude", "need test-execution output to judge correctness")
        props = propose_evidence_changes(min_recurrence=3)
        assert any("test-execution" in p["gap"] for p in props), props  # recurring gap proposed
        record_evidence_type("test_run_output", "evaluators repeatedly lacked it")
        bump_evidence_influence("test_run_output")  # it got cited -> survives
        assert active_evidence_types()[0]["name"] == "test_run_output", active_evidence_types()
        normalized = normalize_evidence_type_citations(
            [
                " test_run_output ",
                {"name": "test_run_output"},
                {"type": "unknown_field"},
            ]
        )
        assert normalized == ["test_run_output", "unknown_field"], normalized
        cited = record_evidence_type_citations(
            ["test_run_output", "unknown_field", "test_run_output"]
        )
        assert cited == ["test_run_output"], cited
        record_evidence_type("unused_field", "speculative")
        retired = prune_dead_evidence(min_influence=1)
        assert "unused_field" in retired and "test_run_output" not in retired, retired

        # Schema-migration approval: preview first, recurrence-gated apply, gap status update.
        gap_text = "need stderr capture to judge failures"
        for _ in range(3):
            record_evidence_gap("expY", "judge-b", gap_text)
        preview = approve_evidence_type(
            "stderr_capture", gap_text, min_recurrence=3, window_days=120, apply=False
        )
        assert (
            preview["eligible"] and not preview["applied"] and preview["gaps_marked"] == 0
        ), preview
        assert preview["evidence_type_status"] == "new", preview
        applied = approve_evidence_type(
            "stderr_capture",
            gap_text,
            rationale="fixture approval",
            min_recurrence=3,
            apply=True,
        )
        assert applied["applied"] and applied["gaps_marked"] == 3, applied
        assert applied["evidence_type_status"] == "active", applied
        with _conn() as c:
            open_left = c.execute(
                "SELECT COUNT(*) FROM evidence_gaps WHERE gap=? AND status='open'",
                (gap_text,),
            ).fetchone()[0]
            approved = c.execute(
                "SELECT COUNT(*) FROM evidence_gaps WHERE gap=? AND status='approved'",
                (gap_text,),
            ).fetchone()[0]
            et_status = c.execute(
                "SELECT status FROM evidence_types WHERE name='stderr_capture'"
            ).fetchone()[0]
        assert open_left == 0 and approved == 3 and et_status == "active", (
            open_left,
            approved,
            et_status,
        )
        with _conn() as c:
            c.execute("UPDATE evidence_types SET status='retired' WHERE name='stderr_capture'")
        record_evidence_gap("expZ", "judge-c", gap_text)
        reactivated = approve_evidence_type(
            "stderr_capture",
            gap_text,
            rationale="bring back",
            min_recurrence=1,
            apply=True,
        )
        assert reactivated["evidence_type_status"] == "reactivated", reactivated
        assert reactivated["gaps_marked"] == 1, reactivated
        blocked = approve_evidence_type(
            "blocked_type", "nonexistent gap", min_recurrence=3, apply=True
        )
        assert not blocked["applied"] and not blocked["eligible"], blocked

        # QUALITY-MAGNITUDE learner: continuous scores, free-agent effort reward.
        for i in range(3):
            record_run(f"q_cur_{i}", "o/r", "implement", "cursor", experiment_id=f"E{i}")
            record_run(f"q_cod_{i}", "o/r", "implement", "codex", experiment_id=f"E{i}")
            record_evaluation(f"E{i}", "cursor", f"j{i}", 8.5)  # cursor consistently rated higher
            record_evaluation(f"E{i}", "codex", f"j{i}", 7.0)
        record_cost("q_cur_0", cost_usd=0.0)  # cursor free
        record_cost("q_cod_0", cost_usd=2.0)  # codex metered
        qv = relearn_quality({"implement": {"cursor": 0.5, "codex": 0.5}})
        qw = {x["agent"]: x for x in current_weights("implement", qv)}
        assert (
            qw["cursor"]["posterior"] > qw["codex"]["posterior"]
        ), qw  # magnitude preserved (8.5>7.0)
        assert (
            current_weights("implement", qv)[0]["agent"] == "cursor"
        ), "free+best ranks first, no div0"
        for agent, tokens, latency in (
            ("lean", 10_000, 10.0),
            ("heavy", 20_000_000, 600.0),
        ):
            rid = f"effort_{agent}"
            record_run(rid, "o/r#effort", "effortlearn", agent, experiment_id=f"EFF-{agent}")
            record_evaluation(f"EFF-{agent}", agent, "judge", 8.0)
            record_cost(rid, tokens_in=tokens, latency_s=latency)
        effort_v = relearn_quality({"effortlearn": {"lean": 0.5, "heavy": 0.5}})
        effort_w = {x["agent"]: x for x in current_weights("effortlearn", effort_v)}
        assert effort_w["lean"]["posterior"] == effort_w["heavy"]["posterior"], effort_w
        assert effort_w["lean"]["score"] > effort_w["heavy"]["score"], effort_w
        with _conn() as c:
            heavy_reason = c.execute(
                "SELECT rationale FROM route_weights WHERE version=? AND task_type='effortlearn' "
                "AND agent='heavy'",
                (effort_v,),
            ).fetchone()[0]
        assert (
            "mean_tokens=20000000" in heavy_reason and "mean_latency_s=600.0" in heavy_reason
        ), heavy_reason
        record_run("telemetry_only", "o/r#telemetry", "coldtelemetry", "agent_t")
        record_cost("telemetry_only", tokens_in=50_000_000, latency_s=3600.0, cost_usd=10.0)
        cold_v = relearn_quality({"coldtelemetry": {"agent_t": 0.5}})
        cold_w = current_weights("coldtelemetry", cold_v)[0]
        assert (
            cold_w["n_obs"] == 0 and cold_w["posterior"] == 0.5 and cold_w["score"] == 0.5
        ), cold_w

        # F1 regression for the LIVE learner (2026-07-03 audit): silence must not earn the best
        # effort multiplier. Identical evaluations; only 'qpaid' has effort telemetry — 'qdark'
        # must be imputed to the same penalty, not scored penalty-free.
        for qa in ("qpaid", "qdark"):
            record_run(f"qi_{qa}", "o/r#qi", "qimpute", qa, experiment_id=f"QI-{qa}")
            record_evaluation(f"QI-{qa}", qa, "judge", 8.0)
        record_cost("qi_qpaid", cost_usd=3.0, tokens_in=1_000_000, latency_s=120.0)
        qi_v = relearn_quality({"qimpute": {"qpaid": 0.5, "qdark": 0.5}})
        qi_w = {x["agent"]: x for x in current_weights("qimpute", qi_v)}
        assert abs(qi_w["qpaid"]["score"] - qi_w["qdark"]["score"]) < 1e-9, qi_w
        with _conn() as c:
            qi_srcs = dict(
                c.execute(
                    "SELECT agent, rationale FROM route_weights WHERE version=? AND task_type='qimpute'",
                    (qi_v,),
                ).fetchall()
            )
        assert "effort_src=mmm" in qi_srcs["qpaid"], qi_srcs
        assert "effort_src=ggg" in qi_srcs["qdark"], qi_srcs

        # 16a recency-decay regression: identical outcome COUNTS, opposite ORDER — the agent whose
        # success is FRESH must out-rank the one whose success is STALE; with decay disabled
        # (ORCH_RELEARN_HALF_LIFE_DAYS=0) they must tie (the pre-16a behavior).
        dk_now = int(time.time())
        dk_old = dk_now - 60 * 86400
        for dk_agent, succ_ts, fail_ts in (
            ("fresh_win", dk_now, dk_old),
            ("stale_win", dk_old, dk_now),
        ):
            record_run(f"dk_{dk_agent}_s", "o/r#dk", "decaytest", dk_agent, ts=succ_ts)
            record_outcome(
                f"dk_{dk_agent}_s", adjudicated_verdict="PASS", merged=True, durability="durable"
            )
            record_run(f"dk_{dk_agent}_f", "o/r#dk", "decaytest", dk_agent, ts=fail_ts)
            record_outcome(
                f"dk_{dk_agent}_f", adjudicated_verdict="FAIL", merged=False, durability="abandoned"
            )
        dk_priors = {"decaytest": {"fresh_win": 0.5, "stale_win": 0.5}}
        dk_v = relearn_quality(dk_priors)
        dk_w = {x["agent"]: x for x in current_weights("decaytest", dk_v)}
        assert dk_w["fresh_win"]["posterior"] > dk_w["stale_win"]["posterior"], dk_w
        os.environ["ORCH_RELEARN_HALF_LIFE_DAYS"] = "0"
        try:
            dk_v0 = relearn_quality(dk_priors)
        finally:
            del os.environ["ORCH_RELEARN_HALF_LIFE_DAYS"]
        dk_w0 = {x["agent"]: x for x in current_weights("decaytest", dk_v0)}
        assert abs(dk_w0["fresh_win"]["posterior"] - dk_w0["stale_win"]["posterior"]) < 1e-9, dk_w0

        # item 9 two-tier enum: an abandoned outcome classified transient_infra must NOT lower
        # the posterior (the agent was killed by the environment, not out-coded); an identical
        # unclassified abandonment must. Merged rows refuse classification.
        for ti_agent in ("sturdy", "unlucky"):
            for i in range(2):
                rid = f"ti_{ti_agent}_ok{i}"
                record_run(rid, f"o/ti#{ti_agent}{i}", "infratest", ti_agent)
                record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
            rid_f = f"ti_{ti_agent}_fail"
            record_run(rid_f, f"o/ti#{ti_agent}f", "infratest", ti_agent)
            record_outcome(rid_f, adjudicated_verdict="FAIL", merged=False, durability="abandoned")
        assert mark_transient_infra("ti_unlucky_fail", reason="marker rc=137") is True
        assert mark_transient_infra("ti_unlucky_fail") is False  # idempotent
        assert mark_transient_infra("ti_sturdy_ok0") is False  # merged rows refuse

        # AN EDGE WITH NO TARGET ENVELOPE MUST RAISE UNLESS THE CALLER DECLARES IT. 296 orphan edges
        # accumulated silently because this writer inserted them while its sibling
        # `record_capability_consumption` raised on the identical condition -- and 202 arrived in ONE
        # backfill that reported plain success. Both directions asserted: a leaking guard would stop
        # every role proposal recording, which is the more expensive failure.
        with _conn() as _c:
            # A run written WITHOUT a completion event -- `record_run` emits one, so it cannot be
            # used here; the whole point is a target that has no envelope to point at.
            _c.execute(
                "INSERT OR IGNORE INTO runs (run_id,ts,target,task_type,agent) "
                "VALUES ('orphan-probe',0,'o/r#1','implement','codex')"
            )
            try:
                _record_influence_edge_in_conn(
                    _c,
                    target_run_id="orphan-probe",
                    influence_type="role",
                    influence_id="role:probe",
                    accepted=True,
                )
                raise AssertionError("an unlinked edge must not be insertable by default")
            except ValueError as exc:
                assert "no completion event" in str(exc), exc
            _ok = _record_influence_edge_in_conn(
                _c,
                target_run_id="orphan-probe",
                influence_type="role",
                influence_id="role:probe",
                accepted=True,
                allow_unlinked=True,
            )
            assert _ok["edge_id"].startswith("edge:"), _ok
            # Clean up: a later assertion in this selftest checks the fixture holds ZERO orphan
            # edges, and that check is worth more than this one's leftovers.
            _c.execute("DELETE FROM influence_edges WHERE edge_id=?", (_ok["edge_id"],))
            _c.execute("DELETE FROM runs WHERE run_id='orphan-probe'")
        ti_v = relearn_quality({"infratest": {"sturdy": 0.5, "unlucky": 0.5}})
        ti_w = {x["agent"]: x for x in current_weights("infratest", ti_v)}
        assert ti_w["unlucky"]["posterior"] > ti_w["sturdy"]["posterior"], ti_w
        assert ti_w["unlucky"]["n_obs"] == 2 and ti_w["sturdy"]["n_obs"] == 3, ti_w
        ti_legacy = relearn({"infratest": {"sturdy": 0.5, "unlucky": 0.5}})
        ti_lw = {x["agent"]: x for x in current_weights("infratest", ti_legacy)}
        assert ti_lw["unlucky"]["posterior"] > ti_lw["sturdy"]["posterior"], ti_lw

        # 16g Bradley-Terry: consistent duel outcomes rank A>B>C; sparse data refuses (ready=False)
        # and the task_type filter scopes correctly.
        for i in range(3):
            for impl in ("bt_a", "bt_b", "bt_c"):
                record_run(f"bt_{impl}_{i}", "o/bt", "btduel", impl, experiment_id=f"BT{i}")
            for judge in ("j1", "j2"):
                record_evaluation(f"BT{i}", "bt_a", judge, 9.0)
                record_evaluation(f"BT{i}", "bt_b", judge, 6.0)
                record_evaluation(f"BT{i}", "bt_c", judge, 3.0)
        bt = bt_strengths(task_type="btduel")
        assert bt["ready"] and bt["comparisons"] == 18, bt
        s = bt["strengths"]
        assert s["bt_a"] > s["bt_b"] > s["bt_c"], s
        assert bt_strengths(task_type="btduel", min_comparisons=99)["ready"] is False
        assert bt_strengths(task_type="no-such-type")["ready"] is False

        # 16f resume tokens: roundtrip + paste-ready command; unknown kinds keep the raw token.
        record_resume_token("rt-1", "claude", "claude_session", "abc-123", cwd="/tmp/w")
        rh = resume_hint("rt-1")
        assert rh and rh["command"] == "claude --resume abc-123" and rh["cwd"] == "/tmp/w", rh
        record_resume_token("rt-2", "vibe", "vibe_unknown", "tok")
        assert resume_hint("rt-2")["command"] is None
        assert resume_hint("rt-none") is None

        # 16h owner questions: record→dedupe-while-open→answer→decision; expiry ratifies default.
        q1 = record_owner_question(
            "Keep CSV export default ON?", "yes, keep ON", repo="o/r", expires_days=7
        )
        assert q1["status"] == "open" and not q1["deduped"], q1
        q1b = record_owner_question("Keep CSV export default ON?", "different default", repo="o/r")
        assert q1b["deduped"] and q1b["question_id"] == q1["question_id"], q1b
        assert len(open_owner_questions()) == 1
        assert answer_owner_question(q1["question_id"], "no, make it opt-in") is True
        assert answer_owner_question(q1["question_id"], "again") is False  # idempotent
        dec = owner_decisions_for(repo="o/r")
        assert (
            dec and dec[0]["decision"] == "no, make it opt-in" and dec[0]["source"] == "owner"
        ), dec
        record_owner_question(
            "Drop legacy endpoint?", "keep it for now", repo="o/r", expires_days=-1
        )
        assert expire_owner_questions() == 1
        dec2 = owner_decisions_for(repo="o/r", limit=5)
        assert any(
            d["decision"] == "keep it for now" and d["source"] == "default_ratified" for d in dec2
        ), dec2
        assert not open_owner_questions(), "expiry must leave no open backlog"

        # Quality learner folds in production-only outcomes without requiring cross-evaluations.
        for i in range(3):
            record_run(
                f"eval_a_{i}",
                "o/r#eval",
                "prodlearn",
                "agent_a",
                experiment_id=f"EA{i}",
            )
            record_run(
                f"eval_b_{i}",
                "o/r#eval",
                "prodlearn",
                "agent_b",
                experiment_id=f"EB{i}",
            )
            record_evaluation(f"EA{i}", "agent_a", f"judge-a-{i}", 9.0)
            record_evaluation(f"EB{i}", "agent_b", f"judge-b-{i}", 5.0)
        eval_only = relearn_quality({"prodlearn": {"agent_a": 0.6, "agent_b": 0.5}})
        assert current_weights("prodlearn", eval_only)[0]["agent"] == "agent_a"
        for i in range(10):
            rid_a = f"prod_a_{i}"
            rid_b = f"prod_b_{i}"
            record_run(rid_a, "o/r#prod", "prodlearn", "agent_a")
            record_run(rid_b, "o/r#prod", "prodlearn", "agent_b")
            record_outcome(rid_a, adjudicated_verdict="PASS", merged=True, durability="reverted")
            record_outcome(rid_b, adjudicated_verdict="PASS", merged=True, durability="durable")
        blended = relearn_quality({"prodlearn": {"agent_a": 0.6, "agent_b": 0.5}})
        blended_weights = {x["agent"]: x for x in current_weights("prodlearn", blended)}
        assert blended_weights["agent_b"]["n_obs"] == 13, blended_weights
        assert (
            blended_weights["agent_b"]["posterior"] > blended_weights["agent_a"]["posterior"]
        ), blended_weights
        assert current_weights("prodlearn", blended)[0]["agent"] == "agent_b", blended_weights

        # Assigned keepalive observations are retained but excluded from causal route_weights learning.
        assign_priors = {"assignlearn": {"agentX": 0.4, "agentY": 0.4}}
        for i in range(4):
            rid = f"assign_exp_x_{i}"
            record_run(rid, "o/r#assign", "assignlearn", "agentX", mode="remote")
            record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
        assign_v0 = relearn_quality(assign_priors)
        assign_w0 = {x["agent"]: x for x in current_weights("assignlearn", assign_v0)}
        assert (
            assign_w0["agentX"]["posterior"] > 0.4 and assign_w0["agentX"]["n_obs"] == 4
        ), assign_w0
        assert (
            assign_w0["agentY"]["posterior"] == 0.4 and assign_w0["agentY"]["n_obs"] == 0
        ), assign_w0
        for i in range(12):
            rid = f"keepalive:o/r#assign-{i}:agentY"
            record_run(
                rid,
                "o/r#assign",
                "assignlearn",
                "agentY",
                mode="remote",
                source="keepalive",
                assignment="assigned",
            )
            record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
            record_cost(rid, cost_usd=99.0)
        assign_v1 = relearn_quality(assign_priors)
        assign_w1 = {x["agent"]: x for x in current_weights("assignlearn", assign_v1)}
        assert assign_w1["agentX"]["posterior"] == assign_w0["agentX"]["posterior"], assign_w1
        assert (
            assign_w1["agentY"]["posterior"] == 0.4 and assign_w1["agentY"]["n_obs"] == 0
        ), assign_w1
        with _conn() as c:
            assigned_count = c.execute(
                "SELECT COUNT(*) FROM runs WHERE task_type='assignlearn' AND assignment='assigned'"
            ).fetchone()[0]
        assert assigned_count == 12, assigned_count

        for i in range(5):
            rid = f"keepalive:o/r#none-{i}:none"
            record_run(
                rid,
                "o/r#none",
                "assignlearn",
                "none",
                mode="remote",
                source="keepalive",
                assignment="none",
                work_type="renovate",
            )
            record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
            record_cost(rid, cost_usd=77.0)
        assign_v2 = relearn_quality(assign_priors)
        assign_w2 = {x["agent"]: x for x in current_weights("assignlearn", assign_v2)}
        assert assign_w2["agentX"]["posterior"] == assign_w0["agentX"]["posterior"], assign_w2
        assert (
            assign_w2["agentY"]["posterior"] == 0.4 and assign_w2["agentY"]["n_obs"] == 0
        ), assign_w2
        assert "none" not in assign_w2, assign_w2

        # Role invocations get their own task_type surface, linked to the downstream run outcome.
        record_role_run(
            "role-good",
            "redirect",
            "o/r#role",
            "cursor",
            backend_run_id="offload:cursor:1",
            action="redirect",
            decision_source="redirect_agent",
            proposal={"action": "redirect"},
        )
        record_run("downstream-good", "o/r#role", "implement", "codex")
        record_outcome(
            "downstream-good",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        linked = join_role_to_outcome("role-good", "downstream-good", notes="accepted redirect")
        assert (
            linked["linked"] and linked["synced"] and linked["task_type"] == "role:redirect"
        ), linked
        record_role_run("role-bad", "redirect", "o/r#role", "codex", action="redirect")
        record_run("downstream-bad", "o/r#role", "implement", "codex")
        record_outcome(
            "downstream-bad",
            adjudicated_verdict="PASS",
            merged=True,
            durability="reverted",
        )
        join_role_to_outcome("role-bad", "downstream-bad")
        with _conn() as c:
            role_row = c.execute(
                "SELECT task_type, role_name, decomposition FROM runs WHERE run_id='role-good'"
            ).fetchone()
            influenced = c.execute(
                "SELECT influenced_by_run_id FROM outcomes WHERE run_id='downstream-good'"
            ).fetchone()[0]
            role_outcome = c.execute(
                "SELECT durability, notes FROM outcomes WHERE run_id='role-good'"
            ).fetchone()
        assert role_row[0] == "role:redirect" and role_row[1] == "redirect", role_row
        assert "offload:cursor:1" in (role_row[2] or ""), role_row
        assert influenced == "role-good", influenced
        assert role_outcome[0] == "durable" and "accepted redirect" in role_outcome[1], role_outcome
        role_v = relearn_quality({"role:redirect": {"cursor": 0.5, "codex": 0.5}})
        role_weights = {x["agent"]: x for x in current_weights("role:redirect", role_v)}
        assert (
            role_weights["cursor"]["posterior"] > role_weights["codex"]["posterior"]
        ), role_weights
        record_role_run("role-rejected", "redirect", "o/r#role", "vibe", action="redirect")
        record_run("downstream-rejected", "o/r#role", "implement", "vibe")
        record_outcome(
            "downstream-rejected",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        rejected = join_role_to_outcome("role-rejected", "downstream-rejected", accepted=False)
        assert rejected["linked"] and not rejected["synced"], rejected
        with _conn() as c:
            no_role_outcome = c.execute(
                "SELECT 1 FROM outcomes WHERE run_id='role-rejected'"
            ).fetchone()
        assert no_role_outcome is None, no_role_outcome

        # Multi-reviewer evaluated runs count once, and their outcome fallback is not double-counted.
        record_run("panel-run", "o/r#panel", "panellearn", "panel_agent", experiment_id="PANEL")
        record_evaluation("PANEL", "panel_agent", "judge-1", 7.0)
        record_evaluation("PANEL", "panel_agent", "judge-2", 8.0)
        record_evaluation("PANEL", "panel_agent", "judge-3", 9.0)
        record_outcome("panel-run", adjudicated_verdict="PASS", merged=True, durability="reverted")
        panel_version = relearn_quality({"panellearn": {"panel_agent": 0.5}})
        panel_weight = current_weights("panellearn", panel_version)[0]
        assert panel_weight["n_obs"] == 1 and panel_weight["posterior"] > 0.5, panel_weight

        # Superseded-model evidence is still used, but carries lower effective weight.
        base_ts = int(time.time()) - 100
        for i in range(4):
            old_rid = f"drift_old_{i}"
            new_rid = f"drift_new_{i}"
            null_rid = f"drift_null_{i}"
            record_run(
                old_rid,
                "o/r#drift",
                "driftlearn",
                "old_agent",
                ts=base_ts + i,
                model="old-model",
            )
            record_execution_attempt(
                old_rid,
                attempt_id=f"worker:{old_rid}",
                operation_role="worker",
                profile_id="codex-old-profile",
                resolved_provider="openai",
                resolved_model="old-model",
                status="success",
            )
            record_run(
                new_rid,
                "o/r#drift",
                "driftlearn",
                "new_agent",
                ts=base_ts + i,
                model="new-model",
            )
            record_execution_attempt(
                new_rid,
                attempt_id=f"worker:{new_rid}",
                operation_role="worker",
                profile_id="codex-new-profile",
                resolved_provider="openai",
                resolved_model="new-model",
                status="success",
            )
            record_run(null_rid, "o/r#drift", "driftlearn", "null_agent", ts=base_ts + i)
            record_outcome(old_rid, adjudicated_verdict="PASS", merged=True, durability="durable")
            record_outcome(new_rid, adjudicated_verdict="PASS", merged=True, durability="durable")
            record_outcome(null_rid, adjudicated_verdict="PASS", merged=True, durability="durable")
        record_run(
            "drift_old_current",
            "o/r#drift",
            "driftlearn",
            "old_agent",
            ts=base_ts + 10,
            model="new-model",
        )
        record_execution_attempt(
            "drift_old_current",
            attempt_id="worker:drift-old-current",
            operation_role="worker",
            profile_id="codex-new-profile",
            resolved_provider="openai",
            resolved_model="new-model",
            status="success",
        )
        record_run(
            "drift_null_current",
            "o/r#drift",
            "driftlearn",
            "null_agent",
            ts=base_ts + 10,
            model="new-model",
        )
        drift_v = relearn_quality(
            {"driftlearn": {"old_agent": 0.5, "new_agent": 0.5, "null_agent": 0.5}}
        )
        drift_w = {x["agent"]: x for x in current_weights("driftlearn", drift_v)}
        assert drift_w["old_agent"]["n_obs"] == 4, drift_w
        assert drift_w["new_agent"]["posterior"] > drift_w["old_agent"]["posterior"], drift_w
        assert (
            abs(drift_w["new_agent"]["posterior"] - drift_w["null_agent"]["posterior"]) < 1e-9
        ), drift_w
        with _conn() as c:
            rationale = c.execute(
                "SELECT rationale FROM route_weights WHERE version=? AND task_type='driftlearn' "
                "AND agent='old_agent'",
                (drift_v,),
            ).fetchone()[0]
        assert "eff_n=2.0" in rationale and "superseded_model_runs=4" in rationale, rationale

        # Issue 7: normal local, remote Keepalive, experiment, role, and skill
        # flows all traverse decision -> attempt -> completion -> outcome -> durability.
        lineage_runs = (
            ("lineage-local", "local", None, "orchestrator_local", "experimental"),
            ("lineage-remote", "remote", None, "keepalive", "assigned"),
            ("lineage-experiment", "full", "lineage-exp", "orchestrator_local", "experimental"),
        )
        for lineage_index, (
            lineage_run,
            lineage_mode,
            lineage_exp,
            lineage_source,
            lineage_assignment,
        ) in enumerate(lineage_runs, start=1):
            record_run(
                lineage_run,
                "owner/repo#lineage",
                "implement",
                "codex",
                mode=lineage_mode,
                experiment_id=lineage_exp,
                source=lineage_source,
                assignment=lineage_assignment,
                pr_number=700 + lineage_index,
            )
            record_execution_attempt(
                lineage_run,
                attempt_id=f"attempt:{lineage_run}",
                operation_role="worker",
                profile_id="codex-sol",
                resolved_provider="openai",
                resolved_model="gpt-5.6-codex",
                status="success",
            )
            record_outcome(
                lineage_run,
                verifier_verdict="PASS",
                adjudicated_verdict="PASS",
                merged=True,
                durability="durable",
            )
        record_role_run(
            "lineage-role", "redirect", "owner/repo#lineage", "cursor", action="redirect"
        )
        skill_events = [
            record_skill_invocation(skill_id, "fixture-version", result="succeeded")
            for skill_id in ("repo-audit", "workflow-steward", "git-remote-sync")
        ]
        record_run(
            "lineage-influenced",
            "owner/repo#lineage",
            "implement",
            "codex",
            mode="local",
            influenced_by_role_run_ids=["lineage-role"],
            influenced_by_skill_event_ids=[skill_events[0]["event_id"]],
            influenced_by_workflow_ids=["keepalive-workloop"],
            capability_ids=["feedback-store"],
            acceptance_gate_ids=["issue-7-ac"],
        )
        record_execution_attempt(
            "lineage-influenced",
            attempt_id="attempt:lineage-influenced",
            operation_role="worker",
            profile_id="codex-sol",
            resolved_provider="openai",
            resolved_model="gpt-5.6-codex",
            status="success",
        )
        record_outcome(
            "lineage-influenced",
            verifier_verdict="PASS",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        redaction = record_completion_event(
            "lineage-redaction",
            event_type="completion",
            phase="execution",
            producer="selftest",
            payload={"raw_prompt": "never retain", "token": "ghp_secretfixture123456"},
        )
        assert redaction["validation_status"] == "redacted", redaction
        with _conn() as c:
            required_phases = {
                "trigger",
                "decision",
                "execution",
                "artifact",
                "verification",
                "outcome",
                "durability",
            }
            for lineage_run, *_ in lineage_runs:
                phases = {
                    row[0]
                    for row in c.execute(
                        "SELECT phase FROM completion_events WHERE run_id=?", (lineage_run,)
                    ).fetchall()
                }
                assert required_phases.issubset(phases), (lineage_run, phases)
            role_outcome = c.execute(
                "SELECT adjudicated_verdict,durability FROM outcomes WHERE run_id='lineage-role'"
            ).fetchone()
            skill_edge = c.execute(
                "SELECT outcome_verdict,durability FROM influence_edges "
                "WHERE target_run_id='lineage-influenced' AND influence_type='skill'"
            ).fetchone()
        assert role_outcome == ("PASS", "durable"), role_outcome
        assert skill_edge == ("PASS", "durable"), skill_edge

        # ---- an offload-backed role run carries the transport capability, and one without
        # ---- a backend run does not: the tag follows the recorded link, never the role name.
        # The DB here is a fresh tmp file, but the tags come out of the LEDGER: this writer
        # refuses a capability edge with no version lineage, on purpose, so a capability is never
        # credited with a version it never had. `offload`'s row exists only on an instance that
        # has run it, so the SECTION is gated and named — everything else here still runs.
        if env_prereq.runnable(
            gaps,
            env_prereq.ledger_rows_absent("role-redirect", "offload"),
            env_prereq.ledger_version_lineage_absent("role-redirect", "offload"),
        ):
            record_role_run(
                "role:redirect:offloaded",
                "redirect",
                "owner/repo#tr",
                "cursor",
                backend_run_id="offload:abc123",
            )
            record_role_run("role:redirect:replayed", "redirect", "owner/repo#tr", "cursor")
            with _conn() as c:
                offloaded = {
                    r[0]
                    for r in c.execute(
                        "SELECT capability_id FROM influence_edges WHERE target_run_id=? "
                        "AND influence_type='capability'",
                        ("role:redirect:offloaded",),
                    ).fetchall()
                }
                replayed = {
                    r[0]
                    for r in c.execute(
                        "SELECT capability_id FROM influence_edges WHERE target_run_id=? "
                        "AND influence_type='capability'",
                        ("role:redirect:replayed",),
                    ).fetchall()
                }
            assert offloaded == {"role-redirect", "offload"}, offloaded
            assert replayed == {"role-redirect"}, replayed

        # ---- a REJECTED edge must never inherit the acting run's success ---------------
        # `_propagate_outcome_lineage_in_conn` filters on accepted=1. That filter is the whole
        # un-gameable-label guard on this path: without it, a role whose proposal was thrown out
        # for being invalid would be credited with the delivery someone else produced, and route
        # weights would learn from advice that was never followed. Recording the disagreement is
        # correct; copying the PASS is not. (2026-08-21)
        record_role_run(
            "lineage-role-rejected",
            "prompt",
            "owner/repo#rejected",
            "cursor",
            action="implement",
        )
        record_run(
            "lineage-rejected-work", "owner/repo#rejected", "implement", "codex", mode="local"
        )
        record_influence_edge(
            target_run_id="lineage-rejected-work",
            influence_type="role",
            influence_id="lineage-role-rejected",
            source_run_id="lineage-role-rejected",
            accepted=False,
            metadata={"status": "rejected", "disagreement": True},
        )
        record_outcome(
            "lineage-rejected-work",
            verifier_verdict="PASS",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        with _conn() as c:
            rejected_edge = c.execute(
                "SELECT accepted,counterfactual,outcome_verdict,durability FROM influence_edges "
                "WHERE target_run_id='lineage-rejected-work'"
            ).fetchone()
            copied = c.execute(
                "SELECT 1 FROM outcomes WHERE run_id='lineage-role-rejected'"
            ).fetchone()
        assert rejected_edge == (0, 1, None, None), rejected_edge
        assert copied is None, "a rejected role must not inherit the acting run's PASS"
        lineage_health = completion_event_health()
        assert lineage_health["accepted_influence_linked"] >= 3, lineage_health
        assert lineage_health["orphan_edges"] == 0, lineage_health

        # Same LEDGER prerequisite as above, and the same reason it is a SECTION rather than the
        # whole selftest: every edge here is a `role-triage` capability edge, and the writer needs
        # that row's version lineage to create one at all.
        if env_prereq.runnable(
            gaps,
            env_prereq.ledger_rows_absent("role-triage"),
            env_prereq.ledger_version_lineage_absent("role-triage"),
        ):
            # ---- capability attribution must reach a run that CAN resolve -------------------
            # A role run is advisory and never gets an `outcomes` row, so its own capability edge is
            # permanently unresolvable. The run that ACTS on the proposal inherits the attribution,
            # and outcome propagation then resolves it on the already-existing path.
            record_role_run("role:triage:x:1", "triage", "o/r#7", "gemini")
            capq = (
                "SELECT capability_id,target_run_id,durability FROM influence_edges "
                "WHERE influence_type='capability'"
            )
            with _conn() as cc:
                pre = cc.execute(capq + " AND target_run_id='role:triage:x:1'").fetchall()
            assert pre and pre[0][2] is None, f"advisory role run should have no outcome: {pre}"

            record_run(
                "cap-inherit-work",
                target="o/r#7",
                task_type="implement",
                agent="codex",
                influenced_by_role_run_ids=["role:triage:x:1"],
            )
            with _conn() as cc:
                mid = cc.execute(capq + " AND target_run_id='cap-inherit-work'").fetchall()
            assert mid and mid[0][0] == "role-triage", f"attribution not inherited: {mid}"

            record_outcome(
                "cap-inherit-work", verifier_verdict="PASS", merged=1, durability="durable"
            )
            with _conn() as cc:
                post = cc.execute(capq + " AND target_run_id='cap-inherit-work'").fetchall()
            assert post and post[0][2] == "durable", f"outcome did not reach the edge: {post}"

            # A run declaring the capability ITSELF must not also get an inherited duplicate.
            record_role_run("role:triage:x:2", "triage", "o/r#8", "gemini")
            record_run(
                "cap-nodupe",
                target="o/r#8",
                task_type="implement",
                agent="codex",
                capability_ids=["role-triage"],
                influenced_by_role_run_ids=["role:triage:x:2"],
            )
            with _conn() as cc:
                dupes = cc.execute(capq + " AND target_run_id='cap-nodupe'").fetchall()
            assert len(dupes) == 1, f"double-counted the same capability: {dupes}"

            # DELIBERATE BREAK -> REVERT: stub the lookup and nothing is inherited, so the edge stays
            # unresolvable — proving the inheritance is what makes measurement possible.
            saved_lookup = globals()["_capability_attribution_of"]
            try:
                globals()["_capability_attribution_of"] = lambda *a, **k: []
                record_role_run("role:triage:x:3", "triage", "o/r#9", "gemini")
                record_run(
                    "cap-broken",
                    target="o/r#9",
                    task_type="implement",
                    agent="codex",
                    influenced_by_role_run_ids=["role:triage:x:3"],
                )
                with _conn() as cc:
                    broken = cc.execute(capq + " AND target_run_id='cap-broken'").fetchall()
                assert not broken, "break did not change behaviour — test is vacuous"
            finally:
                globals()["_capability_attribution_of"] = saved_lookup
            record_role_run("role:triage:x:4", "triage", "o/r#10", "gemini")
            record_run(
                "cap-reverted",
                target="o/r#10",
                task_type="implement",
                agent="codex",
                influenced_by_role_run_ids=["role:triage:x:4"],
            )
            with _conn() as cc:
                rev = cc.execute(capq + " AND target_run_id='cap-reverted'").fetchall()
            assert rev, "revert did not restore inheritance"

        # JSON snapshot (the reviewable project copy of the dataset)
        snap = snapshot_json(Path(tmp) / "snap.json")
        assert (
            Path(snap["path"]).exists()
            and snap["rows"]["runs"] > 0
            and snap["rows"]["execution_traces"] == 5
            and snap["rows"]["execution_attempts"] >= 5
            and "evidence_types" in snap["rows"]
        ), snap
        # --- the verification event must NAME the gate, and only when one ran ---
        # An episode whose verification does not say WHAT passed is refused as
        # `unnamed_verification`, which is why no otherwise-complete episode was ever eligible.
        record_run("gate-named", "o/r#1", "implement", "codex", pr_number=41)
        record_outcome(
            "gate-named",
            verifier_verdict="PASS",
            adjudicated_verdict="PASS",
            merged=1,
            ci_status="success",
            durability="durable",
        )
        named = (
            _conn()
            .execute(
                "SELECT payload_json FROM completion_events WHERE run_id='gate-named' "
                "AND phase='verification'"
            )
            .fetchone()
        )
        assert named, "verification event missing"
        assert json.loads(named[0]).get("acceptance_gate_ids") == ["ci"], named[0]
        # And it must stay UNNAMED when no gate ran -- naming it unconditionally would restore
        # exactly the ungameable-label failure this check exists to prevent.
        record_run("gate-unnamed", "o/r#2", "implement", "codex", pr_number=42)
        record_outcome(
            "gate-unnamed",
            verifier_verdict="PASS",
            adjudicated_verdict="PASS",
            merged=1,
            durability="durable",
        )
        unnamed = (
            _conn()
            .execute(
                "SELECT payload_json FROM completion_events WHERE run_id='gate-unnamed' "
                "AND phase='verification'"
            )
            .fetchone()
        )
        assert unnamed and "acceptance_gate_ids" not in json.loads(unnamed[0]), unnamed[0]
        # Both events must survive payload validation -- a nested key would have been rejected.
        assert all(
            row[0] == "accepted"
            for row in _conn().execute(
                "SELECT validation_status FROM completion_events WHERE run_id IN "
                "('gate-named','gate-unnamed') AND phase='verification'"
            )
        ), "verification rejected"

        env_prereq.report_gaps("feedback.py", gaps)
        print(
            "feedback.py selftest: OK (prior→posterior learning, durability/verifier-as-success, late updates, "
            "versioned weights, eval matrix, human calibration, evidence-gap growth+prune+approval, "
            "trace retention, test_evaluator_trace_cannot_resolve_worker_model, conservative legacy migration, "
            "quality-magnitude/outcome learner + effort reward, safe completion lineage + "
            "rejected-edge non-inheritance, named verification gate, json snapshot)"
            + (f" — {len(set(gaps))} section(s) skipped, see above" if gaps else "")
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "migrate-execution-attempts":
        report = migrate_legacy_execution_attempts(apply="--apply" in argv[1:])
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if argv and argv[0] == "completion-events":
        limit = 1000
        durable_only = "--durable-only" in argv[1:]
        if "--limit" in argv[1:]:
            try:
                limit = max(1, min(10000, int(argv[argv.index("--limit") + 1])))
            except (ValueError, IndexError):
                print("invalid --limit", file=sys.stderr)
                return 2
        rows = completion_event_episodes(limit=limit, accepted_only=True, durable_only=durable_only)
        if "--json" in argv[1:]:
            print(json.dumps(rows, sort_keys=True))
        else:
            for row in rows:
                print(json.dumps(row, sort_keys=True))
        return 0
    # item 16h/16f operator surface: list/answer owner questions, look up resume hints.
    if argv and argv[0] == "questions":
        expire_owner_questions()
        rows = open_owner_questions()
        if not rows:
            print("no open owner questions (defaults applying; nothing waiting on you)")
        for row in rows:
            days_left = max(0, (row["expires_ts"] - int(time.time())) // 86400)
            print(
                f"  [{row['question_id']}] ({row['repo'] or row['target'] or 'fleet'}) "
                f"{row['question']}"
            )
            print(f"      default (applies in {days_left}d if unanswered): {row['default_action']}")
            print(f"      answer: python3 {__file__} answer {row['question_id']} \"<your answer>\"")
        return 0
    if argv and argv[0] == "answer" and len(argv) >= 3:
        ok = answer_owner_question(argv[1], " ".join(argv[2:]))
        print("answered" if ok else "not answered (unknown id or not open)")
        return 0 if ok else 1
    if argv and argv[0] == "resume-hint" and len(argv) >= 2:
        hint = resume_hint(argv[1])
        print(json.dumps(hint, indent=2) if hint else f"no resume token for {argv[1]}")
        return 0 if hint else 1
    init = _conn()
    init.close()
    print(f"feedback store: {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
