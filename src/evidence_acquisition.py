#!/usr/bin/env python3
"""Layer 3 — the evidence-acquisition lane.

Routes a BOUNDED amount of matching work at capabilities that are plausibly useful but
evidence-starved, so a gate that needs N independent durable reuses can actually reach them.

Why this is a lane and not a loop. `capabilities.unblock()` already decides WHETHER a capability is
worth feeding and this module does not re-decide it: exactly one branch there returns `feed: True`
(gate fully evaluated, no recorded failure/rework, just short of its reuse threshold). Everything
else -- retired, gate-blocked, unmeasured, trigger-less, or held by a documented default-off switch
-- is `feed: False`, and this lane simply obeys that. Re-deriving the decision here would create a
second opinion about the same question, which is how two evidence standards end up in one ledger.

SHADOW BY DEFAULT. `ORCH_EVIDENCE_ACQUISITION` unset or 0 means plan-only: the lane computes what it
would route, writes the plan, and routes nothing. That is the documented default-off kill switch;
live routing spends real agent capacity, and a lane that spends capacity on its first run is not
something to enable by import.

WHAT IT WILL NOT DO, by construction:
  * feed a capability `unblock()` did not mark feedable, including a default-off switch;
  * exceed `MAX_FEEDS_PER_CYCLE` capabilities or `MAX_ITEMS_PER_CAPABILITY` items in one cycle;
  * invent work -- it only nominates items already present in the caller's candidate list;
  * create a queue for the owner. There is no approval step and nothing accumulates: an unfed
    capability is simply reconsidered next cycle, and the plan artifact is FYI.

HONEST STATE AT BUILD TIME (2026-08-22): the feedable set is 0 of 42. Every capability short of its
threshold is held by a documented default-off switch, which `unblock()` correctly refuses to feed.
So this lane no-ops today and says so in one line. That is deliberate: it was built to a design that
was already owner-approved, and it reports `nothing_to_feed` rather than looking broken or silently
doing nothing. Do NOT "fix" a persistent `nothing_to_feed` by relaxing the feed decision -- that
decision is the safety property, and the empty set is the honest answer until a switch is flipped.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import capabilities

# Bounded on both axes. A lane that can feed everything at once is a capacity incident, and the cap
# is per-cycle rather than cumulative so a backlog can never build up inside the lane itself.
MAX_FEEDS_PER_CYCLE = int(os.environ.get("ORCH_EVIDENCE_ACQUISITION_MAX_FEEDS") or 1)
MAX_ITEMS_PER_CAPABILITY = int(os.environ.get("ORCH_EVIDENCE_ACQUISITION_MAX_ITEMS") or 3)
LIVE_FLAG = "ORCH_EVIDENCE_ACQUISITION"


def live_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the documented default-off switch is explicitly set to 1."""
    env = os.environ if env is None else env
    return str(env.get(LIVE_FLAG, "")).strip() == "1"


def feedable(ledger: dict[str, Any] | None = None, *, now: int | None = None) -> list[dict]:
    """Capabilities `unblock()` says are worth feeding, richest debt first.

    The ordering is by REMAINING evidence debt ascending: a capability three reuses from its gate is
    cheaper to finish than one ten away, and finishing gates is the point. Ties break on capability
    id so the same input always produces the same plan (a replayable lane, not a lottery).
    """
    # The loader returns the capability MAPPING directly; tests inject a {"capabilities": {...}}
    # wrapper. Accept both rather than making callers know which. `load_declared`, not the writing
    # `load()`: `plan()` promises "reads the ledger, writes nothing", and a bare `load()` seeds,
    # reconciles and expires rows and writes them into the shared ledger. `capabilities.REG` is
    # named so the path is read when this runs, not when `load` was defined.
    source = ledger if ledger is not None else capabilities.load_declared(capabilities.REG)
    caps = source.get("capabilities", source) if isinstance(source, dict) else {}
    rows = []
    for cap_id, cap in caps.items():
        verdict = capabilities.unblock(cap, now=now)
        if not verdict.get("feed"):
            continue
        debt = capabilities.evidence_debt(cap)
        rows.append(
            {
                "capability_id": cap_id,
                "remaining": int(debt.get("remaining") or 0),
                "blocker": verdict.get("blocker"),
                "action": verdict.get("action"),
                "matcher": cap.get("matcher") or {},
            }
        )
    return sorted(rows, key=lambda row: (row["remaining"], row["capability_id"]))


def plan(
    candidates: list[dict] | None = None,
    *,
    ledger: dict | None = None,
    now: int | None = None,
    env: dict | None = None,
) -> dict:
    """What this cycle WOULD route, bounded. Pure: reads the ledger, writes nothing.

    `candidates` are work items the caller already has (the same dicts the tick passes around, with
    at least `target` and `task_type`). The lane never invents an item; if the caller has nothing,
    the plan is empty even when a capability is starving -- "no work exists" and "the capability is
    unfeedable" are different states and both are reported.
    """
    candidates = list(candidates or [])
    rows = feedable(ledger, now=now)
    assignments: list[dict] = []
    for row in rows[:MAX_FEEDS_PER_CYCLE]:
        matched = [
            item
            for item in candidates
            if capabilities._matches_trigger(
                {"matcher": row["matcher"]},
                {
                    "repository": str(item.get("target") or "").rsplit("#", 1)[0],
                    "task_type": item.get("task_type"),
                    "lane": item.get("lane"),
                },
            )[0]
        ]
        take = min(row["remaining"] or MAX_ITEMS_PER_CAPABILITY, MAX_ITEMS_PER_CAPABILITY)
        assignments.append(
            {
                "capability_id": row["capability_id"],
                "remaining": row["remaining"],
                "items": [item.get("target") for item in matched[:take]],
                "matching_candidates": len(matched),
                "reason": row["blocker"],
            }
        )
    fed = sum(1 for a in assignments if a["items"])
    if not rows:
        state = "nothing_to_feed"
    elif not candidates:
        state = "no_candidates"
    elif not fed:
        state = "no_matching_work"
    else:
        state = "planned"
    return {
        "schema": "orchestrator.evidence-acquisition-plan",
        "version": 1,
        "generated_at": int(time.time()) if now is None else int(now),
        "live": live_enabled(env),
        "state": state,
        # Both quantities in one place: how many capabilities are starving, and how many this cycle
        # can actually help. `feedable 0` is the answer to "why did nothing happen".
        "summary": (
            f"feedable {len(rows)} / capped {MAX_FEEDS_PER_CYCLE} / "
            f"candidates {len(candidates)} / fed {fed}"
        ),
        "feedable_count": len(rows),
        "cap_per_cycle": MAX_FEEDS_PER_CYCLE,
        "max_items_per_capability": MAX_ITEMS_PER_CAPABILITY,
        "assignments": assignments,
        "deferred": [row["capability_id"] for row in rows[MAX_FEEDS_PER_CYCLE:]],
    }


def run(
    candidates: list[dict] | None = None,
    *,
    ledger: dict | None = None,
    now: int | None = None,
    env: dict | None = None,
    write: Path | None = None,
) -> dict:
    """Compute the plan, record a heartbeat, and route only when the switch is on."""
    result = plan(candidates, ledger=ledger, now=now, env=env)
    try:
        capabilities.production_heartbeat(
            "evidence-acquisition",
            "invocation" if result["assignments"] else "match",
            ref=result["state"],
            metadata={"feedable": result["feedable_count"], "live": result["live"]},
        )
    except Exception:
        # A heartbeat must never prevent the lane from reporting; the plan is the product.
        pass
    result["routed"] = []
    if result["live"] and result["state"] == "planned":
        # Live routing is intentionally the ONLY branch that acts, and it is unreachable without the
        # documented default-off switch. Routing itself is delegated to the caller: this lane
        # nominates, the dispatcher dispatches, so there is exactly one dispatch path in the system.
        result["routed"] = [a for a in result["assignments"] if a["items"]]
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def render_tick_line(plan_path: Path) -> str:
    """The tick's one-line EVIDENCE-ACQ summary of a plan that `run(write=...)` wrote.

    orchestrate.sh used to print this line from a here-document fed to `python3 -`. Homebrew bash
    5.3 writes a small here-document into a pipe before the reading command starts, so the writer
    is the reader's own process. If the kernel gives that pipe less room than the document, the
    write blocks forever, and that is how the 2026-09-26 tick hung for 141 hours. The text here is
    identical to what the here-document printed. One addition: a plan that is valid JSON but not an
    object used to end in a traceback, and now gets the same "unreadable" line as any other bad plan.
    """
    try:
        with open(plan_path, encoding="utf-8") as fh:
            plan_obj = json.load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - the line names every failure and must never raise
        return f"  EVIDENCE-ACQ: plan unreadable ({exc})"
    if not isinstance(plan_obj, dict):
        return f"  EVIDENCE-ACQ: plan unreadable (not a JSON object: {type(plan_obj).__name__})"
    live = " [LIVE]" if plan_obj.get("live") else " [shadow]"
    return f"  EVIDENCE-ACQ: {plan_obj.get('state')} — {plan_obj.get('summary')}{live}"


def _selftest() -> None:
    now = 1_700_000_000

    def cap(**over):
        base = {
            "status": "shadow",
            "liveness": None,
            "evidence_threshold": {"min_independent_durable_reuse": 3},
            "gate_blocks_execution": True,
            "gate_reason": "needs independent durable reuses",
            "gate_criteria": {"min_independent_durable_reuse": 3},
            "causal_evidence": {
                "readiness": {
                    "durable_subjects": ["subject-a"],
                    "terminal_outcomes": 1,
                    "failures": 0,
                    "rework": 0,
                    "latest_evidence_ts": now - 3600,
                    "ready": False,
                }
            },
            "matcher": {"field": "task_type", "operator": "in", "value": ["implement"]},
            "kill_switch": "disable via config",
            "rollback": "revert",
            "outcome_links": [],
            "event_history": [],
        }
        base.update(over)
        return {**capabilities._blank_capability("c"), **base}

    items = [
        {"target": "o/r#1", "task_type": "implement"},
        {"target": "o/r#2", "task_type": "implement"},
        {"target": "o/r#3", "task_type": "review"},
    ]

    # A FAILURE-BLOCKED GATE IS NEVER FED — the completion gate this lane was specified against.
    blocked = cap(
        causal_evidence={
            "readiness": {
                "durable_subjects": ["subject-a"],
                "terminal_outcomes": 1,
                "failures": 1,
                "rework": 0,
                "latest_evidence_ts": now - 3600,
                "ready": False,
            }
        }
    )
    led = {"capabilities": {"blocked": blocked}}
    assert not feedable(led, now=now), "a failure-blocked gate must never be fed"
    assert plan(items, ledger=led, now=now)["state"] == "nothing_to_feed"

    # A DEFAULT-OFF SWITCH IS NEVER FED either (guard added the same day in capabilities.unblock).
    off = cap(kill_switch="restore documented default-off gate")
    assert not feedable(
        {"capabilities": {"off": off}}, now=now
    ), "default-off switch must not be fed"

    # THE CAP HOLDS. Three feedable capabilities, one fed, two deferred — never all at once.
    many = {"capabilities": {f"c{i}": cap() for i in range(3)}}
    rows = feedable(many, now=now)
    assert rows, "feedable fixture must exercise the cap assertions"
    p = plan(items, ledger=many, now=now)
    assert len(p["assignments"]) <= MAX_FEEDS_PER_CYCLE, (
        "MAX_FEEDS_PER_CYCLE must limit assignments",
        p,
    )
    assert len(p["deferred"]) == max(0, len(rows) - MAX_FEEDS_PER_CYCLE), p
    for assignment in p["assignments"]:
        assert len(assignment["items"]) <= MAX_ITEMS_PER_CAPABILITY, assignment
        # Only matching work is nominated: the `review` item must never appear.
        assert "o/r#3" not in assignment["items"], assignment

    # NO WORK AND NO FEEDABLE CAPABILITY ARE DIFFERENT STATES, and both are reported.
    assert plan([], ledger=many, now=now)["state"] in {"no_candidates", "nothing_to_feed"}

    # SHADOW BY DEFAULT: nothing is routed without the documented switch, even with a full plan.
    shadow = run(items, ledger=many, now=now, env={})
    assert shadow["live"] is False and shadow["routed"] == [], shadow
    assert not live_enabled({}) and not live_enabled({LIVE_FLAG: "0"})
    assert live_enabled({LIVE_FLAG: "1"})

    # THE DEFAULT LEDGER PATH MUST WORK. Every assertion above injects a ledger, so none of them
    # exercise the real loader -- the first version of this selftest passed while
    # `capabilities.load_ledger()` did not even exist. Exercise it explicitly.
    live_rows = feedable(now=now)
    assert isinstance(live_rows, list), live_rows
    live_plan = plan([], now=now)
    assert live_plan["state"] in {"nothing_to_feed", "no_candidates"}, live_plan
    assert "feedable" in live_plan["summary"], live_plan

    # The summary must carry BOTH quantities, so "nothing happened" is never a bare silence.
    text = plan(items, ledger={"capabilities": {}}, now=now)["summary"]
    assert "feedable 0" in text and "candidates" in text, text

    # THE TICK LINE, rendered from the written plan by this module, never by a here-document.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        written = Path(td) / "plan.json"
        shadow_plan = run(items, ledger={"capabilities": {}}, now=now, env={}, write=written)
        line = render_tick_line(written)
        assert line == (
            f"  EVIDENCE-ACQ: {shadow_plan['state']} — {shadow_plan['summary']} [shadow]"
        ), line
        assert render_tick_line(Path(td) / "absent.json").startswith(
            "  EVIDENCE-ACQ: plan unreadable ([Errno 2]"
        )

    print(
        "evidence_acquisition.py selftest: OK (feed obeys unblock, failure-blocked and "
        "default-off never fed, per-cycle cap + per-capability cap, shadow by default, "
        "distinct empty states, tick line rendered from the written plan)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # `tick-line` prints the tick's summary of an existing plan and computes nothing. With no
    # command, the module runs the lane, exactly as it always has.
    ap.add_argument("command", nargs="?", choices=("tick-line",))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", type=Path)
    ap.add_argument("--plan", type=Path, help="tick-line: the plan file the run wrote")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.command == "tick-line":
        if args.plan is None:
            ap.error("tick-line needs --plan, the file the run wrote with --write")
        print(render_tick_line(args.plan))
        return 0
    result = run(write=args.write)
    print(
        json.dumps(result, indent=2, sort_keys=True)
        if args.json
        else f"evidence-acquisition: {result['state']} — {result['summary']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
