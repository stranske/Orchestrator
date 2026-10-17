"""Shadow routing economics; reads Brain and #668 capacity snapshots, never dispatches.

q and cost are agent -> task -> number. Missing measurements stay None, including
lambda; zero denotes a measured slack price. No dependency on unmerged pacing code.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import TypeGuard

SCHEMA = "orchestrator.route-weights-comparative/v1"
WINDOW_DAYS = 14
MAX_AGE_SECONDS = 2 * 3600


def _number(value) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def comparative_ranks(q, cost, lam, price) -> dict:
    """Order by q - (lambda + list price) * capacity cost; unknown scores sort last."""
    ranks: dict[str, list[dict]] = {}
    for agent, cells in q.items():
        for task, quality in cells.items():
            values = (quality, cost.get(agent, {}).get(task), lam.get(agent), price.get(agent))
            measured = all(_number(value) for value in values)
            score = values[0] - (values[2] + values[3]) * values[1] if measured else None
            row = {"agent": agent, "comparative_score": score}
            if not measured:
                row["reason"] = "quality, capacity cost, lambda or price unmeasured"
            ranks.setdefault(task, []).append(row)
    for rows in ranks.values():
        rows.sort(
            key=lambda row: (
                row["comparative_score"] is None,
                -row["comparative_score"] if row["comparative_score"] is not None else 0,
                row["agent"],
            )
        )
    return ranks


def shadow_prices(q, demand, cost, budget, price, *, max_iter=50) -> dict:
    """Bounded deterministic price adjustment specified by #676 (not an LP solver).

    Each over-budget seat loses its cheapest-to-displace class at the next price
    boundary. Ties move just beyond that boundary. Discrete/infeasible assignments
    can reach max_iter; that is not a convergence claim.
    """
    reasons = []
    tasks = {task for cells in q.values() for task in cells}
    if not q or not tasks:
        reasons.append("no quality cells with n_obs >= 20")
    for task in tasks:
        if not _number(demand.get(task)) or demand[task] < 0:
            reasons.append(f"demand[{task}] unmeasured or invalid")
    for agent, cells in q.items():
        if not _number(budget.get(agent)):
            reasons.append(f"budget[{agent}] unmeasured")
        if not _number(price.get(agent)) or price[agent] < 0:
            reasons.append(f"price[{agent}] unmeasured or invalid")
        for task, quality in cells.items():
            unit = cost.get(agent, {}).get(task)
            if not _number(quality) or not 0 <= quality <= 1:
                reasons.append(f"quality[{agent},{task}] unmeasured or invalid")
            if not _number(unit) or unit < 0:
                reasons.append(f"cost[{agent},{task}] unmeasured or invalid")
    if reasons:
        return {
            "lambda": dict.fromkeys(q),
            "assignment": dict.fromkeys(sorted(tasks)),
            "iterations": 0,
            "status": "inputs_unmeasured",
            "reasons": sorted(reasons),
        }
    lam = dict.fromkeys(q, 0.0)
    for iteration in range(max_iter + 1):
        ranks = comparative_ranks(q, cost, lam, price)
        assignment = {task: rows[0]["agent"] for task, rows in ranks.items()}
        usage = {
            agent: sum(
                demand[task] * cost[agent][task]
                for task, chosen in assignment.items()
                if chosen == agent
            )
            for agent in q
        }
        over = [agent for agent in sorted(q) if usage[agent] > budget[agent] + 1e-12]
        status = "converged" if not over else "max_iter"
        result = {
            "lambda": lam.copy(),
            "assignment": assignment,
            "iterations": iteration,
            "status": status,
        }
        if not over or iteration == max_iter:
            return result
        # Reassign after each single-seat adjustment; never act on stale runners-up.
        moves = []
        for agent in over:
            for task, chosen in assignment.items():
                if chosen != agent or demand[task] == 0 or cost[agent][task] == 0:
                    continue
                rows = ranks[task]
                if len(rows) > 1:
                    delta = (rows[0]["comparative_score"] - rows[1]["comparative_score"]) / cost[
                        agent
                    ][task]
                    moves.append((delta, agent, task))
        if not moves:
            result["reasons"] = ["no alternative can relieve the over-budget assignment"]
            return result
        delta, agent, _ = min(moves)
        lam[agent] += delta + max(1e-12, abs(lam[agent] + delta) * 1e-12)
    raise AssertionError("unreachable")


def _timestamp(value) -> float | None:
    if _number(value):
        return float(value)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _window(db_path: Path, now: int, window_days: int) -> tuple[list[dict], list[dict], list[str]]:
    """Read-only, no schema migration and no accidental empty Brain creation."""
    if not db_path.exists():
        return [], [], ["Brain unavailable"]
    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            weights = [
                dict(row)
                for row in conn.execute(
                    "SELECT agent, task_type, posterior, n_obs FROM route_weights "
                    "WHERE version=(SELECT MAX(version) FROM route_weights) AND n_obs>=20"
                )
            ]
            runs = [
                dict(row)
                for row in conn.execute(
                    "SELECT r.agent,r.task_type,r.source,c.tokens_in,c.tokens_out,c.cost_usd,"
                    "c.source AS cost_source FROM runs r LEFT JOIN costs c ON c.run_id=r.run_id "
                    "WHERE r.ts>=? AND r.ts<=?",
                    (now - window_days * 86400, now),
                )
            ]
        return weights, runs, []
    except sqlite3.Error as exc:
        return [], [], [f"Brain inputs unavailable: {exc}"]


def measure(
    db_path: Path, snapshot: dict | None, *, now=None, window_days=WINDOW_DAYS, q=None
) -> dict:
    """Adapt only #668's stated pools[p].pacing contract, with explicit provenance.

    D is each class's share of window volume; c is burn_per_day / agent's window
    run count, times the local class/agent mean-token ratio (1.0, flagged, if absent).
    B is allowed_fraction - used_fraction. No burn estimate is invented.
    """
    from feedback import COMPLETE_COST_SOURCES, MIN_COST_COVERAGE, TOKEN_SOURCES

    if window_days < 1:
        raise ValueError("window_days must be positive")
    now = int(time.time()) if now is None else now
    weights, runs, reasons = _window(db_path, now, window_days)
    if q is None:
        q = {}
        for row in weights:
            q.setdefault(row["agent"], {})[row["task_type"]] = row["posterior"]
    tasks = {task for cells in q.values() for task in cells}
    counts = {task: sum(row["task_type"] == task for row in runs) for task in tasks}
    total = len(runs)
    demand = {task: count / total if total else None for task, count in counts.items()}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    observed = _timestamp(snapshot.get("generated_at"))
    pools = snapshot.get("pools")
    pools = pools if isinstance(pools, dict) else {}
    cost: dict = {}
    budget: dict = {}
    price: dict = {}
    sources: dict = {}
    pool_ids: dict = {}
    for agent, cells in q.items():
        mapped = [
            (name, pool)
            for name, pool in pools.items()
            if isinstance(pool, dict) and pool.get("agent") == agent
        ]
        budget[agent], price[agent] = None, None
        cost[agent] = dict.fromkeys(cells)
        sources[agent] = {"source": None, "reason": None, "imputed": []}
        if len(mapped) != 1:
            sources[agent]["reason"] = "needs #668 pacing state: unique pool mapping absent"
            continue
        pool_id, pool = mapped[0]
        pool_ids[agent] = pool_id
        pacing = pool.get("pacing")
        if not isinstance(pacing, dict):
            sources[agent]["reason"] = "needs #668 pacing state: pacing block absent"
            continue
        stamp = _timestamp(pacing.get("observed_at", observed))
        end = _timestamp(pacing.get("period_end"))
        if stamp is None or not 0 <= now - stamp <= MAX_AGE_SECONDS or end is None or end <= now:
            sources[agent][
                "reason"
            ] = "needs #668 pacing state: absent/stale observation or expired period"
            continue
        used, allowed, burn = (
            pacing.get(key) for key in ("used_fraction", "allowed_fraction", "burn_per_day")
        )
        if pacing.get("state") not in ("ok", "warn", "shed") or not _number(
            pacing.get("resets_available")
        ):
            sources[agent]["reason"] = "needs #668 pacing state: state/resets unmeasured"
            continue
        if _number(used) and _number(allowed) and used >= 0 and allowed >= 0:
            budget[agent] = allowed - used
        agent_runs = [row for row in runs if row["agent"] == agent]
        # Cost rows represent API list-price equivalents, not subscription bills.
        # Muse / explicitly metered Claude need a measured list price; subscriptions
        # otherwise have measured marginal price zero regardless of capacity tier.
        metered = agent == "muse" or pool_id.endswith(("paygo", "metered"))
        priced = [
            row["cost_usd"]
            for row in agent_runs
            if row["cost_source"] in COMPLETE_COST_SOURCES
            and _number(row["cost_usd"])
            and row["cost_usd"] >= 0
        ]
        coverage = len(priced) / len(agent_runs) if agent_runs else 0.0
        price[agent] = (
            (sum(priced) / len(priced) if priced and coverage >= MIN_COST_COVERAGE else None)
            if metered
            else 0.0
        )
        if not _number(burn) or burn < 0 or not agent_runs:
            sources[agent]["reason"] = "burn_per_day or window run count unmeasured"
            continue
        measured = [
            row
            for row in agent_runs
            if row["source"] == "orchestrator_local"
            and row["cost_source"] in TOKEN_SOURCES
            and _number(row["tokens_in"])
            and _number(row["tokens_out"])
            and row["tokens_in"] + row["tokens_out"] > 0
        ]
        mean = (
            sum(row["tokens_in"] + row["tokens_out"] for row in measured) / len(measured)
            if measured
            else None
        )
        for task in cells:
            local = [row for row in measured if row["task_type"] == task]
            multiplier = (
                (sum(row["tokens_in"] + row["tokens_out"] for row in local) / len(local) / mean)
                if local and mean is not None
                else 1.0
            )
            if not local:
                sources[agent]["imputed"].append(task)
            cost[agent][task] = burn / len(agent_runs) * multiplier
        sources[agent][
            "source"
        ] = "#668 burn_per_day / window_runs; local token ratio (1.0 if imputed)"
    result = shadow_prices(q, demand, cost, budget, price)
    reasons += [f"{agent}: {row['reason']}" for agent, row in sources.items() if row["reason"]]
    if reasons:
        result.update(status="inputs_unmeasured", **{"lambda": dict.fromkeys(q)})
    reasons += result.get("reasons", [])
    result.update(
        schema=SCHEMA,
        reasons=sorted(set(reasons)),
        inputs={
            "pacing_observed_at": observed,
            "cost_source_by_agent": sources,
            "pool_by_agent": pool_ids,
            "demand": demand,
            "cost": cost,
            "budget": budget,
            "price": price,
        },
        window_days=window_days,
        ranks=comparative_ranks(q, cost, result["lambda"], price),
    )
    return result


def history_path(state_dir: Path | None = None) -> Path:
    """Transport is configurable until #668 publishes a canonical history location."""
    base = state_dir or Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
    return base / "pacing-history.ndjson"


def report(db_path: Path, path: Path, *, now=None, window_days=WINDOW_DAYS) -> dict:
    """Replay at most one (latest) capacity snapshot per UTC hour, without look-ahead runs."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    now = int(time.time()) if now is None else now
    hours: dict = {}
    errors: list[str] = []
    try:
        for line in path.read_text().splitlines():
            try:
                snapshot = json.loads(line)
                stamp = _timestamp(snapshot.get("generated_at"))
                if stamp is not None and now - window_days * 86400 <= stamp <= now:
                    hour = int(stamp // 3600)
                    if hour not in hours or stamp > hours[hour][0]:
                        hours[hour] = (stamp, snapshot)
            except (ValueError, AttributeError):
                errors.append("malformed pacing snapshot")
    except OSError as exc:
        errors.append(f"needs #668 pacing state: {exc}")
    weights, _, brain_errors = _window(db_path, now, window_days)
    errors += brain_errors
    q: dict = {}
    for row in weights:
        q.setdefault(row["agent"], {})[row["task_type"]] = row["posterior"]
    for agent in ("codex", "claude"):
        if agent not in q:
            errors.append(f"{agent}: no quality cells with n_obs >= 20; live lambda unmeasured")
    posterior: dict[str, list[dict]] = {}
    for agent, cells in q.items():
        for task, quality in cells.items():
            if _number(quality):
                posterior.setdefault(task, []).append({"agent": agent, "posterior": quality})
    for rows in posterior.values():
        rows.sort(key=lambda row: (-row["posterior"], row["agent"]))
    classes = {
        task: {
            "posterior_top": rows[0]["agent"],
            "comparative_top": None,
            "hours_differing": None,
            "max_lambda": None,
        }
        for task, rows in posterior.items()
    }
    maxima: dict = {}
    measured, statuses = 0, []
    for stamp, snapshot in sorted(hours.values()):
        result = measure(db_path, snapshot, now=int(stamp), window_days=window_days, q=q)
        statuses.append(result["status"])
        if result["status"] != "inputs_unmeasured":
            for agent, value in result["lambda"].items():
                maxima[agent] = max(maxima.get(agent, 0.0), value)
        if result["status"] != "converged":
            errors += result["reasons"]
            continue
        measured += 1
        for task, rows in result["ranks"].items():
            row = classes[task]
            row["comparative_top"] = rows[0]["agent"]
            row["hours_differing"] = (row["hours_differing"] or 0) + int(
                rows[0]["agent"] != row["posterior_top"]
            )
            row["max_lambda"] = max(
                row["max_lambda"] or 0.0, *(result["lambda"][entry["agent"]] for entry in rows)
            )
    differing = sum(row["hours_differing"] > 0 for row in classes.values()) if measured else None
    status = (
        "inputs_unmeasured"
        if not hours or errors or "inputs_unmeasured" in statuses
        else "max_iter" if "max_iter" in statuses else "converged"
    )
    if status == "converged" and differing == 0 and all(value == 0 for value in maxima.values()):
        status = "not_testable_slack"
    if not hours:
        errors.append("needs #668 pacing state: no hourly snapshots in window")
    return {
        "classes": classes,
        "classes_differing": differing,
        "lambda": maxima or dict.fromkeys(q),
        "status": status,
        "reasons": sorted(set(errors)),
        "window_days": window_days,
        "hours_measured": measured,
        "hours_observed": len(hours),
    }


def format_report(result: dict) -> str:
    lines = [
        f"{task}: " + " ".join(f"{key}={value}" for key, value in row.items())
        for task, row in sorted(result["classes"].items())
    ]
    lines += [
        f"classes_differing {result['classes_differing']}",
        f"status {result['status']}; hours_measured {result['hours_measured']}/{result['hours_observed']}",
    ]
    lines += result["reasons"]
    return "\n".join(lines)
