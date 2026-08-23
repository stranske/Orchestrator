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
from pathlib import Path

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
    env: dict | None = None,
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
    env: dict | None = None,
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
                repo,
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


def remote_tick(
    items: list,
    cap: dict,
    *,
    learned: dict | None = None,
    dry_run: bool = True,
    do_ingest: bool = True,
    max_delegations: int | None = None,
    env: dict | None = None,
    runtime_ac_gate_fn=None,
    research_tick_fn=None,
) -> dict:
    """Choose + delegate each item to a keepalive agent (remote), then ingest outcomes. Applies no labels
    when dry_run; do_ingest=False skips the ingest pass (tests). Caps delegations per tick
    (ORCH_MAX_REMOTE_PER_TICK, default 3) so a large backlog can't fan out unbounded autonomous spend —
    excess items are DEFERRED to the next tick."""
    import os

    env = os.environ if env is None else env
    roles.reset_role_invocation_counts()
    if not dry_run:
        claims.reap_stale()
    cap_n = (
        max_delegations
        if max_delegations is not None
        else int(os.environ.get("ORCH_MAX_REMOTE_PER_TICK", "3"))
    )
    chosen, no_capacity, deferred, blocked, adversarial_reviews, runtime_ac_gates = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
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
    for item in items:
        if len(chosen) >= cap_n:  # per-tick cap: defer the rest (cost guard)
            deferred.append(item.get("target"))
            continue
        tt = item.get("task_type", "implement")
        held = claims.holder(str(item.get("target")))
        if held:
            blocked.append(
                {
                    "target": item.get("target"),
                    "task_type": tt,
                    "reason": f"claimed by {held.get('agent')}",
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
        if not dry_run:
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
        chosen.append(
            {
                "target": item.get("target"),
                "task_type": tt,
                "agent": pick["agent"],
                "applied": res.get("applied"),
                "skip": res.get("skip"),
                "dry_run": dry_run,
            }
        )
    production_reserve: dict[str, int] = {}
    for row in chosen:
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
    ingest = (
        outcomes.ingest_outcomes(dry_run=dry_run)
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
        "adversarial_reviews": adversarial_reviews,
        "runtime_ac_gates": runtime_ac_gates,
        "role_shadows": role_shadows,
        "research": research,
    }


def _selftest():
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
        research_arbitration = {}

        def capture_reserved_research(*args, **kwargs):
            research_arbitration.update(kwargs)
            return {
                "status": "blocked",
                "planned": [],
                "active": False,
                "blocked_reasons": ["production_reserved"],
            }

        production_range = {
            "target": "o/r#production-range",
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
        assert research_arbitration["production_reserve"], research_arbitration
        assert (
            claims.holder(production_range["target"]) is None
        ), "production-reserved dry-run created a research claim"
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
            "no-capacity skip, per-tick cap, adversarial review hook, runtime AC gate hook, "
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
        feedback.DB_PATH = old_feedback_db


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0
    dry = "--active" not in argv  # DEFAULT shadow/dry-run; --active really delegates + ingests
    cap = router.load_capacity()
    items = router.load_backlog()
    out = remote_tick(items, cap, learned=router.learned_ranks(), dry_run=dry)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
