#!/usr/bin/env python3
"""Bounded comparative intake for the existing research/ABCD lifecycle.

Dedup: research_scheduler owns admission, exp_abcd owns member identity and judging,
synthesis_promotion owns delivery. This rail adds cadence and coverage selection only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

COVERAGE_DAYS = 21
DAILY_CAP = 4
WINNER_MERGE_GRACE_DAYS = 7
# A pending verdict whose unknowns never resolve (a 404 winner PR, GitHub unreachable) must not keep
# the producer ON forever: past grace + this bound it resolves to a persisted fail, toward OFF.
UNRESOLVED_UNKNOWN_DAYS = 14
TRIAL_PASS_MERGED = 6
PRODUCER_FOLLOWUP_DAYS = 30
PRODUCER_LOCK_MAX_HOURS = 72
TERMINAL_STATUSES = {"merged", "no_winner", "discarded", "expired", "member_pushed"}


def state_path():
    return (
        Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
        / "experiment-producer.json"
    )


def read_state():
    try:
        return json.loads(state_path().read_text())
    except FileNotFoundError:
        return {}


def save_state(state):
    from fleet_shapes import write_json_atomic

    write_json_atomic(state_path(), state)


@contextmanager
def locked_state():
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def draw(now, last_draw_ts, rate_per_day, launched_today, rng, *, pools=None, cells=()):
    """Bernoulli approximation to Poisson arrivals, bounded per UTC day.

    Pure when called alone. cadence_draw persists every draw, including pacing skips:
    skipped capacity never accumulates a deferred burst.
    """
    elapsed = max(0, min(86400, now - last_draw_ts))
    p = -math.expm1(-max(0, rate_per_day) * elapsed / 86400)
    debt = sum(c.get("age_days", math.inf) > COVERAGE_DAYS for c in cells)
    drainable = sum(
        c.get("age_days", math.inf) > COVERAGE_DAYS and c.get("pacing", "unknown") == "ok"
        for c in cells
    )
    blocked = {k: v for k, v in (pools or {}).items() if v != "ok"}
    if blocked:
        reason = "pacing " + ", ".join(f"{k}={v}" for k, v in sorted(blocked.items()))
        print(
            f"skipped: {reason}; coverage debt {debt} cells > 21d, drainable {drainable}",
            file=sys.stderr,
        )
        return {"fire": False, "p": p, "reason": reason}
    if launched_today >= DAILY_CAP:
        return {"fire": False, "p": p, "reason": "daily_cap"}
    fire = rng.random() < p
    return {"fire": fire, "p": p, "reason": "draw" if fire else "no_draw"}


def cadence_draw(now, rate, rng, *, pools=None, cells=(), skip_reason=None, count_skip=True):
    with locked_state():
        state = read_state()
        day = time.strftime("%Y%m%d", time.gmtime(now))
        count = state.get("launched_today", 0) if state.get("day") == day else 0
        result = (
            {"fire": False, "p": 0, "reason": skip_reason}
            if skip_reason
            else draw(
                now,
                state.get("last_draw_ts", now - 3600),
                rate,
                count,
                rng,
                pools=pools,
                cells=cells,
            )
        )
        state.update(
            day=day,
            last_draw_ts=now,
            launched_today=count,
            cells_fresh=sum(c.get("age_days", math.inf) <= COVERAGE_DAYS for c in cells),
            cells_total=len(cells),
        )
        if not result["fire"] and count_skip:
            reasons = state.setdefault("skips", {})
            reasons[result["reason"]] = reasons.get(result["reason"], 0) + 1
        save_state(state)
        return result


def record_launch(now=None):
    now = time.time() if now is None else now
    with locked_state():
        state = read_state()
        day = time.strftime("%Y%m%d", time.gmtime(now))
        count = state.get("launched_today", 0) if state.get("day") == day else 0
        state.update(day=day, launched_today=count + 1)
        save_state(state)


def record_skip(reason, *, target=None, now=None):
    """Return whether a skip was recorded; unnamed tests count once per issue/UTC day."""
    now = time.time() if now is None else now
    with locked_state():
        state = read_state()
        if reason == "no_named_test" and target:
            day = time.strftime("%Y%m%d", time.gmtime(now))
            if state.get("skip_day") != day:
                state.update(skip_day=day, skip_issues=[])
            if target in state["skip_issues"]:
                return False
            state["skip_issues"].append(target)
        skips = state.setdefault("skips", {})
        skips[reason] = skips.get(reason, 0) + 1
        save_state(state)
        return True


def choose_subject(feed, forum, cells, rng):
    if not feed:
        return None
    forum = [i for i in forum if i in feed]
    if forum and rng.random() < 0.5:
        return {"subject": min(forum, key=lambda i: i.get("created_at", "")), "reason": "forum"}
    classes = {i.get("task_type", "implement") for i in feed}
    relevant = [c for c in cells if c["task_type"] in classes]
    stale = [c for c in relevant if c.get("age_days", math.inf) > COVERAGE_DAYS]
    if stale:
        task_type = max(stale, key=lambda c: c.get("age_days", math.inf))["task_type"]
        reason = "coverage"
    else:
        choices = sorted(classes)
        weights = [
            max([c.get("age_days", 1) for c in relevant if c["task_type"] == t] or [1])
            for t in choices
        ]
        task_type = rng.choices(choices, weights=[max(1, min(w, 365)) for w in weights])[0]
        reason = "stratified"
    return {
        "subject": rng.choice([i for i in feed if i.get("task_type", "implement") == task_type]),
        "reason": reason,
    }


def choose_arms(task_type, ranking, cells, rng):
    rows = [c for c in cells if c["task_type"] == task_type]
    agents = {c["agent"] for c in rows}
    order = [a for a in ranking if a in agents]
    if len(agents) < 2:
        return []
    incumbent = (order or sorted(agents))[0]
    challenger = max(
        (c for c in rows if c["agent"] != incumbent), key=lambda c: c.get("age_days", math.inf)
    )["agent"]
    arms = [
        {"arm_id": f"agent-{a}", "agents": [a], "strategy": "single"}
        for a in (incumbent, challenger)
    ]
    if rng.random() < 0.25:
        arms.append(
            {
                "arm_id": f"brief-{incumbent}",
                "agents": [incumbent],
                "strategy": "single",
                "brief": "Start with a minimal failing named test, then implement the smallest complete fix.",
            }
        )
    return arms


def route_cells(task_types, agents, *, conn=None, now=None):
    """Only head-to-head evaluations refresh coverage; ordinary outcomes do not."""
    import feedback

    close = conn is None
    db = conn or feedback._conn()
    try:
        rows = db.execute(
            "SELECT r.task_type, e.implementation_agent, MAX(e.ts) FROM evaluations_v2 e JOIN runs r ON r.experiment_id=e.experiment_id AND r.agent=e.implementation_agent WHERE e.experiment_id IN (SELECT experiment_id FROM evaluations_v2 GROUP BY experiment_id HAVING COUNT(DISTINCT implementer_arm_id)>=2 AND COUNT(DISTINCT evaluator_id)>=2) GROUP BY r.task_type, e.implementation_agent"
        ).fetchall()
        stamps = {(t, a): ts for t, a, ts in rows}
    finally:
        if close:
            db.close()
    now = time.time() if now is None else now
    return [
        {
            "task_type": t,
            "agent": a,
            "age_days": (now - stamps[(t, a)]) / 86400 if (t, a) in stamps else math.inf,
        }
        for t in task_types
        for a in agents
    ]


def trial_window_end(state):
    """One deadline for launch admission, trial cohort, and verdict grace periods.

    The persisted start pins the full 21-day window to the second. The shell's date
    is a fallback before that start exists, never an override of an existing trial.
    """
    if state.get("trial_start"):
        return state["trial_start"] + COVERAGE_DAYS * 86400
    end = os.environ.get("ORCH_EXPERIMENT_TRIAL_END", "0")
    if end == "0":
        return math.inf
    return datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()


def trial_launch_hold(now=None):
    state = read_state()
    end = trial_window_end(state)
    now = time.time() if now is None else now
    verdict = state.get("trial_verdict", "pending")
    if verdict == "fail":
        reason = state.get("trial_reason", "insufficient_merged_winners")
        return f"producer: trial verdict fail ({reason}); launches stopped — owner decision"
    if now >= end and verdict != "pass":
        ended = time.strftime("%Y%m%d", time.gmtime(end))
        resolves = time.strftime(
            "%Y%m%d",
            time.gmtime(end + (WINNER_MERGE_GRACE_DAYS + UNRESOLVED_UNKNOWN_DAYS) * 86400),
        )
        return (
            f"producer: trial window ended {ended}; verdict pending "
            f"(resolves by {resolves}); launches held"
        )
    return None


def build_plan(
    items, cap, *, learned, claimed_targets, excluded_targets, production_reserve, rng, conn=None
):
    import backlog
    import capacity
    import research_scheduler
    import router

    hold = trial_launch_hold()
    if hold:
        print(hold, file=sys.stderr)
        return {
            "planned": [],
            "skipped": [{"reason": hold}],
            "candidates": [],
            "producer_cells": [],
        }

    spare = research_scheduler.spare_capacity(research_scheduler.capacity_state_from_snapshot(cap))
    skipped = []
    eligible = [
        i
        for i in items
        if i.get("target")
        and not i["target"].lower().startswith("stranske/collab-admin#")
        and i["target"] not in claimed_targets | (excluded_targets or set())
        and i.get("lane", "research") in {"research", "opener"}
        and not backlog.research_ownership_skip_reason(i.get("labels") or [])
    ]
    for item in eligible:
        if not named_test(item.get("body", "")):
            skipped.append({"target": item["target"], "reason": "no_named_test"})
    eligible = [i for i in eligible if named_test(i.get("body", ""))]
    classes = set(router.ROUTE_TABLE)
    cells = route_cells(classes, cap.get("agents", {}), conn=conn)
    cells = [
        c
        for c in cells
        if c["agent"]
        in {
            entry["agent"] for entry in router.ROUTE_TABLE.get(c["task_type"], {}).get("agents", [])
        }
        and c["agent"] not in router.BACKUP_AGENTS
    ]
    pacing = capacity.experiment_pacing(cap)
    for c in cells:
        c["pacing"] = pacing.get(c["agent"], "unknown")
    feed_classes = {i.get("task_type", "implement") for i in eligible}
    candidate_cells = [c for c in cells if c["pacing"] == "ok" and c["task_type"] in feed_classes]
    debt = sum(c.get("age_days", math.inf) > COVERAGE_DAYS for c in cells)
    drainable = sum(c.get("age_days", math.inf) > COVERAGE_DAYS for c in candidate_cells)
    blocked = {a: p for a, p in pacing.items() if p != "ok"}
    if blocked:
        reason = "pacing " + ", ".join(f"{a}={p}" for a, p in sorted(blocked.items()))
        print(f"{reason}; coverage debt {debt} cells > 21d, drainable {drainable}", file=sys.stderr)
        if not candidate_cells:
            return {
                "planned": [],
                "skipped": skipped + [{"reason": reason}],
                "candidates": [],
                "producer_cells": cells,
            }
    viable = {
        c["task_type"]
        for c in candidate_cells
        if len({r["agent"] for r in candidate_cells if r["task_type"] == c["task_type"]}) >= 2
    }
    eligible = [i for i in eligible if i.get("task_type", "implement") in viable]
    forum = [
        i
        for i in eligible
        if "experiment:forum" in i.get("labels", [])
        or len(re.findall(r"(?m)^\s*- \[[ xX]\]", i.get("body", ""))) >= 9
    ]
    choice = choose_subject(eligible, forum, candidate_cells, rng)
    if not choice:
        return {
            "planned": [],
            "skipped": skipped + [{"reason": "no_subject"}],
            "candidates": [],
            "producer_cells": cells,
        }
    item = choice["subject"]
    task = item.get("task_type", "implement")
    ranks = research_scheduler.learned_posteriors(task, list(cap.get("agents", {})), learned)
    order = sorted(
        ranks,
        key=lambda a: ((learned or {}).get(task, {}).get(a, {}).get("rank", math.inf), -ranks[a]),
    )
    arms = choose_arms(task, order, candidate_cells, rng)
    selected = [a for arm in arms for a in arm["agents"]]
    pools = {}
    for agent in selected:
        state = pacing.get(agent, "unknown")
        if "=" in state:
            pool, pool_state = state.split("=", 1)
            pools[pool] = pool_state
        else:
            pools[agent] = state
    for agent, n in Counter(selected).items():
        if spare.get(agent, 0) - (production_reserve or {}).get(agent, 0) < n:
            pools[agent] = "production_reserve"
    job = {
        "item": item,
        "target": item["target"],
        "task_type": task,
        "arms": list(dict.fromkeys(selected)),
        "producer_arms": arms,
        "trigger": choice["reason"],
        "pools": pools,
    }
    return {
        "planned": [job] if len(arms) >= 2 else [],
        "candidates": [],
        "skipped": skipped,
        "producer_cells": cells,
    }


def gh_call(argv):
    """Best-effort GitHub mutation/read. Callers may inject this boundary offline."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode:
            if argv[2:4] == ["-X", "DELETE"] and "HTTP 404" in (proc.stderr or ""):
                return {"stdout": ""}
            return {"error": (proc.stderr or proc.stdout)[-500:]}
        return {"stdout": proc.stdout}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}


def issue_status(target, running, *, gh_fn=None):
    if not target or "#" not in target:
        return {"error": "no subject"}
    repo, number = target.rsplit("#", 1)
    endpoint = f"repos/{repo}/issues/{number}/labels"
    argv = (
        ["gh", "api", "-X", "POST", endpoint, "-f", "labels[]=status:in-progress"]
        if running
        else ["gh", "api", "-X", "DELETE", endpoint + "/status%3Ain-progress"]
    )
    return (gh_fn or gh_call)(argv)


def release_lock(root, target, delivery, *, gh_fn=None, now=None):
    """Release once, retrying only failures. Persist before returning to the caller."""
    if delivery.get("lock_released_ts") is not None:
        return delivery
    delivery["issue_status"] = issue_status(target, False, gh_fn=gh_fn)
    if "error" not in delivery["issue_status"]:
        delivery["lock_released_ts"] = time.time() if now is None else now
    from fleet_shapes import write_json_atomic

    write_json_atomic(Path(root) / "producer-delivery.json", delivery)
    return delivery


def named_test_paths(command):
    return list(dict.fromkeys(re.findall(r"tests/[\w./-]+\.py(?:::[\w\[\]-]+)*", command or "")))


def named_test(spec):
    """Only explicit fenced/backtick pytest commands or named test nodes qualify."""
    commands = re.findall(r"`([^`\n]*(?:pytest|python3? [^`\n]*--selftest)[^`\n]*)`", spec)
    for command in commands:
        if command.strip().startswith(("pytest ", "python3 ", "python ", "/")):
            import shlex

            from synthesis_promotion import _safe_command

            try:
                tokens = shlex.split(command)
                if _safe_command(tokens) and (named_test_paths(command) or "--selftest" in tokens):
                    return command.strip()
            except ValueError:
                pass
    nodes = re.findall(r"tests/[\w./-]+\.py(?:::[\w\[\]-]+)*", spec)
    return "python3 -m pytest -q " + " ".join(dict.fromkeys(nodes)) if nodes else None


def producer_summary(exp_dir=None, *, gh_fn=None):
    import exp_abcd

    metrics = dict(
        launched=0,
        post_window_launches=0,
        evaluated=0,
        winner_prs=0,
        winner_merged=0,
        cells_fresh=0,
        cells_total=0,
    )
    errors = []
    try:
        trial_state = read_state()
    except (ValueError, OSError):
        trial_state = {}
    start = trial_state.get("trial_start", 0)
    end = trial_window_end(trial_state)
    unreadable_meta = 0
    for path in Path(exp_dir or exp_abcd.EXP_DIR).glob("*/meta.json"):
        # Only a meta that READS as a producer experiment can make the verdict unknown; a corrupt
        # legacy meta.json is counted for the report and never holds the trial pending.
        try:
            meta = json.loads(path.read_text())
        except (ValueError, OSError):
            unreadable_meta += 1
            continue
        if not isinstance(meta, dict) or meta.get("producer") != "experiment_producer":
            continue
        try:
            if meta.get("launched_ts", 0) > end:
                metrics["post_window_launches"] += 1
            if not start <= meta.get("launched_ts", 0) <= end:
                continue
            metrics["launched"] += 1
            root = path.parent
            decision = root / "producer-delivery.json"
            if not decision.exists():
                continue
            delivery = json.loads(decision.read_text())
            metrics["evaluated"] += int(delivery.get("scored_arms", 0) >= 2)
            if delivery.get("pr_url"):
                metrics["winner_prs"] += 1
                answer = (gh_fn or gh_call)(
                    ["gh", "api", f"repos/{meta['repo']}/pulls/{delivery['pr_number']}"]
                )
                if "error" in answer:
                    errors.append("merge_state_unknown")
                else:
                    merge_state = json.loads(answer["stdout"])
                    if "merged_at" not in merge_state or merge_state.get("merge_state_unknown"):
                        errors.append("merge_state_unknown")
                        continue
                    merged = merge_state["merged_at"]
                    if merged:
                        metrics["winner_merged"] += int(
                            datetime.fromisoformat(merged.replace("Z", "+00:00")).timestamp()
                            <= end + WINNER_MERGE_GRACE_DAYS * 86400
                        )
        except (ValueError, OSError, KeyError):
            errors.append("unreadable_experiment")
    try:
        state = read_state()
        metrics.update(
            cells_fresh=state.get("cells_fresh", 0),
            cells_total=state.get("cells_total", 0),
            skips=state.get("skips", {}),
        )
    except (ValueError, OSError):
        metrics["skips"] = {"unreadable_state": 1}
    metrics["unknowns"] = sorted(errors)
    metrics["unreadable_meta"] = unreadable_meta
    return metrics


def trial_defaults(now=None, *, active=False):
    """Start only on an active producer tick; shadow/default queries are read-only."""
    now = time.time() if now is None else now
    with locked_state():
        state = read_state()
        if not state.get("trial_end") and active:
            state.update(
                trial_start=now,
                trial_end=time.strftime("%Y%m%d", time.gmtime(now + COVERAGE_DAYS * 86400)),
            )
            save_state(state)
            print(
                f"[experiment producer] deployed; trial end {state['trial_end']}", file=sys.stderr
            )
        return state.get(
            "trial_end", time.strftime("%Y%m%d", time.gmtime(now + COVERAGE_DAYS * 86400))
        ), state.get("trial_verdict", "pending")


def _cached_verdict(state, now):
    # A terminal verdict is immutable, including when later GitHub reads would flake.
    if state.get("trial_verdict") in {"pass", "fail"}:
        return state["trial_verdict"]
    if (
        not state.get("trial_start")
        or now < trial_window_end(state) + WINNER_MERGE_GRACE_DAYS * 86400
    ):
        return "pending"
    if state.get("last_verdict_check") == time.strftime("%Y%m%d", time.gmtime(now)):
        return "pending"
    return None


def trial_verdict(now=None, *, summary_fn=None):
    now = time.time() if now is None else now
    cached = _cached_verdict(read_state(), now)
    if cached is not None:
        return cached
    with locked_state():
        state = read_state()
        # Another tick may have checked while we waited for the lock.
        cached = _cached_verdict(state, now)
        if cached is not None:
            return cached
        day = time.strftime("%Y%m%d", time.gmtime(now))
        # Stamp before polling so even an interrupted/failed check is bounded daily.
        state["last_verdict_check"] = day
        save_state(state)
        metrics = (summary_fn or producer_summary)()
        unknown = len(metrics.get("unknowns") or [])
        merged = int(metrics.get("winner_merged") or 0)
        resolve_ts = (
            trial_window_end(state) + (WINNER_MERGE_GRACE_DAYS + UNRESOLVED_UNKNOWN_DAYS) * 86400
        )
        reason = None
        if merged >= TRIAL_PASS_MERGED:
            verdict = "pass"  # unknowns can only ADD merges, never remove counted ones
        elif unknown and now < resolve_ts:
            verdict = "pending"
        elif unknown:
            verdict, reason = "fail", "unresolved_unknown"
        else:
            verdict = "fail"
        state.update(trial_verdict=verdict, trial_metrics=metrics, trial_unknown=unknown)
        if verdict == "pending":
            state.setdefault("pending_since", day)
            state["resolves_on"] = time.strftime("%Y%m%d", time.gmtime(resolve_ts))
        if reason:
            state["trial_reason"] = reason
        save_state(state)
        if verdict == "pending":
            print(
                f"[experiment producer] verdict pending since {state['pending_since']}, "
                f"unknown {unknown}, resolves fail on {state['resolves_on']}",
                file=sys.stderr,
            )
        return verdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial-defaults", action="store_true")
    parser.add_argument("--trial-verdict", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        assert draw(3600, 0, 2, DAILY_CAP, random.Random(1))["reason"] == "daily_cap"
        assert (
            named_test("`tests/test_a.py::test_x`")
            == "python3 -m pytest -q tests/test_a.py::test_x"
        )
        print("experiment_producer.py selftest: OK (cap and frozen named test)")
    elif args.trial_defaults:
        print(*trial_defaults())
    elif args.trial_verdict:
        print(trial_verdict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
