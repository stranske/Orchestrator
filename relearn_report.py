#!/usr/bin/env python3
"""relearn_report.py - run the feedback learner and report learned routing beliefs.

The router's ROUTE_TABLE is the hand-set prior. This periodic job turns that prior into
task_type_priors, runs the feedback learner to write a new versioned route_weights row set,
then reports where learned ordering now agrees or disagrees with the prior order.

`--dry-run` computes the same priors and reports which learner/version would be used, but
does not write route_weights. `--selftest` runs offline against a temp feedback store.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from pathlib import Path

import feedback
import router


def priors_from_route_table(route_table) -> dict:
    """Build {task_type: {agent: prior_success}} from a ROUTE_TABLE-shaped dict.

    Duplicate agents within a task_type keep their first/best rank.
    """
    out = {}
    for task_type, spec in route_table.items():
        priors = {}
        rank = 0
        for entry in spec.get("agents", []):
            agent = entry["agent"]
            if agent in priors:
                continue
            priors[agent] = round(max(0.45, 0.70 - 0.05 * rank), 2)
            rank += 1
        out[task_type] = priors
    out.update(_role_priors_from_registry(out))
    return out


def blend_bt_priors(task_type_priors: dict, *, bt_fn=None) -> dict:
    """item 16(g) (2026-07-08): warm-start priors from the A/B/C duel data. Where Bradley-Terry
    strengths are data-ready for a task type, re-rank that task's agents by strength, map ranks
    onto the SAME 0.45-0.70 band the table uses, and blend 50/50 with the hand-set prior — the
    table keeps half its voice (it encodes owner intent), the duels supply the other half.
    Not-ready task types pass through untouched. Returns {task_type: comparisons} for reporting."""
    bt_fn = bt_fn or feedback.bt_strengths
    used: dict = {}
    for task_type, priors in task_type_priors.items():
        try:
            bt = bt_fn(task_type=task_type)
        except Exception:
            continue
        if not bt.get("ready"):
            continue
        strengths = bt.get("strengths") or {}
        ranked = [a for a in sorted(strengths, key=lambda a: -strengths[a]) if a in priors]
        if not ranked:
            continue
        for rank, agent in enumerate(ranked):
            bt_prior = max(0.45, 0.70 - 0.05 * rank)
            priors[agent] = round((priors[agent] + bt_prior) / 2, 3)
        used[task_type] = bt.get("comparisons", 0)
    return used


def _role_priors_from_registry(task_priors: dict) -> dict:
    """Derive role:<name> priors from each role's route_as task prior.

    Roles reuse an existing task_type as the capacity/routing prior, but role
    outcomes are learned on their own task_type surface so PromptAgent,
    RedirectAgent, etc. do not contaminate ordinary review/implement weights.
    """
    try:
        import roles
    except Exception:
        return {}

    role_priors = {}
    for role_name, role in getattr(roles, "ROLE_REGISTRY", {}).items():
        base = task_priors.get(role.route_as) or {}
        priors = {agent: prior for agent, prior in base.items() if agent in role.eligible_backends}
        if priors:
            role_priors[feedback.role_task_type(role_name)] = priors
    return role_priors


def _quality_learner_writes_current_weight_table() -> bool:
    """Best-effort guard for preferring relearn_quality only when it feeds current_weights."""
    if not hasattr(feedback, "relearn_quality"):
        return False
    try:
        qsrc = inspect.getsource(feedback.relearn_quality)
        csrc = inspect.getsource(feedback.current_weights)
    except (OSError, TypeError):
        return False
    return "route_weights" in qsrc and "route_weights" in csrc


def _learner():
    if _quality_learner_writes_current_weight_table():
        return (
            "feedback.relearn_quality",
            feedback.relearn_quality,
            (
                "preferred continuous-reward learner; it writes route_weights read by current_weights"
            ),
        )
    return (
        "feedback.relearn",
        feedback.relearn,
        ("binary outcome learner; no compatible relearn_quality was found"),
    )


def _max_version() -> int:
    with feedback._conn() as c:
        return c.execute("SELECT COALESCE(MAX(version),0) FROM route_weights").fetchone()[0]


def _version_exists(version: int | None) -> bool:
    if version is None or version < 1:
        return False
    with feedback._conn() as c:
        row = c.execute(
            "SELECT 1 FROM route_weights WHERE version=? LIMIT 1", (version,)
        ).fetchone()
    return row is not None


def _rank_map(order: list[str]) -> dict:
    return {agent: i + 1 for i, agent in enumerate(order)}


def _ordered_rows(rows: list[dict], prior_order: list[str]) -> list[dict]:
    """Keep learned score order deterministic when scores tie."""
    prior_rank = {agent: i for i, agent in enumerate(prior_order)}
    return sorted(
        rows,
        key=lambda r: (
            -(r["score"] if r.get("score") is not None else -1.0),
            prior_rank.get(r["agent"], len(prior_rank)),
            r["agent"],
        ),
    )


def _task_report(
    task_type: str, priors: dict, new_version: int, previous_version: int | None
) -> dict:
    prior_order = list(priors)
    prior_ranks = _rank_map(prior_order)

    new_rows = _ordered_rows(feedback.current_weights(task_type, new_version), prior_order)
    previous_rows = (
        _ordered_rows(feedback.current_weights(task_type, previous_version), prior_order)
        if previous_version is not None
        else []
    )
    previous_order = (
        [row["agent"] for row in previous_rows] if previous_version is not None else None
    )
    previous_ranks = _rank_map(previous_order or [])

    learned_order = [row["agent"] for row in new_rows]
    learned_ranks = _rank_map(learned_order)
    cold_start = bool(new_rows) and all((row.get("n_obs") or 0) == 0 for row in new_rows)
    diverges = False if cold_start else learned_order != prior_order

    rows = []
    for row in new_rows:
        agent = row["agent"]
        learned_rank = learned_ranks[agent]
        prior_rank = prior_ranks.get(agent)
        previous_rank = previous_ranks.get(agent)
        rows.append(
            {
                "agent": agent,
                "prior": priors.get(agent),
                "posterior": row.get("posterior"),
                "score": row.get("score"),
                "n_obs": row.get("n_obs"),
                "learned_rank": learned_rank,
                "prior_rank_delta": (learned_rank - prior_rank) if prior_rank is not None else None,
                "previous_rank_delta": (
                    (learned_rank - previous_rank) if previous_rank is not None else None
                ),
            }
        )

    return {
        "task_type": task_type,
        "prior_order": prior_order,
        "learned_order": learned_order,
        "previous_order": previous_order,
        "diverges_from_prior": diverges,
        "cold_start": cold_start,
        "note": (
            "cold start: all n_obs are 0, posterior equals the prior, and nothing is learned yet"
            if cold_start
            else None
        ),
        "rows": rows,
    }


def build_report(window_days: int = 90, *, dry_run: bool = False, route_table=None) -> dict:
    route_table = route_table or router.ROUTE_TABLE
    task_type_priors = priors_from_route_table(route_table)
    bt_blended = blend_bt_priors(task_type_priors)
    learner_name, learn, learner_note = _learner()
    before_version = _max_version()
    previous_version = before_version if before_version >= 1 else None
    would_write_version = before_version + 1

    if dry_run:
        tasks = []
        for task_type, priors in task_type_priors.items():
            tasks.append(
                {
                    "task_type": task_type,
                    "prior_order": list(priors),
                    "rows": [
                        {"agent": agent, "prior": prior, "prior_rank": i + 1}
                        for i, (agent, prior) in enumerate(priors.items())
                    ],
                }
            )
        return {
            "generated_at": int(time.time()),
            "dry_run": True,
            "db_path": str(feedback.DB_PATH),
            "window_days": window_days,
            "learner": learner_name,
            "learner_note": learner_note,
            "current_version": previous_version,
            "would_write_version": would_write_version,
            "new_version": None,
            "previous_version": previous_version,
            "tasks": tasks,
        }

    new_version = learn(task_type_priors, window_days=window_days)
    previous_version = new_version - 1 if _version_exists(new_version - 1) else None
    return {
        "generated_at": int(time.time()),
        "dry_run": False,
        "db_path": str(feedback.DB_PATH),
        "window_days": window_days,
        "learner": learner_name,
        "learner_note": learner_note,
        "bt_blended": bt_blended,  # 16(g): task_types whose priors were duel-warm-started
        "new_version": new_version,
        "previous_version": previous_version,
        "tasks": [
            _task_report(task_type, priors, new_version, previous_version)
            for task_type, priors in task_type_priors.items()
        ],
    }


def _fmt_num(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.3f}"


def _fmt_delta(value) -> str:
    if value is None:
        return "n/a"
    if value > 0:
        return f"+{value}"
    return str(value)


def format_human(report: dict) -> str:
    lines = []
    if report["dry_run"]:
        lines.append(
            f"relearn_report dry-run: learner={report['learner']} window_days={report['window_days']} "
            f"would_write_version={report['would_write_version']}"
        )
        lines.append(report["learner_note"])
        lines.append("No route_weights version was written.")
        lines.append("")
        for task in report["tasks"]:
            lines.append(f"{task['task_type']}: prior order")
            for row in task["rows"]:
                lines.append(
                    f"  {row['prior_rank']}. {row['agent']} prior={_fmt_num(row['prior'])}"
                )
        return "\n".join(lines)

    lines.append(
        f"relearn_report: learner={report['learner']} window_days={report['window_days']} "
        f"wrote_version={report['new_version']} previous_version={report['previous_version'] or 'none'}"
    )
    lines.append(report["learner_note"])
    lines.append(
        "Rank deltas are learned_rank minus comparison_rank; negative means the agent rose."
    )

    for task in report["tasks"]:
        flag = " DIVERGED from ROUTE_TABLE prior" if task["diverges_from_prior"] else ""
        lines.append("")
        lines.append(f"{task['task_type']}:{flag}")
        lines.append(f"  prior:   {' > '.join(task['prior_order'])}")
        lines.append(f"  learned: {' > '.join(task['learned_order'])}")
        if task["previous_order"] is not None:
            lines.append(
                f"  previous:{' > '.join(task['previous_order']) if task['previous_order'] else ' none'}"
            )
        if task["note"]:
            lines.append(f"  {task['note']}")
        lines.append("  agent       posterior  score      n_obs  rank  d_prior  d_prev")
        for row in task["rows"]:
            lines.append(
                f"  {row['agent']:<11} {_fmt_num(row['posterior']):>9}  "
                f"{_fmt_num(row['score']):>9}  {_fmt_num(row['n_obs']):>5}  "
                f"{row['learned_rank']:>4}  {_fmt_delta(row['prior_rank_delta']):>7}  "
                f"{_fmt_delta(row['previous_rank_delta']):>6}"
            )
    return "\n".join(lines)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _selftest():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="relearn-report-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    try:
        sample = {
            "x": {
                "role": "code",
                "agents": [
                    {"agent": "a"},
                    {"agent": "b"},
                    {"agent": "a"},
                    {"agent": "c"},
                    {"agent": "d"},
                    {"agent": "e"},
                    {"agent": "f"},
                    {"agent": "g"},
                ],
            }
        }
        priors = priors_from_route_table(sample)
        assert list(priors["x"]) == ["a", "b", "c", "d", "e", "f", "g"], priors
        assert priors["x"]["a"] == 0.70 and priors["x"]["b"] == 0.65, priors
        assert priors["x"]["f"] == 0.45 and priors["x"]["g"] == 0.45, priors

        route_table = {
            "implement": {
                "role": "code",
                "agents": [
                    {"agent": "prior_top", "mode": "full", "late": False},
                    {"agent": "evidence_wins", "mode": "full", "late": False},
                    {"agent": "prior_top", "mode": "frontier", "late": True},
                ],
            }
        }
        task_priors = priors_from_route_table(route_table)
        learner_name, learn, _ = _learner()
        v0 = learn(task_priors, window_days=90)
        assert v0 == 1, v0

        for i in range(12):
            rid = f"win-{i}"
            exp = f"exp-win-{i}"
            feedback.record_run(rid, "o/r#1", "implement", "evidence_wins", experiment_id=exp)
            feedback.record_outcome(
                rid, adjudicated_verdict="PASS", merged=True, durability="durable"
            )
            feedback.record_evaluation(exp, "evidence_wins", f"judge-{i}", 10.0)

            rid = f"lose-{i}"
            exp = f"exp-lose-{i}"
            feedback.record_run(rid, "o/r#2", "implement", "prior_top", experiment_id=exp)
            feedback.record_outcome(
                rid, adjudicated_verdict="PASS", merged=True, durability="reverted"
            )
            feedback.record_evaluation(exp, "prior_top", f"judge-{i}", 1.0)

        report = build_report(window_days=90, route_table=route_table)
        assert report["learner"] == learner_name, report["learner"]

        # 16(g): duel-ready task types blend BT rank into the table prior (50/50 on the same
        # 0.45-0.70 band); not-ready types pass through untouched; injected fit, pure.
        bt_priors = {
            "implement": {"cursor": 0.70, "codex": 0.65},
            "review": {"cursor": 0.70, "codex": 0.65},
        }

        def fake_bt(task_type=None, **_kw):
            if task_type == "implement":
                return {
                    "ready": True,
                    "comparisons": 12,
                    "strengths": {"codex": 1.6, "cursor": 0.4},
                }
            return {"ready": False, "comparisons": 1}

        used = blend_bt_priors(bt_priors, bt_fn=fake_bt)
        assert used == {"implement": 12}, used
        # codex (BT rank 0 -> 0.70) blends 0.65 -> 0.675; cursor (rank 1 -> 0.65) 0.70 -> 0.675
        assert bt_priors["implement"] == {"cursor": 0.675, "codex": 0.675}, bt_priors
        assert bt_priors["review"] == {"cursor": 0.70, "codex": 0.65}, bt_priors
        assert report["new_version"] == 2, report["new_version"]
        task = report["tasks"][0]
        assert task["learned_order"][0] == "evidence_wins", task
        assert task["diverges_from_prior"] is True, task
        weights = feedback.current_weights("implement", report["new_version"])
        assert weights and weights[0]["agent"] == "evidence_wins", weights
        assert _max_version() == report["new_version"], report

        outcome_route_table = {
            "outcome_only": {
                "role": "code",
                "agents": [
                    {"agent": "prior_agent", "mode": "full", "late": False},
                    {"agent": "prod_agent", "mode": "full", "late": False},
                ],
            }
        }
        for i in range(12):
            prior_rid = f"outcome-prior-{i}"
            prod_rid = f"outcome-prod-{i}"
            feedback.record_run(prior_rid, "o/r#prior", "outcome_only", "prior_agent")
            feedback.record_run(prod_rid, "o/r#prod", "outcome_only", "prod_agent")
            feedback.record_outcome(
                prior_rid, adjudicated_verdict="PASS", merged=True, durability="reverted"
            )
            feedback.record_outcome(
                prod_rid, adjudicated_verdict="PASS", merged=True, durability="durable"
            )
        outcome_report = build_report(window_days=90, route_table=outcome_route_table)
        outcome_task = outcome_report["tasks"][0]
        assert outcome_report["learner"] == learner_name, outcome_report["learner"]
        assert outcome_task["learned_order"][0] == "prod_agent", outcome_task
        assert outcome_task["rows"][0]["n_obs"] == 12, outcome_task

        print(
            "relearn_report.py selftest: OK "
            f"(priors deduped, {learner_name} wrote versioned weights, "
            "eval and production evidence override prior)"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run feedback relearning and report route weights."
    )
    parser.add_argument("--window-days", type=_positive_int, default=90)
    parser.add_argument("--json", action="store_true", help="print JSON instead of human text")
    parser.add_argument(
        "--dry-run", action="store_true", help="compute priors without writing weights"
    )
    parser.add_argument("--selftest", action="store_true", help="run offline selftest")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    report = build_report(window_days=args.window_days, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
