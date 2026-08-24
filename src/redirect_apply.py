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

  1. `link_applied_outcomes()` — ALWAYS ON, mutates nothing. For every redirect role run that has
     an accepted influence edge to a dispatch that has since reached a terminal outcome, append the
     `redirect_outcome_link` corpus event. `accepted=True` is truthful here and un-gameable: the
     edge exists only because the plan's own `--influenced-by-role-run-id` stamp rode a dispatch
     that really ran. This is the step that makes `synced_role_outcomes` climb with no human.
  2. `apply_candidates()` / `apply_one()` — the apply path, DEFAULT OFF behind
     `ORCH_REDIRECT_APPLY_BOOTSTRAP`. Candidates are the redirect reports `keepalive_supervisor`
     already writes on its own cadence step — not a second discovery path, and no extra gh
     traffic. With the flag off the cadence spends NOTHING: authorising means running RedirectAgent,
     which costs a backend offload, and there is no point buying a proposal that cannot be applied
     (`redirect_sweep` already records proposals for these targets anyway). `apply_plan`'s contract
     is held by this module's selftest with injected runners. `--dry-run` forces authorisation
     anyway for an operator who wants to see the predicate against live reports.

AUTHORISATION IS OBJECTIVE, NOT REVIEWED. `authorize()` is a pure function of recorded state. Its
load-bearing condition is `pid_alive is False`: on an already-dead lane the `stop-process` step is a
no-op, so applying reduces to release-claim + delegate — exactly what the closer/opener rails do to
a dead stalled lane every hour anyway. The only difference is WHICH prompt and WHICH agent the retry
uses, which is the role's entire contribution and the thing that needs measuring. It never kills a
live process and never steals another agent's live claim.

SELF-LIMITING BY CONSTRUCTION. The bootstrap stops as soon as the gate's own deficits reach zero —
there is no date to expire unnoticed (FM3) and no latch whose clear path it blocks (FM2). Bounds:
one target at most once, and `MAX_APPLIES_PER_DAY` per day, both derived from the append-only
corpus rather than a side file.

    python3 redirect_apply.py --status            # gate deficits + what the bootstrap would do
    python3 redirect_apply.py --link-outcomes     # append links for applied redirects (no mutation)
    python3 redirect_apply.py                     # dry-run authorisation over live candidates
    ORCH_REDIRECT_APPLY_BOOTSTRAP=1 python3 redirect_apply.py --apply    # the only mutating form
    python3 redirect_apply.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

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
# 1. The always-on linker: recorded state -> corpus link. No mutation anywhere.
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


def link_applied_outcomes(
    *, dry_run: bool = False, corpus_path: Path | None = None, conn=None
) -> dict[str, Any]:
    """Append a `redirect_outcome_link` event per applied redirect that has an outcome."""
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


def authorize(
    *,
    plan_obj: dict,
    role_run_id: str | None,
    decision_source: str | None,
    errors: list | None,
    pid_alive: bool,
    claim_holder: dict | None,
    prior_agent: str | None,
    gate: dict,
    applied_targets: set[str],
    applies_today: int,
    flag_on: bool,
) -> dict[str, Any]:
    """Decide apply/refuse from recorded state alone. No human, no review queue.

    Every block below is a fact about the lane or the corpus, never a judgement about the
    proposal's content — the proposal's quality is exactly what the resulting outcome measures.
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
    if pid_alive:
        blocks.append("prior process is still alive — apply never kills a live lane")
    if claim_holder:
        held_by = str(claim_holder.get("agent") or "")
        if prior_agent and held_by and held_by != prior_agent:
            blocks.append(f"target is claimed by {held_by!r}, not the stalled agent")
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
    if not gate.get("bootstrap_needed"):
        blocks.append("gate deficits are closed — bootstrap has finished its job")
    if target and target in applied_targets:
        blocks.append("this target has already been applied once")
    if applies_today >= MAX_APPLIES_PER_DAY:
        blocks.append(f"daily bound reached ({applies_today}/{MAX_APPLIES_PER_DAY})")
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

    pid = report.get("pid")
    applied_targets, applies_today = _applied_history(corpus, now=now)
    authorization = authorize(
        plan_obj=plan_obj,
        role_run_id=role_run_id,
        decision_source=result.get("decision_source"),
        errors=result.get("errors"),
        pid_alive=bool(pid is not None and pid_alive_fn(int(pid))),
        claim_holder=claims.holder(str(report.get("target") or "")),
        prior_agent=report.get("agent"),
        gate=gate,
        applied_targets=applied_targets,
        applies_today=applies_today,
        flag_on=flag_enabled(env),
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


def screen_report(
    report: dict,
    *,
    gate: dict,
    applied_targets: set[str],
    applies_today: int,
    pid_checker=None,
) -> dict[str, Any]:
    """The subset of authorisation that needs NO role run — so asking costs nothing.

    Written after asking "would anything be authorised right now?" the expensive way and spending
    17 live gemini offloads to hear "no". Everything here is a fact about the lane or the corpus;
    only the plan-shaped blocks (action, mutating step, prompt, lineage stamp) need the role.
    """
    pid_alive_fn = pid_checker or redirect_plan._pid_alive
    target = str(report.get("target") or "")
    pid = report.get("pid")
    blocks: list[str] = []
    if pid is not None and pid_alive_fn(int(pid)):
        blocks.append("prior process is still alive — apply never kills a live lane")
    holder = claims.holder(target) if target else None
    if holder:
        held_by = str(holder.get("agent") or "")
        prior = report.get("agent")
        if prior and held_by and held_by != prior:
            blocks.append(f"target is claimed by {held_by!r}, not the stalled agent")
    if not gate.get("bootstrap_needed"):
        blocks.append("gate deficits are closed — bootstrap has finished its job")
    if target and target in applied_targets:
        blocks.append("this target has already been applied once")
    if applies_today >= MAX_APPLIES_PER_DAY:
        blocks.append(f"daily bound reached ({applies_today}/{MAX_APPLIES_PER_DAY})")
    return {"target": target, "passes_screen": not blocks, "blocks": blocks}


def screen_candidates(
    *, report_dir: Path | None = None, corpus_path: Path | None = None, pid_checker=None
) -> dict[str, Any]:
    """How many live candidates could possibly be authorised, without running a single role."""
    import keepalive_supervisor

    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    directory = report_dir or keepalive_supervisor.DEFAULT_STAGE2_REPORT_DIR
    gate = gate_state(corpus)
    applied_targets, applies_today = _applied_history(corpus)
    rows = []
    for path in (
        sorted(Path(directory).glob("*.keepalive-supervisor-report.json"))
        if Path(directory).is_dir()
        else []
    ):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows.append(
            screen_report(
                report,
                gate=gate,
                applied_targets=applied_targets,
                applies_today=applies_today,
                pid_checker=pid_checker,
            )
        )
    return {
        "reports_seen": len(rows),
        "gate": gate,
        "passing_screen": len([r for r in rows if r["passes_screen"]]),
        "report_dir": str(directory),
        "candidates": rows,
        "note": "a candidate passing the screen still needs the role to propose "
        "redirect/decompose; the screen costs no offload",
    }


def apply_candidates(
    *,
    report_dir: Path | None = None,
    limit: int = MAX_APPLIES_PER_DAY,
    corpus_path: Path | None = None,
    acceptance_criteria: str = "",
    backend: str | None = None,
    dry_run: bool = False,
    spend_offloads: bool = False,
    env: dict | None = None,
    role_runner=None,
    apply_runner=None,
    pid_checker=None,
) -> dict[str, Any]:
    """Walk the supervisor's already-written redirect reports and authorise each in turn.

    Stops at the first authorised apply (limit), because the daily bound is 1 and a bootstrap that
    fires N times on one tick is not a bootstrap.
    """
    import keepalive_supervisor

    # COST HONESTY. apply_one() runs RedirectAgent, which spends a real backend offload. With the
    # flag off there is nothing we could do with the proposal, and redirect_sweep already records
    # proposals for these same targets on its own cadence step — so authorising on a disarmed
    # bootstrap would double-spend capacity to learn nothing. apply_plan's contract is exercised by
    # the selftest with injected runners instead. An operator can still force it with --dry-run.
    if not flag_enabled(env) and not (dry_run and spend_offloads):
        return {
            "reports_seen": 0,
            "considered": 0,
            "authorized": 0,
            "applied": 0,
            "skipped": f"{BOOTSTRAP_FLAG} is off; authorising would spend one backend "
            f"offload per candidate — pass --dry-run --spend-offloads to force it, "
            f"or --screen for the free subset",
            "report_dir": str(report_dir or keepalive_supervisor.DEFAULT_STAGE2_REPORT_DIR),
            "results": [],
        }

    directory = report_dir or keepalive_supervisor.DEFAULT_STAGE2_REPORT_DIR
    reports = (
        sorted(Path(directory).glob("*.keepalive-supervisor-report.json"))
        if Path(directory).is_dir()
        else []
    )
    results: list[dict] = []
    authorized = applied = considered = 0
    for path in reports:
        if applied >= max(int(limit), 0):
            break
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "target": path.name,
                    "authorized": False,
                    "applied": False,
                    "blocks": [f"unreadable report: {str(exc)[:80]}"],
                }
            )
            continue
        considered += 1
        outcome = apply_one(
            report=report,
            acceptance_criteria=acceptance_criteria or str(report.get("acceptance_criteria") or ""),
            backend=backend,
            corpus_path=corpus_path,
            env={} if dry_run else env,
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
                "blocks": auth["blocks"],
                "role_run_id": outcome["role_run_id"],
            }
        )
    return {
        "reports_seen": len(reports),
        "considered": considered,
        "authorized": authorized,
        "applied": applied,
        "report_dir": str(directory),
        "results": results,
    }


def status(corpus_path: Path | None = None, *, env: dict | None = None) -> dict[str, Any]:
    """Read-only: where the gate stands, whether the bootstrap is armed, what is unlinked."""
    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    gate = gate_state(corpus)
    applied_targets, applies_today = _applied_history(corpus)
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
        "applied_targets": sorted(applied_targets),
        "applies_today": applies_today,
        "daily_bound": MAX_APPLIES_PER_DAY,
        "unlinked_applied_outcomes": len(
            [
                r
                for r in pending_outcome_links()
                if (str(r["role_run_id"]), str(r["influenced_run_id"]))
                not in redirect_shadow.linked_pairs(corpus)
            ]
        ),
    }


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


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="redirect-apply-"))
    old_db, old_corpus = feedback.DB_PATH, redirect_shadow.CORPUS_PATH
    old_prompt_dir = redirect_plan.PROMPT_DIR
    feedback.DB_PATH = tmp / "brain.db"
    corpus = tmp / "corpus.jsonl"
    redirect_plan.PROMPT_DIR = tmp / "prompts"
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
        first = link_applied_outcomes(corpus_path=corpus)
        assert first["linked"] == 1 and first["links"][0]["synced"] is True, first
        # Idempotent: a second pass must not double-count into the gate.
        assert link_applied_outcomes(corpus_path=corpus)["linked"] == 0, "linker re-linked"
        assert redirect_shadow.summarize(corpus)["synced_role_outcomes"] >= 1

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

        st = status(corpus, env={})
        assert st["flag_source"] == "explicit", st
        assert st["flag_on"] is False and st["gate"]["synced_role_outcomes"] >= 1, st

        print(
            "redirect_apply.py selftest: OK (flag-off dry run, live-pid refusal, unstamped "
            "refusal, claim-theft refusal, self-limiting gate, flag-on apply carries the "
            "lineage stamp, per-target/per-day bounds, idempotent linker, rejected-edge "
            "refusal, free screen agrees with authorize, --dry-run cannot spend silently)"
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
    ap.add_argument("--status", action="store_true", help="gate deficits and bootstrap state")
    ap.add_argument(
        "--link-outcomes",
        action="store_true",
        help="append outcome links for applied redirects (no mutation)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help=f"authorise and apply live candidates (needs {BOOTSTRAP_FLAG}=1)",
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
        help="with --dry-run, really run the role per candidate (costs one offload each)",
    )
    ap.add_argument("--limit", type=int, default=MAX_APPLIES_PER_DAY)
    ap.add_argument(
        "--report-dir", help="directory of keepalive-supervisor redirect reports (default: its own)"
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

    if args.screen:
        out = screen_candidates(
            report_dir=Path(args.report_dir) if args.report_dir else None, corpus_path=corpus
        )
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(
                f"screen: {out['passing_screen']} of {out['reports_seen']} live candidates could "
                f"be authorised (no offload spent)"
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
        out = status(corpus)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            gate = out["gate"]
            print(
                f"{CAPABILITY_ID}: flag {BOOTSTRAP_FLAG}="
                f"{'1 (ARMED)' if out['flag_on'] else '0 (off)'} "
                f"[as the tick sees it; source: {out['flag_source']}]"
            )
            print(
                f"  gate: synced_role_outcomes={gate['synced_role_outcomes']} "
                f"(need {gate['synced_needed']} more), "
                f"linked_disagreements={gate['linked_disagreements']} "
                f"(need {gate['disagreements_needed']} more)"
            )
            print(
                f"  ready_for_supervised_apply={gate['ready_for_supervised_apply']} "
                f"bootstrap_needed={gate['bootstrap_needed']}"
            )
            print(
                f"  applied today {out['applies_today']}/{out['daily_bound']}; "
                f"unlinked applied outcomes: {out['unlinked_applied_outcomes']}"
            )
        return 0

    # --apply: candidates are the redirect reports the SUPERVISOR ALREADY WROTE on its own
    # cadence step. Deliberately not a second discovery path — re-running live_targets() would
    # spend gh search budget to rediscover what is already on disk, and two discovery paths drift.
    out = apply_candidates(
        report_dir=Path(args.report_dir) if args.report_dir else None,
        limit=args.limit,
        corpus_path=corpus,
        acceptance_criteria=args.acceptance_criteria,
        backend=args.backend or None,
        dry_run=args.dry_run,
        spend_offloads=args.spend_offloads,
    )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(
            f"reports={out['reports_seen']} considered={out['considered']} "
            f"authorized={out['authorized']} applied={out['applied']} "
            f"(flag {BOOTSTRAP_FLAG}={'1' if flag_enabled() else '0'})"
        )
        for row in out["results"]:
            verdict = (
                "APPLIED"
                if row.get("applied")
                else "authorized (flag off)" if row.get("authorized") else "refused"
            )
            print(f"  {row['target']}: {verdict}")
            for block in row.get("blocks") or []:
                print(f"      - {block}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
