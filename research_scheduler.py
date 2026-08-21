#!/usr/bin/env python3
"""research_scheduler.py — the orchestrator's capacity-aware research arm.

Runs the orchestrator's OWN science (comparative-advantage experiments, evaluations, adversarial
reviews, feature-hardening) ONLY when spare capacity exists, prioritizing jobs by information gain
per unit capacity. Design + grounding: EVAL_AND_TESTING.md. Feeds feedback.py (the dataset).

Four responsibilities, each pure + selftested here:
  1. spare_capacity()  — never-always-on gate: who has headroom to spend on science right now.
  2. select_jobs()     — budgeted acquisition (knapsack by info/cost): WHAT to run + at WHAT intensity.
  3. select_arms()     — Top-Two variable-N (2-5) random assignment: WHICH agents in an experiment.
  4. build_research_plan() — live capacity/backlog/hypotheses -> dispatchable exp_abcd plan.
Plus a hypothesis registry (the queue of comparative-advantage questions, seeded with established ideas).
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

import capacity
import feedback
import research_subjects

ORCH = Path(__file__).resolve().parent
HYP_PATH = Path(
    os.environ.get("ORCH_HYP_PATH", ORCH / "experiments" / "hypotheses.json")
)

# Spare-capacity budget per tier when a seat is `ok` (use-it-or-lose-it: free/flat are cheap to spend on
# science; metered reasoning is spent cautiously; paygo not at all by default). Scaled down on `warn`.
TIER_SPARE = {"free": 3, "flat": 3, "metered": 1, "paygo": 0}
DATA_HUNGER_THRESHOLD = feedback.PRIOR_STRENGTH
STALE_DAYS = float(os.environ.get("ORCH_STALE_DAYS", "30"))
DEFAULT_AGENT_TIERS = capacity.agent_tiers()
TASK_STAKES = {
    "runtime_ac": 1.35,
    "cross_repo": 1.25,
    "epic": 1.15,
    "implement": 1.10,
    "testgen": 1.00,
    "codemod": 0.90,
    "mechanical": 0.70,
    "polish": 0.60,
    "review": 0.55,
}
LANE_STAKES = {"opener": 1.05, "closer": 1.00}


# An ARM is either a single agent (str) or an assignment STRATEGY (dict) — so the system can learn that
# running MULTIPLE agents, despite the up-front inefficiency, sometimes yields the better outcome (the
# synthesize phase makes that payoff real). The strategy's value = synthesized-best quality vs its TOTAL
# cost (sum of arms) vs the single-agent baseline. Nothing here precludes "multi-agent wins" as a finding.
def describe_arm(arm) -> str:
    if isinstance(arm, str):
        return arm
    kind = arm.get("strategy", "single")
    agents = "+".join(arm.get("agents", []))
    return f"{kind}({agents}{'+synth' if arm.get('synthesize') else ''})"


def arm_agents(arm) -> list:
    return [arm] if isinstance(arm, str) else list(arm.get("agents", []))


def _unique_agents(arms_or_agents: list) -> list[str]:
    out: list[str] = []
    for arm in arms_or_agents:
        for agent in arm_agents(arm):
            if agent not in out:
                out.append(agent)
    return out


def route_n_obs(
    task_type: str, agents: list, *, conn=None, weights: list[dict] | None = None
) -> dict[str, int]:
    """Latest route_weights n_obs for the requested task/agents. Missing cells count as 0.

    `conn` and `weights` keep this pure/injectable for tests; live callers default to feedback.current_weights().
    """
    names = _unique_agents(agents)
    out = {a: 0 for a in names}
    if not names:
        return out
    rows = weights
    if rows is None and conn is not None:
        placeholders = ",".join("?" for _ in names)
        try:
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM route_weights"
            ).fetchone()[0]
            rows = [
                {"agent": a, "n_obs": n}
                for a, n in conn.execute(
                    f"SELECT agent, n_obs FROM route_weights "
                    f"WHERE version=? AND task_type=? AND agent IN ({placeholders})",
                    [version, task_type, *names],
                ).fetchall()
            ]
        except Exception:
            rows = []
    if rows is None:
        try:
            rows = feedback.current_weights(task_type)
        except Exception:
            rows = []
    for row in rows or []:
        agent = row.get("agent")
        if agent in out:
            try:
                out[agent] = max(0, int(row.get("n_obs") or 0))
            except (TypeError, ValueError):
                out[agent] = 0
    return out


def data_hunger(
    task_type: str,
    agents: list,
    *,
    conn=None,
    weights: list[dict] | None = None,
    prior_strength: float = DATA_HUNGER_THRESHOLD,
) -> float:
    """Return 0..1 pressure to gather data for under-observed (task_type, agent) cells.

    Hunger is high when any eligible cell is below the prior-strength pseudo-count and tapers to zero
    once every relevant cell has at least that many observations.
    """
    n_obs = route_n_obs(task_type, agents, conn=conn, weights=weights)
    if not n_obs:
        return 0.0
    k = max(1.0, float(prior_strength))
    deficits = [max(0.0, k - float(n)) / k for n in n_obs.values()]
    return max(0.0, min(1.0, max(deficits)))


def _feedback_conn(conn):
    if conn is not None:
        return conn, False
    try:
        return feedback._conn(), True
    except Exception:
        return None, False


def staleness_days(
    task_type: str, agent: str, *, conn=None, now: int | None = None
) -> float:
    """Days since the latest eval or outcome observation for this (task_type, agent) cell."""
    db, close = _feedback_conn(conn)
    if db is None:
        return float("inf")
    try:
        eval_ts = db.execute(
            "SELECT MAX(e.ts) FROM runs r JOIN evaluations e "
            "ON r.experiment_id=e.experiment_id AND r.agent=e.implementer "
            "WHERE r.task_type=? AND r.agent=?",
            (task_type, agent),
        ).fetchone()[0]
        outcome_ts = db.execute(
            "SELECT MAX(COALESCE(o.durability_checked_ts, r.ts)) "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.task_type=? AND r.agent=?",
            (task_type, agent),
        ).fetchone()[0]
    except Exception:
        return float("inf")
    finally:
        if close:
            db.close()
    last = max([ts for ts in (eval_ts, outcome_ts) if ts is not None], default=None)
    if last is None:
        return float("inf")
    return max(0.0, float((now or int(time.time())) - int(last)) / 86400.0)


def staleness_hunger(
    task_type: str | None = None,
    agent: str | None = None,
    *,
    conn=None,
    stale_days: float = STALE_DAYS,
    now: int | None = None,
    days: float | None = None,
) -> float:
    """0..1 pressure to re-test a cell after its last observation ages past `stale_days`."""
    if days is None:
        if not task_type or not agent:
            return 0.0
        days = staleness_days(task_type, agent, conn=conn, now=now)
    if days == float("inf"):
        return 1.0
    stale = max(1.0, float(stale_days))
    if days <= stale:
        return 0.0
    return max(0.0, min(1.0, (float(days) - stale) / stale))


def model_drifted(task_type: str, agent: str, *, conn=None) -> bool:
    """True when current worker provenance succeeds the latest evidence provenance.

    Evaluator/verifier/replay models and legacy ``runs.model`` tags are intentionally
    excluded. A profile-label change alone is not successor evidence: only a changed
    provider-resolved model identity triggers drift. Unknown identity is conservative.
    """
    db, close = _feedback_conn(conn)
    if db is None:
        return False
    try:
        current = feedback.latest_worker_identity_for_agent(agent, conn=db)
        evidence = feedback.latest_worker_identity_for_agent(
            agent, task_type=task_type, evidence_only=True, conn=db
        )
    except Exception:
        return False
    finally:
        if close:
            db.close()
    if not current or not evidence:
        return False
    current_pair = (current.get("resolved_provider"), current.get("resolved_model"))
    evidence_pair = (
        evidence.get("resolved_provider"),
        evidence.get("resolved_model"),
    )
    return bool(current_pair[1] and evidence_pair[1] and current_pair != evidence_pair)


def acquisition_hunger(
    task_type: str,
    agents: list,
    *,
    conn=None,
    weights: list[dict] | None = None,
    prior_strength: float = DATA_HUNGER_THRESHOLD,
) -> float:
    """Combined 0..1 acquisition pressure from n_obs deficit, staleness, and model drift."""
    if not agents:
        return 0.0
    data = data_hunger(
        task_type, agents, conn=conn, weights=weights, prior_strength=prior_strength
    )
    stale = max(
        (staleness_hunger(task_type, a, conn=conn) for a in _unique_agents(agents)),
        default=0.0,
    )
    drift = (
        1.0
        if any(model_drifted(task_type, a, conn=conn) for a in _unique_agents(agents))
        else 0.0
    )
    return max(data, stale, drift)


def _agent_acquisition_scores(
    task_type: str,
    agents: list,
    *,
    conn=None,
    weights: list[dict] | None = None,
    prior_strength: float = DATA_HUNGER_THRESHOLD,
    n_obs: dict[str, int] | None = None,
) -> dict[str, float]:
    names = _unique_agents(agents)
    observed = n_obs or route_n_obs(task_type, names, conn=conn, weights=weights)
    k = max(1.0, float(prior_strength))
    scores = {}
    for agent in names:
        try:
            data = max(0.0, k - float(observed.get(agent, 0))) / k
        except (TypeError, ValueError):
            data = 1.0
        scores[agent] = max(
            max(0.0, min(1.0, data)),
            staleness_hunger(task_type, agent, conn=conn),
            1.0 if model_drifted(task_type, agent, conn=conn) else 0.0,
        )
    return scores


# Seeded hypotheses — established comparative-advantage ideas to TEST (your examples), incl. run-1 finding
# and the MULTI-AGENT-value questions you raised (H4/H5: when is paying for parallel agents worth it?).
SEED_HYPOTHESES = [
    {
        "id": "H1",
        "claim": "cursor composer >= premium seats on well-specified integration implements",
        "task_type": "implement",
        "conditions": "tightly-specified, bounded",
        "arms": ["cursor", "codex", "claude"],
        "evidence": {"n": 1, "posterior": 0.60, "status": "accumulating"},
    },
    {
        "id": "H2",
        "claim": "a cheap codex/gpt-mini model is cost-efficient on mechanical codemods",
        "task_type": "mechanical",
        "conditions": "format/lint/dep/codemod",
        "arms": ["codex", "cursor", "vibe"],
        "evidence": {"n": 0, "posterior": 0.50, "status": "open"},
    },
    {
        "id": "H3",
        "claim": "gemini's large context wins on whole-file comprehension/refactor",
        "task_type": "implement",
        "conditions": "very large single file",
        "arms": ["gemini", "claude", "codex"],
        "evidence": {"n": 0, "posterior": 0.50, "status": "open"},
    },
    {
        "id": "H4",
        "claim": "a high-cost + low-cost pair (+synthesis) beats a single high-cost agent often "
        "enough to justify the extra spend on harder implements",
        "task_type": "implement",
        "conditions": "ambiguous / higher-stakes",
        "arms": [
            {"strategy": "single", "agents": ["claude"]},
            {
                "strategy": "parallel",
                "agents": ["claude", "cursor"],
                "synthesize": True,
            },
        ],
        "evidence": {"n": 0, "posterior": 0.50, "status": "open"},
    },
    {
        "id": "H5",
        "claim": "two HIGH-cost agents in parallel rarely beat one high + one low (diversity > "
        "raw horsepower) — i.e. mixed-tier parallelism is the cost-efficient multi-agent shape",
        "task_type": "implement",
        "conditions": "when multi-agent is warranted at all",
        "arms": [
            {"strategy": "parallel", "agents": ["claude", "codex"], "synthesize": True},
            {
                "strategy": "parallel",
                "agents": ["claude", "cursor"],
                "synthesize": True,
            },
        ],
        "evidence": {"n": 0, "posterior": 0.50, "status": "open"},
    },
]


def load_hypotheses(path: Path = HYP_PATH) -> list:
    if path.exists():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(SEED_HYPOTHESES, indent=2))
    return list(SEED_HYPOTHESES)


def save_hypotheses(hyps: list, path: Path = HYP_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hyps, indent=2))


# --- 1. WHEN: spare-capacity gate -------------------------------------------
def spare_capacity(capacity_state: dict) -> dict:
    """capacity_state: {agent: {"status": ok|warn|shed, "tier": free|flat|metered|paygo}}.
    Returns {agent: spare_units} — 0 means 'don't spend this seat on science now'. The engine does
    NOTHING when this sums to 0 (production routing untouched)."""
    out = {}
    for agent, st in capacity_state.items():
        base = TIER_SPARE.get(st.get("tier", "metered"), 0)
        status = st.get("status", "ok")
        out[agent] = base if status == "ok" else (base // 2 if status == "warn" else 0)
    return out


# --- 2. WHAT + INTENSITY: budgeted acquisition (knapsack by info/cost) -------
def info_value(uncertainty: float, stakes: float, staleness: float) -> float:
    """Acquisition value: test where we're least sure (uncertainty), on what matters (stakes), and what
    we haven't measured lately (staleness). Uncertainty-based acquisition (active learning).
    """
    return max(0.0, uncertainty) * max(0.0, stakes) * max(0.1, staleness)


def select_jobs(jobs: list, budget: int) -> list:
    """Greedy knapsack by info/cost ratio under the capacity budget -> variable intensity: abundant
    budget runs the intense jobs, scarce budget runs only the cheapest-highest-value spot-check.
    job: {id, info_value, capacity_cost}. Returns the chosen jobs (order = priority)."""
    ranked = sorted(
        jobs, key=lambda j: j["info_value"] / max(1, j["capacity_cost"]), reverse=True
    )
    chosen, spent = [], 0
    for j in ranked:
        if spent + j["capacity_cost"] <= budget:
            chosen.append(j)
            spent += j["capacity_cost"]
    return chosen


# --- 3. WHICH agents: Top-Two variable-N random assignment ------------------
def select_arms(
    plausible: list,
    posteriors: dict,
    n_units: dict,
    *,
    rng: random.Random | None = None,
    max_n: int = 5,
    task_type: str | None = None,
    conn=None,
    weights: list[dict] | None = None,
    n_obs: dict[str, int] | None = None,
    hunger: float | None = None,
    hunger_scores: dict[str, float] | None = None,
) -> list:
    """Choose 2-5 agents for an experiment. Top-Two seeding (best-arm identification, not reward-max):
    always the LEADER (max posterior) + the CHALLENGER (highest uncertainty among the rest); then add
    RANDOM extras up to N, where N grows with available spare capacity. Randomization defeats the
    self-confirming blind spot; Top-Two seeding keeps it efficient (~35% fewer trials than vanilla TS).
    """
    rng = rng or random.Random()
    plausible = [
        a for a in plausible if n_units.get(a, 0) > 0
    ]  # only seats with spare capacity
    if len(plausible) < 2:
        return plausible
    observed = n_obs or {}
    if not observed and task_type:
        observed = route_n_obs(task_type, plausible, conn=conn, weights=weights)
    if hunger_scores is None and task_type:
        hunger_scores = _agent_acquisition_scores(
            task_type, plausible, conn=conn, weights=weights, n_obs=observed
        )
    hunger_scores = hunger_scores or {}
    if hunger is None and task_type:
        hunger = max(hunger_scores.values(), default=0.0)
    else:
        hunger = hunger or 0.0
    leader = max(plausible, key=lambda a: posteriors.get(a, 0.5))
    rest = [a for a in plausible if a != leader]
    # challenger = the most UNCERTAIN of the rest: posterior nearest 0.5 (least-resolved comparative claim)
    challenger = min(rest, key=lambda a: abs(posteriors.get(a, 0.5) - 0.5))
    arms = [leader, challenger]
    extras = [a for a in rest if a != challenger]
    rng.shuffle(extras)
    # N scales with total spare units available across plausible seats (capacity-aware intensity)
    total_spare = sum(n_units.get(a, 0) for a in plausible)
    base_target = 2 + total_spare // 3  # 2 base + 1 per ~3 spare units
    hunger_boost = int(hunger * max(0, len(plausible) - base_target))
    target_n = min(max_n, len(plausible), base_target + hunger_boost)
    if hunger > 0:
        # While acquisition pressure is hot, spend extras on least-fresh cells first: under-observed,
        # stale, or model-drifted cells all surface through the same score.
        extras.sort(
            key=lambda a: (-hunger_scores.get(a, 0.0), observed.get(a, 0), rng.random())
        )
    for a in extras:
        if len(arms) >= target_n:
            break
        arms.append(a)
    return arms


def should_test(
    task: dict, hyps: list, spare: dict, *, conn=None, weights: list[dict] | None = None
) -> dict | None:
    """A rough cost/benefit RECOMMENDATION (not a mandate) on whether to make this task an experiment.
    The orchestrator EXERCISES this judgment and may override it — and the system is strongly biased to
    UPDATE the heuristic from whether past tests of this kind proved informative (changed routing /
    resolved a hypothesis). Early, with rough priors, uncertainty is high so it tests more; as hypotheses
    resolve it tests less — judgment built by exercising it (EVAL_AND_TESTING.md B0/A1). Capacity-gated.
    Arms may be single agents OR multi-agent strategies (H4/H5), so 'multi-agent wins' stays learnable.
    The autonomous exp_abcd launcher currently auto-fires only simple single-agent arm sets; strategy arms
    are surfaced as skipped diagnostics until a strategy-aware experiment runner is added.
    """
    if sum(spare.values()) == 0:
        return None
    task_type = task.get("task_type")
    matches = [
        h
        for h in hyps
        if h["task_type"] == task.get("task_type")
        and h["evidence"]["status"] in ("open", "accumulating")
    ]
    if matches:
        h = matches[0]
        runnable = [
            a for a in h["arms"] if all(spare.get(g, 0) > 0 for g in arm_agents(a))
        ]
        if len(runnable) >= 2:
            dh = data_hunger(task_type, runnable, conn=conn, weights=weights)
            hunger = acquisition_hunger(task_type, runnable, conn=conn, weights=weights)
            return {
                "trigger": "hypothesis",
                "hypothesis": h["id"],
                "arms": runnable,
                "kind": (
                    "strategy"
                    if any(not isinstance(a, str) for a in runnable)
                    else "agent"
                ),
                "data_hunger": dh,
                "acquisition_hunger": hunger,
            }
    # opportunistic: lots of free seats sitting idle -> spend them on a comparison (free information)
    free_seats = [a for a, u in spare.items() if u > 0]
    dh = data_hunger(task_type or "implement", free_seats, conn=conn, weights=weights)
    hunger = acquisition_hunger(
        task_type or "implement", free_seats, conn=conn, weights=weights
    )
    min_seats = 2 if hunger >= 0.75 else 3
    min_spare = 2 if hunger >= 0.75 else 6
    if len(free_seats) >= min_seats and sum(spare.values()) >= min_spare:
        return {
            "trigger": "opportunistic",
            "hypothesis": None,
            "arms": free_seats,
            "kind": "agent",
            "data_hunger": dh,
            "acquisition_hunger": hunger,
        }
    return None


def capacity_state_from_snapshot(
    capacity_snapshot: dict, *, tiers: dict[str, str] | None = None
) -> dict:
    """Adapt capacity.py's live JSON shape into spare_capacity()'s small pure input shape."""
    tiers = tiers or DEFAULT_AGENT_TIERS
    agents = (
        capacity_snapshot.get("agents", {})
        if isinstance(capacity_snapshot, dict)
        else {}
    )
    out = {}
    for agent, meta in agents.items():
        meta = meta or {}
        out[agent] = {
            "status": meta.get("state", meta.get("status", "unknown")),
            "tier": tiers.get(agent, "metered"),
        }
    return out


def learned_posteriors(
    task_type: str, agents: list[str], learned: dict | None = None
) -> dict[str, float]:
    """Extract learned posterior probabilities for a task/agent slice, defaulting to an uninformative 0.5."""
    rows = (learned or {}).get(task_type) if learned else None
    out = {}
    for agent in agents:
        row = (rows or {}).get(agent) if isinstance(rows, dict) else None
        try:
            out[agent] = (
                float(row.get("posterior"))
                if isinstance(row, dict) and row.get("posterior") is not None
                else 0.5
            )
        except (TypeError, ValueError):
            out[agent] = 0.5
    return out


def _hypothesis_by_id(hyps: list[dict]) -> dict[str, dict]:
    return {str(h.get("id")): h for h in hyps if h.get("id")}


def _hypothesis_uncertainty(hyp: dict | None) -> float:
    if not hyp:
        return 0.35
    try:
        posterior = float((hyp.get("evidence") or {}).get("posterior", 0.5))
    except (TypeError, ValueError):
        posterior = 0.5
    return max(0.05, min(1.0, 1.0 - 2.0 * abs(posterior - 0.5)))


def _item_stakes(item: dict) -> float:
    task_type = item.get("task_type", "implement")
    lane = item.get("lane")
    return TASK_STAKES.get(task_type, 0.80) * LANE_STAKES.get(lane, 1.0)


def _is_launchable_arm_set(arms: list) -> bool:
    # exp_abcd.prepare currently launches one implementation worktree per agent. Strategy arms need a
    # synthesis-aware runner before they can be auto-fired; keep them visible but non-launchable here.
    return len(arms) >= 2 and all(isinstance(a, str) for a in arms)


def _strategy_experiment_command_template(hypothesis: str | None) -> list[str]:
    cmd = ["python3", str(ORCH / "strategy_experiment.py")]
    if hypothesis:
        cmd.extend(["--hypothesis", str(hypothesis)])
    else:
        cmd.extend(["--arms-json", "<arms-json>"])
    cmd.extend(
        [
            "--repo",
            "<owner/repo>",
            "--spec-file",
            "<spec.md>",
            "--exp-id",
            "<exp_id>",
            "--json",
        ]
    )
    return cmd


def research_job_candidates(
    items: list[dict],
    spare: dict,
    hyps: list[dict],
    *,
    learned: dict | None = None,
    conn=None,
    weights: list[dict] | None = None,
    claimed_targets: set[str] | None = None,
    excluded_targets: set[str] | None = None,
    unevaluated_cap: int = research_subjects.DEFAULT_UNEVALUATED_CAP,
    per_subject_cap: int = research_subjects.DEFAULT_PER_SUBJECT_CAP,
    rng: random.Random | None = None,
    skipped: list[dict] | None = None,
) -> list[dict]:
    """Turn backlog items into scored, launchable research jobs.

    This is the live bridge the pure scheduler needs: backlog supplies concrete subjects, hypotheses supply
    the question under test, learned weights supply posteriors, and spare capacity gates the whole thing.
    """
    rng = rng or random.Random()
    claimed_targets = claimed_targets or set()
    excluded_targets = excluded_targets or set()
    hyp_by_id = _hypothesis_by_id(hyps)
    jobs = []
    seen_subject_families: set[str] = set()
    for item in items:
        if item.get("lane") and item.get("lane") != "opener":
            if skipped is not None:
                skipped.append(
                    {
                        "target": item.get("target"),
                        "task_type": item.get("task_type", "implement"),
                        "reason": "not_opener",
                    }
                )
            continue
        target = str(item.get("target") or "")
        if not target or "#" not in target:
            if skipped is not None:
                skipped.append(
                    {
                        "target": target,
                        "task_type": item.get("task_type", "implement"),
                        "reason": "invalid_target",
                    }
                )
            continue
        if target in excluded_targets:
            if skipped is not None:
                skipped.append(
                    {
                        "target": target,
                        "task_type": item.get("task_type", "implement"),
                        "reason": "production_reserved",
                    }
                )
            continue
        if target in claimed_targets:
            if skipped is not None:
                skipped.append(
                    {
                        "target": target,
                        "task_type": item.get("task_type", "implement"),
                        "reason": "claimed_target",
                    }
                )
            continue
        task_type = item.get("task_type", "implement")
        decision = should_test(item, hyps, spare, conn=conn, weights=weights)
        if not decision:
            continue
        candidate_arms = decision.get("arms") or []
        if not _is_launchable_arm_set(candidate_arms):
            if skipped is not None:
                skipped.append(
                    {
                        "target": target,
                        "task_type": task_type,
                        "hypothesis": decision.get("hypothesis"),
                        "reason": "strategy_arms_not_launchable",
                        "arms": [describe_arm(a) for a in candidate_arms],
                        "strategy_experiment_plan_command_template": _strategy_experiment_command_template(
                            decision.get("hypothesis")
                        ),
                        "strategy_experiment_prepare_guard": (
                            "active prepare requires --prepare --confirm-strategy and "
                            "ORCH_STRATEGY_EXPERIMENT=1"
                        ),
                    }
                )
            continue
        posteriors = learned_posteriors(task_type, candidate_arms, learned)
        n_obs = route_n_obs(task_type, candidate_arms, conn=conn, weights=weights)
        hunger = acquisition_hunger(
            task_type, candidate_arms, conn=conn, weights=weights
        )
        arms = select_arms(
            candidate_arms,
            posteriors,
            spare,
            rng=rng,
            task_type=task_type,
            conn=conn,
            weights=weights,
            n_obs=n_obs,
            hunger=max(hunger, float(decision.get("data_hunger") or 0.0)),
        )
        if len(arms) < 2:
            continue
        identity = research_subjects.subject_identity(
            target,
            task_type,
            item.get("body") or item.get("title") or "",
            item.get("base_sha"),
            arms,
            item.get("profiles"),
        )
        if identity["subject_family_id"] in seen_subject_families:
            if skipped is not None:
                skipped.append(
                    {
                        "target": target,
                        "task_type": task_type,
                        "subject_id": identity["subject_id"],
                        "subject_family_id": identity["subject_family_id"],
                        "reason": "duplicate_candidate_in_plan",
                    }
                )
            continue
        admission = research_subjects.assess_candidate(
            target=target,
            task_type=task_type,
            spec=item.get("body") or item.get("title") or "",
            base_sha=item.get("base_sha"),
            arms=arms,
            profiles=item.get("profiles"),
            conn=conn,
            unevaluated_cap=unevaluated_cap,
            per_subject_cap=per_subject_cap,
        )
        if not admission["eligible"]:
            if skipped is not None:
                skipped.append(
                    {
                        "target": target,
                        "task_type": task_type,
                        "subject_id": admission["subject_id"],
                        "subject_family_id": admission["subject_family_id"],
                        "reason": admission["reason"],
                        "unevaluated_backlog": admission.get("unevaluated_backlog"),
                        "unevaluated_cap": admission.get("unevaluated_cap"),
                        "cooldown_until": admission.get("cooldown_until"),
                        "existing_exp_id": admission.get("existing_exp_id"),
                    }
                )
            continue
        seen_subject_families.add(identity["subject_family_id"])
        prior_subject_experiments = research_subjects.prior_experiment_count(
            identity, conn=conn
        )
        repetition_weight = 1.0 / (1.0 + prior_subject_experiments)
        hyp = hyp_by_id.get(str(decision.get("hypothesis")))
        uncertainty = max(
            _hypothesis_uncertainty(hyp),
            float(decision.get("data_hunger") or 0.0),
            hunger,
        )
        value = (
            info_value(uncertainty, _item_stakes(item), max(0.10, hunger))
            * repetition_weight
        )
        jobs.append(
            {
                "id": target,
                "target": target,
                "task_type": task_type,
                "item": item,
                "trigger": decision.get("trigger"),
                "hypothesis": decision.get("hypothesis"),
                "kind": decision.get("kind"),
                "subject_id": identity["subject_id"],
                "subject_family_id": identity["subject_family_id"],
                "canonical_target": identity["canonical_target"],
                "spec_hash": identity["spec_hash"],
                "base_sha": identity.get("base_sha"),
                "arm_set_hash": identity["arm_set_hash"],
                "arms": arms,
                "candidate_arms": candidate_arms,
                "n": len(arms),
                "n_obs": {a: n_obs.get(a, 0) for a in arms},
                "posteriors": {a: round(posteriors.get(a, 0.5), 4) for a in arms},
                "data_hunger": round(float(decision.get("data_hunger") or 0.0), 4),
                "acquisition_hunger": round(float(hunger), 4),
                "prior_subject_experiments": prior_subject_experiments,
                "repetition_weight": round(repetition_weight, 6),
                "uncertainty": round(float(uncertainty), 4),
                "capacity_cost": max(1, len(arms)),
                "info_value": round(float(value), 6),
            }
        )
    return jobs


def build_research_plan(
    items: list[dict],
    capacity_snapshot: dict,
    *,
    learned: dict | None = None,
    hyps: list[dict] | None = None,
    conn=None,
    weights: list[dict] | None = None,
    claimed_targets: set[str] | None = None,
    excluded_targets: set[str] | None = None,
    production_reserve: dict[str, int] | None = None,
    unevaluated_cap: int = research_subjects.DEFAULT_UNEVALUATED_CAP,
    per_subject_cap: int = research_subjects.DEFAULT_PER_SUBJECT_CAP,
    max_jobs: int = 1,
    budget: int | None = None,
    rng: random.Random | None = None,
) -> dict:
    """Capacity/backlog/hypotheses -> selected research experiment plan.

    The returned jobs are safe for tick.py to turn into exp_abcd.prepare calls. This function never claims,
    labels, spawns, writes specs, or mutates external state.
    """
    # Credit where the DRIVER actually enters this module. The heartbeat previously sat
    # only in main(), which no driver calls -- dispatcher/tick call this function
    # directly -- so the capability ran constantly and recorded nothing. (2026-08-20)
    _capability_heartbeat()
    hyps = load_hypotheses() if hyps is None else hyps
    spare = spare_capacity(capacity_state_from_snapshot(capacity_snapshot))
    for agent, units in (production_reserve or {}).items():
        spare[agent] = max(0, spare.get(agent, 0) - max(0, int(units or 0)))
    total_spare = sum(spare.values())
    if total_spare <= 0:
        return {
            "status": "no_spare",
            "spare": spare,
            "budget": 0,
            "candidates": [],
            "planned": [],
            "skipped": [],
            "blocked_reasons": ["no_spare_after_production_reserve"],
        }
    budget = total_spare if budget is None else max(0, int(budget))
    skipped: list[dict] = []
    candidates = research_job_candidates(
        items,
        spare,
        hyps,
        learned=learned,
        conn=conn,
        weights=weights,
        claimed_targets=claimed_targets,
        excluded_targets=excluded_targets,
        unevaluated_cap=unevaluated_cap,
        per_subject_cap=per_subject_cap,
        rng=rng,
        skipped=skipped,
    )
    selected = select_jobs(candidates, budget=budget)
    db = conn or feedback._conn()
    close_db = conn is None
    try:
        current_unevaluated = len(research_subjects.unevaluated_experiment_ids(db))
    finally:
        if close_db:
            db.close()
    remaining_backlog_slots = max(0, int(unevaluated_cap) - current_unevaluated)
    selection_cap = remaining_backlog_slots
    if max_jobs >= 0:
        selection_cap = min(selection_cap, max_jobs)
    overflow = selected[selection_cap:]
    selected = selected[:selection_cap]
    for job in overflow:
        skipped.append(
            {
                "target": job.get("target"),
                "task_type": job.get("task_type"),
                "subject_id": job.get("subject_id"),
                "subject_family_id": job.get("subject_family_id"),
                "reason": "unevaluated_backlog_batch_reserve",
                "unevaluated_backlog": current_unevaluated,
                "unevaluated_cap": int(unevaluated_cap),
            }
        )
    blocked_reasons = sorted(
        {
            str(row.get("reason"))
            for row in skipped
            if row.get("reason")
            in {
                "unevaluated_backlog_cap",
                "unevaluated_backlog_batch_reserve",
                "subject_active",
                "subject_evaluable",
                "subject_planned",
                "subject_cooldown",
                "subject_backlog_cap",
                "duplicate_candidate_in_plan",
                "production_reserved",
                "claimed_target",
            }
        }
    )
    return {
        "status": "planned" if selected else ("blocked" if blocked_reasons else "no_subject"),
        "spare": spare,
        "budget": budget,
        "candidates": candidates,
        "planned": selected,
        "skipped": skipped,
        "blocked_reasons": blocked_reasons,
        "production_reserve": dict(sorted((production_reserve or {}).items())),
        "unevaluated_cap": unevaluated_cap,
        "unevaluated_backlog": current_unevaluated,
        "remaining_backlog_slots": remaining_backlog_slots,
        "per_subject_cap": per_subject_cap,
    }


def _selftest():
    import sqlite3

    cap = {
        "cursor": {"status": "ok", "tier": "free"},
        "vibe": {"status": "ok", "tier": "flat"},
        "codex": {"status": "warn", "tier": "metered"},
        "claude": {"status": "shed", "tier": "metered"},
        "gpt": {"status": "ok", "tier": "paygo"},
    }
    sp = spare_capacity(cap)
    assert sp["cursor"] == 3 and sp["vibe"] == 3, sp  # free/flat idle -> generous spare
    assert (
        sp["codex"] == 0 and sp["claude"] == 0
    ), sp  # warn metered halves to 0; shed -> 0
    assert sp["gpt"] == 0, sp  # paygo not spent on science by default

    # knapsack: with a tight budget, take the best info/cost jobs only (variable intensity)
    jobs = [
        {"id": "cheap-eval", "info_value": 6, "capacity_cost": 1},
        {"id": "big-experiment", "info_value": 12, "capacity_cost": 5},
        {"id": "adversarial", "info_value": 4, "capacity_cost": 2},
    ]
    assert [j["id"] for j in select_jobs(jobs, budget=1)] == [
        "cheap-eval"
    ], "scarce -> cheapest high-value"
    big = select_jobs(jobs, budget=8)
    assert (
        "big-experiment" in [j["id"] for j in big] and len(big) >= 2
    ), "abundant -> intense set"

    # Top-Two variable-N: leader + most-uncertain challenger always; extras scale with spare; deterministic rng
    posteriors = {"cursor": 0.8, "codex": 0.55, "claude": 0.5, "vibe": 0.45}
    units = {
        "cursor": 3,
        "codex": 3,
        "claude": 3,
        "vibe": 3,
    }  # total 12 -> target N = 2 + 12//3 = 5 (capped)
    arms = select_arms(
        ["cursor", "codex", "claude", "vibe"], posteriors, units, rng=random.Random(0)
    )
    assert arms[0] == "cursor", "leader (max posterior) first"
    assert arms[1] == "claude", "challenger = posterior nearest 0.5 (most uncertain)"
    assert 2 <= len(arms) <= 5 and len(set(arms)) == len(arms), arms
    scarce = select_arms(
        ["cursor", "codex", "claude"],
        posteriors,
        {"cursor": 1, "codex": 1, "claude": 0},
        rng=random.Random(0),
    )
    assert set(scarce) == {"cursor", "codex"}, "no-spare seats excluded -> 2 arms"

    weights_empty = [
        {"agent": a, "n_obs": 0}
        for a in ["cursor", "codex", "claude", "gemini", "vibe"]
    ]
    assert data_hunger("implement", ["cursor", "codex"], weights=weights_empty) == 1.0
    weights_full = [
        {"agent": a, "n_obs": int(DATA_HUNGER_THRESHOLD)}
        for a in ["cursor", "codex", "claude"]
    ]
    assert (
        data_hunger("implement", ["cursor", "codex", "claude"], weights=weights_full)
        == 0.0
    )

    now = int(time.time())
    freshness_db = sqlite3.connect(":memory:")
    freshness_db.executescript(feedback.SCHEMA)
    feedback._migrate_schema(freshness_db)

    def add_run(run_id, task_type, agent, ts, model=None, outcome=True):
        freshness_db.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, model) VALUES (?,?,?,?,?,?)",
            (run_id, ts, "o/r#fresh", task_type, agent, model),
        )
        if model:
            feedback._record_execution_attempt_in_conn(
                freshness_db,
                run_id=run_id,
                attempt_id=f"attempt:{run_id}",
                operation_role="worker",
                profile_id=f"profile:{model}",
                resolved_provider="fixture",
                resolved_model=model,
                status="success",
                completed_ts=ts,
                recorded_ts=ts,
            )
        if outcome:
            freshness_db.execute(
                "INSERT INTO outcomes "
                "(run_id, verifier_verdict, adjudicated_verdict, merged, ci_status, durability, "
                "durability_checked_ts, notes) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, None, "PASS", 1, "green", "durable", ts, None),
            )

    recent_ts = now - 2 * 86400
    old_ts = now - int((STALE_DAYS * 3) * 86400)
    add_run("recent-cell", "freshness", "cursor", recent_ts, "cursor-worker-2026-07")
    add_run("old-cell", "freshness", "vibe", old_ts, "codestral-latest")
    assert staleness_days("freshness", "cursor", conn=freshness_db, now=now) < 3
    assert staleness_hunger("freshness", "cursor", conn=freshness_db, now=now) == 0.0
    assert staleness_hunger("freshness", "vibe", conn=freshness_db, now=now) == 1.0
    assert staleness_hunger(days=STALE_DAYS / 2) == 0.0
    assert staleness_hunger(days=STALE_DAYS * 3) == 1.0

    add_run("drift-old", "driftcheck", "codex", now - 10 * 86400, "gpt-5.5")
    add_run("drift-new", "driftcheck", "codex", now - 3600, "gpt-5.6", outcome=False)
    assert model_drifted("driftcheck", "codex", conn=freshness_db) is True
    freshness_db.execute(
        "INSERT INTO outcomes "
        "(run_id, verifier_verdict, adjudicated_verdict, merged, ci_status, durability, "
        "durability_checked_ts, notes) VALUES (?,?,?,?,?,?,?,?)",
        ("drift-new", None, "PASS", 1, "green", "durable", now - 1800, None),
    )
    assert model_drifted("driftcheck", "codex", conn=freshness_db) is False
    add_run("null-old", "driftcheck", "null_agent", now - 5000, "old")
    add_run("null-current", "driftcheck", "null_agent", now - 1000, None, outcome=False)
    assert model_drifted("driftcheck", "null_agent", conn=freshness_db) is False
    full_fresh_weights = [
        {"agent": a, "n_obs": int(DATA_HUNGER_THRESHOLD)}
        for a in ["cursor", "codex", "vibe"]
    ]
    assert (
        acquisition_hunger(
            "freshness", ["cursor"], conn=freshness_db, weights=full_fresh_weights
        )
        == 0.0
    )
    assert (
        acquisition_hunger(
            "freshness", ["vibe"], conn=freshness_db, weights=full_fresh_weights
        )
        == 1.0
    )

    hungry_post = {
        "cursor": 0.9,
        "codex": 0.7,
        "claude": 0.5,
        "gemini": 0.3,
        "vibe": 0.2,
    }
    hungry_units = {"cursor": 1, "codex": 1, "claude": 1, "gemini": 1, "vibe": 1}
    hungry_obs = {"cursor": 8, "codex": 8, "claude": 8, "gemini": 0, "vibe": 0}
    hungry_arms = select_arms(
        ["cursor", "codex", "claude", "gemini", "vibe"],
        hungry_post,
        hungry_units,
        rng=random.Random(1),
        n_obs=hungry_obs,
        hunger=1.0,
    )
    assert len(hungry_arms) == 5 and {"gemini", "vibe"} <= set(hungry_arms), hungry_arms

    current_models = {
        "cursor": "cursor-worker-2026-07",
        "codex": "gpt-5.6",
        "claude": "claude-sonnet-4-6",
    }
    for agent, model in current_models.items():
        add_run(f"select-{agent}", "freshselect", agent, recent_ts, model)
    add_run("select-vibe", "freshselect", "vibe", old_ts, "codestral-latest")
    select_weights = [
        {"agent": a, "n_obs": int(DATA_HUNGER_THRESHOLD)}
        for a in ["cursor", "codex", "claude", "vibe"]
    ]
    stale_recruited = select_arms(
        ["cursor", "codex", "claude", "vibe"],
        hungry_post,
        {"cursor": 1, "codex": 1, "claude": 1, "vibe": 1},
        rng=random.Random(2),
        task_type="freshselect",
        conn=freshness_db,
        weights=select_weights,
    )
    assert "vibe" in stale_recruited and len(stale_recruited) == 4, stale_recruited

    # should_test: hypothesis-driven match vs nothing-when-no-capacity
    hyps = load_hypotheses(Path("/tmp/__hyp_selftest.json"))
    plan = should_test(
        {"task_type": "implement"}, hyps, {"cursor": 3, "codex": 3, "claude": 1}
    )
    assert plan and plan["trigger"] == "hypothesis", plan
    assert (
        should_test({"task_type": "implement"}, hyps, {"cursor": 0, "codex": 0}) is None
    ), "no capacity -> no test"

    # strategy arms (multi-agent value stays learnable): describe/normalize + strategy-vs-single comparison
    para = {"strategy": "parallel", "agents": ["claude", "cursor"], "synthesize": True}
    assert describe_arm(para) == "parallel(claude+cursor+synth)" and arm_agents(
        para
    ) == ["claude", "cursor"]
    assert describe_arm("codex") == "codex" and arm_agents("codex") == ["codex"]
    strat = [
        {
            "id": "S",
            "task_type": "x",
            "conditions": "",
            "arms": [{"strategy": "single", "agents": ["claude"]}, para],
            "evidence": {"status": "open"},
        }
    ]
    ps = should_test({"task_type": "x"}, strat, {"claude": 3, "cursor": 3})
    assert (
        ps and ps["kind"] == "strategy" and len(ps["arms"]) == 2
    ), ps  # single vs parallel compared
    assert (
        should_test({"task_type": "x"}, strat, {"claude": 3}) is None
    ), "parallel arm unrunnable w/o cursor spare"
    hungry_op = should_test(
        {"task_type": "unknown"},
        [],
        {"gemini": 1, "vibe": 1},
        weights=[{"agent": "gemini", "n_obs": 0}, {"agent": "vibe", "n_obs": 0}],
    )
    assert hungry_op and hungry_op["trigger"] == "opportunistic", hungry_op
    add_run("sated-gemini", "unknown", "gemini", recent_ts, "gemini-2.5-pro")
    add_run("sated-vibe", "unknown", "vibe", recent_ts, "codestral-latest")
    sated_op = should_test(
        {"task_type": "unknown"},
        [],
        {"gemini": 1, "vibe": 1},
        conn=freshness_db,
        weights=[
            {"agent": "gemini", "n_obs": int(DATA_HUNGER_THRESHOLD)},
            {"agent": "vibe", "n_obs": int(DATA_HUNGER_THRESHOLD)},
        ],
    )
    assert sated_op is None, sated_op
    assert (
        should_test(
            {"task_type": "unknown"},
            [],
            {"gemini": 0, "vibe": 0},
            weights=weights_empty,
        )
        is None
    )
    assert any(
        h["id"] == "H4" for h in SEED_HYPOTHESES
    ), "multi-agent-value hypothesis seeded"

    live_cap = {
        "agents": {
            "cursor": {"state": "ok"},
            "codex": {"state": "ok"},
            "claude": {"state": "ok"},
            "vibe": {"state": "ok"},
        }
    }
    cap_state = capacity_state_from_snapshot(live_cap)
    assert (
        cap_state["cursor"]["status"] == "ok" and cap_state["vibe"]["tier"] == "flat"
    ), cap_state
    learned = {
        "implement": {
            "cursor": {"posterior": 0.55, "n_obs": 1},
            "codex": {"posterior": 0.80, "n_obs": 4},
            "claude": {"posterior": 0.50, "n_obs": 0},
        }
    }
    items = [
        {
            "target": "o/r#research",
            "task_type": "implement",
            "lane": "opener",
            "title": "Do it",
        }
    ]
    live_plan = build_research_plan(
        items,
        live_cap,
        learned=learned,
        hyps=hyps,
        claimed_targets=set(),
        rng=random.Random(0),
        conn=freshness_db,
    )
    assert live_plan["status"] == "planned" and live_plan["planned"], live_plan
    job = live_plan["planned"][0]
    assert job["target"] == "o/r#research" and job["hypothesis"] == "H1", job
    assert len(job["arms"]) >= 2 and job["capacity_cost"] == len(job["arms"]), job
    blocked_plan = build_research_plan(
        items,
        live_cap,
        learned=learned,
        hyps=hyps,
        claimed_targets={"o/r#research"},
        rng=random.Random(0),
        conn=freshness_db,
    )
    assert (
        blocked_plan["status"] == "blocked"
        and blocked_plan["blocked_reasons"] == ["claimed_target"]
        and not blocked_plan["planned"]
    ), blocked_plan
    closer_plan = build_research_plan(
        [{"target": "o/r#release", "task_type": "implement", "lane": "closer"}],
        live_cap,
        learned=learned,
        hyps=hyps,
        rng=random.Random(0),
        conn=freshness_db,
    )
    assert closer_plan["status"] == "no_subject", closer_plan
    strategy_plan = build_research_plan(
        [{"target": "o/r#strategy", "task_type": "x", "lane": "opener"}],
        {"agents": {"claude": {"state": "ok"}, "cursor": {"state": "ok"}}},
        hyps=strat,
        rng=random.Random(0),
        conn=freshness_db,
    )
    assert (
        strategy_plan["status"] == "no_subject" and strategy_plan["skipped"]
    ), strategy_plan
    assert (
        strategy_plan["skipped"][0]["reason"] == "strategy_arms_not_launchable"
    ), strategy_plan
    strategy_skip = strategy_plan["skipped"][0]
    assert (
        strategy_skip["strategy_experiment_plan_command_template"][1].endswith(
            "strategy_experiment.py"
        )
        and "--hypothesis" in strategy_skip["strategy_experiment_plan_command_template"]
        and "ORCH_STRATEGY_EXPERIMENT=1"
        in strategy_skip["strategy_experiment_prepare_guard"]
    ), strategy_skip

    # Subject control: identical target/spec/base rows yield one candidate in a plan;
    # once launched, a second plan gets an explicit active/backlog reason.
    research_subjects.ensure_schema(freshness_db)
    subject_item = {
        "target": "o/r#subject",
        "task_type": "implement",
        "lane": "opener",
        "title": "Stable research subject",
        "body": "Frozen acceptance criteria",
        "base_sha": "abc123",
    }
    duplicate_plan = build_research_plan(
        [subject_item, dict(subject_item)],
        live_cap,
        learned=learned,
        hyps=hyps,
        conn=freshness_db,
        rng=random.Random(0),
        unevaluated_cap=99,
    )
    assert len(duplicate_plan["candidates"]) == 1, duplicate_plan
    assert any(
        row.get("reason") == "duplicate_candidate_in_plan"
        for row in duplicate_plan["skipped"]
    ), duplicate_plan
    admitted_job = duplicate_plan["planned"][0]
    admitted_identity = research_subjects.subject_identity(
        subject_item["target"],
        subject_item["task_type"],
        subject_item["body"],
        subject_item["base_sha"],
        admitted_job["arms"],
    )
    research_subjects.record_subject(
        admitted_identity,
        lifecycle="active",
        exp_id="subject-exp",
        conn=freshness_db,
    )
    repeated_plan = build_research_plan(
        [subject_item],
        live_cap,
        learned=learned,
        hyps=hyps,
        conn=freshness_db,
        rng=random.Random(0),
        unevaluated_cap=99,
    )
    assert repeated_plan["status"] == "blocked", repeated_plan
    assert set(repeated_plan["blocked_reasons"]) & {
        "subject_active",
        "subject_backlog_cap",
    }, repeated_plan
    freshness_db.execute(
        "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id) "
        "VALUES ('cap-run',?,?,?,'codex','cap-exp')",
        (now, "o/r#cap", "implement"),
    )
    capped_plan = build_research_plan(
        [{**subject_item, "target": "o/r#other"}],
        live_cap,
        learned=learned,
        hyps=hyps,
        conn=freshness_db,
        rng=random.Random(0),
        unevaluated_cap=1,
    )
    assert capped_plan["status"] == "blocked", capped_plan
    assert capped_plan["blocked_reasons"] == ["unevaluated_backlog_cap"], capped_plan
    reserved_plan = build_research_plan(
        [{**subject_item, "target": "o/r#reserved"}],
        live_cap,
        learned=learned,
        hyps=hyps,
        conn=freshness_db,
        production_reserve={agent: 99 for agent in live_cap["agents"]},
        unevaluated_cap=99,
    )
    assert reserved_plan["status"] == "no_spare", reserved_plan
    assert reserved_plan["blocked_reasons"] == [
        "no_spare_after_production_reserve"
    ], reserved_plan

    freshness_db.close()
    Path("/tmp/__hyp_selftest.json").unlink(missing_ok=True)
    print(
        "research_scheduler.py selftest: OK (spare-capacity gate, info/cost knapsack intensity, "
        "Top-Two variable-N arm selection, staleness/model-drift acquisition, "
        "hypothesis/opportunistic triggers, live capacity/backlog research plan, "
        "subject dedup/cooldown/backlog gate, production reserve, "
        "strategy arms = multi-agent value learnable)"
    )


def _demo() -> None:
    import sqlite3

    now = int(time.time())
    conn = sqlite3.connect(":memory:")
    conn.executescript(feedback.SCHEMA)
    feedback._migrate_schema(conn)
    old_ts = now - int((STALE_DAYS * 3) * 86400)
    recent_ts = now - 86400
    for run_id, agent, ts, model in [
        ("demo-cursor", "cursor", recent_ts, "cursor:composer-auto"),
        ("demo-codex", "codex", recent_ts, "codex:full:default"),
        ("demo-vibe", "vibe", old_ts, "vibe:default"),
    ]:
        conn.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, model) VALUES (?,?,?,?,?,?)",
            (run_id, ts, "demo/repo#1", "implement", agent, model),
        )
        conn.execute(
            "INSERT INTO outcomes "
            "(run_id, verifier_verdict, adjudicated_verdict, merged, ci_status, durability, "
            "durability_checked_ts, notes) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, None, "PASS", 1, "green", "durable", ts, None),
        )
    weights = [
        {"agent": a, "n_obs": int(DATA_HUNGER_THRESHOLD)}
        for a in ["cursor", "codex", "vibe"]
    ]
    hunger = acquisition_hunger("implement", ["vibe"], conn=conn, weights=weights)
    arms = select_arms(
        ["cursor", "codex", "vibe"],
        {"cursor": 0.8, "codex": 0.55, "vibe": 0.4},
        {"cursor": 1, "codex": 1, "vibe": 1},
        rng=random.Random(7),
        task_type="implement",
        conn=conn,
        weights=weights,
    )
    print(
        f"demo cell implement/vibe staleness_days={staleness_days('implement', 'vibe', conn=conn, now=now):.1f}"
    )
    print(f"demo acquisition_hunger implement/vibe={hunger:.2f}")
    print(f"demo select_arms recruited={arms}")
    conn.close()


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this infrastructure capability ran. Infra is never ROUTED to — it runs as part
    of the tick — so it records use at its own entrypoint. Lazy import, never raises, and inert
    outside an active tick (ORCH_CAPABILITY_HEARTBEATS). (2026-08-09)"""
    try:
        import capabilities
        capabilities.production_heartbeat("research-scheduler", event_type, ref="research_scheduler.main")
    except Exception:
        pass


def main(argv):
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest()
        return 0
    if "--demo" in argv:
        _demo()
        return 0
    hyps = load_hypotheses()
    print(f"{len(hyps)} hypotheses at {HYP_PATH}")
    for h in hyps:
        print(
            f"  [{h['id']}] {h['evidence']['status']:12} n={h['evidence']['n']} :: {h['claim']}"
        )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
