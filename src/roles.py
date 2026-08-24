#!/usr/bin/env python3
"""Agent-role registry + router-chosen, swappable backends for the Orchestrator.

WHY (see ARCHITECTURE.md — "rails vs. agent-roles"): selection stays deterministic
(the router is the signal feedback.py learns), but *judgment* steps become typed
agent-roles. A role is a contract — typed input -> typed output + a routing prior —
and the LLM behind it is chosen by the SAME learned router via route_role(). That
keeps the model swappable and turns every role into a new learnable surface.

This module ships the first amber boxes from the architecture diagram:
RedirectAgent, PromptAgent, DecomposerAgent, TriageAgent, and AdjudicatorAgent.
RedirectAgent PROPOSES into redirect_plan.py in SHADOW mode only. PromptAgent
authors scoped delegation prompts in SHADOW mode only. DecomposerAgent authors
epic decomposition plans in SHADOW mode only. TriageAgent ranks and groups
backlog items in SHADOW mode only. AdjudicatorAgent assesses disputed blockers
in SHADOW mode only. None mutate state; promotion from advisor -> autonomous
requires measured proposal quality against outcomes.

Adding another future role = register a Role with its
route_as prior, eligible_backends, a build_prompt(ctx)->str, and a validate(obj)->errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import backlog as backlog_mod
import capabilities
import dispatcher
import epic_lane
import feedback
import redirect_plan
import redirect_policy
import router

RESERVE = set(router.RESERVE_AGENTS)  # conserve scarce seats (claude) for roles too
ALLOWED_ACTIONS = set(redirect_policy.ACTIONS)  # wait/collect/inspect/redirect/decompose
CONFIDENCE = {"low", "medium", "high"}
PROMPT_TASK_TYPES = set(router.ROUTE_TABLE)
TRIAGE_ACTIONS = {"work_now", "defer", "needs_scope", "skip", "monitor"}
TRIAGE_REC_KEYS = {"target", "action", "priority", "reason", "batch_id"}
TRIAGE_BATCH_KEYS = {"id", "targets", "reason", "risk"}
ADJUDICATOR_DECISIONS = {"uphold_blocker", "reject_blocker", "needs_more_evidence"}
ADJUDICATOR_CASE_TYPES = {"runtime_ac", "adversarial", "review"}
ADJUDICATOR_TOP_KEYS = {
    "decision",
    "confidence",
    "rationale",
    "evidence_assessment",
    "ground_truth_refs",
    "recommended_next_step",
    "evidence_gaps",
}
ADJUDICATOR_EVIDENCE_KEYS = {"claim", "status", "evidence_ref", "reason"}
ROLE_CAPABILITY_IDS = {
    "redirect": "role-redirect",
    "prompt": "role-prompt",
    "decomposer": "role-decomposer",
    "triage": "role-triage",
    "adjudicator": "role-adjudicator",
}
ROLE_SELECTOR_STATUSES = {"no_matching_work", "matched_not_invoked", "invoked"}
_ROLE_INVOCATION_COUNTS = {name: 0 for name in ROLE_CAPABILITY_IDS}


def _role_capability_event(
    role_name: str,
    event_type: str,
    *,
    ref: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Emit strict production lifecycle evidence; remain inert in direct tests."""
    if os.environ.get("ORCH_CAPABILITY_HEARTBEATS") != "1":
        return
    capabilities.heartbeat(ROLE_CAPABILITY_IDS[role_name], event_type, ref=ref, metadata=metadata)


def _backend_error_detail(result: dict) -> str | None:
    """Return bounded backend diagnostics captured outside stdout/stderr."""
    detail = result.get("agent_log_tail")
    if not detail:
        return None
    return str(detail)


@dataclass(frozen=True)
class Role:
    """A typed, model-agnostic judgment unit the Orchestrator can call.

    route_as is an EXISTING ROUTE_TABLE task_type used purely as the routing prior; the
    learned weights for that task_type then refine which eligible backend wins over time.
    """

    name: str
    description: str
    route_as: str  # routing prior: an existing router.ROUTE_TABLE task_type
    eligible_backends: frozenset  # candidate LLM backends (the `only` set for select_agent)
    mode: str | None  # adapters reasoning mode hint, e.g. "full"
    input_keys: tuple  # required context keys (doc + caller contract)
    output_keys: tuple  # proposal keys the role emits
    build_prompt: Callable[[dict], str]  # context -> backend prompt (JSON-only instruction)
    validate: Callable[[dict], list]  # proposal -> list[str] of errors ([] == valid)
    # Generated roles carry these contracts.  Static roles keep their existing
    # hand-authored validators and therefore leave the fields unset.
    input_schema: dict[str, dict[str, Any]] | None = None
    output_schema: dict[str, dict[str, Any]] | None = None
    authority: str | None = None
    selector: dict[str, Any] | None = None
    capacity_policy: dict[str, Any] | None = None
    prompt_hash: str | None = None
    lifecycle: dict[str, Any] | None = None
    generated: bool = False


# --------------------------------------------------------------------------------------
# RedirectAgent — prompt + schema validation
# --------------------------------------------------------------------------------------
def _redirect_prompt(ctx: dict) -> str:
    report = ctx.get("report") or {}
    ac = (ctx.get("acceptance_criteria") or "").strip() or "(none provided)"
    history = ctx.get("attempt_history") or []

    hints = [f"  - {h.get('kind')}: {h.get('detail')}" for h in (report.get("hints") or [])]
    drift = report.get("drift") or {}
    drift_lines: list[str] = []
    if drift.get("severity") and drift.get("severity") != "none":
        drift_lines.append(f"  severity: {drift.get('severity')}")
        for finding in drift.get("findings") or []:
            paths = ", ".join((finding.get("paths") or [])[:5])
            drift_lines.append(
                f"  - {finding.get('kind')}: {finding.get('detail')}"
                + (f" [{paths}]" if paths else "")
            )
    tail = (report.get("log_tail") or "").strip()

    def block(items: list[str]) -> str:
        return "\n".join(items) if items else "  (none)"

    return "\n".join(
        [
            "You are RedirectAgent, the Orchestrator's redirect judge.",
            "A delegated coding agent is in-flight or has stalled. Decide what the Orchestrator should do",
            "next, and — only if you choose redirect or decompose — author the corrected delegation prompt.",
            "",
            "CRITICAL-EVALUATOR STANCE: your job is the correct call, not an eager one. Prefer 'inspect' or",
            "'wait' unless the evidence below clearly supports stopping the agent. Never invent a root cause",
            "or a file scope you cannot see in the evidence.",
            "",
            f"Target: {report.get('target')}    prior agent: {report.get('agent')}    lane: {report.get('lane')}",
            f"Watch state: {report.get('state')}    watch recommended_action: {report.get('recommended_action')}",
            f"Prior attempts on this target: {len(history)}",
            "",
            "Acceptance criteria the work must satisfy:",
            ac,
            "",
            "Root-cause hints from the prior log:",
            block(hints),
            "",
            "Drift signals (scope creep):",
            block(drift_lines),
            "",
            "Prior log tail:",
            (tail[-1200:] if tail else "  (none)"),
            "",
            "Choose exactly one action:",
            "  wait      - lane is healthy/active; let it run.",
            "  collect   - agent produced changes and looks done; inspect/collect the result.",
            "  inspect   - ambiguous; gather more evidence before any mutation.",
            "  redirect  - stop and re-delegate (same or switched agent) with a corrected prompt.",
            "  decompose - task is too broad; split it and retry the smallest verifiable slice.",
            "",
            "If redirect or decompose, corrected_prompt MUST be a complete standalone delegation prompt that",
            "injects the acceptance criteria, bounds scope to the right files, names what went wrong so it is",
            "not repeated, and states the finish workflow (implement -> validate -> commit -> push -> PR).",
            "switch_agent is OPTIONAL: name a different worker only if the evidence implicates the agent",
            "itself (e.g. repeated auth/capability failure); otherwise null.",
            "",
            "Output STRICT JSON only - no prose, no code fences - matching exactly:",
            '{"action":"wait|collect|inspect|redirect|decompose","reason":"<1-2 sentences>",',
            ' "confidence":"low|medium|high","corrected_prompt":"<full prompt or null>","switch_agent":"<agent or null>"}',
        ]
    )


def _validate_redirect(proposal: Any) -> list:
    errs: list[str] = []
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    action = proposal.get("action")
    if action not in ALLOWED_ACTIONS:
        errs.append(f"action must be one of {sorted(ALLOWED_ACTIONS)}; got {action!r}")
    reason = proposal.get("reason")
    if not (isinstance(reason, str) and reason.strip()):
        errs.append("reason must be a non-empty string")
    if proposal.get("confidence") not in CONFIDENCE:
        errs.append(
            f"confidence must be one of {sorted(CONFIDENCE)}; got {proposal.get('confidence')!r}"
        )
    if action in {"redirect", "decompose"}:
        corrected = proposal.get("corrected_prompt")
        if not (isinstance(corrected, str) and corrected.strip()):
            errs.append(f"{action} requires a non-empty corrected_prompt")
    switch = proposal.get("switch_agent")
    if switch is not None and not (isinstance(switch, str) and switch.strip()):
        errs.append("switch_agent must be a string or null")
    return errs


# --------------------------------------------------------------------------------------
# PromptAgent — prompt-authoring contract + validation
# --------------------------------------------------------------------------------------
def _as_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _prompt_agent_prompt(ctx: dict) -> str:
    target = ctx.get("target") or "(unknown target)"
    task_type = ctx.get("task_type") or "implement"
    lane = ctx.get("lane") or "(unspecified)"
    repo = ctx.get("repo") or "(infer from target if present)"
    goal = (ctx.get("goal") or "").strip() or "(none provided)"
    detail = (ctx.get("target_detail") or "").strip() or "(none provided)"
    context = (ctx.get("context") or "").strip() or "(none provided)"

    def bullets(name: str) -> list[str]:
        items = _as_lines(ctx.get(name))
        return [f"  - {item}" for item in items] if items else ["  (none)"]

    return "\n".join(
        [
            "You are PromptAgent, the Orchestrator's delegation prompt author.",
            "Your job is to turn an issue/brief into a complete, bounded prompt that a coding agent can",
            "execute without guessing. You are NOT selecting the worker; router selection remains deterministic.",
            "",
            "CRITICAL-EVALUATOR STANCE: be precise about scope and uncertainty. Do not invent files, APIs,",
            "or acceptance criteria not supported by the evidence. If the work is underspecified, write a",
            "prompt that starts with the smallest safe investigation step and names the uncertainty.",
            "",
            f"Target: {target}",
            f"Repository/context: {repo}",
            f"Lane: {lane}",
            f"Router/task_type selected by rails: {task_type}",
            "",
            "Goal:",
            goal,
            "",
            "Target detail / issue body / PR context:",
            detail,
            "",
            "Acceptance criteria already known:",
            *bullets("acceptance_criteria"),
            "",
            "Expected in-scope paths or areas:",
            *bullets("expected_paths"),
            "",
            "Constraints:",
            *bullets("constraints"),
            "",
            "Additional context:",
            context,
            "",
            "Output STRICT JSON only - no prose, no code fences - matching exactly:",
            "{",
            '  "task_type":"implement|testgen|mechanical|polish|review|epic|codemod|cross_repo|runtime_ac",',
            '  "summary":"<one sentence>",',
            '  "scoped_prompt":"<complete standalone delegation prompt>",',
            '  "definition_of_done":["<observable done condition>", "..."],',
            '  "acceptance_criteria":["<criterion the delegate must satisfy>", "..."],',
            '  "validation":["<specific command/check or manual evidence>", "..."],',
            '  "expected_paths":["<path or area>", "..."],',
            '  "out_of_scope":["<thing to avoid>", "..."],',
            '  "risk_flags":["<risk or uncertainty>", "..."],',
            '  "confidence":"low|medium|high"',
            "}",
            "",
            "The scoped_prompt MUST include: target, scope boundaries, acceptance criteria, validation,",
            "and finish workflow (implement -> validate -> commit -> push -> PR/update).",
        ]
    )


def _validate_prompt_agent(proposal: Any) -> list:
    errs: list[str] = []
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    task_type = proposal.get("task_type")
    if task_type not in PROMPT_TASK_TYPES:
        errs.append(f"task_type must be one of {sorted(PROMPT_TASK_TYPES)}; got {task_type!r}")
    for key in ("summary", "scoped_prompt"):
        if not (isinstance(proposal.get(key), str) and proposal[key].strip()):
            errs.append(f"{key} must be a non-empty string")
    required_lists = ("definition_of_done", "acceptance_criteria", "validation")
    optional_lists = ("expected_paths", "out_of_scope", "risk_flags")
    for key in required_lists:
        value = proposal.get(key)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) and v.strip() for v in value)
        ):
            errs.append(f"{key} must be a non-empty list of non-empty strings")
    for key in optional_lists:
        value = proposal.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            errs.append(f"{key} must be a list of non-empty strings")
    if proposal.get("confidence") not in CONFIDENCE:
        errs.append(
            f"confidence must be one of {sorted(CONFIDENCE)}; got {proposal.get('confidence')!r}"
        )
    prompt = proposal.get("scoped_prompt")
    if isinstance(prompt, str):
        lowered = prompt.lower()
        if re.search(r"\byou are (cursor|codex|gemini|vibe|aider|claude)\b", lowered):
            errs.append("scoped_prompt must not include agent persona text; dispatcher adds that")
        if "repo playbook (" in lowered:
            errs.append(
                "scoped_prompt must not include repo playbook text; dispatcher injects approved context"
            )
        for required in ("acceptance", "validat", "commit", "push", "pr"):
            if required not in lowered:
                errs.append(f"scoped_prompt must mention {required!r}")
    return errs


def _prompt_agent_dispatch_prompt(proposal: dict, *, target: str, lane: str | None = None) -> str:
    """Render PromptAgent JSON into a dispatch-ready prompt."""
    lines = [
        f"Target: {target}",
        f"Lane: {lane or '(unspecified)'}",
        f"Task type: {proposal.get('task_type')}",
        "",
        proposal["scoped_prompt"].strip(),
        "",
        "Definition of done:",
        *[f"- {item}" for item in proposal.get("definition_of_done") or []],
        "",
        "Acceptance criteria:",
        *[f"- {item}" for item in proposal.get("acceptance_criteria") or []],
        "",
        "Validation:",
        *[f"- {item}" for item in proposal.get("validation") or []],
    ]
    expected = proposal.get("expected_paths") or []
    if expected:
        lines.extend(["", "Expected paths / areas:", *[f"- {item}" for item in expected]])
    out_of_scope = proposal.get("out_of_scope") or []
    if out_of_scope:
        lines.extend(["", "Out of scope:", *[f"- {item}" for item in out_of_scope]])
    risks = proposal.get("risk_flags") or []
    if risks:
        lines.extend(["", "Risks / uncertainties:", *[f"- {item}" for item in risks]])
    lines.extend(
        [
            "",
            "Finish workflow:",
            "- Keep the diff scoped to the prompt.",
            "- Run the listed validation or explain precisely why a check cannot run.",
            "- Commit, push, and open/update the PR only after validation is complete.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------------------
# DecomposerAgent — epic decomposition contract + validation
# --------------------------------------------------------------------------------------
def _decomposer_prompt(ctx: dict) -> str:
    goal = ctx.get("goal") or ""
    repo = ctx.get("repo") or None
    target = ctx.get("target") or None
    context = ctx.get("context") or ""
    subtask_count = ctx.get("subtask_count")
    base = epic_lane.build_planner_prompt(
        goal=goal,
        repo=repo,
        target=target,
        context=context,
        subtask_count=subtask_count,
    )
    return "\n".join(
        [
            "You are DecomposerAgent, the Orchestrator's epic decomposition judge.",
            "Your job is to turn a large or vague goal into a dispatchable subtask DAG.",
            "You are NOT selecting workers, starting tasks, creating branches, or opening PRs; router and",
            "dispatcher remain deterministic rails controlled by the orchestrator.",
            "",
            "CRITICAL-EVALUATOR STANCE: split only when it reduces real execution risk. Prefer a small",
            "number of independently verifiable slices. Make dependencies explicit and keep integration",
            "work visible rather than hiding it in a coding subtask.",
            "",
            base,
        ]
    )


def _validate_decomposer(proposal: Any) -> list:
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    return epic_lane.validate_plan(proposal)


# --------------------------------------------------------------------------------------
# TriageAgent — backlog worth-it and batching contract + validation
# --------------------------------------------------------------------------------------
def _compact_backlog_items(items: list[dict], *, max_items: int | None = None) -> list[dict]:
    selected = items[:max_items] if max_items is not None and max_items >= 0 else list(items)
    compact: list[dict] = []
    for item in selected:
        labels = item.get("labels") or []
        if not isinstance(labels, list):
            labels = [str(labels)]
        body = str(item.get("body") or "").strip()
        if len(body) > 1400:
            body = body[:1400].rstrip() + "..."
        compact.append(
            {
                "target": str(item.get("target") or "").strip(),
                "task_type": str(item.get("task_type") or "implement").strip() or "implement",
                "lane": str(item.get("lane") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "labels": [str(label).strip() for label in labels if str(label).strip()],
                "body": body,
            }
        )
    return [item for item in compact if item["target"]]


def _triage_prompt(ctx: dict) -> str:
    items = _compact_backlog_items(ctx.get("backlog_items") or [], max_items=ctx.get("max_items"))
    capacity = ctx.get("capacity") or {}
    context = (ctx.get("context") or "").strip() or "(none provided)"
    omitted = max(0, int(ctx.get("omitted_count") or 0))

    blocks: list[str] = []
    for idx, item in enumerate(items, 1):
        labels = ", ".join(item.get("labels") or []) or "(none)"
        body = item.get("body") or "(none provided)"
        blocks.extend(
            [
                f"{idx}. Target: {item['target']}",
                f"   Lane/task_type: {item.get('lane') or '(none)'} / {item.get('task_type')}",
                f"   Title: {item.get('title') or '(none)'}",
                f"   Labels: {labels}",
                "   Body:",
                f"   {body}",
            ]
        )
    item_block = "\n".join(blocks) if blocks else "(empty backlog)"
    cap_block = json.dumps(capacity.get("agents", capacity), sort_keys=True)[:1600]

    return "\n".join(
        [
            "You are TriageAgent, the Orchestrator's backlog triage and batching judge.",
            "Your job is to advise which visible backlog items are worth acting on now, which need",
            "scope clarification or deferral, and which items form a logical batch.",
            "",
            "CRITICAL-EVALUATOR STANCE: do not be eager. Mark work needs_scope when the body/title/labels",
            "do not give a coding agent enough acceptance criteria to finish without guessing. Use defer",
            "for blocked or lower-value work, skip for duplicate/out-of-scope/noise, monitor for closer PRs",
            "that need inspection rather than new opener work, and work_now only for ready, bounded work.",
            "",
            "Rails you must not cross:",
            "- Do NOT select, name, rank, or recommend a worker agent/backend/model.",
            "- Do NOT change lane, task_type, labels, readiness, claims, or route selection.",
            "- Do NOT delegate, create branches, push, open PRs, or mutate any state.",
            "- For every visible item below, emit exactly one recommendation using its exact target.",
            "",
            f"Visible backlog items: {len(items)}"
            + (f" ({omitted} additional items omitted)" if omitted else ""),
            item_block,
            "",
            "Capacity snapshot for context only; it is not permission to choose agents:",
            cap_block or "(none)",
            "",
            "Additional orchestrator context:",
            context,
            "",
            "Output STRICT JSON only - no prose, no code fences - matching exactly:",
            "{",
            '  "summary":"<one sentence backlog triage summary>",',
            '  "recommendations":[',
            '    {"target":"owner/repo#N","action":"work_now|defer|needs_scope|skip|monitor",',
            '     "priority":1,"reason":"<1-2 sentences>","batch_id":"<id or null>"}',
            "  ],",
            '  "batches":[',
            '    {"id":"B1","targets":["owner/repo#N"],"reason":"<why grouped>","risk":"low|medium|high"}',
            "  ],",
            '  "global_risks":["<risk or uncertainty>", "..."],',
            '  "confidence":"low|medium|high"',
            "}",
            "",
            "Priority is 1 (highest) through 5 (lowest). Batches are advisory only; router capacity,",
            "claims, and worker selection remain deterministic code rails.",
        ]
    )


def _validate_triage_agent(proposal: Any) -> list:
    errs: list[str] = []
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    allowed_top = {
        "summary",
        "recommendations",
        "batches",
        "global_risks",
        "confidence",
    }
    extra = set(proposal) - allowed_top
    if extra:
        errs.append(f"unexpected top-level keys: {sorted(extra)}")
    if not (isinstance(proposal.get("summary"), str) and proposal["summary"].strip()):
        errs.append("summary must be a non-empty string")
    if proposal.get("confidence") not in CONFIDENCE:
        errs.append(
            f"confidence must be one of {sorted(CONFIDENCE)}; got {proposal.get('confidence')!r}"
        )
    risks = proposal.get("global_risks")
    if not isinstance(risks, list) or not all(isinstance(r, str) and r.strip() for r in risks):
        errs.append("global_risks must be a list of non-empty strings")

    recs = proposal.get("recommendations")
    if not isinstance(recs, list):
        errs.append("recommendations must be a list")
        recs = []
    for idx, rec in enumerate(recs):
        if not isinstance(rec, dict):
            errs.append(f"recommendations[{idx}] is not an object")
            continue
        extra_rec = set(rec) - TRIAGE_REC_KEYS
        if extra_rec:
            errs.append(f"recommendations[{idx}] has unexpected keys: {sorted(extra_rec)}")
        if not (isinstance(rec.get("target"), str) and rec["target"].strip()):
            errs.append(f"recommendations[{idx}].target must be a non-empty string")
        if rec.get("action") not in TRIAGE_ACTIONS:
            errs.append(f"recommendations[{idx}].action must be one of {sorted(TRIAGE_ACTIONS)}")
        priority = rec.get("priority")
        if not isinstance(priority, int) or not 1 <= priority <= 5:
            errs.append(f"recommendations[{idx}].priority must be an integer from 1 to 5")
        if not (isinstance(rec.get("reason"), str) and rec["reason"].strip()):
            errs.append(f"recommendations[{idx}].reason must be a non-empty string")
        batch_id = rec.get("batch_id")
        if batch_id is not None and not (isinstance(batch_id, str) and batch_id.strip()):
            errs.append(f"recommendations[{idx}].batch_id must be a string or null")

    batches = proposal.get("batches")
    if not isinstance(batches, list):
        errs.append("batches must be a list")
        batches = []
    for idx, batch in enumerate(batches):
        if not isinstance(batch, dict):
            errs.append(f"batches[{idx}] is not an object")
            continue
        extra_batch = set(batch) - TRIAGE_BATCH_KEYS
        if extra_batch:
            errs.append(f"batches[{idx}] has unexpected keys: {sorted(extra_batch)}")
        if not (isinstance(batch.get("id"), str) and batch["id"].strip()):
            errs.append(f"batches[{idx}].id must be a non-empty string")
        targets = batch.get("targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(t, str) and t.strip() for t in targets)
        ):
            errs.append(f"batches[{idx}].targets must be a non-empty list of target strings")
        if not (isinstance(batch.get("reason"), str) and batch["reason"].strip()):
            errs.append(f"batches[{idx}].reason must be a non-empty string")
        if batch.get("risk") not in CONFIDENCE:
            errs.append(f"batches[{idx}].risk must be one of {sorted(CONFIDENCE)}")
    return errs


def _validate_triage_context(proposal: dict, backlog_items: list[dict]) -> list[str]:
    errs: list[str] = []
    known = {item["target"] for item in _compact_backlog_items(backlog_items)}
    recs = proposal.get("recommendations") or []
    rec_targets = [rec.get("target") for rec in recs if isinstance(rec, dict)]
    seen = set(rec_targets)
    duplicates = sorted({target for target in rec_targets if rec_targets.count(target) > 1})
    if duplicates:
        errs.append(f"duplicate recommendations for targets: {duplicates}")
    unknown = sorted(target for target in seen if target not in known)
    missing = sorted(known - seen)
    if unknown:
        errs.append(f"recommendations reference unknown targets: {unknown}")
    if missing:
        errs.append(f"missing recommendations for input targets: {missing}")

    batch_ids = {
        batch.get("id") for batch in proposal.get("batches") or [] if isinstance(batch, dict)
    }
    batch_by_target = {
        rec.get("target"): rec.get("batch_id") for rec in recs if isinstance(rec, dict)
    }
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        batch_id = rec.get("batch_id")
        if batch_id is not None and batch_id not in batch_ids:
            errs.append(f"{rec.get('target')} references unknown batch_id {batch_id!r}")
    for batch in proposal.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        batch_id = batch.get("id")
        for target in batch.get("targets") or []:
            if target not in known:
                errs.append(f"batch {batch_id!r} references unknown target {target!r}")
            if batch_by_target.get(target) != batch_id:
                errs.append(
                    f"batch {batch_id!r} includes {target!r} but its recommendation does not use that batch_id"
                )
    return errs


def _baseline_triage(backlog_items: list[dict]) -> dict:
    recommendations = []
    for idx, item in enumerate(_compact_backlog_items(backlog_items), 1):
        action = "monitor" if item.get("lane") == "closer" else "work_now"
        if item.get("task_type") == "epic":
            reason = "Deterministic baseline keeps this visible for epic decomposition."
        elif item.get("lane") == "closer":
            reason = "Deterministic baseline keeps closer PRs visible for inspection."
        else:
            reason = "Deterministic baseline preserves backlog order."
        recommendations.append(
            {
                "target": item["target"],
                "action": action,
                "priority": min(idx, 5),
                "reason": reason,
                "batch_id": None,
            }
        )
    return {
        "summary": "Deterministic baseline preserves visible backlog order.",
        "recommendations": recommendations,
        "batches": [],
        "global_risks": [],
        "confidence": "medium" if recommendations else "high",
    }


# --------------------------------------------------------------------------------------
# AdjudicatorAgent — disputed blocker/veto assessment contract + validation
# --------------------------------------------------------------------------------------
def _compact_adjudication_case(case: dict) -> dict:
    """Keep the role input compact while preserving evidence for ground-truth adjudication."""
    if not isinstance(case, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in (
        "case_type",
        "target",
        "reviewer",
        "source",
        "panel_verdict",
        "gate_verdict",
        "disputed_finding",
        "blocker",
        "acceptance_criteria",
        "ground_truth_evidence",
        "repo_context",
        "prior_decision",
    ):
        if key not in case:
            continue
        value = case[key]
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 2200:
                value = value[:2200].rstrip() + "..."
        compact[key] = value
    return compact


def _adjudicator_prompt(ctx: dict) -> str:
    case = _compact_adjudication_case(ctx.get("case") or {})
    context = (ctx.get("context") or "").strip() or "(none provided)"
    return "\n".join(
        [
            "You are AdjudicatorAgent, the Orchestrator's disputed-blocker judge.",
            "Your job is to assess ONE reviewer veto/blocker against supplied ground-truth evidence and advise",
            "whether the orchestrator should uphold it, reject it, or gather more evidence.",
            "",
            "CRITICAL-EVALUATOR STANCE: adjudicate against evidence, not reviewer confidence. A blocker should",
            "stand only when the supplied ground truth supports it. Reject bare, convention-blind, or contradicted",
            "claims. Use needs_more_evidence when the evidence is insufficient to prove or disprove the claim.",
            "",
            "Rails you must not cross:",
            "- Do NOT emit PASS, FAIL, BLOCKED, verifier_verdict, merge, label, claim, or worker-selection fields.",
            "- Do NOT replace runtime_ac_panel.adjudicate_panel or adversarial.aggregate_veto aggregation math.",
            "- Do NOT delegate, merge, label, claim, create branches, or mutate state.",
            "- recommended_next_step must be an inspection/evidence step, not an execution command.",
            "",
            "Adjudication case JSON:",
            json.dumps(case, indent=2, sort_keys=True),
            "",
            "Additional orchestrator context:",
            context,
            "",
            "Output STRICT JSON only - no prose, no code fences - matching exactly:",
            "{",
            '  "decision":"uphold_blocker|reject_blocker|needs_more_evidence",',
            '  "confidence":"low|medium|high",',
            '  "rationale":"<1-3 sentences tied to ground truth>",',
            '  "evidence_assessment":[',
            '    {"claim":"<reviewer claim>", "status":"supported|contradicted|insufficient",',
            '     "evidence_ref":"<specific supplied evidence ref or null>", "reason":"<why>"}',
            "  ],",
            '  "ground_truth_refs":["<specific supplied evidence ref>", "..."],',
            '  "recommended_next_step":"<human/orchestrator inspection step; no mutation>",',
            '  "evidence_gaps":["<missing evidence needed>", "..."]',
            "}",
        ]
    )


def _validate_adjudicator(proposal: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(proposal, dict):
        return ["proposal is not a JSON object"]
    extra = set(proposal) - ADJUDICATOR_TOP_KEYS
    if extra:
        errs.append(f"unexpected top-level keys: {sorted(extra)}")
    forbidden = {
        "verdict",
        "verifier_verdict",
        "merge",
        "agent",
        "backend",
        "label",
        "claim",
    }
    forbidden_present = sorted(forbidden & set(proposal))
    if forbidden_present:
        errs.append(f"forbidden terminal/mutating keys present: {forbidden_present}")

    decision = proposal.get("decision")
    if decision not in ADJUDICATOR_DECISIONS:
        errs.append(f"decision must be one of {sorted(ADJUDICATOR_DECISIONS)}; got {decision!r}")
    if proposal.get("confidence") not in CONFIDENCE:
        errs.append(
            f"confidence must be one of {sorted(CONFIDENCE)}; got {proposal.get('confidence')!r}"
        )
    if not (isinstance(proposal.get("rationale"), str) and proposal["rationale"].strip()):
        errs.append("rationale must be a non-empty string")
    if not (
        isinstance(proposal.get("recommended_next_step"), str)
        and proposal["recommended_next_step"].strip()
    ):
        errs.append("recommended_next_step must be a non-empty string")
    next_step = str(proposal.get("recommended_next_step") or "").lower()
    if re.search(
        r"\b(merge|push|label|claim|delegate|kill|commit|open pr|create branch)\b",
        next_step,
    ):
        errs.append("recommended_next_step must not contain mutating execution instructions")

    refs = proposal.get("ground_truth_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
        errs.append("ground_truth_refs must be a list of non-empty strings")
    gaps = proposal.get("evidence_gaps")
    if not isinstance(gaps, list) or not all(isinstance(gap, str) and gap.strip() for gap in gaps):
        errs.append("evidence_gaps must be a list of non-empty strings")
    if decision in {"uphold_blocker", "reject_blocker"} and isinstance(refs, list) and not refs:
        errs.append(f"{decision} requires at least one ground_truth_ref")
    if decision == "needs_more_evidence" and isinstance(gaps, list) and not gaps:
        errs.append("needs_more_evidence requires at least one evidence_gap")

    assessments = proposal.get("evidence_assessment")
    if not isinstance(assessments, list) or not assessments:
        errs.append("evidence_assessment must be a non-empty list")
        assessments = []
    for idx, item in enumerate(assessments):
        if not isinstance(item, dict):
            errs.append(f"evidence_assessment[{idx}] is not an object")
            continue
        extra_item = set(item) - ADJUDICATOR_EVIDENCE_KEYS
        if extra_item:
            errs.append(f"evidence_assessment[{idx}] has unexpected keys: {sorted(extra_item)}")
        if not (isinstance(item.get("claim"), str) and item["claim"].strip()):
            errs.append(f"evidence_assessment[{idx}].claim must be a non-empty string")
        if item.get("status") not in {"supported", "contradicted", "insufficient"}:
            errs.append(
                f"evidence_assessment[{idx}].status must be supported|contradicted|insufficient"
            )
        evidence_ref = item.get("evidence_ref")
        if evidence_ref is not None and not (
            isinstance(evidence_ref, str) and evidence_ref.strip()
        ):
            errs.append(f"evidence_assessment[{idx}].evidence_ref must be a string or null")
        if not (isinstance(item.get("reason"), str) and item["reason"].strip()):
            errs.append(f"evidence_assessment[{idx}].reason must be a non-empty string")
    return errs


def _validate_adjudication_case(case: Any) -> list[str]:
    if not isinstance(case, dict):
        return ["case must be a JSON object"]
    errs: list[str] = []
    case_type = str(case.get("case_type") or "").strip()
    if case_type and case_type not in ADJUDICATOR_CASE_TYPES:
        errs.append(f"case_type must be one of {sorted(ADJUDICATOR_CASE_TYPES)}")
    for key in ("target", "disputed_finding", "ground_truth_evidence"):
        value = case.get(key)
        if not value:
            errs.append(f"case.{key} is required")
    return errs


def _baseline_adjudication(case: dict, *, case_errors: list[str] | None = None) -> dict:
    finding = str(
        (case or {}).get("disputed_finding") or (case or {}).get("blocker") or "disputed blocker"
    ).strip()
    gaps = list(case_errors or [])
    if not gaps:
        gaps = [
            "Human/orchestrator inspection is required before accepting or rejecting the disputed blocker."
        ]
    return {
        "decision": "needs_more_evidence",
        "confidence": "medium",
        "rationale": "Deterministic baseline preserves the existing panel/adversarial aggregation and asks for evidence review.",
        "evidence_assessment": [
            {
                "claim": finding or "disputed blocker",
                "status": "insufficient",
                "evidence_ref": None,
                "reason": "No validated AdjudicatorAgent proposal was accepted.",
            }
        ],
        "ground_truth_refs": [],
        "recommended_next_step": "Inspect the cited gate output, test output, repo convention, or diff evidence before acting.",
        "evidence_gaps": gaps,
    }


ROLE_REGISTRY: dict[str, Role] = {
    "redirect": Role(
        name="redirect",
        description=(
            "Diagnose an in-flight/stalled delegated agent from its watch report + acceptance criteria; "
            "decide wait/collect/inspect/redirect/decompose and, for the mutating actions, author the "
            "corrected delegation prompt. route_as='review' is only a PRIOR - learned weights refine the "
            "backend choice over time."
        ),
        route_as="review",
        eligible_backends=frozenset({"gemini", "codex", "cursor", "claude"}),
        mode="full",
        input_keys=("report", "acceptance_criteria"),
        output_keys=(
            "action",
            "reason",
            "confidence",
            "corrected_prompt",
            "switch_agent",
        ),
        build_prompt=_redirect_prompt,
        validate=_validate_redirect,
    ),
    "prompt": Role(
        name="prompt",
        description=(
            "Turn an issue/brief into a complete, scoped delegation prompt with definition-of-done, "
            "acceptance criteria, validation, risk flags, and out-of-scope boundaries. route_as='implement' "
            "is only a PRIOR - learned weights refine backend choice over time."
        ),
        route_as="implement",
        eligible_backends=frozenset({"gemini", "codex", "cursor", "claude", "vibe"}),
        mode="full",
        input_keys=("target", "goal", "task_type"),
        output_keys=(
            "task_type",
            "summary",
            "scoped_prompt",
            "definition_of_done",
            "acceptance_criteria",
            "validation",
            "expected_paths",
            "out_of_scope",
            "risk_flags",
            "confidence",
        ),
        build_prompt=_prompt_agent_prompt,
        validate=_validate_prompt_agent,
    ),
    "decomposer": Role(
        name="decomposer",
        description=(
            "Turn a large/vague goal into a validated epic decomposition plan with dispatchable subtasks, "
            "dependencies, integration order, and re-decomposition triggers. route_as='epic' is only a "
            "PRIOR - learned weights refine backend choice over time."
        ),
        route_as="epic",
        eligible_backends=frozenset({"gemini", "codex", "cursor", "vibe"}),
        mode="full",
        input_keys=("goal",),
        output_keys=("epic", "subtasks", "integration", "re_decomposition_triggers"),
        build_prompt=_decomposer_prompt,
        validate=_validate_decomposer,
    ),
    "triage": Role(
        name="triage",
        description=(
            "Assess a discovered backlog and recommend which visible items to work now, defer, skip, "
            "scope-clarify, monitor, or batch. route_as='review' is only a PRIOR - learned weights refine "
            "backend choice over time. Triage never selects worker agents or mutates backlog state."
        ),
        route_as="review",
        eligible_backends=frozenset({"cursor", "vibe", "gemini", "codex", "claude"}),
        mode="full",
        input_keys=("backlog_items",),
        output_keys=(
            "summary",
            "recommendations",
            "batches",
            "global_risks",
            "confidence",
        ),
        build_prompt=_triage_prompt,
        validate=_validate_triage_agent,
    ),
    "adjudicator": Role(
        name="adjudicator",
        description=(
            "Assess a disputed reviewer blocker/veto against supplied ground-truth evidence and advise "
            "whether to uphold, reject, or gather more evidence. route_as='review' is only a PRIOR - learned "
            "weights refine backend choice over time. Adjudication never replaces deterministic panel or "
            "minority-veto aggregation math."
        ),
        route_as="review",
        eligible_backends=frozenset({"gemini", "codex", "claude"}),
        mode="full",
        input_keys=("case",),
        output_keys=(
            "decision",
            "confidence",
            "rationale",
            "evidence_assessment",
            "ground_truth_refs",
            "recommended_next_step",
            "evidence_gaps",
        ),
        build_prompt=_adjudicator_prompt,
        validate=_validate_adjudicator,
    ),
}


# Generated roles are deliberately segregated from the static production
# roster.  Registration is process-local and shadow-only; restarting the
# process discards it, while the capability ledger retains the audit trail.
GENERATED_ROLE_REGISTRY: dict[str, Role] = {}
_GENERATED_VALUE_TYPES = {"string", "integer", "boolean", "string_list"}
_GENERATED_FORBIDDEN_TEXT = re.compile(
    r"(?:bearer\s+[a-z0-9._~+/-]{8,}|(?:gh[opurs]_|sk-|api[_-]?key)[a-z0-9._-]{8,}|"
    r"(?:token|secret|password|credential)\s*[:=]\s*[^\s]{6,})",
    re.IGNORECASE,
)


def _validate_generated_payload(
    payload: dict[str, Any], schema: dict[str, dict[str, Any]], *, label: str
) -> list[str]:
    """Validate an exact, bounded generated-role payload."""
    if not isinstance(payload, dict):
        return [f"{label} must be an object"]
    errors: list[str] = []
    unknown = sorted(set(payload) - set(schema))
    if unknown:
        errors.append(f"{label} has unsupported keys: {unknown}")
    for key, spec in schema.items():
        required = spec.get("required") is True
        if required and key not in payload:
            errors.append(f"{label} is missing required key: {key}")
            continue
        if key not in payload:
            continue
        value = payload[key]
        type_name = spec.get("type")
        valid = (
            (type_name == "string" and isinstance(value, str))
            or (type_name == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (type_name == "boolean" and isinstance(value, bool))
            or (
                type_name == "string_list"
                and isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
        )
        if type_name not in _GENERATED_VALUE_TYPES or not valid:
            errors.append(f"{label}.{key} must be {type_name}")
            continue
        if isinstance(value, str):
            max_length = int(spec.get("max_length") or 4096)
            if not value.strip() or len(value) > max_length:
                errors.append(f"{label}.{key} is empty or exceeds {max_length} characters")
            if _GENERATED_FORBIDDEN_TEXT.search(value):
                errors.append(f"{label}.{key} contains secret-bearing text")
        if isinstance(value, list):
            max_items = int(spec.get("max_items") or 12)
            if len(value) > max_items:
                errors.append(f"{label}.{key} exceeds {max_items} items")
            if any(not item.strip() or len(item) > 512 for item in value):
                errors.append(f"{label}.{key} contains an empty or oversized item")
            if _GENERATED_FORBIDDEN_TEXT.search(json.dumps(value, sort_keys=True)):
                errors.append(f"{label}.{key} contains secret-bearing text")
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            errors.append(f"{label}.{key} is outside its enum")
    return errors


def _generated_role_prompt(manifest: dict[str, Any], ctx: dict[str, Any]) -> str:
    errors = _validate_generated_payload(ctx, manifest["input_schema"], label="input")
    if errors:
        raise ValueError("; ".join(errors))
    protocol = manifest["prompt_protocol"]
    context_json = json.dumps(ctx, sort_keys=True, separators=(",", ":"))
    if len(context_json) > int(protocol["max_context_chars"]):
        raise ValueError("generated role context exceeds prompt protocol bound")
    return "\n".join(
        [
            protocol["purpose"],
            manifest["authority"],
            *protocol["instructions"],
            "Return exactly one JSON object matching the declared output keys: "
            + ", ".join(sorted(manifest["output_schema"])),
            "Input JSON:",
            context_json,
        ]
    )


def role_from_generated_manifest(manifest: dict[str, Any]) -> Role:
    """Build a Role-compatible, provider/profile-agnostic shadow definition."""
    route_as = str(manifest["route_as"])
    if route_as not in router.ROUTE_TABLE:
        raise ValueError("generated role has unsupported route prior")
    # Resolve eligible backends from the existing router at registration time;
    # the manifest itself never embeds provider/model/profile identity.
    eligible = frozenset(str(item["agent"]) for item in router.ROUTE_TABLE[route_as]["agents"])
    output_schema = manifest["output_schema"]
    return Role(
        name=manifest["name"],
        description=manifest["description"],
        route_as=route_as,
        eligible_backends=eligible,
        mode=None,
        input_keys=tuple(sorted(manifest["input_schema"])),
        output_keys=tuple(sorted(output_schema)),
        build_prompt=lambda ctx: _generated_role_prompt(manifest, ctx),
        validate=lambda proposal: _validate_generated_payload(
            proposal, output_schema, label="output"
        ),
        input_schema=manifest["input_schema"],
        output_schema=output_schema,
        authority=manifest["authority"],
        selector=manifest["selector"],
        capacity_policy=manifest["capacity_policy"],
        prompt_hash=manifest["prompt_hash"],
        lifecycle=manifest["lifecycle"],
        generated=True,
    )


def register_generated_role(manifest: dict[str, Any]) -> Role:
    """Register one validated generated role without changing ROLE_REGISTRY."""
    name = str(manifest.get("name") or "")
    if not name:
        raise ValueError("generated role name is required")
    if name in ROLE_REGISTRY:
        raise ValueError(f"generated role duplicates static role: {name}")
    role = role_from_generated_manifest(manifest)
    existing = GENERATED_ROLE_REGISTRY.get(name)
    if existing and existing.prompt_hash != role.prompt_hash:
        raise ValueError(f"generated role already registered with different contract: {name}")
    GENERATED_ROLE_REGISTRY[name] = role
    _ROLE_INVOCATION_COUNTS.setdefault(name, 0)
    return role


def unregister_generated_role(name: str) -> None:
    GENERATED_ROLE_REGISTRY.pop(name, None)
    _ROLE_INVOCATION_COUNTS.pop(name, None)


def get_role(name: str) -> Role:
    if name in ROLE_REGISTRY:
        return ROLE_REGISTRY[name]
    if name in GENERATED_ROLE_REGISTRY:
        return GENERATED_ROLE_REGISTRY[name]
    raise ValueError(f"unknown role {name!r}")


def _generated_selector_matches(role: Role, context: dict[str, Any]) -> bool:
    selector = role.selector or {}
    value = context.get(selector.get("field"))
    if selector.get("operator") == "equals":
        return value == selector.get("value")
    if selector.get("operator") == "in":
        return value in (selector.get("value") or [])
    return False


def _register_generated_role_capability(manifest: dict[str, Any], ledger_path: Path) -> None:
    capability_id = manifest["capability_id"]
    existing = capabilities.load(ledger_path, create=False) if ledger_path.exists() else {}
    if capability_id in existing:
        old_hash = (existing[capability_id].get("activation_evidence") or {}).get(
            "role_manifest_hash"
        )
        if old_hash != manifest["manifest_id"]:
            raise ValueError("capability already registered with a different generated role")
        return
    lifecycle = manifest["lifecycle"]
    capabilities.register(
        capability_id,
        {
            "status": "shadow",
            "owner": "orchestrator",
            "matcher": manifest["selector"],
            "entrypoint": "roles.py:run_generated_shadow_role",
            "trigger_cadence": "naturally matching supervised shadow tasks",
            "flags_defaults": {
                "shadow_only": True,
                "mutation_allowed": False,
                "profile_bound": False,
            },
            "output_artifact": "schema-validated generated-role proposal",
            "downstream_consumer": "feedback.py:join_role_to_outcome",
            "learning_sink": "feedback role runs and accepted/rejected influence edges",
            "activation_evidence": {"role_manifest_hash": manifest["manifest_id"]},
            "gate_reason": "generated role remains shadow-only and advisory",
            "gate_evidence": "static production roster is unchanged; runner has no dispatch or mutation surface",
            "evidence_threshold": "independent accepted durable outcomes outperform the predecessor before a separate canary decision",
            "activation_deadline": lifecycle["expires_at"],
            "expiry": lifecycle["expires_at"],
            "next_transition": "retired",
            "kill_switch": lifecycle["kill_switch"],
            "rollback": lifecycle["rollback"],
            "predecessor": lifecycle["predecessor"],
        },
        ledger_path,
    )


def run_generated_shadow_role(
    manifest: dict[str, Any],
    *,
    context: dict[str, Any],
    proposal: dict[str, Any],
    target: str,
    backend_agent: str,
    influenced_run_ids: list[str],
    ledger_path: Path,
    env: dict[str, str] | None = None,
    capacity_available: bool = True,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate and record one observational generated-role proposal.

    The caller supplies a proposal already produced in a quarantined role
    attempt.  This function neither invokes a provider nor changes baseline
    behavior; provider/profile identity belongs on that external attempt.
    """
    role = register_generated_role(manifest)
    _register_generated_role_capability(manifest, Path(ledger_path))
    current = int(time.time()) if now is None else int(now)
    lifecycle = manifest["lifecycle"]
    source_env = os.environ if env is None else env
    kill = lifecycle["kill_switch"]
    killed = str(source_env.get(kill["env"], "")) == str(kill["disabled_value"])
    matched = _generated_selector_matches(role, context)
    gate_enabled = not killed and current < int(lifecycle["expires_at"])
    selector = select_role_activation(
        role.name,
        matched=matched,
        gate_enabled=gate_enabled,
        capacity_available=capacity_available,
        max_invocations=int(role.capacity_policy["max_invocations_per_cycle"]),
        reason="generated_role_selector",
        target=target,
        record=False,
    )
    capability_id = manifest["capability_id"]
    event_prefix = hashlib.sha256(
        f"{manifest['manifest_id']}|{target}|{current}".encode()
    ).hexdigest()[:24]
    if matched:
        capabilities.heartbeat(
            capability_id,
            "match",
            path=ledger_path,
            idempotency_key=f"{event_prefix}:match",
        )
    if not selector["invoked"]:
        return {
            "shadow": True,
            "baseline_changed": False,
            "selector": selector,
            "accepted": False,
            "role_run_id": None,
            "errors": [],
        }
    input_errors = _validate_generated_payload(context, role.input_schema or {}, label="input")
    output_errors = role.validate(proposal)
    errors = input_errors + output_errors
    accepted = not errors
    proposal_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    role_run_id = f"role:{role.name}:shadow:{time.time_ns()}"
    feedback.record_role_run(
        role_run_id,
        role.name,
        target,
        backend_agent,
        mode="shadow",
        action="accepted" if accepted else "rejected",
        decision_source="generated-role-shadow",
        proposal={"proposal_hash": proposal_hash},
        rationale=(
            "schema-validated shadow proposal" if accepted else "schema-rejected shadow proposal"
        ),
        ts=current,
    )
    feedback.record_role_selector_event(
        role.name,
        "invoked",
        reason="generated_role_selector",
        target=target,
        matched=True,
        invoked=True,
        accepted=accepted,
        disagreement=not accepted,
        role_run_id=role_run_id,
    )
    capabilities.heartbeat(
        capability_id,
        "invocation",
        ref=role_run_id,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:invocation",
    )
    capabilities.heartbeat(
        capability_id,
        "output",
        ref=proposal_hash,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:output",
    )
    links = [
        feedback.join_role_to_outcome(
            role_run_id,
            run_id,
            accepted=accepted,
            notes="generated-role-shadow-lineage",
        )
        for run_id in influenced_run_ids
    ]
    consumer_ref = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"role_run_id": role_run_id, "targets": sorted(influenced_run_ids)},
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    capabilities.heartbeat(
        capability_id,
        "consumer",
        ref=consumer_ref,
        path=ledger_path,
        idempotency_key=f"{event_prefix}:consumer",
    )
    synced = [link for link in links if link.get("synced")]
    outcome_ref = None
    if accepted and synced:
        outcome_ref = (
            "sha256:" + hashlib.sha256(json.dumps(synced, sort_keys=True).encode()).hexdigest()
        )
        capabilities.heartbeat(
            capability_id,
            "success",
            ref=outcome_ref,
            path=ledger_path,
            idempotency_key=f"{event_prefix}:success",
        )
        capabilities.heartbeat(
            capability_id,
            "outcome",
            ref=outcome_ref,
            path=ledger_path,
            idempotency_key=f"{event_prefix}:outcome",
        )
    for probe, ref in (
        ("producer_probe", manifest["manifest_id"]),
        ("consumer_probe", consumer_ref),
        ("rollback_probe", lifecycle["rollback"]["predecessor"]),
    ):
        capabilities.record_probe(capability_id, probe, passed=True, ref=ref, path=ledger_path)
    if outcome_ref:
        capabilities.record_probe(
            capability_id, "outcome_probe", passed=True, ref=outcome_ref, path=ledger_path
        )
    return {
        "shadow": True,
        "baseline_changed": False,
        "selector": selector,
        "accepted": accepted,
        "role_run_id": role_run_id,
        "proposal_hash": proposal_hash,
        "errors": errors,
        "lineage": links,
        "outcome_ref": outcome_ref,
    }


# --------------------------------------------------------------------------------------
# Routing — reuse the deterministic learned router; restrict to a role's eligible backends
# --------------------------------------------------------------------------------------
def route_role(
    role_name: str,
    cap: dict | None = None,
    *,
    learned: dict | None = None,
    high_leverage: bool = False,
    exploration_rate: float | None = None,
) -> dict | None:
    """Pick the LLM backend for a role via router.select_agent, restricted to eligible_backends.

    Mirrors select_remote_agent's economics: RESERVE seats (claude) are excluded for routine role
    work and only used as a last resort (or when high_leverage=True). Returns the router entry
    {agent, mode, state, capacity_policy, ...} or None when nothing eligible has capacity.
    """
    role = get_role(role_name)
    cap = cap if cap is not None else router.load_capacity()
    learned_all = learned if learned is not None else (router.learned_ranks() or {})
    role_type = feedback.role_task_type(role_name)
    lw = learned_all.get(role_type) if isinstance(learned_all, dict) else None
    if not lw:
        try:
            rows = feedback.current_weights(role_type)
        except Exception:
            rows = []
        if rows:
            lw = {
                row["agent"]: {
                    "rank": i,
                    "n_obs": row.get("n_obs") or 0,
                    "posterior": row.get("posterior"),
                    "score": row.get("score"),
                }
                for i, row in enumerate(rows)
            }
    if not lw and isinstance(learned_all, dict):
        lw = learned_all.get(role.route_as)
    pool = set(role.eligible_backends) if high_leverage else (set(role.eligible_backends) - RESERVE)
    pick = router.select_agent(
        role.route_as, cap, only=pool, learned=lw, exploration_rate=exploration_rate
    )
    if pick is None and not high_leverage:  # last resort: allow reserve seats
        pick = router.select_agent(
            role.route_as,
            cap,
            only=set(role.eligible_backends),
            learned=lw,
            exploration_rate=exploration_rate,
        )
    return pick


def reset_role_invocation_counts() -> None:
    """Reset per-process shadow caps (used at cycle boundaries and by offline tests)."""
    for role_name in _ROLE_INVOCATION_COUNTS:
        _ROLE_INVOCATION_COUNTS[role_name] = 0


def select_role_activation(
    role_name: str,
    *,
    matched: bool,
    gate_enabled: bool,
    capacity_available: bool,
    max_invocations: int = 1,
    reason: str = "matched",
    target: str | None = None,
    disagreement: bool = False,
    record: bool = True,
) -> dict:
    """Return an auditable, bounded selector decision for one typed role.

    This selector never invokes a model.  It makes the important distinction
    between no matching work and matching work withheld by a gate/capacity/cap.
    """
    get_role(role_name)
    invoked_so_far = _ROLE_INVOCATION_COUNTS[role_name]
    if not matched:
        status, why = "no_matching_work", reason or "no_matching_work"
    elif not gate_enabled:
        status, why = "matched_not_invoked", "shadow_gate_disabled"
    elif not capacity_available:
        status, why = "matched_not_invoked", "no_role_capacity"
    elif invoked_so_far >= max(0, int(max_invocations)):
        status, why = "matched_not_invoked", "per_cycle_invocation_cap"
    else:
        status, why = "invoked", reason or "matched"
        _ROLE_INVOCATION_COUNTS[role_name] += 1
    out = {
        "role": role_name,
        "selector_status": status,
        "reason": why,
        "matched": bool(matched),
        "invoked": status == "invoked",
        "disagreement": bool(disagreement),
        "invocation_ordinal": _ROLE_INVOCATION_COUNTS[role_name],
        "max_invocations": max(0, int(max_invocations)),
    }
    if record:
        try:
            feedback.record_role_selector_event(
                role_name,
                status,
                reason=why,
                target=target,
                matched=matched,
                invoked=status == "invoked",
                disagreement=disagreement,
            )
        except Exception as exc:
            out["record_error"] = str(exc)
    return out


def _shadow_gate(env: dict | None) -> bool:
    source = os.environ if env is None else env
    return str(source.get("ORCH_ROLE_SHADOW", "0")).strip() == "1"


def _role_cap(env: dict | None, role_name: str) -> int:
    source = os.environ if env is None else env
    specific = source.get(f"ORCH_{role_name.upper()}_ROLE_MAX_PER_CYCLE")
    raw = specific if specific is not None else source.get("ORCH_ROLE_MAX_PER_CYCLE", "1")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1


def _dispatch_role_matches(assignment: dict) -> dict[str, tuple[bool, str]]:
    detail = "\n".join(
        str(assignment.get(key) or "") for key in ("prompt", "target_detail", "body", "goal")
    ).strip()
    labels = {str(label).lower() for label in assignment.get("labels") or []}
    detail_lower = detail.lower()
    has_acceptance_signal = bool(
        assignment.get("acceptance_criteria")
        or assignment.get("acceptance_gate_ids")
        or (
            "acceptance" in detail_lower
            and any(token in detail_lower for token in ("test", "validate", "verify", "done"))
        )
    )
    underspecified = len(detail) < 180 or not has_acceptance_signal
    high_risk = bool(
        labels & {"risk:high", "security", "runtime-ac", "high-risk"} or assignment.get("high_risk")
    )
    epic = (
        str(assignment.get("task_type") or "") == "epic"
        or str(assignment.get("lane") or "") == "epic_lane"
    )
    return {
        "prompt": (
            underspecified or high_risk,
            "underspecified" if underspecified else "high_risk",
        ),
        "decomposer": (epic, "epic_lane" if epic else "not_epic"),
    }


def activate_dispatch_roles(
    assignment: dict,
    baseline_prompt: str,
    *,
    cwd: str,
    env: dict | None = None,
    cap: dict | None = None,
    dry_run: bool = False,
    prompt_runner=None,
    decomposer_runner=None,
) -> dict:
    """Invoke Prompt/Decomposer at the dispatch seam without changing rails.

    Valid Prompt advice replaces only the authored prompt.  A valid epic plan is
    appended as advisory context after ``epic_lane`` validation.  Agent choice,
    task type, worktree, runtime gates, and all deterministic safety checks remain
    unchanged.  Returned role IDs are stamped onto the downstream run by dispatcher.
    """
    prompt_runner = prompt_runner or run_prompt_agent
    decomposer_runner = decomposer_runner or run_decomposer_agent
    gate = _shadow_gate(env) and not dry_run
    matches = _dispatch_role_matches(assignment)
    out = {
        "prompt": baseline_prompt,
        "accepted_role_run_ids": [],
        "rejected_role_run_ids": [],
        "selectors": [],
        "results": [],
    }
    target = str(assignment.get("target") or "")
    capacity = cap if cap is not None else router.load_capacity()
    for role_name in ("prompt", "decomposer"):
        matched, reason = matches[role_name]
        role_pick = (
            route_role(role_name, cap=capacity, exploration_rate=0.0) if matched and gate else None
        )
        available = bool(role_pick) if matched and gate else True
        selector = select_role_activation(
            role_name,
            matched=matched,
            gate_enabled=gate,
            capacity_available=available,
            max_invocations=_role_cap(env, role_name),
            reason=reason,
            target=target,
            record=not dry_run,
        )
        out["selectors"].append(selector)
        if not selector["invoked"]:
            continue
        common = {
            "target": target,
            "goal": str(assignment.get("goal") or assignment.get("prompt") or target),
            "context": str(assignment.get("context") or ""),
            "repo": target.split("#", 1)[0] if "#" in target else "",
            "cap": capacity,
            "backend": role_pick["agent"] if role_pick else None,
            "dispatch": True,
            "cwd": cwd,
        }
        if role_name == "prompt":
            result = prompt_runner(
                task_type=str(assignment.get("task_type") or "implement"),
                target_detail=str(assignment.get("target_detail") or assignment.get("body") or ""),
                lane=assignment.get("lane"),
                acceptance_criteria=list(assignment.get("acceptance_criteria") or []),
                constraints=list(assignment.get("constraints") or []),
                expected_paths=list(assignment.get("expected_paths") or []),
                **common,
            )
        else:
            result = decomposer_runner(**common)
        role_run_id = result.get("role_run_id")
        accepted = bool(
            role_run_id and result.get("proposal") is not None and not result.get("errors")
        )
        selector["accepted"] = accepted
        try:
            feedback.record_role_selector_event(
                role_name,
                "invoked",
                reason=reason,
                target=target,
                matched=True,
                invoked=True,
                accepted=accepted,
                disagreement=not accepted,
                role_run_id=role_run_id,
            )
        except Exception as exc:
            selector["record_error"] = str(exc)
        if role_run_id:
            key = "accepted_role_run_ids" if accepted else "rejected_role_run_ids"
            out[key].append(role_run_id)
        if accepted and role_name == "prompt":
            out["prompt"] = result["dispatch_prompt"]
        elif accepted and role_name == "decomposer":
            out["prompt"] = (
                out["prompt"].rstrip()
                + "\n\nADVISORY DECOMPOSER PLAN (deterministic epic schema validated; rails remain authoritative):\n"
                + json.dumps(result["proposal"], sort_keys=True)
            )
        out["results"].append(
            {
                "role": role_name,
                "role_run_id": role_run_id,
                "accepted": accepted,
                "decision_source": result.get("decision_source"),
                "errors": list(result.get("errors") or []),
            }
        )
    return out


def activate_tick_triage(
    items: list[dict],
    cap: dict,
    *,
    env: dict | None = None,
    dry_run: bool = False,
    runner=None,
) -> dict:
    """Run one bounded TriageAgent snapshot; never reorder or remove backlog items."""
    runner = runner or run_triage_agent
    gate = _shadow_gate(env) and not dry_run
    matched = bool(items)
    role_pick = route_role("triage", cap=cap, exploration_rate=0.0) if matched and gate else None
    available = bool(role_pick) if matched and gate else True
    selector = select_role_activation(
        "triage",
        matched=matched,
        gate_enabled=gate,
        capacity_available=available,
        max_invocations=_role_cap(env, "triage"),
        reason="bounded_backlog_snapshot",
        target=f"backlog:{len(items)}",
        record=not dry_run,
    )
    if not selector["invoked"]:
        return {"selector": selector, "result": None, "recommendations": {}}
    result = runner(
        backlog_items=items,
        cap=cap,
        backend=role_pick["agent"] if role_pick else None,
        dispatch=True,
        max_items=20,
        cwd=".",
    )
    accepted = bool(
        result.get("role_run_id")
        and result.get("proposal") is not None
        and not result.get("errors")
    )
    selector["accepted"] = accepted
    try:
        feedback.record_role_selector_event(
            "triage",
            "invoked",
            reason="bounded_backlog_snapshot",
            target=f"backlog:{len(items)}",
            matched=True,
            invoked=True,
            accepted=accepted,
            disagreement=not accepted,
            role_run_id=result.get("role_run_id"),
        )
    except Exception as exc:
        selector["record_error"] = str(exc)
    recommendations = {
        row.get("target"): row
        for row in (result.get("advisory_plan") or {}).get("recommendations") or []
        if isinstance(row, dict) and row.get("target")
    }
    return {"selector": selector, "result": result, "recommendations": recommendations}


def adjudication_case_for_disagreement(
    item: dict, gate_status: dict | None, review_status: dict | None
) -> dict | None:
    """Build a case only when two persisted/verifiable verdicts genuinely disagree."""
    gate_verdict = str(
        (gate_status or {}).get("verdict") or item.get("runtime_ac_verdict") or ""
    ).upper()
    review_result = (review_status or {}).get("result") or {}
    panel_verdict = str(
        review_result.get("verdict")
        or item.get("adversarial_verdict")
        or item.get("review_verdict")
        or ""
    ).upper()
    if not gate_verdict or not panel_verdict or gate_verdict == panel_verdict:
        return None
    return {
        "case_type": "runtime_ac" if gate_verdict else "review",
        "target": item.get("target"),
        "gate_verdict": gate_verdict,
        "panel_verdict": panel_verdict,
        "disputed_finding": f"runtime gate {gate_verdict} disagrees with review panel {panel_verdict}",
        "ground_truth_evidence": {
            "runtime_ac": (gate_status or {}).get("evidence_ref")
            or item.get("runtime_ac_evidence_ref")
            or "persisted-runtime-ac",
            "review": (review_status or {}).get("lineage")
            or item.get("review_evidence_ref")
            or "persisted-review",
        },
    }


def activate_adjudicator_disagreement(
    item: dict,
    gate_status: dict | None,
    review_status: dict | None,
    cap: dict,
    *,
    env: dict | None = None,
    dry_run: bool = False,
    runner=None,
) -> dict:
    """Invoke AdjudicatorAgent only for genuine evidence disagreement."""
    runner = runner or run_adjudicator_agent
    case = adjudication_case_for_disagreement(item, gate_status, review_status)
    gate = _shadow_gate(env) and not dry_run
    role_pick = route_role("adjudicator", cap=cap, exploration_rate=0.0) if case and gate else None
    available = bool(role_pick) if case and gate else True
    selector = select_role_activation(
        "adjudicator",
        matched=case is not None,
        gate_enabled=gate,
        capacity_available=available,
        max_invocations=_role_cap(env, "adjudicator"),
        reason="persisted_evidence_disagreement" if case else "evidence_agrees_or_incomplete",
        target=str(item.get("target") or ""),
        disagreement=case is not None,
        record=not dry_run,
    )
    if not selector["invoked"]:
        return {"selector": selector, "case": case, "result": None}
    result = runner(
        case=case,
        cap=cap,
        backend=role_pick["agent"] if role_pick else None,
        dispatch=True,
        cwd=".",
    )
    accepted = bool(
        result.get("role_run_id")
        and result.get("proposal") is not None
        and not result.get("errors")
    )
    selector["accepted"] = accepted
    try:
        feedback.record_role_selector_event(
            "adjudicator",
            "invoked",
            reason="persisted_evidence_disagreement",
            target=str(item.get("target") or ""),
            matched=True,
            invoked=True,
            accepted=accepted,
            disagreement=True,
            role_run_id=result.get("role_run_id"),
        )
    except Exception as exc:
        selector["record_error"] = str(exc)
    return {"selector": selector, "case": case, "result": result}


def proposal_to_policy(proposal: dict) -> dict:
    """Project a role proposal onto the policy_decision shape redirect_plan.plan() consumes."""
    return {
        "action": proposal.get("action"),
        "reason": proposal.get("reason", ""),
        "confidence": proposal.get("confidence", "medium"),
        "advisory": True,
        "source": "redirect_agent",
    }


def _event_stream_message_texts(text: str) -> list[str]:
    """Extract assistant message text from newline-delimited backend event streams."""
    messages: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            message = item.get("text")
            if isinstance(message, str) and message.strip():
                messages.append(message)
        elif event.get("type") == "agent_message":
            message = event.get("text")
            if isinstance(message, str) and message.strip():
                messages.append(message)
    return messages


def _parse_json(text: str | None) -> Any:
    """Tolerant parse of raw JSON, fenced JSON, backend event streams, or first {...} block."""
    if not text:
        return None
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except Exception:
        pass
    for message in reversed(_event_stream_message_texts(candidate)):
        parsed = _parse_json(message)
        if parsed is not None:
            return parsed
    i, j = candidate.find("{"), candidate.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(candidate[i : j + 1])
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------------------
# RedirectAgent shadow runner — propose only, never mutate
# --------------------------------------------------------------------------------------
def run_redirect_agent(
    report: dict,
    acceptance_criteria: str,
    *,
    attempt_history: list | None = None,
    cap: dict | None = None,
    learned: dict | None = None,
    backend: str | None = None,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    high_leverage: bool = False,
    lane: str | None = None,
    task_type: str | None = None,
    next_agent: str | None = None,
    timeout: int = 600,
    exploration_rate: float | None = None,
    record_corpus: bool = False,
    corpus_path: str | None = None,
) -> dict:
    """SHADOW ONLY. Route a backend, build the role prompt, obtain a proposal, and return a dry-run plan.

    Proposal source precedence: proposal_json (offline/replay) -> --dispatch live offload -> none.
    With no proposal (default), it returns the routing + role prompt and a BASELINE plan from
    redirect_policy.decide so callers can see what would happen without spending a backend. A live or
    replayed proposal supplies the action AND the corrected prompt (prompt_override) for the plan.
    Never mutates: apply via `redirect_plan.py --apply --confirm-target` after human/seat review.
    """
    role = ROLE_REGISTRY["redirect"]
    if dispatch:
        _role_capability_event("redirect", "match", metadata={"target": report.get("target")})
    report = dict(report)  # never mutate the caller's report
    attempt_history = attempt_history or []
    ctx = {
        "report": report,
        "acceptance_criteria": acceptance_criteria,
        "attempt_history": attempt_history,
    }
    prompt = role.build_prompt(ctx)

    routing = None
    backend_name = backend
    if backend_name is None:
        routing = route_role(
            "redirect",
            cap=cap,
            learned=learned,
            high_leverage=high_leverage,
            exploration_rate=exploration_rate,
        )
        backend_name = routing["agent"] if routing else None

    baseline = redirect_policy.decide(report, attempt_history)  # already policy_decision-shaped

    proposal: dict | None = None
    errors: list[str] = []
    raw_output: str | None = None
    backend_run_id: str | None = None
    backend_error_detail: str | None = None
    backend_model: str | None = None

    if proposal_json is not None:
        proposal = proposal_json
    elif dispatch:
        if not backend_name:
            errors.append("no eligible backend has capacity for the redirect role")
        else:
            _role_capability_event("redirect", "invocation", metadata={"backend": backend_name})
            res = dispatcher.offload(
                backend_name,
                prompt,
                cwd=str(report.get("worktree") or "."),
                mode=role.mode,
                timeout=timeout,
            )
            backend_run_id = res.get("run_id")
            backend_model = res.get("model")
            raw_output = res.get("output", "")
            backend_error_detail = _backend_error_detail(res)
            if res.get("exit") not in (0, None):
                errors.append(f"backend exit={res.get('exit')} {res.get('error') or ''}".strip())
            proposal = _parse_json(raw_output)
            if proposal is None:
                errors.append("could not parse a JSON proposal from the backend output")

    if proposal is not None:
        verrs = role.validate(proposal)
        if verrs:
            errors.extend(verrs)
            proposal = None  # reject invalid; fall back to the baseline policy

    if proposal is not None:
        report["policy_decision"] = proposal_to_policy(proposal)
        override = proposal.get("corrected_prompt") or None
        plan_obj = redirect_plan.plan(
            report,
            next_agent=(proposal.get("switch_agent") or next_agent),
            lane=lane,
            task_type=task_type,
            prompt_override=override,
        )
        decision_source = "redirect_agent"
    else:
        report["policy_decision"] = dict(baseline)
        plan_obj = redirect_plan.plan(report, next_agent=next_agent, lane=lane, task_type=task_type)
        decision_source = "baseline_policy"

    role_run_id: str | None = None
    role_record_error: str | None = None
    corpus_record: dict | None = None
    if dispatch and backend_name:
        role_run_id = f"role:redirect:{backend_name}:{time.time_ns()}"
        try:
            feedback.record_role_run(
                role_run_id,
                "redirect",
                str(report.get("target") or "redirect-role"),
                backend_name,
                reasoning_level=role.mode,
                backend_run_id=backend_run_id,
                action=(proposal or {}).get("action") if proposal else None,
                decision_source=decision_source,
                proposal=proposal,
                # Cost telemetry, not provenance: 450 local role runs had model=NULL. Read from
                # backend_model, NOT res: a replayed/offline proposal (proposal_json) never calls
                # dispatcher.offload, so `res` is unbound there. That raised an unbound-local
                # NameError which the `except` below swallowed into role_record_error and reset
                # role_run_id to None -- so no role run was recorded, attach_role_lineage never
                # fired, the dispatch command carried no --influenced-by-role-run-id, and no
                # role->outcome edge could ever form on the replay path. (2026-08-21)
                model=backend_model,
            )
        except Exception as exc:
            role_record_error = str(exc)
            role_run_id = None
        if record_corpus:
            try:
                import redirect_shadow

                corpus_record = redirect_shadow.record_proposal(
                    role_run_id=role_run_id,
                    report=report,
                    acceptance_criteria=acceptance_criteria,
                    proposal=proposal,
                    baseline=baseline,
                    decision_source=decision_source,
                    errors=errors,
                    backend=backend_name,
                    backend_run_id=backend_run_id,
                    plan=plan_obj,
                    raw_output=raw_output,
                    backend_error_detail=backend_error_detail,
                    corpus_path=(Path(corpus_path) if corpus_path else redirect_shadow.CORPUS_PATH),
                )
            except Exception as exc:
                # Corpus recording is best-effort; don't fail the main path.
                corpus_record = {"recorded": False, "error": str(exc)}

    if role_run_id and proposal is not None and not errors:
        plan_obj = redirect_plan.attach_role_lineage(plan_obj, role_run_id)

    if dispatch and proposal is not None and not errors and backend_run_id:
        _role_capability_event(
            "redirect",
            "success",
            ref=backend_run_id,
            metadata={"decision_source": decision_source},
        )

    return {
        "role": "redirect",
        "shadow": True,
        "mutates_state": False,
        "backend": backend_name,
        "role_run_id": role_run_id,
        "backend_run_id": backend_run_id,
        "role_record_error": role_record_error,
        "corpus_record": corpus_record,
        "routing": routing,
        "decision_source": decision_source,
        "prompt": prompt,
        "proposal": proposal,
        "baseline": baseline,
        "errors": errors,
        "plan": plan_obj,
        "raw_output": raw_output,
        "backend_error_detail": backend_error_detail,
    }


def run_prompt_agent(
    *,
    target: str,
    goal: str,
    task_type: str = "implement",
    target_detail: str = "",
    context: str = "",
    repo: str = "",
    lane: str | None = None,
    acceptance_criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    expected_paths: list[str] | None = None,
    cap: dict | None = None,
    learned: dict | None = None,
    backend: str | None = None,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    high_leverage: bool = False,
    cwd: str = ".",
    timeout: int = 600,
    exploration_rate: float | None = None,
) -> dict:
    """SHADOW ONLY. Author a scoped delegation prompt; never delegates or mutates state."""
    role = ROLE_REGISTRY["prompt"]
    if dispatch:
        _role_capability_event("prompt", "match", metadata={"target": target})
    task_type = task_type or "implement"
    ctx = {
        "target": target,
        "goal": goal,
        "task_type": task_type,
        "target_detail": target_detail,
        "context": context,
        "repo": repo,
        "lane": lane,
        "acceptance_criteria": acceptance_criteria or [],
        "constraints": constraints or [],
        "expected_paths": expected_paths or [],
    }
    prompt = role.build_prompt(ctx)

    routing = None
    backend_name = backend
    if backend_name is None:
        routing = route_role(
            "prompt",
            cap=cap,
            learned=learned,
            high_leverage=high_leverage,
            exploration_rate=exploration_rate,
        )
        backend_name = routing["agent"] if routing else None

    baseline_prompt = dispatcher.build_prompt(task_type, target, target_detail or goal, lane=lane)
    proposal: dict | None = None
    errors: list[str] = []
    raw_output: str | None = None
    backend_run_id: str | None = None
    backend_error_detail: str | None = None
    backend_model: str | None = None

    if proposal_json is not None:
        proposal = proposal_json
    elif dispatch:
        if not backend_name:
            errors.append("no eligible backend has capacity for the prompt role")
        else:
            _role_capability_event("prompt", "invocation", metadata={"backend": backend_name})
            res = dispatcher.offload(backend_name, prompt, cwd=cwd, mode=role.mode, timeout=timeout)
            backend_run_id = res.get("run_id")
            backend_model = res.get("model")
            raw_output = res.get("output", "")
            backend_error_detail = _backend_error_detail(res)
            if res.get("exit") not in (0, None):
                errors.append(f"backend exit={res.get('exit')} {res.get('error') or ''}".strip())
            proposal = _parse_json(raw_output)
            if proposal is None:
                errors.append("could not parse a JSON proposal from the backend output")

    if proposal is not None:
        verrs = role.validate(proposal)
        if proposal.get("task_type") != task_type:
            verrs.append(
                f"task_type must stay on the deterministic rail-selected value {task_type!r}; "
                f"got {proposal.get('task_type')!r}"
            )
        if verrs:
            errors.extend(verrs)
            proposal = None

    if proposal is not None:
        dispatch_prompt = _prompt_agent_dispatch_prompt(proposal, target=target, lane=lane)
        decision_source = "prompt_agent"
    else:
        dispatch_prompt = baseline_prompt
        decision_source = "baseline_dispatcher"

    role_run_id: str | None = None
    role_record_error: str | None = None
    if dispatch and backend_name:
        role_run_id = f"role:prompt:{backend_name}:{time.time_ns()}"
        try:
            feedback.record_role_run(
                role_run_id,
                "prompt",
                target or "prompt-role",
                backend_name,
                reasoning_level=role.mode,
                backend_run_id=backend_run_id,
                action=proposal.get("task_type") if proposal else None,
                decision_source=decision_source,
                proposal=proposal,
                # Cost telemetry, not provenance; None on the replay path (see run_redirect_agent).
                model=backend_model,
            )
        except Exception as exc:
            role_record_error = str(exc)
            role_run_id = None

    if dispatch and proposal is not None and not errors and backend_run_id:
        _role_capability_event(
            "prompt",
            "success",
            ref=backend_run_id,
            metadata={"decision_source": decision_source},
        )

    return {
        "role": "prompt",
        "shadow": True,
        "mutates_state": False,
        "backend": backend_name,
        "role_run_id": role_run_id,
        "backend_run_id": backend_run_id,
        "role_record_error": role_record_error,
        "routing": routing,
        "decision_source": decision_source,
        "prompt": prompt,
        "proposal": proposal,
        "errors": errors,
        "baseline_prompt": baseline_prompt,
        "dispatch_prompt": dispatch_prompt,
        "raw_output": raw_output,
        "backend_error_detail": backend_error_detail,
    }


def run_decomposer_agent(
    *,
    goal: str,
    repo: str = "",
    target: str = "",
    context: str = "",
    subtask_count: int | None = None,
    cap: dict | None = None,
    learned: dict | None = None,
    backend: str | None = None,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    high_leverage: bool = False,
    cwd: str = ".",
    timeout: int = 600,
    exploration_rate: float | None = None,
) -> dict:
    """SHADOW ONLY. Author/validate an epic decomposition plan; never dispatches subtasks."""
    role = ROLE_REGISTRY["decomposer"]
    if dispatch:
        _role_capability_event("decomposer", "match", metadata={"target": target or repo})
    ctx = {
        "goal": goal,
        "repo": repo,
        "target": target,
        "context": context,
        "subtask_count": subtask_count,
    }
    prompt = role.build_prompt(ctx)

    routing = None
    backend_name = backend
    if backend_name is None:
        routing = route_role(
            "decomposer",
            cap=cap,
            learned=learned,
            high_leverage=high_leverage,
            exploration_rate=exploration_rate,
        )
        backend_name = routing["agent"] if routing else None

    baseline_prompt = epic_lane.build_planner_prompt(
        goal=goal,
        repo=repo or None,
        target=target or None,
        context=context,
        subtask_count=subtask_count,
    )
    proposal: dict | None = None
    errors: list[str] = []
    raw_output: str | None = None
    backend_run_id: str | None = None
    backend_error_detail: str | None = None
    backend_model: str | None = None

    if proposal_json is not None:
        proposal = proposal_json
    elif dispatch:
        if not backend_name:
            errors.append("no eligible backend has capacity for the decomposer role")
        else:
            _role_capability_event("decomposer", "invocation", metadata={"backend": backend_name})
            res = dispatcher.offload(backend_name, prompt, cwd=cwd, mode=role.mode, timeout=timeout)
            backend_run_id = res.get("run_id")
            backend_model = res.get("model")
            raw_output = res.get("output", "")
            backend_error_detail = _backend_error_detail(res)
            if res.get("exit") not in (0, None):
                errors.append(f"backend exit={res.get('exit')} {res.get('error') or ''}".strip())
            proposal = _parse_json(raw_output)
            if proposal is None:
                errors.append("could not parse a JSON decomposition plan from the backend output")

    dispatch_prompts: list[dict] = []
    if proposal is not None:
        verrs = role.validate(proposal)
        if verrs:
            errors.extend(verrs)
            proposal = None
        else:
            dispatch_prompts = epic_lane.build_dispatch_prompts(proposal)

    if proposal is not None:
        decision_source = "decomposer_agent"
    else:
        decision_source = "baseline_epic_lane"

    role_run_id: str | None = None
    role_record_error: str | None = None
    if dispatch and backend_name:
        role_run_id = f"role:decomposer:{backend_name}:{time.time_ns()}"
        try:
            feedback.record_role_run(
                role_run_id,
                "decomposer",
                target or repo or "decomposer-role",
                backend_name,
                reasoning_level=role.mode,
                backend_run_id=backend_run_id,
                action=(f"{len(dispatch_prompts)}-subtasks" if proposal else None),
                decision_source=decision_source,
                proposal=proposal,
                # Cost telemetry, not provenance; None on the replay path (see run_redirect_agent).
                model=backend_model,
            )
        except Exception as exc:
            role_record_error = str(exc)
            role_run_id = None

    if dispatch and proposal is not None and not errors and backend_run_id:
        _role_capability_event(
            "decomposer",
            "success",
            ref=backend_run_id,
            metadata={"decision_source": decision_source},
        )

    return {
        "role": "decomposer",
        "shadow": True,
        "mutates_state": False,
        "backend": backend_name,
        "role_run_id": role_run_id,
        "backend_run_id": backend_run_id,
        "role_record_error": role_record_error,
        "routing": routing,
        "decision_source": decision_source,
        "prompt": prompt,
        "proposal": proposal,
        "errors": errors,
        "baseline_prompt": baseline_prompt,
        "dispatch_prompts": dispatch_prompts,
        "raw_output": raw_output,
        "backend_error_detail": backend_error_detail,
    }


def run_triage_agent(
    *,
    backlog_items: list[dict],
    context: str = "",
    max_items: int = 20,
    cap: dict | None = None,
    learned: dict | None = None,
    backend: str | None = None,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    high_leverage: bool = False,
    cwd: str = ".",
    timeout: int = 600,
    exploration_rate: float | None = None,
) -> dict:
    """SHADOW ONLY. Advise backlog priority/batching; never claims, delegates, or mutates state."""
    role = ROLE_REGISTRY["triage"]
    if dispatch:
        _role_capability_event("triage", "match", metadata={"visible_items": len(backlog_items)})
    max_items = max(0, int(max_items))
    visible_items = _compact_backlog_items(backlog_items, max_items=max_items)
    omitted_count = max(0, len(backlog_items) - len(visible_items))
    cap_for_prompt = cap if cap is not None else router.load_capacity()
    ctx = {
        "backlog_items": visible_items,
        "capacity": cap_for_prompt,
        "context": context,
        "max_items": max_items,
        "omitted_count": omitted_count,
    }
    prompt = role.build_prompt(ctx)

    routing = None
    backend_name = backend
    if backend_name is None:
        routing = route_role(
            "triage",
            cap=cap,
            learned=learned,
            high_leverage=high_leverage,
            exploration_rate=exploration_rate,
        )
        backend_name = routing["agent"] if routing else None

    baseline = _baseline_triage(visible_items)
    proposal: dict | None = None
    errors: list[str] = []
    raw_output: str | None = None
    backend_run_id: str | None = None
    backend_error_detail: str | None = None
    backend_model: str | None = None

    if proposal_json is not None:
        proposal = proposal_json
    elif dispatch:
        if not backend_name:
            errors.append("no eligible backend has capacity for the triage role")
        else:
            _role_capability_event("triage", "invocation", metadata={"backend": backend_name})
            res = dispatcher.offload(backend_name, prompt, cwd=cwd, mode=role.mode, timeout=timeout)
            backend_run_id = res.get("run_id")
            backend_model = res.get("model")
            raw_output = res.get("output", "")
            backend_error_detail = _backend_error_detail(res)
            if res.get("exit") not in (0, None):
                errors.append(f"backend exit={res.get('exit')} {res.get('error') or ''}".strip())
            proposal = _parse_json(raw_output)
            if proposal is None:
                errors.append("could not parse a JSON triage proposal from the backend output")

    if proposal is not None:
        verrs = role.validate(proposal)
        if not verrs:
            verrs.extend(_validate_triage_context(proposal, visible_items))
        if verrs:
            errors.extend(verrs)
            proposal = None

    if proposal is not None:
        decision_source = "triage_agent"
        advisory_plan = proposal
    else:
        decision_source = "baseline_backlog_order"
        advisory_plan = baseline

    role_run_id: str | None = None
    role_record_error: str | None = None
    if dispatch and backend_name:
        role_run_id = f"role:triage:{backend_name}:{time.time_ns()}"
        try:
            actions: dict[str, Any] = {}
            for rec in advisory_plan.get("recommendations") or []:
                action = rec.get("action")
                actions[action] = actions.get(action, 0) + 1
            action_summary = ",".join(f"{k}:{v}" for k, v in sorted(actions.items())) or None
            feedback.record_role_run(
                role_run_id,
                "triage",
                f"triage:{len(visible_items)}-items",
                backend_name,
                reasoning_level=role.mode,
                backend_run_id=backend_run_id,
                action=action_summary,
                decision_source=decision_source,
                proposal=proposal,
                # Cost telemetry, not provenance; None on the replay path (see run_redirect_agent).
                model=backend_model,
            )
        except Exception as exc:
            role_record_error = str(exc)
            role_run_id = None

    if dispatch and proposal is not None and not errors and backend_run_id:
        _role_capability_event(
            "triage",
            "success",
            ref=backend_run_id,
            metadata={"decision_source": decision_source},
        )

    return {
        "role": "triage",
        "shadow": True,
        "mutates_state": False,
        "backend": backend_name,
        "role_run_id": role_run_id,
        "backend_run_id": backend_run_id,
        "role_record_error": role_record_error,
        "routing": routing,
        "decision_source": decision_source,
        "prompt": prompt,
        "proposal": proposal,
        "errors": errors,
        "baseline": baseline,
        "advisory_plan": advisory_plan,
        "visible_items": visible_items,
        "omitted_count": omitted_count,
        "raw_output": raw_output,
        "backend_error_detail": backend_error_detail,
    }


def run_adjudicator_agent(
    *,
    case: dict,
    context: str = "",
    cap: dict | None = None,
    learned: dict | None = None,
    backend: str | None = None,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    high_leverage: bool = False,
    cwd: str = ".",
    timeout: int = 600,
    exploration_rate: float | None = None,
) -> dict:
    """SHADOW ONLY. Assess a disputed blocker/veto; never mutates or emits final panel verdicts."""
    role = ROLE_REGISTRY["adjudicator"]
    if dispatch:
        _role_capability_event(
            "adjudicator", "match", metadata={"target": (case or {}).get("target")}
        )
    compact_case = _compact_adjudication_case(case or {})
    case_errors = _validate_adjudication_case(compact_case)
    prompt = role.build_prompt({"case": compact_case, "context": context})

    routing = None
    backend_name = backend
    if backend_name is None:
        routing = route_role(
            "adjudicator",
            cap=cap,
            learned=learned,
            high_leverage=high_leverage,
            exploration_rate=exploration_rate,
        )
        backend_name = routing["agent"] if routing else None

    baseline = _baseline_adjudication(compact_case, case_errors=case_errors)
    proposal: dict | None = None
    errors: list[str] = list(case_errors)
    raw_output: str | None = None
    backend_run_id: str | None = None
    backend_error_detail: str | None = None
    backend_model: str | None = None

    if proposal_json is not None:
        proposal = proposal_json
    elif dispatch:
        if not backend_name:
            errors.append("no eligible backend has capacity for the adjudicator role")
        elif case_errors:
            errors.append("case validation failed; refusing live dispatch")
        else:
            _role_capability_event("adjudicator", "invocation", metadata={"backend": backend_name})
            res = dispatcher.offload(backend_name, prompt, cwd=cwd, mode=role.mode, timeout=timeout)
            backend_run_id = res.get("run_id")
            backend_model = res.get("model")
            raw_output = res.get("output", "")
            backend_error_detail = _backend_error_detail(res)
            if res.get("exit") not in (0, None):
                errors.append(f"backend exit={res.get('exit')} {res.get('error') or ''}".strip())
            proposal = _parse_json(raw_output)
            if proposal is None:
                errors.append(
                    "could not parse a JSON adjudication proposal from the backend output"
                )

    if proposal is not None:
        verrs = role.validate(proposal)
        if verrs:
            errors.extend(verrs)
            proposal = None

    if proposal is not None and not case_errors:
        decision_source = "adjudicator_agent"
        advisory_plan = proposal
    else:
        decision_source = "baseline_needs_more_evidence"
        advisory_plan = baseline

    role_run_id: str | None = None
    role_record_error: str | None = None
    if dispatch and backend_name:
        role_run_id = f"role:adjudicator:{backend_name}:{time.time_ns()}"
        try:
            feedback.record_role_run(
                role_run_id,
                "adjudicator",
                str(compact_case.get("target") or "adjudicator-role"),
                backend_name,
                reasoning_level=role.mode,
                backend_run_id=backend_run_id,
                action=advisory_plan.get("decision"),
                decision_source=decision_source,
                proposal=proposal,
                # Cost telemetry, not provenance; None on the replay path (see run_redirect_agent).
                model=backend_model,
            )
        except Exception as exc:
            role_record_error = str(exc)
            role_run_id = None

    if dispatch and proposal is not None and not errors and backend_run_id:
        _role_capability_event(
            "adjudicator",
            "success",
            ref=backend_run_id,
            metadata={"decision_source": decision_source},
        )

    return {
        "role": "adjudicator",
        "shadow": True,
        "mutates_state": False,
        "backend": backend_name,
        "role_run_id": role_run_id,
        "backend_run_id": backend_run_id,
        "role_record_error": role_record_error,
        "routing": routing,
        "decision_source": decision_source,
        "prompt": prompt,
        "proposal": proposal,
        "errors": errors,
        "baseline": baseline,
        "advisory_plan": advisory_plan,
        "case": compact_case,
        "raw_output": raw_output,
        "backend_error_detail": backend_error_detail,
    }


def format_human(result: dict) -> str:
    lines = [
        f"role={result.get('role')} backend={result.get('backend') or '-'} "
        f"decision_source={result.get('decision_source')} shadow={result.get('shadow')}",
    ]
    if result.get("role_run_id"):
        lines.append(f"role_run_id={result['role_run_id']}")
    if result.get("role_record_error"):
        lines.append(f"role_record_error={result['role_record_error']}")
    corpus_rec = result.get("corpus_record")
    if corpus_rec and isinstance(corpus_rec, dict) and corpus_rec.get("recorded"):
        lines.append(f"corpus_recorded={corpus_rec.get('corpus')}")
    if result.get("errors"):
        lines.append("errors: " + "; ".join(result["errors"]))
    lines.append("")
    lines.append(redirect_plan.format_human(result.get("plan") or {}))
    lines.append("")
    lines.append("SHADOW: advisory only - nothing was changed. To act after review:")
    lines.append(
        "  python3 redirect_plan.py --apply --confirm-target <exact-target> --next-agent <agent> "
        "--report-json <report-with-policy_decision>.json"
    )
    return "\n".join(lines)


def format_prompt_human(result: dict) -> str:
    lines = [
        f"role={result.get('role')} backend={result.get('backend') or '-'} "
        f"decision_source={result.get('decision_source')} shadow={result.get('shadow')}",
    ]
    if result.get("role_run_id"):
        lines.append(f"role_run_id={result['role_run_id']}")
    if result.get("role_record_error"):
        lines.append(f"role_record_error={result['role_record_error']}")
    if result.get("errors"):
        lines.append("errors: " + "; ".join(result["errors"]))
    lines.append("")
    lines.append("--- dispatch_prompt ---")
    lines.append((result.get("dispatch_prompt") or "").rstrip())
    lines.append("")
    lines.append("SHADOW: advisory prompt only - nothing was delegated or changed.")
    return "\n".join(lines)


def format_decomposer_human(result: dict) -> str:
    lines = [
        f"role={result.get('role')} backend={result.get('backend') or '-'} "
        f"decision_source={result.get('decision_source')} shadow={result.get('shadow')}",
    ]
    if result.get("role_run_id"):
        lines.append(f"role_run_id={result['role_run_id']}")
    if result.get("role_record_error"):
        lines.append(f"role_record_error={result['role_record_error']}")
    if result.get("errors"):
        lines.append("errors: " + "; ".join(result["errors"]))
    prompts = result.get("dispatch_prompts") or []
    lines.append(f"dispatch_prompts={len(prompts)}")
    if prompts:
        lines.append("")
        lines.append("--- dispatch_prompts ---")
        for item in prompts:
            lines.append(
                f"{item.get('id')}: lane={item.get('lane')} task_type={item.get('task_type')} "
                f"deps={item.get('dependencies') or []}"
            )
            preview = (item.get("prompt") or "").strip()
            if preview:
                lines.append(preview[:600] + ("..." if len(preview) > 600 else ""))
                lines.append("")
    else:
        lines.append("")
        lines.append("--- baseline_planner_prompt ---")
        lines.append((result.get("baseline_prompt") or "").rstrip())
    lines.append("SHADOW: advisory decomposition only - no subtasks were delegated or changed.")
    return "\n".join(lines)


def format_triage_human(result: dict) -> str:
    lines = [
        f"role={result.get('role')} backend={result.get('backend') or '-'} "
        f"decision_source={result.get('decision_source')} shadow={result.get('shadow')}",
    ]
    if result.get("role_run_id"):
        lines.append(f"role_run_id={result['role_run_id']}")
    if result.get("role_record_error"):
        lines.append(f"role_record_error={result['role_record_error']}")
    if result.get("errors"):
        lines.append("errors: " + "; ".join(result["errors"]))
    plan = result.get("advisory_plan") or {}
    lines.append(
        f"visible_items={len(result.get('visible_items') or [])} omitted={result.get('omitted_count') or 0}"
    )
    lines.append("")
    lines.append("--- triage ---")
    if plan.get("summary"):
        lines.append(plan["summary"])
    for rec in plan.get("recommendations") or []:
        batch = f" batch={rec.get('batch_id')}" if rec.get("batch_id") else ""
        lines.append(
            f"- p{rec.get('priority')} {rec.get('action')} {rec.get('target')}{batch}: "
            f"{rec.get('reason')}"
        )
    batches = plan.get("batches") or []
    if batches:
        lines.append("")
        lines.append("--- batches ---")
        for batch in batches:
            lines.append(
                f"- {batch.get('id')} risk={batch.get('risk')}: "
                f"{', '.join(batch.get('targets') or [])} - {batch.get('reason')}"
            )
    risks = plan.get("global_risks") or []
    if risks:
        lines.append("")
        lines.append("--- risks ---")
        lines.extend(f"- {risk}" for risk in risks)
    lines.append("")
    lines.append(
        "SHADOW: advisory triage only - no claims, labels, delegates, or routing decisions were changed."
    )
    return "\n".join(lines)


def format_adjudicator_human(result: dict) -> str:
    lines = [
        f"role={result.get('role')} backend={result.get('backend') or '-'} "
        f"decision_source={result.get('decision_source')} shadow={result.get('shadow')}",
    ]
    if result.get("role_run_id"):
        lines.append(f"role_run_id={result['role_run_id']}")
    if result.get("role_record_error"):
        lines.append(f"role_record_error={result['role_record_error']}")
    if result.get("errors"):
        lines.append("errors: " + "; ".join(result["errors"]))
    case = result.get("case") or {}
    plan = result.get("advisory_plan") or {}
    lines.append(f"target={case.get('target') or '-'} case_type={case.get('case_type') or '-'}")
    lines.append("")
    lines.append("--- adjudication ---")
    lines.append(f"decision={plan.get('decision')} confidence={plan.get('confidence')}")
    if plan.get("rationale"):
        lines.append(plan["rationale"])
    assessments = plan.get("evidence_assessment") or []
    if assessments:
        lines.append("")
        lines.append("--- evidence_assessment ---")
        for item in assessments:
            ref = f" [{item.get('evidence_ref')}]" if item.get("evidence_ref") else ""
            lines.append(f"- {item.get('status')}{ref}: {item.get('claim')} - {item.get('reason')}")
    refs = plan.get("ground_truth_refs") or []
    if refs:
        lines.append("")
        lines.append("--- ground_truth_refs ---")
        lines.extend(f"- {ref}" for ref in refs)
    gaps = plan.get("evidence_gaps") or []
    if gaps:
        lines.append("")
        lines.append("--- evidence_gaps ---")
        lines.extend(f"- {gap}" for gap in gaps)
    if plan.get("recommended_next_step"):
        lines.append("")
        lines.append("--- next_step ---")
        lines.append(plan["recommended_next_step"])
    lines.append("")
    lines.append(
        "SHADOW: advisory adjudication only - panel/adversarial aggregation, gates, merges, labels, "
        "claims, and delegation were not changed."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Selftest (offline - no live LLM, no capacity file)
# --------------------------------------------------------------------------------------
def _selftest() -> None:
    import contextlib
    import io
    import tempfile

    fake_cap = {"agents": {a: {"state": "ok"} for a in ("gemini", "codex", "cursor", "vibe")}}
    old_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="roles-selftest-")
    feedback.DB_PATH = Path(tmp) / "roles.db"

    try:
        # route_role: excludes RESERVE (claude) by default, and only routes within eligible backends.
        pick = route_role("redirect", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert (
            pick is not None and pick["agent"] in ROLE_REGISTRY["redirect"].eligible_backends
        ), pick
        assert pick["agent"] != "claude", pick
        prompt_pick = route_role("prompt", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert (
            prompt_pick is not None
            and prompt_pick["agent"] in ROLE_REGISTRY["prompt"].eligible_backends
        ), prompt_pick
        assert prompt_pick["agent"] != "claude", prompt_pick
        decomposer_pick = route_role("decomposer", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert (
            decomposer_pick is not None
            and decomposer_pick["agent"] in ROLE_REGISTRY["decomposer"].eligible_backends
        ), decomposer_pick
        assert decomposer_pick["agent"] != "claude", decomposer_pick
        triage_pick = route_role("triage", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert (
            triage_pick is not None
            and triage_pick["agent"] in ROLE_REGISTRY["triage"].eligible_backends
        ), triage_pick
        assert triage_pick["agent"] != "claude", triage_pick
        adjudicator_pick = route_role("adjudicator", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert (
            adjudicator_pick is not None
            and adjudicator_pick["agent"] in ROLE_REGISTRY["adjudicator"].eligible_backends
        ), adjudicator_pick
        assert adjudicator_pick["agent"] != "claude", adjudicator_pick

        # Role-specific learned weights override the generic route_as prior.
        feedback.record_role_run("role-pref-cursor", "redirect", "o/r#role", "cursor")
        feedback.record_outcome(
            "role-pref-cursor", adjudicated_verdict="PASS", durability="durable"
        )
        for idx in range(6):
            rid = f"role-bad-codex-{idx}"
            feedback.record_role_run(rid, "redirect", "o/r#role", "codex")
            feedback.record_outcome(rid, adjudicated_verdict="PASS", durability="reverted")
        feedback.relearn_quality(
            {feedback.role_task_type("redirect"): {"codex": 0.7, "cursor": 0.5}}
        )
        learned_pick = route_role("redirect", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert learned_pick and learned_pick["agent"] == "cursor", learned_pick

        # last-resort: when only claude has capacity, fall back to the reserve seat.
        claude_only = {"agents": {"claude": {"state": "ok"}}}
        pick2 = route_role("redirect", cap=claude_only, learned={}, exploration_rate=0.0)
        assert pick2 is not None and pick2["agent"] == "claude", pick2

        # validation: a good redirect proposal passes; bad ones are caught.
        good = {
            "action": "redirect",
            "reason": "auth failure root cause",
            "confidence": "high",
            "corrected_prompt": "Re-run with a valid token; scope to src/.",
            "switch_agent": None,
        }
        assert _validate_redirect(good) == [], _validate_redirect(good)
        assert _validate_redirect(
            {
                "action": "redirect",
                "reason": "x",
                "confidence": "high",
                "corrected_prompt": "",
            }
        ), "empty corrected_prompt must fail"
        assert _validate_redirect(
            {"action": "frobnicate", "reason": "x", "confidence": "high"}
        ), "bad action must fail"

        good_prompt = {
            "task_type": "implement",
            "summary": "Implement the LMS progress summary.",
            "scoped_prompt": (
                "Implement stranske/LMS#12 by adding the progress summary only. Acceptance criteria: "
                "show completed lesson counts. Validation: run pytest tests/test_progress.py. "
                "Commit, push, and open a PR after validation."
            ),
            "definition_of_done": ["Progress summary appears for seeded learners"],
            "acceptance_criteria": ["Completed lesson count is visible"],
            "validation": ["pytest tests/test_progress.py"],
            "expected_paths": ["app/progress.py", "tests/test_progress.py"],
            "out_of_scope": ["Do not redesign course navigation"],
            "risk_flags": ["Existing fixtures may need a seeded enrollment"],
            "confidence": "high",
        }
        assert _validate_prompt_agent(good_prompt) == [], _validate_prompt_agent(good_prompt)
        bad_prompt = dict(good_prompt)
        bad_prompt["validation"] = []
        assert _validate_prompt_agent(bad_prompt), "empty validation list must fail"

        # tolerant JSON parse (fenced + trailing prose).
        assert (
            _parse_json('```json\n{"action":"wait","reason":"r","confidence":"low"}\n```')["action"]
            == "wait"
        )
        assert (
            _parse_json('here you go {"action":"inspect","reason":"r","confidence":"low"} done')[
                "action"
            ]
            == "inspect"
        )
        codex_events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_0",
                            "type": "agent_message",
                            "text": json.dumps(
                                {
                                    "action": "collect",
                                    "reason": "done",
                                    "confidence": "high",
                                    "corrected_prompt": None,
                                    "switch_agent": None,
                                }
                            ),
                        },
                    }
                ),
            ]
        )
        assert _parse_json(codex_events)["action"] == "collect"

        report = {
            "agent": "cursor",
            "target": "stranske/Repo#7",
            "lane": "opener",
            "task_type": "implement",
            "pid": 4242,
            "log": "/tmp/a.log",
            "worktree": "/tmp/wt",
            "base_ref": "origin/main",
            "expected_paths": ["src"],
            "state": "stalled",
            "recommended_action": "inspect",
            "signals": {"pid_alive": True, "has_worktree_changes": False},
            "hints": [{"kind": "auth", "detail": "401 Unauthorized"}],
            "drift": {"severity": "none", "findings": []},
            "log_tail": "HTTP 401\n",
        }

        # prompt builder embeds the AC and demands JSON.
        prompt = _redirect_prompt(
            {"report": report, "acceptance_criteria": "All endpoints return 200."}
        )
        assert (
            "All endpoints return 200." in prompt and "STRICT JSON" in prompt
        ), "prompt must carry AC + JSON contract"

        # replayed proposal -> the agent's corrected prompt flows into the plan; still dry-run + confirm-gated.
        proposed = {
            "action": "redirect",
            "reason": "stale auth token",
            "confidence": "high",
            "corrected_prompt": "RETRY: export a fresh token, keep the diff in src/, satisfy the AC, push, open PR.",
            "switch_agent": "codex",
        }
        res = run_redirect_agent(
            report,
            "All endpoints return 200.",
            proposal_json=proposed,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert res["mutates_state"] is False and res["plan"]["dry_run_only"] is True, res
        assert res["decision_source"] == "redirect_agent", res
        assert res["role_run_id"] is None and res["backend_run_id"] is None, res
        assert res["plan"]["action"] == "redirect" and res["plan"]["requires_confirmation"], res[
            "plan"
        ]
        assert res["plan"]["prompt_text"] == proposed["corrected_prompt"], res["plan"][
            "prompt_text"
        ]
        # switch_agent becomes the next worker in the delegate step.
        delegate_cmd = res["plan"]["steps"][-1]["commands"][0]
        assert "codex" in delegate_cmd, delegate_cmd

        # invalid proposal -> reject + fall back to the deterministic baseline policy.
        bad = run_redirect_agent(
            report,
            "AC",
            proposal_json={
                "action": "redirect",
                "reason": "x",
                "confidence": "high",
                "corrected_prompt": "",
            },
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert bad["decision_source"] == "baseline_policy" and bad["errors"], bad
        # baseline for a stalled + auth-hint report is a high-confidence redirect (deterministic).
        assert bad["plan"]["action"] == "redirect", bad["plan"]

        # no proposal, no dispatch -> baseline plan + the role prompt are returned, still no mutation.
        shadow = run_redirect_agent(report, "AC", cap=fake_cap, learned={}, exploration_rate=0.0)
        assert shadow["decision_source"] == "baseline_policy" and shadow["prompt"], shadow
        assert shadow["mutates_state"] is False, shadow

        old_offload = dispatcher.offload

        def fake_gemini_offload(agent, prompt, cwd=".", mode=None, timeout=600):
            return {
                "agent": agent,
                "exit": 70,
                "output": "",
                "error": "agent returned no stdout; agy log tail captured",
                "run_id": "offload:gemini:selftest",
                "agent_log_tail": "neither PlanModel nor RequestedModel specified. You must specify a valid model.",
            }

        try:
            dispatcher.offload = fake_gemini_offload
            gemini_failure = run_redirect_agent(
                report,
                "AC",
                backend="gemini",
                dispatch=True,
                cap=fake_cap,
                learned={},
                exploration_rate=0.0,
            )
            assert gemini_failure["decision_source"] == "baseline_policy", gemini_failure
            assert "agy log tail" in "; ".join(gemini_failure["errors"]), gemini_failure
            assert "PlanModel" in (gemini_failure["backend_error_detail"] or ""), gemini_failure
        finally:
            dispatcher.offload = old_offload

        prompt_res = run_prompt_agent(
            target="stranske/LMS#12",
            goal="Add a learner progress summary.",
            task_type="implement",
            target_detail="Instructor needs to see completed lesson counts.",
            proposal_json=good_prompt,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            prompt_res["mutates_state"] is False and prompt_res["decision_source"] == "prompt_agent"
        ), prompt_res
        assert "Definition of done:" in prompt_res["dispatch_prompt"], prompt_res["dispatch_prompt"]
        assert "pytest tests/test_progress.py" in prompt_res["dispatch_prompt"], prompt_res[
            "dispatch_prompt"
        ]
        assert (
            prompt_res["role_run_id"] is None and prompt_res["backend_run_id"] is None
        ), prompt_res

        mismatched = dict(good_prompt)
        mismatched["task_type"] = "review"
        mismatch_res = run_prompt_agent(
            target="stranske/LMS#12",
            goal="Add a learner progress summary.",
            task_type="implement",
            proposal_json=mismatched,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            mismatch_res["decision_source"] == "baseline_dispatcher" and mismatch_res["errors"]
        ), mismatch_res

        baseline_prompt_res = run_prompt_agent(
            target="stranske/LMS#12",
            goal="Add a learner progress summary.",
            task_type="implement",
            target_detail="Instructor needs to see completed lesson counts.",
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert baseline_prompt_res["decision_source"] == "baseline_dispatcher", baseline_prompt_res
        assert (
            "Work stranske/LMS#12 to completion" in baseline_prompt_res["dispatch_prompt"]
        ), baseline_prompt_res

        decomposer_prompt = _decomposer_prompt(
            {
                "goal": "Add instructor analytics to the LMS",
                "repo": "stranske/learning-management-system",
                "target": "stranske/learning-management-system#999",
                "subtask_count": 3,
            }
        )
        assert (
            "DecomposerAgent" in decomposer_prompt and "Plan-and-Solve" in decomposer_prompt
        ), decomposer_prompt
        good_plan = epic_lane._valid_plan()
        assert _validate_decomposer(good_plan) == [], _validate_decomposer(good_plan)
        decomposed = run_decomposer_agent(
            goal="Add instructor analytics to the LMS",
            repo="stranske/learning-management-system",
            target="stranske/learning-management-system#999",
            subtask_count=3,
            proposal_json=good_plan,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            decomposed["mutates_state"] is False
            and decomposed["decision_source"] == "decomposer_agent"
        ), decomposed
        assert len(decomposed["dispatch_prompts"]) == 3, decomposed["dispatch_prompts"]
        assert (
            decomposed["role_run_id"] is None and decomposed["backend_run_id"] is None
        ), decomposed

        bad_plan = json.loads(json.dumps(good_plan))
        bad_plan["integration"]["order"] = ["E1"]
        bad_decomposed = run_decomposer_agent(
            goal="Add instructor analytics to the LMS",
            proposal_json=bad_plan,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            bad_decomposed["decision_source"] == "baseline_epic_lane" and bad_decomposed["errors"]
        ), bad_decomposed
        assert bad_decomposed["dispatch_prompts"] == [], bad_decomposed

        triage_items = [
            {
                "target": "stranske/Repo#1",
                "lane": "opener",
                "task_type": "implement",
                "title": "Add learner progress summary",
                "labels": ["status: ready"],
                "body": "Show completed lesson counts. AC: pytest passes.",
            },
            {
                "target": "stranske/Repo#2",
                "lane": "opener",
                "task_type": "implement",
                "title": "Fix reports",
                "labels": ["status: ready"],
                "body": "",
            },
            {
                "target": "stranske/Repo#100",
                "lane": "closer",
                "task_type": "implement",
                "title": "Agent PR",
                "labels": ["agent:codex"],
                "body": "Review open PR result.",
            },
        ]
        triage_prompt = _triage_prompt(
            {"backlog_items": triage_items, "capacity": fake_cap, "max_items": 3}
        )
        assert "TriageAgent" in triage_prompt and "Do NOT select" in triage_prompt, triage_prompt
        good_triage = {
            "summary": "One item is ready, one needs scope, and one closer PR should be monitored.",
            "recommendations": [
                {
                    "target": "stranske/Repo#1",
                    "action": "work_now",
                    "priority": 1,
                    "reason": "The body includes a concrete outcome and validation.",
                    "batch_id": "B1",
                },
                {
                    "target": "stranske/Repo#2",
                    "action": "needs_scope",
                    "priority": 4,
                    "reason": "The title is vague and the body has no acceptance criteria.",
                    "batch_id": None,
                },
                {
                    "target": "stranske/Repo#100",
                    "action": "monitor",
                    "priority": 2,
                    "reason": "Closer PRs should be inspected before new delegation.",
                    "batch_id": "B1",
                },
            ],
            "batches": [
                {
                    "id": "B1",
                    "targets": ["stranske/Repo#1", "stranske/Repo#100"],
                    "reason": "Both concern progress-summary delivery and verification.",
                    "risk": "medium",
                },
            ],
            "global_risks": ["The second issue needs a clearer body before delegation."],
            "confidence": "high",
        }
        assert _validate_triage_agent(good_triage) == [], _validate_triage_agent(good_triage)
        assert _validate_triage_context(good_triage, triage_items) == [], _validate_triage_context(
            good_triage, triage_items
        )
        triaged = run_triage_agent(
            backlog_items=triage_items,
            proposal_json=good_triage,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            triaged["mutates_state"] is False and triaged["decision_source"] == "triage_agent"
        ), triaged
        assert len(triaged["advisory_plan"]["recommendations"]) == 3, triaged
        assert triaged["role_run_id"] is None and triaged["backend_run_id"] is None, triaged

        bad_triage = json.loads(json.dumps(good_triage))
        bad_triage["recommendations"] = bad_triage["recommendations"][:2]
        bad_triaged = run_triage_agent(
            backlog_items=triage_items,
            proposal_json=bad_triage,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            bad_triaged["decision_source"] == "baseline_backlog_order" and bad_triaged["errors"]
        ), bad_triaged
        assert (
            bad_triaged["advisory_plan"]["recommendations"][0]["action"] == "work_now"
        ), bad_triaged

        adjudication_case = {
            "case_type": "runtime_ac",
            "target": "stranske/Repo#200",
            "reviewer": "vibe",
            "source": "runtime_ac_panel",
            "panel_verdict": "NEEDS_REVIEW",
            "gate_verdict": "PASS",
            "disputed_finding": "Reviewer claimed AC1 lacks runtime evidence.",
            "acceptance_criteria": [{"id": "AC1", "statement": "Dashboard shows count"}],
            "ground_truth_evidence": [
                {
                    "ref": "frontend_verify:count",
                    "status": "PASS",
                    "summary": "ARIA text shows count 7",
                }
            ],
            "repo_context": "Dashboard counts are verified through frontend_verify ARIA output.",
        }
        adjudicator_prompt = _adjudicator_prompt({"case": adjudication_case})
        assert (
            "AdjudicatorAgent" in adjudicator_prompt and "Do NOT emit PASS" in adjudicator_prompt
        ), adjudicator_prompt
        assert _validate_adjudication_case(adjudication_case) == [], _validate_adjudication_case(
            adjudication_case
        )
        good_adjudication = {
            "decision": "reject_blocker",
            "confidence": "high",
            "rationale": "The cited frontend verification directly contradicts the missing-evidence claim.",
            "evidence_assessment": [
                {
                    "claim": "AC1 lacks runtime evidence",
                    "status": "contradicted",
                    "evidence_ref": "frontend_verify:count",
                    "reason": "The ARIA evidence shows the required dashboard count.",
                }
            ],
            "ground_truth_refs": ["frontend_verify:count"],
            "recommended_next_step": "Inspect the frontend_verify ARIA output and panel note before resolving the blocker.",
            "evidence_gaps": [],
        }
        assert _validate_adjudicator(good_adjudication) == [], _validate_adjudicator(
            good_adjudication
        )
        adjudicated = run_adjudicator_agent(
            case=adjudication_case,
            proposal_json=good_adjudication,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            adjudicated["mutates_state"] is False
            and adjudicated["decision_source"] == "adjudicator_agent"
        ), adjudicated
        assert adjudicated["advisory_plan"]["decision"] == "reject_blocker", adjudicated
        assert (
            adjudicated["role_run_id"] is None and adjudicated["backend_run_id"] is None
        ), adjudicated

        bad_adjudication = dict(good_adjudication)
        bad_adjudication["verdict"] = "PASS"
        bad_adjudication["recommended_next_step"] = "Merge the PR"
        bad_adjudicated = run_adjudicator_agent(
            case=adjudication_case,
            proposal_json=bad_adjudication,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            bad_adjudicated["decision_source"] == "baseline_needs_more_evidence"
            and bad_adjudicated["errors"]
        ), bad_adjudicated
        assert (
            bad_adjudicated["advisory_plan"]["decision"] == "needs_more_evidence"
        ), bad_adjudicated

        incomplete_case = dict(adjudication_case)
        incomplete_case.pop("ground_truth_evidence")
        invalid_case = run_adjudicator_agent(
            case=incomplete_case,
            proposal_json=good_adjudication,
            cap=fake_cap,
            learned={},
            exploration_rate=0.0,
        )
        assert (
            invalid_case["decision_source"] == "baseline_needs_more_evidence"
            and invalid_case["errors"]
        ), invalid_case

        # All five roles share the bounded selector contract: one invocation per
        # cycle by default, then an explicit matched_not_invoked cap diagnostic.
        reset_role_invocation_counts()
        for role_name in ROLE_REGISTRY:
            first = select_role_activation(
                role_name,
                matched=True,
                gate_enabled=True,
                capacity_available=True,
                max_invocations=1,
                reason="selftest_fixture",
                record=False,
            )
            second = select_role_activation(
                role_name,
                matched=True,
                gate_enabled=True,
                capacity_available=True,
                max_invocations=1,
                reason="selftest_fixture",
                record=False,
            )
            assert first["selector_status"] == "invoked", (role_name, first)
            assert second["selector_status"] == "matched_not_invoked", (role_name, second)
            assert second["reason"] == "per_cycle_invocation_cap", (role_name, second)

        # LINEAGE STAMP -- the regression that broke role->outcome attribution entirely.
        # On the REPLAY path (a proposal handed in as proposal_json rather than fetched by
        # dispatcher.offload) record_role_run read `res`, which is only bound inside the offload
        # branch. The resulting unbound-local NameError was swallowed by the surrounding
        # `except Exception`, which reset role_run_id to None -- so no role run was recorded, the
        # accepted proposal was never stamped onto the dispatch, and no role->outcome edge could
        # form. Nothing raised; the chain just silently did not exist. Assert every link.
        reset_role_invocation_counts()
        old_prompt_dir = redirect_plan.PROMPT_DIR
        redirect_plan.PROMPT_DIR = Path(tmp) / "redirect-prompts"
        redirect_plan.PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            stamped = run_redirect_agent(
                report,
                "All endpoints return 200.",
                proposal_json=proposed,
                backend="codex",
                dispatch=True,
                cap=fake_cap,
                learned={},
                exploration_rate=0.0,
            )
            # (1) the advisory role run exists, and its model stays NULL: no backend ran, so there
            # is no provider-resolved identity to claim (cost telemetry, not provenance).
            role_run_id = stamped["role_run_id"]
            assert role_run_id and stamped["role_record_error"] is None, stamped
            with feedback._conn() as conn:
                assert conn.execute(
                    "SELECT role_name,model FROM runs WHERE run_id=?", (role_run_id,)
                ).fetchone() == ("redirect", None), role_run_id

            # (2) the accepted proposal is stamped onto the dispatch command the plan emits.
            assert stamped["plan"]["accepted_role_run_id"] == role_run_id, stamped["plan"]
            delegate_argv = stamped["plan"]["steps"][-1]["commands"][0]
            flag_at = delegate_argv.index("--influenced-by-role-run-id")
            assert delegate_argv[flag_at + 1] == role_run_id, delegate_argv

            # (3) that argv really parses through the delegate CLI into the kwarg feedback reads
            # -- the string being present in the plan is not evidence that it arrives.
            Path(stamped["plan"]["prompt_file"]).write_text(stamped["plan"]["prompt_text"])
            seen: dict = {}

            def _capture_delegate(*args, **kwargs):
                seen.update(kwargs)
                return {"run_id": "selftest-delegate"}

            old_delegate = dispatcher.delegate
            dispatcher.delegate = _capture_delegate
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    dispatcher.main(delegate_argv[2:])
            finally:
                dispatcher.delegate = old_delegate
            assert seen.get("influenced_by_role_run_ids") == [role_run_id], seen

            # (4) the ACTING run's terminal outcome mirrors back onto the advisory role run.
            feedback.record_run(
                "work:lineage-stamp",
                str(report["target"]),
                "implement",
                "cursor",
                influenced_by_role_run_ids=[role_run_id],
            )
            feedback.record_outcome(
                "work:lineage-stamp", adjudicated_verdict="PASS", merged=True, durability="durable"
            )
            with feedback._conn() as conn:
                assert conn.execute(
                    "SELECT adjudicated_verdict,durability FROM outcomes WHERE run_id=?",
                    (role_run_id,),
                ).fetchone() == ("PASS", "durable"), role_run_id
                assert conn.execute(
                    "SELECT outcome_verdict,durability FROM influence_edges "
                    "WHERE target_run_id='work:lineage-stamp' AND influence_type='role'"
                ).fetchone() == ("PASS", "durable")

            # (5) a REJECTED role run is still recorded -- disagreement is evidence -- but it is
            # never stamped onto the dispatch and never inherits the acting run's success.
            rejected = run_redirect_agent(
                report,
                "AC",
                proposal_json={
                    "action": "redirect",
                    "reason": "x",
                    "confidence": "high",
                    "corrected_prompt": "",
                },
                backend="codex",
                dispatch=True,
                cap=fake_cap,
                learned={},
                exploration_rate=0.0,
            )
            assert rejected["role_run_id"] and rejected["errors"], rejected
            assert "accepted_role_run_id" not in rejected["plan"], rejected["plan"]
            assert (
                "--influenced-by-role-run-id" not in rejected["plan"]["steps"][-1]["commands"][0]
            ), rejected["plan"]
            feedback.record_run("work:rejected-stamp", str(report["target"]), "implement", "cursor")
            feedback.record_influence_edge(
                target_run_id="work:rejected-stamp",
                influence_type="role",
                influence_id=rejected["role_run_id"],
                source_run_id=rejected["role_run_id"],
                accepted=False,
                metadata={"status": "rejected", "disagreement": True},
            )
            feedback.record_outcome(
                "work:rejected-stamp", adjudicated_verdict="PASS", merged=True, durability="durable"
            )
            with feedback._conn() as conn:
                assert conn.execute(
                    "SELECT accepted,counterfactual,outcome_verdict FROM influence_edges "
                    "WHERE target_run_id='work:rejected-stamp'"
                ).fetchone() == (0, 1, None)
                assert (
                    conn.execute(
                        "SELECT 1 FROM outcomes WHERE run_id=?", (rejected["role_run_id"],)
                    ).fetchone()
                    is None
                ), "a rejected role must not inherit a PASS"
        finally:
            redirect_plan.PROMPT_DIR = old_prompt_dir

        # The operator's own route into the replay path must actually run. A function-local
        # `from pathlib import Path` in main() made Path a local for the WHOLE function, so every
        # `--context-file` branch raised UnboundLocalError -- same failure class as the `res` bug
        # above, one frame away. Drive the real CLI, not the helper it calls.
        context_file = Path(tmp) / "role-context.md"
        context_file.write_text("CONTEXT-FROM-FILE marker\n")
        cli_out = io.StringIO()
        with contextlib.redirect_stdout(cli_out):
            rc = main(
                [
                    "prompt",
                    "--target",
                    "o/r#1",
                    "--goal",
                    "g",
                    "--context-file",
                    str(context_file),
                    "--json",
                ]
            )
        assert rc == 0, rc
        assert "CONTEXT-FROM-FILE marker" in json.loads(cli_out.getvalue())["prompt"]

        print(
            "roles.py selftest: OK (route_role + RedirectAgent dry-run + PromptAgent + "
            "DecomposerAgent + TriageAgent + AdjudicatorAgent + bounded selectors + role learning + "
            "replay-path role_run_id -> plan stamp -> delegate CLI -> mirrored outcome)"
        )
    finally:
        feedback.DB_PATH = old_db
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _read_backlog_items(path: str) -> list[dict]:
    if not path:
        return backlog_mod.load_backlog()
    data = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return items
        backlog_items = data.get("backlog")
        if isinstance(backlog_items, list):
            return backlog_items
    raise ValueError("backlog JSON must be a list or an object with an items/backlog list")


def _read_case_json(path: str) -> dict:
    data = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("case JSON must be an object")
    return data


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(
        description="Agent-role registry + RedirectAgent shadow proposer (never mutates)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("route", help="show the router-chosen backend for a role")
    pr.add_argument("--role", default="redirect")
    pr.add_argument("--high-leverage", action="store_true", help="allow reserve seats (claude)")
    pr.add_argument("--json", action="store_true", dest="as_json")

    rd = sub.add_parser("redirect", help="run RedirectAgent in shadow and print a dry-run plan")
    rd.add_argument(
        "--report-json",
        default="",
        help="watch.py JSON report; stdin used when omitted",
    )
    rd.add_argument(
        "--ac",
        "--acceptance-criteria",
        dest="ac",
        default="",
        help="acceptance criteria the work must satisfy",
    )
    rd.add_argument(
        "--attempt-history-json", default="", help="prior watch reports for this target"
    )
    rd.add_argument("--backend", default="", help="force a backend; default routes via route_role")
    rd.add_argument(
        "--dispatch",
        action="store_true",
        help="actually offload to the backend (spends its capacity); default is route+prompt only",
    )
    rd.add_argument(
        "--proposal-json",
        default="",
        help="replay a pre-captured proposal instead of dispatching",
    )
    rd.add_argument("--lane", default="")
    rd.add_argument("--task-type", default="")
    rd.add_argument("--next-agent", default="")
    rd.add_argument("--high-leverage", action="store_true")
    rd.add_argument(
        "--record-corpus",
        action="store_true",
        help="append this live dispatch to the RedirectAgent shadow corpus",
    )
    rd.add_argument("--corpus", default="", help="optional redirect shadow corpus path")
    rd.add_argument("--json", action="store_true", dest="as_json")

    pp = sub.add_parser(
        "prompt", help="run PromptAgent in shadow and print a dispatch-ready prompt"
    )
    pp.add_argument("--target", required=True)
    pp.add_argument("--goal", required=True)
    pp.add_argument("--task-type", default="implement")
    pp.add_argument("--target-detail", default="")
    pp.add_argument("--context", default="")
    pp.add_argument("--context-file", default="")
    pp.add_argument("--repo", default="")
    pp.add_argument("--lane", default="")
    pp.add_argument("--acceptance-criterion", action="append", default=[])
    pp.add_argument("--constraint", action="append", default=[])
    pp.add_argument("--expected-path", action="append", default=[])
    pp.add_argument("--backend", default="", help="force a backend; default routes via route_role")
    pp.add_argument(
        "--dispatch",
        action="store_true",
        help="actually offload to the backend (spends its capacity); default is route+prompt only",
    )
    pp.add_argument(
        "--proposal-json",
        default="",
        help="replay a pre-captured proposal instead of dispatching",
    )
    pp.add_argument("--cwd", default=".", help="cwd for live offload context")
    pp.add_argument("--timeout", type=int, default=600)
    pp.add_argument("--high-leverage", action="store_true")
    pp.add_argument("--json", action="store_true", dest="as_json")

    dc = sub.add_parser(
        "decompose", help="run DecomposerAgent in shadow and print an epic plan preview"
    )
    dc.add_argument("--goal", required=True)
    dc.add_argument("--repo", default="")
    dc.add_argument("--target", default="")
    dc.add_argument("--context", default="")
    dc.add_argument("--context-file", default="")
    dc.add_argument("--subtask-count", type=int, default=None)
    dc.add_argument("--backend", default="", help="force a backend; default routes via route_role")
    dc.add_argument(
        "--dispatch",
        action="store_true",
        help="actually offload to the backend (spends its capacity); default is route+prompt only",
    )
    dc.add_argument(
        "--proposal-json",
        default="",
        help="replay a pre-captured epic plan instead of dispatching",
    )
    dc.add_argument("--cwd", default=".", help="cwd for live offload context")
    dc.add_argument("--timeout", type=int, default=600)
    dc.add_argument("--high-leverage", action="store_true")
    dc.add_argument("--json", action="store_true", dest="as_json")

    tr = sub.add_parser("triage", help="run TriageAgent in shadow against a backlog snapshot")
    tr.add_argument(
        "--backlog-json",
        default="",
        help="captured backlog JSON; '-' reads stdin; omitted reads local handoff backlog",
    )
    tr.add_argument("--context", default="")
    tr.add_argument("--context-file", default="")
    tr.add_argument("--max-items", type=int, default=20)
    tr.add_argument("--backend", default="", help="force a backend; default routes via route_role")
    tr.add_argument(
        "--dispatch",
        action="store_true",
        help="actually offload to the backend (spends its capacity); default is route+prompt only",
    )
    tr.add_argument(
        "--proposal-json",
        default="",
        help="replay a pre-captured triage proposal instead of dispatching",
    )
    tr.add_argument("--cwd", default=".", help="cwd for live offload context")
    tr.add_argument("--timeout", type=int, default=600)
    tr.add_argument("--high-leverage", action="store_true")
    tr.add_argument("--json", action="store_true", dest="as_json")

    aj = sub.add_parser(
        "adjudicate",
        help="run AdjudicatorAgent in shadow against one disputed blocker case",
    )
    aj.add_argument("--case-json", required=True, help="case JSON file; '-' reads stdin")
    aj.add_argument("--context", default="")
    aj.add_argument("--context-file", default="")
    aj.add_argument("--backend", default="", help="force a backend; default routes via route_role")
    aj.add_argument(
        "--dispatch",
        action="store_true",
        help="actually offload to the backend (spends its capacity); default is route+prompt only",
    )
    aj.add_argument(
        "--proposal-json",
        default="",
        help="replay a pre-captured adjudication proposal",
    )
    aj.add_argument("--cwd", default=".", help="cwd for live offload context")
    aj.add_argument("--timeout", type=int, default=600)
    aj.add_argument("--high-leverage", action="store_true")
    aj.add_argument("--json", action="store_true", dest="as_json")

    lk = sub.add_parser(
        "link-outcome",
        help="link a role run to the downstream run outcome it influenced",
    )
    lk.add_argument("--role-run-id", required=True)
    lk.add_argument("--influenced-run-id", required=True)
    lk.add_argument(
        "--not-accepted",
        action="store_true",
        help="record the audit link but do not sync the downstream outcome onto the role run",
    )
    lk.add_argument("--notes", default="")
    lk.add_argument("--json", action="store_true", dest="as_json")

    sm = sub.add_parser(
        "summarize-proposals",
        help="summarize RedirectAgent proposal quality/disagreement corpus",
    )
    sm.add_argument("--corpus", default="", help="path to the redirect shadow corpus JSONL file")
    sm.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)

    if args.cmd == "route":
        pick = route_role(args.role, high_leverage=args.high_leverage)
        if args.as_json:
            print(json.dumps(pick, indent=2))
        else:
            print(f"role={args.role} backend={(pick or {}).get('agent', '(none available)')}")
        return 0

    if args.cmd == "redirect":
        report = (
            json.loads(open(args.report_json).read()) if args.report_json else json.load(sys.stdin)
        )
        history = (
            json.loads(open(args.attempt_history_json).read())
            if args.attempt_history_json
            else None
        )
        proposal = json.loads(open(args.proposal_json).read()) if args.proposal_json else None
        result = run_redirect_agent(
            report,
            args.ac,
            attempt_history=history,
            backend=(args.backend or None),
            dispatch=args.dispatch,
            proposal_json=proposal,
            high_leverage=args.high_leverage,
            lane=(args.lane or None),
            task_type=(args.task_type or None),
            next_agent=(args.next_agent or None),
            record_corpus=args.record_corpus,
            corpus_path=(args.corpus or None),
        )
        print(json.dumps(result, indent=2) if args.as_json else format_human(result))
        return 0
    if args.cmd == "prompt":
        context = args.context
        if args.context_file:
            context = (context + "\n\n" if context else "") + Path(args.context_file).read_text()
        proposal = json.loads(open(args.proposal_json).read()) if args.proposal_json else None
        result = run_prompt_agent(
            target=args.target,
            goal=args.goal,
            task_type=args.task_type,
            target_detail=args.target_detail,
            context=context,
            repo=args.repo,
            lane=(args.lane or None),
            acceptance_criteria=args.acceptance_criterion,
            constraints=args.constraint,
            expected_paths=args.expected_path,
            backend=(args.backend or None),
            dispatch=args.dispatch,
            proposal_json=proposal,
            high_leverage=args.high_leverage,
            cwd=args.cwd,
            timeout=args.timeout,
        )
        print(json.dumps(result, indent=2) if args.as_json else format_prompt_human(result))
        return 0
    if args.cmd == "decompose":
        context = args.context
        if args.context_file:
            context = (context + "\n\n" if context else "") + Path(args.context_file).read_text()
        proposal = json.loads(open(args.proposal_json).read()) if args.proposal_json else None
        result = run_decomposer_agent(
            goal=args.goal,
            repo=args.repo,
            target=args.target,
            context=context,
            subtask_count=args.subtask_count,
            backend=(args.backend or None),
            dispatch=args.dispatch,
            proposal_json=proposal,
            high_leverage=args.high_leverage,
            cwd=args.cwd,
            timeout=args.timeout,
        )
        print(json.dumps(result, indent=2) if args.as_json else format_decomposer_human(result))
        return 0
    if args.cmd == "triage":
        context = args.context
        if args.context_file:
            context = (context + "\n\n" if context else "") + Path(args.context_file).read_text()
        proposal = json.loads(open(args.proposal_json).read()) if args.proposal_json else None
        result = run_triage_agent(
            backlog_items=_read_backlog_items(args.backlog_json),
            context=context,
            max_items=args.max_items,
            backend=(args.backend or None),
            dispatch=args.dispatch,
            proposal_json=proposal,
            high_leverage=args.high_leverage,
            cwd=args.cwd,
            timeout=args.timeout,
        )
        print(json.dumps(result, indent=2) if args.as_json else format_triage_human(result))
        return 0
    if args.cmd == "adjudicate":
        context = args.context
        if args.context_file:
            context = (context + "\n\n" if context else "") + Path(args.context_file).read_text()
        proposal = json.loads(open(args.proposal_json).read()) if args.proposal_json else None
        result = run_adjudicator_agent(
            case=_read_case_json(args.case_json),
            context=context,
            backend=(args.backend or None),
            dispatch=args.dispatch,
            proposal_json=proposal,
            high_leverage=args.high_leverage,
            cwd=args.cwd,
            timeout=args.timeout,
        )
        print(json.dumps(result, indent=2) if args.as_json else format_adjudicator_human(result))
        return 0
    if args.cmd == "link-outcome":
        result = feedback.join_role_to_outcome(
            args.role_run_id,
            args.influenced_run_id,
            accepted=not args.not_accepted,
            notes=args.notes or None,
        )
        if result.get("synced"):
            role_name = str(result.get("task_type") or "").removeprefix("role:")
            if role_name in ROLE_CAPABILITY_IDS:
                _role_capability_event(
                    role_name,
                    "outcome",
                    ref=args.influenced_run_id,
                    metadata={"role_run_id": args.role_run_id},
                )
        print(json.dumps(result, indent=2) if args.as_json else result)
        return 0
    if args.cmd == "summarize-proposals":
        # NO local `from pathlib import Path` here. It made Path a local of main() for the WHOLE
        # function, so every earlier `--context-file` branch (all five role subcommands) died with
        # `UnboundLocalError: cannot access local variable 'Path'` -- the operator's own route into
        # the replay path that this lineage work depends on. Module-level Path (line 33) is the one
        # to use. Caught by `ruff check --select F` as F823. (2026-08-21)
        import redirect_shadow

        corpus_path = Path(args.corpus) if args.corpus else redirect_shadow.CORPUS_PATH
        summary = redirect_shadow.summarize(corpus_path)
        print(
            json.dumps(summary, indent=2)
            if args.as_json
            else (
                f"RedirectAgent shadow corpus: {summary['n']} proposals\n"
                f"  Disagreement rate: {summary['disagreement_rate']} ({summary['disagreements']} disagreed)\n"
                f"  Proposal actions: {summary['proposal_action_distribution']}\n"
                f"  Baseline actions: {summary['baseline_action_distribution']}\n"
                f"  Backends: {summary['backend_distribution']}\n"
                f"  Synced role outcomes: {summary['synced_role_outcomes']}\n"
                f"  Ready for supervised apply: {summary['ready_for_supervised_apply']}"
            )
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
