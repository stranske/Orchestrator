#!/usr/bin/env python3
"""router.py — the orchestrator brain: backlog + capacity + claims -> dispatch plan.

PURE PLANNER. Reads capacity.json (capacity.py), the per-target claim ledger
(claims.py), and a backlog of actionable work, and emits a dispatch plan
(routing-decision.json). It does NOT execute — execution is downstream via
adapters.py — so this stays fully testable offline.

The ROUTE_TABLE below is a HAND-SET PRIOR, intentionally expressed as data: the
LangSmith feedback loop re-ranks it later from observed per-(task_type, agent)
effectiveness (capacity-per-verified-success, pass-then-rework, feedback-miss).
Nothing here learns yet — it applies the prior under three policies:

  - priority   : code tasks planned first; review (advisory, non-gating) takes
                 only leftover capacity, and is dropped entirely when a SCARCE
                 coding seat (codex/claude) is under warn/shed pressure.
  - sequencing : within a task's ranked list, `ok` beats `warn`, and non-late
                 (free/flat) beats late (prepaid-frontier / paygo). Free is thus
                 spent first and frontier/paygo only when free is tight. Gemini's
                 windowed-prepaid policy can promote good-fit work during drain
                 windows and demote it during reserve windows.
  - concurrency: distinct targets are claimed (claims.py) so N agents run at
                 once with same-target collision impossible; capped by lanes.

`--selftest` runs offline with mocked capacity+backlog. `--dry-run` prints the
plan it WOULD dispatch without claiming or writing.
"""

from __future__ import annotations

import json
import hashlib
import os
import random
import sys
import time
from pathlib import Path

import capabilities
import claims
import execution_profiles
import feedback

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
CAPACITY_JSON = HANDOFF / "capacity.json"
BACKLOG_JSON = HANDOFF / "backlog.json"  # written by the (gh-backed) discovery step
DECISION_JSON = HANDOFF / "routing-decision.json"

MAX_CONCURRENT_DEFAULT = 4  # tunable lane cap; LangSmith may adjust
PER_AGENT_TICK_CAP = 1  # spread load: each agent takes ≤N/tick before doubling
EXPLORATION_RATE_DEFAULT = 0.05  # sustained exploration rate; override with ORCH_EXPLORATION_RATE
EXPLORATION_MODE_DEFAULT = (
    "epsilon-greedy"  # keeps the same ε safety cap; override with thompson-hybrid
)
SCARCE_AGENTS = ("codex", "claude")  # review yields when these are pressured
# Agents the GitHub keepalive can run REMOTELY via an `agent:<X>` label (must match dispatcher.REMOTE_AGENTS).
# vibe/aider are LOCAL-only (no keepalive lane), so remote delegation can't choose them.
KEEPALIVE_AGENTS = {"cursor", "codex", "claude", "gemini"}
# Premium/scarce seats RESERVED for high-leverage or last-resort remote work. Claude's WEEKLY cap is
# limited (owner used ~25% on day 1) and capacity.py only sees the 5h block, not the weekly burn — so
# routine keepalive delegation AVOIDS Claude, using it only when the work is high-leverage or no cheaper
# keepalive agent has capacity (owner directive 2026-06-14). To force Claude off entirely when the week
# is tight, shed it: touch ~/.codex/handoff/capacity-shed/claude.
RESERVE_AGENTS = {"claude"}
# Agents held OUT of routine auto-selection and kept purely as BACKUP / overflow capacity. Unlike
# RESERVE_AGENTS (claude — still chosen as a last resort), a BACKUP agent is NEVER auto-selected: it is
# reachable only on EXPLICIT demand — a manual `dispatcher.py offload/delegate --agent aider`, or
# `select_agent(..., only={"aider"})`. Owner directive 2026-06-21: aider is paygo (burns the pay-go
# Codestral credit) and was the weakest seat on the recent build-off — hold it as backup, not a seat.
BACKUP_AGENTS = {"aider"}
GEMINI_GOOD_FIT_TASKS = {"implement", "testgen", "epic", "cross_repo", "runtime_ac", "review"}

# Route table: task_type -> {role, agents:[{agent, mode, late}]}. `mode` is the
# adapter hint ('composer'/'frontier' for cursor; 'cheap'/'full' picks the model).
# `late` entries (prepaid-frontier, paygo) are used only after non-late options.
# Cursor accounting (corrected 2026-06-14 from real usage data): cursor's **Composer** (the orchestrator's
# local cursor-agent lane) is CHEAP — it was only ~3% of usage. That burn was the KEEPALIVE's GitHub
# cursor lane defaulting to gpt-5.5-high Max-Mode Cloud Agents (no --model pin) — a DIFFERENT path, fixed
# separately. So composer stays a normal cheap lane here; it draws the metered API allowance (LOCAL_POLICY.md) (not
# truly unlimited — capacity.py models that + 429-sheds when spent), but it is not the seat to avoid.
# GEMINI: a REASONING seat (≈2nd tier), compute-metered, 5h+weekly prepaid windowed capacity.
# Never mechanical/polish; use capacity.py's steady/reserve/drain policy for substantial good-fit work.
ROUTE_TABLE = {
    # TIER POLICY (2026-08-08, stage 1 of 2): task types are assigned a model LEVEL, not just an
    # agent order. cheap = mechanical/polish/codemod (low reasoning), mid = review/testgen (gated or
    # read-heavy, stage 2), full = implement/epic/cross_repo/runtime_ac (a mistake is expensive to
    # unwind). Modes map to adapters.MODEL_TIERS; unpinned agents (vibe/aider/cursor) ignore the
    # tier and use their single lane, so a tier token there is documentation, not behaviour.
    "mechanical": {
        "role": "code",
        "agents": [  # NO gemini — wasteful for low-reasoning work
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "cheap", "late": False},
            {"agent": "codex", "mode": "cheap", "late": False},
            {"agent": "aider", "mode": "cheap", "late": True},  # paygo credit -> late
            {"agent": "claude", "mode": "cheap", "late": False},
        ],
    },
    "implement": {
        "role": "code",
        "agents": [
            {"agent": "claude", "mode": "full", "late": False},
            {"agent": "codex", "mode": "full", "late": False},
            {
                "agent": "gemini",
                "mode": "full",
                "late": False,
            },  # reasoning seat; shines when codex/claude 5h-shed
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "full", "late": False},
            # REMOVED 2026-08-08 (owner policy): the late cursor/frontier lane. Cursor runs Composer
            # ONLY — see adapters.CURSOR_COMPOSER_MODEL. Cursor already appears above on composer, so
            # this entry only ever offered a second, pricier way to reach the same seat.
            {"agent": "aider", "mode": "full", "late": True},
        ],
    },
    # STAGE 2 (2026-08-10): testgen -> mid. `testgen_gate.py` must pass before a PR, so a generated
    # test that is wrong is caught by machine ground truth rather than by model horsepower — the
    # flagship tier was buying little here. codex Sol->Terra and gemini Pro->Flash-high.
    "testgen": {
        "role": "code_bounded",
        "agents": [  # generated tests must pass testgen_gate.py before PR
            {"agent": "codex", "mode": "mid", "late": False},
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "mid", "late": False},
            {"agent": "gemini", "mode": "mid", "late": False},
            {"agent": "aider", "mode": "mid", "late": True},
        ],
    },
    "epic": {
        "role": "planning",
        "agents": [  # vague goal -> structured subtask plan; avoid Claude by default
            {"agent": "gemini", "mode": "full", "late": False},
            {"agent": "codex", "mode": "full", "late": False},
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "full", "late": False},
            {"agent": "aider", "mode": "full", "late": True},
        ],
    },
    "cross_repo": {
        "role": "planning",
        "agents": [  # coordinated source+consumer planning
            {"agent": "gemini", "mode": "full", "late": False},
            {"agent": "codex", "mode": "full", "late": False},
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "full", "late": False},
            {"agent": "aider", "mode": "full", "late": True},
        ],
    },
    "runtime_ac": {
        "role": "planning",
        "agents": [  # runtime acceptance-criteria evidence planning
            {"agent": "gemini", "mode": "full", "late": False},
            {"agent": "codex", "mode": "full", "late": False},
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "full", "late": False},
            {"agent": "aider", "mode": "full", "late": True},
        ],
    },
    "codemod": {
        "role": "code_bounded",
        "agents": [  # cross-file structural campaigns; cheap lanes first
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "cheap", "late": False},
            {"agent": "codex", "mode": "cheap", "late": False},
            # gemini full->cheap is the one real behaviour change in stage 1 (3.1 Pro -> 3.6 Flash-low).
            # Watch this cell: if codemod diff quality drops, promote gemini here to 'mid' (Flash-high)
            # rather than reverting the whole tier.
            {"agent": "gemini", "mode": "cheap", "late": False},
            {"agent": "aider", "mode": "cheap", "late": True},
        ],
    },
    "polish": {
        "role": "code_bounded",
        "agents": [  # NO gemini — bounded follow-ups are cheap work
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "cheap", "late": False},
            {"agent": "codex", "mode": "cheap", "late": False},
            {"agent": "aider", "mode": "cheap", "late": True},
        ],
    },
    # STAGE 2 (2026-08-10): review -> mid, deliberately in BOTH directions. gemini drops Pro ->
    # Flash-high (cheaper on a compute-metered seat), while codex Luna->Terra and claude
    # Haiku->Sonnet 5 go UP: review quality feeds the durability labels the whole learner depends
    # on, so it is the one place worth paying more rather than less.
    "review": {
        "role": "review",
        "agents": [
            {"agent": "cursor", "mode": "composer", "late": False},
            {"agent": "vibe", "mode": "mid", "late": False},
            {
                "agent": "gemini",
                "mode": "mid",
                "late": False,
            },  # Google = a 5th family; reasoning review is unit-worthy
            {"agent": "codex", "mode": "mid", "late": False},  # only-if-idle enforced below
            {"agent": "claude", "mode": "mid", "late": False},
        ],
    },
}


def _state(cap: dict, agent: str) -> str:
    return (cap.get("agents", {}).get(agent, {}) or {}).get("state", "unknown")


def _policy(cap: dict, agent: str) -> str:
    return (cap.get("agents", {}).get(agent, {}) or {}).get("policy", "")


def _capacity_policy_bias(task_type: str, agent: str, cap: dict) -> int:
    """Soft capacity policy rank adjustment.

    Negative is better. Keep this after hard state/late checks so policy never jumps a shed/warn/late
    boundary. Gemini drain should spend suitable prepaid window headroom; reserve should hold it behind
    normal ok seats without making it unavailable for worthwhile fallback work.
    """
    if agent != "gemini":
        return 0
    policy = _policy(cap, agent)
    if policy == "drain" and task_type in GEMINI_GOOD_FIT_TASKS:
        return -1
    if policy == "reserve":
        return 1
    return 0


def _drain_urgency(agent: str, cap: dict) -> float:
    """item 16(e) (2026-07-08): CONTINUOUS use-it-or-lose-it pressure, refining ranking WITHIN a
    capacity tier (the int policy bias above still defines the tier — this never jumps a
    shed/warn/late boundary). For seats reporting windowed-quota fields, unused expiring quota
    close to refresh reads as cheaper: urgency = -(unused_fraction x time_pressure), where
    time_pressure ramps 0 (fresh window) -> 1 (refresh imminent). Seats without the fields (flat
    or unmetered) contribute 0.0 — ranking unchanged. Replaces nothing; deepens the binary drain
    flag into budget pacing (R3: BaRP/ParetoBandit)."""
    row = cap.get("agents", {}).get(agent, {}) or {}
    try:
        soft = float(row.get("soft_units_5h") or 0)
        used = float(row.get("estimated_units_5h") or 0)
        mins = float(row.get("minutes_to_window_refresh"))
    except (TypeError, ValueError):
        return 0.0
    if soft <= 0 or mins < 0:
        return 0.0
    unused_fraction = max(0.0, min(1.0, (soft - used) / soft))
    window_minutes = 300.0  # 5h windows
    time_pressure = max(0.0, min(1.0, 1.0 - mins / window_minutes))
    return -round(unused_fraction * time_pressure, 4)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _exploration_rate(rate: float | None) -> float:
    value = _env_float("ORCH_EXPLORATION_RATE", EXPLORATION_RATE_DEFAULT) if rate is None else rate
    return max(0.0, min(1.0, float(value)))


def _exploration_mode(mode: str | None = None) -> str:
    raw = (
        (mode or os.environ.get("ORCH_EXPLORATION_MODE") or EXPLORATION_MODE_DEFAULT)
        .strip()
        .lower()
    )
    raw = raw.replace("_", "-")
    aliases = {
        "epsilon": "epsilon-greedy",
        "epsilon-greedy": "epsilon-greedy",
        "egreedy": "epsilon-greedy",
        "thompson": "thompson-hybrid",
        "thompson-hybrid": "thompson-hybrid",
        "hybrid-thompson": "thompson-hybrid",
    }
    return aliases.get(raw, EXPLORATION_MODE_DEFAULT)


def _learned_rank(learned: dict | None, agent: str, fallback: int) -> int:
    if not learned or agent not in learned:
        return fallback
    value = learned[agent]
    if isinstance(value, dict):
        return int(value.get("rank", fallback))
    return int(value)


def _learned_n_obs(learned: dict | None, agent: str) -> int:
    if not learned or agent not in learned:
        return 0
    value = learned[agent]
    if isinstance(value, dict):
        return int(value.get("n_obs") or 0)
    return 0


def _learned_posterior_score(
    learned: dict | None, agent: str, fallback: float
) -> tuple[float, float, int]:
    posterior = fallback
    score = fallback
    n_obs = 0
    if learned and agent in learned and isinstance(learned[agent], dict):
        row = learned[agent]
        try:
            posterior = (
                float(row.get("posterior")) if row.get("posterior") is not None else fallback
            )
        except (TypeError, ValueError):
            posterior = fallback
        try:
            score = float(row.get("score")) if row.get("score") is not None else posterior
        except (TypeError, ValueError):
            score = posterior
        try:
            n_obs = max(0, int(row.get("n_obs") or 0))
        except (TypeError, ValueError):
            n_obs = 0
    return max(0.001, min(0.999, posterior)), max(0.0, score), n_obs


def _prior_posterior_from_rank(idx: int, total: int) -> float:
    """Cold-start prior approximation for Thompson sampling.

    Live learned rows already carry posterior/score. When a candidate has no row, keep the hand-set route
    order as a conservative pseudo-posterior so Thompson exploration does not erase the table prior.
    """
    if total <= 1:
        return 0.60
    step = 0.20 / max(1, total - 1)
    return max(0.45, min(0.70, 0.65 - idx * step))


def _thompson_sample(row: tuple, learned: dict | None, rng, *, total_candidates: int) -> float:
    _score_tuple, entry, _st, idx = row
    fallback = _prior_posterior_from_rank(idx, total_candidates)
    posterior, score, n_obs = _learned_posterior_score(learned, entry["agent"], fallback)
    strength = max(1.0, float(getattr(feedback, "PRIOR_STRENGTH", 8.0)) + float(n_obs))
    alpha = max(0.001, posterior * strength)
    beta = max(0.001, (1.0 - posterior) * strength)
    effort_multiplier = score / posterior if posterior > 0 else 1.0
    effort_multiplier = max(0.0, min(1.5, effort_multiplier))
    return rng.betavariate(alpha, beta) * effort_multiplier


def entry_agent(picked: tuple) -> str:
    """Agent name out of a scored-selection tuple, for heartbeat refs."""
    try:
        return str(picked[1].get("agent") or "?")
    except Exception:
        return "?"


def _capability_heartbeat(capability_id: str, event_type: str, *, ref: str = "") -> None:
    """Record that a routing capability ran. Daily-coalesced (routing runs many times per tick),
    lazy import, never raises, inert outside an active tick. (2026-08-09)"""
    try:
        import capabilities

        capabilities.daily_heartbeat(capability_id, event_type, ref=ref or None)
    except Exception:
        pass


def _thompson_exploration_choice(scored: list, learned: dict | None, rng) -> tuple | None:
    """Choose a same-policy-tier challenger with a Thompson-style posterior sample.

    This is intentionally hybrid: the router still enters exploration only under ε, and this helper can
    only choose from the winner's existing policy tier. It cannot jump to late/paygo or warn capacity while
    an ok non-late winner exists.
    """
    if len(scored) < 2:
        return None
    winner_policy = scored[0][0][:4]  # under-cap, non-late, ok-before-warn, capacity-policy bias
    pool = [row for row in scored[1:] if row[0][:4] == winner_policy]
    if not pool:
        return None
    total = len(scored)
    best = max(
        pool, key=lambda row: (_thompson_sample(row, learned, rng, total_candidates=total), -row[3])
    )
    return best


def _exploration_choice(scored: list, learned: dict | None, rng) -> tuple | None:
    """Choose a same-policy-tier challenger, preferring the least-observed agent.

    Exploration should refresh posteriors without violating the router's hard economics:
    do not jump to late/paygo or warn/over-cap candidates while an ok/non-late candidate
    exists in the winner's tier.
    """
    if len(scored) < 2:
        return None
    winner_policy = scored[0][0][:4]  # under-cap, non-late, ok-before-warn, capacity-policy bias
    pool = [row for row in scored[1:] if row[0][:4] == winner_policy]
    if not pool:
        return None
    min_n = min(_learned_n_obs(learned, row[1]["agent"]) for row in pool)
    least_observed = [row for row in pool if _learned_n_obs(learned, row[1]["agent"]) == min_n]
    return rng.choice(least_observed)


def select_agent(
    task_type: str,
    cap: dict,
    *,
    allow_warn: bool = True,
    load: dict | None = None,
    per_agent_cap: int = PER_AGENT_TICK_CAP,
    learned: dict | None = None,
    only: set | None = None,
    exploration_rate: float | None = None,
    exploration_mode: str | None = None,
    rng=None,
    profile_seed: int | None = None,
    profile_scores: dict[str, float] | None = None,
    causal_context: dict | None = None,
    profile_transport: str = "local",
):
    """Pick the best available agent entry for a task, or None if none have capacity.

    Selectable: state in {ok, warn(if allow_warn)}; shed/unknown skipped. Ranked by
    (under-tick-cap first, then non-late, then ok-before-warn, then capacity policy, then LEARNED rank), with
    ROUTE_TABLE order as a stable tiebreak. The under-cap key SPREADS concurrent work; the
    learned key lets the feedback store REORDER agents within a capacity tier from observed
    effectiveness — but only when `learned` is supplied (no data => identical to the prior).
    `learned` = {agent: rank} or {agent: {rank,n_obs,...}} for THIS task_type (0 = best); absent
    agents sort after known ones.
    `only` (optional) = restrict candidates to this set of agents (e.g. KEEPALIVE_AGENTS for remote).
    With exploration enabled, occasionally pick a same-policy-tier challenger. The default
    epsilon-greedy mode refreshes least-observed eligible agents inside the same ε cap; Thompson-hybrid
    remains available as an override for posterior-sampling challenger reviews.
    """
    spec = ROUTE_TABLE.get(task_type)
    if not spec:
        return None
    load = load or {}
    scored = []
    for idx, entry in enumerate(spec["agents"]):
        if only is not None and entry["agent"] not in only:
            continue
        # backup-only seats (aider): never auto-selected; reachable only when a caller explicitly
        # restricts to them via `only` (e.g. only={"aider"}). Keeps them as on-demand overflow capacity.
        if entry["agent"] in BACKUP_AGENTS and not (only and entry["agent"] in only):
            continue
        st = _state(cap, entry["agent"])
        if st in ("shed", "unknown"):
            continue
        if st == "warn" and not allow_warn:
            continue
        agent_profiles = execution_profiles.profiles_for_agent(
            entry["agent"], transport=profile_transport
        )
        if agent_profiles and not any(
            (
                cap.get("profiles", {}).get(profile["profile_id"], {}).get("state", st)
                not in ("shed", "unknown")
            )
            for profile in agent_profiles
        ):
            # A shared provider pool can shed every model profile while the
            # coarse agent snapshot still says OK. Treat that agent as
            # unavailable so the deterministic router can fall through to the
            # next agent instead of selecting profile=None and crashing.
            continue
        over_cap = 1 if load.get(entry["agent"], 0) >= per_agent_cap else 0
        score = (
            over_cap,
            0 if not entry["late"] else 1,
            0 if st == "ok" else 1,
            _capacity_policy_bias(task_type, entry["agent"], cap),
            _drain_urgency(entry["agent"], cap),
        )  # 16(e): continuous pacing within the tier
        if learned:  # feedback-learned reorder WITHIN the tier (opt-in via data)
            score = score + (_learned_rank(learned, entry["agent"], len(learned) + idx),)
        scored.append((score, entry, st, idx))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])  # stable -> preserves table order within a score
    picked = scored[0]
    explored = False
    rng = rng or random
    epsilon = _exploration_rate(exploration_rate)
    if epsilon > 0 and rng.random() < epsilon:
        mode = _exploration_mode(exploration_mode)
        challenger = (
            _thompson_exploration_choice(scored, learned, rng)
            if mode == "thompson-hybrid"
            else _exploration_choice(scored, learned, rng)
        )
        if challenger is not None:
            picked = challenger
            explored = True
            if mode == "thompson-hybrid":
                # Records when Thompson sampling ACTUALLY chose a challenger — not merely when the
                # flag is set — so the capability's evidence reflects real use. Daily-coalesced:
                # routing runs many times per tick. (2026-08-09)
                _capability_heartbeat(
                    "thompson-hybrid-routing",
                    "invocation",
                    ref=f"{task_type}:{entry_agent(challenger)}",
                )
    _, entry, st, _idx = picked
    result = {
        **entry,
        "state": st,
        "capacity_policy": _policy(cap, entry["agent"]),
        "exploration": explored,
        "exploration_mode": _exploration_mode(exploration_mode) if explored else "",
    }
    profiles = execution_profiles.profiles_for_agent(entry["agent"], transport=profile_transport)
    if profiles:
        candidate_ids = [profile["profile_id"] for profile in profiles]
        gates = {
            profile_id: {
                "eligible": (
                    cap.get("profiles", {}).get(profile_id, {}).get("state", st)
                    not in ("shed", "unknown")
                ),
                "capacity_state": cap.get("profiles", {}).get(profile_id, {}).get("state", st),
            }
            for profile_id in candidate_ids
        }
        static_scores = {
            profile["profile_id"]: (
                float(profile_scores.get(profile["profile_id"], 0.0))
                if profile_scores
                else float(profile.get("prior_offset") or 0.0)
            )
            for profile in profiles
        }
        envelope = execution_profiles.select_profile(
            task_type,
            (causal_context or {}).get("target"),
            candidate_ids,
            rng_seed=int(profile_seed or 0),
            scores=static_scores,
            gate_results=gates,
            exploration=explored,
            exploration_policy=(
                f"{_exploration_mode(exploration_mode)}-profile"
                if explored
                else "deterministic-profile-prior"
            ),
            causal_context=causal_context,
        )
        if not envelope["selected_profile_id"]:
            return None
        selected = execution_profiles.get_profile(envelope["selected_profile_id"])
        result.update(
            {
                "selected_profile_id": selected["profile_id"],
                "requested_model": selected["requested_model"],
                "reasoning_effort": selected["reasoning_effort"],
                "permission_mode": selected["permission_mode"],
                "transport": profile_transport,
                "profile_decision": envelope,
                "candidate_profile_ids": envelope["candidate_profile_ids"],
                "profile_policy_version": envelope["policy_version"],
                "profile_assignment_probability": envelope["assignment_probability"],
                "profile_rng_seed": envelope["rng_seed"],
            }
        )
    return result


def replay_profile_choice(assignment: dict) -> dict | None:
    envelope = assignment.get("profile_decision")
    return execution_profiles.replay_decision(envelope) if envelope else None


def learned_ranks() -> dict | None:
    """Build {task_type: {agent: rank}} from the feedback store's current learned weights (0 = best,
    by descending score). Empty/None until relearn()/relearn_quality() has run — so the live planner
    applies learning when it exists and falls back to the hand-set prior when it doesn't."""
    out = {}
    for tt in ROUTE_TABLE:
        try:
            w = feedback.current_weights(tt)
        except Exception:
            w = []
        if w:
            out[tt] = {
                row["agent"]: {
                    "rank": i,
                    "n_obs": row.get("n_obs") or 0,
                    "posterior": row.get("posterior"),
                    "score": row.get("score"),
                }
                for i, row in enumerate(w)
            }
    return out or None


def select_remote_agent(
    task_type: str,
    cap: dict,
    *,
    load: dict | None = None,
    learned: dict | None = None,
    high_leverage: bool = False,
):
    """Gate #2: choose which `agent:<X>` label to apply for REMOTE keepalive delegation. Route-table +
    learned-weights + capacity ranking, restricted to keepalive-runnable lanes (KEEPALIVE_AGENTS;
    vibe/aider have no remote lane). RESERVE_AGENTS (claude) are EXCLUDED for routine work — Claude's
    weekly cap is scarce — and used ONLY when `high_leverage=True` or no cheaper keepalive agent has
    capacity (last-resort). Returns the chosen entry {agent, mode, state} or None. The caller hands the
    agent to dispatcher.delegate_remote()."""
    pool = KEEPALIVE_AGENTS if high_leverage else (KEEPALIVE_AGENTS - RESERVE_AGENTS)
    pick = select_agent(
        task_type,
        cap,
        load=load,
        learned=learned,
        only=pool,
        profile_transport="remote",
    )
    if (
        pick is None and not high_leverage
    ):  # last resort: nothing cheaper has capacity -> allow reserve
        pick = select_agent(
            task_type,
            cap,
            load=load,
            learned=learned,
            only=KEEPALIVE_AGENTS,
            profile_transport="remote",
        )
    return pick


def _pressure(cap: dict) -> bool:
    """A scarce coding seat is constrained -> review work yields this tick."""
    return any(_state(cap, a) in ("warn", "shed") for a in SCARCE_AGENTS)


def _lane_cap(cap: dict, max_concurrent: int) -> int:
    """How many concurrent lanes capacity allows: non-shed code agents, capped."""
    code_agents = {
        e["agent"]
        for spec in ROUTE_TABLE.values()
        if spec.get("role") != "review"
        for e in spec["agents"]
    }
    available = sum(1 for a in code_agents if _state(cap, a) not in ("shed", "unknown"))
    return max(1, min(max_concurrent, available)) if available else 0


def plan(
    backlog: list[dict],
    cap: dict,
    *,
    max_concurrent: int = MAX_CONCURRENT_DEFAULT,
    dry_run: bool = False,
    learned: dict | None = None,
    capability_records: dict | None = None,
) -> dict:
    """Build the dispatch plan. backlog item = {target, task_type, lane}. `learned` (optional) =
    {task_type: {agent: rank}} from the feedback store; reorders within capacity tiers when present.
    """
    if not dry_run:
        reaped = claims.reap_stale()
    else:
        reaped = []
    if capability_records is None:
        try:
            capability_records = capabilities.load(create=False)
        except (OSError, ValueError, json.JSONDecodeError):
            capability_records = {}

    code_items = [b for b in backlog if ROUTE_TABLE.get(b["task_type"], {}).get("role") != "review"]
    review_items = [
        b for b in backlog if ROUTE_TABLE.get(b["task_type"], {}).get("role") == "review"
    ]

    cap_lanes = _lane_cap(cap, max_concurrent)
    pressure = _pressure(cap)
    active_claim_map = claims.active_claims()
    in_flight = set(active_claim_map.keys())
    assignments: list[dict] = []
    rejections: list[dict] = []
    claimed_by: dict[str, str] = {
        target: str((meta or {}).get("agent") or "unknown")
        for target, meta in active_claim_map.items()
    }
    capacity_rejections: list[dict] = []
    already_routed: list[dict] = []
    load: dict[str, int] = {}  # per-agent assignments THIS tick (spread)
    notes: list[str] = []
    if reaped:
        notes.append(f"reaped stale claims: {reaped}")

    def _assign(items: list[dict], *, allow_warn: bool) -> None:
        for b in items:
            if len(assignments) >= cap_lanes:
                notes.append(f"lane cap {cap_lanes} reached; deferred remaining work")
                rejections.append(
                    {
                        "target": b.get("target"),
                        "task_type": b.get("task_type"),
                        "reason": "lane_cap_reached",
                        "lane_cap": cap_lanes,
                    }
                )
                continue
            tgt = b["target"]
            holder = active_claim_map.get(tgt)
            if holder is not None:
                holder_agent = str((holder or {}).get("agent") or "unknown")
                claimed_by[tgt] = holder_agent
                row = {
                    "target": tgt,
                    "task_type": b.get("task_type"),
                    "reason": "claimed",
                    "claimed_by": holder_agent,
                }
                rejections.append(row)
                continue
            if tgt in in_flight:
                row = {
                    "target": tgt,
                    "task_type": b.get("task_type"),
                    "reason": "already_routed",
                }
                already_routed.append(row)
                rejections.append(row)
                continue
            profile_seed = int.from_bytes(
                hashlib.sha256(
                    f"{b.get('target')}|{b.get('task_type')}|{execution_profiles.PROFILE_POLICY_VERSION}".encode()
                ).digest()[:8],
                "big",
            )
            entry = select_agent(
                b["task_type"],
                cap,
                allow_warn=allow_warn,
                load=load,
                learned=(learned or {}).get(b["task_type"]),
                profile_seed=profile_seed,
                causal_context={
                    "target": b.get("target"),
                    **({"subject_id": b["subject_id"]} if b.get("subject_id") else {}),
                    **({"arm_id": b["arm_id"]} if b.get("arm_id") else {}),
                },
            )
            if not entry:
                notes.append(f"no capacity for {b['task_type']} target {tgt}")
                row = {
                    "target": tgt,
                    "task_type": b.get("task_type"),
                    "reason": "capacity_rejected",
                    "capacity_states": {
                        agent: (meta or {}).get("state", "unknown")
                        for agent, meta in sorted((cap.get("agents") or {}).items())
                    },
                }
                capacity_rejections.append(row)
                rejections.append(row)
                continue
            got = True if dry_run else claims.claim(tgt, entry["agent"])
            if not got:
                race_holder = claims.holder(tgt) or {}
                holder_agent = str(race_holder.get("agent") or "unknown")
                claimed_by[tgt] = holder_agent
                rejections.append(
                    {
                        "target": tgt,
                        "task_type": b.get("task_type"),
                        "reason": "claim_race_lost",
                        "claimed_by": holder_agent,
                    }
                )
                continue
            in_flight.add(tgt)
            load[entry["agent"]] = load.get(entry["agent"], 0) + 1
            policy = entry.get("capacity_policy") or ""
            capacity_tag = f"{entry['state']}/{policy}" if policy else entry["state"]
            capability_seed = int.from_bytes(
                hashlib.sha256(
                    f"{tgt}|{b['task_type']}|{b.get('lane')}|{capabilities.CAPABILITY_POLICY_VERSION}".encode()
                ).digest()[:8],
                "big",
            )
            capability_decision = capabilities.capability_routing_decision(
                {
                    "target": tgt,
                    "repository": tgt.split("#", 1)[0],
                    "task_type": b["task_type"],
                    "lane": b.get("lane") or "opener",
                },
                capabilities_by_id=capability_records,
                seed=capability_seed,
            )
            assignments.append(
                {
                    "agent": entry["agent"],
                    "mode": entry["mode"],
                    "target": tgt,
                    "task_type": b["task_type"],
                    "lane": b.get("lane"),
                    "capacity_state": entry["state"],
                    "capacity_policy": policy,
                    "reason": f"{b['task_type']}→{entry['agent']}/{entry['mode']} ({capacity_tag})"
                    f"{(' via ' + entry.get('exploration_mode', 'epsilon-greedy') + ' exploration') if entry.get('exploration') else ''}",
                    "exploration": bool(entry.get("exploration")),
                    "exploration_mode": entry.get("exploration_mode") or "",
                    "selected_profile_id": entry.get("selected_profile_id"),
                    "requested_model": entry.get("requested_model"),
                    "reasoning_effort": entry.get("reasoning_effort"),
                    "permission_mode": entry.get("permission_mode"),
                    "transport": entry.get("transport"),
                    "candidate_profile_ids": entry.get("candidate_profile_ids") or [],
                    "profile_policy_version": entry.get("profile_policy_version"),
                    "profile_assignment_probability": entry.get("profile_assignment_probability"),
                    "profile_rng_seed": entry.get("profile_rng_seed"),
                    "profile_decision": entry.get("profile_decision"),
                    "capability_decision": capability_decision,
                    "eligible_capability_ids": capability_decision["eligible_capability_ids"],
                    "capability_rejection_reasons": capability_decision["rejection_reasons"],
                    "selected_capability_id": capability_decision["selected_capability_id"],
                    "selected_capability_version_id": capability_decision[
                        "selected_capability_version_id"
                    ],
                    "capability_policy_version": capability_decision["policy_version"],
                    "capability_rng_seed": capability_decision["seed"],
                    "capability_assignment_probability": capability_decision["propensity"],
                    "capability_fallback": capability_decision["fallback"],
                    "capability_ids": (
                        [capability_decision["selected_capability_id"]]
                        if capability_decision["selected_capability_id"]
                        else []
                    ),
                    "capability_version_ids": (
                        [capability_decision["selected_capability_version_id"]]
                        if capability_decision["selected_capability_version_id"]
                        else []
                    ),
                }
            )

    if cap_lanes == 0:
        notes.append("no non-shed coding capacity; nothing dispatched")
        for item in code_items + (review_items if not pressure else []):
            row = {
                "target": item.get("target"),
                "task_type": item.get("task_type"),
                "reason": "capacity_rejected",
                "capacity_states": {
                    agent: (meta or {}).get("state", "unknown")
                    for agent, meta in sorted((cap.get("agents") or {}).items())
                },
            }
            capacity_rejections.append(row)
            rejections.append(row)
    else:
        _assign(code_items, allow_warn=True)  # coding first
        if pressure:
            notes.append("scarce seat under pressure → review tasks dropped this tick")
        else:
            _assign(review_items, allow_warn=False)  # review only on idle capacity

    actionable = len(code_items) + (0 if pressure else len(review_items))
    backoff_ticks = 1 if actionable == 0 else 0  # empty backlog → idle backoff hint

    return {
        "generated_at": int(time.time()),
        "dry_run": dry_run,
        "assignments": assignments,
        "lane_cap": cap_lanes,
        "pressure": pressure,
        "backoff_ticks": backoff_ticks,
        "shed": [a for a in cap.get("agents", {}) if _state(cap, a) == "shed"],
        "notes": notes,
        "selected_count": len(backlog),
        "assigned_count": len(assignments),
        "dispatched_count": 0,
        "rejections": rejections,
        "claimed_by": claimed_by,
        "capacity_rejections": capacity_rejections,
        "already_routed": already_routed,
    }


def load_capacity() -> dict:
    try:
        return json.loads(CAPACITY_JSON.read_text())
    except Exception:
        return {"agents": {}}


def load_backlog() -> list[dict]:
    """Backlog is produced by the gh-backed discovery step (decoupled from planning).

    TODO(discovery): wire the opener/closer discovery to write backlog.json:
      [{target, task_type, lane}]. Until then this reads the file if present, else [].
    """
    try:
        data = json.loads(BACKLOG_JSON.read_text())
        return data.get("items", data) if isinstance(data, (list, dict)) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
def _selftest() -> None:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="router-selftest-")
    os.environ["HANDOFF_DIR"] = tmp
    old_exploration_rate = os.environ.get("ORCH_EXPLORATION_RATE")
    old_exploration_mode = os.environ.get("ORCH_EXPLORATION_MODE")
    os.environ["ORCH_EXPLORATION_RATE"] = "0"
    # point claims at the same temp dir
    claims._handoff_dir = lambda: Path(tmp)  # type: ignore
    try:

        def cap(states: dict) -> dict:
            return {"agents": {a: {"state": s} for a, s in states.items()}}

        all_ok = cap(
            {
                "cursor": "ok",
                "vibe": "ok",
                "codex": "ok",
                "claude": "ok",
                "gemini": "ok",
                "aider": "ok",
            }
        )
        bk = [
            {"target": "r/Repo#1", "task_type": "mechanical", "lane": "closer"},
            {"target": "r/Repo#2", "task_type": "implement", "lane": "opener"},
            {"target": "r/Repo#3", "task_type": "review", "lane": "closer"},
        ]
        p = plan(bk, all_ok, dry_run=True)
        by_t = {a["target"]: a for a in p["assignments"]}
        assert by_t["r/Repo#1"]["agent"] == "cursor", by_t[
            "r/Repo#1"
        ]  # mechanical → composer leads (cheap lane)
        assert by_t["r/Repo#1"]["mode"] == "composer"
        assert by_t["r/Repo#2"]["agent"] == "claude", by_t[
            "r/Repo#2"
        ]  # implement → claude leads (hand-set prior)
        assert by_t["r/Repo#3"]["task_type"] == "review"  # review runs (no pressure)

        # learned-weights wiring: a learned rank reorders WITHIN the capacity tier (feedback loop closing)
        assert (
            select_agent("implement", all_ok, learned={"cursor": 0, "claude": 1})["agent"]
            == "cursor"
        ), "learned weights should reorder cursor ahead of claude"
        assert (
            select_agent("implement", all_ok)["agent"] == "claude"
        ), "no learned data => hand-set prior unchanged"
        assert (
            select_agent("testgen", all_ok)["agent"] == "codex"
        ), "testgen lane should not default to claude"
        assert (
            select_agent("epic", all_ok)["agent"] == "gemini"
        ), "epic lane should default to Gemini/AGY, not Claude"
        assert (
            select_agent("cross_repo", all_ok)["agent"] == "gemini"
        ), "cross_repo lane should default to Gemini/AGY"
        assert (
            select_agent("runtime_ac", all_ok)["agent"] == "gemini"
        ), "runtime_ac lane should default to Gemini/AGY"
        assert (
            select_agent("codemod", all_ok)["agent"] == "cursor"
        ), "codemod lane should default to composer"
        assert (
            select_agent("codemod", all_ok)["mode"] == "composer"
        ), "codemod lane should use cursor composer"
        learned_meta = {
            "claude": {"rank": 0, "n_obs": 9},
            "codex": {"rank": 1, "n_obs": 0},
            "gemini": {"rank": 2, "n_obs": 4},
            "cursor": {"rank": 3, "n_obs": 2},
            "vibe": {"rank": 4, "n_obs": 1},
            "aider": {"rank": 5, "n_obs": 0},
        }
        explore = select_agent(
            "implement", all_ok, learned=learned_meta, exploration_rate=1.0, rng=random.Random(7)
        )
        assert explore["exploration"] is True, explore
        assert explore["exploration_mode"] == "epsilon-greedy", explore
        assert explore["agent"] == "codex", explore
        assert (
            explore["agent"] != "aider"
        ), "exploration must not jump to a late/paygo tier while non-late is available"
        epsilon_explore = select_agent(
            "implement",
            all_ok,
            learned=learned_meta,
            exploration_rate=1.0,
            exploration_mode="epsilon-greedy",
            rng=random.Random(7),
        )
        assert (
            epsilon_explore["agent"] == "codex" and epsilon_explore["exploration"] is True
        ), epsilon_explore
        assert epsilon_explore["exploration_mode"] == "epsilon-greedy", epsilon_explore
        assert _exploration_mode("thompson") == "thompson-hybrid"
        assert _exploration_mode("invalid") == "epsilon-greedy"
        assert (
            _learned_posterior_score(
                {"edge": {"posterior": 1.0, "score": 1.0, "n_obs": 2}}, "edge", 0.5
            )[0]
            == 0.999
        )
        edge_row = ((0, 0, 0, 0, 0), {"agent": "edge"}, "ok", 0)
        edge_sample = _thompson_sample(
            edge_row,
            {"edge": {"posterior": 1.0, "score": 1.0, "n_obs": 2}},
            random.Random(0),
            total_candidates=1,
        )
        assert edge_sample >= 0.0, edge_sample
        thompson_learned = {
            "claude": {"rank": 0, "n_obs": 20, "posterior": 0.80, "score": 0.80},
            "codex": {"rank": 1, "n_obs": 1, "posterior": 0.78, "score": 0.78},
            "gemini": {"rank": 2, "n_obs": 1, "posterior": 0.40, "score": 0.40},
            "cursor": {"rank": 3, "n_obs": 1, "posterior": 0.35, "score": 0.35},
            "vibe": {"rank": 4, "n_obs": 1, "posterior": 0.30, "score": 0.30},
            "aider": {"rank": 5, "n_obs": 0, "posterior": 0.95, "score": 0.95},
        }
        th = select_agent(
            "implement",
            all_ok,
            learned=thompson_learned,
            exploration_rate=1.0,
            exploration_mode="thompson-hybrid",
            rng=random.Random(11),
        )
        assert th["exploration"] is True and th["exploration_mode"] == "thompson-hybrid", th
        assert th["agent"] in {"codex", "gemini", "cursor", "vibe"}, th
        assert (
            th["agent"] != "aider"
        ), "Thompson hybrid must stay in the winner's non-late policy tier"

        # gate #2: select_remote_agent restricts to keepalive lanes AND reserves claude (weekly-cap scarce)
        all_keep = cap({"cursor": "ok", "codex": "ok", "claude": "ok", "gemini": "ok"})
        assert (
            select_remote_agent("testgen", all_keep)["agent"] == "codex"
        ), "remote testgen uses non-reserve lane"
        assert (
            select_remote_agent("epic", all_keep)["agent"] == "gemini"
        ), "remote epic uses Gemini/AGY, not Claude"
        assert (
            select_remote_agent("cross_repo", all_keep)["agent"] == "gemini"
        ), "remote cross_repo uses Gemini/AGY"
        assert (
            select_remote_agent("runtime_ac", all_keep)["agent"] == "gemini"
        ), "remote runtime_ac uses Gemini/AGY"
        assert (
            select_remote_agent("codemod", all_keep)["agent"] == "cursor"
        ), "remote codemod uses cheap cursor, not Claude"
        rr = select_remote_agent("implement", all_keep)
        assert rr["agent"] in (KEEPALIVE_AGENTS - RESERVE_AGENTS), rr  # routine: NOT claude
        assert rr["agent"] == "codex", rr  # codex leads the non-reserve implement pool
        assert (
            select_remote_agent("implement", all_keep, high_leverage=True)["agent"] == "claude"
        ), "high-leverage allows claude"
        claude_only = cap({"claude": "ok", "cursor": "shed", "codex": "shed", "gemini": "shed"})
        assert (
            select_remote_agent("implement", claude_only)["agent"] == "claude"
        ), "last-resort: claude when nothing cheaper"
        vibe_only = cap(
            {"vibe": "ok", "cursor": "shed", "codex": "shed", "claude": "shed", "gemini": "shed"}
        )
        assert (
            select_remote_agent("implement", vibe_only) is None
        ), "vibe is local-only, not keepalive-runnable"
        assert (
            select_remote_agent("implement", all_keep, learned={"gemini": 0})["agent"] == "gemini"
        ), "learned weights reorder within the non-reserve pool"

        # sequencing: claude shed → implement falls to codex
        p2 = plan(
            [bk[1]],
            cap({"claude": "shed", "codex": "ok", "cursor": "ok", "vibe": "ok"}),
            dry_run=True,
        )
        assert p2["assignments"][0]["agent"] == "codex", p2["assignments"]
        profile_assignment = p2["assignments"][0]
        assert profile_assignment["selected_profile_id"] in execution_profiles.PROFILE_REGISTRY
        replayed = replay_profile_choice(profile_assignment)
        assert (
            replayed
            and replayed["selected_profile_id"] == profile_assignment["selected_profile_id"]
        )
        assert replayed["rng_seed"] == profile_assignment["profile_rng_seed"]
        assert replayed["policy_version"] == profile_assignment["profile_policy_version"]

        # mechanical with composer+vibe shed → codex (cheap) BEFORE aider (paygo/late)
        p3 = plan(
            [bk[0]],
            cap({"cursor": "shed", "vibe": "shed", "codex": "ok", "aider": "ok"}),
            dry_run=True,
        )
        assert (
            p3["assignments"][0]["agent"] == "codex" and p3["assignments"][0]["mode"] == "cheap"
        ), p3["assignments"]

        # backup-only (owner 2026-06-21): everything non-late shed → aider is NOT auto-selected; it is
        # held as explicit backup capacity, reachable only via `only={"aider"}`, never routine routing.
        cap_all_shed = cap(
            {"cursor": "shed", "vibe": "shed", "codex": "shed", "claude": "shed", "aider": "ok"}
        )
        p3b = plan([bk[0]], cap_all_shed, dry_run=True)
        assert not p3b["assignments"], (
            "aider must not auto-fill as last resort",
            p3b["assignments"],
        )
        assert (
            select_agent("mechanical", cap_all_shed, only={"aider"})["agent"] == "aider"
        ), "aider stays reachable as explicit backup capacity"

        # priority: scarce seat (codex) warn → review dropped, code still runs
        p4 = plan(
            bk,
            cap({"cursor": "ok", "vibe": "ok", "codex": "warn", "claude": "ok", "aider": "ok"}),
            dry_run=True,
        )
        assert p4["pressure"] is True
        assert all(a["task_type"] != "review" for a in p4["assignments"]), p4["assignments"]
        assert any(a["task_type"] == "mechanical" for a in p4["assignments"])

        # review never pushes a NON-scarce agent into warn: composer(cursor)=warn is skipped
        # (allow_warn=False); vibe shed; codex/claude idle (no pressure) → review falls to codex.
        # (codex=warn here would instead trigger the pressure rule and drop review — covered by p4.)
        p4b = plan(
            [bk[2]],
            cap({"cursor": "warn", "vibe": "shed", "codex": "ok", "claude": "ok"}),
            dry_run=True,
        )
        assert p4b["assignments"] and p4b["assignments"][0]["agent"] == "codex", p4b["assignments"]

        # load-spread: 4 implement tasks must FAN OUT across ranked agents, not dogpile
        # claude (the integration-test bug). With all ok: claude, codex, gemini, cursor/vibe.
        four_impl = [
            {"target": f"i/Repo#{i}", "task_type": "implement", "lane": "opener"} for i in range(4)
        ]
        ps = plan(four_impl, all_ok, dry_run=True)
        agents_used = [a["agent"] for a in ps["assignments"]]
        assert (
            len(set(agents_used)) == 4
        ), f"4 implement tasks must spread to 4 agents, got {agents_used}"
        assert (
            agents_used[0] == "claude"
        ), agents_used  # highest-priority task still gets the best agent (hand-set prior)
        # cap also prevents dogpiling: 2 implement tasks but only claude up -> lane_cap=1, so
        # ONE runs this tick (claude), the other waits — no two concurrent claude rounds.
        ps2 = plan(
            four_impl[:2],
            cap(
                {"claude": "ok", "codex": "shed", "cursor": "shed", "vibe": "shed", "aider": "shed"}
            ),
            dry_run=True,
        )
        assert [a["agent"] for a in ps2["assignments"]] == ["claude"] and ps2["lane_cap"] == 1, ps2[
            "assignments"
        ]

        # concurrency: real claims prevent double-assigning an in-flight target
        claims.claim("r/Repo#1", "someone")
        p5 = plan(bk, all_ok, dry_run=False)
        assert all(
            a["target"] != "r/Repo#1" for a in p5["assignments"]
        ), "claimed target must be skipped"
        assert any(a["target"] == "r/Repo#2" for a in p5["assignments"])
        assert p5["claimed_by"]["r/Repo#1"] == "someone", p5
        assert any(
            row["target"] == "r/Repo#1" and row["reason"] == "claimed" for row in p5["rejections"]
        ), p5

        duplicate_item = {
            "target": "r/Repo#duplicate",
            "task_type": "implement",
            "lane": "opener",
        }
        duplicate = plan([duplicate_item, dict(duplicate_item)], all_ok, dry_run=True)
        assert duplicate["assigned_count"] == 1, duplicate
        assert duplicate["already_routed"] == [
            {
                "target": "r/Repo#duplicate",
                "task_type": "implement",
                "reason": "already_routed",
            }
        ], duplicate

        # lane cap bounds concurrency
        many = [
            {"target": f"r/Repo#{i}", "task_type": "mechanical", "lane": "closer"}
            for i in range(20)
        ]
        claims.reap_stale()
        for t in list(claims.active_claims()):
            claims.release(t)
        p6 = plan(many, all_ok, max_concurrent=3, dry_run=False)
        assert (
            len(p6["assignments"]) == 3
        ), f"lane cap should bound to 3, got {len(p6['assignments'])}"

        # gemini (Antigravity) is routable when ok; existing tests leave it unset (=>unknown=>skipped),
        # so it only participates when capacity explicitly says ok — here it's the only one available.
        gem = cap(
            {
                "cursor": "shed",
                "vibe": "shed",
                "codex": "shed",
                "claude": "shed",
                "gemini": "ok",
                "aider": "shed",
            }
        )
        pg = plan(
            [{"target": "g/r#1", "task_type": "implement", "lane": "opener"}], gem, dry_run=True
        )
        assert pg["assignments"] and pg["assignments"][0]["agent"] == "gemini", pg["assignments"]

        # Gemini Policy routing tests
        # 1. Promotion during drain: gemini is chosen ahead of claude/codex
        cap_drain = {
            "agents": {
                "claude": {"state": "ok"},
                "codex": {"state": "ok"},
                "gemini": {"state": "ok", "policy": "drain"},
                "cursor": {"state": "ok"},
                "vibe": {"state": "ok"},
                "aider": {"state": "ok"},
            }
        }
        pg_prom = plan(
            [{"target": "g/r#1", "task_type": "implement", "lane": "opener"}],
            cap_drain,
            dry_run=True,
        )
        assert pg_prom["assignments"] and pg_prom["assignments"][0]["agent"] == "gemini", pg_prom[
            "assignments"
        ]
        assert pg_prom["assignments"][0]["capacity_policy"] == "drain", pg_prom["assignments"][0]
        assert (
            select_agent("testgen", cap_drain)["agent"] == "gemini"
        ), "drain should spend AGY on good-fit testgen"
        assert (
            select_agent("review", cap_drain)["agent"] == "gemini"
        ), "drain should spend AGY on reasoning review"

        # 2. Demotion during reserve: gemini is deferred behind non-late agents
        cap_reserve = {
            "agents": {
                "claude": {"state": "shed"},
                "codex": {"state": "shed"},
                "gemini": {"state": "ok", "policy": "reserve"},
                "cursor": {"state": "ok"},
                "vibe": {"state": "ok"},
                "aider": {"state": "ok"},
            }
        }
        pg_dem = plan(
            [{"target": "g/r#1", "task_type": "implement", "lane": "opener"}],
            cap_reserve,
            dry_run=True,
        )
        assert pg_dem["assignments"] and pg_dem["assignments"][0]["agent"] in (
            "cursor",
            "vibe",
        ), pg_dem["assignments"]

        # 3. Reserve remains a fallback before paygo/late capacity for good-fit work.
        cap_reserve_late = {
            "agents": {
                "claude": {"state": "shed"},
                "codex": {"state": "shed"},
                "gemini": {"state": "ok", "policy": "reserve"},
                "cursor": {"state": "ok"},
                "vibe": {"state": "shed"},
                "aider": {"state": "ok"},
            }
        }
        pick_last = select_agent("implement", cap_reserve_late, load={"cursor": 1})
        assert (
            pick_last["agent"] == "gemini" and pick_last["capacity_policy"] == "reserve"
        ), pick_last

        # empty backlog → backoff hint, no assignments
        p7 = plan([], all_ok, dry_run=True)
        assert p7["assignments"] == [] and p7["backoff_ticks"] == 1, p7

        # all shed → nothing dispatched
        p8 = plan(
            bk,
            cap(
                {
                    "cursor": "shed",
                    "vibe": "shed",
                    "codex": "shed",
                    "claude": "shed",
                    "aider": "shed",
                }
            ),
            dry_run=True,
        )
        assert p8["assignments"] == [] and p8["lane_cap"] == 0, p8
        assert p8["selected_count"] == len(bk) and p8["assigned_count"] == 0, p8

        # 16(e) continuous drain urgency: unused expiring quota near refresh reads as cheaper;
        # fresh windows and field-less (flat) seats contribute 0 — ranking unchanged for them.
        urgent = {
            "agents": {
                "gemini": {
                    "state": "ok",
                    "soft_units_5h": 8.0,
                    "estimated_units_5h": 0.0,
                    "minutes_to_window_refresh": 30,
                }
            }
        }
        fresh = {
            "agents": {
                "gemini": {
                    "state": "ok",
                    "soft_units_5h": 8.0,
                    "estimated_units_5h": 0.0,
                    "minutes_to_window_refresh": 290,
                }
            }
        }
        spent = {
            "agents": {
                "gemini": {
                    "state": "ok",
                    "soft_units_5h": 8.0,
                    "estimated_units_5h": 8.0,
                    "minutes_to_window_refresh": 30,
                }
            }
        }
        assert _drain_urgency("gemini", urgent) < _drain_urgency("gemini", fresh) <= 0, (
            _drain_urgency("gemini", urgent),
            _drain_urgency("gemini", fresh),
        )
        assert _drain_urgency("gemini", spent) == 0.0
        assert _drain_urgency("cursor", urgent) == 0.0, "field-less seats unaffected"

        print(
            "router.py selftest: OK (route-table prior, capacity sequencing, code>review "
            "priority, ε/Thompson-hybrid exploration, only-if-idle review, claims-gated concurrency, "
            "lane cap, idle backoff)"
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("HANDOFF_DIR", None)
        if old_exploration_rate is None:
            os.environ.pop("ORCH_EXPLORATION_RATE", None)
        else:
            os.environ["ORCH_EXPLORATION_RATE"] = old_exploration_rate
        if old_exploration_mode is None:
            os.environ.pop("ORCH_EXPLORATION_MODE", None)
        else:
            os.environ["ORCH_EXPLORATION_MODE"] = old_exploration_mode


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    dry = "--dry-run" in argv
    cap = load_capacity()
    backlog = load_backlog()
    try:
        mx = int(os.environ.get("ORCH_MAX_CONCURRENT", "") or MAX_CONCURRENT_DEFAULT)
    except ValueError:
        mx = MAX_CONCURRENT_DEFAULT
    decision = plan(
        backlog, cap, max_concurrent=mx, dry_run=dry, learned=learned_ranks()
    )  # learning reorders within tiers; ORCH_MAX_CONCURRENT=1 throttles the first supervised tick
    if dry:
        print(json.dumps(decision, indent=2))
    else:
        HANDOFF.mkdir(parents=True, exist_ok=True)
        DECISION_JSON.write_text(json.dumps(decision, indent=2) + "\n")
        print(json.dumps(decision, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
