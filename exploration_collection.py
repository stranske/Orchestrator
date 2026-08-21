#!/usr/bin/env python3
"""exploration_collection.py - opt-in supervised exploration collection windows.

This is Stage 2 of the exploration evidence plan. It builds a bounded dispatch
window for evidence collection, but stays dry-run by default. Active dispatch
requires:

  1. --apply
  2. --confirm-window
  3. ORCH_EXPLORATION_EVIDENCE=1

The command filters to low-risk opener work, applies a temporary exploration
mode/rate only inside the window, rejects late/paygo assignments, and never
changes the router default.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import claims
import dispatcher
import exploration_evidence_plan
import feedback
import router

MODE_CHOICES = ("auto", "epsilon-greedy", "thompson-hybrid")
ENV_FLAG = "ORCH_EXPLORATION_EVIDENCE"
MAX_RATE = exploration_evidence_plan.SUPERVISED_COLLECTION_RATE
DEFAULT_MIN_EXPLORATORY_DISPATCHES = 1
DEFAULT_SEED_SEARCH_LIMIT = 128


def _safe_rate(raw: float | None, fallback: float) -> float:
    value = fallback if raw is None else raw
    return max(0.0, min(MAX_RATE, float(value)))


def _deficit_by_mode(plan: dict) -> dict[str, int]:
    return {
        row["mode"]: int(row.get("remaining_outcome_runs") or 0)
        for row in plan.get("direct_mode_deficits") or []
    }


def choose_mode(plan: dict, requested: str = "auto", *, now: int | None = None) -> str:
    requested = (requested or "auto").replace("_", "-").lower()
    if requested in {"epsilon", "epsilon-greedy"}:
        return "epsilon-greedy"
    if requested in {"thompson", "thompson-hybrid"}:
        return "thompson-hybrid"
    deficits = _deficit_by_mode(plan)
    eps = deficits.get("epsilon-greedy", 0)
    th = deficits.get("thompson-hybrid", 0)
    if eps > th:
        return "epsilon-greedy"
    if th > eps:
        return "thompson-hybrid"
    # Tie: alternate by UTC week so repeated automatic windows do not starve a mode.
    now = int(time.time()) if now is None else now
    week_index = now // (7 * 86400)
    return "epsilon-greedy" if week_index % 2 == 0 else "thompson-hybrid"


def _recommended_task_types(plan: dict) -> list[str]:
    return [
        row["task_type"]
        for row in plan.get("candidate_task_types") or []
        if row.get("recommended")
    ][:3]


def _filter_backlog(
    plan: dict,
    backlog_payload: dict | None,
    max_items: int,
    *,
    exclude_targets: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    items = exploration_evidence_plan._backlog_items(backlog_payload=backlog_payload)
    allowed = set(_recommended_task_types(plan))
    exclude_targets = exclude_targets or set()
    selected: list[dict] = []
    skipped: list[dict] = []
    for item in items:
        task_type = item.get("task_type") or "implement"
        target = item.get("target") or ""
        reason = ""
        if target in exclude_targets:
            reason = "target excluded from this supervised window"
        elif item.get("lane") != "opener":
            reason = "not opener lane"
        elif task_type not in allowed:
            reason = "task type not recommended for this collection window"
        elif task_type not in exploration_evidence_plan.LOW_RISK_OPENER_TASK_TYPES:
            reason = "task type is not low-risk for supervised collection"
        if reason:
            skipped.append({"target": target, "task_type": task_type, "lane": item.get("lane"), "reason": reason})
            continue
        selected.append({
            "target": target,
            "task_type": task_type,
            "lane": "opener",
            "labels": item.get("labels") or [],
            "title": item.get("title") or "",
            "body": item.get("body") or "",
        })
        if len(selected) >= max_items:
            break
    return selected, skipped


def _entry_is_late(task_type: str, assignment: dict) -> bool:
    for entry in (router.ROUTE_TABLE.get(task_type, {}) or {}).get("agents") or []:
        if entry.get("agent") == assignment.get("agent") and entry.get("mode") == assignment.get("mode"):
            return bool(entry.get("late"))
    return False


def _sanitize_decision(decision: dict, *, require_exploration: bool = False) -> tuple[dict, list[dict]]:
    kept = []
    rejected = []
    for assignment in decision.get("assignments") or []:
        reason = ""
        if assignment.get("lane") != "opener":
            reason = "not opener lane"
        elif assignment.get("task_type") not in exploration_evidence_plan.LOW_RISK_OPENER_TASK_TYPES:
            reason = "not a low-risk supervised-collection task type"
        elif assignment.get("agent") in router.BACKUP_AGENTS:
            reason = "backup/paygo agent is not allowed in supervised exploration windows"
        elif _entry_is_late(assignment.get("task_type") or "", assignment):
            reason = "late/paygo route entry is not allowed in supervised exploration windows"
        elif require_exploration and not assignment.get("exploration"):
            reason = "non-exploratory assignment does not collect direct exploration evidence"
        if reason:
            rejected.append({"assignment": assignment, "reason": reason})
        else:
            kept.append(assignment)
    clean = {**decision, "assignments": kept}
    notes = list(clean.get("notes") or [])
    if rejected:
        notes.append(f"rejected {len(rejected)} unsafe supervised-window assignments")
    clean["notes"] = notes
    return clean, rejected


def _release_rejected_claims(rejected: list[dict]) -> None:
    for row in rejected:
        assignment = row.get("assignment") or {}
        target = assignment.get("target")
        agent = assignment.get("agent")
        if target and agent:
            try:
                claims.release(target, agent)
            except Exception:
                pass


def _release_assignments(assignments: list[dict]) -> None:
    for assignment in assignments:
        target = assignment.get("target")
        agent = assignment.get("agent")
        if target and agent:
            try:
                claims.release(target, agent)
            except Exception:
                pass


@contextmanager
def _temporary_router_env(*, mode: str, rate: float):
    old_mode = os.environ.get("ORCH_EXPLORATION_MODE")
    old_rate = os.environ.get("ORCH_EXPLORATION_RATE")
    os.environ["ORCH_EXPLORATION_MODE"] = mode
    os.environ["ORCH_EXPLORATION_RATE"] = f"{rate:.3f}"
    try:
        yield
    finally:
        if old_mode is None:
            os.environ.pop("ORCH_EXPLORATION_MODE", None)
        else:
            os.environ["ORCH_EXPLORATION_MODE"] = old_mode
        if old_rate is None:
            os.environ.pop("ORCH_EXPLORATION_RATE", None)
        else:
            os.environ["ORCH_EXPLORATION_RATE"] = old_rate


@contextmanager
def _temporary_random_seed(seed: int | None):
    if seed is None:
        yield
        return
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


def _plan_for_seed(
    *,
    selected: list[dict],
    capacity_snapshot: dict,
    mode: str,
    rate_value: float,
    cap: int,
    dry_run: bool,
    seed: int | None,
    require_exploration: bool,
) -> tuple[dict, list[dict]]:
    with _temporary_router_env(mode=mode, rate=rate_value), _temporary_random_seed(seed):
        decision = router.plan(
            selected,
            capacity_snapshot,
            max_concurrent=cap,
            dry_run=dry_run,
            learned=router.learned_ranks(),
        )
    return _sanitize_decision(decision, require_exploration=require_exploration)


def _exploratory_count(decision: dict) -> int:
    return sum(1 for assignment in decision.get("assignments") or [] if assignment.get("exploration"))


def _issue_number(target: str) -> str:
    match = re.search(r"#(\d+)\s*$", target or "")
    return match.group(1) if match else ""


def _issue_context(item: dict) -> tuple[str, str, str, str]:
    target = item.get("target") or ""
    issue = _issue_number(target)
    closes = f"Closes #{issue}" if issue else f"References {target}"
    title = item.get("title") or target
    body = (item.get("body") or "").strip()
    return target, closes, title, body


def _testgen_direct_evidence_prompt(item: dict) -> str:
    target, closes, title, body = _issue_context(item)
    return (
        f"Complete {target} as focused test-generation work for the exact issue below.\n\n"
        f"Issue title: {title}\n\n"
        "Issue body:\n"
        f"{body or '(no issue body available)'}\n\n"
        "Workflow:\n"
        "- Read the issue title/body above first; do not infer a different target from nearby backlog issues.\n"
        "- Add focused pytest coverage for the requested module/behavior only.\n"
        "- Keep production-code changes out unless a tiny, clearly explained testability fix is unavoidable.\n"
        "- Run the validation command named by the issue, plus any narrow formatting/static check needed for touched files.\n"
        "- Commit the implementation, push the branch, and open a PR that includes the validation command/results and "
        f"`{closes}`.\n"
        "- If the issue cannot be completed from local repo context, report the blocker plainly and leave the worktree clean."
    )


def _direct_evidence_prompt(item: dict) -> str | None:
    """Return an issue-completion prompt for task types whose default prompt is insufficient.

    `dispatcher.py` intentionally treats codemod as a campaign-planning lane for
    normal range rollout. Direct exploration evidence needs outcome-bearing
    opener work, so codemod evidence windows must ask for an issue-completing PR
    while preserving `task_type=codemod` in the feedback row.

    Testgen's normal dispatcher prompt is outcome-oriented, but it does not carry
    the issue body. Supervised windows may contain several similar testgen
    issues, so direct evidence assignments also need explicit issue context to
    prevent agents from implementing the wrong queued item.
    """

    task_type = item.get("task_type") or ""
    if task_type == "testgen":
        return _testgen_direct_evidence_prompt(item)
    if task_type != "codemod":
        return None
    target, closes, title, body = _issue_context(item)
    return (
        f"Complete {target} as an issue implementation, not just a codemod campaign plan.\n\n"
        f"Issue title: {title}\n\n"
        "Issue body:\n"
        f"{body or '(no issue body available)'}\n\n"
        "Workflow:\n"
        "- Implement the requested refactor in the repository with the smallest scoped diff that satisfies the issue.\n"
        "- Add or update the focused tests named by the issue when they are part of the acceptance criteria.\n"
        "- Do not stop after producing a standalone campaign JSON, dry-run plan, or advisory report; those are not sufficient evidence for this collection window.\n"
        "- Run the validation command named by the issue, plus any narrow formatting/static check needed for touched files.\n"
        "- Commit the implementation, push the branch, and open a PR that includes the validation command/results and "
        f"`{closes}`.\n"
        "- If the issue cannot be completed from local repo context, report the blocker plainly and leave the worktree clean."
    )


def _attach_direct_evidence_prompts(decision: dict, selected: list[dict]) -> dict:
    by_target = {item.get("target"): item for item in selected}
    assignments = []
    for assignment in decision.get("assignments") or []:
        item = by_target.get(assignment.get("target"))
        prompt = _direct_evidence_prompt(item or {})
        if prompt and not assignment.get("prompt"):
            assignment = {**assignment, "prompt": prompt}
        assignments.append(assignment)
    return {**decision, "assignments": assignments}


def _seed_candidates(rng_seed: int | None, seed_search_limit: int) -> list[int | None]:
    if rng_seed is not None:
        return [rng_seed]
    limit = max(0, int(seed_search_limit))
    return list(range(limit)) or [None]


def build_window(
    *,
    requested_mode: str = "auto",
    rate: float | None = None,
    max_dispatches: int | None = None,
    backlog_payload: dict | None = None,
    capacity_payload: dict | None = None,
    dry_run: bool = True,
    rng_seed: int | None = None,
    min_exploratory_dispatches: int = DEFAULT_MIN_EXPLORATORY_DISPATCHES,
    seed_search_limit: int = DEFAULT_SEED_SEARCH_LIMIT,
    exclude_targets: list[str] | None = None,
) -> dict:
    acquisition = exploration_evidence_plan.build_plan(
        backlog_payload=backlog_payload,
        capacity_payload=capacity_payload,
    )
    mode = choose_mode(acquisition, requested_mode)
    window_rows = {
        row["mode"]: row
        for row in (acquisition.get("supervised_collection") or {}).get("windows") or []
    }
    window = window_rows.get(mode) or {}
    window_cap = int(window.get("max_exploratory_dispatches_per_day") or exploration_evidence_plan.SUPERVISED_COLLECTION_DAILY_CAP)
    cap = max(0, min(window_cap, int(max_dispatches or window_cap)))
    selected, skipped = _filter_backlog(
        acquisition,
        backlog_payload,
        cap,
        exclude_targets=set(exclude_targets or []),
    )
    rate_value = _safe_rate(rate, exploration_evidence_plan.SUPERVISED_COLLECTION_RATE)
    capacity_snapshot = exploration_evidence_plan._capacity_snapshot(capacity_payload=capacity_payload)
    min_exploratory = max(0, int(min_exploratory_dispatches))

    blocked_reasons = []
    if not window.get("eligible"):
        blocked_reasons.append("acquisition planner does not currently mark this mode eligible")
    if not selected:
        blocked_reasons.append("no eligible low-risk opener backlog items selected")
    if cap <= 0:
        blocked_reasons.append("max dispatch cap is zero")
    if min_exploratory <= 0:
        blocked_reasons.append("min exploratory dispatches must be at least one")

    decision = {
        "generated_at": int(time.time()),
        "dry_run": True,
        "assignments": [],
        "lane_cap": 0,
        "pressure": False,
        "backoff_ticks": 1,
        "shed": [],
        "notes": ["supervised exploration collection window blocked"],
    }
    rejected_assignments: list[dict] = []
    seed_search = {
        "selected_seed": None,
        "attempted": 0,
        "limit": max(0, int(seed_search_limit)),
        "min_exploratory_dispatches": min_exploratory,
        "best_exploratory_dispatches": 0,
        "found": False,
    }
    if not blocked_reasons:
        best_decision = None
        best_rejected: list[dict] = []
        best_count = -1
        for seed in _seed_candidates(rng_seed, seed_search_limit):
            probe_decision, probe_rejected = _plan_for_seed(
                selected=selected,
                capacity_snapshot=capacity_snapshot,
                mode=mode,
                rate_value=rate_value,
                cap=cap,
                dry_run=True,
                seed=seed,
                require_exploration=True,
            )
            count = _exploratory_count(probe_decision)
            seed_search["attempted"] += 1
            if count > best_count:
                best_count = count
                best_decision = probe_decision
                best_rejected = probe_rejected
                seed_search["selected_seed"] = seed
                seed_search["best_exploratory_dispatches"] = count
            if count >= min_exploratory:
                seed_search["found"] = True
                break

        if not seed_search["found"]:
            blocked_reasons.append(
                f"no router seed produced {min_exploratory} direct exploration assignment(s)"
            )
            if best_decision is not None:
                decision = best_decision
                rejected_assignments = best_rejected
        elif dry_run:
            decision = best_decision or decision
            rejected_assignments = best_rejected
        else:
            decision, rejected_assignments = _plan_for_seed(
                selected=selected,
                capacity_snapshot=capacity_snapshot,
                mode=mode,
                rate_value=rate_value,
                cap=cap,
                dry_run=False,
                seed=seed_search["selected_seed"],
                require_exploration=True,
            )
        if not dry_run and rejected_assignments:
            _release_rejected_claims(rejected_assignments)
        if not dry_run and _exploratory_count(decision) < min_exploratory:
            _release_assignments(decision.get("assignments") or [])
            blocked_reasons.append(
                "active claim race left too few direct exploration assignments to dispatch"
            )
            decision = {**decision, "assignments": []}
    decision = _attach_direct_evidence_prompts(decision, selected)

    return {
        "generated_at": int(time.time()),
        "read_only": dry_run,
        "active_dispatch": not dry_run,
        "mode": mode,
        "requested_mode": requested_mode,
        "exploration_rate": rate_value,
        "max_dispatches": cap,
        "min_exploratory_dispatches": min_exploratory,
        "direct_exploration_assignments": _exploratory_count(decision),
        "seed_search": seed_search,
        "eligible": (
            not blocked_reasons
            and bool(decision.get("assignments"))
            and _exploratory_count(decision) >= min_exploratory
        ),
        "blocked_reasons": blocked_reasons,
        "selected_backlog": selected,
        "skipped_backlog": skipped[:20],
        "exclude_targets": list(exclude_targets or []),
        "rejected_assignments": rejected_assignments,
        "decision": decision,
        "acquisition_stage": acquisition.get("stage"),
        "acquisition_next_action": acquisition.get("next_action"),
        "safety": {
            "requires_apply": True,
            "requires_confirm_window": True,
            "requires_env": f"{ENV_FLAG}=1",
            "opener_only": True,
            "low_risk_task_types": sorted(exploration_evidence_plan.LOW_RISK_OPENER_TASK_TYPES),
            "late_paygo_allowed": False,
            "default_policy_change": False,
        },
    }


def format_human(window: dict) -> str:
    lines = [
        "exploration_collection: "
        f"mode={window['mode']} rate={window['exploration_rate']:.2f} "
        f"eligible={window['eligible']} dry_run={window['read_only']}",
        f"stage={window.get('acquisition_stage')} next={window.get('acquisition_next_action')}",
        f"selected_backlog={len(window.get('selected_backlog') or [])} "
        f"assignments={len((window.get('decision') or {}).get('assignments') or [])} "
        f"direct_exploration={window.get('direct_exploration_assignments', 0)}",
    ]
    seed_search = window.get("seed_search") or {}
    if seed_search:
        lines.append(
            "seed_search: "
            f"found={seed_search.get('found')} selected_seed={seed_search.get('selected_seed')} "
            f"attempted={seed_search.get('attempted')} "
            f"min={seed_search.get('min_exploratory_dispatches')}"
        )
    if window.get("blocked_reasons"):
        lines.append("blocked:")
        for reason in window["blocked_reasons"]:
            lines.append(f"  - {reason}")
    assignments = (window.get("decision") or {}).get("assignments") or []
    if assignments:
        lines.append("assignments:")
        for assignment in assignments:
            marker = " exploration" if assignment.get("exploration") else ""
            lines.append(
                f"  {assignment['target']}: {assignment['task_type']} -> "
                f"{assignment['agent']}/{assignment['mode']}{marker}"
            )
    lines.append("active dispatch requires --apply --confirm-window and ORCH_EXPLORATION_EVIDENCE=1")
    return "\n".join(lines)


def _selftest() -> None:
    old_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="exploration-collection-")
    feedback.DB_PATH = Path(tmp) / "feedback.db"
    try:
        backlog_payload = {
            "items": [
                {"target": "o/r#1", "task_type": "implement", "lane": "opener"},
                {
                    "target": "o/r#2",
                    "task_type": "testgen",
                    "lane": "opener",
                    "title": "Add parser boundary tests",
                    "body": "## Scope\n- Add tests for scripts/parser.py only.",
                },
                {"target": "o/r#3", "task_type": "epic", "lane": "opener"},
                {"target": "o/r#4", "task_type": "implement", "lane": "closer"},
            ]
        }
        capacity_payload = {
            "agents": {
                "claude": {"state": "ok"},
                "codex": {"state": "ok"},
                "gemini": {"state": "ok"},
                "cursor": {"state": "ok"},
                "vibe": {"state": "ok"},
                "aider": {"state": "ok"},
            }
        }
        dry = build_window(
            requested_mode="epsilon-greedy",
            rate=1.0,
            max_dispatches=2,
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            dry_run=True,
            seed_search_limit=64,
        )
        assert dry["read_only"] is True and dry["active_dispatch"] is False, dry
        assert dry["mode"] == "epsilon-greedy", dry
        assert dry["exploration_rate"] == MAX_RATE, dry["exploration_rate"]
        assert len(dry["selected_backlog"]) == 2, dry["selected_backlog"]
        assert all(item["lane"] == "opener" for item in dry["selected_backlog"]), dry
        assert dry["direct_exploration_assignments"] >= 1, dry
        assert all(a["exploration"] for a in dry["decision"]["assignments"]), dry
        assert all(a["lane"] == "opener" for a in dry["decision"]["assignments"]), dry
        assert all(not _entry_is_late(a["task_type"], a) for a in dry["decision"]["assignments"]), dry
        assert all(a["agent"] not in router.BACKUP_AGENTS for a in dry["decision"]["assignments"]), dry
        testgen_prompt = _direct_evidence_prompt(
            {
                "target": "o/r#2",
                "task_type": "testgen",
                "title": "Add parser boundary tests",
                "body": "## Scope\n- Add tests for scripts/parser.py only.",
            }
        )
        assert testgen_prompt is not None
        assert "exact issue below" in testgen_prompt, testgen_prompt
        assert "Add parser boundary tests" in testgen_prompt, testgen_prompt
        assert "scripts/parser.py only" in testgen_prompt, testgen_prompt
        assert "Closes #2" in testgen_prompt, testgen_prompt
        assert "ORCH_EXPLORATION_EVIDENCE=1" in format_human(dry), format_human(dry)

        codemod_dry = build_window(
            requested_mode="epsilon-greedy",
            rate=1.0,
            max_dispatches=1,
            backlog_payload={
                "items": [
                    {
                        "target": "o/r#6",
                        "task_type": "codemod",
                        "lane": "opener",
                        "title": "Refactor helper",
                        "body": "## Acceptance Criteria\n- pytest tests/test_helper.py passes.",
                    }
                ]
            },
            capacity_payload=capacity_payload,
            dry_run=True,
            seed_search_limit=64,
        )
        codemod_assignment = codemod_dry["decision"]["assignments"][0]
        assert codemod_assignment["task_type"] == "codemod", codemod_assignment
        assert "not just a codemod campaign plan" in codemod_assignment["prompt"], codemod_assignment
        assert "Closes #6" in codemod_assignment["prompt"], codemod_assignment

        excluded = build_window(
            requested_mode="epsilon-greedy",
            rate=1.0,
            max_dispatches=2,
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            dry_run=True,
            seed_search_limit=64,
            exclude_targets=["o/r#2"],
        )
        assert "o/r#2" not in [item["target"] for item in excluded["selected_backlog"]], excluded
        assert any(
            row.get("target") == "o/r#2"
            and row.get("reason") == "target excluded from this supervised window"
            for row in excluded["skipped_backlog"]
        ), excluded

        blocked = build_window(
            backlog_payload={"items": [{"target": "o/r#5", "task_type": "epic", "lane": "opener"}]},
            capacity_payload=capacity_payload,
            dry_run=True,
        )
        assert blocked["eligible"] is False and blocked["blocked_reasons"], blocked
        print("exploration_collection.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in supervised exploration collection window.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--mode", choices=MODE_CHOICES, default="auto")
    parser.add_argument("--rate", type=float, default=None)
    parser.add_argument("--max-dispatches", type=int, default=None)
    parser.add_argument("--min-exploratory-dispatches", type=int, default=DEFAULT_MIN_EXPLORATORY_DISPATCHES)
    parser.add_argument("--seed-search-limit", type=int, default=DEFAULT_SEED_SEARCH_LIMIT)
    parser.add_argument("--rng-seed", type=int, default=None)
    parser.add_argument("--exclude-target", action="append", default=[], help="skip a known unsuitable target")
    parser.add_argument("--apply", action="store_true", help="actively dispatch the window")
    parser.add_argument("--confirm-window", action="store_true", help="required with --apply")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    if args.apply and (not args.confirm_window or os.environ.get(ENV_FLAG) != "1"):
        result = {
            "error": "active dispatch requires --confirm-window and ORCH_EXPLORATION_EVIDENCE=1",
            "read_only": True,
        }
        print(json.dumps(result, indent=2) if args.as_json else result["error"])
        return 2

    window = build_window(
        requested_mode=args.mode,
        rate=args.rate,
        max_dispatches=args.max_dispatches,
        dry_run=not args.apply,
        rng_seed=args.rng_seed,
        min_exploratory_dispatches=args.min_exploratory_dispatches,
        seed_search_limit=args.seed_search_limit,
        exclude_targets=args.exclude_target,
    )
    if args.apply:
        decision = window.get("decision") or {}
        if not window.get("eligible") or not decision.get("assignments"):
            result = {**window, "dispatch_result": {"count": 0, "blocked": True}}
        else:
            router.HANDOFF.mkdir(parents=True, exist_ok=True)
            router.DECISION_JSON.write_text(json.dumps(decision, indent=2) + "\n")
            result = {**window, "dispatch_result": dispatcher.run(decision, dry_run=False)}
        print(json.dumps(result, indent=2) if args.as_json else format_human(result))
        return 0

    print(json.dumps(window, indent=2) if args.as_json else format_human(window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
