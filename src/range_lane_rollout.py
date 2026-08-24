#!/usr/bin/env python3
"""range_lane_rollout.py - guarded rollout/apply surface for specialized range lanes.

The range-lane helpers (testgen, epic, codemod, cross_repo, runtime_ac) already
produce prompts, strict JSON contracts, dry-run plans, and gates. This module is
the first first-class rollout layer over those helpers: it filters the backlog to
eligible range-lane opener work, asks the normal router for assignments, previews
the concrete dispatches, and only actively dispatches when explicitly confirmed.

Active dispatch requires:
  1. --apply
  2. --confirm-rollout
  3. ORCH_RANGE_LANE_ROLLOUT=1
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import backlog
import capabilities
import capacity
import claims
import dispatcher
import env_prereq
import router

ENV_FLAG = "ORCH_RANGE_LANE_ROLLOUT"
RANGE_TASK_TYPES = ("testgen", "epic", "codemod", "cross_repo", "runtime_ac")
DEFAULT_MAX_DISPATCHES = 2


def _live_backlog_payload() -> dict[str, Any]:
    items = backlog.build_backlog(
        backlog.SUPPORTED_REPOS,
        backlog.live_fetch_issues,
        backlog.live_fetch_prs,
        scoped=backlog.load_scoped_blockers(),
    )
    return {"source": "live", "items": items}


def _cached_backlog_payload(path: Path = backlog.BACKLOG_JSON) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {**payload, "source": payload.get("source") or "cached"}
        return {"source": "cached-invalid", "items": []}
    except Exception:
        return {"source": "cached-missing", "items": []}


def _load_backlog_payload(*, cached: bool = False) -> dict[str, Any]:
    return _cached_backlog_payload() if cached else _live_backlog_payload()


def _backlog_items(backlog_payload: dict[str, Any] | None = None) -> list[dict]:
    payload = backlog_payload if backlog_payload is not None else _load_backlog_payload()
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items or [] if isinstance(item, dict)]


def _capacity_snapshot(
    capacity_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return capacity_payload if capacity_payload is not None else capacity.build()


def _filter_backlog(
    backlog_payload: dict[str, Any] | None,
    *,
    task_types: set[str],
    max_items: int,
) -> tuple[list[dict], list[dict]]:
    selected: list[dict] = []
    skipped: list[dict] = []
    for item in _backlog_items(backlog_payload):
        target = str(item.get("target") or "")
        task_type = str(item.get("task_type") or "implement")
        lane = str(item.get("lane") or "")
        reason = ""
        if lane != "opener":
            reason = "not opener lane"
        elif task_type not in task_types:
            reason = "not a range-lane task type"
        elif not target:
            reason = "missing target"
        if reason:
            skipped.append(
                {
                    "target": target,
                    "task_type": task_type,
                    "lane": lane,
                    "reason": reason,
                }
            )
            continue
        selected.append(
            {
                "target": target,
                "task_type": task_type,
                "lane": "opener",
                "labels": item.get("labels") or [],
                "title": item.get("title") or "",
                "body": item.get("body") or "",
            }
        )
        if len(selected) >= max_items:
            break
    return selected, skipped


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


def _sanitize_decision(decision: dict, task_types: set[str]) -> tuple[dict, list[dict]]:
    kept = []
    rejected = []
    for assignment in decision.get("assignments") or []:
        reason = ""
        if assignment.get("lane") != "opener":
            reason = "not opener lane"
        elif assignment.get("task_type") not in task_types:
            reason = "not a selected range-lane task type"
        elif assignment.get("agent") in router.BACKUP_AGENTS:
            reason = "backup/paygo agent is not allowed in automatic range-lane rollout"
        if reason:
            rejected.append({"assignment": assignment, "reason": reason})
        else:
            kept.append(assignment)
    clean = {**decision, "assignments": kept}
    if rejected:
        notes = list(clean.get("notes") or [])
        notes.append(f"rejected {len(rejected)} unsafe range-lane assignments")
        clean["notes"] = notes
    return clean, rejected


def _dispatch_preview(decision: dict) -> list[dict]:
    preview = dispatcher.run(decision, dry_run=True, heartbeat=False)
    rows = []
    for row in preview.get("launched") or []:
        rows.append(
            {
                "target": row.get("target"),
                "task_type": row.get("task_type"),
                "lane": row.get("lane"),
                "agent": row.get("agent"),
                "mode": row.get("mode"),
                "model": row.get("model"),
                "worktree": row.get("cwd"),
                "worktree_missing": bool(row.get("worktree_missing")),
            }
        )
    return rows


def build_rollout(
    *,
    task_types: set[str] | None = None,
    max_dispatches: int = DEFAULT_MAX_DISPATCHES,
    backlog_payload: dict[str, Any] | None = None,
    cached_backlog: bool = False,
    capacity_payload: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    selected_task_types = task_types or set(RANGE_TASK_TYPES)
    cap = max(0, int(max_dispatches))
    effective_backlog_payload = (
        backlog_payload
        if backlog_payload is not None
        else _load_backlog_payload(cached=cached_backlog)
    )
    selected, skipped = _filter_backlog(
        effective_backlog_payload,
        task_types=selected_task_types,
        max_items=cap,
    )
    capacity_snapshot = _capacity_snapshot(capacity_payload)
    blocked_reasons: list[str] = []
    blocked_details: list[dict] = []
    if cap <= 0:
        blocked_reasons.append("max dispatch cap is zero")
        blocked_details.append({"reason": "max_dispatch_cap_zero", "max_dispatches": cap})
    if not selected:
        blocked_reasons.append("no eligible range-lane opener backlog items selected")
        blocked_details.append(
            {
                "reason": "no_eligible_range_backlog",
                "skipped_count": len(skipped),
            }
        )

    decision: dict[str, Any] = {
        "generated_at": int(time.time()),
        "dry_run": True,
        "assignments": [],
        "lane_cap": 0,
        "pressure": False,
        "backoff_ticks": 1,
        "shed": [],
        "notes": ["range lane rollout blocked"],
    }
    rejected_assignments: list[dict] = []
    if not blocked_reasons:
        decision = router.plan(
            selected,
            capacity_snapshot,
            max_concurrent=cap,
            dry_run=dry_run,
            learned=router.learned_ranks(),
        )
        decision, rejected_assignments = _sanitize_decision(decision, selected_task_types)
        if not dry_run and rejected_assignments:
            _release_rejected_claims(rejected_assignments)
        for row in rejected_assignments:
            blocked_details.append(
                {
                    "reason": "unsafe_assignment_rejected",
                    "target": (row.get("assignment") or {}).get("target"),
                    "detail": row.get("reason"),
                }
            )
        if not decision.get("assignments"):
            for row in decision.get("rejections") or []:
                blocked_details.append(dict(row))
                reason = str(row.get("reason") or "router_rejected")
                target = row.get("target") or "unknown target"
                detail = f"{target}: {reason}"
                if row.get("claimed_by"):
                    detail += f" by {row['claimed_by']}"
                blocked_reasons.append(detail)
            if not decision.get("rejections"):
                blocked_reasons.append("router produced zero assignments without a candidate")
                blocked_details.append(
                    {
                        "reason": "router_zero_assignments",
                        "notes": decision.get("notes") or [],
                    }
                )

    blocked_reasons = list(dict.fromkeys(blocked_reasons))
    selected_count = len(selected)
    assigned_count = len(decision.get("assignments") or [])

    return {
        "generated_at": int(time.time()),
        "read_only": dry_run,
        "active_dispatch": not dry_run,
        "backlog_source": (
            effective_backlog_payload.get("source")
            if isinstance(effective_backlog_payload, dict)
            else ""
        ),
        "task_types": sorted(selected_task_types),
        "max_dispatches": cap,
        "eligible": not blocked_reasons and bool(decision.get("assignments")),
        "blocked_reasons": blocked_reasons,
        "blocked_details": blocked_details,
        "counts": {
            "selected": selected_count,
            "assigned": assigned_count,
            "dispatched": 0,
        },
        "claimed_by": decision.get("claimed_by") or {},
        "capacity_rejections": decision.get("capacity_rejections") or [],
        "already_routed": decision.get("already_routed") or [],
        "selected_backlog": selected,
        "skipped_backlog": skipped[:20],
        "rejected_assignments": rejected_assignments,
        "decision": decision,
        "dispatch_preview": (_dispatch_preview(decision) if decision.get("assignments") else []),
        "safety": {
            "requires_apply": True,
            "requires_confirm_rollout": True,
            "requires_env": f"{ENV_FLAG}=1",
            "opener_only": True,
            "range_task_types": list(RANGE_TASK_TYPES),
            "backup_paygo_allowed": False,
            "default_policy_change": False,
        },
    }


def format_human(rollout: dict) -> str:
    lines = [
        "range_lane_rollout: "
        f"eligible={rollout['eligible']} dry_run={rollout['read_only']} "
        f"max_dispatches={rollout['max_dispatches']}",
        f"task_types={','.join(rollout.get('task_types') or [])}",
        f"selected_backlog={(rollout.get('counts') or {}).get('selected', 0)} "
        f"assignments={(rollout.get('counts') or {}).get('assigned', 0)} "
        f"dispatched={(rollout.get('counts') or {}).get('dispatched', 0)}",
    ]
    if rollout.get("blocked_reasons"):
        lines.append("blocked:")
        for reason in rollout["blocked_reasons"]:
            lines.append(f"  - {reason}")
    preview = rollout.get("dispatch_preview") or []
    if preview:
        lines.append("dispatch preview:")
        for row in preview:
            marker = " missing-worktree" if row.get("worktree_missing") else ""
            lines.append(
                f"  {row['target']}: {row['task_type']} -> " f"{row['agent']}/{row['mode']}{marker}"
            )
    lines.append(f"active dispatch requires --apply --confirm-rollout and {ENV_FLAG}=1")
    return "\n".join(lines)


def _selftest() -> None:
    old_handoff = os.environ.get("HANDOFF_DIR")
    old_rate = os.environ.get("ORCH_EXPLORATION_RATE")
    with tempfile.TemporaryDirectory(prefix="range-rollout-selftest-") as tmp:
        os.environ["HANDOFF_DIR"] = tmp
        os.environ["ORCH_EXPLORATION_RATE"] = "0"
        backlog_payload = {
            "items": [
                {"target": "o/r#1", "task_type": "testgen", "lane": "opener"},
                {"target": "o/r#2", "task_type": "epic", "lane": "opener"},
                {"target": "o/r#3", "task_type": "implement", "lane": "opener"},
                {"target": "o/r#4", "task_type": "runtime_ac", "lane": "closer"},
                {"target": "o/r#5", "task_type": "codemod", "lane": "opener"},
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
        dry = build_rollout(
            task_types={"testgen", "epic", "codemod"},
            max_dispatches=2,
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            dry_run=True,
        )
        assert dry["read_only"] is True and dry["active_dispatch"] is False, dry
        assert dry["eligible"] is True, dry
        assert [item["target"] for item in dry["selected_backlog"]] == [
            "o/r#1",
            "o/r#2",
        ], dry
        assert all(a["lane"] == "opener" for a in dry["decision"]["assignments"]), dry
        assert all(
            a["task_type"] in {"testgen", "epic", "codemod"} for a in dry["decision"]["assignments"]
        ), dry
        assert dry["dispatch_preview"], dry
        assert dry["counts"] == {"selected": 2, "assigned": 2, "dispatched": 0}, dry
        assert "ORCH_RANGE_LANE_ROLLOUT=1" in format_human(dry), format_human(dry)

        blocked = build_rollout(
            task_types={"cross_repo"},
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            dry_run=True,
        )
        assert blocked["eligible"] is False and blocked["blocked_reasons"], blocked
        assert blocked["blocked_details"], blocked
        assert any(
            row["reason"] == "not a range-lane task type" for row in blocked["skipped_backlog"]
        ), blocked

        no_capacity = build_rollout(
            task_types={"testgen"},
            backlog_payload=backlog_payload,
            capacity_payload={
                "agents": {
                    agent: {"state": "shed"}
                    for agent in ("claude", "codex", "gemini", "cursor", "vibe")
                }
            },
            dry_run=True,
        )
        assert no_capacity["eligible"] is False, no_capacity
        assert no_capacity["counts"]["selected"] == 1, no_capacity
        assert no_capacity["counts"]["assigned"] == 0, no_capacity
        assert no_capacity["capacity_rejections"], no_capacity
        assert any(
            row.get("reason") == "capacity_rejected" for row in no_capacity["blocked_details"]
        ), no_capacity

        stale = Path(tmp) / "claims" / claims._slug("o/r#1")
        stale.mkdir(parents=True)
        (stale / "meta").write_text(
            json.dumps(
                {
                    "target": "o/r#1",
                    "agent": "research",
                    "pid": 2147480000,
                    # genuinely stale: past the item-13 reap grace window (a FRESH dead-pid
                    # claim is now deliberately protected as the router→dispatcher handoff)
                    "ts": time.time() - (claims.REAP_GRACE_SECONDS + 5),
                }
            )
        )
        stale_dry = build_rollout(
            task_types={"testgen", "epic", "codemod"},
            max_dispatches=2,
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            dry_run=True,
        )
        assert stale_dry["decision"]["assignments"][0]["target"] == "o/r#1", stale_dry
        assert stale.exists(), "read-only preview should not mutate claim state"
        stale_active = build_rollout(
            task_types={"testgen", "epic", "codemod"},
            max_dispatches=2,
            backlog_payload=backlog_payload,
            capacity_payload=capacity_payload,
            dry_run=False,
        )
        assert stale_active["decision"]["assignments"][0]["target"] == "o/r#1", stale_active
        assert any(
            "reaped stale claims" in note and "o/r#1" in note
            for note in stale_active["decision"].get("notes", [])
        ), stale_active
    if old_handoff is None:
        os.environ.pop("HANDOFF_DIR", None)
    else:
        os.environ["HANDOFF_DIR"] = old_handoff
    if old_rate is None:
        os.environ.pop("ORCH_EXPLORATION_RATE", None)
    else:
        os.environ["ORCH_EXPLORATION_RATE"] = old_rate
    print(
        "range_lane_rollout.py selftest: OK (selected/assigned/dispatched counts, "
        "claim/capacity/already-routed diagnostics, guarded dispatch)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded rollout/apply surface for range lanes.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--task-type", action="append", choices=RANGE_TASK_TYPES)
    parser.add_argument("--max-dispatches", type=int, default=DEFAULT_MAX_DISPATCHES)
    parser.add_argument("--apply", action="store_true", help="actively dispatch the rollout")
    parser.add_argument("--confirm-rollout", action="store_true", help="required with --apply")
    parser.add_argument(
        "--cached-backlog",
        action="store_true",
        help="use ~/.codex/handoff/backlog.json instead of refreshing live GitHub state",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        # Every `build_rollout` here runs a dry dispatch preview, and a codex assignment resolves
        # an EXACT profile — which needs the version-capable Codex binary and fails closed rather
        # than falling back to PATH. That is the whole selftest's spine, not one section of it, so
        # the gate is the selftest. The reason names the binary; verify.py counts it and bounds it.
        if env_prereq.selftest_skipped(
            "range_lane_rollout.py", env_prereq.codex_profile_binary_absent()
        ):
            return 0
        _selftest()
        return 0

    if args.apply and (not args.confirm_rollout or os.environ.get(ENV_FLAG) != "1"):
        result = {
            "error": f"active dispatch requires --confirm-rollout and {ENV_FLAG}=1",
            "read_only": True,
        }
        print(json.dumps(result, indent=2) if args.as_json else result["error"])
        return 2

    rollout = build_rollout(
        task_types=set(args.task_type) if args.task_type else None,
        max_dispatches=args.max_dispatches,
        cached_backlog=args.cached_backlog,
        dry_run=not args.apply,
    )
    if args.apply:
        decision = rollout.get("decision") or {}
        if not rollout.get("eligible") or not decision.get("assignments"):
            result = {**rollout, "dispatch_result": {"count": 0, "blocked": True}}
        else:
            assignments = decision.get("assignments") or []
            capabilities.production_heartbeat(
                "range-lane-rollout",
                "match",
                metadata={"assignment_count": len(assignments)},
            )
            # Tag the runs this rollout is about to launch, so the outcome can come back to the
            # capability. The heartbeats below already fire ONLY on this live-apply branch (preview
            # never reaches here), and these are the very assignments dispatcher.run launches — so
            # the tag is the same recorded fact, carried to the run that can terminate. Without it
            # the capability logged 13 live invocations and zero outcome edges. (2026-08-21)
            for assignment in assignments:
                if isinstance(assignment, dict):
                    assignment["capability_ids"] = list(
                        dict.fromkeys(
                            list(assignment.get("capability_ids") or []) + ["range-lane-rollout"]
                        )
                    )
            router.HANDOFF.mkdir(parents=True, exist_ok=True)
            router.DECISION_JSON.write_text(json.dumps(decision, indent=2) + "\n")
            capabilities.production_heartbeat(
                "range-lane-rollout",
                "invocation",
                ref=str(router.DECISION_JSON),
                metadata={"assignment_count": len(assignments)},
            )
            dispatch_result = dispatcher.run(decision, dry_run=False)
            dispatched = len((dispatch_result or {}).get("launched") or [])
            result = {
                **rollout,
                "dispatch_result": dispatch_result,
                "counts": {
                    **(rollout.get("counts") or {}),
                    "dispatched": dispatched,
                },
            }
            if dispatched:
                capabilities.production_heartbeat(
                    "range-lane-rollout",
                    "success",
                    ref=str(router.DECISION_JSON),
                    metadata={"dispatched": dispatched},
                )
        print(json.dumps(result, indent=2) if args.as_json else format_human(result))
        return 0

    print(json.dumps(rollout, indent=2) if args.as_json else format_human(rollout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
