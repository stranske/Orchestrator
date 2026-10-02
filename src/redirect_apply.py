#!/usr/bin/env python3
"""redirect_apply.py — the consumer `redirect_plan.apply_plan` never had, and the automatic
outcome linker that lets the Stage-2 gate lift without a human.

WHY THIS EXISTS (measured 2026-08-21).

`redirect_plan.apply_plan()` is complete and careful: exact `confirm_target`, writes the prompt
before stopping anything, skips the kill when the PID is already gone, treats a missing claim as
non-fatal, aborts on delegate failure. It had **zero callers in the tree** — the only reference was
the string `"downstream_consumer": "redirect_plan.py:apply_plan"` in the capability ledger. The
role's accepted `role_run_id` was therefore stamped onto a `delegate-retry` command that nothing
ever ran.

THE DEADLOCK THAT KEPT IT THAT WAY. `redirect_shadow.summarize()` gates apply on
`ready_for_supervised_apply`, which needs `synced_role_outcomes >= 10` AND
`linked_disagreements >= 3`. `join_role_to_outcome` returns `synced=False` whenever
`accepted=False`, and `historical_outcome_link` is explicitly `synced=False /
not_role_learning=True` — correctly, because a historical replay did not cause the old outcome. So
`synced_role_outcomes` counts **only advice that was actually applied**. The gate that authorises
applying requires ten applied outcomes. Measured state: 143 proposals, 119 valid, 124 historical
replays (that route is EXHAUSTED — `ready_for_historical_replay_analysis` is already True), and
`synced_role_outcomes = 5`, `linked_disagreements = 0`, all five created by hand with
`roles.py link-outcome`. Five links in roughly two months, zero on the disagreement cases the gate
actually discriminates on.

WHY THE EASY ESCAPES ARE WRONG.
  * Counting historical/counterfactual links toward `synced_role_outcomes` would make the gate mean
    less than it says and would train role learning on outcomes the role did not cause.
  * Counting *agreement* cases — where the deterministic rail independently did what the role
    advised — is free credit: a role that echoes the baseline would harvest it forever. That is
    precisely what the `linked_disagreements` requirement exists to refuse, and it cannot be
    satisfied that way, because on a disagreement the observable outcome belongs to the rail's
    choice, not the role's.
  * Asking the owner to apply and link by hand is the current design (`keepalive_supervisor`
    emits the commands, `next_step="review_redirectagent_proposal"`). It produced 5 links and 0
    disagreement links in two months, and it is a per-item approval queue, which CLAUDE.md §3
    forbids.

WHAT THIS DOES INSTEAD. Two functions on one daily cadence:

  1. `link_applied_outcomes()` — ALWAYS ON. For every redirect role run that has an accepted
     influence edge to a dispatch that has since reached a terminal outcome, append one
     `redirect_outcome_link` event to the redirect corpus (`redirect_shadow.CORPUS_PATH` by
     default) via `redirect_shadow.link_outcome`, which also syncs the accepted advice through
     `feedback.join_role_to_outcome`. It never kills a process, releases a claim, or delegates a
     retry — lane mutation is exclusively the apply path. `accepted=True` is truthful here and
     un-gameable: the edge exists only because the plan's own `--influenced-by-role-run-id` stamp
     rode a dispatch that really ran. This is the step that makes `synced_role_outcomes` climb with
     no human. Callers who only want the answer without writing use
     `preview_link_applied_outcomes()` or `link_applied_outcomes(dry_run=True)`.
  2. `apply_candidates()` / `apply_one()` — the apply path, DEFAULT OFF behind
     `ORCH_REDIRECT_APPLY_BOOTSTRAP`. Candidates are the targets of the keepalive supervisor's
     LATEST stage-2 run, read from the plan that cadence step writes — not a second discovery path,
     and no extra gh traffic. With the flag off the cadence spends NOTHING: authorising means
     running RedirectAgent, which costs a backend offload, and there is no point buying a proposal
     that cannot be applied. `apply_plan`'s contract is held by this module's selftest with
     injected runners. `--dry-run --spend-offloads` forces authorisation anyway for an operator who
     wants to see the predicate against live reports; it still spends only on what passes the
     screen below.

AUTHORISATION IS OBJECTIVE, NOT REVIEWED. `authorize()` is a pure function of recorded state. Its
load-bearing condition is that the prior lane is shown NOT LIVE: a pid the process table no longer
holds or, for a lane with no pid at all, the supervisor's own report that it is stalled or exited.
On such a lane the `stop-process` step is a no-op, so applying reduces to release-claim + delegate —
exactly what the closer/opener rails do to a dead stalled lane every hour anyway. The only difference
is WHICH prompt and WHICH agent the retry uses, which is the role's entire contribution and the thing
that needs measuring. It never kills a live process, never acts on a lane whose liveness it cannot
show, and never steals another agent's live claim.

THE SCREEN BEFORE THE JUDGE (2026-10-02). Measured on arrival: the step bought the role's verdict on
reports it could never act on, one metered offload (600 s timeout) per file in the supervisor's
report directory every day — 759 judgements from 2026-08-21 to 2026-10-02, every plan `wait`, none
authorised. Three defects, each a relationship rather than a number:
  * The POPULATION was every report ever written. Nothing prunes that directory, so all 30 files
    were for PRs closed between 2026-06-23 and 2026-09-20, while the supervisor's own run minutes
    earlier had found 0 live candidates. Candidates now come from that run's plan, and a plan the
    cadence registry would call stale is no population at all (`stage2_population`).
  * LIVENESS defaulted to dead. A keepalive report carries no pid — `keepalive_shadow` never sets
    one, because the lane is a GitHub Actions run — and `pid is None` read as `pid_alive=False`, so
    "apply never kills a live lane" held only for local lanes, and this step reads none. For every
    candidate it ever saw, the judge's verdict was the only thing between a lane the supervisor
    called `running` and release-claim + delegate. Liveness is now three-valued, and UNKNOWN is a
    refusal (`lane_liveness`, `_lane_blocks`).
  * Identical input was re-judged. 766 live judgements covered 39 distinct reports; one was judged
    36 times. An input already answered with a verdict no plan can apply is not asked again until
    it changes (`judged_inputs`).
`screen_report()` applies all of it before any offload, through the SAME `_lane_blocks` that
`authorize()` applies after one, so the free screen and the authorisation cannot disagree about a
lane. The keepalive supervisor's planner asks the lane half (`lane_refusals`) before it counts a
candidate as needing a Stage-2 recording, so its dashboard warn never asks anyone to buy the
judgement this screen refuses. Every run reports what it spent beside what could still be
authorised: a gate that reports only its blocking quantity cannot be told apart from a deadlock,
and the deficits here stood at 5 and 3 for 42 days while the drainable count, never printed, was 0.

SELF-LIMITING BY CONSTRUCTION. The bootstrap stops as soon as the gate's own deficits reach zero —
there is no date to expire unnoticed (FM3) and no latch whose clear path it blocks (FM2). Bounds:
one target at most once, and `MAX_APPLIES_PER_DAY` per day, both derived from the append-only
corpus rather than a side file.

    python3 redirect_apply.py --status            # read-only: gate deficits beside the drainable count
    python3 redirect_apply.py --screen            # free: which current candidates could be authorised
    python3 redirect_apply.py --link-outcomes     # append redirect_outcome_link corpus events
    ORCH_REDIRECT_APPLY_BOOTSTRAP=1 python3 redirect_apply.py --apply    # lane mutation (kill/release/delegate)
    python3 redirect_apply.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import cadence_registry
import capabilities
import claims
import feedback
import redirect_plan
import redirect_shadow

CAPABILITY_ID = "redirect-apply-bootstrap"
BOOTSTRAP_FLAG = "ORCH_REDIRECT_APPLY_BOOTSTRAP"
MAX_APPLIES_PER_DAY = 1
DAY_SECONDS = 86400
ROLE_RUN_PREFIX = "role:redirect:"
# A verdict that is still pending is not evidence yet — the same bar capability_outcome_bridge uses.
TERMINAL_VERDICTS = {"PASS", "FAIL"}

# A lane's liveness in the words of the report that describes it. watch.py and keepalive_shadow share
# this vocabulary, and a keepalive report carries NO pid, so for it these words are the only liveness
# evidence that exists. Only the two states in which the monitor itself says the lane is not
# progressing let an unknown-pid lane through.
REPORTED_NOT_LIVE_STATES = frozenset({"stalled", "exited"})
REPORTED_LIVE_STATES = frozenset({"running", "progress"})
# Recommendations no redirect improves on: `wait` is the monitor seeing an active lane, `collect` is
# the work already produced. Every one of the 759 judgements of a `wait` report came back `wait`.
NO_REDIRECT_RECOMMENDATIONS = frozenset({"wait", "collect"})
# The worst case of one run is this many role runs at the role's 600 s timeout. 3 keeps a run under
# half an hour, inside the tick's per-command alert, and candidates are served least-recently-judged
# first, so the cap can defer a candidate by a day but cannot starve one.
MAX_OFFLOADS_PER_RUN = 3
# The cadence step whose ARTIFACT is the candidate population.
STAGE2_PLAN_STEP = "keepalive-stage2-plan"
LIVE_PID_BLOCK = "prior process is still alive — apply never kills a live lane"


def _heartbeat(event_type: str, *, ref: str | None = None, metadata: dict | None = None) -> None:
    """Lifecycle evidence, best-effort. Gated by ORCH_CAPABILITY_HEARTBEATS like its siblings."""
    try:
        capabilities.production_heartbeat(CAPABILITY_ID, event_type, ref=ref, metadata=metadata)
    except Exception:  # noqa: BLE001
        pass


def flag_enabled(env: dict | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(BOOTSTRAP_FLAG, "0")).strip() == "1"


def flag_as_the_tick_sees_it() -> tuple[bool, str]:
    """Resolve the flag the way the TICK will, and name where the value came from.

    Reporting must not depend on the invoking shell. The flag is armed by an `export` in
    orchestrate.sh, so an interactive `--status` reading only `os.environ` says "off" while the
    hourly tick has it ON — the same defect FM2 records for `_predicate_flag` ("read os.environ, so
    the score depended on the invoking shell"). Reuse the one resolver that EXECUTES the prologue
    instead of regex-scraping it (a naive parse misses conditionals that override a default), and
    degrade to ambient with the source named rather than silently. (2026-08-21)
    """
    ambient = os.environ.get(BOOTSTRAP_FLAG)
    if ambient is not None:
        return ambient.strip() == "1", "ambient"
    try:
        from capability_recurrence_check import tick_env

        resolved = tick_env(refresh=True)
    except Exception:  # noqa: BLE001
        return False, "unresolved (tick env unavailable; ambient unset)"
    if BOOTSTRAP_FLAG in resolved:
        return str(resolved[BOOTSTRAP_FLAG]).strip() == "1", "tick"
    return False, "unset"


# --------------------------------------------------------------------------------------------
# The gate this exists to un-starve.
# --------------------------------------------------------------------------------------------


def gate_state(corpus_path: Path | None = None) -> dict[str, Any]:
    """Stage-2 readiness plus the two deficits the bootstrap is allowed to close."""
    summary = redirect_shadow.summarize(corpus_path or redirect_shadow.CORPUS_PATH)
    synced = int(summary.get("synced_role_outcomes") or 0)
    disagreements = int(summary.get("linked_disagreements") or 0)
    synced_needed = max(redirect_shadow.LINKED_OUTCOME_TARGET - synced, 0)
    disagreements_needed = max(redirect_shadow.DISAGREEMENT_OUTCOME_TARGET - disagreements, 0)
    return {
        "synced_role_outcomes": synced,
        "linked_disagreements": disagreements,
        "synced_needed": synced_needed,
        "disagreements_needed": disagreements_needed,
        "ready_for_supervised_apply": bool(summary.get("ready_for_supervised_apply")),
        "ready_for_historical_replay_analysis": bool(
            summary.get("ready_for_historical_replay_analysis")
        ),
        # The whole bootstrap turns itself off here. No date, no flag to re-check by hand.
        "bootstrap_needed": bool(synced_needed or disagreements_needed),
        "valid_proposals": int(summary.get("valid_proposals") or 0),
    }


# --------------------------------------------------------------------------------------------
# 1. The always-on linker: recorded state -> corpus link (never kill/release/delegate).
# --------------------------------------------------------------------------------------------


def pending_outcome_links(*, conn=None) -> list[dict[str, Any]]:
    """Redirect role runs whose stamped dispatch has reached a terminal outcome, not yet linked.

    Derived entirely from edges the dispatch itself wrote: an `influence_type='role'` edge with
    `accepted=1` exists only because the plan's `--influenced-by-role-run-id` stamp rode a real
    `dispatcher delegate`. Nothing here infers that advice was followed — the edge is the proof.
    """
    close = conn is None
    c = conn or feedback._conn()
    try:
        rows = c.execute(
            """SELECT ie.source_run_id, ie.target_run_id, o.adjudicated_verdict, o.durability
                 FROM influence_edges ie
                 JOIN outcomes o ON o.run_id = ie.target_run_id
                WHERE ie.influence_type = 'role'
                  AND ie.accepted = 1
                  AND ie.source_run_id LIKE ?
                  AND o.adjudicated_verdict IS NOT NULL
                ORDER BY ie.created_ts, ie.edge_id""",
            (ROLE_RUN_PREFIX + "%",),
        ).fetchall()
    finally:
        if close:
            c.close()
    return [
        {"role_run_id": r[0], "influenced_run_id": r[1], "verdict": r[2], "durability": r[3]}
        for r in rows
        if str(r[2] or "").upper() in TERMINAL_VERDICTS
    ]


def preview_link_applied_outcomes(*, corpus_path: Path | None = None, conn=None) -> dict[str, Any]:
    """Read-only: which applied redirects would be linked, without writing the corpus or brain."""
    return link_applied_outcomes(dry_run=True, corpus_path=corpus_path, conn=conn)


def link_applied_outcomes(
    *, dry_run: bool = False, corpus_path: Path | None = None, conn=None
) -> dict[str, Any]:
    """Append a `redirect_outcome_link` event per applied redirect that has an outcome.

    Writes: one `redirect_outcome_link` JSONL event per pending pair to the redirect corpus
    (``redirect_shadow.CORPUS_PATH`` unless overridden), via ``redirect_shadow.link_outcome``,
    which also records the accepted advice through ``feedback.join_role_to_outcome``.

    Never does: kill a live process, release a claim, or delegate a retry — those are
    ``apply_plan`` / ``apply_one`` only. Pass ``dry_run=True`` (or call
    ``preview_link_applied_outcomes``) to compute pending links without writing.
    """
    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    already = redirect_shadow.linked_pairs(corpus)
    pending = [
        row
        for row in pending_outcome_links(conn=conn)
        if (str(row["role_run_id"]), str(row["influenced_run_id"])) not in already
    ]
    linked: list[dict] = []
    for row in pending:
        if dry_run:
            linked.append({**row, "linked": False, "dry_run": True})
            continue
        result = redirect_shadow.link_outcome(
            str(row["role_run_id"]),
            str(row["influenced_run_id"]),
            accepted=True,
            notes="linked automatically from the applied redirect's own influence edge",
            corpus_path=corpus,
        )
        synced = bool(((result.get("event") or {}).get("link_result") or {}).get("synced"))
        linked.append({**row, "linked": True, "synced": synced})
        if synced:
            _heartbeat(
                "outcome",
                ref=str(row["influenced_run_id"]),
                metadata={"role_run_id": row["role_run_id"]},
            )
    return {
        "pending": len(pending),
        "linked": len([r for r in linked if r.get("linked")]),
        "dry_run": bool(dry_run),
        "links": linked[:20],
    }


# --------------------------------------------------------------------------------------------
# 2. Authorisation — a pure function, so it is testable without a lane.
# --------------------------------------------------------------------------------------------


def lane_liveness(report: dict, *, pid_checker=None) -> bool | None:
    """Is the prior lane alive? True, False, or None — and None means UNKNOWN, never False.

    The process table can answer only for a lane that HAS a process id. A report without one —
    every keepalive supervisor report, because that lane is a GitHub Actions run — or with one that
    is not a positive integer is unknown here. Whether an unknown lane may be acted on is
    `_lane_blocks`' question, answered from what the report itself says about the lane.
    """
    pid = report.get("pid")
    if pid is None:
        return None  # no pid: liveness is UNKNOWN, not "dead"
    try:
        pid_number = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_number <= 0:
        return None
    return bool((pid_checker or redirect_plan._pid_alive)(pid_number))


def lane_facts(report: dict, *, pid_checker=None) -> dict[str, Any]:
    """The three lane facts a report states, keyed as `authorize()` takes them.

    The one reading of a report: the free screen, `apply_one` and the keepalive supervisor's
    planner all call this, so none of them can read a different lane from the same report.
    """
    return {
        "pid_alive": lane_liveness(report, pid_checker=pid_checker),
        "lane_state": report.get("state"),
        "recommended_action": report.get("recommended_action"),
    }


def _report_lane_blocks(
    *, pid_alive: bool | None, lane_state: str | None, recommended_action: str | None
) -> list[str]:
    """The refusals that are facts about the lane as its REPORT describes it: liveness and the
    monitor's recommendation. No corpus, claim or gate is read, so a caller that holds only a
    report gets the same answer the screen would give."""
    state = str(lane_state or "")
    blocks: list[str] = []
    if pid_alive:
        blocks.append(LIVE_PID_BLOCK)
    elif state in REPORTED_LIVE_STATES:
        blocks.append(
            f"the supervisor reports the lane {state!r} — a live lane is never redirected"
        )
    elif pid_alive is None and state not in REPORTED_NOT_LIVE_STATES:
        blocks.append(
            f"lane liveness is UNKNOWN: no pid, and the report calls it {state or 'nothing'!r} — "
            "apply acts only on a lane shown stalled or dead"
        )
    if recommended_action in NO_REDIRECT_RECOMMENDATIONS:
        blocks.append(
            f"the supervisor recommends {recommended_action!r}, which no redirect improves on"
        )
    return blocks


def lane_refusals(report: dict, *, pid_checker=None) -> list[str]:
    """Why the screen refuses this report's lane before it pays a judge; empty means it doesn't.

    For a caller that holds only a report. The keepalive supervisor's planner asks this before it
    counts a candidate as needing a Stage-2 recording, so it counts the lanes the screen admits.
    """
    return _report_lane_blocks(**lane_facts(report, pid_checker=pid_checker))


def _lane_blocks(
    *,
    target: str,
    pid_alive: bool | None,
    lane_state: str | None,
    recommended_action: str | None,
    claim_holder: dict | None,
    prior_agent: str | None,
    gate: dict,
    applied_targets: set[str],
    applies_today: int,
) -> list[str]:
    """Every refusal that is a fact about the LANE or the CORPUS. None of them needs the proposal.

    ONE function, consumed by `screen_report()` before any offload and by `authorize()` after one,
    so the free screen can never pass a lane the authorisation would refuse, nor the reverse.
    `lane_state` / `recommended_action` of None mean the caller has no report to read them from and
    add nothing; a `pid_alive` of None is UNKNOWN and is refused unless the report says the lane is
    stalled or exited — the only two states in which its own monitor says it is not progressing.
    The lane half is `_report_lane_blocks`, which the planner reaches through `lane_refusals`.
    """
    blocks = _report_lane_blocks(
        pid_alive=pid_alive, lane_state=lane_state, recommended_action=recommended_action
    )
    if claim_holder:
        held_by = str(claim_holder.get("agent") or "")
        if prior_agent and held_by and held_by != prior_agent:
            blocks.append(f"target is claimed by {held_by!r}, not the stalled agent")
    if not gate.get("bootstrap_needed"):
        blocks.append("gate deficits are closed — bootstrap has finished its job")
    if target and target in applied_targets:
        blocks.append("this target has already been applied once")
    if applies_today >= MAX_APPLIES_PER_DAY:
        blocks.append(f"daily bound reached ({applies_today}/{MAX_APPLIES_PER_DAY})")
    return blocks


def authorize(
    *,
    plan_obj: dict,
    role_run_id: str | None,
    decision_source: str | None,
    errors: list | None,
    pid_alive: bool | None,
    claim_holder: dict | None,
    prior_agent: str | None,
    gate: dict,
    applied_targets: set[str],
    applies_today: int,
    flag_on: bool,
    lane_state: str | None = None,
    recommended_action: str | None = None,
) -> dict[str, Any]:
    """Decide apply/refuse from recorded state alone. No human, no review queue.

    Every block below is a fact about the lane or the corpus, never a judgement about the
    proposal's content — the proposal's quality is exactly what the resulting outcome measures.
    `pid_alive=None` is an UNKNOWN lane (see `lane_liveness`); pass the report's `state` and
    `recommended_action` so the same lane rules the free screen applied are applied again here.
    """
    target = str(plan_obj.get("target") or "")
    blocks: list[str] = []
    if plan_obj.get("action") not in redirect_plan.APPLY_ACTIONS:
        blocks.append(f"plan action {plan_obj.get('action')!r} is not applyable")
    if decision_source != "redirect_agent" or errors or not role_run_id:
        blocks.append("proposal was not a valid accepted RedirectAgent decision")
    # Refuse to apply advice that cannot be measured. An un-stamped plan would mutate a lane and
    # teach the learner nothing, which is the worst of both.
    if role_run_id and plan_obj.get("accepted_role_run_id") != role_run_id:
        blocks.append("plan does not carry the role lineage stamp")
    delegate_steps = [
        step
        for step in plan_obj.get("steps") or []
        if step.get("id") in redirect_plan.APPLY_STEP_IDS
    ]
    if not delegate_steps:
        blocks.append("plan has no mutating step to apply")
    for step in delegate_steps:
        for command in step.get("commands") or []:
            if redirect_plan._has_placeholder(command):
                blocks.append("plan still contains a placeholder command")
                break
    if not plan_obj.get("prompt_text") or not plan_obj.get("prompt_file"):
        blocks.append("plan is missing prompt_text/prompt_file")
    blocks.extend(
        _lane_blocks(
            target=target,
            pid_alive=pid_alive,
            lane_state=lane_state,
            recommended_action=recommended_action,
            claim_holder=claim_holder,
            prior_agent=prior_agent,
            gate=gate,
            applied_targets=applied_targets,
            applies_today=applies_today,
        )
    )
    return {
        "allowed": not blocks,
        "blocks": blocks,
        "target": target,
        "role_run_id": role_run_id,
        "flag_on": bool(flag_on),
        # Authorised but flag-off is the normal resting state, not an error.
        "would_mutate": bool(not blocks and flag_on),
        "closes_disagreement_deficit": bool(gate.get("disagreements_needed")),
    }


def _applied_history(corpus_path: Path, *, now: int | None = None) -> tuple[set[str], int]:
    events = redirect_shadow.applied_events(corpus_path)
    stamp = int(time.time()) if now is None else int(now)
    targets = {str(e.get("target")) for e in events if e.get("applied") and e.get("target")}
    today = len(
        [e for e in events if e.get("applied") and int(e.get("ts") or 0) >= stamp - DAY_SECONDS]
    )
    return targets, today


# --------------------------------------------------------------------------------------------
# 3. The driver.
# --------------------------------------------------------------------------------------------


def apply_one(
    *,
    report: dict,
    acceptance_criteria: str,
    backend: str | None = None,
    corpus_path: Path | None = None,
    env: dict | None = None,
    now: int | None = None,
    role_runner=None,
    apply_runner=None,
    pid_checker=None,
) -> dict[str, Any]:
    """Run RedirectAgent on one report, authorise, and apply only if the flag allows it.

    The role is run here rather than replayed from the corpus because the corpus stores a plan
    SUMMARY: applying needs the real argv, including the lineage stamp.
    """
    import roles

    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    runner = role_runner or roles.run_redirect_agent
    pid_alive_fn = pid_checker or redirect_plan._pid_alive
    gate = gate_state(corpus)
    _heartbeat(
        "match",
        ref=str(report.get("target") or ""),
        metadata={"bootstrap_needed": gate["bootstrap_needed"]},
    )

    result = runner(
        report,
        acceptance_criteria,
        backend=backend,
        dispatch=True,
        lane=report.get("lane"),
        task_type=report.get("task_type"),
    )
    plan_obj = result.get("plan") or {}
    role_run_id = result.get("role_run_id")

    # Log the proposal to the corpus exactly as the shadow sweep would, so the gate sees it.
    redirect_shadow.record_proposal(
        role_run_id=role_run_id,
        report=report,
        acceptance_criteria=acceptance_criteria,
        proposal=result.get("proposal"),
        baseline=result.get("baseline") or {},
        decision_source=result.get("decision_source") or "",
        errors=result.get("errors"),
        backend=result.get("backend"),
        backend_run_id=result.get("backend_run_id"),
        plan=plan_obj,
        corpus_path=corpus,
        source="live-dispatch",
    )

    applied_targets, applies_today = _applied_history(corpus, now=now)
    authorization = authorize(
        plan_obj=plan_obj,
        role_run_id=role_run_id,
        decision_source=result.get("decision_source"),
        errors=result.get("errors"),
        claim_holder=claims.holder(str(report.get("target") or "")),
        prior_agent=report.get("agent"),
        gate=gate,
        applied_targets=applied_targets,
        applies_today=applies_today,
        flag_on=flag_enabled(env),
        **lane_facts(report, pid_checker=pid_alive_fn),
    )
    apply_result = None
    if authorization["would_mutate"]:
        _heartbeat("invocation", ref=authorization["target"], metadata={"role_run_id": role_run_id})
        kwargs = {"confirm_target": authorization["target"], "pid_checker": pid_alive_fn}
        if apply_runner is not None:
            kwargs["runner"] = apply_runner
        apply_result = redirect_plan.apply_plan(plan_obj, **kwargs)
        if apply_result.get("applied"):
            _heartbeat(
                "success", ref=authorization["target"], metadata={"role_run_id": role_run_id}
            )
    redirect_shadow.record_apply(
        role_run_id=role_run_id,
        target=authorization["target"],
        plan_action=plan_obj.get("action"),
        authorization=authorization,
        apply_result=apply_result,
        dry_run=not authorization["would_mutate"],
        corpus_path=corpus,
    )
    return {
        "gate": gate,
        "authorization": authorization,
        "apply_result": apply_result,
        "role_run_id": role_run_id,
        "target": authorization["target"],
        "decision_source": result.get("decision_source"),
        "errors": result.get("errors") or [],
    }


def default_stage2_plan_path() -> Path:
    """Where the stage-2 plan step writes its plan: `$ORCH_STATE_DIR/<the registry's artifact>`.

    orchestrate.sh passes the path explicitly; this default serves an operator's shell. The file
    NAME is read from the cadence registry rather than re-typed, so the step that writes it and the
    step that reads it cannot name two different files.
    """
    state_dir = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    return state_dir / str(cadence_registry.STEP_BY_KEY[STAGE2_PLAN_STEP]["artifact"])


def stage2_population(plan_path: Path | None = None, *, now: int | None = None) -> dict[str, Any]:
    """The supervisor's CURRENT candidates: the eligible plans of its latest stage-2 run.

    THE MEASURING WINDOW IS THE DRAINING WINDOW. The supervisor decides who is a candidate each time
    it runs, but nothing prunes the report files it leaves behind, so a directory glob is every PR
    that was EVER a candidate — 30 files, every one for a closed PR, on 2026-10-02. The plan is the
    one artifact that says who is a candidate NOW, and the same cadence step writes it atomically.

    `candidates` is a list — possibly EMPTY, which is a measurement — or None when the population
    cannot be known, and `status` says which: `current`, or `missing` / `unreadable` / `stale` with
    a `reason` naming what clears it. A plan older than `cadence_registry.stale_after_seconds`
    allows its own step is not a population: the PRs in it may have closed since.
    """
    path = Path(plan_path) if plan_path else default_stage2_plan_path()
    max_age = cadence_registry.stale_after_seconds(cadence_registry.STEP_BY_KEY[STAGE2_PLAN_STEP])
    current = int(time.time()) if now is None else int(now)
    clears = f"the next successful {STAGE2_PLAN_STEP} run clears this"
    out: dict[str, Any] = {
        "path": str(path),
        "max_age_s": max_age,
        "generated_at": None,
        "age_s": None,
        "candidates": None,
        "ineligible": 0,
    }
    if not path.is_file():
        return {**out, "status": "missing", "reason": f"no stage-2 plan at {path}; {clears}"}
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
        generated_at = int(plan["generated_at"])
        entries = plan["plans"]
        if not isinstance(entries, list):
            raise TypeError("'plans' is not a list")
    except Exception as exc:  # noqa: BLE001
        return {
            **out,
            "status": "unreadable",
            "reason": f"stage-2 plan at {path} is unreadable ({str(exc)[:80]}); {clears}",
        }
    age = current - generated_at
    out.update(generated_at=generated_at, age_s=age)
    if age > max_age:
        return {
            **out,
            "status": "stale",
            "reason": f"stage-2 plan is {age // 3600}h old, past the {max_age // 3600}h the "
            f"cadence registry allows its step; {clears}",
        }
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        report = entry.get("report") if isinstance(entry, dict) else None
        # The supervisor's single-authority rule decides eligibility; this only honours it.
        eligible = isinstance(entry, dict) and entry.get("eligible") is True
        if not eligible or not isinstance(report, dict) or not report.get("target"):
            out["ineligible"] += 1
            continue
        candidates.append(
            {"report": report, "acceptance_criteria": str(entry.get("acceptance_criteria") or "")}
        )
    return {**out, "status": "current", "reason": "", "candidates": candidates}


def _reports_on_disk(report_dir: Path) -> list[dict[str, Any]]:
    """Every report file left in the supervisor's directory, in candidate shape.

    COUNTED, never judged: being on disk is not being a candidate. An unreadable file keeps its
    file name as its target so it is still counted.
    """
    directory = Path(report_dir)
    paths = (
        sorted(directory.glob("*.keepalive-supervisor-report.json")) if directory.is_dir() else []
    )
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            report = None
        if not isinstance(report, dict) or not report.get("target"):
            report = {"target": path.name}
        rows.append({"report": report, "acceptance_criteria": ""})
    return rows


def judgement_key(report: dict, acceptance_criteria: str) -> tuple[str, str, str | None]:
    """What the role is asked, as the corpus records it: the target plus the SAME two digests
    `redirect_shadow.build_entry` writes, so a key computed here matches one recorded there."""
    return (
        str(report.get("target") or ""),
        redirect_shadow._sha_json(report),
        redirect_shadow._sha_text(acceptance_criteria),
    )


def judged_inputs(corpus_path: Path) -> tuple[dict[tuple, dict[str, Any]], dict[str, int]]:
    """What the role has already answered, read from the corpus' own proposal events.

    Returns `({judgement_key: newest valid verdict}, {target: newest judgement ts})`. Only a VALID
    live judgement is an answer: a backend failure or a rejected proposal answered nothing, so it
    never suppresses a retry. The second map serves each run's candidates least-recently-judged
    first, which is what keeps `MAX_OFFLOADS_PER_RUN` from starving any of them.
    """
    answers: dict[tuple, dict[str, Any]] = {}
    last_judged: dict[str, int] = {}
    for event in redirect_shadow._iter_events(corpus_path):
        if event.get("kind") != "redirect_proposal" or event.get("source") != "live-dispatch":
            continue
        target = str(event.get("target") or "")
        ts = int(event.get("ts") or 0)
        last_judged[target] = max(last_judged.get(target, 0), ts)
        if not event.get("valid_proposal"):
            continue
        key = (target, event.get("report_sha256"), event.get("acceptance_criteria_sha256"))
        if ts >= int((answers.get(key) or {}).get("ts") or 0):
            answers[key] = {"action": event.get("proposal_action"), "ts": ts}
    return answers, last_judged


def _utc_day(ts: Any) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts or 0)))


def screen_report(
    report: dict,
    *,
    gate: dict,
    applied_targets: set[str],
    applies_today: int,
    pid_checker=None,
    judged: dict | None = None,
    acceptance_criteria: str = "",
) -> dict[str, Any]:
    """The subset of authorisation that needs NO role run — so asking costs nothing.

    Written after asking "would anything be authorised right now?" the expensive way and spending
    17 live gemini offloads to hear "no". Everything here is a fact about the lane or the corpus;
    only the plan-shaped blocks (action, mutating step, prompt, lineage stamp) need the role. The
    lane facts go through `_lane_blocks`, exactly as `authorize()`'s do, so the two cannot
    disagree. With `judged` (from `judged_inputs`) it adds the one block authorisation has no use
    for: this exact input was already answered with a verdict no plan can apply.
    """
    target = str(report.get("target") or "")
    blocks = _lane_blocks(
        target=target,
        **lane_facts(report, pid_checker=pid_checker),
        claim_holder=claims.holder(target) if target else None,
        prior_agent=report.get("agent"),
        gate=gate,
        applied_targets=applied_targets,
        applies_today=applies_today,
    )
    answered = (judged or {}).get(judgement_key(report, acceptance_criteria))
    if answered and answered.get("action") not in redirect_plan.APPLY_ACTIONS:
        blocks.append(
            f"this exact report was already judged {answered.get('action')!r} on "
            f"{_utc_day(answered.get('ts'))} — identical input is not re-judged; a changed report "
            "clears this"
        )
    return {"target": target, "passes_screen": not blocks, "blocks": blocks}


def _screen(
    *,
    report_dir: Path,
    plan_path: Path | None,
    corpus_path: Path,
    acceptance_criteria: str = "",
    pid_checker=None,
    now: int | None = None,
) -> dict[str, Any]:
    """Screen the supervisor's current candidates, free.

    Shared by `screen_candidates` (the drainable count) and `apply_candidates` (which spends only on
    what passes), so the number a run prints is the number that run acted on.
    """
    population = stage2_population(plan_path, now=now)
    on_disk = _reports_on_disk(report_dir)
    gate = gate_state(corpus_path)
    applied_targets, applies_today = _applied_history(corpus_path, now=now)
    judged, last_judged = judged_inputs(corpus_path)
    candidates = population["candidates"]  # the supervisor's latest run, never the directory
    current_targets = {str(c["report"].get("target")) for c in candidates or []}
    rows: list[dict[str, Any]] = []
    for cand in candidates or []:
        report = cand["report"]
        criteria = (
            acceptance_criteria
            or str(report.get("acceptance_criteria") or "")
            or cand["acceptance_criteria"]
        )
        verdict = screen_report(
            report,
            gate=gate,
            applied_targets=applied_targets,
            applies_today=applies_today,
            pid_checker=pid_checker,
            judged=judged,
            acceptance_criteria=criteria,
        )
        rows.append(
            {
                **verdict,
                "report": report,
                "acceptance_criteria": criteria,
                "last_judged": last_judged.get(verdict["target"], 0),
            }
        )
    passing = sorted(
        (row for row in rows if row["passes_screen"]),
        key=lambda row: (row["last_judged"], row["target"]),
    )
    on_disk_targets = (str(row["report"].get("target") or "") for row in on_disk)
    return {
        "population": {k: v for k, v in population.items() if k != "candidates"},
        "gate": gate,
        "reports_on_disk": len(on_disk),
        "stale_targets": sorted(t for t in on_disk_targets if t not in current_targets),
        "rows": rows,
        "passing": passing,
        "passing_screen": None if candidates is None else len(passing),
    }


def screen_candidates(
    *,
    report_dir: Path | None = None,
    plan_path: Path | None = None,
    corpus_path: Path | None = None,
    pid_checker=None,
    now: int | None = None,
) -> dict[str, Any]:
    """How many current candidates could possibly be authorised, without running a single role.

    This is the bootstrap's DRAINABLE quantity, the number printed beside the gate's deficits.
    `passing_screen` is an int — 0 is a measurement — or None when the population itself is
    unknown, with the reason in `population`.
    """
    import keepalive_supervisor

    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    directory = report_dir or keepalive_supervisor.DEFAULT_STAGE2_REPORT_DIR
    screen = _screen(
        report_dir=Path(directory),
        plan_path=plan_path,
        corpus_path=corpus,
        pid_checker=pid_checker,
        now=now,
    )
    return {
        "reports_seen": screen["reports_on_disk"],
        "current_candidates": None if screen["passing_screen"] is None else len(screen["rows"]),
        "stale_reports": len(screen["stale_targets"]),
        "stale_targets": screen["stale_targets"],
        "gate": screen["gate"],
        "passing_screen": screen["passing_screen"],
        "population": screen["population"],
        "report_dir": str(directory),
        "candidates": [
            {key: row[key] for key in ("target", "passes_screen", "blocks")}
            for row in screen["rows"]
        ],
        "note": "a candidate passing the screen still needs the role to propose "
        "redirect/decompose; the screen costs no offload",
    }


def apply_candidates(
    *,
    report_dir: Path | None = None,
    plan_path: Path | None = None,
    limit: int = MAX_APPLIES_PER_DAY,
    max_offloads: int = MAX_OFFLOADS_PER_RUN,
    corpus_path: Path | None = None,
    acceptance_criteria: str = "",
    backend: str | None = None,
    dry_run: bool = False,
    spend_offloads: bool = False,
    env: dict | None = None,
    role_runner=None,
    apply_runner=None,
    pid_checker=None,
    now: int | None = None,
) -> dict[str, Any]:
    """Screen the supervisor's current candidates for free, then judge only what passes.

    Stops at the first authorised apply (limit), because the daily bound is 1 and a bootstrap that
    fires N times on one tick is not a bootstrap. Spends at most `max_offloads` role runs, serving
    the least-recently-judged candidates first. Every run reports BOTH quantities —
    `offloads_spent` and `passing_screen` — because either one alone can hide a deadlock.
    """
    import keepalive_supervisor

    directory = Path(report_dir or keepalive_supervisor.DEFAULT_STAGE2_REPORT_DIR)
    # COST HONESTY. apply_one() runs RedirectAgent, which spends a real backend offload. With the
    # flag off there is nothing we could do with the proposal, so authorising on a disarmed
    # bootstrap would spend capacity to learn nothing. apply_plan's contract is exercised by the
    # selftest with injected runners instead. An operator can still force it with --dry-run.
    if not flag_enabled(env) and not (dry_run and spend_offloads):
        return {
            "reports_seen": 0,
            "considered": 0,
            "authorized": 0,
            "applied": 0,
            "offloads_spent": 0,
            # NOT MEASURED, which is not zero: with the flag off nothing is read or spent.
            "passing_screen": None,
            "skipped": f"{BOOTSTRAP_FLAG} is off; authorising would spend one backend "
            f"offload per candidate — pass --dry-run --spend-offloads to force it, "
            f"or --screen for the free subset",
            "report_dir": str(directory),
            "results": [],
        }

    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    screen = _screen(
        report_dir=directory,
        plan_path=plan_path,
        corpus_path=corpus,
        acceptance_criteria=acceptance_criteria,
        pid_checker=pid_checker,
        now=now,
    )
    results: list[dict] = [
        {
            "target": row["target"],
            "authorized": False,
            "applied": False,
            "offloaded": False,
            "blocks": row["blocks"],
        }
        for row in screen["rows"]
        if not row["passes_screen"]
    ]
    authorized = applied = offloads = deferred = 0
    for row in screen["passing"]:
        held: str | None = None
        if applied >= max(int(limit), 0):
            held = "not judged: this run already made its bounded apply"
        elif offloads >= max(int(max_offloads), 0):
            deferred += 1
            held = (
                f"deferred: the per-run offload cap is spent ({offloads}/{max_offloads}); "
                "least-recently-judged candidates are served first next run"
            )
        if held:
            results.append(
                {
                    "target": row["target"],
                    "authorized": False,
                    "applied": False,
                    "offloaded": False,
                    "deferred": True,
                    "blocks": [held],
                }
            )
            continue
        offloads += 1
        outcome = apply_one(
            report=row["report"],
            acceptance_criteria=row["acceptance_criteria"],
            backend=backend,
            corpus_path=corpus,
            env={} if dry_run else env,
            now=now,
            role_runner=role_runner,
            apply_runner=apply_runner,
            pid_checker=pid_checker,
        )
        auth = outcome["authorization"]
        was_applied = bool((outcome.get("apply_result") or {}).get("applied"))
        authorized += 1 if auth["allowed"] else 0
        applied += 1 if was_applied else 0
        results.append(
            {
                "target": outcome["target"],
                "authorized": auth["allowed"],
                "applied": was_applied,
                "offloaded": True,
                "blocks": auth["blocks"],
                "role_run_id": outcome["role_run_id"],
            }
        )
    return {
        "reports_seen": screen["reports_on_disk"],
        "current_candidates": None if screen["passing_screen"] is None else len(screen["rows"]),
        "stale_reports": len(screen["stale_targets"]),
        "stale_targets": screen["stale_targets"],
        "considered": len(screen["rows"]),
        "passing_screen": screen["passing_screen"],
        "offloads_spent": offloads,
        "deferred_by_cap": deferred,
        "max_offloads": max(int(max_offloads), 0),
        "authorized": authorized,
        "applied": applied,
        "population": screen["population"],
        "report_dir": str(directory),
        "results": results,
    }


def status(
    corpus_path: Path | None = None,
    *,
    env: dict | None = None,
    report_dir: Path | None = None,
    plan_path: Path | None = None,
    pid_checker=None,
    now: int | None = None,
    link_preview: bool = True,
) -> dict[str, Any]:
    """Read-only: where the gate stands, what could drain it, whether the bootstrap is armed.

    The gate's BLOCKING quantity is its two deficits; its DRAINABLE quantity is how many current
    candidates could possibly be authorised — free to compute, and reported in the same answer,
    because `5/3` alone read as "be patient" for 42 days while the drainable count was 0.

    `link_preview=False` skips the one Brain read, the applied-outcome link preview, for a caller
    that needs only the gate and its drain (`switch_review`'s ON-but-idle row). That read opens
    `feedback._conn()`, which runs the schema script and its migrations and commits, and creates the
    Brain where none exists. Skipped, both link fields are None: NOT READ, never zero.
    """
    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    gate = gate_state(corpus)
    applied_targets, applies_today = _applied_history(corpus, now=now)
    links = preview_link_applied_outcomes(corpus_path=corpus) if link_preview else None
    drain = screen_candidates(
        report_dir=report_dir,
        plan_path=plan_path,
        corpus_path=corpus,
        pid_checker=pid_checker,
        now=now,
    )
    if env is None:
        flag_on, flag_source = flag_as_the_tick_sees_it()
    else:
        flag_on, flag_source = flag_enabled(env), "explicit"
    return {
        "capability_id": CAPABILITY_ID,
        "flag": BOOTSTRAP_FLAG,
        "flag_on": flag_on,
        "flag_source": flag_source,
        "gate": gate,
        # None = the population is unknown (reason in `drainable_population`); 0 is a measurement.
        "drainable": drain["passing_screen"],
        "drainable_population": drain["population"],
        "current_candidates": drain["current_candidates"],
        "reports_on_disk": drain["reports_seen"],
        "stale_reports": drain["stale_reports"],
        "applied_targets": sorted(applied_targets),
        "applies_today": applies_today,
        "daily_bound": MAX_APPLIES_PER_DAY,
        "unlinked_applied_outcomes": None if links is None else int(links["pending"]),
        "pending_outcome_links": None if links is None else links["links"],
    }


def format_drainable(
    passing: int | None,
    *,
    current: int | None,
    on_disk: int,
    stale: int,
    population: dict,
) -> str:
    """One sentence for the drainable quantity. An unknown population and a measured zero must
    never print alike: only one of them is a finding about the lanes."""
    if passing is None:
        return f"UNKNOWN — {population.get('reason') or 'the candidate population was not read'}"
    return (
        f"{passing} of {current} current candidates could be authorised "
        f"({on_disk} report files on disk, {stale} stale: not in the supervisor's latest run)"
    )


def format_status(out: dict) -> list[str]:
    gate = out["gate"]
    closed = not gate["bootstrap_needed"]
    unlinked = out["unlinked_applied_outcomes"]
    return [
        f"{CAPABILITY_ID}: flag {BOOTSTRAP_FLAG}="
        f"{'1 (ARMED)' if out['flag_on'] else '0 (off)'} "
        f"[as the tick sees it; source: {out['flag_source']}]",
        f"  gate: synced_role_outcomes={gate['synced_role_outcomes']} "
        f"(need {gate['synced_needed']} more), "
        f"linked_disagreements={gate['linked_disagreements']} "
        f"(need {gate['disagreements_needed']} more)"
        + (" — FINISHED: the deficits are closed, so --apply judges nothing" if closed else ""),
        "  drainable: "
        + format_drainable(
            out["drainable"],
            current=out["current_candidates"],
            on_disk=out["reports_on_disk"],
            stale=out["stale_reports"],
            population=out["drainable_population"],
        ),
        f"  ready_for_supervised_apply={gate['ready_for_supervised_apply']} "
        f"bootstrap_needed={gate['bootstrap_needed']}",
        f"  applied today {out['applies_today']}/{out['daily_bound']}; "
        f"unlinked applied outcomes: {'not read' if unlinked is None else unlinked}",
    ]


def format_apply(out: dict, *, flag_on: bool) -> list[str]:
    """The per-run summary: what was SPENT beside what could still be authorised, then each
    candidate's verdict. Stale report files are counted, never listed one per line."""
    if out.get("skipped"):
        drainable = f"UNKNOWN — not evaluated: {out['skipped']}"
    else:
        drainable = format_drainable(
            out["passing_screen"],
            current=out.get("current_candidates"),
            on_disk=out["reports_seen"],
            stale=out.get("stale_reports", 0),
            population=out.get("population") or {},
        )
    lines = [
        f"offloads_spent={out['offloads_spent']} authorized={out['authorized']} "
        f"applied={out['applied']} (flag {BOOTSTRAP_FLAG}={'1' if flag_on else '0'})",
        f"  could authorise: {drainable}",
    ]
    if out.get("deferred_by_cap"):
        lines.append(
            f"  deferred by the per-run cap: {out['deferred_by_cap']} "
            f"(cap {out.get('max_offloads')} offloads/run)"
        )
    for row in out["results"]:
        if row.get("applied"):
            verdict = "APPLIED"
        elif row.get("authorized"):
            verdict = "authorized (flag off)"
        elif row.get("offloaded"):
            verdict = "refused"
        elif row.get("deferred"):
            verdict = "deferred (no offload)"
        else:
            verdict = "screened out (no offload)"
        lines.append(f"  {row['target']}: {verdict}")
        lines.extend(f"      - {block}" for block in row.get("blocks") or [])
    return lines


def _recording_role_runner(spent: list):
    """Record the call and return an empty proposal.

    Was `lambda *a, **k: spent.append(a) or {}` — a deliberate "record, then yield {}" idiom that
    reads as a bug to a checker, because `list.append` returns None and `X or {}` therefore always
    takes the right branch. Same behaviour, stated instead of implied.
    """

    def runner(*a, **k) -> dict:
        spent.append(a)
        return {}

    return runner


def _corpus_digest(corpus: Path) -> str:
    """SHA-256 of the corpus file bytes; empty file hashes the empty string."""
    if not corpus.is_file():
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(corpus.read_bytes()).hexdigest()


def _outcome_link_event_count(corpus: Path) -> int:
    return len(redirect_shadow.linked_pairs(corpus))


def _write_stage2_plan(path: Path, reports: list[dict], *, generated_at: int) -> None:
    """A stage-2 plan in the shape keepalive_supervisor writes: one eligible plan per report."""
    plans = [
        {"target": rep["target"], "eligible": True, "report": rep, "acceptance_criteria": "AC"}
        for rep in reports
    ]
    path.write_text(json.dumps({"generated_at": generated_at, "plans": plans}), encoding="utf-8")


def _selftest_prescreen(tmp: Path) -> None:
    """The 2026-10-02 relationship fixes, each against the input that exposed it."""
    import roles

    corpus = tmp / "prescreen-corpus.jsonl"
    report_dir = tmp / "prescreen-reports"
    report_dir.mkdir()
    plan_path = tmp / "prescreen-plan.json"
    now = int(time.time())
    open_gate = {"bootstrap_needed": True, "disagreements_needed": 3}
    spent: list = []

    def must_not_apply(*_a, **_k):
        raise AssertionError("apply_plan was reached when it must not be")

    def judge(action: str):
        verdict = {
            "action": action,
            "reason": "fixture verdict",
            "confidence": "high",
            "corrected_prompt": "Retry with fresh auth, keep scope, validate, push, open PR.",
            "switch_agent": "codex",
        }

        def runner(rep, ac, **kwargs):
            spent.append(rep.get("target"))
            return roles.run_redirect_agent(rep, ac, proposal_json=verdict, **kwargs)

        return runner

    # A keepalive lane exactly as keepalive_supervisor writes it: NO pid.
    lane = {"agent": "keepalive", "lane": "closer", "task_type": "implement"}

    # ---- a pid-less lane the supervisor calls RUNNING is refused, even when the judge says
    # redirect. Until 2026-10-02 `pid is None` read as dead and this applied: release-claim +
    # delegate on a lane keepalive was still driving, with the judge's verdict the only guard.
    running = {**lane, "target": "o/r#11", "state": "running", "recommended_action": "inspect"}
    remote = apply_one(
        report=running,
        acceptance_criteria="AC",
        backend="codex",
        corpus_path=corpus,
        env={BOOTSTRAP_FLAG: "1"},
        role_runner=judge("redirect"),
        pid_checker=lambda pid: False,
        apply_runner=must_not_apply,
    )
    assert remote["authorization"]["allowed"] is False, remote["authorization"]
    assert any("reports the lane 'running'" in b for b in remote["authorization"]["blocks"]), remote
    # The same, for the lane whose liveness NOTHING shows: no pid, and a state neither live nor
    # stalled. The state rule cannot catch this one, so it proves the UNKNOWN rule end to end.
    silent = {**lane, "target": "o/r#10", "state": "missing", "recommended_action": "inspect"}
    blind = apply_one(
        report=silent,
        acceptance_criteria="AC",
        backend="codex",
        corpus_path=corpus,
        env={BOOTSTRAP_FLAG: "1"},
        role_runner=judge("redirect"),
        pid_checker=lambda pid: False,
        apply_runner=must_not_apply,
    )
    assert blind["authorization"]["allowed"] is False, blind["authorization"]
    assert any("liveness is UNKNOWN" in b for b in blind["authorization"]["blocks"]), blind

    # ---- no pid and a state that says nothing about liveness: UNKNOWN, which is refused; the
    # supervisor's own "stalled" is the evidence that lets an unknown-pid lane through.
    def stamped(target: str) -> dict:
        return {
            "action": "redirect",
            "target": target,
            "prompt_text": "x",
            "prompt_file": "f",
            "accepted_role_run_id": "role:redirect:codex:1",
            "steps": [{"id": "delegate-retry", "commands": [["python3", "d.py"]]}],
        }

    common: dict[str, Any] = {
        "role_run_id": "role:redirect:codex:1",
        "decision_source": "redirect_agent",
        "errors": [],
        "pid_alive": None,
        "claim_holder": None,
        "prior_agent": "keepalive",
        "gate": open_gate,
        "applied_targets": set(),
        "applies_today": 0,
        "flag_on": True,
    }
    unknown = authorize(
        plan_obj=stamped("o/r#12"), lane_state="missing", recommended_action="inspect", **common
    )
    assert unknown["allowed"] is False and any("UNKNOWN" in b for b in unknown["blocks"]), unknown
    stalled = authorize(
        plan_obj=stamped("o/r#13"), lane_state="stalled", recommended_action="inspect", **common
    )
    assert stalled["allowed"] is True, stalled
    waiting = authorize(
        plan_obj=stamped("o/r#14"), lane_state="stalled", recommended_action="wait", **common
    )
    assert waiting["allowed"] is False, waiting
    assert any("recommends 'wait'" in b for b in waiting["blocks"]), waiting

    # ---- the population is the supervisor's latest run, never the directory. The stale report is
    # a PR that was stalled when written and has closed since: it would pass every lane rule.
    stale = {**lane, "target": "o/r#20", "state": "stalled", "recommended_action": "inspect"}
    current = {**lane, "target": "o/r#21", "state": "stalled", "recommended_action": "inspect"}
    active = {**lane, "target": "o/r#22", "state": "running", "recommended_action": "wait"}
    for rep in (stale, current, active):
        name = rep["target"].replace("/", "__").replace("#", "__")
        (report_dir / f"{name}.keepalive-supervisor-report.json").write_text(json.dumps(rep))
    _write_stage2_plan(plan_path, [current, active], generated_at=now)
    run: dict[str, Any] = {
        "report_dir": report_dir,
        "plan_path": plan_path,
        "corpus_path": corpus,
        "env": {BOOTSTRAP_FLAG: "1"},
        "role_runner": judge("inspect"),
        "apply_runner": must_not_apply,
        "pid_checker": lambda pid: False,
        "now": now,
    }
    spent.clear()
    first = apply_candidates(**run)
    assert spent == ["o/r#21"], spent
    seen = (first["reports_seen"], first["current_candidates"], first["stale_reports"])
    assert seen == (3, 2, 1), first
    assert (first["passing_screen"], first["offloads_spent"]) == (1, 1), first

    # ---- identical input is judged once: the same report, answered "inspect", is not re-asked.
    spent.clear()
    again = apply_candidates(**run)
    assert spent == [] and (again["passing_screen"], again["offloads_spent"]) == (0, 0), again
    assert any("already judged 'inspect'" in b for r in again["results"] for b in r["blocks"])

    # ---- the per-run cap judges 3 of 5 and defers 2, which go FIRST on the next run.
    many = [
        {**lane, "target": f"o/r#3{i}", "state": "stalled", "recommended_action": "inspect"}
        for i in range(5)
    ]
    _write_stage2_plan(plan_path, many, generated_at=now)
    spent.clear()
    capped = apply_candidates(**run, max_offloads=3)
    assert len(spent) == 3 and capped["deferred_by_cap"] == 2, capped
    judged_first = set(spent)
    changed = [{**rep, "hints": [{"kind": "auth"}]} for rep in many]  # no identical re-ask
    _write_stage2_plan(plan_path, changed, generated_at=now)
    spent.clear()
    apply_candidates(**run, max_offloads=2)
    assert set(spent) == {rep["target"] for rep in many} - judged_first, spent

    # ---- a stale or missing plan is an UNKNOWN population: nothing judged, and never a zero.
    _write_stage2_plan(plan_path, [current], generated_at=now - 10 * DAY_SECONDS)
    spent.clear()
    old = apply_candidates(**run)
    assert spent == [] and old["passing_screen"] is None, old
    assert old["population"]["status"] == "stale", old["population"]
    gone = apply_candidates(**{**run, "plan_path": tmp / "absent-plan.json"})
    assert gone["passing_screen"] is None and gone["population"]["status"] == "missing", gone

    # ---- the renderings: a drained gate says FINISHED; unknown and zero never print alike.
    drained = {
        "flag_on": True,
        "flag_source": "explicit",
        "gate": {
            "synced_role_outcomes": 10,
            "linked_disagreements": 3,
            "synced_needed": 0,
            "disagreements_needed": 0,
            "ready_for_supervised_apply": True,
            "bootstrap_needed": False,
        },
        "drainable": 0,
        "drainable_population": {"status": "current", "reason": ""},
        "current_candidates": 2,
        "reports_on_disk": 3,
        "stale_reports": 1,
        "applies_today": 0,
        "daily_bound": MAX_APPLIES_PER_DAY,
        "unlinked_applied_outcomes": 0,
    }
    assert any("FINISHED" in line for line in format_status(drained)), format_status(drained)
    zero = format_drainable(0, current=2, on_disk=3, stale=1, population={})
    unknown_text = format_drainable(
        None, current=None, on_disk=3, stale=0, population={"reason": "no stage-2 plan at X"}
    )
    assert zero.startswith("0 of 2 current candidates"), zero
    assert unknown_text.startswith("UNKNOWN — no stage-2 plan"), unknown_text

    # ---- link_preview=False opens no Brain, and its link fields are NOT READ, never a zero. The
    # gate and the drain are still read: they are what the caller (switch_review's row) asked for.
    _write_stage2_plan(plan_path, [current], generated_at=now)
    brain_opens: list = []

    def brain_tripwire():
        brain_opens.append("feedback._conn")
        raise AssertionError("status(link_preview=False) opened the Brain")

    saved_conn, feedback._conn = feedback._conn, brain_tripwire
    try:
        lean = status(
            corpus,
            env={BOOTSTRAP_FLAG: "1"},
            report_dir=report_dir,
            plan_path=plan_path,
            now=now,
            link_preview=False,
        )
    finally:
        feedback._conn = saved_conn
    assert brain_opens == [], brain_opens
    assert lean["unlinked_applied_outcomes"] is None and lean["pending_outcome_links"] is None, lean
    assert lean["drainable"] is not None and lean["current_candidates"] == 1, lean
    assert any("unlinked applied outcomes: not read" in line for line in format_status(lean)), lean


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="redirect-apply-"))
    old_db, old_corpus = feedback.DB_PATH, redirect_shadow.CORPUS_PATH
    old_prompt_dir = redirect_plan.PROMPT_DIR
    feedback.DB_PATH = tmp / "brain.db"
    corpus = tmp / "corpus.jsonl"
    redirect_plan.PROMPT_DIR = tmp / "prompts"
    # status() also screens the candidate population; point it at paths that hold nothing, so the
    # selftest never reads the live supervisor's plan or report directory.
    no_reports, no_plan = tmp / "no-reports", tmp / "no-plan.json"
    try:
        # ---- the flag is the kill switch, and it is OFF by default ----------------------
        assert flag_enabled({}) is False
        assert flag_enabled({BOOTSTRAP_FLAG: "1"}) is True

        report = {
            "target": "owner/repo#5",
            "agent": "cursor",
            "lane": "opener",
            "task_type": "implement",
            "pid": 424242,
            "state": "stalled",
            "recommended_action": "inspect",
            "log": str(tmp / "a.log"),
            "worktree": str(tmp),
            "expected_paths": ["src"],
        }
        proposal = {
            "action": "redirect",
            "reason": "stale auth token",
            "confidence": "high",
            "corrected_prompt": "Retry with fresh auth, keep scope, validate, push, open PR.",
            "switch_agent": "codex",
        }

        def role_runner(rep, ac, **kwargs):
            import roles

            return roles.run_redirect_agent(rep, ac, proposal_json=proposal, **kwargs)

        # ---- flag OFF: authorised, reported, and NOTHING is applied --------------------
        # The runner EXPLODES rather than returning a value: if a future edit lets the flag-off or
        # live-pid path reach apply_plan, the failure names the invariant instead of surfacing as
        # an incidental AttributeError somewhere downstream.
        def must_not_run(*args, **kwargs):
            raise AssertionError("apply_plan was reached when it must not be")

        ran: list = []
        dry = apply_one(
            report=report,
            acceptance_criteria="All endpoints return 200.",
            backend="codex",
            corpus_path=corpus,
            env={},
            role_runner=role_runner,
            pid_checker=lambda pid: False,
            apply_runner=must_not_run,
        )
        assert dry["authorization"]["allowed"] is True, dry["authorization"]
        assert dry["authorization"]["would_mutate"] is False, dry["authorization"]
        assert dry["apply_result"] is None and not ran, dry
        # The stamp is what makes the advice measurable; authorisation requires it.
        assert dry["role_run_id"] and dry["role_run_id"].startswith(ROLE_RUN_PREFIX), dry

        # ---- a LIVE process is never killed -------------------------------------------
        live = apply_one(
            report=report,
            acceptance_criteria="AC",
            backend="codex",
            corpus_path=corpus,
            env={BOOTSTRAP_FLAG: "1"},
            role_runner=role_runner,
            pid_checker=lambda pid: True,
            apply_runner=must_not_run,
        )
        assert live["authorization"]["allowed"] is False, live["authorization"]
        assert any("still alive" in b for b in live["authorization"]["blocks"]), live
        assert not ran, "a live lane must never be applied"

        # ---- an unstamped plan is refused ---------------------------------------------
        unstamped = authorize(
            plan_obj={
                "action": "redirect",
                "target": "o/r#1",
                "prompt_text": "x",
                "prompt_file": "f",
                "steps": [{"id": "delegate-retry", "commands": [["python3", "d.py"]]}],
            },
            role_run_id="role:redirect:codex:1",
            decision_source="redirect_agent",
            errors=[],
            pid_alive=False,
            claim_holder=None,
            prior_agent="cursor",
            gate={"bootstrap_needed": True, "disagreements_needed": 3},
            applied_targets=set(),
            applies_today=0,
            flag_on=True,
        )
        assert unstamped["allowed"] is False
        assert any("lineage stamp" in b for b in unstamped["blocks"]), unstamped

        # ---- another agent's live claim is not stolen ----------------------------------
        stolen = authorize(
            plan_obj={
                "action": "redirect",
                "target": "o/r#1",
                "prompt_text": "x",
                "prompt_file": "f",
                "accepted_role_run_id": "role:redirect:codex:1",
                "steps": [{"id": "delegate-retry", "commands": [["python3", "d.py"]]}],
            },
            role_run_id="role:redirect:codex:1",
            decision_source="redirect_agent",
            errors=[],
            pid_alive=False,
            claim_holder={"agent": "gemini"},
            prior_agent="cursor",
            gate={"bootstrap_needed": True, "disagreements_needed": 3},
            applied_targets=set(),
            applies_today=0,
            flag_on=True,
        )
        assert stolen["allowed"] is False and any("claimed by" in b for b in stolen["blocks"])

        # ---- SELF-LIMITING: a satisfied gate refuses further applies -------------------
        satisfied = authorize(
            plan_obj={
                "action": "redirect",
                "target": "o/r#1",
                "prompt_text": "x",
                "prompt_file": "f",
                "accepted_role_run_id": "role:redirect:codex:1",
                "steps": [{"id": "delegate-retry", "commands": [["python3", "d.py"]]}],
            },
            role_run_id="role:redirect:codex:1",
            decision_source="redirect_agent",
            errors=[],
            pid_alive=False,
            claim_holder=None,
            prior_agent="cursor",
            gate={"bootstrap_needed": False, "disagreements_needed": 0},
            applied_targets=set(),
            applies_today=0,
            flag_on=True,
        )
        assert satisfied["allowed"] is False
        assert any("deficits are closed" in b for b in satisfied["blocks"]), satisfied

        # ---- flag ON + dead lane: applies, and only then -------------------------------
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_runner(command, capture_output=True, text=True, check=False):
            calls.append(command)
            return FakeProc()

        hot = apply_one(
            report={**report, "target": "owner/repo#6"},
            acceptance_criteria="AC",
            backend="codex",
            corpus_path=corpus,
            env={BOOTSTRAP_FLAG: "1"},
            role_runner=role_runner,
            pid_checker=lambda pid: False,
            apply_runner=fake_runner,
        )
        assert hot["authorization"]["would_mutate"] is True, hot["authorization"]
        assert hot["apply_result"] and hot["apply_result"]["applied"] is True, hot
        # No kill ran (pid dead), and the delegate carried the stamp downstream.
        assert not any(cmd[0] == "kill" for cmd in calls), calls
        delegate = [cmd for cmd in calls if "delegate" in cmd]
        assert delegate, calls
        flag_at = delegate[0].index("--influenced-by-role-run-id")
        assert delegate[0][flag_at + 1] == hot["role_run_id"], delegate[0]

        # ---- per-target and per-day bounds come from the corpus ------------------------
        targets_seen, today = _applied_history(corpus)
        assert targets_seen == {"owner/repo#6"}, targets_seen
        assert today == 1, today
        repeat = apply_one(
            report={**report, "target": "owner/repo#6"},
            acceptance_criteria="AC",
            backend="codex",
            corpus_path=corpus,
            env={BOOTSTRAP_FLAG: "1"},
            role_runner=role_runner,
            pid_checker=lambda pid: False,
            apply_runner=fake_runner,
        )
        assert repeat["authorization"]["allowed"] is False, repeat["authorization"]
        assert any(
            "already been applied" in b or "daily bound" in b
            for b in repeat["authorization"]["blocks"]
        ), repeat["authorization"]

        # ---- the linker: recorded edge + terminal outcome -> corpus link ---------------
        role_run_id = hot["role_run_id"]
        feedback.record_run(
            "work:applied-redirect",
            "owner/repo#6",
            "implement",
            "codex",
            influenced_by_role_run_ids=[role_run_id],
        )
        assert link_applied_outcomes(corpus_path=corpus)["linked"] == 0, "no outcome yet"
        feedback.record_outcome(
            "work:applied-redirect", adjudicated_verdict="PASS", merged=True, durability="durable"
        )
        pending_preview = preview_link_applied_outcomes(corpus_path=corpus)
        assert pending_preview["pending"] == 1, pending_preview
        pending_hash = _corpus_digest(corpus)
        st_pending = status(corpus, env={}, report_dir=no_reports, plan_path=no_plan)
        assert _corpus_digest(corpus) == pending_hash, "status must not write the corpus"
        assert st_pending["unlinked_applied_outcomes"] == 1, st_pending

        link_events_before = _outcome_link_event_count(corpus)
        first = link_applied_outcomes(corpus_path=corpus)
        assert first["linked"] == 1 and first["links"][0]["synced"] is True, first
        assert (
            _outcome_link_event_count(corpus) == link_events_before + 1
        ), "apply-path must append exactly one link"
        # Idempotent: a second pass must not double-count into the gate.
        assert link_applied_outcomes(corpus_path=corpus)["linked"] == 0, "linker re-linked"
        assert redirect_shadow.summarize(corpus)["synced_role_outcomes"] >= 1

        # ---- status stays read-only after links exist too ---------------------------
        status_hash_before = _corpus_digest(corpus)
        st_linker = status(corpus, env={}, report_dir=no_reports, plan_path=no_plan)
        assert _corpus_digest(corpus) == status_hash_before, "status must not write the corpus"
        assert st_linker["flag_source"] == "explicit", st_linker
        assert (
            st_linker["flag_on"] is False and st_linker["gate"]["synced_role_outcomes"] >= 1
        ), st_linker
        assert st_linker["unlinked_applied_outcomes"] == 0, st_linker

        # A rejected edge is NOT an applied redirect and must never be linked as one.
        feedback.record_role_run(
            "role:redirect:codex:rejected", "redirect", "owner/repo#9", "codex"
        )
        feedback.record_run("work:rejected-redirect", "owner/repo#9", "implement", "codex")
        feedback.record_influence_edge(
            target_run_id="work:rejected-redirect",
            influence_type="role",
            influence_id="role:redirect:codex:rejected",
            source_run_id="role:redirect:codex:rejected",
            accepted=False,
        )
        feedback.record_outcome(
            "work:rejected-redirect", adjudicated_verdict="PASS", merged=True, durability="durable"
        )
        assert (
            link_applied_outcomes(corpus_path=corpus)["linked"] == 0
        ), "a rejected role edge must never be linked as applied advice"

        # ---- the FREE screen must agree with authorize() on every lane-level block, and must
        # ---- never claim a pass that authorize() would refuse for a lane reason.
        open_gate = {"bootstrap_needed": True, "disagreements_needed": 3}
        clean = screen_report(
            {"target": "owner/repo#77", "agent": "cursor", "pid": 424243},
            gate=open_gate,
            applied_targets=set(),
            applies_today=0,
            pid_checker=lambda pid: False,
        )
        assert clean["passes_screen"] is True, clean
        alive = screen_report(
            {"target": "owner/repo#77", "agent": "cursor", "pid": 1},
            gate=open_gate,
            applied_targets=set(),
            applies_today=0,
            pid_checker=lambda pid: True,
        )
        assert alive["passes_screen"] is False
        assert any("still alive" in b for b in alive["blocks"]), alive
        closed = screen_report(
            {"target": "owner/repo#77", "agent": "cursor", "pid": 424243},
            gate={"bootstrap_needed": False},
            applied_targets=set(),
            applies_today=0,
            pid_checker=lambda pid: False,
        )
        assert closed["passes_screen"] is False
        assert any("deficits are closed" in b for b in closed["blocks"]), closed
        bounded = screen_report(
            {"target": "owner/repo#77", "agent": "cursor", "pid": 424243},
            gate=open_gate,
            applied_targets=set(),
            applies_today=MAX_APPLIES_PER_DAY,
            pid_checker=lambda pid: False,
        )
        assert bounded["passes_screen"] is False
        assert any("daily bound" in b for b in bounded["blocks"]), bounded

        # ---- --dry-run must not be a cheap-looking door onto a per-candidate offload ------
        spent: list = []
        guarded = apply_candidates(
            report_dir=tmp,
            corpus_path=corpus,
            dry_run=True,
            env={},
            role_runner=_recording_role_runner(spent),
        )
        assert guarded.get("skipped") and not spent, guarded
        assert "spend-offloads" in guarded["skipped"], guarded

        _selftest_prescreen(tmp)

        # Reporting must not depend on the invoking shell: an explicit env is labelled as such,
        # and an ambient value wins over the tick's default (that IS what would run).
        armed, src = flag_as_the_tick_sees_it()
        assert src in {
            "ambient",
            "tick",
            "unset",
            "unresolved (tick env unavailable; ambient unset)",
        }, src
        os.environ[BOOTSTRAP_FLAG] = "1"
        try:
            assert flag_as_the_tick_sees_it() == (True, "ambient")
        finally:
            os.environ.pop(BOOTSTRAP_FLAG, None)

        print(
            "redirect_apply.py selftest: OK (flag-off dry run, live-pid refusal, unstamped "
            "refusal, claim-theft refusal, self-limiting gate, flag-on apply carries the "
            "lineage stamp, per-target/per-day bounds, idempotent linker, rejected-edge "
            "refusal, free screen agrees with authorize, --dry-run cannot spend silently, "
            "status read-only corpus hash, linker appends exactly one event, pid-less live lane "
            "refused, unknown liveness refused, stale reports never judged, identical input "
            "judged once, per-run offload cap, drained and unknown renderings, "
            "link_preview=False opens no Brain)"
        )
    finally:
        feedback.DB_PATH = old_db
        redirect_shadow.CORPUS_PATH = old_corpus
        redirect_plan.PROMPT_DIR = old_prompt_dir
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument(
        "--status", action="store_true", help="gate deficits beside the drainable count"
    )
    ap.add_argument(
        "--link-outcomes",
        action="store_true",
        help="append redirect_outcome_link corpus events for applied redirects",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help=f"screen, then authorise and apply current candidates (needs {BOOTSTRAP_FLAG}=1)",
    )
    ap.add_argument("--dry-run", action="store_true", help="authorise only; never mutate")
    ap.add_argument(
        "--screen",
        action="store_true",
        help="free pre-screen: the authorisation subset that needs no role run",
    )
    ap.add_argument(
        "--spend-offloads",
        action="store_true",
        help="with --dry-run, really run the role on each candidate that passes the screen "
        "(one offload each, at most --max-offloads)",
    )
    ap.add_argument("--limit", type=int, default=MAX_APPLIES_PER_DAY)
    ap.add_argument(
        "--max-offloads",
        type=int,
        default=MAX_OFFLOADS_PER_RUN,
        help="role runs one --apply may spend",
    )
    ap.add_argument(
        "--report-dir",
        help="the supervisor's report directory; its files are COUNTED, never judged",
    )
    ap.add_argument(
        "--stage2-plan",
        help="the supervisor's stage-2 plan, the candidate population "
        "(default: $ORCH_STATE_DIR/keepalive-supervisor-stage2-plan.json)",
    )
    ap.add_argument("--acceptance-criteria", default="")
    ap.add_argument("--backend", default="")
    ap.add_argument("--corpus")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    corpus = Path(args.corpus) if args.corpus else None
    report_dir = Path(args.report_dir) if args.report_dir else None
    plan_path = Path(args.stage2_plan) if args.stage2_plan else None

    if args.screen:
        out = screen_candidates(report_dir=report_dir, plan_path=plan_path, corpus_path=corpus)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(
                "screen: "
                + format_drainable(
                    out["passing_screen"],
                    current=out["current_candidates"],
                    on_disk=out["reports_seen"],
                    stale=out["stale_reports"],
                    population=out["population"],
                )
                + " (no offload spent)"
            )
            for row in out["candidates"]:
                if not row["passes_screen"]:
                    print(f"  {row['target']}: {'; '.join(row['blocks'])}")
            print(f"  {out['note']}")
        return 0

    if args.link_outcomes:
        out = link_applied_outcomes(dry_run=args.dry_run, corpus_path=corpus)
        print(
            json.dumps(out, indent=2)
            if args.json
            else f"linked {out['linked']} of {out['pending']} pending applied-redirect outcomes"
        )
        return 0

    if args.status or not args.apply:
        out = status(corpus, report_dir=report_dir, plan_path=plan_path)
        print(json.dumps(out, indent=2) if args.json else "\n".join(format_status(out)))
        return 0

    # --apply: candidates are the targets of the SUPERVISOR'S LATEST RUN, read from the plan its
    # own cadence step wrote. Deliberately not a second discovery path — re-running live_targets()
    # would spend gh search budget to rediscover what is already on disk, and two discovery paths
    # drift.
    out = apply_candidates(
        report_dir=report_dir,
        plan_path=plan_path,
        limit=args.limit,
        max_offloads=args.max_offloads,
        corpus_path=corpus,
        acceptance_criteria=args.acceptance_criteria,
        backend=args.backend or None,
        dry_run=args.dry_run,
        spend_offloads=args.spend_offloads,
    )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print("\n".join(format_apply(out, flag_on=flag_enabled())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
