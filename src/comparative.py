"""Shadow routing economics; reads Brain and #668 capacity snapshots, never dispatches.

q and cost are agent -> task -> number. Missing measurements stay None, including
lambda; zero denotes a measured slack price. Freshness and reserve inputs follow
the shared pacing contract.
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

from pacing import STALE_AFTER_SECONDS

SCHEMA = "orchestrator.route-weights-comparative/v1"
WINDOW_DAYS = 14
MIN_MEASURED_HOUR_SHARE = 0.5
_SIMPLEX_EPS = 1e-12


def _number(value) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def comparative_ranks(q, cost, lam, price, *, value=None, assignment=None) -> dict:
    """Order by q*V - (lambda + price)*cost; optimal allocation breaks score ties."""
    value = value or {}
    assignment = assignment or {}
    ranks: dict[str, list[dict]] = {}
    for agent, cells in q.items():
        for task, quality in cells.items():
            values = (quality, cost.get(agent, {}).get(task), lam.get(agent), price.get(agent))
            measured = all(_number(v) for v in values)
            score = (
                quality * value.get(task, 1.0) - (values[2] + values[3]) * values[1]
                if measured
                else None
            )
            row = {"agent": agent, "comparative_score": score}
            if not measured:
                row["reason"] = "quality, capacity cost, lambda or price unmeasured"
            ranks.setdefault(task, []).append(row)
    for task, rows in ranks.items():
        rows.sort(
            key=lambda row: (
                row["comparative_score"] is None,
                -round(row["comparative_score"], 12) if row["comparative_score"] is not None else 0,
                -(assignment.get(task) or {}).get(row["agent"], 0),
                row["agent"],
            )
        )
    return ranks


def _dense_simplex(matrix, rhs, objective):
    """Two-phase dense primal simplex with Bland entering AND leaving rules.

    All accepted rows are <= with nonnegative RHS. Phase I therefore has the
    explicit feasible slack basis and zero auxiliary objective: no artificial
    variables/pivots are needed. Phase II installs the economic objective.
    Slack-column reduced costs are the original-row duals, never phase-I prices.
    """
    n, m = len(objective), len(rhs)
    tableau = [
        list(row) + [float(i == j) for j in range(m)] + [float(rhs[i])]
        for i, row in enumerate(matrix)
    ]
    basis = list(range(n, n + m))
    tableau.append([0.0] * (n + m + 1))  # Phase I: feasible origin, objective zero.
    tableau[-1][:n] = [-float(v) for v in objective]  # Phase II.
    iterations = 0
    while True:
        entering = next((j for j in range(n + m) if tableau[-1][j] < -_SIMPLEX_EPS), None)
        if entering is None:
            break
        candidates = [i for i in range(m) if tableau[i][entering] > _SIMPLEX_EPS]
        if not candidates:
            raise ArithmeticError("unbounded LP despite finite demand bounds")
        ratio = min(tableau[i][-1] / tableau[i][entering] for i in candidates)
        leaving = min(
            (
                i
                for i in candidates
                if abs(tableau[i][-1] / tableau[i][entering] - ratio) <= _SIMPLEX_EPS
            ),
            key=lambda i: basis[i],
        )
        pivot = tableau[leaving][entering]
        tableau[leaving] = [v / pivot for v in tableau[leaving]]
        for i in range(m + 1):
            if i == leaving:
                continue
            factor = tableau[i][entering]
            tableau[i] = [v - factor * w for v, w in zip(tableau[i], tableau[leaving])]
            tableau[i][entering] = 0.0
        basis[leaving] = entering
        iterations += 1
    primal = [0.0] * n
    for i, basic in enumerate(basis):
        if basic < n:
            primal[basic] = max(0.0, tableau[i][-1])
    # A basic slack has exactly zero dual, including degenerate optima. Reading
    # phase I here would lose capacity opportunity cost and violate dual feasibility.
    dual = [0.0 if n + i in basis else max(0.0, tableau[-1][n + i]) for i in range(m)]
    return primal, dual, iterations


def shadow_prices(q, demand, cost, budget, price, *, value=None) -> dict:
    """Solve max sum x*(q*V-p*c), capacity <= B and class volume <= D.

    Assignment is task -> {agent: fractional runs}, so split/unmet demand is
    represented honestly. V defaults to one. Negative RHS/cost/price is invalid;
    missing/nonfinite inputs remain unmeasured, distinct from measured zero.
    """
    value = value or {}
    tasks = sorted({task for cells in q.values() for task in cells})
    reasons, invalid = [], []
    if not q or not tasks:
        reasons.append("no quality cells with n_obs >= 20")

    def check(label, v, lo=0, hi=None):
        if not _number(v):
            reasons.append(f"{label} unmeasured")
        elif v < lo or (hi is not None and v > hi):
            invalid.append(f"{label} invalid")

    for task in tasks:
        check(f"demand[{task}]", demand.get(task))
        check(f"value[{task}]", value.get(task, 1.0))
    for agent, cells in q.items():
        check(f"budget[{agent}]", budget.get(agent))
        check(f"price[{agent}]", price.get(agent))
        for task, quality in cells.items():
            check(f"quality[{agent},{task}]", quality, hi=1)
            check(f"cost[{agent},{task}]", cost.get(agent, {}).get(task))
    if reasons or invalid:
        return {
            "lambda": dict.fromkeys(q),
            "mu": dict.fromkeys(tasks),
            "assignment": dict.fromkeys(tasks),
            "iterations": 0,
            "status": "infeasible_inputs" if invalid else "inputs_unmeasured",
            "reasons": sorted(reasons + invalid),
        }
    agents = sorted(q)
    variables = [(a, k) for a in agents for k in sorted(q[a])]
    matrix = [[cost[a][k] if a == agent else 0.0 for a, k in variables] for agent in agents]
    matrix += [[float(k == task) for a, k in variables] for task in tasks]
    rhs = [budget[a] for a in agents] + [demand[k] for k in tasks]
    objective = [q[a][k] * value.get(k, 1.0) - price[a] * cost[a][k] for a, k in variables]
    primal, dual, iterations = _dense_simplex(matrix, rhs, objective)
    lam = dict(zip(agents, dual[: len(agents)]))
    assignment: dict = {k: {} for k in tasks}
    for (a, k), x in zip(variables, primal):
        if x > 0:
            assignment[k][a] = x
    return {
        "lambda": lam,
        "mu": dict(zip(tasks, dual[len(agents) :])),
        "assignment": assignment,
        "iterations": iterations,
        "status": "optimal",
        "reasons": [],
        "objective": sum(x * v for x, v in zip(primal, objective)),
        "usage": {a: sum(assignment[k].get(a, 0) * cost[a][k] for k in q[a]) for a in agents},
        "unmet_demand": {k: max(0.0, demand[k] - sum(assignment[k].values())) for k in tasks},
    }


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
        return [], [], [f"{db_path}: Brain database missing"]
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
        return [], [], [f"{db_path}: Brain inputs unavailable: {exc}"]


def _pacing(snapshot, agent):
    """Prefer the #680 agent contract, then accept the older pool contract."""
    agents = snapshot.get("agents")
    entry = agents.get(agent) if isinstance(agents, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("pacing"), dict):
        return agent, entry["pacing"], f"agents[{agent}].pacing"
    pools = snapshot.get("pools")
    mapped = [
        (name, row)
        for name, row in (pools.items() if isinstance(pools, dict) else [])
        if isinstance(row, dict) and row.get("agent") == agent
    ]
    if len(mapped) == 1:
        name, row = mapped[0]
        if isinstance(row.get("pacing"), dict):
            return name, row["pacing"], f"pools[{name}].pacing"
        return name, None, f"pools[{name}].pacing missing"
    return None, None, f"agents[{agent}].pacing missing; pools mapping absent or ambiguous"


def measure(
    db_path: Path, snapshot: dict | None, *, now=None, window_days=WINDOW_DAYS, q=None
) -> dict:
    """D is remaining-period runs; c is period fraction/run; B is remaining fraction.

    The earliest period_end among fully measured agents is the common horizon.
    Unmeasured agents are excluded from the LP and retain null prices/ranks.
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
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    observed = _timestamp(snapshot.get("generated_at", snapshot.get("ts")))
    cost: dict = {}
    budget: dict = {}
    price: dict = {}
    sources: dict = {}
    pool_ids: dict = {}
    ends: dict = {}
    measured_q: dict = {}
    for agent, cells in q.items():
        budget[agent], price[agent] = None, None
        cost[agent] = dict.fromkeys(cells)
        sources[agent] = {"source": None, "reason": None, "imputed": []}
        pool_id, pacing, location = _pacing(snapshot, agent)
        pool_ids[agent] = pool_id

        def missing(reason):
            sources[agent]["reason"] = f"{location}: {reason}"

        if pacing is None:
            missing("pacing unmeasured")
            continue
        if pacing.get("stale"):
            missing("stale=true; pacing unmeasured")
            continue
        stamp = _timestamp(
            pacing.get("observed_at", pacing.get("source_ts", pacing.get("ts", observed)))
        )
        end = _timestamp(pacing.get("period_end"))
        if (
            stamp is None
            or stamp > now
            or ("stale" not in pacing and now - stamp > STALE_AFTER_SECONDS)
        ):
            missing("observed_at/source_ts/ts absent, stale or in the future")
            continue
        if end is None or end <= now:
            missing("period_end missing or expired")
            continue
        if pacing.get("state") not in ("ok", "warn", "shed"):
            missing("state unmeasured or invalid")
            continue
        # Allowance-to-date is diagnostic; the LP spends remaining-period capacity.
        sources[agent]["allowed_fraction"] = pacing.get("allowed_fraction")
        if "effective_reserve" not in pacing:
            missing("effective_reserve missing")
            continue
        used, reserve = pacing.get("used_fraction"), pacing["effective_reserve"]
        if not _number(used) or used < 0:
            missing("used_fraction unmeasured or invalid")
            continue
        if not _number(reserve) or not 0 <= reserve <= 1:
            missing("effective_reserve unmeasured or invalid")
            continue
        remaining = (1 - reserve) - used
        budget[agent] = max(0.0, remaining)
        sources[agent]["over_reserve"] = remaining < 0
        burn_key = next(
            (key for key in ("burn_rate_per_day", "burn_per_day") if _number(pacing.get(key))), None
        )
        agent_runs = [row for row in runs if row["agent"] == agent]
        if burn_key is None or pacing[burn_key] < 0:
            missing("burn_rate_per_day/burn_per_day unmeasured or invalid")
            continue
        if not agent_runs:
            missing(f"{db_path}: runs[{agent}] window run count unmeasured")
            continue
        metered = agent == "muse" or pool_id.endswith(("paygo", "metered"))
        priced = [
            row["cost_usd"]
            for row in agent_runs
            if row["cost_source"] in COMPLETE_COST_SOURCES
            and _number(row["cost_usd"])
            and row["cost_usd"] >= 0
        ]
        coverage = len(priced) / len(agent_runs)
        price[agent] = (
            (sum(priced) / len(priced) if priced and coverage >= MIN_COST_COVERAGE else None)
            if metered
            else 0.0
        )
        if price[agent] is None:
            missing(f"{db_path}: costs[{agent}].cost_usd complete coverage unmeasured")
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
                sum(row["tokens_in"] + row["tokens_out"] for row in local) / len(local) / mean
                if local and mean is not None
                else 1.0
            )
            if not local:
                sources[agent]["imputed"].append(task)
            cost[agent][task] = pacing[burn_key] / (len(agent_runs) / window_days) * multiplier
        sources[agent][
            "source"
        ] = f"{location}.{burn_key} / runs_per_day; local token ratio (1.0 if imputed)"
        if not cells or any(not _number(v) or not 0 <= v <= 1 for v in cells.values()):
            missing(f"{db_path}: posterior[{agent}] unmeasured or invalid")
            continue
        measured_q[agent] = cells
        ends[agent] = end
    horizon = min(ends.values()) if ends else None
    seconds_left = horizon - now if horizon is not None else None
    demand = {
        task: (
            sum(row["task_type"] == task for row in runs) * seconds_left / (window_days * 86400)
            if seconds_left is not None and runs
            else None
        )
        for task in tasks
    }
    result = shadow_prices(measured_q, demand, cost, budget, price)
    if result["status"] == "optimal" and len(measured_q) < len(q):
        result["status"] = "partial"
    result["lambda"] = {agent: result["lambda"].get(agent) for agent in q}
    reasons += [f"{agent}: {row['reason']}" for agent, row in sources.items() if row["reason"]]
    reasons += result.get("reasons", [])
    ranks = comparative_ranks(q, cost, result["lambda"], price, assignment=result["assignment"])
    for rows in ranks.values():
        for row in rows:
            if row["comparative_score"] is None:
                row["reason"] = sources[row["agent"]]["reason"] or row.get("reason")
    result.update(
        schema=SCHEMA,
        reasons=sorted(set(reasons)),
        window_days=window_days,
        ranks=ranks,
        inputs={
            "pacing_observed_at": observed,
            "cost_source_by_agent": sources,
            "pool_by_agent": pool_ids,
            "demand": demand,
            "cost": cost,
            "budget": budget,
            "price": price,
            "period_end": horizon,
            "period_end_source": "earliest fully measured agent period_end",
            "seconds_left": seconds_left,
        },
    )
    return result


def history_path(state_dir: Path | None = None) -> Path:
    base = state_dir or Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
    return base / "capacity-ledger.ndjson"


def report(db_path: Path, path: Path, *, now=None, window_days=WINDOW_DAYS) -> dict:
    """Replay latest snapshot per UTC hour; hour failures do not veto the window."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    now = int(time.time()) if now is None else now
    hours: dict = {}
    errors: list[str] = []
    hour_reasons: dict = {}
    paths = [path]
    legacy = path.with_name("pacing-history.ndjson")
    if legacy != path and legacy.exists():
        paths.append(legacy)
    for source in paths:
        try:
            for line in source.read_text().splitlines():
                try:
                    snapshot = json.loads(line)
                    if (
                        source.name != "pacing-history.ndjson"
                        and snapshot.get("event") != "pacing_snapshot"
                    ):
                        continue
                    stamp = _timestamp(snapshot.get("ts", snapshot.get("generated_at")))
                    if stamp is not None and now - window_days * 86400 <= stamp <= now:
                        hour = int(stamp // 3600)
                        if hour not in hours or stamp > hours[hour][0]:
                            hours[hour] = (stamp, snapshot)
                except (ValueError, AttributeError):
                    errors.append(f"{source}: malformed pacing snapshot")
        except OSError as exc:
            errors.append(f"{source}: pacing_snapshot input unavailable ({exc.strerror})")
    weights, _, brain_errors = _window(db_path, now, window_days)
    errors += brain_errors
    q: dict = {}
    for row in weights:
        q.setdefault(row["agent"], {})[row["task_type"]] = row["posterior"]
    # The pass number is specifically the measured Codex/Claude comparison.
    required_q = {a: q[a] for a in ("codex", "claude") if a in q}
    for agent in ("codex", "claude"):
        if agent not in required_q:
            errors.append(f"{db_path}: posterior[{agent}] missing n_obs >= 20")
    classes: dict = {
        k: {
            "posterior_top": min(
                (a for a in required_q if k in required_q[a]), key=lambda a: (-required_q[a][k], a)
            ),
            "comparative_top": None,
            "hours_differing": None,
            "max_lambda": None,
            "unmet_demand": None,
        }
        for k in {k for cells in required_q.values() for k in cells}
    }
    maxima: dict = {}
    measured = 0
    for stamp, snapshot in sorted(hours.values()):
        result = measure(db_path, snapshot, now=int(stamp), window_days=window_days, q=required_q)
        if result["status"] != "optimal" or len(required_q) != 2:
            hour_reasons[str(int(stamp // 3600))] = result["reasons"] or [
                "codex/claude comparison unmeasured"
            ]
            continue
        measured += 1
        for agent, v in result["lambda"].items():
            maxima[agent] = max(maxima.get(agent, 0.0), v)
        for task, rows in result["ranks"].items():
            row = classes[task]
            row["comparative_top"] = rows[0]["agent"]
            row["unmet_demand"] = result["unmet_demand"][task]
            row["hours_differing"] = (row["hours_differing"] or 0) + int(
                rows[0]["agent"] != row["posterior_top"]
            )
            row["max_lambda"] = max(
                row["max_lambda"] or 0.0, *(result["lambda"][r["agent"]] for r in rows)
            )
    enough = bool(hours) and measured / len(hours) >= MIN_MEASURED_HOUR_SHARE
    differing = sum(row["hours_differing"] > 0 for row in classes.values()) if measured else None
    status = "optimal" if enough else "inputs_unmeasured"
    if enough and differing == 0 and all(v == 0 for v in maxima.values()):
        status = "not_testable_slack"
    if not hours:
        errors.append(f"{path}: no event=pacing_snapshot rows in window (ts required)")
    return {
        "classes": classes,
        "classes_differing": differing,
        "lambda": maxima or dict.fromkeys(q),
        "status": status,
        "reasons": sorted(set(errors)),
        "hour_reasons": hour_reasons,
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
    for hour, reasons in result.get("hour_reasons", {}).items():
        lines.append(f"hour {hour}: " + "; ".join(reasons))
    return "\n".join(lines)
