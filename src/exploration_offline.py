#!/usr/bin/env python3
"""Size exploration from historical weights; this is not a causal policy evaluation.

Each graded run is a hypothetical exploration opportunity under neutral capacity.
We compare seeded challengers conditional on the epsilon gate firing, then scale
the counts and posterior differences by the unchanged default exploration rate.
Historical capacity, assignment propensities and counterfactual outcomes are not
available, so these are sizing estimates, never observed PASS improvements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import exploration_review
import feedback
import router


def _version_at(connection: sqlite3.Connection, timestamp: int) -> int | None:
    # A version is a batch. Do not use any of its rows until the entire batch is
    # in force; order by activation time, with version breaking same-second ties.
    row = connection.execute(
        "SELECT version FROM route_weights GROUP BY version HAVING MAX(ts)<=? "
        "ORDER BY MAX(ts) DESC, version DESC LIMIT 1",
        (timestamp,),
    ).fetchone()
    return int(row[0]) if row else None


def build_report(
    *,
    db_path: Path | None = None,
    window_days: int = 90,
    now: int | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    now = int(time.time()) if now is None else now
    since = now - window_days * 86400
    database = feedback.DB_PATH if db_path is None else db_path
    report: dict[str, Any] = {
        "kind": "sizing_estimate",
        "read_only": True,
        "window_start": since,
        "window_end": now,
        "seed": seed,
        "exploration_rate": router.EXPLORATION_RATE_DEFAULT,
        "assumptions": "neutral capacity; paired seeded challengers conditional on exploration; "
        "posterior-implied PASS proxy, not observed or causal uplift",
        "runs": [],
        "task_types": {},
    }
    # Opening read-only avoids creating or migrating the Brain during analysis.
    with closing(sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")  # keep runs and all weight versions in one consistent snapshot
        runs = c.execute(
            "SELECT r.run_id, r.ts, r.task_type, r.source, r.mode, o.durability, "
            "o.adjudicated_verdict, o.verifier_verdict, o.failure_class "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.ts>=? AND r.ts<=? ORDER BY r.ts, r.run_id",
            (since, now),
        ).fetchall()
        weights: dict[tuple[int, str], list[dict]] = {}
        for run in runs:
            if not feedback._has_outcome_evidence(
                run["durability"],
                run["adjudicated_verdict"],
                run["verifier_verdict"],
                failure_class=run["failure_class"],
            ):
                continue
            task_type = run["task_type"]
            stat = report["task_types"].setdefault(
                task_type,
                {
                    "graded_runs": 0,
                    "compared_runs": 0,
                    "different_challengers": 0,
                    "skipped_runs": 0,
                    "posterior_pass_difference": 0.0,
                },
            )
            stat["graded_runs"] += 1
            version = _version_at(c, run["ts"])
            result = {
                "run_id": run["run_id"],
                "task_type": task_type,
                "ts": run["ts"],
                "route_weights_version": version,
            }
            report["runs"].append(result)
            if version is None:
                result["skip_reason"] = "no_weights_in_force"
            else:
                key = (version, task_type)
                if key not in weights:
                    weights[key] = [
                        dict(row)
                        for row in c.execute(
                            "SELECT agent, posterior, score, n_obs FROM route_weights "
                            "WHERE version=? AND task_type=? ORDER BY score DESC, agent",
                            key,
                        )
                    ]
                rows = weights[key]
                if not rows:
                    result["skip_reason"] = "no_task_weights_in_force"
                else:
                    learned = exploration_review._learned(rows)
                    posterior = {row["agent"]: row["posterior"] for row in rows}
                    paired_seed = int.from_bytes(
                        hashlib.sha256(f"{seed}:{run['run_id']}".encode()).digest()[:8], "big"
                    )
                    # Ingest also reuses older orchestrator dispatch rows. Their
                    # source can differ from keepalive, but local/reserve seats
                    # must still stay out of hypothetical remote challengers.
                    remote = (
                        run["source"] in {"keepalive", "orchestrator_remote"}
                        or run["mode"] == "remote"
                        or run["run_id"].startswith("remote:")
                    )
                    picks: dict[str, Any] = {}
                    for mode in ("epsilon-greedy", "thompson-hybrid"):
                        picks[mode] = router.select_agent(
                            task_type,
                            exploration_review._neutral_capacity(),
                            learned=learned,
                            only=(
                                router.KEEPALIVE_AGENTS - router.RESERVE_AGENTS if remote else None
                            ),
                            exploration_rate=1.0,
                            exploration_mode=mode,
                            rng=random.Random(paired_seed),
                            simulate=True,
                            profile_transport="remote" if remote else "local",
                        )
                    epsilon, thompson = picks.values()
                    if not all(pick and pick["exploration"] for pick in picks.values()):
                        result["skip_reason"] = "no_same_tier_challenger"
                    elif any(posterior.get(pick["agent"]) is None for pick in picks.values()):
                        result["skip_reason"] = "missing_challenger_posterior"
                    else:
                        difference = float(posterior[thompson["agent"]]) - float(
                            posterior[epsilon["agent"]]
                        )
                        different = epsilon["agent"] != thompson["agent"]
                        result.update(
                            epsilon_challenger=epsilon["agent"],
                            thompson_challenger=thompson["agent"],
                            different_challengers=different,
                            posterior_pass_difference=difference,
                        )
                        stat["compared_runs"] += 1
                        stat["different_challengers"] += int(different)
                        stat["posterior_pass_difference"] += difference
            if "skip_reason" in result:
                stat["skipped_runs"] += 1
    rate = report["exploration_rate"]
    for stat in report["task_types"].values():
        stat["expected_exploration_decisions"] = stat["compared_runs"] * rate
        stat["expected_different_challengers"] = stat["different_challengers"] * rate
        stat["expected_pass_difference"] = stat["posterior_pass_difference"] * rate
    return report


def format_human(report: dict) -> str:
    lines = [
        f"exploration_offline: sizing estimate (epsilon={report['exploration_rate']:.1%})",
        report["assumptions"],
    ]
    for task_type, stat in sorted(report["task_types"].items()):
        lines.append(
            f"  {task_type}: graded={stat['graded_runs']} compared={stat['compared_runs']} "
            f"different_challengers={stat['different_challengers']} skipped={stat['skipped_runs']} "
            f"expected_exploration={stat['expected_exploration_decisions']:.2f} "
            f"expected_different={stat['expected_different_challengers']:.2f} "
            f"posterior_implied_expected_PASS_difference={stat['expected_pass_difference']:+.4f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=feedback.DB_PATH)
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(db_path=args.db, window_days=args.window_days, seed=args.seed)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_human(report))


if __name__ == "__main__":
    main()
