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
# #731 campaign: no_named_test / considered cells ≤ 25% at the 2026-10-25 producer-line reading.
CAMPAIGN_SHARE_MAX = 0.25
CAMPAIGN_SHARE_CHECK_DATE = "2026-10-25"


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


def record_skip(reason, *, target=None, now=None, test_class="none"):
    """Return whether a skip was recorded; unnamed tests count once per issue/UTC day.

    Class tallies reset with the UTC day so the campaign share (no_named_test / cells)
    is a same-day reading, not a lifetime sum that ratchets past any fixed threshold.
    """
    now = time.time() if now is None else now
    with locked_state():
        state = read_state()
        if reason == "no_named_test" and target:
            day = time.strftime("%Y%m%d", time.gmtime(now))
            if state.get("skip_day") != day:
                state.update(skip_day=day, skip_issues=[], no_named_test_classes={})
            if target in state["skip_issues"]:
                return False
            state["skip_issues"].append(target)
        if reason == "no_named_test":
            classes = state.setdefault("no_named_test_classes", {})
            cls = test_class if test_class in {"file-only", "ci-only", "none"} else "none"
            classes[cls] = classes.get(cls, 0) + 1
        skips = state.setdefault("skips", {})
        skips[reason] = skips.get(reason, 0) + 1
        save_state(state)
        return True


def no_named_test_day_counts(state, now=None):
    """Same-UTC-day unnamed-test skips; stale skip_issues from a prior day count as zero."""
    now = time.time() if now is None else now
    day = time.strftime("%Y%m%d", time.gmtime(now))
    if state.get("skip_day") != day:
        return {"count": 0, "classes": {}}
    return {
        "count": len(state.get("skip_issues") or []),
        "classes": {
            key: int(state.get("no_named_test_classes", {}).get(key, 0) or 0)
            for key in ("file-only", "ci-only", "none")
        },
    }


def no_named_test_share(unnamed, cells_total):
    """Share of considered cells skipped for lacking a named test; None when cells are unknown."""
    cells = int(cells_total or 0)
    if cells <= 0:
        return None
    return int(unnamed or 0) / cells


def project_no_named_test_share(items, cells_total):
    """Project the campaign share under current admission (unscoped bodies only)."""
    classes = Counter()
    unnamed = 0
    for item in items or []:
        body = item.get("body", "") if isinstance(item, dict) else ""
        if test_admission(body) != "unscoped":
            continue
        unnamed += 1
        classes[unnamed_skip_class(body) or "none"] += 1
    share = no_named_test_share(unnamed, cells_total)
    return {
        "no_named_test": unnamed,
        "cells_total": int(cells_total or 0),
        "share": share,
        "classes": {key: classes.get(key, 0) for key in ("file-only", "ci-only", "none")},
        "passes_campaign": share is not None and share <= CAMPAIGN_SHARE_MAX,
    }


def campaign_no_named_test_reading(metrics=None, *, now=None, items=None, cells_total=None):
    """Structured #731 campaign share for the 2026-10-25 producer-line reading.

    Prefer live ``producer_summary`` metrics (same-UTC-day skip_issues / cells). When ``items``
    are supplied — interim runners without producer state — project under current admission.
    """
    now = time.time() if now is None else now
    reading_day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if items is not None:
        projected = project_no_named_test_share(
            items, cells_total if cells_total is not None else 0
        )
        unnamed = projected["no_named_test"]
        cells = projected["cells_total"]
        share = projected["share"]
        classes = projected["classes"]
        source = "admission_projection"
    else:
        summary = metrics if metrics is not None else producer_summary()
        unnamed = summary.get("no_named_test")
        if unnamed is None:
            unnamed = (summary.get("skips") or {}).get("no_named_test", 0)
        cells = int(cells_total if cells_total is not None else summary.get("cells_total") or 0)
        share = summary.get("no_named_test_share")
        if share is None:
            share = no_named_test_share(unnamed, cells)
        classes = summary.get("no_named_test_classes") or {}
        source = "producer_summary"
    passes = share is not None and share <= CAMPAIGN_SHARE_MAX
    return {
        "check_date": CAMPAIGN_SHARE_CHECK_DATE,
        "reading_day": reading_day,
        "is_check_day": reading_day >= CAMPAIGN_SHARE_CHECK_DATE,
        "no_named_test": int(unnamed or 0),
        "cells_total": int(cells or 0),
        "share": share,
        "share_pct": None if share is None else f"{100 * share:.1f}%",
        "classes": {key: int(classes.get(key, 0) or 0) for key in ("file-only", "ci-only", "none")},
        "passes_campaign": passes,
        "threshold": CAMPAIGN_SHARE_MAX,
        "source": source,
    }


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
        if test_admission(item.get("body", "")) == "unscoped":
            skipped.append(
                {
                    "target": item["target"],
                    "reason": "no_named_test",
                    "test_class": unnamed_skip_class(item.get("body", "")) or "none",
                }
            )
    eligible = [i for i in eligible if test_admission(i.get("body", "")) != "unscoped"]
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
        import claims

        held = claims.holder(target) if target else None
        if (
            held
            and held.get("agent") == "research"
            and held.get("experiment_reservation") == Path(root).name
        ):
            claims.release(target, "research")
    from fleet_shapes import write_json_atomic

    write_json_atomic(Path(root) / "producer-delivery.json", delivery)
    return delivery


def named_test_paths(command):
    return list(
        dict.fromkeys(
            re.findall(r"tests/[\w./-]+(?:\.py(?:::[\w\[\]-]+)*|\.test\.js)", command or "")
        )
    )


def named_test(spec):
    """Only explicit fenced/backtick pytest/node commands or named test nodes qualify."""
    import shlex

    from synthesis_promotion import _safe_command

    commands = re.findall(
        r"`([^`\n]*(?:pytest|python3? [^`\n]*--selftest|node --test)[^`\n]*)`", spec or ""
    )
    for command in commands:
        stripped = command.strip()
        if not stripped.startswith(("pytest ", "python3 ", "python ", "node ", "/")):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        if tokens[:2] == ["node", "--test"]:
            if _safe_command(tokens):
                return stripped
            continue
        if _safe_command(tokens) and (named_test_paths(command) or "--selftest" in tokens):
            return stripped
    nodes = re.findall(r"tests/[\w./-]+\.py(?:::[\w\[\]-]+)*", spec or "")
    if nodes:
        return "python3 -m pytest -q " + " ".join(dict.fromkeys(nodes))
    javascript = re.findall(r"tests/[\w./-]+\.test\.js", spec or "")
    return "node --test " + " ".join(dict.fromkeys(javascript)) if javascript else None


def unnamed_skip_class(spec):
    """Classify a body with no named test: file-only, ci-only, or none.

    Preference order matches the #731 measure: Acceptance Criteria first, else the whole body.
    A test *file* path that admission still rejects (for example `.github/...test.js`) counts as
    file-only so the producer line can show why the pool was skipped.
    """
    if named_test(spec):
        return None
    section = re.search(
        r"(?ims)^#{1,6}\s+acceptance\s+criteria\s*\n(.*?)(?=^#{1,6}\s|\Z)", spec or ""
    )
    hay = section[1] if section else (spec or "")
    if re.search(r"(?:tests/[\w./-]+(?:\.py(?:::[\w\[\]-]*)*|\.test\.js)|[\w./-]+\.test\.js)", hay):
        return "file-only"
    if re.search(r"(?i)\bCI\b|tests? pass", hay):
        return "ci-only"
    return "none"


def test_admission(spec):
    """Classify the specification; only an explicit acceptance section permits derivation."""
    command = named_test(spec)
    if command:
        if re.search(r"`[^`]*pytest", spec or "") or re.search(r"`[^`]*node --test", spec or ""):
            return "named"
        return "file-only"
    section = re.search(r"(?ims)^#{1,6}\s+acceptance\s+criteria\s*\n(.*?)(?=^#{1,6}\s|\Z)", spec)
    if section and re.search(r"\w{3}", section[1]):
        return "ci-only" if re.search(r"(?i)\bCI\b|tests? pass", section[1]) else "none"
    return "unscoped"


@contextmanager
def experiment_lock(root):
    """Serialize stage advancement, including the intent-before-spawn window."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".producer-stage.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _stage_save(root, meta):
    from fleet_shapes import write_json_atomic

    write_json_atomic(Path(root) / "meta.json", meta)


def _stage_git(wt, args, repo):
    import worktree_git

    result = worktree_git.run(Path(wt), args, repo=repo, timeout=120)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout)[-1000:])
    return result.stdout.strip()


def stage_launch_allowed(meta):
    """A delayed launch rechecks ownership, the exact issue and current spare capacity."""
    import backlog
    import capacity
    import claims
    import research_scheduler

    if time.time() - meta["launched_ts"] >= PRODUCER_LOCK_MAX_HOURS * 3600:
        return "producer_expired"
    held = claims.holder(meta["subject"])
    if (
        not held
        or held.get("agent") != "research"
        or held.get("experiment_reservation") != meta["exp_id"]
    ):
        return "claim_lost"
    repo, number = meta["subject"].rsplit("#", 1)
    answer = gh_call(["gh", "api", f"repos/{repo}/issues/{number}"])
    try:
        issue = json.loads(answer["stdout"])
        if hashlib_sha(issue.get("body", "").encode()) != meta["spec_sha256"]:
            return "issue_spec_changed"
        labels = [x["name"] for x in issue.get("labels", []) if x["name"] != "status:in-progress"]
        if issue.get("state") != "open" or backlog.research_ownership_skip_reason(labels):
            return "subject_owned_or_closed"
    except (KeyError, TypeError, ValueError):
        return "issue_state_unknown"
    snapshot = capacity.build()
    pacing = capacity.experiment_pacing(snapshot)
    spare = research_scheduler.spare_capacity(
        research_scheduler.capacity_state_from_snapshot(snapshot)
    )
    demand = Counter(m["agent"] for m in meta["members"] if m.get("role") != "review")
    for agent, count in demand.items():
        if (
            pacing.get(agent) != "ok"
            or spare.get(agent, 0) - meta.get("production_reserve", {}).get(agent, 0) < count
        ):
            return "capacity_pending"
    return None


def hashlib_sha(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()


def start_test_derivation(meta, root, canon, *, spawn_fn):
    """Launch a separate tests-only execution; it is never a comparison member."""
    import exp_abcd

    spec = (Path(root) / "spec.md").read_text()
    if test_admission(spec) == "unscoped":
        raise ValueError("derivation requires acceptance criteria")
    member = next(m for m in meta["members"] if m.get("role") != "review")
    agent = member["agent"]
    path = "tests/test_experiment_" + meta["spec_sha256"][:16] + ".py"
    wt = exp_abcd.exp_worktree(meta["repo"], meta["exp_id"], agent, "derive-tests")
    branch = exp_abcd.exp_branch(meta["exp_id"], agent, "derive-tests")
    run_id = f"exp:{meta['exp_id']}:derive-tests"
    stage = meta["test_stage"] = {
        "phase": "derivation_pending",
        "test_source": "derived",
        "path": path,
        "worktree": str(wt),
        "branch": branch,
        "run_id": run_id,
        "agent": agent,
        "profile_id": member.get("profile_id"),
        "log": str(Path(root) / "derive-tests.log"),
    }
    _stage_save(root, meta)
    reason = stage_launch_allowed(meta)
    if reason:
        stage["wait_reason"] = reason
        _stage_save(root, meta)
        return {
            "exp_id": meta["exp_id"],
            "base_sha": meta["base_sha"],
            "launched": [],
            "test_stage": stage,
        }
    return launch_test_derivation(meta, root, canon, spawn_fn=spawn_fn)


def launch_test_derivation(meta, root, canon, *, spawn_fn):
    import exp_abcd
    import feedback

    stage = meta["test_stage"]
    wt = Path(stage["worktree"])
    if (wt / ".git").exists():
        if _stage_git(wt, ["rev-parse", "HEAD"], meta["repo"]) != meta["base_sha"] or _stage_git(
            wt, ["status", "--porcelain"], meta["repo"]
        ):
            raise ValueError("derivation_worktree_not_at_original_base")
    else:
        exp_abcd.provision._run(
            [
                "git",
                "-C",
                str(canon),
                "worktree",
                "add",
                "-b",
                stage["branch"],
                str(wt),
                meta["base_sha"],
            ]
        )
    prompt = (
        "Write and commit ONLY the NEW Python test file " + stage["path"] + ". "
        "Derive behavioral assertions from the Acceptance Criteria below, against existing production APIs. "
        "Do not implement the fix, change any existing file, runner/config, dependencies or git metadata. "
        "Do not push, open a PR, or contact GitHub. Tests must expose the requested behavior with real assertions, "
        "not unconditional failures, missing imports, skips or collection errors. Include the relevant acceptance "
        "criterion in each test docstring. Commit the tests even though they fail.\n\n"
        + (Path(root) / "spec.md").read_text()
    )
    stage.update(phase="deriving", launch_intent=True)
    _stage_save(root, meta)  # Unknown spawn outcomes never launch a second writer.
    feedback.record_run(
        stage["run_id"],
        meta["subject"],
        "testgen",
        stage["agent"],
        experiment_id=meta["exp_id"],
        routing_metadata={"producer_stage": "derive-tests"},
    )
    pid = spawn_fn(
        stage["agent"],
        exp_abcd.AGENT_MODE.get(stage["agent"], "full"),
        prompt,
        wt,
        Path(stage["log"]),
        run_id=stage["run_id"],
        target=meta["subject"],
        task_type="testgen",
        profile_id=(
            stage.get("profile_id")
            if stage.get("profile_id") in exp_abcd.execution_profiles.PROFILE_REGISTRY
            else None
        ),
    )
    stage["pid"] = pid
    _stage_save(root, meta)
    import claims

    claims.update_metadata(meta["subject"], "research", pids=[pid], refresh_ts=True)
    return {
        "exp_id": meta["exp_id"],
        "base_sha": meta["base_sha"],
        "launched": [{**stage}],
        "test_stage": stage,
    }


def freeze_derived_tests(meta, root):
    """Certify a tests-only F over B, with attributable assertion RED on unchanged production."""
    import ast
    import tempfile

    import local_verify

    stage = meta["test_stage"]
    wt, repo, base = Path(stage["worktree"]), meta["repo"], meta["base_sha"]
    if _stage_git(wt, ["status", "--porcelain"], repo):
        raise ValueError("derivation_worktree_dirty")
    head = _stage_git(wt, ["rev-parse", "HEAD"], repo)
    if stage.get("frozen_sha"):
        if (
            head != stage["frozen_sha"]
            or _stage_git(wt, ["rev-parse", head + ":" + stage["path"]], repo) != stage["test_blob"]
        ):
            raise ValueError("frozen_identity_change")
        return
    _stage_git(wt, ["merge-base", "--is-ancestor", base, head], repo)
    changed = _stage_git(wt, ["diff", "--name-only", base, head], repo).splitlines()
    if changed != [stage["path"]]:
        raise ValueError("derivation_must_change_only_the_named_new_test")
    if _stage_git(wt, ["ls-tree", base, "--", stage["path"]], repo):
        raise ValueError("derivation_must_not_replace_existing_tests")
    tree = _stage_git(wt, ["ls-tree", head, "--", stage["path"]], repo)
    if not tree.startswith("100644 blob "):
        raise ValueError("derived_test_not_regular_file")
    source = _stage_git(wt, ["show", head + ":" + stage["path"]], repo)
    parsed = ast.parse(source)
    tests = [
        n
        for n in ast.walk(parsed)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    ]
    if not tests or any(not ast.get_docstring(n) for n in tests):
        raise ValueError("derived_tests_need_acceptance_docstrings")
    assertions = [n for n in ast.walk(parsed) if isinstance(n, ast.Assert)]
    if not assertions or any(isinstance(n.test, ast.Constant) for n in assertions):
        raise ValueError("derived_tests_need_behavioral_assertions")
    with tempfile.TemporaryDirectory(prefix="producer-red-") as tmp:
        dest = Path(tmp)
        if error := local_verify._extract_base(wt, head, dest):
            raise ValueError(error)
        argv = [
            sys.executable,
            "-m",
            "pytest",
            stage["path"],
            "-v",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
        ]
        report, stdout = local_verify._run_argv(argv, dest, 120, env=local_verify._probe_env())
        nodes = local_verify._parse_node_outcomes(stdout)
        valid = (
            report.get("returncode") == 1
            and nodes
            and all(outcome == "FAILED" for _, outcome in nodes)
            and ("AssertionError" in stdout or re.search(r"(?m)^E\s+assert\b", stdout))
            and local_verify._pytest_failure_cause(report)["test_failure"]
        )
        from fleet_shapes import write_json_atomic

        write_json_atomic(
            Path(root) / "derived-red-proof.json",
            {
                "base_sha": base,
                "frozen_sha": head,
                "report": report,
                "nodes": nodes,
                "ok": bool(valid),
            },
        )
        if not valid:
            raise ValueError("derived_red_not_attributable_assertion_failure")
    blob = _stage_git(wt, ["rev-parse", head + ":" + stage["path"]], repo)
    stage.update(
        phase="tests_frozen",
        frozen_sha=head,
        test_blob=blob,
        command="python3 -m pytest -q " + stage["path"],
    )
    meta["comparison_base_sha"] = head
    _stage_save(root, meta)


def frozen_tests_intact(meta, wt):
    stage = meta.get("test_stage") or {}
    if stage.get("test_source") != "derived":
        return True
    if not stage.get("frozen_sha") or not stage.get("test_blob"):
        return False
    try:
        repo = meta["repo"]
        _stage_git(wt, ["merge-base", "--is-ancestor", stage["frozen_sha"], "HEAD"], repo)
        committed = _stage_git(wt, ["rev-parse", "HEAD:" + stage["path"]], repo)
        actual = _stage_git(wt, ["hash-object", "--", stage["path"]], repo)
        return (
            committed == actual == stage["test_blob"]
            and not (Path(wt) / stage["path"]).is_symlink()
        )
    except (ValueError, OSError):
        return False


def advance_test_stage(meta, root, *, launch_fn, spawn_fn):
    """Resume one exact derivation or comparison launch; callers hold experiment_lock."""
    import exp_abcd

    stage = meta["test_stage"]
    if stage["phase"] == "comparison_running":
        return None
    if stage["phase"] == "refused":
        return {"status": "discarded", "reason": stage["reason"]}
    canon = exp_abcd.provision.ensure_canonical(meta["repo"])
    if stage["phase"] == "derivation_pending":
        if reason := stage_launch_allowed(meta):
            return {"status": reason}
        launch_test_derivation(meta, root, canon, spawn_fn=spawn_fn)
        return {"status": "deriving"}
    if stage["phase"] == "deriving":
        try:
            done = json.loads((Path(root) / "done" / (stage["run_id"] + ".json")).read_text())
        except (OSError, ValueError):
            return {"status": "derivation_terminal_unknown"}
        if done.get("run_id") != stage["run_id"] or done.get("rc_of") != "agent":
            return {"status": "derivation_terminal_unknown"}
        try:
            if done.get("rc") != 0:
                raise ValueError("derivation_agent_failed")
            freeze_derived_tests(meta, root)
        except (ValueError, OSError, SyntaxError) as exc:
            stage.update(phase="refused", reason=str(exc))
            _stage_save(root, meta)
            return {"status": "discarded", "reason": str(exc)}
    if reason := stage_launch_allowed(meta):
        return {"status": reason}
    launched = launch_fn(
        meta, root, canon, (Path(root) / "spec.md").read_text(), seed=stage["frozen_sha"]
    )
    if any(row.get("state") == "launch-unknown" for row in launched):
        return {"status": "comparison_launch_unknown"}
    stage["phase"] = "comparison_running"
    _stage_save(root, meta)
    with locked_state():
        state = read_state()
        state["derived_tests"] = state.get("derived_tests", 0) + 1
        save_state(state)
    return {"status": "comparison_running"}


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
        day = no_named_test_day_counts(state)
        cells_total = state.get("cells_total", 0)
        metrics.update(
            cells_fresh=state.get("cells_fresh", 0),
            cells_total=cells_total,
            skips=state.get("skips", {}),
            no_named_test=day["count"],
            no_named_test_classes=day["classes"],
            no_named_test_share=no_named_test_share(day["count"], cells_total),
            derived_tests=state.get("derived_tests", 0),
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
    parser.add_argument(
        "--campaign-share",
        action="store_true",
        help="print the #731 no_named_test/cells campaign reading as JSON",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        assert draw(3600, 0, 2, DAILY_CAP, random.Random(1))["reason"] == "daily_cap"
        assert (
            named_test("`tests/test_a.py::test_x`")
            == "python3 -m pytest -q tests/test_a.py::test_x"
        )
        assert named_test("`node --test tests/foo.test.js`") == "node --test tests/foo.test.js"
        assert unnamed_skip_class("## Acceptance Criteria\n- Tests pass in CI.\n") == "ci-only"
        assert no_named_test_share(2, 37) == 2 / 37
        projected = project_no_named_test_share(
            [
                {"body": "no tests and no acceptance criteria"},
                {"body": "## Acceptance Criteria\n- Tests pass in CI.\n"},
                {"body": "tests/test_x.py"},
            ],
            37,
        )
        assert projected["no_named_test"] == 1 and projected["passes_campaign"]
        check_day = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc).timestamp()
        reading = campaign_no_named_test_reading(
            {"no_named_test": 2, "cells_total": 37, "no_named_test_share": 2 / 37},
            now=check_day,
        )
        assert reading["is_check_day"] and reading["passes_campaign"]
        print("experiment_producer.py selftest: OK (cap and frozen named test)")
    elif args.trial_defaults:
        print(*trial_defaults())
    elif args.trial_verdict:
        print(trial_verdict())
    elif args.campaign_share:
        print(json.dumps(campaign_no_named_test_reading(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
