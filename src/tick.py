#!/usr/bin/env python3
"""tick.py — the autonomous REMOTE orchestration tick (the cron loop's brain; OFF until the owner activates).

For each actionable opener/closer item: CHOOSE a keepalive agent (router.select_remote_agent — reserve-
aware, so routine work avoids Claude's scarce weekly cap) -> APPLY its `agent:<X>` label
(dispatcher.delegate_remote) to drive the GitHub keepalive on REMOTE capacity -> then INGEST keepalive PR
outcomes (outcomes.ingest_outcomes) so the feedback loop gets LIVE data. This is the "orchestrator mostly
drives the remote system" model; local CLI delegation (dispatcher.delegate) handles the minority of
bounded local coding.

DEFAULT is dry-run (SHADOW): prints what it WOULD delegate, applies NO labels, writes nothing. `--active`
really applies labels + ingests + writes the heartbeat (legacy lanes yield). orchestrate.sh wires this;
**cron stays OFF until the owner schedules it.** `--selftest` is fully offline.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import adversarial
import capabilities
import claims
import dispatcher
import exp_abcd
import feedback
import outcomes
import provision
import research_scheduler
import research_subjects
import roles
import router
import runtime_ac_gate

RESEARCH_MAX_PER_TICK = 1


def _adversarial_context(item: dict, reason: str) -> str:
    labels = ", ".join(item.get("labels") or []) or "(none)"
    title = item.get("title") or ""
    return (
        f"High-stakes closer PR {item.get('target')}: {title}. Reason: {reason}. Labels: {labels}."
    )


def _adversarial_review_status(
    item: dict,
    *,
    dry_run: bool,
    env: Mapping[str, str] | None = None,
    provision_fn=None,
    review_fn=None,
) -> dict | None:
    reason = adversarial.high_stakes_reason(item)
    if not reason:
        return None
    target = item.get("target")
    base = {"target": target, "reason": reason}
    if dry_run:
        return {**base, "status": "planned"}
    env = env or {}
    if not adversarial.review_enabled(env):
        return {
            **base,
            "status": "required_but_not_run",
            "detail": "set ORCH_RUN_ADVERSARIAL_REVIEW=1 to run the advisory panel",
        }
    reviewers = adversarial.reviewers_from_env(env)
    try:
        if provision_fn is None:
            import provision

            provision_fn = provision.provision
        if review_fn is None:
            review_fn = adversarial.review
        worktree = provision_fn(str(target), "closer")
        result = review_fn(str(worktree), reviewers, _adversarial_context(item, reason))
        lineage = None
        lineage_run_id = item.get("run_id") or feedback.latest_run_id_for_target(str(target))
        if lineage_run_id:
            try:
                result_hash = feedback._completion_hash(result)
                lineage = feedback.record_completion_event(
                    lineage_run_id,
                    event_type="panel",
                    phase="verification",
                    producer="adversarial",
                    status=result.get("verdict"),
                    payload={
                        "panel_ids": [f"adversarial:{reviewer}" for reviewer in reviewers],
                        "adjudication_id": feedback._completion_hash(
                            {"target": target, "reviewers": reviewers, "reason": reason}
                        ),
                        "result_hashes": [result_hash],
                        "verification": {
                            "adjudicated_verdict": result.get("verdict"),
                            "verifier_ids": reviewers,
                            "result_hashes": {"panel": result_hash},
                        },
                    },
                )
            except Exception as exc:
                lineage = {"error_hash": feedback._completion_hash(str(exc))}
        return {
            **base,
            "status": "executed",
            "reviewers": reviewers,
            "worktree": str(worktree),
            "result": result,
            "lineage": lineage,
        }
    except Exception as exc:
        return {**base, "status": "failed", "error": str(exc)}


def _target_repo(target: str) -> str | None:
    if not target or "#" not in target:
        return None
    return target.split("#", 1)[0]


def _target_number(target: str) -> str | None:
    if not target or "#" not in target:
        return None
    return target.rsplit("#", 1)[1]


def _fetch_issue_body(target: str, *, issue_body_fn=None) -> str | None:
    if issue_body_fn:
        return issue_body_fn(target)
    repo, number = _target_repo(target), _target_number(target)
    if not repo or not number:
        return None
    import subprocess

    try:
        out = subprocess.run(
            ["gh", "issue", "view", number, "-R", repo, "--json", "title,body"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
    except Exception:
        return None
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    text = f"# {title}\n\n{body}".strip()
    return text or None


def _public_research_plan(plan: dict) -> dict:
    """Drop bulky backlog bodies from tick JSON while preserving planner diagnostics."""

    def clean_job(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "item"}

    return {
        **plan,
        "candidates": [clean_job(j) for j in plan.get("candidates", [])],
        "planned": [clean_job(j) for j in plan.get("planned", [])],
    }


def _research_claim_metadata(prepare_result: dict) -> dict:
    """Extract watchable claim metadata from exp_abcd.prepare output."""
    pids: list[int] = []
    logs: list[str] = []
    worktrees: list[str] = []
    for row in prepare_result.get("launched") or []:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            pids.append(pid)
        if row.get("log"):
            logs.append(str(row["log"]))
        if row.get("worktree"):
            worktrees.append(str(row["worktree"]))
    meta = {
        "pids": pids,
        "logs": logs,
        "worktrees": worktrees,
    }
    if prepare_result.get("exp_id"):
        meta["exp_id"] = str(prepare_result["exp_id"])
        meta["experiment_dir"] = str(exp_abcd.exp_paths(str(prepare_result["exp_id"])))
    return meta


# Canonical definition lives in exp_abcd, next to the arm/member normaliser it feeds --
# two launchers consume it and a second copy would let one drift back to the legacy shape.
research_v2_arms = exp_abcd.research_v2_arms


def research_tick(
    items: list[dict],
    cap: dict,
    *,
    learned: dict | None = None,
    dry_run: bool = True,
    env: Mapping[str, str] | None = None,
    max_experiments: int = RESEARCH_MAX_PER_TICK,
    conn=None,
    prepare_fn=None,
    issue_body_fn=None,
    rng=None,
    hyps: list | None = None,
    excluded_targets: set[str] | None = None,
    production_reserve: dict[str, int] | None = None,
    unevaluated_cap: int = research_subjects.DEFAULT_UNEVALUATED_CAP,
    per_subject_cap: int = research_subjects.DEFAULT_PER_SUBJECT_CAP,
) -> dict:
    """Capacity-gated opportunistic research. Shadow by default; active launch needs ORCH_RESEARCH_ARM=1."""
    env = os.environ if env is None else env
    if not dry_run:
        claims.reap_stale()
    rng = rng or __import__("random").Random()
    active_claims = set(claims.active_claims().keys())
    plan_out = research_scheduler.build_research_plan(
        items,
        cap,
        learned=learned,
        hyps=hyps,
        conn=conn,
        claimed_targets=active_claims,
        excluded_targets=excluded_targets,
        production_reserve=production_reserve,
        unevaluated_cap=unevaluated_cap,
        per_subject_cap=per_subject_cap,
        max_jobs=max_experiments,
        rng=rng,
    )
    if not dry_run:
        for row in plan_out.get("skipped") or []:
            if row.get("reason"):
                research_subjects.record_event(
                    "rejected",
                    target=row.get("target"),
                    task_type=row.get("task_type"),
                    reason=row.get("reason"),
                    metadata={
                        key: row.get(key)
                        for key in (
                            "subject_id",
                            "subject_family_id",
                            "unevaluated_backlog",
                            "unevaluated_cap",
                            "existing_exp_id",
                        )
                        if row.get(key) is not None
                    },
                    conn=conn,
                )
    if not plan_out.get("planned"):
        return {**_public_research_plan(plan_out), "planned": [], "active": False}

    plans, launched = [], []
    for job in plan_out.get("planned", []):
        item = job["item"]
        task_type = job.get("task_type", item.get("task_type", "implement"))
        arms = list(job.get("arms") or [])
        target = str(item.get("target"))
        plan = {k: v for k, v in job.items() if k != "item"}
        plan.update(
            {
                "status": "planned",
                "target": target,
                "task_type": task_type,
                "rationale": "shadow plan; set ORCH_RESEARCH_ARM=1 with --active to launch",
            }
        )
        plans.append(plan)

        if dry_run or env.get("ORCH_RESEARCH_ARM") != "1":
            continue
        subject_identity = None
        try:
            spec = _fetch_issue_body(target, issue_body_fn=issue_body_fn)
            if not spec:
                plan["active_status"] = "no_issue_body"
                research_subjects.record_event(
                    "rejected",
                    target=target,
                    task_type=task_type,
                    reason="no_issue_body",
                    conn=conn,
                )
                continue
            subject_identity = research_subjects.subject_identity(
                target,
                task_type,
                spec,
                item.get("base_sha"),
                arms,
                item.get("profiles"),
            )
            admission = research_subjects.assess_candidate(
                target=target,
                task_type=task_type,
                spec=spec,
                base_sha=item.get("base_sha"),
                arms=arms,
                profiles=item.get("profiles"),
                conn=conn,
                unevaluated_cap=unevaluated_cap,
                per_subject_cap=per_subject_cap,
            )
            if not admission["eligible"]:
                plan["active_status"] = "blocked_by_subject_control"
                plan["blocked_reason"] = admission["reason"]
                research_subjects.record_event(
                    "rejected",
                    identity=subject_identity,
                    reason=admission["reason"],
                    metadata={
                        "unevaluated_backlog": admission.get("unevaluated_backlog"),
                        "unevaluated_cap": admission.get("unevaluated_cap"),
                    },
                    conn=conn,
                )
                continue
            capabilities.production_heartbeat(
                "research-scheduler",
                "match",
                ref=subject_identity["subject_id"],
                metadata={"target": target, "task_type": task_type},
            )
            if not claims.claim(target, "research"):
                plan["active_status"] = "blocked_by_claim"
                research_subjects.record_event(
                    "rejected",
                    identity=subject_identity,
                    reason="claim_race",
                    conn=conn,
                )
                continue
            repo = _target_repo(target)
            exp_id = f"tick-{int(time.time())}-{target.lower().replace('/', '-').replace('#', '-')}"
            spec_dir = Path(tempfile.mkdtemp(prefix="orch-research-spec-"))
            spec_file = spec_dir / "spec.md"
            spec_file.write_text(spec, encoding="utf-8")
            # prepare_arms, not prepare: the v2 manifest is what makes evaluations_v2 reachable.
            prepare = prepare_fn or exp_abcd.prepare_arms
            capabilities.production_heartbeat(
                "research-scheduler",
                "invocation",
                ref=subject_identity["subject_id"],
                metadata={"target": target, "task_type": task_type},
            )
            prepare_result = prepare(
                str(repo),
                str(spec_file),
                exp_id,
                research_v2_arms(arms, item.get("profiles")),
                task_type=task_type,
            )
            launched.append(prepare_result)
            plan["active_status"] = "launched"
            plan["exp_id"] = exp_id
            prepared_base_sha = prepare_result.get("base_sha")
            meta_path = exp_abcd.exp_paths(exp_id) / "meta.json"
            if not prepared_base_sha and meta_path.exists():
                try:
                    prepared_base_sha = json.loads(meta_path.read_text()).get("base_sha")
                except (OSError, json.JSONDecodeError):
                    prepared_base_sha = None
            subject_identity = research_subjects.subject_identity(
                target,
                task_type,
                spec,
                prepared_base_sha or item.get("base_sha"),
                arms,
                item.get("profiles"),
            )
            research_subjects.record_subject(
                subject_identity,
                lifecycle="active",
                exp_id=exp_id,
                reason="research_tick_launch",
                conn=conn,
            )
            capabilities.production_heartbeat(
                "research-scheduler",
                "success",
                ref=exp_id,
                metadata={
                    "subject_id": subject_identity["subject_id"],
                    "target": target,
                    "task_type": task_type,
                },
            )
            plan["subject_id"] = subject_identity["subject_id"]
            plan["subject_family_id"] = subject_identity["subject_family_id"]
            plan["base_sha"] = subject_identity.get("base_sha")
            claim_meta = _research_claim_metadata(prepare_result)
            if claim_meta["pids"]:
                claims.update_metadata(
                    target,
                    "research",
                    refresh_ts=True,
                    lane=item.get("lane") or "opener",
                    task_type=task_type,
                    **claim_meta,
                )
                plan["claim_status"] = "watchable"
            else:
                claims.release(target, "research")
                plan["claim_status"] = "released_no_child_pids"
        except Exception as exc:
            plan["active_status"] = "failed"
            plan["error"] = str(exc)
            research_subjects.record_event(
                "rejected",
                identity=subject_identity,
                target=target,
                task_type=task_type,
                reason="launch_failed",
                metadata={"error": str(exc)[:500]},
                conn=conn,
            )
            claims.release(target, "research")
    public = _public_research_plan(plan_out)
    return {
        **{k: v for k, v in public.items() if k != "planned"},
        "status": "planned" if plans else "no_plan",
        "planned": plans,
        "active": bool(launched),
        "launched": launched,
    }


DISPATCH_LANE_ENV = "ORCH_DISPATCH_LANE"

# THE PER-TICK DELEGATION CAP, and the bound on how many items a tick examines to fill it. Both are
# resolved once, by `delegation_bounds`, and `remote_tick` writes both into the plan beside the
# counts they bound, so the loop and the TICK-PLAN headline read the same numbers.
DELEGATIONS_PER_TICK_ENV = "ORCH_MAX_REMOTE_PER_TICK"
DELEGATIONS_PER_TICK_DEFAULT = 3
# Every examined item costs a `gh api` label read plus the role-selector hooks (and, for a closer
# item, the runtime-AC gate and, when high-stakes, an adversarial panel). A refusal spends nothing
# remote, so it no longer counts against the cap, which leaves this bound as what keeps a backlog
# of owned items from turning into unbounded reads. Measured over the live period (2026-06-15 to
# 09-02): the backlog per tick was p90 24, max 42 items, and the items that were not owned were p90
# 3, p99 15, max 29. Examined first (`examination_order`), the unowned ones fit inside 4 x 3 = 12 in
# all but 25 of 1,857 ticks.
EXAMINED_PER_DELEGATION = 4


def delegation_bounds(
    env: Mapping[str, str], max_delegations: int | None = None
) -> tuple[int, int]:
    """(delegation cap, examination bound) for one tick: `ORCH_MAX_REMOTE_PER_TICK` (default 3) and
    `EXAMINED_PER_DELEGATION` times it. `max_delegations` overrides the variable (tests)."""
    cap_n = (
        max_delegations
        if max_delegations is not None
        else int(env.get(DELEGATIONS_PER_TICK_ENV, str(DELEGATIONS_PER_TICK_DEFAULT)))
    )
    return cap_n, EXAMINED_PER_DELEGATION * cap_n


def is_delegation(row: Mapping[str, Any]) -> bool:
    """THE ONE PREDICATE for "this plan row is a delegation": the dispatcher neither refused it
    (`skip`: paused, owned by an agent, or ownership unread) nor rejected it (`error`). In a shadow
    tick the label WOULD apply; in an active tick it was attempted and its run recorded, whether or
    not GitHub accepted the label.

    The per-tick cap counts exactly these rows, `production_reserve` reserves research capacity for
    exactly these, a rejected-role influence edge is written only for these, and the TICK-PLAN
    headline counts them as delegations. Until 2026-10-04 the cap counted every row that reached the
    dispatcher. Refusals spend nothing remote, yet they filled it: over the live period (2026-06-15
    to 09-02) 2,826 of its slots went to refusals and 25 to delegations, 546 ticks deferred an item
    while every slot held a refusal, and 65 targets that carried no agent label when first deferred
    were never examined at all. A row with no `labels_read` still counts: the cap bounds spend, and
    the safe error is to count a row that spent nothing, never to miss one that did."""
    return not row.get("skip") and not row.get("error")


def _discovery_refusal(item: Mapping[str, Any]) -> str | None:
    """What the dispatcher's own rail (`_remote_skip_reason`) says about the labels DISCOVERY saw on
    this item. None when it would not refuse them, or when the item carries no labels at all, which
    is unknown and so examined with the delegable items. It only ORDERS the examination: the live
    read inside `delegate_remote` still decides every delegation."""
    labels = item.get("labels")
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return None
    names = {str(lab.get("name", "")) if isinstance(lab, Mapping) else str(lab) for lab in labels}
    return dispatcher._remote_skip_reason(names, "")


def examination_order(items: list) -> list[tuple[dict, bool]]:
    """`(item, delegable)` pairs: items the rail would not refuse on their discovery labels first,
    the rest after them, each group in backlog order.

    The order is what keeps the examination bound from latching. Discovery lists a closer item only
    when its PR already carries an `agent:*` label (`backlog.build_backlog`), and it lists the closer
    items first. Examined in that order, they are refused one after another, and enough of them hold
    every item behind them out of every tick until keepalive finishes them. Examined last, they can
    hold back nothing that could be delegated."""
    delegable: list[tuple[dict, bool]] = []
    refused: list[tuple[dict, bool]] = []
    for item in items:
        if _discovery_refusal(item) is None:
            delegable.append((item, True))
        else:
            refused.append((item, False))
    return delegable + refused


def remote_tick(
    items: list,
    cap: dict,
    *,
    learned: dict | None = None,
    dry_run: bool = True,
    do_ingest: bool = True,
    ingest_dry_run: bool | None = None,
    max_delegations: int | None = None,
    env: Mapping[str, str] | None = None,
    runtime_ac_gate_fn=None,
    research_tick_fn=None,
) -> dict:
    """Choose + delegate each item to a keepalive agent (remote), then ingest outcomes. Applies no labels
    when dry_run; do_ingest=False skips the ingest pass (tests).

    Two bounds per tick, from `delegation_bounds`. The CAP (ORCH_MAX_REMOTE_PER_TICK, default 3)
    counts delegations, the rows `is_delegation` accepts, so a large backlog can't fan out unbounded
    autonomous spend. A refused row spends nothing remote and takes no slot. The EXAMINATION bound
    (EXAMINED_PER_DELEGATION x the cap) counts every item that enters the per-item pipeline, so a
    backlog of owned items can't turn into unbounded label reads and hooks. Items reach the pipeline
    in `examination_order`. Every item left once either bound is reached is DEFERRED to the next tick.

    The plan names each deferral's bound, and how many deferred items were delegable on their
    discovery labels (`deferral.delegable`, the blocking quantity) beside how many of those wait only
    on this tick's own delegations (`deferral.drainable`). Those drain without help: once a label
    applies, the target is owned, so the next tick's dispatcher refuses it and it takes no slot. A
    delegable item deferred by the examination bound waits behind examined items that did not
    delegate, so it is not drainable. A shadow tick applies nothing, so its plan repeats."""
    import os

    env = os.environ if env is None else env
    roles.reset_role_invocation_counts()
    if not dry_run:
        claims.reap_stale()
    cap_n, examine_n = delegation_bounds(env, max_delegations)
    chosen: list[Any] = []
    no_capacity: list[Any] = []
    deferred: list[Any] = []
    deferred_by_cap: list[Any] = []
    deferred_by_examine_cap: list[Any] = []
    deferred_delegable: list[Any] = []
    deferred_drainable: list[Any] = []
    delegations = refused = errors = examined = 0
    blocked: list[Any] = []
    adversarial_reviews: list[Any] = []
    runtime_ac_gates: list[Any] = []
    role_shadows: list[dict] = []
    triage_shadow = roles.activate_tick_triage(items, cap, env=env, dry_run=dry_run)
    role_shadows.append(
        {
            "role": "triage",
            "selector": triage_shadow.get("selector"),
            "role_run_id": (triage_shadow.get("result") or {}).get("role_run_id"),
        }
    )
    runtime_ac_gate_fn = runtime_ac_gate_fn or runtime_ac_gate.gate_status
    for item, delegable in examination_order(items):
        target = item.get("target")
        if delegations >= cap_n or examined >= examine_n:  # per-tick bounds: defer the rest
            by_cap = delegations >= cap_n
            deferred.append(target)
            (deferred_by_cap if by_cap else deferred_by_examine_cap).append(target)
            if delegable:
                deferred_delegable.append(target)
                # Held only by this tick's own delegations, which free their slots once applied. A
                # cap of 0 is reached with none, and nothing frees it but the operator.
                if by_cap and delegations > 0:
                    deferred_drainable.append(target)
            continue
        examined += 1
        tt = item.get("task_type", "implement")
        held = claims.holder(str(item.get("target")))
        if held is not None:  # None is the one answer that means free; an unknown holder holds
            blocked.append(
                {
                    "target": item.get("target"),
                    "task_type": tt,
                    "reason": f"claimed by {held.get('agent') or 'an unknown holder'}",
                }
            )
            continue
        gate_status = runtime_ac_gate_fn(item, dry_run=dry_run, env=env)
        if gate_status:
            runtime_ac_gates.append(gate_status)
            if gate_status.get("blocks"):
                adjudication = roles.activate_adjudicator_disagreement(
                    item, gate_status, None, cap, env=env, dry_run=dry_run
                )
                role_shadows.append(
                    {
                        "role": "adjudicator",
                        "target": item.get("target"),
                        "selector": adjudication.get("selector"),
                        "role_run_id": (adjudication.get("result") or {}).get("role_run_id"),
                    }
                )
                blocked.append(
                    {
                        "target": item.get("target"),
                        "task_type": tt,
                        "reason": f"runtime AC gate {gate_status.get('status')}",
                        "verdict": gate_status.get("verdict"),
                    }
                )
                continue
        task_learned = (learned or {}).get(tt) if learned else None
        pick = router.select_remote_agent(tt, cap, learned=task_learned)
        if not pick:
            no_capacity.append(
                {
                    "target": item.get("target"),
                    "task_type": tt,
                    "reason": "no keepalive-agent capacity (try local or wait)",
                }
            )
            continue
        review_status = _adversarial_review_status(item, dry_run=dry_run, env=env)
        if review_status:
            adversarial_reviews.append(review_status)
        adjudication = roles.activate_adjudicator_disagreement(
            item, gate_status, review_status, cap, env=env, dry_run=dry_run
        )
        role_shadows.append(
            {
                "role": "adjudicator",
                "target": item.get("target"),
                "selector": adjudication.get("selector"),
                "role_run_id": (adjudication.get("result") or {}).get("role_run_id"),
            }
        )
        recommendation = (triage_shadow.get("recommendations") or {}).get(item.get("target")) or {}
        triage_role_id = (triage_shadow.get("result") or {}).get("role_run_id")
        triage_agrees = recommendation.get("action") in {"work_now", "monitor"}
        accepted_role_ids = [triage_role_id] if triage_role_id and triage_agrees else []
        delegate_kwargs = {"task_type": tt, "dry_run": dry_run}
        if accepted_role_ids:
            delegate_kwargs["influenced_by_role_run_ids"] = accepted_role_ids
        res = dispatcher.delegate_remote(pick["agent"], item["target"], **delegate_kwargs)
        row = {
            "target": item.get("target"),
            "task_type": tt,
            "agent": pick["agent"],
            "applied": res.get("applied"),
            "skip": res.get("skip"),
            "labels_read": res.get("labels_read"),
            "error": res.get("error"),
            "dry_run": dry_run,
        }
        # A disagreement edge needs a run to point at, and only a delegation records one. Written for
        # a refusal, it pointed at a run id nothing recorded, and a later delegation of the same
        # target to the same agent inherited it: of the 210 rejected `remote:` role edges in the
        # Brain on 2026-10-04, 2 were written in the tick that recorded their run.
        if not dry_run and is_delegation(row):
            repo, num = provision.parse_target(item["target"])
            downstream_run_id = f"remote:{repo}#{num}:{pick['agent']}"
            rejected_ids = []
            if triage_role_id and not triage_agrees:
                rejected_ids.append(triage_role_id)
            adjudicator_role_id = (adjudication.get("result") or {}).get("role_run_id")
            if adjudicator_role_id:
                rejected_ids.append(adjudicator_role_id)
            for role_run_id in rejected_ids:
                feedback.record_influence_edge(
                    target_run_id=downstream_run_id,
                    influence_type="role",
                    influence_id=role_run_id,
                    source_run_id=role_run_id,
                    accepted=False,
                    metadata={"status": "shadow_only", "disagreement": True},
                )
        chosen.append(row)
        if is_delegation(row):
            delegations += 1
        elif row["skip"]:
            refused += 1
        else:
            errors += 1
    # Research yields capacity only to rows that will run: a refusal runs no agent. Every examined
    # target stays excluded from research, because a refused one is another agent's work.
    production_reserve: dict[str, int] = {}
    for row in chosen:
        if is_delegation(row):
            production_reserve[row["agent"]] = production_reserve.get(row["agent"], 0) + 1
    reserved_targets = {str(row["target"]) for row in chosen if row.get("target")}
    range_task_types = {"testgen", "epic", "codemod", "cross_repo", "runtime_ac"}
    reserved_targets.update(
        str(item.get("target"))
        for item in items
        if item.get("target")
        and item.get("lane", "opener") == "opener"
        and item.get("task_type") in range_task_types
    )
    research = (research_tick_fn or research_tick)(
        items,
        cap,
        learned=learned,
        dry_run=dry_run,
        env=env,
        excluded_targets=reserved_targets,
        production_reserve=production_reserve,
    )
    # Ingestion is the Brain's largest evidence source and must not depend on whether this tick
    # DELEGATES: since 2026-09-03 the default active tick ingests live while delegating nothing
    # (ORCH_DISPATCH_LANE=0), so the two get separate switches. None keeps the old coupling.
    ingest = (
        outcomes.ingest_outcomes(dry_run=dry_run if ingest_dry_run is None else ingest_dry_run)
        if do_ingest
        else {"note": "skipped (do_ingest=False)"}
    )
    return {
        "chosen": chosen,
        "no_capacity": no_capacity,
        "deferred": deferred,
        "blocked": blocked,
        "ingest": ingest,
        "dry_run": dry_run,
        "cap": cap_n,
        "examine_cap": examine_n,
        "delegations": delegations,
        "refused": refused,
        "errors": errors,
        "examined": examined,
        "deferral": {
            "delegable": len(deferred_delegable),
            "drainable": len(deferred_drainable),
            "by_cap": deferred_by_cap,
            "by_examine_cap": deferred_by_examine_cap,
            "delegable_targets": deferred_delegable,
        },
        "adversarial_reviews": adversarial_reviews,
        "runtime_ac_gates": runtime_ac_gates,
        "role_shadows": role_shadows,
        "research": research,
    }


def _selftest_dispatch_lane_default_off() -> None:
    """`--active` ingests live and delegates nothing unless ORCH_DISPATCH_LANE=1 — the default the
    assessment set; the flag flips only delegation, never ingestion."""
    import os as _os

    calls: list[dict] = []
    real = globals()["remote_tick"]
    real_cap, real_backlog, real_learned = (
        router.load_capacity,
        router.load_backlog,
        router.learned_ranks,
    )

    def fake_tick(items, cap, **kw):
        calls.append(kw)
        return {"chosen": [], "deferred": []}

    saved = _os.environ.pop(DISPATCH_LANE_ENV, None)
    try:
        globals()["remote_tick"] = fake_tick
        router.load_capacity = lambda: {}
        router.load_backlog = lambda: []
        router.learned_ranks = lambda: {}
        main(["--active"])
        assert calls[-1]["dry_run"] is True and calls[-1]["ingest_dry_run"] is False, (
            "default active tick must ingest live and delegate nothing",
            calls[-1],
        )
        main([])
        assert calls[-1]["dry_run"] is True and calls[-1]["ingest_dry_run"] is True, calls[-1]
        _os.environ[DISPATCH_LANE_ENV] = "1"
        main(["--active"])
        assert calls[-1]["dry_run"] is False and calls[-1]["ingest_dry_run"] is False, (
            "ORCH_DISPATCH_LANE=1 must re-enable delegation",
            calls[-1],
        )
    finally:
        globals()["remote_tick"] = real
        router.load_capacity, router.load_backlog, router.learned_ranks = (
            real_cap,
            real_backlog,
            real_learned,
        )
        if saved is None:
            _os.environ.pop(DISPATCH_LANE_ENV, None)
        else:
            _os.environ[DISPATCH_LANE_ENV] = saved


def _selftest():
    _selftest_dispatch_lane_default_off()
    import os
    import sqlite3
    import tempfile

    old_exploration_rate = os.environ.get("ORCH_EXPLORATION_RATE")
    old_handoff = os.environ.get("HANDOFF_DIR")
    old_feedback_db = feedback.DB_PATH
    old_claims_handoff = claims._handoff_dir
    os.environ["ORCH_EXPLORATION_RATE"] = "0"
    tmp_handoff = tempfile.mkdtemp(prefix="tick-selftest-handoff-")
    os.environ["HANDOFF_DIR"] = tmp_handoff
    # Role selector hooks intentionally record accepted/missed seams. Keep the
    # selftest evidence in a disposable Brain rather than the live learning DB.
    feedback.DB_PATH = Path(tmp_handoff) / "feedback.db"
    claims._handoff_dir = lambda: Path(tmp_handoff)  # type: ignore
    # Offline, as the module says: every remote_tick below would otherwise read each target's labels
    # from GitHub. Answered, with no labels, unless a case says otherwise.
    old_target_labels = dispatcher._target_labels
    dispatcher._target_labels = lambda target: (set(), "")  # type: ignore
    subject_conn = sqlite3.connect(":memory:")
    subject_conn.executescript(research_scheduler.feedback.SCHEMA)
    research_scheduler.feedback._migrate_schema(subject_conn)

    def cap(states):
        return {"agents": {a: {"state": s} for a, s in states.items()}}

    def no_research(*args, **kwargs):
        return {"status": "skipped", "planned": [], "active": False}

    try:
        all_keep = cap({"cursor": "ok", "codex": "ok", "claude": "ok", "gemini": "ok"})
        items = [
            {"target": "stranske/Workflows#101", "task_type": "implement"},
            {"target": "stranske/Counter_Risk#5", "task_type": "mechanical"},
        ]
        out = remote_tick(
            items, all_keep, dry_run=True, do_ingest=False, research_tick_fn=no_research
        )
        assert len(out["chosen"]) == 2 and out["dry_run"] is True, out
        assert all(c["agent"] != "claude" for c in out["chosen"]), out[
            "chosen"
        ]  # reserve-aware: routine avoids claude
        assert out["chosen"][0]["agent"] == "codex", out[
            "chosen"
        ]  # implement -> codex (non-reserve lead)
        assert out["chosen"][1]["agent"] == "cursor", out["chosen"]  # mechanical -> cursor
        learned = {"implement": {"gemini": {"rank": 0, "n_obs": 2}}}
        learned_out = remote_tick(
            [{"target": "o/r#learned", "task_type": "implement"}],
            all_keep,
            learned=learned,
            dry_run=True,
            do_ingest=False,
            research_tick_fn=no_research,
        )
        assert (
            learned_out["chosen"][0]["agent"] == "gemini"
        ), learned_out  # task-specific learned slice is used
        out2 = remote_tick(
            [{"target": "o/r#1", "task_type": "implement"}],
            cap(
                {
                    "vibe": "ok",
                    "cursor": "shed",
                    "codex": "shed",
                    "claude": "shed",
                    "gemini": "shed",
                }
            ),
            dry_run=True,
            do_ingest=False,
            research_tick_fn=no_research,
        )
        assert out2["no_capacity"] and not out2["chosen"], out2  # no keepalive capacity -> skipped
        # per-tick cap: 2 items, cap=1 -> 1 delegated, 1 deferred (cost guard)
        out3 = remote_tick(
            items,
            all_keep,
            dry_run=True,
            do_ingest=False,
            max_delegations=1,
            research_tick_fn=no_research,
        )
        assert len(out3["chosen"]) == 1 and len(out3["deferred"]) == 1, out3
        # Ownership GitHub did not answer for is REFUSED, in a shadow tick and an active one alike,
        # with the reason in the plan; and a held claim whose meta is unreadable blocks the target.
        # Both reads used to answer "free" (2026-10-04).
        dispatcher._target_labels = lambda target: (None, "gh exit 1: HTTP 502")  # type: ignore
        for shadow in (True, False):
            refused = remote_tick(
                [{"target": "o/r#901", "task_type": "implement"}],
                all_keep,
                dry_run=shadow,
                do_ingest=False,
                env={},
                research_tick_fn=no_research,
            )
            row = refused["chosen"][0]
            assert row["labels_read"] is False and not row["applied"], refused
            assert "labels unread (gh exit 1: HTTP 502)" in str(row["skip"]), refused
        dispatcher._target_labels = lambda target: (set(), "")  # type: ignore
        unstamped = claims._claims_dir() / claims._slug("o/r#902")
        unstamped.mkdir(parents=True)  # mkdir'd and never stamped: held, holder unknown
        held_out = remote_tick(
            [{"target": "o/r#902", "task_type": "implement"}],
            all_keep,
            dry_run=True,
            do_ingest=False,
            research_tick_fn=no_research,
        )
        assert not held_out["chosen"], held_out
        assert held_out["blocked"][0]["reason"] == "claimed by an unknown holder", held_out
        claims.release("o/r#902")
        # THE CAP COUNTS DELEGATIONS, NEVER REFUSALS (2026-10-04). Three targets the live read
        # shows owned, then one it does not: the refusals used to take the only slot of a cap of 1
        # and defer the one target that could be delegated.
        owned_live = {"o/r#911", "o/r#912", "o/r#913"}

        def owned_or_fresh(target):
            return ({"agent:codex"} if target in owned_live else set()), ""

        dispatcher._target_labels = owned_or_fresh  # type: ignore
        capped = remote_tick(
            [{"target": f"o/r#{n}", "task_type": "implement"} for n in (911, 912, 913, 914)],
            all_keep,
            dry_run=True,
            do_ingest=False,
            max_delegations=1,
            research_tick_fn=no_research,
        )
        assert (capped["delegations"], capped["refused"], capped["examined"]) == (1, 3, 4), capped
        assert capped["deferred"] == [] and capped["chosen"][-1]["target"] == "o/r#914", capped
        # The examination bound still holds a backlog of refusals to EXAMINED_PER_DELEGATION reads
        # per slot, and the plan names what it held back: blocking, and not drainable.
        reads = []

        def owned_and_counted(target):
            reads.append(target)
            return {"agent:codex"}, ""

        dispatcher._target_labels = owned_and_counted  # type: ignore
        bounded = remote_tick(
            [{"target": f"o/r#{920 + i}", "task_type": "implement"} for i in range(6)],
            all_keep,
            dry_run=True,
            do_ingest=False,
            max_delegations=1,
            research_tick_fn=no_research,
        )
        assert len(reads) == bounded["examined"] == bounded["examine_cap"] == 4, (reads, bounded)
        assert bounded["deferral"]["by_examine_cap"] == ["o/r#924", "o/r#925"], bounded
        assert (bounded["deferral"]["delegable"], bounded["deferral"]["drainable"]) == (
            2,
            0,
        ), bounded
        # Items the rail refuses on their DISCOVERY labels are examined after the rest, so owned
        # closer PRs at the head of the backlog cannot hold back an item that can be delegated.
        owned_live = {"o/r#931", "o/r#932", "o/r#933"}
        dispatcher._target_labels = owned_or_fresh  # type: ignore
        ordered = remote_tick(
            [
                {"target": f"o/r#{n}", "lane": "closer", "labels": ["agent:codex"]}
                for n in (931, 932, 933)
            ]
            + [{"target": "o/r#934", "task_type": "implement", "labels": ["status: ready"]}],
            all_keep,
            dry_run=True,
            do_ingest=False,
            max_delegations=1,
            research_tick_fn=no_research,
        )
        assert ordered["chosen"][0]["target"] == "o/r#934" and ordered["delegations"] == 1, ordered
        assert ordered["deferred"] == ["o/r#931", "o/r#932", "o/r#933"], ordered
        assert (ordered["deferral"]["delegable"], ordered["deferral"]["drainable"]) == (
            0,
            0,
        ), ordered
        dispatcher._target_labels = lambda target: (set(), "")  # type: ignore
        research_arbitration = {}

        def capture_reserved_research(*args, **kwargs):
            research_arbitration.update(kwargs)
            return {
                "status": "blocked",
                "planned": [],
                "active": False,
                "blocked_reasons": ["production_reserved"],
            }

        # Numbered: a target with no PR number is a dispatcher ERROR row, which reserves nothing.
        # This case used one, and its truthiness check passed only because errors reserved too.
        production_range = {
            "target": "o/r#77",
            "task_type": "testgen",
            "lane": "opener",
        }
        arbitration = remote_tick(
            [production_range],
            all_keep,
            dry_run=True,
            do_ingest=False,
            research_tick_fn=capture_reserved_research,
        )
        assert arbitration["chosen"][0]["target"] == production_range["target"], arbitration
        assert (
            production_range["target"] in research_arbitration["excluded_targets"]
        ), research_arbitration
        assert research_arbitration["production_reserve"] == {
            arbitration["chosen"][0]["agent"]: 1
        }, research_arbitration
        assert (
            claims.holder(production_range["target"]) is None
        ), "production-reserved dry-run created a research claim"
        # A refusal runs no agent, so it reserves no capacity. Its target stays excluded from
        # research, because it is another agent's work.
        dispatcher._target_labels = lambda target: ({"agent:codex"}, "")  # type: ignore
        research_arbitration.clear()
        refused_range = remote_tick(
            [production_range],
            all_keep,
            dry_run=True,
            do_ingest=False,
            research_tick_fn=capture_reserved_research,
        )
        assert refused_range["refused"] == 1, refused_range
        assert research_arbitration["production_reserve"] == {}, research_arbitration
        assert production_range["target"] in research_arbitration["excluded_targets"]
        dispatcher._target_labels = lambda target: (set(), "")  # type: ignore
        high = {
            "target": "stranske/Workflows#202",
            "task_type": "implement",
            "lane": "closer",
            "labels": ["risk:high"],
            "title": "security-sensitive workflow change",
        }
        out4 = remote_tick(
            [high], all_keep, dry_run=True, do_ingest=False, research_tick_fn=no_research
        )
        assert out4["adversarial_reviews"][0]["status"] == "planned", out4
        assert out4["chosen"][0]["target"] == high["target"], out4
        quiet = _adversarial_review_status(high, dry_run=False, env={})
        assert quiet["status"] == "required_but_not_run", quiet
        ran = _adversarial_review_status(
            high,
            dry_run=False,
            env={"ORCH_RUN_ADVERSARIAL_REVIEW": "1", "ORCH_ADVERSARIAL_REVIEWERS": "vibe,gemini"},
            provision_fn=lambda target, lane: "/tmp/mock-worktree",
            review_fn=lambda worktree, reviewers, context: {
                "verdict": "PASS",
                "n_vetoes": 0,
                "reviewers": reviewers,
                "context": context,
            },
        )
        assert ran["status"] == "executed" and ran["reviewers"] == ["vibe", "gemini"], ran
        assert ran["result"]["verdict"] == "PASS", ran
        assert (
            _adversarial_review_status(
                {"target": "o/r#1", "lane": "closer", "labels": []}, dry_run=True
            )
            is None
        )

        research_shadow = research_tick(
            [{"target": "o/r#research", "task_type": "implement", "lane": "opener"}],
            cap({"cursor": "ok", "codex": "ok", "gemini": "ok", "vibe": "ok"}),
            dry_run=True,
            conn=subject_conn,
            hyps=research_scheduler.SEED_HYPOTHESES,
            rng=__import__("random").Random(0),
            unevaluated_cap=1000,
        )
        assert research_shadow["planned"] and research_shadow["active"] is False, research_shadow

        fired = []
        active = research_tick(
            [{"target": "o/r#research2", "task_type": "testgen", "lane": "opener"}],
            cap({"cursor": "ok", "codex": "ok", "gemini": "ok", "vibe": "ok"}),
            dry_run=False,
            env={"ORCH_RESEARCH_ARM": "1"},
            conn=subject_conn,
            prepare_fn=lambda repo, spec_file, exp_id, arms_v2, task_type="implement": fired.append(
                {
                    "repo": repo,
                    "spec": Path(spec_file).read_text(),
                    "exp_id": exp_id,
                    "agents": arms_v2,
                    "task_type": task_type,
                }
            )
            or {
                "exp_id": exp_id,
                "repo": repo,
                "launched": [
                    {
                        "agent": "cursor",
                        "pid": os.getpid(),
                        "log": "/tmp/research-cursor.log",
                        "worktree": "/tmp/research-cursor",
                    }
                ],
            },
            issue_body_fn=lambda target: "Concrete frozen spec",
            hyps=research_scheduler.SEED_HYPOTHESES,
            rng=__import__("random").Random(0),
            unevaluated_cap=1000,
        )
        assert active["active"] and fired and fired[0]["repo"] == "o/r", active
        assert fired[0]["task_type"] == "testgen", f"expected testgen, got {fired[0]['task_type']}"
        research_claim = claims.holder("o/r#research2")
        assert research_claim and research_claim.get("pids"), research_claim
        assert research_claim.get("exp_id") == fired[0]["exp_id"], research_claim

        released = research_tick(
            [{"target": "o/r#research-no-pids", "task_type": "implement", "lane": "opener"}],
            cap({"cursor": "ok", "codex": "ok", "gemini": "ok", "vibe": "ok"}),
            dry_run=False,
            env={"ORCH_RESEARCH_ARM": "1"},
            conn=subject_conn,
            prepare_fn=lambda repo, spec_file, exp_id, arms_v2, task_type="implement": {
                "exp_id": exp_id,
                "repo": repo,
                "launched": [m for a in arms_v2 for m in a["agents"]],
            },
            issue_body_fn=lambda target: "Concrete frozen spec",
            hyps=research_scheduler.SEED_HYPOTHESES,
            rng=__import__("random").Random(0),
            unevaluated_cap=1000,
        )
        assert released["active"], released
        assert claims.holder("o/r#research-no-pids") is None, released
        gated = research_tick(
            [{"target": "o/r#research3", "task_type": "implement", "lane": "opener"}],
            cap({"cursor": "ok", "codex": "ok", "gemini": "ok", "vibe": "ok"}),
            dry_run=False,
            env={},
            conn=subject_conn,
            prepare_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("must not launch")
            ),
            issue_body_fn=lambda target: "Concrete frozen spec",
            hyps=research_scheduler.SEED_HYPOTHESES,
            rng=__import__("random").Random(0),
            unevaluated_cap=1000,
        )
        assert gated["planned"] and gated["active"] is False, gated
        no_spare_research = research_tick(
            [{"target": "o/r#research4", "task_type": "implement", "lane": "opener"}],
            cap({"cursor": "shed", "codex": "shed", "gemini": "shed", "vibe": "shed"}),
            dry_run=True,
            conn=subject_conn,
            hyps=research_scheduler.SEED_HYPOTHESES,
        )
        assert (
            no_spare_research["status"] == "no_spare" and not no_spare_research["planned"]
        ), no_spare_research

        with tempfile.TemporaryDirectory(prefix="runtime-ac-gate-") as tmp:
            runtime_item = {
                "target": "stranske/Workflows#303",
                "task_type": "implement",
                "lane": "closer",
                "labels": ["runtime-ac"],
                "title": "Runtime-sensitive merge",
            }
            spec_path = runtime_ac_gate.spec_path(runtime_item["target"], spec_dir=tmp)
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text("{}", encoding="utf-8")

            planned_out = remote_tick(
                [runtime_item],
                all_keep,
                dry_run=True,
                do_ingest=False,
                research_tick_fn=no_research,
                runtime_ac_gate_fn=lambda item, **kwargs: {
                    "target": item["target"],
                    "status": "planned",
                    "blocks": False,
                },
            )
            assert planned_out["runtime_ac_gates"][0]["status"] == "planned", planned_out
            assert planned_out["chosen"][0]["target"] == runtime_item["target"], planned_out

            blocked_out = remote_tick(
                [runtime_item],
                all_keep,
                dry_run=False,
                env={},
                do_ingest=False,
                research_tick_fn=no_research,
                runtime_ac_gate_fn=lambda item, **kwargs: {
                    "target": item["target"],
                    "status": "executed",
                    "verdict": "FAIL",
                    "blocks": True,
                },
            )
            assert blocked_out["runtime_ac_gates"][0]["blocks"] is True, blocked_out
            assert blocked_out["blocked"] and not blocked_out["chosen"], blocked_out

        # --- v2 arm identity (line D): legacy members are why evaluations_v2 stayed empty ---
        v2 = research_v2_arms(["codex", "claude", "codex"], {"codex": "codex:gpt-5.6"})
        assert [a["arm_id"] for a in v2] == ["agent-codex", "agent-claude"], v2
        assert v2[0]["profile_id"] == "codex:gpt-5.6" and v2[1]["profile_id"] is None, v2
        assert all(len(a["agents"]) == 1 for a in v2), v2
        _arms_norm, _members = exp_abcd._normalize_arm_members(v2)
        # The whole point: members must come back NON-legacy, or record_evaluation_v2 never fires.
        assert [m["legacy"] for m in exp_abcd.experiment_members({"members": _members})] == [
            False,
            False,
        ], _members
        assert {m["member_id"] for m in _members} == {
            "agent-codex--member-01-codex",
            "agent-claude--member-01-claude",
        }, _members
        assert research_v2_arms([]) == [], "empty agent list must not fabricate an arm"

        print(
            "tick.py selftest: OK (remote choose->delegate per item, reserve-aware, learned weights, "
            "no-capacity skip, per-tick cap counts delegations not refusals, examination bound, "
            "refused-on-discovery-labels examined last, refusals reserve no research capacity, "
            "unread labels and unknown claim holders refuse, "
            "adversarial review hook, runtime AC gate hook, "
            "production-before-research arbitration, true research task_type, "
            "shadow/opt-in research hook)"
        )
    finally:
        if old_exploration_rate is None:
            os.environ.pop("ORCH_EXPLORATION_RATE", None)
        else:
            os.environ["ORCH_EXPLORATION_RATE"] = old_exploration_rate
        if old_handoff is None:
            os.environ.pop("HANDOFF_DIR", None)
        else:
            os.environ["HANDOFF_DIR"] = old_handoff
        import shutil

        subject_conn.close()
        shutil.rmtree(tmp_handoff, ignore_errors=True)
        claims._handoff_dir = old_claims_handoff  # type: ignore
        dispatcher._target_labels = old_target_labels  # type: ignore
        feedback.DB_PATH = old_feedback_db


def plan_headline(out: Mapping[str, Any], artifact: Any, *, lane_live: bool) -> str:
    """The one TICK-PLAN line. Each bound prints beside the count it bounds, as the plan carries
    them (`delegations`/`cap`, `examined`/`examine_cap`), and the deferred items print their blocking
    quantity (`deferral.delegable`) beside their drainable one. Those numbers are read from the plan
    `remote_tick` wrote, never recounted here, so the line cannot disagree with the loop. A number the
    plan does not carry prints `?`, never 0: unknown is not a measured zero.

    Until 2026-10-04 the shadow line read "3 targets chosen, 0 applied, 3 skipped" every hour.
    "Skipped" lumped a refusal together with a delegation that would have applied, so it could not show
    that every slot held a refusal while an item was deferred."""
    chosen = out.get("chosen") or []
    applied = sum(row.get("applied") is True for row in chosen)
    # The ownership read's own pair: a target whose labels GitHub did not return is refused,
    # and the next tick reads it again, so "0 unanswered" is this refusal fully drained. A row
    # that never reached a read (an error) is in neither count.
    answered = sum(row.get("labels_read") is True for row in chosen)
    unanswered = sum(row.get("labels_read") is False for row in chosen)
    deferral = out.get("deferral") or {}

    def n(value: Any) -> str:
        return "?" if value is None else str(value)

    delegations = f"delegations {n(out.get('delegations'))}/{n(out.get('cap'))}"
    delegations += f" attempted, {applied} applied" if lane_live else " would apply (shadow)"
    errors = f", {out['errors']} dispatcher errors" if out.get("errors") else ""
    return (
        f"TICK-PLAN: {delegations}; {n(out.get('refused'))} refused{errors}; "
        f"examined {n(out.get('examined'))}/{n(out.get('examine_cap'))}; "
        f"label reads {answered} answered, {unanswered} unanswered (refused); "
        f"{len(out.get('no_capacity') or [])} no capacity, "
        f"{len(out.get('blocked') or [])} blocked; "
        f"deferred {len(out.get('deferred') or [])} (delegable {n(deferral.get('delegable'))}, "
        f"drainable {n(deferral.get('drainable'))}) -> {artifact}"
    )


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0
    active = "--active" in argv
    # THE DISPATCH LANE IS SHADOW BY DEFAULT (assessment 2026-09-03, item 1): in 30 days this lane
    # made 14 remote dispatches and none was shown to be the labelled agent's work — the 5 credited
    # merges were other lanes' work or empty, the 2 PRs closed unmerged were bootstraps whose agent
    # round never completed, and 7 labelled issues closed with no PR from the labelled agent, 4 of
    # them delivered by codex keepalive or by hand (re-measured 2026-10-04 from keepalive's runner
    # records) — while keepalive ran 1,239 agent rounds without it, and its heartbeat
    # cost the closer lane a round each time it fired. An
    # active tick therefore INGESTS live (the Brain's evidence) and DELEGATES nothing unless
    # ORCH_DISPATCH_LANE=1 is set deliberately. Announced every run so the state is never silent.
    lane_live = active and os.environ.get(DISPATCH_LANE_ENV, "0") == "1"
    print(
        f"[tick] mode={'active' if active else 'shadow'} dispatch_lane={'LIVE' if lane_live else 'shadow (' + DISPATCH_LANE_ENV + '=0)'} ingest={'live' if active else 'dry'}",
        file=sys.stderr,
    )
    cap = router.load_capacity()
    items = router.load_backlog()
    out = remote_tick(
        items,
        cap,
        learned=router.learned_ranks(),
        dry_run=not lane_live,
        ingest_dry_run=not active,
    )
    if "--summary" in argv:
        state_dir = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator"))
        artifact = state_dir / "tick-plan.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tick-plan-", suffix=".json", dir=state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(out, handle, indent=2, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, artifact)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(plan_headline(out, artifact, lane_live=lane_live))
    else:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
