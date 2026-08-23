#!/usr/bin/env python3
"""Immutable execution profiles and additive hierarchical routing evidence.

Profiles separate model/reasoning/permission/transport choices from the legacy
``runs.mode`` compatibility field.  The production router may emit profiles,
but v2 learned weights remain shadow-only unless explicitly enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
from typing import Any

PROFILE_SCHEMA_VERSION = 1
PROFILE_POLICY_VERSION = "execution-profile-policy-v1"
MIN_RESOLVED_COVERAGE = float(os.environ.get("ORCH_PROFILE_MIN_RESOLVED_COVERAGE", "0.8"))
MIN_EXACT_OBSERVATIONS = int(os.environ.get("ORCH_PROFILE_MIN_EXACT_OBSERVATIONS", "3"))
PRIOR_STRENGTH = 8.0
MAX_SUCCESSOR_TRANSFER = 0.10

# Provider pool policy is authoritative here and consumed by capacity.py and
# research_scheduler.py.  Multiple profiles can map to the same real balance.
# One pool per real account. Every field below is copied from `capacity.AGENTS` -- the authoritative
# per-agent account/window model -- rather than invented here. execution_profiles CANNOT import
# capacity (capacity imports this module), so `test_capacity_pools_match_capacity_agents` asserts the
# two agree and fails if either drifts. A pool that misdescribes an account would mis-report capacity
# to the dispatcher, so this is a copy with a guard, never a guess.
CAPACITY_POOLS = {
    "codex-subscription": {
        "provider": "openai",
        "account": "chatgpt-pro",
        "window": "5h+weekly",
        "tier": "metered",
        "agent": "codex",
    },
    "claude-subscription": {
        "provider": "anthropic",
        "account": "claude-team-max",
        "window": "5h+weekly",
        "tier": "metered",
        "agent": "claude",
    },
    "cursor-subscription": {
        "provider": "cursor",
        "account": "cursor-pro-plus",
        "window": "monthly",
        "tier": "metered",
        "agent": "cursor",
    },
    "gemini-prepaid": {
        "provider": "google",
        "account": "antigravity-ai-pro",
        "window": "5h+weekly",
        "tier": "metered",
        "agent": "gemini",
    },
    "vibe-subscription": {
        "provider": "mistral",
        "account": "mistral-vibe-sub",
        "window": "subscription",
        "tier": "flat",
        "agent": "vibe",
    },
    "aider-paygo": {
        "provider": "mistral",
        "account": "mistral-codestral",
        "window": "daily",
        "tier": "paygo",
        "agent": "aider",
    },
}


def _profile(
    profile_id: str,
    model: str,
    reasoning: str,
    *,
    prior_offset: float = 0.0,
    agent: str = "codex",
    provider: str = "openai",
    pool: str = "codex-subscription",
    adapter_version: str = "codex-cli-profile-v1",
) -> dict[str, Any]:
    """One registered execution profile. Defaults stay codex so existing entries are unchanged.

    `requested_model` is what we ASK for; it is never treated as resolved identity. Where an agent's
    adapter can only report a routing tag (`agy:`, `cursor:`, `vibe:`), the attempt completes
    UNRESOLVED and that agent's work stays unminable -- correctly. Registering the profile anyway is
    deliberate: an unresolved worker attempt is a visible, attributable gap, whereas no profile at
    all is silence, and silence is what hid a dead miner for 43 days. `mining_coverage` names which
    agents are in which state on every periodic report.
    """
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "agent": agent,
        "provider": provider,
        "requested_model": model,
        "reasoning_effort": reasoning,
        "permission_mode": "workspace-write",
        "transport_support": ["local", "offload", "experiment"],
        "legacy_adapter_mode": "full",
        "adapter_version": adapter_version,
        "prompt_version": "orchestrator-prompt-v1",
        "capacity_pool_ids": [pool],
        "lifecycle_status": "active",
        "successor_profile_id": None,
        "prior_offset": prior_offset,
    }


PROFILE_REGISTRY: dict[str, dict[str, Any]] = {
    p["profile_id"]: p
    for p in (
        _profile("codex-5.6-sol-high", "gpt-5.6-sol", "high", prior_offset=0.05),
        _profile("codex-5.6-terra-high", "gpt-5.6-terra", "high"),
        _profile("codex-5.6-luna-high", "gpt-5.6-luna", "high", prior_offset=-0.02),
        # One profile per agent so every seat can record a worker attempt. Models are the identities
        # `adapters.model_identity(agent, "full")` reports; `test_registry_models_match_adapters`
        # fails if they drift, because a registry that disagrees with the adapter would request a
        # model the seat never runs.
        _profile(
            "claude-sonnet-5-high",
            "claude-sonnet-5",
            "high",
            agent="claude",
            provider="anthropic",
            pool="claude-subscription",
            adapter_version="claude-cli-profile-v1",
        ),
        _profile(
            "aider-codestral-high",
            "mistral/codestral-latest",
            "high",
            agent="aider",
            provider="mistral",
            pool="aider-paygo",
            adapter_version="aider-cli-profile-v1",
        ),
        # Every seat's model is NAMED, each from the seat's own authority: codex/claude from the
        # tier research, vibe from its CLI config, aider from its floating alias, gemini from agy's
        # advertised-models probe, cursor from its known default. None is a routing tag, so no seat
        # is unidentifiable -- what they all still need is worker TRACING to confirm what served.
        # GEMINI HAS TWO LINES, AND THEY ARE NOT A VERSION SEQUENCE. Flash (3.7/3.6/3.5, each with
        # high/medium/low effort) is the fast mid-tier; Pro (3.1) is the higher-end reasoning tier.
        # `3.7 > 3.1` is newer FLASH, not better than PRO -- reading those numbers as one ladder is
        # the trap, and both lines are legitimately used depending on the task.
        #
        # Registering only the Pro profile was a real regression: `_select_offload_profile` then
        # pinned every gemini offload to Pro, silently overriding `DEFAULT_OFFLOAD_TIER = "mid"` and
        # the comment beside it which had ALREADY diagnosed this exact waste ("a gemini offload
        # burned Pro"). A profile that pins one rung of a three-rung ladder removes the choice the
        # ladder exists to make.
        _profile(
            "gemini-3.6-flash-high",
            "gemini-3.6-flash-high",
            "high",
            agent="gemini",
            provider="google",
            pool="gemini-prepaid",
            adapter_version="agy-cli-profile-v1",
        ),
        _profile(
            "gemini-3.1-pro-high",
            "gemini-3.1-pro-high",
            "high",
            agent="gemini",
            provider="google",
            pool="gemini-prepaid",
            adapter_version="agy-cli-profile-v1",
        ),
        _profile(
            "cursor-composer-2.5",
            "composer-2.5",
            "high",
            agent="cursor",
            provider="cursor",
            pool="cursor-subscription",
            adapter_version="cursor-cli-profile-v1",
        ),
        _profile(
            "vibe-medium-3.5",
            "mistral-medium-3.5",
            "high",
            agent="vibe",
            provider="mistral",
            pool="vibe-subscription",
            adapter_version="vibe-cli-profile-v1",
        ),
    )
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_profiles (
  registry_version INTEGER NOT NULL, profile_id TEXT NOT NULL,
  agent TEXT NOT NULL, provider TEXT NOT NULL, requested_model TEXT NOT NULL,
  reasoning_effort TEXT, permission_mode TEXT NOT NULL,
  transport_support_json TEXT NOT NULL, legacy_adapter_mode TEXT,
  adapter_version TEXT NOT NULL, prompt_version TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL, successor_profile_id TEXT,
  definition_json TEXT NOT NULL, created_ts INTEGER NOT NULL,
  PRIMARY KEY (registry_version, profile_id)
);
CREATE TABLE IF NOT EXISTS capacity_pools (
  pool_id TEXT PRIMARY KEY, provider TEXT NOT NULL, account TEXT NOT NULL,
  window TEXT NOT NULL, tier TEXT NOT NULL, definition_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_profile_pools (
  registry_version INTEGER NOT NULL, profile_id TEXT NOT NULL, pool_id TEXT NOT NULL,
  PRIMARY KEY (registry_version, profile_id, pool_id)
);
CREATE TABLE IF NOT EXISTS routing_decisions_v2 (
  decision_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, task_type TEXT NOT NULL,
  target TEXT, candidate_profiles_json TEXT NOT NULL, gate_results_json TEXT NOT NULL,
  scores_json TEXT NOT NULL, selected_profile_id TEXT, exploration INTEGER NOT NULL,
  exploration_policy TEXT NOT NULL, rng_seed INTEGER NOT NULL,
  policy_version TEXT NOT NULL, assignment_probability REAL NOT NULL,
  causal_context_json TEXT, replay_hash TEXT NOT NULL,
  profile_attempt_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS route_weights_v2 (
  version INTEGER NOT NULL, ts INTEGER NOT NULL, task_type TEXT NOT NULL,
  profile_id TEXT NOT NULL, agent TEXT NOT NULL, provider TEXT NOT NULL,
  agent_prior REAL NOT NULL, provider_prior REAL NOT NULL, transferred_prior REAL NOT NULL,
  posterior REAL NOT NULL, n_obs INTEGER NOT NULL, effective_n REAL NOT NULL,
  score REAL NOT NULL, prior_source TEXT NOT NULL,
  resolved_model_coverage REAL NOT NULL, learning_gate_passed INTEGER NOT NULL,
  evidence_age_days REAL, rationale TEXT NOT NULL,
  PRIMARY KEY (version, task_type, profile_id)
);
"""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    required = {
        "profile_id",
        "agent",
        "provider",
        "requested_model",
        "reasoning_effort",
        "permission_mode",
        "transport_support",
        "adapter_version",
        "prompt_version",
        "capacity_pool_ids",
        "lifecycle_status",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise ValueError(f"execution profile missing fields: {missing}")
    if profile["permission_mode"] not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(f"invalid permission_mode: {profile['permission_mode']}")
    if not profile["capacity_pool_ids"]:
        raise ValueError("execution profile requires at least one capacity pool")
    unknown = sorted(set(profile["capacity_pool_ids"]) - set(CAPACITY_POOLS))
    if unknown:
        raise ValueError(f"unknown capacity pools: {unknown}")
    return dict(profile)


def get_profile(profile: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(profile, dict):
        return validate_profile(profile)
    try:
        return validate_profile(PROFILE_REGISTRY[str(profile)])
    except KeyError as exc:
        raise ValueError(f"unknown execution profile: {profile}") from exc


def profiles_for_agent(agent: str, *, transport: str | None = None) -> list[dict[str, Any]]:
    rows = [
        dict(p)
        for p in PROFILE_REGISTRY.values()
        if p["agent"] == agent and p["lifecycle_status"] == "active"
    ]
    if transport:
        rows = [p for p in rows if transport in p["transport_support"]]
    return sorted(rows, key=lambda row: row["profile_id"])


def ensure_schema(conn: sqlite3.Connection, *, now: int | None = None) -> None:
    conn.executescript(SCHEMA)
    decision_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(routing_decisions_v2)").fetchall()
    }
    if "profile_attempt_ids_json" not in decision_columns:
        conn.execute(
            "ALTER TABLE routing_decisions_v2 ADD COLUMN "
            "profile_attempt_ids_json TEXT NOT NULL DEFAULT '[]'"
        )
    weight_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(route_weights_v2)").fetchall()
    }
    if "provider_prior" not in weight_columns:
        conn.execute(
            "ALTER TABLE route_weights_v2 ADD COLUMN provider_prior REAL NOT NULL DEFAULT 0.5"
        )
    now = int(now or time.time())
    for pool_id, pool in CAPACITY_POOLS.items():
        payload = _canonical(pool)
        existing = conn.execute(
            "SELECT definition_json FROM capacity_pools WHERE pool_id=?", (pool_id,)
        ).fetchone()
        if existing and existing[0] != payload:
            raise ValueError(f"immutable capacity pool changed: {pool_id}")
        conn.execute(
            "INSERT OR IGNORE INTO capacity_pools VALUES (?,?,?,?,?,?)",
            (pool_id, pool["provider"], pool["account"], pool["window"], pool["tier"], payload),
        )
    for profile in PROFILE_REGISTRY.values():
        p = validate_profile(profile)
        payload = _canonical(p)
        existing = conn.execute(
            "SELECT definition_json FROM execution_profiles WHERE registry_version=? AND profile_id=?",
            (PROFILE_SCHEMA_VERSION, p["profile_id"]),
        ).fetchone()
        if existing and existing[0] != payload:
            raise ValueError(f"immutable execution profile changed: {p['profile_id']}")
        conn.execute(
            "INSERT OR IGNORE INTO execution_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                PROFILE_SCHEMA_VERSION,
                p["profile_id"],
                p["agent"],
                p["provider"],
                p["requested_model"],
                p["reasoning_effort"],
                p["permission_mode"],
                _canonical(p["transport_support"]),
                p.get("legacy_adapter_mode"),
                p["adapter_version"],
                p["prompt_version"],
                p["lifecycle_status"],
                p.get("successor_profile_id"),
                payload,
                now,
            ),
        )
        for pool_id in p["capacity_pool_ids"]:
            conn.execute(
                "INSERT OR IGNORE INTO execution_profile_pools VALUES (?,?,?)",
                (PROFILE_SCHEMA_VERSION, p["profile_id"], pool_id),
            )


def select_profile(
    task_type: str,
    target: str | None,
    candidate_profile_ids: list[str],
    *,
    rng_seed: int,
    scores: dict[str, float] | None = None,
    gate_results: dict[str, dict[str, Any]] | None = None,
    exploration: bool = False,
    exploration_policy: str = "deterministic-best",
    policy_version: str = PROFILE_POLICY_VERSION,
    causal_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = sorted(dict.fromkeys(str(pid) for pid in candidate_profile_ids))
    for profile_id in candidates:
        get_profile(profile_id)
    gates = gate_results or {pid: {"eligible": True} for pid in candidates}
    eligible = [pid for pid in candidates if (gates.get(pid) or {}).get("eligible", True)]
    if not eligible:
        selected = None
        probability = 0.0
    elif exploration:
        selected = random.Random(int(rng_seed)).choice(eligible)
        probability = 1.0 / len(eligible)
    else:
        values = scores or {}
        selected = min(eligible, key=lambda pid: (-float(values.get(pid, 0.0)), pid))
        probability = 1.0
    body = {
        "schema_version": 2,
        "task_type": task_type,
        "target": target,
        "candidate_profile_ids": candidates,
        "gate_results": gates,
        "scores": scores or {},
        "selected_profile_id": selected,
        "exploration": bool(exploration),
        "exploration_policy": exploration_policy,
        "rng_seed": int(rng_seed),
        "policy_version": policy_version,
        "assignment_probability": probability,
        "causal_context": causal_context or {},
    }
    body["replay_hash"] = _digest(body)
    body["decision_id"] = f"profile-decision:{body['replay_hash'][:24]}"
    return body


def replay_decision(envelope: dict[str, Any]) -> dict[str, Any]:
    replayed = select_profile(
        envelope["task_type"],
        envelope.get("target"),
        envelope["candidate_profile_ids"],
        rng_seed=int(envelope["rng_seed"]),
        scores=envelope.get("scores") or {},
        gate_results=envelope.get("gate_results") or {},
        exploration=bool(envelope.get("exploration")),
        exploration_policy=envelope.get("exploration_policy") or "deterministic-best",
        policy_version=envelope["policy_version"],
        causal_context=envelope.get("causal_context") or {},
    )
    if replayed["replay_hash"] != envelope.get("replay_hash"):
        raise ValueError("profile decision envelope is not replayable")
    return replayed


def record_decision(
    conn: sqlite3.Connection, envelope: dict[str, Any], *, ts: int | None = None
) -> None:
    replay_decision(envelope)
    ensure_schema(conn, now=ts)
    conn.execute(
        "INSERT OR REPLACE INTO routing_decisions_v2 "
        "(decision_id,ts,task_type,target,candidate_profiles_json,gate_results_json,"
        "scores_json,selected_profile_id,exploration,exploration_policy,rng_seed,"
        "policy_version,assignment_probability,causal_context_json,replay_hash,"
        "profile_attempt_ids_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            envelope["decision_id"],
            int(ts or time.time()),
            envelope["task_type"],
            envelope.get("target"),
            _canonical(envelope["candidate_profile_ids"]),
            _canonical(envelope.get("gate_results") or {}),
            _canonical(envelope.get("scores") or {}),
            envelope.get("selected_profile_id"),
            int(bool(envelope.get("exploration"))),
            envelope.get("exploration_policy") or "",
            int(envelope["rng_seed"]),
            envelope["policy_version"],
            float(envelope.get("assignment_probability") or 0.0),
            _canonical(envelope.get("causal_context") or {}),
            envelope["replay_hash"],
            _canonical(envelope.get("profile_attempt_ids") or []),
        ),
    )


def attach_profile_attempt(
    conn: sqlite3.Connection, decision_id: str, attempt_id: str
) -> list[str]:
    """Attach a real persisted attempt to its immutable routing decision."""
    row = conn.execute(
        "SELECT profile_attempt_ids_json,selected_profile_id FROM routing_decisions_v2 "
        "WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown routing decision: {decision_id}")
    attempt = conn.execute(
        "SELECT profile_id FROM execution_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if not attempt:
        raise ValueError(f"unknown execution attempt: {attempt_id}")
    if row[1] and attempt[0] != row[1]:
        raise ValueError("routing decision/profile attempt mismatch")
    ids = sorted(set(json.loads(row[0] or "[]")) | {attempt_id})
    conn.execute(
        "UPDATE routing_decisions_v2 SET profile_attempt_ids_json=? WHERE decision_id=?",
        (_canonical(ids), decision_id),
    )
    return ids


def resolved_model_coverage(conn: sqlite3.Connection, profile_id: str) -> dict[str, Any]:
    profile = get_profile(profile_id)
    total, resolved, fallback, latest = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN resolved_model=? THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN fallback_reason IS NOT NULL OR (resolved_model IS NOT NULL AND resolved_model<>?) THEN 1 ELSE 0 END), "
        "MAX(COALESCE(ea.completed_ts,ea.recorded_ts)) FROM execution_attempts ea "
        "JOIN runs r ON r.run_id=ea.run_id "
        "WHERE ea.operation_role='worker' AND ea.profile_id=? "
        "AND COALESCE(r.assignment,'experimental')<>'instrumentation'",
        (profile["requested_model"], profile["requested_model"], profile_id),
    ).fetchone()
    total = int(total or 0)
    resolved = int(resolved or 0)
    fallback = int(fallback or 0)
    coverage = resolved / total if total else 0.0
    return {
        "profile_id": profile_id,
        "attempts": total,
        "resolved_attempts": resolved,
        "coverage": coverage,
        "fallback_rate": fallback / total if total else 0.0,
        "learning_ready": total >= MIN_EXACT_OBSERVATIONS and coverage >= MIN_RESOLVED_COVERAGE,
        "latest_evidence_ts": latest,
    }


def _bounded_prior(
    agent_prior: float, provider_prior: float, profile: dict[str, Any]
) -> tuple[float, str]:
    offset = max(
        -MAX_SUCCESSOR_TRANSFER,
        min(MAX_SUCCESSOR_TRANSFER, float(profile.get("prior_offset") or 0.0)),
    )
    hierarchical = 0.75 * agent_prior + 0.25 * provider_prior
    return max(0.01, min(0.99, hierarchical + offset)), "agent_provider_bounded_transfer"


def relearn_route_weights_v2(
    conn: sqlite3.Connection,
    task_type_priors: dict[str, dict[str, float]],
    *,
    now: int | None = None,
) -> int:
    """Write shadow profile weights; legacy rows without profile IDs are never inferred."""
    ensure_schema(conn, now=now)
    now = int(now or time.time())
    version = int(
        conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM route_weights_v2").fetchone()[0]
    )
    for task_type, priors in task_type_priors.items():
        # Research arms and retries are correlated within an independent subject
        # family. Reuse the issue-1 subject weights instead of allowing repeated
        # rows from one issue to overwhelm the hierarchical prior. Production
        # (non-experiment) runs remain one observation each; legacy experiments
        # without explicit subject linkage are conservatively omitted.
        import research_subjects

        subject_weights = research_subjects.effective_evidence_weights(
            conn=conn, task_type=task_type
        )
        for profile in PROFILE_REGISTRY.values():
            if profile["agent"] not in priors:
                continue
            profile_id = profile["profile_id"]
            agent_prior = float(priors[profile["agent"]])
            provider_agents = {
                candidate["agent"]
                for candidate in PROFILE_REGISTRY.values()
                if candidate["provider"] == profile["provider"] and candidate["agent"] in priors
            }
            provider_prior = (
                sum(float(priors[agent]) for agent in provider_agents) / len(provider_agents)
                if provider_agents
                else agent_prior
            )
            transferred, source = _bounded_prior(agent_prior, provider_prior, profile)
            coverage = resolved_model_coverage(conn, profile_id)
            rows: list[tuple] = []
            if coverage["learning_ready"]:
                rows = conn.execute(
                    "SELECT r.run_id,o.durability,o.adjudicated_verdict,"
                    "o.verifier_verdict,r.ts,r.experiment_id "
                    "FROM runs r JOIN outcomes o ON o.run_id=r.run_id "
                    "WHERE r.task_type=? AND COALESCE(r.assignment,'experimental')='experimental' "
                    "AND EXISTS (SELECT 1 FROM execution_attempts ea WHERE ea.run_id=r.run_id "
                    "AND ea.operation_role='worker' AND ea.profile_id=? AND ea.resolved_model=?)",
                    (task_type, profile_id, profile["requested_model"]),
                ).fetchall()
            weighted_rewards: list[tuple[float, float, int]] = []
            for run_id, durability, adjudicated, verifier, run_ts, experiment_id in rows:
                if experiment_id is not None:
                    if str(run_id) not in subject_weights:
                        continue
                    weight = float(subject_weights[str(run_id)])
                else:
                    weight = 1.0
                reward = (
                    1.0
                    if str(durability or "").lower() == "durable"
                    and str(adjudicated or "").upper().startswith("PASS")
                    and not str(verifier or "").upper().startswith("FAIL")
                    else 0.0
                )
                weighted_rewards.append((reward, weight, int(run_ts or 0)))
            n = len(weighted_rewards)
            effective_n = sum(weight for _reward, weight, _ts in weighted_rewards)
            reward_sum = sum(reward * weight for reward, weight, _ts in weighted_rewards)
            learning_gate_passed = (
                coverage["learning_ready"] and effective_n >= MIN_EXACT_OBSERVATIONS
            )
            learned_effective_n = effective_n if learning_gate_passed else 0.0
            learned_reward_sum = reward_sum if learning_gate_passed else 0.0
            posterior = (PRIOR_STRENGTH * transferred + learned_reward_sum) / (
                PRIOR_STRENGTH + learned_effective_n
            )
            evidence_ts = max((_ts for _reward, _weight, _ts in weighted_rewards), default=0)
            age = ((now - evidence_ts) / 86400.0) if evidence_ts else None
            rationale = (
                f"shadow_v2 agent_prior={agent_prior:.3f} provider_prior={provider_prior:.3f} "
                f"transferred_prior={transferred:.3f} "
                f"n={n} effective_n={effective_n:.3f} "
                f"coverage={coverage['coverage']:.3f} gate={learning_gate_passed} "
                "legacy_profile_inference=false"
            )
            conn.execute(
                "INSERT INTO route_weights_v2 "
                "(version,ts,task_type,profile_id,agent,provider,agent_prior,provider_prior,"
                "transferred_prior,posterior,n_obs,effective_n,score,prior_source,"
                "resolved_model_coverage,learning_gate_passed,evidence_age_days,rationale) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version,
                    now,
                    task_type,
                    profile_id,
                    profile["agent"],
                    profile["provider"],
                    agent_prior,
                    provider_prior,
                    transferred,
                    posterior,
                    n,
                    effective_n,
                    posterior,
                    source,
                    coverage["coverage"],
                    int(learning_gate_passed),
                    age,
                    rationale,
                ),
            )
    return version


def current_profile_weights(conn: sqlite3.Connection, task_type: str) -> list[dict[str, Any]]:
    ensure_schema(conn)
    version = conn.execute("SELECT COALESCE(MAX(version),0) FROM route_weights_v2").fetchone()[0]
    rows = conn.execute(
        "SELECT profile_id,agent,posterior,n_obs,score,resolved_model_coverage,learning_gate_passed,evidence_age_days "
        "FROM route_weights_v2 WHERE version=? AND task_type=? ORDER BY score DESC,profile_id",
        (version, task_type),
    ).fetchall()
    keys = (
        "profile_id",
        "agent",
        "posterior",
        "n_obs",
        "score",
        "resolved_model_coverage",
        "learning_gate_passed",
        "evidence_age_days",
    )
    return [dict(zip(keys, row)) for row in rows]


def report(conn: sqlite3.Connection, *, now: int | None = None) -> dict[str, Any]:
    ensure_schema(conn, now=now)
    now = int(now or time.time())
    profiles = []
    instrumentation_attempts_total = 0
    for profile in profiles_for_agent("codex"):
        cov = resolved_model_coverage(conn, profile["profile_id"])
        instrumentation_attempts = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_attempts ea JOIN runs r ON r.run_id=ea.run_id "
                "WHERE ea.operation_role='worker' AND ea.profile_id=? "
                "AND r.assignment='instrumentation'",
                (profile["profile_id"],),
            ).fetchone()[0]
            or 0
        )
        instrumentation_attempts_total += instrumentation_attempts
        profiles.append(
            {
                "profile_id": profile["profile_id"],
                "agent": profile["agent"],
                "provider": profile["provider"],
                "requested_model": profile["requested_model"],
                "reasoning_effort": profile["reasoning_effort"],
                "capacity_pool_ids": profile["capacity_pool_ids"],
                "readiness": "ready" if cov["learning_ready"] else "cold",
                "resolved_model_coverage": cov["coverage"],
                "fallback_rate": cov["fallback_rate"],
                "learning_eligible_attempts": cov["attempts"],
                "instrumentation_attempts": instrumentation_attempts,
                "evidence_age_days": (
                    ((now - cov["latest_evidence_ts"]) / 86400.0)
                    if cov["latest_evidence_ts"]
                    else None
                ),
            }
        )
    decisions, mean_propensity = conn.execute(
        "SELECT COUNT(*),AVG(assignment_probability) FROM routing_decisions_v2"
    ).fetchone()
    shared_pool_burn = {pool_id: 0 for pool_id in CAPACITY_POOLS}
    for profile in PROFILE_REGISTRY.values():
        attempts = conn.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE operation_role='worker' AND profile_id=?",
            (profile["profile_id"],),
        ).fetchone()[0]
        for pool_id in profile["capacity_pool_ids"]:
            shared_pool_burn[pool_id] += int(attempts or 0)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "policy_version": PROFILE_POLICY_VERSION,
        "profiles": profiles,
        "ready_profiles": sum(1 for p in profiles if p["readiness"] == "ready"),
        "cold_starts": sum(1 for p in profiles if p["readiness"] == "cold"),
        "routing_decisions": int(decisions or 0),
        "mean_assignment_probability": float(mean_propensity or 0.0),
        # This increment writes/reports v2 in shadow; production selection still
        # reads v1 agent weights until an observed report cycle approves rollout.
        "profile_weight_reads_enabled": False,
        "profile_weight_read_requested": os.environ.get("ORCH_PROFILE_WEIGHTS_V2") == "1",
        "shared_capacity_pools": sorted(CAPACITY_POOLS),
        "shared_pool_burn": shared_pool_burn,
        "instrumentation_attempts": instrumentation_attempts_total,
        "instrumentation_excluded_from_learning": True,
    }
