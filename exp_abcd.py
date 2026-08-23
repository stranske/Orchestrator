#!/usr/bin/env python3
"""exp_abcd.py — run an A/B/C/D agent experiment and feed the result to the learning store.

WHY THIS EXISTS: an orchestrator that always hands "implement" to the same agent is following
a rule, not making a judgment — and it never gathers evidence that the rule is right. This is
the cure: give the SAME frozen spec to every plausible agent, in isolated worktrees, then have
every agent cross-evaluate every output. That produces UNBIASED cross-agent evidence (the
bootstrap exploration the feedback loop needs to escape selection bias) which is recorded in
feedback.py — closing the loop end to end.

It deliberately does NOT use dispatcher.delegate: that enforces one-agent-per-target (claims)
and opens a PR. Here, four agents implement the same task on isolated branches and NOTHING is
merged — the deliverable is the comparison, not a PR. Reuses provision (local-disk worktrees)
and the same PATH+auth detached wrapper as the dispatcher.

Phases (each a separate invocation; implements run detached so this returns fast):
  prepare  <repo> <spec_file> <exp_id> <a,b,c,d>  provision isolated worktrees + spawn each
                                                   agent implementing the spec at its mode; record runs
  status   <exp_id>                                per-agent: alive? diff stat? log tail
  collect  <repo> <exp_id>                         write each agent's diff (vs base) to exp dir
  evaluate <repo> <spec_file> <exp_id> [agents]    each agent scores every diff -> 4x4 matrix -> store
                                                   accepts --timeout/--eval-timeout seconds

Pure helpers are selftested offline (`--selftest`).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import adapters
import capabilities
import dispatcher
import execution_profiles
import feedback
import judge_reliability
import provision
import research_subjects
import synthesis_promotion
import watch

ORCH = Path(__file__).resolve().parent
EXP_DIR = Path(os.environ.get("ORCH_EXP_DIR", ORCH / "experiments"))
DISPATCH_LOG_DIR = (
    Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff")) / "dispatch-logs"
)

# The reasoning/mode the ORCHESTRATOR would assign each agent for a complex, multi-file
# integration implement — set deliberately, so the experiment tests the choice I'd actually make:
#   claude/codex = full premium reasoning; gemini = its reasoning default (a 2nd-tier reasoning
#   seat); cursor = composer, the FREE lane — the very habit under test (is composer adequate
#   for a reasoning-heavy implement, or is leaning on it for "implement" a mistake?).
AGENT_MODE = {
    "claude": "full",
    "codex": "full",
    "gemini": "full",
    "cursor": "composer",
    "vibe": "full",
}

# User policy (2026-06-15): an A/B run must ALWAYS field >=4 evaluators. More independent judges
# (a) LIMIT SELF-FAVORING — the topped-up judges are non-implementers, with no own work to inflate —
# and (b) yield more signal on each evaluator's own strengths/weaknesses. Generalizes the docsdrift1
# N=2 fragility finding (a 2-implementer A/B had no counted neutral judge). Top up in this order:
# most-reliable-neutral first; gemini last since _winner_and_harvest excludes it from the ranking mean.
EVALUATOR_TOPUP_ORDER = ("claude", "codex", "cursor", "vibe", "gemini")
MIN_EVALUATORS = 4

AUTH_FILES = {  # same as dispatcher: PATH resolves the binary, auth lets it actually run
    "cursor": "$HOME/.cursor/cursor-agent.env",
    "claude": "$HOME/.codex/handoff/.claude-oauth-token",
    "aider": "$HOME/.codex/handoff/aider.env",
}


def _identity_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "")).strip("-.")
    if not slug:
        raise ValueError("experiment identity cannot be empty")
    return slug


def member_identity(arm_id: str, agent: str, ordinal: int) -> str:
    """Return the immutable identity for one execution inside one strategy arm."""
    return f"{_identity_slug(arm_id)}--member-{int(ordinal) + 1:02d}-" f"{_identity_slug(agent)}"


def _artifact_identity(agent: str, member_id: str | None = None) -> str:
    # Legacy experiments have no member id and intentionally retain their old paths.
    return _identity_slug(member_id or agent)


def exp_branch(exp_id: str, agent: str, member_id: str | None = None) -> str:
    return f"exp/{_identity_slug(exp_id)}-{_artifact_identity(agent, member_id)}"


def exp_worktree(repo: str, exp_id: str, agent: str, member_id: str | None = None) -> Path:
    return provision.WORKTREES_DIR / (
        f"{provision.repo_slug(repo)}__{_identity_slug(exp_id)}__"
        f"{_artifact_identity(agent, member_id)}"
    )


def exp_diff_path(agent: str, member_id: str | None = None) -> str:
    return f"diff-{_artifact_identity(agent, member_id)}.patch"


def exp_log_path(agent: str, member_id: str | None = None) -> str:
    return f"{_artifact_identity(agent, member_id)}.log"


def experiment_members(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Read v2 arm/member metadata, with a non-model-specific legacy fallback."""
    raw_members = meta.get("members")
    if isinstance(raw_members, list) and raw_members:
        members = []
        for raw in raw_members:
            if not isinstance(raw, dict):
                raise ValueError("experiment member metadata must be objects")
            arm_id = str(raw.get("arm_id") or "").strip()
            member_id = str(raw.get("member_id") or "").strip()
            agent = str(raw.get("agent") or "").strip()
            if not arm_id or not member_id or not agent:
                raise ValueError("v2 experiment member lacks arm_id/member_id/agent")
            members.append(
                {
                    "arm_id": arm_id,
                    "member_id": member_id,
                    "agent": agent,
                    "profile_id": raw.get("profile_id"),
                    "strategy": raw.get("strategy") or "single",
                    "legacy": False,
                }
            )
        ids = [member["member_id"] for member in members]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate experiment member_id")
        return members
    return [
        {
            "arm_id": None,
            "member_id": str(agent),
            "agent": str(agent),
            "profile_id": None,
            "strategy": "legacy_agent",
            "legacy": True,
        }
        for agent in (meta.get("agents") or [])
    ]


def _member_run_id(exp_id: str, member: dict[str, Any]) -> str:
    if member.get("legacy"):
        return f"{exp_id}:{member['agent']}"
    return f"{exp_id}:member:{member['member_id']}"


def _member_routing_metadata(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_identity_version": 2,
        "experiment_arm_id": member.get("arm_id"),
        "experiment_member_id": member.get("member_id"),
        "profile_id": member.get("profile_id"),
        "strategy": member.get("strategy"),
    }


def exp_paths(exp_id: str) -> Path:
    return EXP_DIR / exp_id


def implement_prompt(spec: str) -> str:
    """Identical for every agent — fairness requires one frozen spec. Commit, but DON'T push or
    open a PR: this is an isolated experiment branch, judged by its diff."""
    return (
        "You are implementing a change to this repository against a FROZEN SPECIFICATION. "
        "Work only in the repository working directory where you were launched; do not clone, "
        "copy, or switch to another scratch repository because the experiment harness collects "
        "the committed diff from this exact worktree. "
        "Implement it COMPLETELY and correctly — satisfy every requirement and the repo's "
        "definition-of-done in the spec (e.g. registering new workflows/files where the spec says to). "
        "When done: run any obvious local checks, then `git add -A` and `git commit` with a clear "
        "message. DO NOT push and DO NOT open a pull request — this is an isolated experiment branch; "
        "the deliverable is your committed diff. NOTE: the local environment may lack some test "
        "dependencies; if the test suite cannot run, that is fine — prioritize a COMPLETE, CORRECT "
        "implementation of the spec (including the new tests as files) and do NOT spend significant "
        "time on environment setup. Here is the specification:\n\n" + spec
    )


def _active_evidence_contract() -> str:
    types = feedback.active_evidence_types()
    if not types:
        return (
            "Active evidence types: none currently registered. Return "
            '"cited_evidence_types": [].'
        )
    lines = [
        "Active evidence types you may cite if they materially affect your judgment:",
    ]
    for row in types:
        rationale = f" - {row['rationale']}" if row.get("rationale") else ""
        lines.append(f"- {row['name']}{rationale}")
    lines.append(
        'Return exact names in "cited_evidence_types" only when the evidence type '
        "was actually used in your verdict; otherwise return []."
    )
    return "\n".join(lines)


def evaluate_prompt(
    spec: str,
    candidates: dict[str, str],
    *,
    evidence_contract_plan: dict[str, Any] | None = None,
) -> str:
    """Anonymized: candidates are labeled by letter, NOT agent — so evaluators judge on merit.
    The orchestrator keeps the letter->agent map secret and checks self-favoring AFTER scoring.
    """
    blocks = "\n\n".join(f"===== CANDIDATE {k} =====\n{v}" for k, v in candidates.items())
    letters = ", ".join(candidates.keys())
    return (
        "You are an impartial code reviewer. Below is a frozen SPECIFICATION followed by several "
        "candidate implementations (anonymized as letters). Judge each ONLY on how well it satisfies "
        "the spec and the repo's definition-of-done, its correctness, and its risk.\n\n"
        "Use the supplied specification, candidate diffs, and explicit evidence in this prompt. Do not "
        "inspect orchestration worktrees, process tables, or unrelated repo state; list missing runtime "
        "or test evidence in evidence_gaps instead.\n\n"
        "Return STRICT JSON only, no prose, exactly this shape:\n"
        '{"scores": {"' + letters.split(",")[0].strip() + '": <0-10>, ...for every candidate}, '
        '"best": "<letter>", "worst": "<letter>", '
        '"notes": {"<letter>": "<one-line: key strength or fatal flaw>"}, '
        '"evidence_gaps": ["<missing evidence that would have improved judgment>", "..."], '
        '"cited_evidence_types": ["<exact active evidence type name>", "..."], '
        '"cited_evidence_contracts": ["<exact supplied shadow contract plan ID>"]}\n'
        'Use "evidence_gaps": [] if the supplied spec and diffs were sufficient.\n\n'
        + _active_evidence_contract()
        + "\n\n"
        + (
            __import__("capability_compiler").evaluator_prompt_fragment(evidence_contract_plan)
            if evidence_contract_plan is not None
            else "No candidate-only shadow evidence contract was supplied."
        )
        + "\n\n"
        "===== SPECIFICATION =====\n" + spec + "\n\n" + blocks
    )


def _wrapped(agent: str, argv: list[str]) -> str:
    # Keep the experiment runner aligned with dispatcher/offload isolation.
    # Cursor and Vibe otherwise try to write mutable state under real-home
    # dotdirs, which the Codex sandbox may not allow.
    return (
        f"{dispatcher._path_prefix()}; "
        f"({dispatcher._agent_runtime_prelude(agent)}"
        f"{dispatcher._auth_prelude(agent)}{shlex.join(argv)})"
    )


def _record_execution_start(
    agent: str,
    mode: str,
    run_id: str,
    target: str,
    task_type: str,
    log: Path,
    profile: dict | None = None,
    causal_context: dict | None = None,
) -> int:
    started_ts = int(time.time())
    adapters.record_ledger(
        agent,
        count=1,
        cost_usd=0.0,
        event="start",
        run_id=run_id,
        target=target,
        mode=mode,
        model=adapters.model_identity(agent, mode, profile),
        task_type=task_type,
        log_file=str(log),
        started_ts=started_ts,
        selected_profile_id=profile.get("profile_id") if profile else None,
        requested_model=profile.get("requested_model") if profile else None,
        policy_version=execution_profiles.PROFILE_POLICY_VERSION if profile else None,
        propensity=1.0 if profile else None,
        causal_context=causal_context,
    )
    return started_ts


def _record_execution_complete(
    agent: str,
    mode: str,
    run_id: str,
    target: str,
    task_type: str,
    log: Path,
    started_ts: int,
    profile: dict | None = None,
    causal_context: dict | None = None,
) -> None:
    adapters.record_ledger(
        agent,
        count=0,
        cost_usd=0.0,
        event="complete",
        run_id=run_id,
        target=target,
        mode=mode,
        task_type=task_type,
        log_file=str(log),
        started_ts=started_ts,
        selected_profile_id=profile.get("profile_id") if profile else None,
        requested_model=profile.get("requested_model") if profile else None,
        policy_version=execution_profiles.PROFILE_POLICY_VERSION if profile else None,
        propensity=1.0 if profile else None,
        causal_context=causal_context,
    )


def _completion_cmd(
    agent: str,
    mode: str,
    run_id: str,
    target: str,
    task_type: str,
    log: Path,
    started_ts: int,
    profile: dict | None = None,
    causal_context: dict | None = None,
) -> str:
    argv = [
        "python3",
        str(ORCH / "ledger_reconcile.py"),
        "complete",
        "--run-id",
        run_id,
        "--agent",
        agent,
        "--target",
        target,
        "--mode",
        str(mode or ""),
        "--task-type",
        task_type,
        "--log-file",
        str(log),
        "--started-ts",
        str(started_ts),
    ]
    if profile:
        argv += [
            "--selected-profile-id",
            profile["profile_id"],
            "--requested-model",
            profile["requested_model"],
            "--policy-version",
            execution_profiles.PROFILE_POLICY_VERSION,
            "--propensity",
            "1.0",
        ]
    if causal_context and causal_context.get("subject_id"):
        argv += ["--subject-id", str(causal_context["subject_id"])]
    if causal_context and causal_context.get("arm_id"):
        argv += ["--arm-id", str(causal_context["arm_id"])]
    return shlex.join(argv)


def _spawn(
    agent: str,
    mode: str,
    prompt: str,
    cwd: Path,
    log: Path,
    *,
    run_id: str | None = None,
    target: str | None = None,
    task_type: str = "implement",
    profile_id: str | None = None,
    causal_context: dict | None = None,
) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    if agent == "gemini":
        prompt = dispatcher._gemini_workspace_prompt(prompt, cwd)
    profile = execution_profiles.get_profile(profile_id) if profile_id else None
    argv = (
        adapters.build_command(
            agent, prompt, mode, cwd=cwd, profile=profile, transport="experiment"
        )
        if profile
        else adapters.build_command(agent, prompt, mode, cwd=cwd)
    )
    run_id = run_id or f"exp:{log.stem}:{agent}:{time.time_ns()}"
    target = target or f"exp:{cwd.name}"
    started_ts = _record_execution_start(
        agent, mode, run_id, target, task_type, log, profile, causal_context
    )
    if profile:
        feedback.record_execution_attempt(
            run_id,
            attempt_id=f"attempt:profile:{run_id}",
            operation_role="worker",
            profile_id=profile["profile_id"],
            requested_provider=profile["provider"],
            requested_model=profile["requested_model"],
            status="started",
            source="experiment-profile-decision",
            started_ts=started_ts,
        )
    complete = _completion_cmd(
        agent, mode, run_id, target, task_type, log, started_ts, profile, causal_context
    )
    # Marker BEFORE the python completion: the python step gets SIGKILLed in the wild (audit F2);
    # the microsecond printf survives and ledger_reconcile backfills latency/exit from it.
    marker = adapters.done_marker_cmd(run_id, log, "orch_exp_rc")
    wrapped = f"{_wrapped(agent, argv)}; orch_exp_rc=$?; {marker}; {complete}; exit $orch_exp_rc"
    with log.open("a") as fh:
        fh.write(
            f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} EXP "
            f"{agent}/{mode} [{task_type}] cwd={cwd} run_id={run_id} ===\n"
        )
        try:
            proc = subprocess.Popen(
                ["bash", "-lc", wrapped],
                cwd=str(cwd),
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            if profile:
                feedback.complete_profile_attempt_unresolved(
                    run_id,
                    selected_profile_id=profile["profile_id"],
                    fallback_reason="profile_process_start_failed",
                    status="failed",
                    completed_ts=int(time.time()),
                )
            _record_execution_complete(
                agent,
                mode,
                run_id,
                target,
                task_type,
                log,
                started_ts,
                profile,
                causal_context,
            )
            raise
    return proc.pid


def prepare(
    repo: str,
    spec_file: str,
    exp_id: str,
    agents: list[str],
    task_type: str = "implement",
    profiles: dict[str, str] | None = None,
) -> dict:
    spec = Path(spec_file).read_text()
    canon = provision.ensure_canonical(repo)
    base = provision.base_branch(repo)
    provision._run(["git", "-C", str(canon), "fetch", "origin", base], check=False)
    edir = exp_paths(exp_id)
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "spec.md").write_text(spec)
    # Pin the exact cut point (2026-07-08): consumer repos merge PRs hourly, so "origin/<base>"
    # DRIFTS between launch and the followup's collect/anchor passes — objective anchors must
    # apply arm diffs against the SHA the worktrees were actually cut from or clean patches
    # false-fail as apply-fail (observed live on the first anchored experiment).
    base_sha = provision._run(
        ["git", "-C", str(canon), "rev-parse", f"origin/{base}"], check=False
    ).stdout.strip()
    (edir / "meta.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "base": base,
                "base_sha": base_sha or None,
                "agents": agents,
                "exp_id": exp_id,
                "task_type": task_type,
            }
        )
    )
    launched = []
    prompt = implement_prompt(spec)
    for agent in agents:
        wt = exp_worktree(repo, exp_id, agent)
        br = exp_branch(exp_id, agent)
        if not (wt / ".git").exists():
            provision._run(
                [
                    "git",
                    "-C",
                    str(canon),
                    "worktree",
                    "add",
                    "-b",
                    br,
                    str(wt),
                    f"origin/{base}",
                ]
            )
        profile_id = (profiles or {}).get(agent)
        profile = execution_profiles.get_profile(profile_id) if profile_id else None
        mode = AGENT_MODE.get(agent, "full")
        log = edir / f"{agent}.log"
        rid = f"{exp_id}:{agent}"
        target = f"{repo} [exp {exp_id}]"
        feedback.record_run(
            rid,
            f"{repo} [exp {exp_id}]",
            task_type,
            agent,
            mode=mode,
            reasoning_level=profile.get("reasoning_effort") if profile else mode,
            rationale="A/B/C/D experiment — unbiased cross-agent evidence",
            experiment_id=exp_id,
            model=adapters.model_identity(agent, mode, profile),
            capability_ids=["abcd-experiment"],
            influenced_by_workflow_ids=["exp_abcd"],
            routing_metadata=(
                {
                    "selected_profile_id": profile_id,
                    "requested_model": profile["requested_model"],
                    "transport": "experiment",
                    "profile_policy_version": execution_profiles.PROFILE_POLICY_VERSION,
                    "profile_assignment_probability": 1.0,
                }
                if profile
                else None
            ),
        )
        pid = _spawn(
            agent,
            mode,
            prompt,
            wt,
            log,
            run_id=rid,
            target=target,
            task_type=task_type,
            profile_id=profile_id,
            causal_context={"arm_id": agent},
        )
        launched.append(
            {
                "agent": agent,
                "mode": mode,
                "profile_id": profile_id,
                "pid": pid,
                "worktree": str(wt),
                "branch": br,
                "log": str(log),
            }
        )
    return {"exp_id": exp_id, "repo": repo, "base": base, "launched": launched}


def _normalize_arm_members(arms: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    normalized_arms: list[dict] = []
    members: list[dict] = []
    seen_arms: set[str] = set()
    seen_members: set[str] = set()
    for arm in arms:
        if not isinstance(arm, dict):
            raise ValueError("strategy arms must be objects")
        arm_id = str(arm.get("arm_id") or "").strip()
        agents = [str(agent).strip() for agent in (arm.get("agents") or [])]
        if not arm_id or not agents or any(not agent for agent in agents):
            raise ValueError("each strategy arm requires arm_id and agents")
        if arm_id in seen_arms:
            raise ValueError(f"duplicate arm_id: {arm_id}")
        seen_arms.add(arm_id)
        raw_members = arm.get("members") or []
        arm_members = []
        for ordinal, agent in enumerate(agents):
            raw = raw_members[ordinal] if ordinal < len(raw_members) else {}
            member_id = str(raw.get("member_id") or member_identity(arm_id, agent, ordinal))
            if member_id in seen_members:
                raise ValueError(f"duplicate member_id: {member_id}")
            seen_members.add(member_id)
            member = {
                "arm_id": arm_id,
                "member_id": member_id,
                "agent": agent,
                "profile_id": raw.get("profile_id") or arm.get("profile_id"),
                "strategy": arm.get("strategy") or ("single" if len(agents) == 1 else "parallel"),
                "ordinal": ordinal,
            }
            arm_members.append(member)
            members.append(member)
        normalized = dict(arm)
        normalized["arm_id"] = arm_id
        normalized["agents"] = agents
        normalized["members"] = arm_members
        normalized_arms.append(normalized)
    if not normalized_arms:
        raise ValueError("at least one strategy arm is required")
    return normalized_arms, members


def research_v2_arms(agents: list[str], profiles=None) -> list[dict]:
    """Turn a plain agent list into v2 arm/member descriptors. Pure; selftested.

    `prepare` writes only `meta["agents"]`, so `experiment_members()` takes its legacy fallback and
    every member comes back `legacy=True` -- which is why `record_evaluation_v2` never fires and
    `evaluations_v2` sat empty while `evaluations` accumulated thousands of
    `agent_parent_projection` rows. `prepare_arms` already persists the exact v2 manifest; the
    launchers simply never called it.

    One arm per agent, which is exactly what the legacy path meant: each agent is its own
    experimental condition. `profile_id` is carried through when the plan knows it and left None
    when it does not -- an honest unknown, never a fabricated identity.

    Lives HERE, not in a launcher, because two launchers need it (`tick.research_tick` and
    `exploration_backfill`) and a second copy would let one drift back to the legacy shape.
    """
    lookup = profiles if isinstance(profiles, dict) else {}
    arms: list[dict] = []
    for agent in dict.fromkeys(str(a).strip() for a in agents if str(a).strip()):
        arms.append(
            {
                "arm_id": f"agent-{agent}",
                "agents": [agent],
                "strategy": "single",
                "profile_id": lookup.get(agent) or None,
            }
        )
    return arms


def prepare_arms(
    repo: str,
    spec_file: str,
    exp_id: str,
    arms: list[dict[str, Any]],
    task_type: str = "implement",
) -> dict:
    """Prepare every member independently and persist one immutable v2 manifest."""
    spec = Path(spec_file).read_text()
    canon = provision.ensure_canonical(repo)
    base = provision.base_branch(repo)
    provision._run(["git", "-C", str(canon), "fetch", "origin", base], check=False)
    base_sha = provision._run(
        ["git", "-C", str(canon), "rev-parse", f"origin/{base}"], check=False
    ).stdout.strip()
    normalized_arms, members = _normalize_arm_members(arms)
    edir = exp_paths(exp_id)
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "spec.md").write_text(spec)
    meta = {
        "schema_version": 2,
        "repo": repo,
        "base": base,
        "base_sha": base_sha or None,
        "agents": list(dict.fromkeys(member["agent"] for member in members)),
        "exp_id": exp_id,
        "task_type": task_type,
        "arms": normalized_arms,
        "members": members,
    }
    (edir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))

    launched = []
    prompt = implement_prompt(spec)
    for member in members:
        agent = member["agent"]
        member_id = member["member_id"]
        wt = exp_worktree(repo, exp_id, agent, member_id)
        br = exp_branch(exp_id, agent, member_id)
        if not (wt / ".git").exists():
            provision._run(
                [
                    "git",
                    "-C",
                    str(canon),
                    "worktree",
                    "add",
                    "-b",
                    br,
                    str(wt),
                    f"origin/{base}",
                ]
            )
        profile_id = member.get("profile_id")
        execution_profile_id = (
            profile_id if profile_id in execution_profiles.PROFILE_REGISTRY else None
        )
        profile = (
            execution_profiles.get_profile(execution_profile_id) if execution_profile_id else None
        )
        mode = AGENT_MODE.get(agent, "full")
        log = edir / exp_log_path(agent, member_id)
        run_id = _member_run_id(exp_id, member)
        target = f"{repo} [exp {exp_id} arm {member['arm_id']} member {member_id}]"
        feedback.record_run(
            run_id,
            target,
            task_type,
            agent,
            mode=mode,
            reasoning_level=profile.get("reasoning_effort") if profile else mode,
            rationale=f"experiment arm {member['arm_id']} member {member_id}",
            experiment_id=exp_id,
            model=adapters.model_identity(agent, mode, profile),
            capability_ids=["abcd-experiment"],
            influenced_by_workflow_ids=["exp_abcd"],
            routing_metadata={
                **(_member_routing_metadata(member) or {}),
                **(
                    {
                        "selected_profile_id": execution_profile_id,
                        "requested_model": profile["requested_model"],
                        "transport": "experiment",
                        "profile_policy_version": execution_profiles.PROFILE_POLICY_VERSION,
                        "profile_assignment_probability": 1.0,
                    }
                    if profile
                    else {}
                ),
            },
        )
        pid = _spawn(
            agent,
            mode,
            prompt,
            wt,
            log,
            run_id=run_id,
            target=target,
            task_type=task_type,
            profile_id=execution_profile_id,
            causal_context={"arm_id": member["arm_id"]},
        )
        launched.append(
            {
                **member,
                "run_id": run_id,
                "mode": mode,
                "pid": pid,
                "worktree": str(wt),
                "branch": br,
                "log": str(log),
                "diff": str(edir / exp_diff_path(agent, member_id)),
            }
        )
    return {
        "exp_id": exp_id,
        "repo": repo,
        "base": base,
        "base_sha": base_sha or None,
        "launched": launched,
        "arms": normalized_arms,
    }


def prepare_arm(
    repo: str,
    spec_file: str,
    exp_id: str,
    arm: dict[str, Any],
    task_type: str = "implement",
) -> dict:
    """Compatibility wrapper for callers intentionally preparing exactly one arm."""
    return prepare_arms(repo, spec_file, exp_id, [arm], task_type)


def status(exp_id: str, *, stale_seconds: int = watch.DEFAULT_STALE_SECONDS) -> dict:
    meta = json.loads((exp_paths(exp_id) / "meta.json").read_text())
    repo, base = meta["repo"], meta["base"]
    out = []
    for member in experiment_members(meta):
        agent = member["agent"]
        artifact_member = None if member["legacy"] else member["member_id"]
        wt = exp_worktree(repo, exp_id, agent, artifact_member)
        log = exp_paths(exp_id) / exp_log_path(agent, artifact_member)
        report = watch.classify_lane(
            agent=agent,
            target=(
                f"{repo} [exp {exp_id}]"
                if member["legacy"]
                else f"{repo} [exp {exp_id} arm {member['arm_id']} member {member['member_id']}]"
            ),
            log=str(log),
            worktree=str(wt),
            base_ref=f"origin/{base}",
            stale_seconds=stale_seconds,
        )
        signals = report.get("signals") or {}
        out.append(
            {
                "agent": agent,
                "arm_id": member.get("arm_id"),
                "member_id": member["member_id"],
                "profile_id": member.get("profile_id"),
                "worktree_diff_stat": signals.get("uncommitted_diff_stat", "(none)"),
                "committed_diff_stat": signals.get("committed_diff_stat", "(none)"),
                "log_tail": report.get("log_tail", ""),
                "state": report.get("state"),
                "recommended_action": report.get("recommended_action"),
                "hints": report.get("hints", []),
                "signals": signals,
            }
        )
    return {"exp_id": exp_id, "members": out, "agents": out}


def _join_diffs(*parts: str) -> str:
    """Concatenate git diffs WITHOUT mangling them (2026-07-08 live-caught bug): the old
    `"\\n".join(part.strip() ...)` deleted trailing context lines — a diff's empty context line is
    a single SPACE, which str.strip() eats, leaving the hunk shorter than its header claims →
    `git apply` rejects the whole patch as corrupt. Each kept part keeps its internal bytes and a
    terminating newline; multi-file diffs concatenate back-to-back with no separator."""
    kept = [p for p in parts if p and p.strip()]
    return "".join(p if p.endswith("\n") else p + "\n" for p in kept)


# Per-candidate diff cap inside the anonymized eval prompt (2026-07-08 live-caught bug): the eval
# command passes the prompt as ONE argv element via "$(cat file)", so an 8.4MB cursor diff blew
# execve's arg limit ("Argument list too long") and killed EVERY judge. No judge usefully reads
# megabytes of diff anyway; oversized candidates are truncated with an explicit marker so judges
# can score what fits and know something was cut.
EVAL_DIFF_CAP_CHARS = 100_000


def _capped_diff(text: str, cap: int = EVAL_DIFF_CAP_CHARS) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n[... diff truncated: {len(text) - cap} of {len(text)} chars cut]\n"


def collect(repo: str, exp_id: str) -> dict:
    meta = json.loads((exp_paths(exp_id) / "meta.json").read_text())
    base = meta["base"]
    base_ref = f"origin/{base}"
    edir = exp_paths(exp_id)
    written = {}
    for member in experiment_members(meta):
        agent = member["agent"]
        artifact_member = None if member["legacy"] else member["member_id"]
        wt = exp_worktree(repo, exp_id, agent, artifact_member)
        # THE WORKTREE IS NOT THE ONLY COPY, AND ASSUMING IT WAS DESTROYED RECOVERED EVIDENCE.
        # Worktrees are reclaimed by the GC after 14 days; the arm's commits live on in the shared
        # per-repo store as `exp/<exp_id>-<agent>`, which is exactly why the GC skips the archive
        # tier for this root. Without this fallback, collect() on a reclaimed experiment returned an
        # EMPTY diff, and followup then stamped `followup-skip.json` -- permanently marking evidence
        # as lost while the branch sat intact. Measured 2026-08-21: it zeroed 4 recovered experiments
        # before the fallback landed. Same computation, branch instead of worktree.
        if not (wt / ".git").exists():
            try:
                import experiment_recovery

                # THE MEMBER ID IS PART OF THE BRANCH NAME (`exp/<exp_id>-<member_id>`), so a v2
                # arm's branch is unreachable without it and recovery reported the evidence gone.
                recovered = experiment_recovery.arm_diff(
                    repo, exp_id, agent, base=base, member_id=artifact_member
                )
            except Exception:
                recovered = None
            if recovered is not None:
                rp = edir / exp_diff_path(agent, artifact_member)
                # NEVER REPLACE EVIDENCE WITH SILENCE: an empty recovery must not clobber a diff that
                # is already on disk. An empty diff is a real verdict (apply-fail / no-delivery), so
                # writing one over real content would forge machine ground truth.
                if recovered.strip() or not (rp.exists() and rp.read_text().strip()):
                    rp.write_text(recovered)
                written[member["member_id"]] = {
                    "path": str(rp),
                    "bytes": len(rp.read_text()),
                    "source": "branch",
                }
                continue
        merge_base = (
            provision._run(
                ["git", "-C", str(wt), "merge-base", "HEAD", base_ref], check=False
            ).stdout.strip()
            or base_ref
        )
        committed = provision._run(
            ["git", "-C", str(wt), "--no-pager", "diff", merge_base, "HEAD"],
            check=False,
        ).stdout
        uncommitted = provision._run(
            ["git", "-C", str(wt), "--no-pager", "diff", "HEAD"],
            check=False,
        ).stdout
        # Capture the candidate's real delta without letting stale origin/<base>
        # drift expand the patch to the whole repository.
        d = _join_diffs(committed, uncommitted)
        if not d.strip() and agent == "gemini":
            d = _gemini_scratch_diff(edir / exp_log_path(agent, artifact_member), base)
        p = edir / exp_diff_path(agent, artifact_member)
        p.write_text(d)
        written[member["member_id"]] = {
            "path": str(p),
            "bytes": len(d),
            "arm_id": member.get("arm_id"),
            "member_id": member["member_id"],
            "agent": agent,
            "profile_id": member.get("profile_id"),
        }
    return {"exp_id": exp_id, "diffs": written}


def _gemini_scratch_diff(log: Path, base: str) -> str:
    """Recover diffs when Antigravity reports success from its scratch clone.

    The prompt and adapter direct Gemini to the assigned worktree, but agy can
    still decide to clone into its writable scratch area. If that happens, the
    committed implementation is real evidence; recover it instead of recording
    a hollow zero-byte candidate.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    candidates: list[Path] = []
    for match in re.finditer(r"(/Users/[^\s`]+/antigravity-cli/scratch/[^\s`]+)", text):
        path = Path(match.group(1).rstrip(".,)"))
        if path not in candidates:
            candidates.append(path)
    for path in candidates:
        if not (path / ".git").exists():
            continue
        d = provision._run(
            ["git", "-C", str(path), "--no-pager", "diff", f"origin/{base}", "HEAD"],
            check=False,
        ).stdout
        if d.strip():
            return d
    return ""


def _extract_json(text: str) -> dict | None:
    """Agents are told STRICT JSON but may wrap it in prose/markdown. Find the JSON object that
    actually contains "scores" — scan every balanced {...} from the end (the final answer).
    """
    if not text:
        return None
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for s in reversed(starts):
        depth = 0
        for i in range(s, len(text)):
            depth += 1 if text[i] == "{" else (-1 if text[i] == "}" else 0)
            if depth == 0:
                try:
                    obj = json.loads(text[s : i + 1])
                    if isinstance(obj, dict) and "scores" in obj:
                        return obj
                except Exception:
                    pass
                break
    return None


def _extract_evidence_gaps(parsed: dict | None) -> list[str]:
    """Normalize evaluator-reported missing evidence into durable schema-growth rows."""
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("evidence_gaps")
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    gaps: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            text = value.get("gap") or value.get("missing") or value.get("evidence") or ""
        else:
            text = str(value)
        text = " ".join(text.strip().split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        gaps.append(text[:500])
    return gaps


def _extract_cited_evidence_types(parsed: dict | None) -> list[str]:
    if not isinstance(parsed, dict):
        return []
    return feedback.normalize_evidence_type_citations(parsed.get("cited_evidence_types"))


def _eval_command(agent: str, promptfile: str) -> str:
    """Bash command running one evaluator with the (large) prompt read from a file via
    "$(cat ...)" — shell substitution avoids embedding 270KB+ in an argv we build in Python.
    """
    path = 'export PATH="/opt/homebrew/bin:$HOME/.local/bin:$HOME/.cursor/bin:$PATH"'
    prelude = dispatcher._agent_runtime_prelude(agent) + dispatcher._auth_prelude(agent)
    P = f'"$(cat {shlex.quote(promptfile)})"'
    codex_sandbox_args = (
        "--dangerously-bypass-approvals-and-sandbox"
        if adapters.codex_bypass_inner_sandbox()
        else "--sandbox read-only"
    )
    gemini_dir = os.environ.get(
        "ORCH_GEMINI_DIR",
        str(adapters.AGENT_RUNTIME / "gemini" / ".gemini"),
    )
    gemini_log_file = os.environ.get(
        "ORCH_GEMINI_LOG_FILE",
        str(adapters.AGENT_RUNTIME / "gemini" / "logs" / "agy-eval.log"),
    )
    gemini_model = adapters.gemini_model()
    gemini_model_arg = f" --model {shlex.quote(gemini_model)}" if gemini_model else ""
    gemini_add_dir = shlex.quote(str(ORCH.resolve()))
    cmds = {
        "claude": f"claude -p {P} --dangerously-skip-permissions",
        "codex": f"codex exec --skip-git-repo-check {codex_sandbox_args} {P}",
        "cursor": f"cursor-agent -p {P} --force --output-format text --trust --workspace .",
        "gemini": f"agy -p {P} --dangerously-skip-permissions --add-dir {gemini_add_dir} --print-timeout 40m",
        "vibe": f"vibe --prompt {P} --auto-approve --output text --trust",
    }
    cmds["gemini"] = (
        f"agy --gemini_dir {shlex.quote(gemini_dir)}{gemini_model_arg} "
        f"--print {P} --dangerously-skip-permissions --add-dir {gemini_add_dir} "
        f"--print-timeout 40m --log-file {shlex.quote(gemini_log_file)}"
    )
    return f"{path}; ({prelude}{cmds[agent]})"


def _capacity_map() -> dict:
    """Best-effort agent->capacity row from the tick's capacity.json artifact (hourly). Used only
    to PREFER a drain-mode seat as the neutral judge — its expiring window makes the extra
    evaluation effectively free. Absence/staleness degrades to static EVALUATOR_TOPUP_ORDER."""
    path = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff")) / "capacity.json"
    try:
        if path.exists() and time.time() - path.stat().st_mtime < 7200:
            agents = (json.loads(path.read_text()) or {}).get("agents")
            if isinstance(agents, dict):
                return agents
    except Exception:
        pass
    return {}


def _ensure_min_evaluators(
    evaluators: list[str],
    minimum: int = MIN_EVALUATORS,
    *,
    implementers: list[str] | None = None,
    capacity: dict | None = None,
) -> list[str]:
    """Guarantee >=`minimum` evaluators (capped at the available roster) by topping up from
    EVALUATOR_TOPUP_ORDER. Judges added beyond the caller's set are by construction NOT among the
    default implementer-judges, so they are NEUTRAL — directly limiting self-favoring at small N.

    12c (2026-07-08): ALSO guarantee >=1 neutral judge whenever `implementers` is known — a 4-arm
    experiment satisfies `minimum` with implementers alone and previously fielded ONLY
    self-interested judges (observed live as 9/10 score spreads with visible self-preference).
    Among eligible neutrals, drain-mode capacity is preferred (expiring quota = free referee).
    Pure; selftested (capacity injected)."""
    out = list(dict.fromkeys(evaluators))  # dedupe, preserve caller order
    for a in EVALUATOR_TOPUP_ORDER:
        if len(out) >= minimum:
            break
        if a not in out:
            out.append(a)
    impl = set(implementers or ())
    if impl and all(a in impl for a in out):
        pool = [a for a in EVALUATOR_TOPUP_ORDER if a not in impl and a not in out]
        if pool:
            cap = capacity or {}

            def _drain_rank(agent: str) -> tuple:
                row = cap.get(agent) or {}
                is_drain = str(row.get("policy") or "") == "drain"
                return (0 if is_drain else 1, EVALUATOR_TOPUP_ORDER.index(agent))

            pool.sort(key=_drain_rank)
            out.append(pool[0])
    return out


def _normalize_evaluator_specs(
    requested: list[Any] | None,
    *,
    default_agents: list[str],
    implementer_agents: list[str],
) -> list[dict[str, Any]]:
    raw = list(requested or default_agents)
    explicit: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            spec = {"agent": item}
        elif isinstance(item, dict):
            spec = dict(item)
        else:
            raise ValueError(f"invalid evaluator: {item!r}")
        agent = str(spec.get("agent") or "").strip()
        if not agent:
            raise ValueError("evaluator requires agent")
        evaluator_id = str(
            spec.get("evaluator_id") or spec.get("profile_id") or spec.get("arm_id") or agent
        ).strip()
        spec.update({"agent": agent, "evaluator_id": evaluator_id})
        explicit.append(spec)
    topped_agents = _ensure_min_evaluators(
        [spec["agent"] for spec in explicit],
        implementers=implementer_agents,
        capacity=_capacity_map(),
    )
    represented = {spec["agent"] for spec in explicit}
    explicit.extend(
        {"agent": agent, "evaluator_id": agent}
        for agent in topped_agents
        if agent not in represented
    )
    ids = [spec["evaluator_id"] for spec in explicit]
    if len(ids) != len(set(ids)):
        raise ValueError("evaluator identities must be unique")
    return explicit


def _eval_artifact_token(evaluator_id: str) -> str:
    return _identity_slug(evaluator_id)


def evaluate(
    repo: str,
    spec_file: str,
    exp_id: str,
    evaluators: list[Any] | None = None,
    timeout: int = 1500,
) -> dict:
    """Anonymized cross-evaluation with PER-JUDGE RANDOMIZED candidate order (defeats positional bias —
    run-1 used one fixed order for all judges). Each judge gets its OWN letter->agent map; scores are
    recorded agent-keyed in feedback.py. Evaluators run CONCURRENTLY. Returns the per-judge maps.
    """
    edir = exp_paths(exp_id)
    meta = json.loads((edir / "meta.json").read_text())
    spec = Path(spec_file).read_text()
    members = experiment_members(meta)
    member_by_id = {member["member_id"]: member for member in members}
    implementers = []
    diffs = {}
    for member in members:
        artifact_member = None if member["legacy"] else member["member_id"]
        path = edir / exp_diff_path(member["agent"], artifact_member)
        if path.exists() and path.stat().st_size > 0:
            implementers.append(member["member_id"])
            diffs[member["member_id"]] = _capped_diff(path.read_text())
    evaluator_specs = _normalize_evaluator_specs(
        evaluators,
        default_agents=list(dict.fromkeys(member["agent"] for member in members)),
        implementer_agents=list(dict.fromkeys(member["agent"] for member in members)),
    )
    maps, procs = {}, {}
    for evaluator in evaluator_specs:  # launch all evaluators at once
        ev = evaluator["evaluator_id"]
        agent = evaluator["agent"]
        token = _eval_artifact_token(ev)
        seed = int(hashlib.md5(f"{exp_id}:{ev}".encode()).hexdigest()[:8], 16)
        order = implementers[:]
        random.Random(seed).shuffle(order)  # DISTINCT order per judge
        letters_ev = {chr(65 + i): a for i, a in enumerate(order)}
        maps[ev] = letters_ev
        cand = {L: diffs[a] for L, a in letters_ev.items()}
        pf = edir / f"eval-prompt-{token}.txt"
        pf.write_text(evaluate_prompt(spec, cand))
        out_path = edir / f"eval-out-{token}.txt"
        out = out_path.open("w")
        mode = AGENT_MODE.get(agent, "full")
        run_id = f"{exp_id}:eval:{ev}"
        target = f"{repo} [exp {exp_id} eval]"
        feedback.record_run(
            run_id,
            target,
            "review",
            agent,
            mode=mode,
            reasoning_level=mode,
            experiment_id=exp_id,
            model=adapters.model_identity(agent, mode),
            rationale="A/B/C/D experiment cross-evaluator capacity run",
            routing_metadata={
                "experiment_identity_version": 2 if meta.get("schema_version") == 2 else 1,
                "evaluator_id": ev,
                "evaluator_arm_id": evaluator.get("arm_id"),
                "profile_id": evaluator.get("profile_id"),
            },
        )
        started_ts = _record_execution_start(agent, mode, run_id, target, "review", out_path)
        out.write(
            f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} EXP-EVAL "
            f"{agent}/{mode} evaluator_id={ev} [review] exp={exp_id} run_id={run_id} ===\n"
        )
        out.flush()
        procs[ev] = (
            subprocess.Popen(
                ["bash", "-lc", _eval_command(agent, str(pf))],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            ),
            out,
            run_id,
            started_ts,
            mode,
            target,
            out_path,
            evaluator,
        )
    (edir / "eval-maps.json").write_text(json.dumps(maps, indent=2))  # per-judge secret maps
    matrix = {}
    legacy_scores: dict[tuple[str, str], list[float]] = {}
    legacy_verdicts: dict[tuple[str, str], list[dict]] = {}

    def finalize_evaluator(ev: str, ctx: tuple) -> None:
        p, out, run_id, started_ts, mode, target, out_path, evaluator = ctx
        evaluator_agent = evaluator["agent"]
        _record_execution_complete(
            evaluator_agent, mode, run_id, target, "review", out_path, started_ts
        )
        out.close()
        parsed = _extract_json(out_path.read_text(errors="replace"))
        matrix[ev] = parsed
        for gap in _extract_evidence_gaps(parsed):
            feedback.record_evidence_gap(f"{exp_id}:eval", ev, gap)
        cited = feedback.record_evidence_type_citations(_extract_cited_evidence_types(parsed))
        if parsed and isinstance(parsed.get("scores"), dict):
            for L, score in parsed["scores"].items():
                impl_id = maps[ev].get(L.strip().upper()[:1])  # this judge's own map
                if impl_id is None:
                    continue
                try:
                    sc = float(score)
                except Exception:
                    continue
                impl = member_by_id[impl_id]
                verdict = {
                    "letter": L,
                    "candidate_id": impl_id,
                    "note": (parsed.get("notes") or {}).get(L),
                    "cited_evidence_types": cited,
                }
                legacy_key = (impl["agent"], evaluator_agent)
                legacy_scores.setdefault(legacy_key, []).append(sc)
                legacy_verdicts.setdefault(legacy_key, []).append(verdict)
                if not impl["legacy"]:
                    feedback.record_evaluation_v2(
                        experiment_id=exp_id,
                        implementer_arm_id=impl["arm_id"],
                        implementer_member_id=impl["member_id"],
                        implementer_profile_id=impl.get("profile_id"),
                        implementation_agent=impl["agent"],
                        evaluator_id=ev,
                        evaluator_arm_id=evaluator.get("arm_id"),
                        evaluator_profile_id=evaluator.get("profile_id"),
                        evaluator_agent=evaluator_agent,
                        score=sc,
                        verdict=verdict,
                    )

    pending = dict(procs)
    deadlines = {ev: time.time() + timeout for ev in pending}

    def still_running(proc) -> bool:
        poll = getattr(proc, "poll", None)
        if poll is not None:
            return poll() is None
        try:
            proc.wait(timeout=0)
            return False
        except subprocess.TimeoutExpired:
            return True
        except TypeError:
            proc.wait()
            return False

    while pending:
        progressed = False
        now = time.time()
        for ev, ctx in list(pending.items()):
            p = ctx[0]
            running = still_running(p)
            if running and now < deadlines[ev]:
                continue
            if running:
                p.kill()
                try:
                    p.wait(timeout=5)
                except Exception:
                    pass
            else:
                try:
                    p.wait(timeout=0)
                except Exception:
                    pass
            finalize_evaluator(ev, ctx)
            del pending[ev]
            progressed = True
        if pending and not progressed:
            next_deadline = min(deadlines[ev] for ev in pending)
            time.sleep(max(0.1, min(1.0, next_deadline - time.time())))
    # Keep the existing agent-level learner populated without pretending that
    # multiple exact arms are one causal observation: dual-write their mean as
    # the parent-level compatibility projection.
    for (impl_agent, evaluator_agent), scores in legacy_scores.items():
        feedback.record_evaluation(
            exp_id,
            impl_agent,
            evaluator_agent,
            sum(scores) / len(scores),
            verdict={
                "identity": "agent_parent_projection",
                "exact_observation_count": len(scores),
                "observations": legacy_verdicts[(impl_agent, evaluator_agent)],
            },
        )
    # 12b: record machine-ground-truth anchors alongside the judge scores (decisive arms only;
    # time-budgeted). Anchor plumbing must never break the eval phase itself.
    objective = None
    if os.environ.get("ORCH_OBJECTIVE_ANCHOR", "1").strip().lower() not in (
        "0",
        "false",
        "off",
    ):
        try:
            import objective_anchor

            objective = objective_anchor.anchor_experiment(exp_id)
        except Exception as exc:
            objective = {"error": str(exc)[:200]}
    return {
        "exp_id": exp_id,
        "maps": maps,
        "implementers": implementers,
        "evaluators": [spec["evaluator_id"] for spec in evaluator_specs],
        "parsed_ok": {ev: bool(m) for ev, m in matrix.items()},
        "objective_anchors": objective,
    }


def followup(
    *,
    max_experiments: int = 1,
    min_idle_s: int = 900,
    # ONE window, defined in research_subjects and consumed here — see EVALUABLE_WINDOW_DAYS.
    # Hardcoding 14 here while the cap counted over all time is what deadlocked the arm.
    max_age_days: int = research_subjects.EVALUABLE_WINDOW_DAYS,
    eval_timeout: int = 600,
    collect_fn=None,
    evaluate_fn=None,
    synthesize_fn=None,
    subject_lifecycle_fn=None,
    promotion_reconcile_fn=None,
    promotion_completion_fn=None,
    promotion_resume_fn=None,
    promotion_verify_fn=None,
    promotion_outcome_lookup_fn=None,
    promotion_mirror_fn=None,
) -> dict:
    """Drive the UNFINISHED half of the experiment lifecycle (2026-07-08). The tick LAUNCHES
    A/B/C experiments, but nothing ever ran collect/evaluate on them — ZERO tick-* evaluation
    rows existed when this landed (all 310 judge rows came from manual/backfill campaigns), so
    the research arm burned implementer capacity and returned no learning signal at all.

    Eligible = has meta.json + spec.md, no eval-maps.json, no followup-skip.json, younger than
    `max_age_days`, and every agent log idle >= `min_idle_s` (finished or dead — either way
    there is nothing left to wait for). Runs collect + evaluate (judges bounded by
    `eval_timeout`) on at most `max_experiments` per call so a tick never runs away.
    Experiments whose collect recovers zero non-empty diffs are stamped followup-skip.json
    (worktrees GC'd before collection — evidence lost, do not rescan forever). Judge scores AND
    objective anchors (12b) both record through evaluate(). Evaluated experiments
    then enter the resumable synthesis-promotion lifecycle; a daily ship-gate
    stamp is written only after a verified candidate or terminal discard exists,
    never merely because an asynchronous synthesis launched."""
    now = time.time()
    collect_fn = collect_fn or collect
    evaluate_fn = evaluate_fn or evaluate
    subject_lifecycle_fn = subject_lifecycle_fn or research_subjects.mark_lifecycle
    out: dict = {"processed": [], "skipped": [], "promotions": [], "eligible": 0}
    if not EXP_DIR.exists():
        return out
    dirs = sorted(
        (d for d in EXP_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    promotion_reconcile = promotion_reconcile_fn or synthesis_promotion.reconcile
    gate_stamp = EXP_DIR / ".last-ship-gate"
    stamp_fresh = gate_stamp.exists() and (now - gate_stamp.stat().st_mtime) < 86400
    launch_available = not stamp_fresh
    promotion_inflight = False

    def persist_terminal_checkpoint(edir: Path, state: dict) -> None:
        phase = state.get("delivery_phase")
        if phase not in {"candidate_ready", "discarded", "durable"}:
            return
        verdict = (
            "use" if phase == "candidate_ready" else "durable" if phase == "durable" else "discard"
        )
        payload = {
            "ts": int(now),
            "verdict": verdict,
            "delivery_phase": phase,
            "canonical_state": state.get("canonical_state"),
            "candidate_id": (state.get("candidate") or {}).get("candidate_id"),
            "reason": ((state.get("phase_history") or [{}])[-1]).get("reason"),
            "promotion_state": str(synthesis_promotion.state_path(edir)),
        }
        (edir / "ship-gate.json").write_text(json.dumps(payload, indent=2) + "\n")
        gate_stamp.touch()

    # Reconcile previously launched/evaluated promotions before discovering new
    # experiments. This is the resume path that the old eval-maps skip omitted.
    for edir in dirs:
        state = synthesis_promotion.load_state(edir)
        if state is None:
            continue
        phase_before = state.get("delivery_phase")
        launch_fn = None
        if phase_before == "evaluated" and launch_available and not promotion_inflight:
            meta = json.loads((edir / "meta.json").read_text())
            launch_fn = lambda repo=meta["repo"], exp_id=edir.name: (
                (synthesize_fn or synthesize)(repo, exp_id)
            )
        try:
            promotion = promotion_reconcile(
                edir,
                launch_fn=launch_fn,
                completion_fn=promotion_completion_fn,
                resume_fn=promotion_resume_fn or _resume_synthesis_promotion,
                verify_fn=promotion_verify_fn,
                outcome_lookup_fn=promotion_outcome_lookup_fn,
                mirror_fn=promotion_mirror_fn,
            )
            state = promotion["state"]
            persist_terminal_checkpoint(edir, state)
            actions = promotion.get("actions") or []
            if "synthesis_launched" in actions:
                launch_available = False
            phase_after = state.get("delivery_phase")
            if phase_after not in synthesis_promotion.TERMINAL_PHASES:
                promotion_inflight = True
                launch_available = False
            out["promotions"].append(
                {
                    "exp_id": edir.name,
                    "phase_before": phase_before,
                    "delivery_phase": phase_after,
                    "canonical_state": state.get("canonical_state"),
                    "actions": actions,
                    "candidate_id": (state.get("candidate") or {}).get("candidate_id"),
                }
            )
        except Exception as exc:
            out["promotions"].append(
                {"exp_id": edir.name, "phase_before": phase_before, "error": str(exc)[:256]}
            )
            promotion_inflight = True
            launch_available = False
    for edir in dirs:
        meta_p, spec_p = edir / "meta.json", edir / "spec.md"
        if not meta_p.exists() or not spec_p.exists():
            continue
        if (edir / "eval-maps.json").exists() or (edir / "followup-skip.json").exists():
            continue
        if (now - meta_p.stat().st_mtime) / 86400.0 > max_age_days:
            continue
        try:
            meta = json.loads(meta_p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # ELIGIBILITY IS PER MEMBER, NOT PER AGENT. `prepare_arms` writes one log per MEMBER
        # (`<member_id>.log`), so reading `meta["agents"]` looked for `<agent>.log`, never found it,
        # and every v2 experiment fell out of this loop before it could be collected -- producing
        # zero `evaluations_v2` rows, which is the exact gap the v2 identity work set out to close.
        # `experiment_members` keeps the legacy shape too: a legacy member's id IS its agent, so
        # `exp_log_path(agent, None)` still resolves to `<agent>.log` for recovered experiments.
        try:
            members = experiment_members(meta)
        except ValueError:
            continue  # malformed member metadata: skip the experiment, never guess its identity
        logs = [
            edir / exp_log_path(m["agent"], None if m["legacy"] else m["member_id"])
            for m in members
        ]
        if not logs or not all(log.exists() for log in logs):
            continue
        if any(now - log.stat().st_mtime < min_idle_s for log in logs):
            out["skipped"].append({"exp_id": edir.name, "reason": "still-running"})
            continue
        out["eligible"] += 1
        capabilities.production_heartbeat(
            "abcd-experiment",
            "match",
            ref=edir.name,
            metadata={"phase": "followup", "repo": meta.get("repo")},
        )
        try:
            subject_lifecycle_fn(edir.name, "evaluable", reason="all_arm_logs_idle")
        except Exception:
            pass  # Subject telemetry must never break experiment recovery.
        if len(out["processed"]) >= max_experiments:
            out["skipped"].append({"exp_id": edir.name, "reason": "per-call-cap"})
            continue
        repo = meta["repo"]
        capabilities.production_heartbeat(
            "abcd-experiment",
            "invocation",
            ref=edir.name,
            metadata={"phase": "collect-evaluate", "repo": repo},
        )
        collected = collect_fn(repo, edir.name)
        nonempty = sum(1 for v in (collected.get("diffs") or {}).values() if v.get("bytes"))
        if not nonempty:
            (edir / "followup-skip.json").write_text(
                json.dumps({"reason": "no-diffs-recoverable", "ts": int(now)})
            )
            out["processed"].append({"exp_id": edir.name, "diffs": 0, "evaluated": False})
            try:
                subject_lifecycle_fn(edir.name, "failed", reason="no_diffs_recoverable")
            except Exception:
                pass
        else:
            ev = evaluate_fn(repo, str(spec_p), edir.name, None, timeout=eval_timeout)
            try:
                subject_lifecycle_fn(edir.name, "evaluated", reason="followup_evaluation_complete")
            except Exception:
                pass
            entry = {
                "exp_id": edir.name,
                "diffs": nonempty,
                "evaluated": True,
                "evaluators": ev.get("evaluators"),
                "objective_anchors": ev.get("objective_anchors"),
            }
            # Verified synthesis promotion: persist evaluated state before launching
            # asynchronous synthesis. Followup will poll/resume/verify on later ticks;
            # no issue/PR is published here and the daily stamp waits for a verified
            # candidate or terminal discard.
            if os.environ.get("ORCH_FOLLOWUP_SHIP_GATE", "1").strip().lower() not in (
                "0",
                "false",
                "off",
            ):
                try:
                    synthesis_promotion.ensure_evaluated_state(edir, meta=meta, now=int(now))
                    if launch_available and not promotion_inflight:
                        promotion = promotion_reconcile(
                            edir,
                            launch_fn=lambda repo=repo, exp_id=edir.name: (
                                (synthesize_fn or synthesize)(repo, exp_id)
                            ),
                            completion_fn=promotion_completion_fn,
                            resume_fn=promotion_resume_fn or _resume_synthesis_promotion,
                            verify_fn=promotion_verify_fn,
                            outcome_lookup_fn=promotion_outcome_lookup_fn,
                            mirror_fn=promotion_mirror_fn,
                        )
                        promotion_state = promotion["state"]
                        persist_terminal_checkpoint(edir, promotion_state)
                        entry["ship_gate"] = promotion_state["delivery_phase"]
                        entry["promotion_actions"] = promotion.get("actions") or []
                        launch_available = False
                        if (
                            promotion_state["delivery_phase"]
                            not in synthesis_promotion.TERMINAL_PHASES
                        ):
                            promotion_inflight = True
                    else:
                        entry["ship_gate"] = "queued_evaluated"
                except Exception as exc:
                    entry["ship_gate"] = f"error: {str(exc)[:120]}"
            out["processed"].append(entry)
            capabilities.production_heartbeat(
                "abcd-experiment",
                "success",
                ref=edir.name,
                metadata={"evaluators": ev.get("evaluators"), "diffs": nonempty},
            )
            capabilities.production_heartbeat(
                "abcd-experiment",
                "outcome",
                ref=f"experiment:{edir.name}",
                metadata={"sink": "feedback.evaluations_v2"},
            )
        # item 11: event-driven redirect-corpus intake — failed arms (killed markers / no diff)
        # are the failure-shaped material the hourly sweep never sees (watched=0 at tick time).
        # Same env gate as the sweep's corpus recording; never breaks the followup itself.
        if os.environ.get("ORCH_REDIRECT_SWEEP_RECORD_CORPUS", "0") == "1":
            try:
                import redirect_sweep

                corpus_res = redirect_sweep.record_experiment_candidates(edir.name, meta, edir)
                out["processed"][-1]["redirect_corpus"] = {
                    "recorded": corpus_res.get("recorded_count", 0),
                    "actionable": corpus_res.get("experiment_actionable", 0),
                }
            except Exception as exc:
                out["processed"][-1]["redirect_corpus"] = {"error": str(exc)[:200]}
    return out


def _winner_and_harvest(
    verdicts: dict,
    maps: dict,
    reliability: dict | None = None,
    fallback_exclude_judges=("gemini",),
) -> dict:
    """Compute weighted ranking and harvest material from per-judge verdicts.

    Reliability-ready judges use data-driven weights. Not-ready judges are neutral,
    except the known legacy Gemini exclusion remains as a fallback until enough
    evaluator evidence exists to override it. Scores are for human reporting and
    synthesis context only, not a ship gate.
    """
    active_judges = [j for j in verdicts if verdicts.get(j)]
    if reliability is None:
        try:
            reliability = judge_reliability.summarize()
        except Exception:
            reliability = {"judges": {}}
    weights = judge_reliability.weights_from_summary(
        reliability,
        evaluators=active_judges,
        fallback_exclude_judges=fallback_exclude_judges,
    )
    judges = [j for j in active_judges if weights.get(j, 0.0) > 0.0]
    if not judges:
        raise ValueError("no eligible evaluator scores after reliability fallback")
    agents = sorted({a for m in maps.values() for a in m.values()})
    means, notes = {}, {}
    for agent in agents:
        scs, ns = [], []
        for j in judges:
            L = next((l for l, a in maps.get(j, {}).items() if a == agent), None)
            if L and L in verdicts[j].get("scores", {}):
                scs.append((float(verdicts[j]["scores"][L]), weights.get(j, 1.0)))
                note = (verdicts[j].get("notes") or {}).get(L)
                if note:
                    ns.append(f"{j}: {note}")
        denom = sum(w for _, w in scs)
        means[agent] = (sum(score * w for score, w in scs) / denom) if denom else 0.0
        notes[agent] = ns
    winner = max(means, key=means.get)
    return {
        "winner": winner,
        "winner_mean": means[winner],
        "means": means,
        "ranking": sorted(means.items(), key=lambda kv: -kv[1]),
        "winner_weaknesses": notes[winner],
        "alt_strengths": {a: notes[a] for a in means if a != winner},
        "judge_weights": {j: weights.get(j, 0.0) for j in active_judges},
        "reliability_ready_judges": (reliability or {}).get("ready_judge_count", 0),
    }


def _gate_prompt(ranking: list, notes_by_agent: dict) -> str:
    """The ship/discard decision is a JUDGMENT, not a score cutoff (scores are for humans). A strong
    reasoning agent — reading the actual code in the winner's worktree — decides whether the best approach
    is a productive starting point, and whether divergence makes base-choice high-stakes.
    """
    summary = "; ".join(f"{a}={m:.1f}" for a, m in ranking)
    notes = (
        "\n".join(f"- {a}: {'; '.join(ns)}" for a, ns in notes_by_agent.items() if ns) or "- (none)"
    )
    return (
        "You are deciding whether an N-way experiment's best implementation is worth BUILDING ON or should "
        "be discarded. You are in the top-ranked implementation's worktree — READ THE ACTUAL CODE. A review "
        f"panel scored the approaches (higher=better, for context only): {summary}. Panel notes per approach:\n"
        f"{notes}\n\nDecide with JUDGMENT, not a number cutoff:\n"
        "1. Is the best approach a PRODUCTIVE STARTING POINT (mostly the right direction / substantially "
        "accurate, so finishing it is efficient), or are its weaknesses serious enough that building on it "
        "would be inefficient and low-quality long-term (better to discard and restart)?\n"
        "2. Do the approaches DIVERGE so much that choosing the wrong base would turn implementation into a "
        "digression? If so, which base avoids that?\n\n"
        'Return STRICT JSON only: {"decision":"use"|"discard","base":"<agent name or empty>",'
        '"divergence_risk":"low"|"medium"|"high","rationale":"<1-2 sentences>"}'
    )


def usefulness_gate(repo: str, exp_id: str, harvest: dict, judge_agent: str = "codex") -> dict:
    """Delegate the ship/discard judgment to a strong reasoning agent reading the winner's actual code."""
    notes_by_agent = {
        harvest["winner"]: harvest["winner_weaknesses"],
        **harvest["alt_strengths"],
    }
    meta = json.loads((exp_paths(exp_id) / "meta.json").read_text())
    members = {member["member_id"]: member for member in experiment_members(meta)}
    winner_member = members.get(harvest["winner"])
    if winner_member is None:
        raise ValueError(f"winner identity missing from experiment metadata: {harvest['winner']}")
    artifact_member = None if winner_member["legacy"] else winner_member["member_id"]
    wt = exp_worktree(repo, exp_id, winner_member["agent"], artifact_member)
    mode = "assess" if judge_agent == "codex" else "full"  # codex assess = read-only sandbox
    out = dispatcher.offload(
        judge_agent,
        _gate_prompt(harvest["ranking"], notes_by_agent),
        cwd=str(wt),
        mode=mode,
        timeout=900,
    )
    parsed = _extract_json(out.get("output", "")) or {}
    return {
        "decision": parsed.get("decision", "use"),
        "base": parsed.get("base") or harvest["winner"],
        "divergence_risk": parsed.get("divergence_risk"),
        "rationale": parsed.get("rationale"),
        "judge": judge_agent,
    }


def _synthesis_prompt(h: dict, base: str | None = None) -> str:
    strengths = (
        "\n".join(f"- {n}" for a in h["alt_strengths"] for n in h["alt_strengths"][a])
        or "- (none noted)"
    )
    weaknesses = "\n".join(f"- {n}" for n in h["winner_weaknesses"]) or "- (none noted)"
    return (
        "An independent review panel selected YOUR implementation in this worktree as the strongest base to "
        "build on. Make it the strongest possible version by working "
        "SURGICALLY on what's already here: (a) fix the weaknesses reviewers noted in your implementation, "
        "and (b) adopt the specific strengths reviewers found in ALTERNATIVE implementations. Keep your "
        "existing structure; make minimal, targeted additions — do NOT rewrite wholesale. Then `git add -A` "
        "and `git commit`; do NOT push or open a PR.\n\n"
        f"WEAKNESSES TO FIX in your implementation:\n{weaknesses}\n\n"
        f"STRENGTHS TO ADOPT from alternative implementations:\n{strengths}\n"
    )


def _resume_synthesis_promotion(state: dict) -> dict:
    """Resume one interrupted synthesis attempt in its existing isolated worktree.

    The durable promotion state supplies the exact agent/worktree lineage. A new
    run id preserves attempt identity; the original synthesis run remains the
    outcome anchor. This never pushes or opens a PR.
    """
    synthesis = state.get("synthesis") or {}
    agent = str(synthesis.get("synth_agent") or "").strip()
    worktree = Path(str(synthesis.get("worktree") or ""))
    if not agent or not worktree.exists():
        return {"blocked": True, "reason": "resume lacks synthesis agent/worktree"}
    ordinal = len(synthesis.get("resume_history") or []) + 1
    run_id = f"{state['experiment_id']}:synth:resume:{ordinal}"
    log = Path(str(synthesis.get("log") or exp_paths(state["experiment_id"])))
    log = log.parent / f"synth-resume-{ordinal}-{_identity_slug(agent)}.log"
    mode = AGENT_MODE.get(agent, "full")
    target = f"{state.get('repo')} [exp {state['experiment_id']} synth resume {ordinal}]"
    feedback.record_run(
        run_id,
        target,
        "synthesize",
        agent,
        mode=mode,
        experiment_id=state["experiment_id"],
        model=adapters.model_identity(agent, mode),
        rationale="resume interrupted verified-synthesis attempt in the same isolated worktree",
        routing_metadata={
            "promotion_resume": True,
            "root_synthesis_run_id": synthesis.get("root_run_id"),
            "resume_ordinal": ordinal,
        },
    )
    prompt = (
        "Resume the interrupted experiment synthesis in this existing worktree. Inspect git status, "
        "the current diff, and prior commits before editing. Complete the reviewer-guided synthesis "
        "already underway, run the relevant local checks, and commit the finished result. Do not "
        "push, open an issue/PR, merge, or broaden scope."
    )
    pid = _spawn(
        agent,
        mode,
        prompt,
        worktree,
        log,
        run_id=run_id,
        target=target,
        task_type="synthesize",
    )
    return {"pid": pid, "run_id": run_id, "log": str(log)}


def synthesize(
    repo: str,
    exp_id: str,
    judge_agent: str = "codex",
    synth_agent: str | None = None,
    gate: dict | None = None,
    reliability: dict | None = None,
) -> dict:
    """Harvest the best of all approaches into the chosen base's worktree and commit it — the experiment's
    shippable deliverable. Ship/discard is a delegated JUDGMENT (usefulness_gate), not a score cutoff;
    discard only if a strong reasoning agent judges no approach a productive starting point.

    Tranche 0 lane B: Now iterates evaluator keys persisted in eval-maps.json, including neutral top-up judges.
    """
    edir = exp_paths(exp_id)
    launch_artifact = edir / "synthesis-launch.json"
    if launch_artifact.exists():
        try:
            prior_launch = json.loads(launch_artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior_launch = {}
        if (
            prior_launch.get("status") == "launched"
            and prior_launch.get("experiment_id") == exp_id
            and prior_launch.get("repo") == repo
            and prior_launch.get("pid")
            and prior_launch.get("run_id")
        ):
            return {
                key: prior_launch.get(key)
                for key in (
                    "base",
                    "synth_agent",
                    "run_id",
                    "pid",
                    "worktree",
                    "log",
                    "ranking",
                    "gate",
                )
            } | {"recovered_launch": True}
    maps = json.loads((edir / "eval-maps.json").read_text())
    meta = json.loads((edir / "meta.json").read_text())

    # eval-maps.json is the durable list of actual judges.  In particular, it
    # contains neutral top-ups that are intentionally absent from meta['agents'].
    verdicts = {}
    for ev in maps:
        f = edir / f"eval-out-{_eval_artifact_token(ev)}.txt"
        if f.exists():
            verdicts[ev] = _extract_json(f.read_text(errors="replace"))
    try:
        h = _winner_and_harvest(verdicts, maps, reliability=reliability)
    except ValueError as exc:
        return {"blocked": True, "reason": str(exc), "ranking": []}
    if gate is None:
        gate = usefulness_gate(repo, exp_id, h, judge_agent)
    if gate.get("decision") != "use":
        return {
            "discard": True,
            "gate": gate,
            "ranking": h["ranking"],
        }  # judged not a productive base
    base = gate.get("base") or h["winner"]
    members = {member["member_id"]: member for member in experiment_members(meta)}
    base_member = members.get(base)
    if base_member is None:
        # Legacy gates historically returned the agent.  That remains valid only
        # when it resolves to exactly one candidate; same-agent v2 arms are never
        # guessed or collapsed.
        matches = [member for member in members.values() if member["agent"] == base]
        if len(matches) == 1:
            base_member = matches[0]
            base = base_member["member_id"]
        else:
            return {
                "blocked": True,
                "reason": f"ambiguous synthesis base: {base}",
                "ranking": h["ranking"],
            }
    synth_agent = synth_agent or base_member["agent"]
    artifact_member = None if base_member["legacy"] else base_member["member_id"]
    wt = exp_worktree(repo, exp_id, base_member["agent"], artifact_member)
    log = (
        edir
        / f"synth-{_artifact_identity(base_member['agent'], artifact_member)}-{_identity_slug(synth_agent)}.log"
    )
    mode = AGENT_MODE.get(synth_agent, "full")
    run_id = (
        f"{exp_id}:synth" if base_member["legacy"] else f"{exp_id}:synth:{base_member['arm_id']}"
    )
    target = f"{repo} [exp {exp_id} synth]"
    launch_record = {
        "schema_version": 1,
        "status": "prepared",
        "experiment_id": exp_id,
        "repo": repo,
        "base": base,
        "synth_agent": synth_agent,
        "run_id": run_id,
        "worktree": str(wt),
        "log": str(log),
        "ranking": h["ranking"],
        "gate": gate,
        "prepared_ts": int(time.time()),
        "direct_publication_allowed": False,
    }
    synthesis_promotion._atomic_json(launch_artifact, launch_record)
    feedback.record_run(
        run_id,
        target,
        "synthesize",
        synth_agent,
        mode=mode,
        experiment_id=exp_id,
        model=adapters.model_identity(synth_agent, mode),
        rationale=f"harvest panel-named strengths into base ({base}); not wasted capacity",
        routing_metadata=(
            None
            if base_member["legacy"]
            else {**_member_routing_metadata(base_member), "synthesis_base_member_id": base}
        ),
    )
    try:
        pid = _spawn(
            synth_agent,
            mode,
            _synthesis_prompt(h, base),
            wt,
            log,
            run_id=run_id,
            target=target,
            task_type="synthesize",
        )
    except Exception as exc:
        synthesis_promotion._atomic_json(
            launch_artifact,
            {
                **launch_record,
                "status": "failed",
                "failure_id": feedback._completion_hash(str(exc)),
                "failed_ts": int(time.time()),
            },
        )
        raise
    synthesis_promotion._atomic_json(
        launch_artifact,
        {**launch_record, "status": "launched", "pid": pid, "launched_ts": int(time.time())},
    )
    return {
        "base": base,
        "synth_agent": synth_agent,
        "run_id": run_id,
        "pid": pid,
        "worktree": str(wt),
        "log": str(log),
        "ranking": h["ranking"],
        "gate": gate,
    }


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "synthesize":
        judge = argv[3] if len(argv) > 3 else "codex"
        print(json.dumps(synthesize(argv[1], argv[2], judge), indent=2, default=str))
        return 0
    if cmd == "prepare":
        repo, spec_file, exp_id, agents = argv[1], argv[2], argv[3], argv[4].split(",")
        task_type = argv[5] if len(argv) > 5 else "implement"
        print(
            json.dumps(prepare(repo, spec_file, exp_id, agents, task_type), indent=2, default=str)
        )
        return 0
    if cmd == "prepare-arms":
        # Tranche 0 lane B: Arm-aware preparation
        # argv[1] = repo, argv[2] = spec_file, argv[3] = exp_id, argv[4] = arms_json
        import json as json_mod

        repo, spec_file, exp_id, arms_json = argv[1], argv[2], argv[3], argv[4]
        task_type = argv[5] if len(argv) > 5 else "implement"
        arms = json_mod.loads(arms_json)
        print(
            json.dumps(
                prepare_arms(repo, spec_file, exp_id, arms, task_type), indent=2, default=str
            )
        )
        return 0
    if cmd == "status":
        print(json.dumps(status(argv[1]), indent=2, default=str))
        return 0
    if cmd == "followup":
        max_exp = int(argv[argv.index("--max") + 1]) if "--max" in argv else 1
        print(json.dumps(followup(max_experiments=max_exp), indent=2, default=str))
        return 0
    if cmd == "collect":
        print(json.dumps(collect(argv[1], argv[2]), indent=2, default=str))
        return 0
    if cmd == "evaluate":
        evs = None
        timeout = 1500
        i = 4
        while i < len(argv):
            arg = argv[i]
            if arg in ("--timeout", "--eval-timeout"):
                timeout = int(argv[i + 1])
                i += 2
            elif evs is None:
                evs = arg.split(",")
                i += 1
            else:
                raise SystemExit(f"unknown evaluate argument: {arg}")
        print(
            json.dumps(
                evaluate(argv[1], argv[2], argv[3], evs, timeout=timeout),
                indent=2,
                default=str,
            )
        )
        return 0
    print(f"unknown phase: {cmd}", file=sys.stderr)
    return 2


def _selftest():
    global EXP_DIR
    assert exp_branch("e1", "claude") == "exp/e1-claude"
    assert (
        exp_worktree("stranske/Workflows", "e1", "codex").name == "stranske__Workflows__e1__codex"
    )
    assert AGENT_MODE["cursor"] == "composer" and AGENT_MODE["claude"] == "full"
    p = implement_prompt("SPEC-BODY")
    assert "FROZEN SPECIFICATION" in p and "DO NOT push" in p and "SPEC-BODY" in p, p
    ev = evaluate_prompt("S", {"A": "diffA", "B": "diffB"})
    assert (
        "STRICT JSON" in ev and "CANDIDATE A" in ev and "CANDIDATE B" in ev and "anonymized" in ev
    ), ev
    w = _wrapped("cursor", ["cursor-agent", "-p", "x"])
    assert "/.local/bin" in w and "cursor-agent.env" in w and "set -a" in w, w
    wv = _wrapped("vibe", ["vibe", "--prompt", "x"])
    assert "cursor-agent.env" not in wv, "vibe needs no sourced auth file"
    # JSON extraction survives prose/markdown wrapping and picks the object with "scores"
    assert _extract_json('chat\n```json\n{"scores":{"A":7,"B":5}}\n```\n') == {
        "scores": {"A": 7, "B": 5}
    }
    assert _extract_json('prefix {"x":1} then {"scores":{"A":8},"best":"A"} end')["best"] == "A"
    assert _extract_json("no json at all") is None
    assert _extract_evidence_gaps(
        {
            "evidence_gaps": [
                " need test output ",
                {"gap": "need test output"},
                {"missing": "coverage delta"},
            ]
        }
    ) == ["need test output", "coverage delta"]
    assert _extract_cited_evidence_types(
        {"cited_evidence_types": [" test_run_output ", {"name": "test_run_output"}]}
    ) == ["test_run_output"]
    old_bypass = os.environ.get("ORCH_CODEX_BYPASS_INNER_SANDBOX")
    try:
        os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "0"
        ec = _eval_command("codex", "/tmp/p.txt")
        assert 'read-only "$(cat /tmp/p.txt)"' in ec and "homebrew" in ec, ec
        os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "1"
        ecn = _eval_command("codex", "/tmp/p.txt")
        assert "--dangerously-bypass-approvals-and-sandbox" in ecn and "--sandbox" not in ecn, ecn
    finally:
        if old_bypass is None:
            os.environ.pop("ORCH_CODEX_BYPASS_INNER_SANDBOX", None)
        else:
            os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = old_bypass
    ecl = _eval_command("claude", "/tmp/p.txt")
    assert ".claude-oauth-token" in ecl and 'claude -p "$(cat /tmp/p.txt)"' in ecl, ecl
    eg = _eval_command("gemini", "/tmp/p.txt")
    assert f"--add-dir {shlex.quote(str(ORCH.resolve()))}" in eg and "homebrew" in eg, eg
    evb = _eval_command("vibe", "/tmp/p.txt")
    assert "--trust" in evb, evb
    # >=4-evaluator policy: a 2-implementer A/B tops up with NEUTRAL non-implementers (limits self-favoring)
    e2 = _ensure_min_evaluators(["codex", "cursor"])
    assert len(e2) == 4 and e2[:2] == [
        "codex",
        "cursor",
    ], e2  # caller set preserved, then topped up
    assert "claude" in e2 and not set(e2) & {
        "gemini"
    }, e2  # neutral judges added; gemini last (not needed at 4)
    e5 = _ensure_min_evaluators(["claude", "codex", "gemini", "cursor", "vibe"])
    assert len(e5) == 5, e5  # already >=4 → unchanged (no dupes)
    assert (
        _ensure_min_evaluators(["codex"])[0] == "codex"
        and len(_ensure_min_evaluators(["codex"])) == 4
    )
    # diff join must not mangle patches: trailing single-space context lines survive, parts
    # concatenate newline-terminated with no separator (live-caught corrupt-patch bug)
    committed_demo = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-old\n+new\n \n"
    uncommitted_demo = "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b"
    joined = _join_diffs(committed_demo, uncommitted_demo)
    assert "\n \ndiff --git a/y" in joined, repr(joined)  # space-context line PRESERVED
    assert joined.endswith("+b\n"), repr(joined[-10:])  # terminating newline added
    assert _join_diffs("", "   \n", uncommitted_demo).startswith(
        "diff --git a/y"
    ), "empty parts dropped"
    # eval prompt diff cap: oversized candidates truncate with an explicit marker (E2BIG bug)
    big = "x" * (EVAL_DIFF_CAP_CHARS + 500)
    capped = _capped_diff(big)
    assert len(capped) < len(big) and "diff truncated: 500 of" in capped, capped[-80:]
    assert _capped_diff("small") == "small"

    # followup: only idle, un-evaluated, un-skipped experiments are driven; cap respected;
    # zero-diff experiments get a skip stamp instead of rescanning forever (injected phases; pure).
    import tempfile as _tf

    old_exp_dir = EXP_DIR
    ftmp = Path(_tf.mkdtemp(prefix="exp-followup-selftest-"))
    old_followup_db = feedback.DB_PATH
    old_capabilities_reg = capabilities.REG
    try:
        EXP_DIR = ftmp
        feedback.DB_PATH = ftmp / "feedback.db"
        capabilities.REG = ftmp / "capabilities.json"
        calls: dict = {"collect": [], "evaluate": [], "subject_lifecycle": []}
        for name, idle, dir_age in (("F1", True, 100), ("F2", True, 200), ("F3", False, 50)):
            d = ftmp / name
            d.mkdir()
            (d / "meta.json").write_text(
                json.dumps({"repo": "o/r", "base": "main", "agents": ["codex"]})
            )
            (d / "spec.md").write_text("SPEC")
            (d / "codex.log").write_text("done")
            if idle:
                old_time = time.time() - 3600
                os.utime(d / "codex.log", (old_time, old_time))
                os.utime(d / "meta.json", (old_time, old_time))
            # deterministic scan order (mtime desc): F3 first (still-running), then F1, then F2
            dir_time = time.time() - dir_age
            os.utime(d, (dir_time, dir_time))
        (ftmp / "F4").mkdir()  # no meta/spec — ignored entirely

        def fake_collect(repo, exp_id):
            calls["collect"].append(exp_id)
            nbytes = 10 if exp_id == "F1" else 0
            return {"exp_id": exp_id, "diffs": {"codex": {"bytes": nbytes}}}

        def fake_evaluate(repo, spec_file, exp_id, evs, timeout=600):
            calls["evaluate"].append(exp_id)
            # honor the real evaluate() contract: eval-maps.json marks the experiment done,
            # which is exactly what makes followup() idempotent
            (ftmp / exp_id / "eval-maps.json").write_text("{}")
            return {
                "exp_id": exp_id,
                "evaluators": ["codex", "claude"],
                "objective_anchors": {"anchored": []},
            }

        synth_wt = ftmp / "_synth_worktree"
        synth_wt.mkdir()
        subprocess.run(["git", "init", str(synth_wt)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(synth_wt), "config", "user.email", "test@example.test"], check=True
        )
        subprocess.run(["git", "-C", str(synth_wt), "config", "user.name", "Test"], check=True)
        (synth_wt / "feature.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(synth_wt), "add", "feature.py"], check=True)
        subprocess.run(
            ["git", "-C", str(synth_wt), "commit", "-m", "base"], check=True, capture_output=True
        )

        def fake_synthesize(repo, exp_id):
            calls.setdefault("synthesize", []).append(exp_id)
            return {
                "gate": {"decision": "use"},
                "ranking": [],
                "base": "codex",
                "synth_agent": "codex",
                "run_id": f"{exp_id}:synth",
                "pid": 999999,
                "worktree": str(synth_wt),
                "log": str(ftmp / exp_id / "synth.log"),
            }

        promotion_mode = {"status": "pending"}

        def fake_promotion_completion(state):
            status = promotion_mode["status"]
            if status == "interrupted":
                return {"status": "interrupted", "reason": "simulated interruption"}
            if status == "complete":
                head = subprocess.run(
                    ["git", "-C", str(synth_wt), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                return {"status": "complete", "commit": head, "marker_hash": "sha256:" + "a" * 64}
            return {"status": "pending"}

        def fake_promotion_resume(state):
            calls.setdefault("resume", []).append(state["experiment_id"])
            return {
                "pid": 999998,
                "run_id": f"{state['experiment_id']}:synth:resume:1",
                "log": str(ftmp / state["experiment_id"] / "synth-resume.log"),
            }

        def fake_promotion_verify(state, root):
            evidence = {
                "scope": {
                    "ok": True,
                    "changed_paths": ["feature.py"],
                    "changed_paths_hash": feedback._completion_hash(["feature.py"]),
                },
                "secret_scan": {"ok": True, "finding_ids": []},
                "local_verify": {
                    "ok": True,
                    "verdict": "PASS",
                    "test_cmd": "pytest tests/test_feature.py",
                },
                "runtime_ac": {"ok": True, "verdict": "PASS"},
                "repo_gates": [{"ok": True, "argv": ["pytest", "tests/test_feature.py"]}],
                "deliberate_break_status": "PASS",
            }
            return {
                "passed": True,
                "transient": False,
                "evidence": evidence,
                "evidence_hash": feedback._completion_hash(evidence),
            }

        def fake_subject_lifecycle(exp_id, lifecycle, reason=None):
            calls["subject_lifecycle"].append((exp_id, lifecycle, reason))
            return True

        fu = followup(
            max_experiments=1,
            collect_fn=fake_collect,
            evaluate_fn=fake_evaluate,
            synthesize_fn=fake_synthesize,
            subject_lifecycle_fn=fake_subject_lifecycle,
            promotion_completion_fn=fake_promotion_completion,
            promotion_resume_fn=fake_promotion_resume,
            promotion_verify_fn=fake_promotion_verify,
        )
        assert len(fu["processed"]) == 1 and fu["processed"][0]["evaluated"], fu
        assert {s["reason"] for s in fu["skipped"]} == {"still-running", "per-call-cap"}, fu
        first = fu["processed"][0]["exp_id"]
        # Evaluation is persisted before async synthesis launch; no terminal stamp yet.
        assert fu["processed"][0].get("ship_gate") == "synth_running", fu
        assert not (ftmp / first / "ship-gate.json").exists()
        assert not (ftmp / ".last-ship-gate").exists()
        assert calls["synthesize"] == [first], calls
        # Second call resumes one simulated interruption exactly once, while the
        # other idle zero-diff experiment is terminally skipped.
        promotion_mode["status"] = "interrupted"
        fu2 = followup(
            max_experiments=2,
            collect_fn=fake_collect,
            evaluate_fn=fake_evaluate,
            synthesize_fn=fake_synthesize,
            subject_lifecycle_fn=fake_subject_lifecycle,
            promotion_completion_fn=fake_promotion_completion,
            promotion_resume_fn=fake_promotion_resume,
            promotion_verify_fn=fake_promotion_verify,
        )
        stamped = [p for p in fu2["processed"] if not p["evaluated"]]
        assert calls["evaluate"] == [first], calls
        assert calls["synthesize"] == [first], "inflight promotion caps synthesis launches"
        assert calls["resume"] == [first], calls
        assert any("synthesis_resumed" in row.get("actions", []) for row in fu2["promotions"]), fu2
        assert stamped and (ftmp / stamped[0]["exp_id"] / "followup-skip.json").exists(), fu2

        # Complete the resumed synthesis. One reconciliation advances through
        # synth_complete -> synth_verified -> candidate_ready exactly once.
        (synth_wt / "feature.py").write_text("VALUE = 2\n")
        subprocess.run(["git", "-C", str(synth_wt), "add", "feature.py"], check=True)
        subprocess.run(
            ["git", "-C", str(synth_wt), "commit", "-m", "synthesis"],
            check=True,
            capture_output=True,
        )
        promotion_mode["status"] = "complete"
        fu3 = followup(
            max_experiments=2,
            collect_fn=fake_collect,
            evaluate_fn=fake_evaluate,
            synthesize_fn=fake_synthesize,
            subject_lifecycle_fn=fake_subject_lifecycle,
            promotion_completion_fn=fake_promotion_completion,
            promotion_resume_fn=fake_promotion_resume,
            promotion_verify_fn=fake_promotion_verify,
        )
        assert not fu3["processed"], fu3
        promoted = synthesis_promotion.load_state(ftmp / first)
        assert promoted["delivery_phase"] == "candidate_ready", promoted
        assert (ftmp / first / "ship-gate.json").exists()
        assert (ftmp / ".last-ship-gate").exists()
        phase_counts = {
            phase: sum(row.get("to") == phase for row in promoted["phase_history"])
            for phase in synthesis_promotion.DELIVERY_PHASES
        }
        assert phase_counts["evaluated"] == 1
        assert phase_counts["synth_running"] == 1
        assert phase_counts["synth_complete"] == 1
        assert phase_counts["synth_verified"] == 1
        assert phase_counts["candidate_ready"] == 1

        # Repeated followup creates no duplicate candidate or phase transition.
        candidate_id = promoted["candidate"]["candidate_id"]
        fu4 = followup(
            max_experiments=2,
            collect_fn=fake_collect,
            evaluate_fn=fake_evaluate,
            synthesize_fn=fake_synthesize,
            subject_lifecycle_fn=fake_subject_lifecycle,
            promotion_completion_fn=fake_promotion_completion,
            promotion_resume_fn=fake_promotion_resume,
            promotion_verify_fn=fake_promotion_verify,
        )
        repeated = synthesis_promotion.load_state(ftmp / first)
        assert repeated["candidate"]["candidate_id"] == candidate_id
        assert sum(row.get("to") == "candidate_ready" for row in repeated["phase_history"]) == 1
        assert not fu4["processed"], fu4
        assert any(
            exp_id == first and lifecycle == "evaluated"
            for exp_id, lifecycle, _reason in calls["subject_lifecycle"]
        ), calls
        assert any(
            lifecycle == "failed" and reason == "no_diffs_recoverable"
            for _exp_id, lifecycle, reason in calls["subject_lifecycle"]
        ), calls
    finally:
        EXP_DIR = old_exp_dir
        feedback.DB_PATH = old_followup_db
        capabilities.REG = old_capabilities_reg
        import shutil as _sh

        _sh.rmtree(ftmp, ignore_errors=True)

    # 12c: a 4-arm experiment satisfies the minimum with implementers alone — guarantee >=1
    # NEUTRAL judge anyway (all-implementer juries showed 9/10 spreads w/ self-preference live).
    arms4 = ["codex", "gemini", "cursor", "vibe"]
    e_neutral = _ensure_min_evaluators(arms4, implementers=arms4)
    assert e_neutral[:4] == arms4 and e_neutral[-1] == "claude" and len(e_neutral) == 5, e_neutral
    # drain-mode capacity is preferred among eligible neutrals (capacity injected; pure)
    e_drain = _ensure_min_evaluators(
        ["codex"],
        minimum=1,
        implementers=["codex"],
        capacity={"gemini": {"policy": "drain"}},
    )
    assert e_drain == ["codex", "gemini"], e_drain
    # without drain info, static EVALUATOR_TOPUP_ORDER picks claude first
    e_static = _ensure_min_evaluators(["codex"], minimum=1, implementers=["codex"])
    assert e_static == ["codex", "claude"], e_static
    # synthesis: winner + harvest from PER-JUDGE maps (codex judge uses a different letter order)
    maps = {
        "claude": {"A": "cursor", "B": "claude", "C": "codex"},
        "codex": {
            "A": "codex",
            "B": "claude",
            "C": "cursor",
        },  # DIFFERENT order — exercises per-judge lookup
        "cursor": {"A": "cursor", "B": "claude", "C": "codex"},
        "gemini": {
            "A": "cursor",
            "B": "claude",
            "C": "codex",
        },  # excluded as unreliable
    }
    verds = {
        "claude": {
            "scores": {"A": 8, "B": 5, "C": 7},
            "notes": {"A": "cleanest", "B": "fragile coupling", "C": "defensive"},
        },
        "codex": {
            "scores": {"A": 8.5, "B": 6, "C": 8},
            "notes": {"A": "unique safety test", "C": "clean"},
        },  # A=codex(self), C=cursor
        "cursor": {
            "scores": {"A": 9, "B": 5, "C": 8},
            "notes": {"C": "global enabled scans all repos"},
        },
        "gemini": {"scores": {"A": 8, "B": 10, "C": 6}, "notes": {"B": "excellent"}},
    }
    h = _winner_and_harvest(verds, maps, reliability={"ready_judge_count": 0, "judges": {}})
    assert h["winner"] == "cursor", h  # cursor 8.33 highest (gemini excluded)
    assert abs(h["winner_mean"] - 8.333) < 0.01, h["winner_mean"]
    assert h["judge_weights"]["gemini"] == 0.0, h[
        "judge_weights"
    ]  # legacy fallback until reliability-ready
    ready_reliability = {
        "ready_judge_count": 4,
        "judges": {
            "claude": {"ready": True, "weight": 0.1},
            "codex": {"ready": True, "weight": 0.1},
            "cursor": {"ready": True, "weight": 0.1},
            "gemini": {"ready": True, "weight": 1.0},
        },
    }
    h_weighted = _winner_and_harvest(verds, maps, reliability=ready_reliability)
    assert h_weighted["winner"] == "claude", h_weighted  # data-ready Gemini can count
    assert h_weighted["judge_weights"]["gemini"] == 1.0, h_weighted["judge_weights"]
    try:
        _winner_and_harvest(
            {"gemini": {"scores": {"A": 10}}},
            {"gemini": {"A": "claude"}},
            reliability={"ready_judge_count": 0, "judges": {}},
        )
        assert False, "all fallback-excluded judges must not be silently re-included"
    except ValueError as exc:
        assert "no eligible evaluator" in str(exc), exc
    assert any(
        "safety test" in s for s in h["alt_strengths"]["codex"]
    ), h  # codex's strength harvested per-judge
    assert h["winner_weaknesses"], "winner's noted weaknesses surfaced for fixing"
    sp = _synthesis_prompt(h, "cursor")
    assert "WEAKNESSES TO FIX" in sp and "STRENGTHS TO ADOPT" in sp and "safety test" in sp, sp
    gp = _gate_prompt(h["ranking"], {h["winner"]: h["winner_weaknesses"], **h["alt_strengths"]})
    assert (
        "PRODUCTIVE STARTING POINT" in gp and "digression" in gp and '"decision"' in gp
    ), gp  # judgment, not cutoff
    # Ledger/run-id integration: exercise the real spawn/evaluate call paths with fake agent processes.
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="exp-abcd-ledger-selftest-"))
    old_exp_dir = EXP_DIR
    old_db = feedback.DB_PATH
    old_handoff, old_ledger = adapters.HANDOFF, adapters.LEDGER
    old_build_command = adapters.build_command
    old_popen = subprocess.Popen
    # ISOLATION, not a skip. This block replaces subprocess.Popen wholesale to fake the agent
    # processes, and `_eval_command` resolves each seat's model on the way — which spawns a CLI
    # catalog probe whenever the advertised-model cache is cold. The probe then lands in FakePopen,
    # which is built for the evaluator's stdout contract, and dies on `stdout.write` with an int.
    # It never showed up locally because the cache is always warm here; on a fresh machine (the
    # first CI run, 2026-08-21) it was a hard AttributeError.
    #
    # ORCH_MODEL_PROBE is the module's own documented kill-switch for exactly this: catalog probes
    # off, pinned models only, NO subprocess. Turning it off for the stubbed window makes the
    # selftest hermetic on every machine — it now runs MORE than it did, not less, which is why
    # this is a fix and not an applicability gate.
    old_model_probe = os.environ.get("ORCH_MODEL_PROBE")
    old_advertised_memo = dict(adapters._ADVERTISED_MEMO)

    class FakePopen:
        next_pid = 4900

        def __init__(self, cmd, cwd=None, stdout=None, **_kwargs):
            self.cmd = cmd
            self.cwd = cwd
            captured.setdefault("popen_cmds", []).append(cmd)
            self.pid = FakePopen.next_pid
            FakePopen.next_pid += 1
            if stdout is not None:
                stdout.write(json.dumps({"usage": {"input_tokens": 11, "output_tokens": 5}}) + "\n")
                stdout.write(
                    json.dumps(
                        {
                            "scores": {"A": 8, "B": 6},
                            "best": "A",
                            "worst": "B",
                            "notes": {"A": "solid", "B": "thin"},
                            "evidence_gaps": ["need runtime logs to judge behavior"],
                            "cited_evidence_types": ["test_run_output", "unknown_type"],
                        }
                    )
                    + "\n"
                )
                stdout.flush()

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True

    try:
        EXP_DIR = tmp / "experiments"
        feedback.DB_PATH = tmp / "feedback" / "orchestrator.db"
        adapters.HANDOFF = tmp
        adapters.LEDGER = tmp / "capacity-ledger.ndjson"
        captured = {}

        def fake_build_command(agent, prompt, mode, cwd=None):
            captured["build"] = {"agent": agent, "prompt": prompt, "mode": mode, "cwd": cwd}
            return ["printf", "fake-agent"]

        adapters.build_command = fake_build_command
        os.environ["ORCH_MODEL_PROBE"] = "0"
        adapters._ADVERTISED_MEMO.clear()
        subprocess.Popen = FakePopen

        run_id = "e1:codex"
        target = "stranske/Repo [exp e1]"
        wt = tmp / "wt"
        wt.mkdir()
        log = tmp / "codex.log"
        feedback.record_run(
            run_id,
            target,
            "implement",
            "codex",
            mode="full",
            experiment_id="e1",
            model=adapters.model_identity("codex", "full"),
        )
        pid = _spawn(
            "codex",
            "full",
            "IMPLEMENT",
            wt,
            log,
            run_id=run_id,
            target=target,
            task_type="implement",
        )
        assert pid == 4900 and "run_id=e1:codex" in log.read_text(), log.read_text()
        wrapped_cmd = " ".join(str(part) for part in captured["popen_cmds"][-1])
        assert "ledger_reconcile.py" in wrapped_cmd and " complete " in wrapped_cmd, wrapped_cmd
        gemini_log = tmp / "gemini.log"
        gemini_pid = _spawn(
            "gemini",
            "full",
            "IMPLEMENT WITH GEMINI",
            wt,
            gemini_log,
            run_id="e1:gemini",
            target=target,
            task_type="implement",
        )
        assert isinstance(gemini_pid, int) and gemini_pid >= 4900, gemini_pid
        assert "GEMINI WORKSPACE:" in captured["build"]["prompt"], captured["build"]
        assert str(wt.resolve()) in captured["build"]["prompt"], captured["build"]
        assert captured["build"]["cwd"] == wt, captured["build"]
        rows = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
        start = next(
            row for row in rows if row.get("run_id") == run_id and row.get("event") == "start"
        )
        assert start["task_type"] == "implement" and start["log_file"] == str(log), start
        complete_cmd = _completion_cmd(
            "codex", "full", run_id, target, "implement", log, start["started_ts"]
        )
        assert (
            "ledger_reconcile.py" in complete_cmd and "--run-id e1:codex" in complete_cmd
        ), complete_cmd
        _record_execution_complete(
            "codex", "full", run_id, target, "implement", log, start["started_ts"]
        )
        import ledger_reconcile

        dry = ledger_reconcile.reconcile(adapters.LEDGER, dry_run=True)
        costs = {row["run_id"]: row for row in dry["costs"]}
        assert costs[run_id]["tokens_in"] == 11 and costs[run_id]["tokens_out"] == 5, dry

        exp_id = "eval1"
        edir = exp_paths(exp_id)
        edir.mkdir(parents=True)
        (edir / "meta.json").write_text(
            json.dumps(
                {
                    "repo": "stranske/Repo",
                    "base": "main",
                    "agents": ["codex", "cursor"],
                    "exp_id": exp_id,
                }
            )
        )
        (edir / "diff-codex.patch").write_text("diff --git a/a b/a\n+codex\n")
        (edir / "diff-cursor.patch").write_text("diff --git a/a b/a\n+cursor\n")
        spec_file = tmp / "spec.md"
        spec_file.write_text("SPEC\n")
        feedback.record_evidence_type("test_run_output", "fixture evidence type")
        ev_out = evaluate(
            "stranske/Repo",
            str(spec_file),
            exp_id,
            evaluators=["codex", "claude", "cursor", "vibe"],
            timeout=1,
        )
        assert all(ev_out["parsed_ok"].values()), ev_out
        rows = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
        eval_rows = [row for row in rows if row.get("run_id") == f"{exp_id}:eval:codex"]
        assert [row.get("event") for row in eval_rows] == [
            "start",
            "complete",
        ], eval_rows
        dry = ledger_reconcile.reconcile(adapters.LEDGER, dry_run=True)
        costs = {row["run_id"]: row for row in dry["costs"]}
        assert costs[f"{exp_id}:eval:codex"]["tokens_in"] == 11, dry
        with feedback._conn() as c:
            judge = c.execute(
                "SELECT task_type, agent FROM runs WHERE run_id=?",
                (f"{exp_id}:eval:codex",),
            ).fetchone()
            scored = c.execute(
                "SELECT COUNT(*) FROM evaluations WHERE experiment_id=?", (exp_id,)
            ).fetchone()[0]
            gaps = c.execute(
                "SELECT COUNT(*) FROM evidence_gaps WHERE ref=?", (f"{exp_id}:eval",)
            ).fetchone()[0]
            influence = c.execute(
                "SELECT influence FROM evidence_types WHERE name='test_run_output'"
            ).fetchone()[0]
        assert judge == ("review", "codex") and scored > 0 and gaps == 4, (
            judge,
            scored,
            gaps,
        )
        assert influence == 4, influence
    finally:
        EXP_DIR = old_exp_dir
        feedback.DB_PATH = old_db
        adapters.HANDOFF = old_handoff
        adapters.LEDGER = old_ledger
        adapters.build_command = old_build_command
        subprocess.Popen = old_popen
        if old_model_probe is None:
            os.environ.pop("ORCH_MODEL_PROBE", None)
        else:
            os.environ["ORCH_MODEL_PROBE"] = old_model_probe
        adapters._ADVERTISED_MEMO.clear()
        adapters._ADVERTISED_MEMO.update(old_advertised_memo)
        shutil.rmtree(tmp, ignore_errors=True)
    # COLLECT MUST RECOVER FROM THE BRANCH WHEN THE WORKTREE IS GONE -- exercised against a REAL
    # repo with a real branch and NO worktree, because a test that re-implements the guard's own
    # condition proves nothing about the code path. Worktrees are GC'd after 14 days while the arm's
    # commits persist as `exp/<id>-<agent>` in the shared store; without this fallback collect()
    # returned an EMPTY diff and followup stamped `followup-skip.json`, permanently recording
    # "evidence lost" while the branch sat intact. It zeroed 4 recovered experiments on 2026-08-21.
    with tempfile.TemporaryDirectory(prefix="collect-branch-") as _td:
        _root = Path(_td)
        _store = _root / "store"
        _store.mkdir()
        _genv = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        def _g(*a):
            subprocess.run(
                ["git", "-C", str(_store), *a],
                check=True,
                capture_output=True,
                text=True,
                env=_genv,
            )

        _g("init", "-q", "-b", "main")
        (_store / "f.txt").write_text("base\n")
        _g("add", "-A")
        _g("commit", "-qm", "base")
        _g("update-ref", "refs/remotes/origin/main", "HEAD")
        _xid = "tick-1700000000-owner-repo-1"
        _g("checkout", "-q", "-b", exp_branch(_xid, "codex"))
        (_store / "f.txt").write_text("base\nARM WORK\n")
        _g("add", "-A")
        _g("commit", "-qm", "arm")
        _g("checkout", "-q", "main")

        _old_repos, _old_expdir = provision.REPOS_DIR, EXP_DIR
        try:
            provision.REPOS_DIR = _root
            (_root / provision.repo_slug("owner/repo")).symlink_to(_store)
            globals()["EXP_DIR"] = _root / "experiments"
            _ed = EXP_DIR / _xid
            _ed.mkdir(parents=True)
            (_ed / "meta.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "repo": "owner/repo",
                        "base": "main",
                        "base_sha": None,
                        "agents": ["codex"],
                        "exp_id": _xid,
                        "task_type": "implement",
                        "arms": [],
                        "members": [],
                    }
                )
            )
            # No worktree exists anywhere -- this is the reclaimed-experiment case.
            _out = collect("owner/repo", _xid)["diffs"]["codex"]
            assert _out["source"] == "branch", _out
            assert _out["bytes"] > 0, _out
            assert "ARM WORK" in (_ed / exp_diff_path("codex")).read_text()
        finally:
            provision.REPOS_DIR = _old_repos
            globals()["EXP_DIR"] = _old_expdir

    print(
        "exp_abcd.py selftest: OK (branch/worktree naming, mode map, frozen implement prompt, "
        "anonymized eval prompt, PATH+auth wrapper, JSON extraction, eval commands, "
        "evidence-gap capture, resumable verified synthesis promotion + JUDGMENT ship-gate, "
        "run_id ledger reconciliation)"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
