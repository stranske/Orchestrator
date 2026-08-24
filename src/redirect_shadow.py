#!/usr/bin/env python3
"""redirect_shadow.py - RedirectAgent proposal corpus and outcome measurement.

This is the advisor -> autonomous ramp's evidence layer. It runs RedirectAgent on
real watch.py reports in SHADOW mode, appends proposal summaries to a local JSONL
corpus, and later links accepted/applied role runs to downstream outcomes through
feedback.py. It never calls redirect_plan --apply and never mutates a lane.

Runtime: corpus lives on local disk (not Dropbox) by default:
~/.codex/orchestrator/redirect-shadow/shadow.jsonl. `--selftest` is offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import feedback

CORPUS_PATH = Path(
    os.environ.get(
        "ORCH_REDIRECT_SHADOW_CORPUS",
        Path.home() / ".codex" / "orchestrator" / "redirect-shadow" / "shadow.jsonl",
    )
)
READINESS_TARGET = 20
LINKED_OUTCOME_TARGET = 10
DISAGREEMENT_OUTCOME_TARGET = 3
SCHEMA_VERSION = 1
DEFAULT_HISTORICAL_CANDIDATE_LIMIT = 25
HISTORICAL_SOURCE = "historical-replay"
APPLY_KIND = "redirect_apply"


# FIVE `_slug`-SHAPED HELPERS EXIST IN THIS TREE AND THEY MUST NOT BE UNIFIED.
# `redirect_shadow._corpus_entry_slug` KEEPS `#`, `redirect_plan._prompt_path_slug` STRIPS it, and
# `exploration_backfill._exp_id_slug` uses `-` and does not map `/`->`__`. That divergence was
# filed as a hygiene item ("same target != same key across modules"), and the fix is NOT to merge
# them: verified 2026-08-21 that nothing joins their outputs -- each feeds a different identifier
# namespace (corpus entry_id / prompt file path / experiment id), and unifying would rewrite
# existing entry_ids, prompt paths and `backfill-` exp_ids, breaking dedupe against historical
# rows for no gain. The real hazard is that a shared NAME invites a future join, so each is named
# for its namespace instead. If you need a target key that crosses modules, add one deliberately;
# do not reach for whichever of these is nearest.
# The hygiene item said THREE; it is five. The other two are `claims._slug` (claim file path,
# and the only one deliberately called cross-module -- range_lane_rollout uses it to build a
# claim path, which is correct BECAUSE it is module-qualified) and `partitioned_review._slug`
# (partition_id, 48-char capped). Both are also namespace-local; neither was renamed because
# their names are already reached through their module.
def _corpus_entry_slug(value: str) -> str:
    s = (value or "").strip().lower().replace("/", "__")
    s = re.sub(r"[^a-z0-9_.#-]+", "_", s)
    return s.strip("_") or "target"


def _sha_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_history(history: list | None) -> str | None:
    """Stable hash for optional attempt-history references in external callers."""
    if not history:
        return None
    return _sha_json(history)


def _read_json(path: str) -> Any:
    if not path:
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_text_arg(value: str, path: str) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return value or ""


def _short_text(text: str | None, limit: int = 1200) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    return stripped[-limit:] if len(stripped) > limit else stripped


def _counter_inc(counter: dict[str, int], key: Any) -> None:
    name = str(key) if key not in (None, "") else "(none)"
    counter[name] = counter.get(name, 0) + 1


def _report_summary(report: dict) -> dict:
    drift = report.get("drift") or {}
    signals = report.get("signals") or {}
    policy = report.get("policy_decision") or {}
    summary = {
        "target": report.get("target") or "",
        "agent": report.get("agent") or "",
        "lane": report.get("lane") or "",
        "task_type": report.get("task_type") or "",
        "state": report.get("state") or "",
        "recommended_action": report.get("recommended_action") or "",
        "policy_action": policy.get("action") or "",
        "policy_confidence": policy.get("confidence") or "",
        "hint_kinds": sorted({h.get("kind") for h in report.get("hints") or [] if h.get("kind")}),
        "drift": {
            "severity": drift.get("severity") or "none",
            "finding_kinds": [f.get("kind") for f in drift.get("findings") or [] if f.get("kind")],
            "changed_path_count": drift.get("changed_path_count"),
        },
        "has_worktree_changes": bool(signals.get("has_worktree_changes")),
        "pid_alive": signals.get("pid_alive"),
        "errors": list(report.get("errors") or []),
    }
    if report.get("sweep_fingerprint"):
        summary["sweep_fingerprint"] = report.get("sweep_fingerprint")
    return summary


def _plan_summary(plan: dict) -> dict:
    prompt_text = plan.get("prompt_text") or ""
    return {
        "action": plan.get("action") or "",
        "confidence": plan.get("confidence") or "",
        "requires_confirmation": bool(plan.get("requires_confirmation")),
        "mutates_state": bool(plan.get("mutates_state")),
        "dry_run_only": bool(plan.get("dry_run_only")),
        "apply_supported": bool(plan.get("apply_supported")),
        "prompt_file": plan.get("prompt_file") or "",
        "prompt_sha256": _sha_text(prompt_text) if prompt_text else None,
        "step_ids": [s.get("id") for s in plan.get("steps") or [] if s.get("id")],
    }


def _proposal_summary(proposal: dict | None) -> dict | None:
    if not isinstance(proposal, dict):
        return None
    corrected = proposal.get("corrected_prompt")
    return {
        "action": proposal.get("action"),
        "confidence": proposal.get("confidence"),
        "reason": proposal.get("reason"),
        "switch_agent": proposal.get("switch_agent"),
        "has_corrected_prompt": bool(isinstance(corrected, str) and corrected.strip()),
        "corrected_prompt_sha256": (_sha_text(corrected) if isinstance(corrected, str) else None),
        "corrected_prompt_preview": (
            _short_text(corrected, 500) if isinstance(corrected, str) else None
        ),
    }


def _outcome_for_run(run_id: str | None) -> dict | None:
    if not run_id:
        return None
    try:
        with feedback._conn() as c:  # same local store; feedback exposes no read helper yet.
            row = c.execute(
                "SELECT verifier_verdict, adjudicated_verdict, merged, ci_status, durability, "
                "durability_checked_ts, notes, influenced_by_run_id FROM outcomes WHERE run_id=?",
                (run_id,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    verifier, adjudicated, merged, ci, durability, checked_ts, notes, influenced = row
    return {
        "run_id": run_id,
        "verifier_verdict": verifier,
        "adjudicated_verdict": adjudicated,
        "merged": bool(merged) if merged is not None else None,
        "ci_status": ci,
        "durability": durability,
        "durability_checked_ts": checked_ts,
        "notes": notes,
        "influenced_by_run_id": influenced,
        "success": bool(feedback._is_success(durability, adjudicated, verifier)),
    }


def _append_event(event: dict, corpus_path: Path = CORPUS_PATH) -> dict:
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with corpus_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    return {"recorded": True, "corpus": str(corpus_path)}


def _iter_events(corpus_path: Path = CORPUS_PATH) -> list[dict]:
    if not corpus_path.exists():
        return []
    rows: list[dict] = []
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def build_entry(result: dict, report: dict, acceptance_criteria: str, *, source: str) -> dict:
    proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else None
    baseline = result.get("baseline") or {}
    plan = result.get("plan") or {}
    summary = _report_summary(report)
    target = summary.get("target") or "redirect-role"
    ts_ns = time.time_ns()
    proposal_action = proposal.get("action") if proposal else None
    baseline_action = baseline.get("action")
    entry = {
        "kind": "redirect_proposal",
        "schema_version": SCHEMA_VERSION,
        "entry_id": f"redirect-shadow:{_corpus_entry_slug(target)}:{ts_ns}",
        "ts": int(time.time()),
        "source": source,
        "shadow": True,
        "mutates_state": bool(result.get("mutates_state")),
        "target": target,
        "backend": result.get("backend") or "",
        "role_run_id": result.get("role_run_id"),
        "backend_run_id": result.get("backend_run_id"),
        "decision_source": result.get("decision_source") or "",
        "valid_proposal": proposal is not None
        and result.get("decision_source") == "redirect_agent",
        "baseline_action": baseline_action,
        "proposal_action": proposal_action,
        "plan_action": plan.get("action"),
        "disagreement": bool(
            proposal_action and baseline_action and proposal_action != baseline_action
        ),
        "errors": list(result.get("errors") or []),
        "report_sha256": _sha_json(report),
        "report": summary,
        "acceptance_criteria_sha256": _sha_text(acceptance_criteria),
        "acceptance_criteria_chars": len(acceptance_criteria or ""),
        "proposal": _proposal_summary(proposal),
        "baseline": {
            "action": baseline.get("action"),
            "confidence": baseline.get("confidence"),
            "reason": baseline.get("reason"),
        },
        "plan": _plan_summary(plan),
    }
    if report.get("sweep_fingerprint"):
        entry["sweep_fingerprint"] = report.get("sweep_fingerprint")
    raw_output = result.get("raw_output")
    if raw_output:
        entry["raw_output_sha256"] = _sha_text(raw_output)
        entry["raw_output_preview"] = _short_text(raw_output)
    backend_error_detail = result.get("backend_error_detail")
    if backend_error_detail:
        entry["backend_error_detail_sha256"] = _sha_text(str(backend_error_detail))
        entry["backend_error_detail_preview"] = _short_text(str(backend_error_detail))
    return entry


def record_redirect(
    report: dict,
    acceptance_criteria: str,
    *,
    attempt_history: list | None = None,
    backend: str | None = None,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    high_leverage: bool = False,
    lane: str | None = None,
    task_type: str | None = None,
    next_agent: str | None = None,
    timeout: int = 600,
    corpus_path: Path = CORPUS_PATH,
    source: str | None = None,
) -> dict:
    """Run RedirectAgent in shadow and append one proposal event. No lane mutation."""
    if not dispatch and proposal_json is None:
        raise ValueError(
            "recording the RedirectAgent corpus requires --dispatch or --proposal-json"
        )
    import roles

    result = roles.run_redirect_agent(
        report,
        acceptance_criteria,
        attempt_history=attempt_history,
        backend=backend,
        dispatch=dispatch,
        proposal_json=proposal_json,
        high_leverage=high_leverage,
        lane=lane,
        task_type=task_type,
        next_agent=next_agent,
        timeout=timeout,
    )
    event_source = source or ("live-dispatch" if dispatch else "replay")
    entry = build_entry(result, report, acceptance_criteria, source=event_source)
    rec = _append_event(entry, corpus_path)
    return {"entry": entry, **rec}


def record_proposal(
    *,
    role_run_id: str | None,
    report: dict,
    acceptance_criteria: str,
    proposal: dict | None,
    baseline: dict,
    decision_source: str,
    errors: list | None,
    backend: str | None,
    backend_run_id: str | None = None,
    plan: dict | None = None,
    raw_output: str | None = None,
    backend_error_detail: str | None = None,
    corpus_path: Path = CORPUS_PATH,
    source: str = "live-dispatch",
) -> dict:
    """Append a proposal event from an already-run RedirectAgent invocation.

    This keeps roles.py from importing the heavier record_redirect path and lets
    callers opt into corpus logging without re-running an agent.
    """
    result = {
        "mutates_state": False,
        "backend": backend,
        "role_run_id": role_run_id,
        "backend_run_id": backend_run_id,
        "decision_source": decision_source,
        "proposal": proposal,
        "baseline": baseline,
        "errors": errors or [],
        "plan": plan or {},
        "raw_output": raw_output,
        "backend_error_detail": backend_error_detail,
    }
    entry = build_entry(result, report, acceptance_criteria, source=source)
    rec = _append_event(entry, corpus_path)
    return {"entry": entry, **rec}


def link_outcome(
    role_run_id: str,
    influenced_run_id: str,
    *,
    accepted: bool = True,
    notes: str | None = None,
    entry_id: str | None = None,
    corpus_path: Path = CORPUS_PATH,
) -> dict:
    """Append an outcome-link event and sync accepted advice through feedback.py."""
    result = feedback.join_role_to_outcome(
        role_run_id,
        influenced_run_id,
        accepted=accepted,
        notes=notes,
    )
    event = {
        "kind": "redirect_outcome_link",
        "schema_version": SCHEMA_VERSION,
        "entry_id": entry_id,
        "ts": int(time.time()),
        "role_run_id": role_run_id,
        "influenced_run_id": influenced_run_id,
        "accepted": bool(accepted),
        "link_result": result,
        "role_outcome": _outcome_for_run(role_run_id),
        "downstream_outcome": _outcome_for_run(influenced_run_id),
    }
    rec = _append_event(event, corpus_path)
    return {"event": event, **rec}


def applied_events(corpus_path: Path = CORPUS_PATH) -> list[dict]:
    """Every recorded apply of a redirect plan, newest last."""
    return [e for e in _iter_events(corpus_path) if e.get("kind") == APPLY_KIND]


def linked_pairs(corpus_path: Path = CORPUS_PATH) -> set[tuple[str, str]]:
    """(role_run_id, influenced_run_id) pairs that already carry an outcome-link event."""
    out: set[tuple[str, str]] = set()
    for event in _iter_events(corpus_path):
        if event.get("kind") != "redirect_outcome_link":
            continue
        role_run_id = event.get("role_run_id")
        influenced = event.get("influenced_run_id")
        if role_run_id and influenced:
            out.add((str(role_run_id), str(influenced)))
    return out


def record_apply(
    *,
    role_run_id: str | None,
    target: str,
    plan_action: str | None,
    authorization: dict,
    apply_result: dict | None,
    dry_run: bool,
    corpus_path: Path = CORPUS_PATH,
) -> dict:
    """Append one apply attempt to the SAME append-only corpus as proposals and links.

    Deliberately not a new store. `summarize()` keys off `kind`, so an unknown kind is inert
    there, and keeping applies beside the proposals they came from is what lets the per-target
    and per-day bounds be derived from recorded history instead of a side file.
    """
    event = {
        "kind": APPLY_KIND,
        "schema_version": SCHEMA_VERSION,
        "ts": int(time.time()),
        "role_run_id": role_run_id,
        "target": target,
        "plan_action": plan_action,
        "dry_run": bool(dry_run),
        "authorized": bool(authorization.get("allowed")),
        "authorization": authorization,
        "applied": bool((apply_result or {}).get("applied")),
        "apply_result": apply_result,
    }
    rec = _append_event(event, corpus_path)
    return {"event": event, **rec}


def historical_outcome_link(
    *,
    role_run_id: str | None,
    target: str,
    outcome: dict,
    entry_id: str | None = None,
    notes: str | None = None,
    corpus_path: Path = CORPUS_PATH,
) -> dict:
    """Append a counterfactual historical outcome link without syncing role learning.

    Historical replay did not cause the old keepalive outcome. Keep this link in
    the shadow corpus for analysis, but do not mirror it into feedback outcomes
    the way accepted/applied live role advice does.
    """
    event = {
        "kind": "redirect_historical_outcome_link",
        "schema_version": SCHEMA_VERSION,
        "entry_id": entry_id,
        "ts": int(time.time()),
        "role_run_id": role_run_id,
        "target": target,
        "accepted": False,
        "counterfactual": True,
        "synced": False,
        "not_role_learning": True,
        "notes": notes,
        "historical_outcome": outcome,
    }
    rec = _append_event(event, corpus_path)
    return {"event": event, **rec}


def summarize(corpus_path: Path = CORPUS_PATH) -> dict:
    events = _iter_events(corpus_path)
    proposals = [e for e in events if e.get("kind") == "redirect_proposal"]
    links = [e for e in events if e.get("kind") == "redirect_outcome_link"]
    historical_links = [e for e in events if e.get("kind") == "redirect_historical_outcome_link"]
    by_backend: dict[str, int] = {}
    baseline_actions: dict[str, int] = {}
    proposal_actions: dict[str, int] = {}
    plan_actions: dict[str, int] = {}
    states: dict[str, int] = {}
    linked_by_role = {e.get("role_run_id"): e for e in links if e.get("role_run_id")}
    historical_by_role = {e.get("role_run_id"): e for e in historical_links if e.get("role_run_id")}

    valid = invalid = disagreements = role_runs = dispatches = historical_replays = 0
    live_valid = live_invalid = 0
    linked_outcomes = accepted_links = synced_links = outcome_successes = disagreement_outcomes = 0
    historical_outcomes = historical_successes = historical_disagreement_outcomes = 0
    for row in proposals:
        _counter_inc(by_backend, row.get("backend"))
        _counter_inc(baseline_actions, row.get("baseline_action"))
        _counter_inc(proposal_actions, row.get("proposal_action"))
        _counter_inc(plan_actions, row.get("plan_action"))
        _counter_inc(states, (row.get("report") or {}).get("state"))
        valid += 1 if row.get("valid_proposal") else 0
        invalid += 1 if row.get("errors") else 0
        disagreements += 1 if row.get("disagreement") else 0
        role_runs += 1 if row.get("role_run_id") else 0
        is_live_dispatch = row.get("source") == "live-dispatch"
        dispatches += 1 if is_live_dispatch else 0
        if is_live_dispatch:
            if row.get("valid_proposal"):
                live_valid += 1
            else:
                live_invalid += 1
        historical_replays += 1 if row.get("source") == HISTORICAL_SOURCE else 0
        historical = historical_by_role.get(row.get("role_run_id"))
        if historical:
            historical_outcomes += 1
            historical_outcome = historical.get("historical_outcome") or {}
            historical_successes += 1 if historical_outcome.get("success") else 0
            historical_disagreement_outcomes += 1 if row.get("disagreement") else 0
        link = linked_by_role.get(row.get("role_run_id"))
        if not link:
            continue
        linked_outcomes += 1
        accepted_links += 1 if link.get("accepted") else 0
        synced_links += 1 if (link.get("link_result") or {}).get("synced") else 0
        outcome = link.get("role_outcome") or {}
        outcome_successes += 1 if outcome.get("success") else 0
        disagreement_outcomes += 1 if row.get("disagreement") else 0

    ready_for_analysis = valid >= READINESS_TARGET
    ready_for_supervised_apply = (
        ready_for_analysis
        and synced_links >= LINKED_OUTCOME_TARGET
        and disagreement_outcomes >= DISAGREEMENT_OUTCOME_TARGET
    )
    ready_for_historical_replay_analysis = (
        ready_for_analysis
        and (synced_links + historical_outcomes) >= LINKED_OUTCOME_TARGET
        and (disagreement_outcomes + historical_disagreement_outcomes)
        >= DISAGREEMENT_OUTCOME_TARGET
    )
    summary = {
        "n": len(proposals),
        "events": len(events),
        "live_dispatches": dispatches,
        "live_valid_proposals": live_valid,
        "live_invalid_or_fallback_proposals": live_invalid,
        "historical_replays": historical_replays,
        "valid_proposals": valid,
        "invalid_or_fallback_proposals": invalid,
        "proposal_valid_rate": round(valid / len(proposals), 3) if proposals else 0.0,
        "disagreements": disagreements,
        "disagreement_rate": round(disagreements / valid, 3) if valid else 0.0,
        "role_runs_recorded": role_runs,
        "outcome_links": len(links),
        "historical_outcome_links": len(historical_links),
        "linked_proposals": linked_outcomes,
        "accepted_links": accepted_links,
        "synced_role_outcomes": synced_links,
        "linked_successes": outcome_successes,
        "linked_success_rate": (
            round(outcome_successes / synced_links, 3) if synced_links else 0.0
        ),
        "linked_disagreements": disagreement_outcomes,
        "historical_linked_proposals": historical_outcomes,
        "historical_linked_successes": historical_successes,
        "historical_linked_success_rate": (
            round(historical_successes / historical_outcomes, 3) if historical_outcomes else 0.0
        ),
        "historical_linked_disagreements": historical_disagreement_outcomes,
        "backend_distribution": by_backend,
        "watch_state_distribution": states,
        "baseline_action_distribution": baseline_actions,
        "proposal_action_distribution": proposal_actions,
        "plan_action_distribution": plan_actions,
        "readiness_target": READINESS_TARGET,
        "linked_outcome_target": LINKED_OUTCOME_TARGET,
        "disagreement_outcome_target": DISAGREEMENT_OUTCOME_TARGET,
        "ready_for_supervised_apply": ready_for_supervised_apply,
        "ready_for_historical_replay_analysis": ready_for_historical_replay_analysis,
        "autonomous_redirect_enabled": False,
        "corpus": str(corpus_path),
    }
    # Compatibility aliases for older caller copy and human CLI output.
    summary["divergence_count"] = summary["disagreements"]
    summary["divergence_rate"] = summary["disagreement_rate"]
    summary["ready_for_analysis"] = ready_for_analysis
    return summary


def historical_candidates_from_keepalive(
    *,
    keepalive_corpus_path: Path | None = None,
    limit: int = DEFAULT_HISTORICAL_CANDIDATE_LIMIT,
    include_calibration: bool = False,
) -> dict:
    """Surface old keepalive PRs that are worth RedirectAgent replay.

    These rows are deliberately *not* copied into the RedirectAgent proposal
    corpus. keepalive_shadow rows were produced by deterministic
    redirect_policy snapshots, and many include outcome labels. They are useful
    to find replay targets, but only a fresh RedirectAgent proposal on a
    reconstructed/blinded report should count toward supervised-apply evidence.
    """
    import keepalive_shadow

    corpus_path = keepalive_corpus_path or keepalive_shadow.CORPUS_PATH
    rows = _iter_events(corpus_path)
    candidates: list[dict] = []
    calibration_excluded = 0
    missing_target = 0
    failure_outcomes = set(getattr(keepalive_shadow, "FAILURE_OUTCOMES", set()))
    success_outcomes = set(getattr(keepalive_shadow, "SUCCESS_OUTCOMES", set()))

    def as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    for row in rows:
        if not isinstance(row, dict):
            continue
        target = row.get("target")
        if not target:
            missing_target += 1
            continue
        outcome = row.get("outcome")
        blunt = row.get("keepalive_blunt") or ""
        shadow_action = row.get("shadow_action") or ""
        signals = row.get("signals_summary") or {}
        raw_disagreement = bool(row.get("disagreement"))
        meaningful = keepalive_shadow.meaningful_disagreement(blunt, shadow_action, outcome)
        is_failure = outcome in failure_outcomes
        is_success = outcome in success_outcomes
        failure_count = as_int(signals.get("failure_count"))
        no_progress = as_int(signals.get("consecutive_no_progress"))
        churn = as_int(signals.get("rounds_without_task_completion"))

        reasons: list[str] = []
        priority = 0
        if meaningful:
            reasons.append("failure_disagreement")
            priority += 100
        if is_failure:
            reasons.append(f"failure_outcome:{outcome}")
            priority += 70
        if blunt == "needs-human":
            reasons.append("keepalive_intervention:needs-human")
            priority += 45
        elif blunt == "switch-agent":
            reasons.append("keepalive_intervention:switch-agent")
            priority += 35
        if failure_count >= 3:
            reasons.append(f"failure_count:{failure_count}")
            priority += 30
        if no_progress >= 3:
            reasons.append(f"consecutive_no_progress:{no_progress}")
            priority += 25
        if churn >= 2:
            reasons.append(f"rounds_without_task_completion:{churn}")
            priority += 20
        if raw_disagreement:
            reasons.append("baseline_disagreement")
            priority += 15

        if not reasons:
            continue

        calibration_only = (
            is_success
            and raw_disagreement
            and not meaningful
            and blunt == "continue"
            and failure_count < 3
            and no_progress < 3
            and churn < 2
        )
        if calibration_only and not include_calibration:
            calibration_excluded += 1
            continue

        if row.get("source") == "backfill":
            reasons.append("historical_final_state_only")
            priority -= 5

        candidates.append(
            {
                "target": target,
                "source": row.get("source") or "",
                "outcome": outcome,
                "watch_state": row.get("watch_state") or "",
                "keepalive_blunt": blunt,
                "shadow_action": shadow_action,
                "disagreement": raw_disagreement,
                "meaningful_disagreement": meaningful,
                "category": (
                    "calibration_only" if calibration_only else "redirect_replay_candidate"
                ),
                "priority": priority,
                "reasons": reasons,
                "signals_summary": signals,
                "evidence_status": "candidate_only_not_redirectagent_proposal",
                "required_next_step": "reconstruct_or_blind_watch_report_then_record_fresh_redirectagent_proposal",
            }
        )

    candidates.sort(key=lambda item: (-item["priority"], item["target"]))
    limited = candidates[: max(0, limit)]
    return {
        "kind": "redirect_historical_candidates",
        "source": "keepalive-shadow",
        "corpus": str(corpus_path),
        "rows_seen": len(rows),
        "candidate_count": len(candidates),
        "returned": len(limited),
        "limit": limit,
        "calibration_only_excluded": calibration_excluded,
        "missing_target_rows": missing_target,
        "include_calibration": include_calibration,
        "not_counted_as_redirectagent_evidence": True,
        "items": limited,
    }


def _already_historical_replayed_targets(corpus_path: Path = CORPUS_PATH) -> set[str]:
    return {
        str(e.get("target"))
        for e in _iter_events(corpus_path)
        if e.get("kind") == "redirect_proposal"
        and e.get("source") == HISTORICAL_SOURCE
        and e.get("valid_proposal") is True
        and e.get("target")
    }


def _historical_report_from_candidate(candidate: dict) -> dict:
    """Build a watch.py-shaped report from historical keepalive signals.

    The terminal outcome is intentionally withheld from this report. RedirectAgent
    sees only state/signal evidence that would have been visible during the run.
    """
    signals_summary = candidate.get("signals_summary") or {}

    def as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    failure_count = as_int(signals_summary.get("failure_count"))
    no_progress = as_int(signals_summary.get("consecutive_no_progress"))
    churn = as_int(signals_summary.get("rounds_without_task_completion"))
    watch_state = candidate.get("watch_state") or ""
    if watch_state in {"running", "progress", "stalled", "exited", "missing"}:
        state = watch_state
    elif no_progress >= 3:
        state = "stalled"
    elif churn >= 2 or failure_count >= 3:
        state = "running"
    else:
        state = "progress"

    drift = {"severity": "none", "findings": [], "changed_path_count": 0}
    if churn >= 2:
        drift = {
            "severity": "medium",
            "findings": [
                {
                    "kind": "broad_churn",
                    "detail": (
                        "historical keepalive marker reported commits or rounds "
                        "without task completion"
                    ),
                    "paths": [],
                }
            ],
            "changed_path_count": None,
        }

    hints: list[dict] = []
    if failure_count:
        hints.append(
            {
                "kind": "gate_failure_rounds",
                "detail": f"historical keepalive marker counted {failure_count} failing rounds",
            }
        )
    if no_progress:
        hints.append(
            {
                "kind": "no_progress_rounds",
                "detail": f"historical keepalive marker counted {no_progress} no-progress rounds",
            }
        )

    if state == "stalled":
        recommended = "inspect"
    elif state == "exited":
        recommended = "collect"
    elif drift.get("severity") in {"medium", "high"}:
        recommended = "inspect"
    else:
        recommended = "wait"

    visible_reasons = [
        reason
        for reason in (candidate.get("reasons") or [])
        if not str(reason).startswith("failure_outcome:")
        and reason not in {"failure_disagreement", "historical_final_state_only"}
    ]
    log_tail = "\n".join(
        [
            "Historical replay from keepalive shadow signals.",
            "Terminal outcome is intentionally withheld from RedirectAgent.",
            f"keepalive_blunt={candidate.get('keepalive_blunt') or '-'}",
            f"failure_count={failure_count}",
            f"consecutive_no_progress={no_progress}",
            f"rounds_without_task_completion={churn}",
            f"gate_conclusion={signals_summary.get('gate_conclusion') or '-'}",
            f"visible_reasons={', '.join(visible_reasons) or '-'}",
        ]
    )
    return {
        "target": candidate.get("target") or "",
        "agent": "keepalive-agent",
        "lane": "closer",
        "task_type": "implement",
        "state": state,
        "recommended_action": recommended,
        "signals": {
            "pid_alive": None,
            "has_worktree_changes": state in {"progress", "exited"},
            "historical_replay": True,
        },
        "hints": hints,
        "drift": drift,
        "errors": [],
        "log_tail": log_tail,
    }


def _historical_outcome_from_candidate(candidate: dict) -> dict | None:
    outcome = candidate.get("outcome")
    target = candidate.get("target") or ""
    if not outcome:
        return _latest_keepalive_outcome_for_target(target)
    if outcome in {"durable", "merged"}:
        fallback = {
            "run_id": None,
            "target": target,
            "verifier_verdict": None,
            "adjudicated_verdict": "PASS",
            "merged": True,
            "ci_status": None,
            "durability": "durable" if outcome == "durable" else "pending",
            "notes": f"historical keepalive-shadow outcome={outcome}",
            "success": True,
        }
    elif outcome in {"needs_human", "closed_unmerged"}:
        fallback = {
            "run_id": None,
            "target": target,
            "verifier_verdict": None,
            "adjudicated_verdict": "FAIL",
            "merged": False,
            "ci_status": None,
            "durability": "abandoned",
            "notes": f"historical keepalive-shadow outcome={outcome}",
            "success": False,
        }
    elif outcome in {"reverted", "reopened", "reworked", "broke_later"}:
        fallback = {
            "run_id": None,
            "target": target,
            "verifier_verdict": None,
            "adjudicated_verdict": "PASS",
            "merged": True,
            "ci_status": None,
            "durability": outcome,
            "notes": f"historical keepalive-shadow outcome={outcome}",
            "success": False,
        }
    else:
        fallback = {
            "run_id": None,
            "target": target,
            "verifier_verdict": None,
            "adjudicated_verdict": None,
            "merged": None,
            "ci_status": None,
            "durability": "pending",
            "notes": f"historical keepalive-shadow outcome={outcome}",
            "success": None,
        }
    live = _latest_keepalive_outcome_for_target(target)
    return live or fallback


def _latest_keepalive_outcome_for_target(target: str) -> dict | None:
    if not target:
        return None
    try:
        with feedback._conn() as c:
            row = c.execute(
                "SELECT r.run_id, o.verifier_verdict, o.adjudicated_verdict, o.merged, "
                "o.ci_status, o.durability, o.notes FROM runs r JOIN outcomes o "
                "ON r.run_id=o.run_id WHERE r.target=? AND "
                "(r.source='keepalive' OR r.run_id LIKE 'keepalive:%') "
                "ORDER BY r.ts DESC LIMIT 1",
                (target,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    run_id, verifier, adjudicated, merged, ci, durability, notes = row
    return {
        "run_id": run_id,
        "target": target,
        "verifier_verdict": verifier,
        "adjudicated_verdict": adjudicated,
        "merged": bool(merged) if merged is not None else None,
        "ci_status": ci,
        "durability": durability,
        "notes": notes,
        "success": bool(feedback._is_success(durability, adjudicated, verifier)),
    }


def collect_historical_from_keepalive(
    *,
    keepalive_corpus_path: Path | None = None,
    limit: int = DEFAULT_HISTORICAL_CANDIDATE_LIMIT,
    include_calibration: bool = False,
    backend: str | None = None,
    dispatch: bool = False,
    timeout: int = 600,
    force: bool = False,
    corpus_path: Path = CORPUS_PATH,
) -> dict:
    """Replay historical keepalive candidates through RedirectAgent in shadow.

    With dispatch=False this is a dry preview. With dispatch=True it spends the
    selected/routed backend, records proposal rows as historical replay, and
    appends counterfactual outcome links for analysis only.
    """
    already = set() if force else _already_historical_replayed_targets(corpus_path)
    candidate_limit = limit if force else max(limit, limit + len(already))
    candidate_result = historical_candidates_from_keepalive(
        keepalive_corpus_path=keepalive_corpus_path,
        limit=candidate_limit,
        include_calibration=include_calibration,
    )
    selected = [
        item
        for item in candidate_result["items"]
        if item.get("target") and item.get("target") not in already
    ][: max(0, limit)]
    if not dispatch:
        return {
            "dry_run": True,
            "candidate_count": candidate_result["candidate_count"],
            "returned_candidates": candidate_result["returned"],
            "selection_limit": limit,
            "already_replayed": sorted(already),
            "would_collect": len(selected),
            "items": selected,
        }

    import roles

    ac = (
        "Historical RedirectAgent replay. Decide the correct next action using "
        "only the supplied watch signals. The terminal PR outcome is withheld "
        "from the prompt and is linked afterward as counterfactual analysis data."
    )
    collected: list[dict] = []
    for item in selected:
        report = _historical_report_from_candidate(item)
        result = roles.run_redirect_agent(
            report,
            ac,
            backend=backend,
            dispatch=True,
            lane="closer",
            task_type="implement",
            timeout=timeout,
        )
        entry = build_entry(result, report, ac, source=HISTORICAL_SOURCE)
        entry["historical_replay"] = {
            "candidate_reasons": item.get("reasons") or [],
            "keepalive_source": item.get("source") or "",
            "keepalive_blunt": item.get("keepalive_blunt") or "",
            "outcome_withheld_from_prompt": True,
            "counterfactual_not_role_learning": True,
        }
        rec = _append_event(entry, corpus_path)
        outcome = _historical_outcome_from_candidate(item)
        linked = None
        if outcome and result.get("role_run_id"):
            linked = historical_outcome_link(
                role_run_id=result.get("role_run_id"),
                target=item.get("target") or "",
                outcome=outcome,
                entry_id=entry.get("entry_id"),
                notes="historical replay counterfactual link; not accepted/applied",
                corpus_path=corpus_path,
            )
        collected.append(
            {
                "target": item.get("target"),
                "entry_id": entry.get("entry_id"),
                "role_run_id": result.get("role_run_id"),
                "backend": result.get("backend"),
                "decision_source": result.get("decision_source"),
                "baseline_action": entry.get("baseline_action"),
                "proposal_action": entry.get("proposal_action"),
                "valid_proposal": entry.get("valid_proposal"),
                "disagreement": entry.get("disagreement"),
                "errors": result.get("errors") or [],
                "outcome_linked": bool(linked),
                "outcome_success": (outcome or {}).get("success"),
                "recorded": rec.get("recorded"),
            }
        )
    return {
        "dry_run": False,
        "candidate_count": candidate_result["candidate_count"],
        "returned_candidates": candidate_result["returned"],
        "selection_limit": limit,
        "already_replayed": sorted(already),
        "collected": len(collected),
        "items": collected,
        "summary": summarize(corpus_path),
    }


def _selftest() -> None:
    import shutil
    import tempfile

    old_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="redirect-shadow-")
    feedback.DB_PATH = Path(tmp) / "feedback.db"
    corpus = Path(tmp) / "shadow.jsonl"
    try:
        report = {
            "agent": "cursor",
            "target": "stranske/Repo#17",
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
            "drift": {"severity": "none", "findings": [], "changed_path_count": 0},
            "log_tail": "HTTP 401\n",
        }
        proposal = {
            "action": "redirect",
            "reason": "stale auth token",
            "confidence": "high",
            "corrected_prompt": "Retry with valid auth, stay in src/, validate, commit, push, open PR.",
            "switch_agent": "codex",
        }
        recorded = record_redirect(
            report,
            "Endpoint returns 200.",
            proposal_json=proposal,
            corpus_path=corpus,
        )
        entry = recorded["entry"]
        assert entry["valid_proposal"] is True and entry["proposal_action"] == "redirect", entry
        assert entry["mutates_state"] is False and entry["plan"]["dry_run_only"] is True, entry
        diagnostic_entry = build_entry(
            {
                "mutates_state": False,
                "backend": "gemini",
                "role_run_id": "role:redirect:gemini:selftest",
                "backend_run_id": "offload:gemini:selftest",
                "decision_source": "baseline_policy",
                "proposal": None,
                "baseline": {
                    "action": "inspect",
                    "confidence": "medium",
                    "reason": "baseline",
                },
                "errors": ["backend exit=70 agent returned no stdout; agy log tail captured"],
                "plan": {"action": "inspect", "dry_run_only": True},
                "raw_output": "",
                "backend_error_detail": "neither PlanModel nor RequestedModel specified. You must specify a valid model.",
            },
            report,
            "Endpoint returns 200.",
            source="live-dispatch",
        )
        assert diagnostic_entry["valid_proposal"] is False, diagnostic_entry
        assert "backend_error_detail_preview" in diagnostic_entry, diagnostic_entry
        assert "PlanModel" in diagnostic_entry["backend_error_detail_preview"], diagnostic_entry
        s1 = summarize(corpus)
        assert s1["n"] == 1 and s1["valid_proposals"] == 1, s1
        assert (
            s1["autonomous_redirect_enabled"] is False and not s1["ready_for_supervised_apply"]
        ), s1

        feedback.record_role_run(
            "role-shadow-good",
            "redirect",
            "stranske/Repo#17",
            "cursor",
            action="redirect",
            decision_source="redirect_agent",
            proposal=proposal,
        )
        manual = dict(entry)
        manual["entry_id"] = "manual-with-role-run"
        manual["role_run_id"] = "role-shadow-good"
        manual["source"] = "live-dispatch"
        _append_event(manual, corpus)
        feedback.record_run("downstream-shadow-good", "stranske/Repo#17", "implement", "codex")
        feedback.record_outcome(
            "downstream-shadow-good",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        linked = link_outcome(
            "role-shadow-good",
            "downstream-shadow-good",
            entry_id="manual-with-role-run",
            notes="accepted shadow redirect",
            corpus_path=corpus,
        )
        assert linked["event"]["link_result"]["synced"] is True, linked
        s2 = summarize(corpus)
        assert s2["n"] == 2 and s2["role_runs_recorded"] == 1, s2
        assert s2["synced_role_outcomes"] == 1 and s2["linked_successes"] == 1, s2

        # Supervised apply requires a large enough valid shadow corpus, not just
        # enough linked successful outcomes from a thin hand-picked subset.
        thin_ready_corpus = Path(tmp) / "thin-ready.jsonl"
        for index in range(LINKED_OUTCOME_TARGET):
            role_run_id = f"role-thin-{index}"
            downstream_run_id = f"downstream-thin-{index}"
            feedback.record_role_run(
                role_run_id,
                "redirect",
                f"stranske/Repo#{100 + index}",
                "cursor",
                action="redirect",
                decision_source="redirect_agent",
                proposal=proposal,
            )
            feedback.record_run(
                downstream_run_id,
                f"stranske/Repo#{100 + index}",
                "implement",
                "codex",
            )
            feedback.record_outcome(
                downstream_run_id,
                adjudicated_verdict="PASS",
                merged=True,
                durability="durable",
            )
            thin_entry = dict(entry)
            thin_entry["entry_id"] = f"thin-valid-{index}"
            thin_entry["target"] = f"stranske/Repo#{100 + index}"
            thin_entry["role_run_id"] = role_run_id
            thin_entry["source"] = "live-dispatch"
            thin_entry["baseline_action"] = "inspect"
            thin_entry["proposal_action"] = "redirect"
            thin_entry["disagreement"] = True
            _append_event(thin_entry, thin_ready_corpus)
            link_outcome(
                role_run_id,
                downstream_run_id,
                entry_id=thin_entry["entry_id"],
                corpus_path=thin_ready_corpus,
            )
        thin_summary = summarize(thin_ready_corpus)
        assert thin_summary["valid_proposals"] == LINKED_OUTCOME_TARGET, thin_summary
        assert thin_summary["synced_role_outcomes"] == LINKED_OUTCOME_TARGET, thin_summary
        assert thin_summary["linked_disagreements"] == LINKED_OUTCOME_TARGET, thin_summary
        assert thin_summary["ready_for_analysis"] is False, thin_summary
        assert thin_summary["ready_for_supervised_apply"] is False, thin_summary

        for index in range(READINESS_TARGET - LINKED_OUTCOME_TARGET):
            extra_entry = dict(entry)
            extra_entry["entry_id"] = f"extra-valid-{index}"
            extra_entry["target"] = f"stranske/Repo#{200 + index}"
            extra_entry["role_run_id"] = None
            extra_entry["source"] = "replay"
            extra_entry["baseline_action"] = "wait"
            extra_entry["proposal_action"] = "wait"
            extra_entry["disagreement"] = False
            _append_event(extra_entry, thin_ready_corpus)
        ready_summary = summarize(thin_ready_corpus)
        assert ready_summary["valid_proposals"] == READINESS_TARGET, ready_summary
        assert ready_summary["ready_for_analysis"] is True, ready_summary
        assert ready_summary["ready_for_supervised_apply"] is True, ready_summary

        keepalive_corpus = Path(tmp) / "keepalive-shadow.jsonl"
        _append_event(
            {
                "target": "stranske/Repo#300",
                "source": "backfill",
                "outcome": "needs_human",
                "keepalive_blunt": "needs-human",
                "shadow_action": "redirect",
                "disagreement": True,
                "signals_summary": {"failure_count": 3},
            },
            keepalive_corpus,
        )
        _append_event(
            {
                "target": "stranske/Repo#301",
                "source": "backfill",
                "outcome": "durable",
                "keepalive_blunt": "continue",
                "shadow_action": "inspect",
                "disagreement": True,
                "signals_summary": {},
            },
            keepalive_corpus,
        )
        _append_event(
            {
                "target": "stranske/Repo#302",
                "source": "shadow",
                "outcome": None,
                "keepalive_blunt": "switch-agent",
                "shadow_action": "inspect",
                "disagreement": True,
                "signals_summary": {"consecutive_no_progress": 3},
            },
            keepalive_corpus,
        )
        historical = historical_candidates_from_keepalive(
            keepalive_corpus_path=keepalive_corpus,
            limit=10,
        )
        assert historical["candidate_count"] == 2, historical
        assert historical["calibration_only_excluded"] == 1, historical
        assert historical["not_counted_as_redirectagent_evidence"] is True, historical
        historical_with_calibration = historical_candidates_from_keepalive(
            keepalive_corpus_path=keepalive_corpus,
            limit=10,
            include_calibration=True,
        )
        assert historical_with_calibration["candidate_count"] == 3, historical_with_calibration
        replay_report = _historical_report_from_candidate(historical["items"][0])
        assert replay_report["target"] == "stranske/Repo#300", replay_report
        assert "needs_human" not in json.dumps(replay_report), replay_report
        dry_collect = collect_historical_from_keepalive(
            keepalive_corpus_path=keepalive_corpus,
            limit=10,
            corpus_path=Path(tmp) / "historical-collect.jsonl",
        )
        assert dry_collect["dry_run"] is True and dry_collect["would_collect"] == 2, dry_collect
        already_replayed_corpus = Path(tmp) / "historical-already.jsonl"
        _append_event(
            {
                "kind": "redirect_proposal",
                "schema_version": SCHEMA_VERSION,
                "entry_id": "already-replayed-top",
                "source": HISTORICAL_SOURCE,
                "target": "stranske/Repo#300",
                "valid_proposal": True,
                "errors": [],
            },
            already_replayed_corpus,
        )
        next_page_collect = collect_historical_from_keepalive(
            keepalive_corpus_path=keepalive_corpus,
            limit=1,
            corpus_path=already_replayed_corpus,
        )
        assert next_page_collect["would_collect"] == 1, next_page_collect
        assert next_page_collect["items"][0]["target"] == "stranske/Repo#302", next_page_collect
        invalid_replayed_corpus = Path(tmp) / "historical-invalid-retry.jsonl"
        _append_event(
            {
                "kind": "redirect_proposal",
                "schema_version": SCHEMA_VERSION,
                "entry_id": "invalid-replayed-top",
                "source": HISTORICAL_SOURCE,
                "target": "stranske/Repo#300",
                "valid_proposal": False,
                "errors": ["parse failed"],
            },
            invalid_replayed_corpus,
        )
        retry_collect = collect_historical_from_keepalive(
            keepalive_corpus_path=keepalive_corpus,
            limit=1,
            corpus_path=invalid_replayed_corpus,
        )
        assert retry_collect["would_collect"] == 1, retry_collect
        assert retry_collect["items"][0]["target"] == "stranske/Repo#300", retry_collect

        hist_entry = dict(entry)
        hist_entry["entry_id"] = "historical-replay-with-outcome"
        hist_entry["role_run_id"] = "role-shadow-historical"
        hist_entry["source"] = HISTORICAL_SOURCE
        hist_entry["baseline_action"] = "inspect"
        hist_entry["proposal_action"] = "redirect"
        hist_entry["disagreement"] = True
        _append_event(hist_entry, corpus)
        historical_outcome_link(
            role_run_id="role-shadow-historical",
            target="stranske/Repo#300",
            outcome={"success": False, "durability": "abandoned"},
            entry_id=hist_entry["entry_id"],
            corpus_path=corpus,
        )
        hist_summary = summarize(corpus)
        assert hist_summary["historical_replays"] == 1, hist_summary
        assert hist_summary["historical_linked_proposals"] == 1, hist_summary
        assert hist_summary["historical_linked_disagreements"] == 1, hist_summary

        # A rejected role proposal is audit-linked but not synced as learning evidence.
        feedback.record_role_run("role-shadow-rejected", "redirect", "stranske/Repo#18", "codex")
        feedback.record_run("downstream-rejected", "stranske/Repo#18", "implement", "codex")
        feedback.record_outcome(
            "downstream-rejected",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        rejected = link_outcome(
            "role-shadow-rejected",
            "downstream-rejected",
            accepted=False,
            corpus_path=corpus,
        )
        assert rejected["event"]["link_result"]["linked"] is True, rejected
        assert rejected["event"]["link_result"]["synced"] is False, rejected
        assert _outcome_for_run("role-shadow-rejected") is None

        try:
            record_redirect(report, "AC", corpus_path=corpus)
        except ValueError as exc:
            assert "--dispatch or --proposal-json" in str(exc), exc
        else:
            raise AssertionError("record_redirect without proposal source should fail")
        print(
            "redirect_shadow.py selftest: OK (record proposal, summarize, link outcomes; no apply)"
        )
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0

    parser = argparse.ArgumentParser(
        description="RedirectAgent shadow proposal corpus (records only; never applies redirects)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="run RedirectAgent in shadow and append a proposal event")
    rec.add_argument(
        "--report-json",
        default="",
        help="watch.py JSON report; stdin used when omitted",
    )
    rec.add_argument("--ac", "--acceptance-criteria", dest="ac", default="")
    rec.add_argument("--ac-file", default="")
    rec.add_argument("--attempt-history-json", default="")
    rec.add_argument("--backend", default="")
    rec.add_argument("--dispatch", action="store_true", help="call the routed/forced backend")
    rec.add_argument(
        "--proposal-json",
        default="",
        help="replay a captured proposal instead of dispatch",
    )
    rec.add_argument("--lane", default="")
    rec.add_argument("--task-type", default="")
    rec.add_argument("--next-agent", default="")
    rec.add_argument("--timeout", type=int, default=600)
    rec.add_argument("--high-leverage", action="store_true")
    rec.add_argument("--corpus", default=str(CORPUS_PATH))
    rec.add_argument("--json", action="store_true", dest="as_json")

    sm = sub.add_parser("summarize", help="summarize RedirectAgent shadow evidence")
    sm.add_argument("--corpus", default=str(CORPUS_PATH))
    sm.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="accepted for consistency; summarize output is always JSON",
    )

    hist = sub.add_parser(
        "historical-candidates",
        help="list historical keepalive rows that are candidates for RedirectAgent replay",
    )
    hist.add_argument("--keepalive-corpus", default="")
    hist.add_argument("--limit", type=int, default=DEFAULT_HISTORICAL_CANDIDATE_LIMIT)
    hist.add_argument(
        "--include-calibration",
        action="store_true",
        help="also include success-only disagreements as lower-priority calibration cases",
    )
    hist.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="accepted for consistency; historical-candidates output is always JSON",
    )

    collect = sub.add_parser(
        "collect-historical",
        help="replay historical keepalive candidates through RedirectAgent and record analysis links",
    )
    collect.add_argument("--keepalive-corpus", default="")
    collect.add_argument("--limit", type=int, default=DEFAULT_HISTORICAL_CANDIDATE_LIMIT)
    collect.add_argument(
        "--include-calibration",
        action="store_true",
        help="also include success-only disagreements as lower-priority calibration cases",
    )
    collect.add_argument("--backend", default="")
    collect.add_argument(
        "--dispatch",
        action="store_true",
        help="actually call RedirectAgent backend; omitted means dry-run preview",
    )
    collect.add_argument("--timeout", type=int, default=600)
    collect.add_argument("--force", action="store_true")
    collect.add_argument("--corpus", default=str(CORPUS_PATH))
    collect.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="accepted for consistency; collect-historical output is always JSON",
    )

    lk = sub.add_parser(
        "link-outcome", help="link an accepted/applied role run to a downstream outcome"
    )
    lk.add_argument("--role-run-id", required=True)
    lk.add_argument("--influenced-run-id", required=True)
    lk.add_argument("--entry-id", default="")
    lk.add_argument("--not-accepted", action="store_true")
    lk.add_argument("--notes", default="")
    lk.add_argument("--corpus", default=str(CORPUS_PATH))
    lk.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)
    if args.cmd == "record":
        if not args.dispatch and not args.proposal_json:
            parser.error(
                "record requires --dispatch or --proposal-json; use roles.py redirect for route-only"
            )
        report = _read_json(args.report_json)
        history = (
            json.loads(Path(args.attempt_history_json).read_text(encoding="utf-8"))
            if args.attempt_history_json
            else None
        )
        proposal = (
            json.loads(Path(args.proposal_json).read_text(encoding="utf-8"))
            if args.proposal_json
            else None
        )
        ac = _read_text_arg(args.ac, args.ac_file)
        result = record_redirect(
            report,
            ac,
            attempt_history=history,
            backend=(args.backend or None),
            dispatch=args.dispatch,
            proposal_json=proposal,
            high_leverage=args.high_leverage,
            lane=(args.lane or None),
            task_type=(args.task_type or None),
            next_agent=(args.next_agent or None),
            timeout=args.timeout,
            corpus_path=Path(args.corpus),
        )
        if args.as_json:
            print(json.dumps(result, indent=2))
        else:
            entry = result["entry"]
            print(
                f"recorded {entry['entry_id']} backend={entry.get('backend') or '-'} "
                f"proposal={entry.get('proposal_action') or '-'} "
                f"baseline={entry.get('baseline_action') or '-'} "
                f"corpus={result['corpus']}"
            )
        return 0
    if args.cmd == "summarize":
        print(json.dumps(summarize(Path(args.corpus)), indent=2))
        return 0
    if args.cmd == "historical-candidates":
        path = Path(args.keepalive_corpus) if args.keepalive_corpus else None
        print(
            json.dumps(
                historical_candidates_from_keepalive(
                    keepalive_corpus_path=path,
                    limit=args.limit,
                    include_calibration=args.include_calibration,
                ),
                indent=2,
            )
        )
        return 0
    if args.cmd == "collect-historical":
        path = Path(args.keepalive_corpus) if args.keepalive_corpus else None
        print(
            json.dumps(
                collect_historical_from_keepalive(
                    keepalive_corpus_path=path,
                    limit=args.limit,
                    include_calibration=args.include_calibration,
                    backend=(args.backend or None),
                    dispatch=args.dispatch,
                    timeout=args.timeout,
                    force=args.force,
                    corpus_path=Path(args.corpus),
                ),
                indent=2,
            )
        )
        return 0
    if args.cmd == "link-outcome":
        result = link_outcome(
            args.role_run_id,
            args.influenced_run_id,
            accepted=not args.not_accepted,
            notes=args.notes or None,
            entry_id=args.entry_id or None,
            corpus_path=Path(args.corpus),
        )
        print(json.dumps(result, indent=2) if args.as_json else result)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
