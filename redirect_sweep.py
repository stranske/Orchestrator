#!/usr/bin/env python3
"""redirect_sweep.py - automatic shadow watch sweep for active local claims.

This is the cron-safe bridge from "watch/redirect plans exist" to "the system
checks active work automatically." By default it is deliberately read-only: no
kill, no claim release, no delegation, and no RedirectAgent live dispatch. It
only classifies active claims with watch.py and writes/prints advisory reports so
the orchestrator can act deliberately. An explicit --record-corpus path can turn
actionable sweep reports into RedirectAgent shadow evidence; even then it never
applies redirect/decompose plans or mutates live lanes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import claims
import provision
import watch

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
DEFAULT_REPORT = STATE_DIR / "redirect-sweep.json"
DEFAULT_MAX_REPORTS = 50
DEFAULT_SHADOW_ACTIONS = ("redirect", "decompose")
DEFAULT_MAX_SHADOW_RECORDS = 3
DEFAULT_SHADOW_DEDUPE_HOURS = 24


def _env_flag(env: dict, name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _safe_log_path(target: str, agent: str) -> Path:
    safe = target.replace("/", "__").replace("#", "_")
    return HANDOFF / "dispatch-logs" / f"{safe}.{agent}.log"


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _list_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value)]


def _policy_action(report: dict) -> str:
    policy = report.get("policy_decision") or {}
    return str(policy.get("action") or report.get("recommended_action") or "unknown")


def _claim_watch_inputs(target: str, meta: dict) -> dict:
    agent = str(meta.get("agent") or "")
    lane = str(meta.get("lane") or "")
    task_type = str(meta.get("task_type") or "")
    log = str(meta.get("log") or "")
    if not log and agent:
        guessed = _safe_log_path(target, agent)
        if guessed.exists():
            log = str(guessed)
    worktree = str(meta.get("worktree") or "")
    if not worktree and lane:
        try:
            candidate = provision.worktree_path(target, lane)
            if candidate.exists():
                worktree = str(candidate)
        except Exception:
            worktree = ""
    return {
        "agent": agent,
        "target": target,
        "lane": lane,
        "task_type": task_type,
        "pid": _int_or_none(meta.get("pid")),
        "log": log,
        "worktree": worktree,
        "base_ref": str(meta.get("base_ref") or ""),
        "expected_paths": _list_value(meta.get("expected_paths")),
    }


def _is_actionable(report: dict) -> bool:
    action = _policy_action(report)
    return (
        action in {"collect", "inspect", "redirect", "decompose"}
        and report.get("state") != "progress"
    )


def _sha_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_sweep_fingerprint(report: dict) -> str:
    """Fingerprint the actionable signal, excluding volatile age counters."""
    policy = report.get("policy_decision") or {}
    drift = report.get("drift") or {}
    signals = report.get("signals") or {}
    normalized = {
        "target": report.get("target") or "",
        "agent": report.get("agent") or "",
        "lane": report.get("lane") or "",
        "task_type": report.get("task_type") or "",
        "state": report.get("state") or "",
        "recommended_action": report.get("recommended_action") or "",
        "policy": {
            "action": policy.get("action") or "",
            "confidence": policy.get("confidence") or "",
            "reason": policy.get("reason") or "",
        },
        "expected_paths": sorted(str(p) for p in report.get("expected_paths") or []),
        "hints": [
            {"kind": h.get("kind") or "", "detail": h.get("detail") or ""}
            for h in report.get("hints") or []
        ],
        "drift": {
            "severity": drift.get("severity") or "",
            "changed_paths": sorted(str(p) for p in drift.get("changed_paths") or []),
            "changed_path_count": drift.get("changed_path_count"),
            "top_level_dirs": sorted(str(p) for p in drift.get("top_level_dirs") or []),
            "findings": drift.get("findings") or [],
        },
        "signals": {
            "pid_alive": signals.get("pid_alive"),
            "has_worktree_changes": signals.get("has_worktree_changes"),
            "git_available": signals.get("git_available"),
            "log_recent": signals.get("log_recent"),
            "worktree_recent": signals.get("worktree_recent"),
            "status_short": signals.get("status_short") or "",
            "uncommitted_diff_stat": signals.get("uncommitted_diff_stat") or "",
            "committed_diff_stat": signals.get("committed_diff_stat") or "",
        },
        "log_tail_sha256": _sha_text(report.get("log_tail") or "") or "",
        "errors": sorted(str(e) for e in report.get("errors") or []),
    }
    return _sha_json(normalized)


def _existing_sweep_fingerprints(corpus_path: Path, *, since_ts: int | None) -> set[str]:
    if not corpus_path.exists():
        return set()
    out: set[str] = set()
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("kind") != "redirect_proposal":
            continue
        if since_ts is not None and int(entry.get("ts") or 0) < since_ts:
            continue
        report_summary = entry.get("report") if isinstance(entry.get("report"), dict) else {}
        fp = entry.get("sweep_fingerprint") or report_summary.get("sweep_fingerprint")
        if fp:
            out.add(str(fp))
    return out


def _parse_action_set(
    value: str | list[str] | tuple[str, ...] | set[str] | None,
) -> set[str]:
    if value is None:
        return set(DEFAULT_SHADOW_ACTIONS)
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        items = [str(item).strip() for item in value]
    allowed = {"collect", "inspect", "redirect", "decompose"}
    return {item for item in items if item in allowed}


def _acceptance_criteria_for_report(report: dict) -> str:
    expected = ", ".join(str(p) for p in report.get("expected_paths") or []) or "(not specified)"
    policy = report.get("policy_decision") or {}
    lines = [
        f"Target: {report.get('target') or '(unknown target)'}",
        f"Lane/task: {report.get('lane') or '(unknown lane)'} / {report.get('task_type') or '(unknown task type)'}",
        f"Prior agent: {report.get('agent') or '(unknown agent)'}",
        f"Expected path scope: {expected}",
        "",
        "Acceptance criteria:",
        "- Diagnose the stalled/deviating delegated lane from the watch evidence only.",
        "- Prefer wait, collect, or inspect when the evidence does not clearly support redirect/decompose.",
        "- If redirecting or decomposing, produce a complete standalone corrected prompt that preserves scope.",
        "- Do not apply redirects, release claims, kill processes, or delegate work; this is shadow evidence only.",
    ]
    if policy.get("action"):
        lines.extend(
            [
                "",
                f"Baseline policy action: {policy.get('action')} ({policy.get('confidence') or 'unknown'} confidence).",
                f"Baseline reason: {policy.get('reason') or '(none)'}",
            ]
        )
    hints = report.get("hints") or []
    if hints:
        lines.append("")
        lines.append("Root-cause hints:")
        for hint in hints[:5]:
            lines.append(f"- {hint.get('kind')}: {hint.get('detail')}")
    drift = report.get("drift") or {}
    findings = drift.get("findings") or []
    if findings:
        lines.append("")
        lines.append("Drift findings:")
        for finding in findings[:5]:
            lines.append(f"- {finding.get('kind')}: {finding.get('detail')}")
    return "\n".join(lines).strip() + "\n"


def record_experiment_candidates(
    exp_id: str,
    meta: dict,
    exp_dir: Path,
    *,
    corpus_path: Path | None = None,
    backend: str | None = None,
    max_records: int | None = None,
    classify_fn=None,
    record_fn=None,
) -> dict:
    """Event-driven corpus intake (2026-07-08 audit item 11). The hourly sweep samples LIVE claims
    at tick time and found watched=0 for weeks — local dispatches finish in minutes, so the corpus
    froze on 2026-06-25 even after the recording flag shipped. Failure-shaped work DOES exist at
    experiment-followup time: arms whose done-marker rc is nonzero (signal-killed/crashed) or that
    finished with no recoverable diff. Classify those retrospectively with watch.classify_lane
    (dead pid + idle log is exactly the shape it classifies) and feed the SAME
    record_shadow_candidates path — identical caps, 24h dedupe, and never-mutates guarantees."""
    import redirect_shadow

    classify_fn = classify_fn or watch.classify_lane
    record_fn = record_fn or record_shadow_candidates
    corpus = corpus_path or redirect_shadow.CORPUS_PATH
    repo = str(meta.get("repo") or "")
    task_type = str(meta.get("task_type") or "implement")
    actionable: list[dict] = []
    checked: list[dict] = []
    for agent in meta.get("agents") or []:
        marker_path = exp_dir / "done" / f"{exp_id}:{agent}.json"
        rc = None
        try:
            rc = int((json.loads(marker_path.read_text()) or {}).get("rc"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            rc = None
        patch = exp_dir / f"diff-{agent}.patch"
        no_diff = (not patch.exists()) or patch.stat().st_size == 0
        failed = (rc is not None and rc != 0) or no_diff
        checked.append({"agent": agent, "rc": rc, "no_diff": no_diff, "failed": failed})
        if not failed:
            continue
        log = exp_dir / f"{agent}.log"
        if not log.exists():
            continue
        worktree = provision.WORKTREES_DIR / f"{provision.repo_slug(repo)}__{exp_id}__{agent}"
        report = classify_fn(
            agent=agent,
            target=f"{repo} [exp {exp_id}]",
            lane="experiment",
            task_type=task_type,
            pid=None,
            log=str(log),
            worktree=str(worktree) if worktree.exists() else None,
        )
        if _is_actionable(report):
            actionable.append(report)
    sweep_like = {
        "generated_at": int(time.time()),
        "source": "experiment-followup",
        "actionable": actionable,
    }
    kwargs: dict = {
        "corpus_path": corpus,
        "dispatch": True,
        "backend": backend or os.environ.get("ORCH_REDIRECT_SWEEP_BACKEND") or "cursor",
    }
    if max_records is not None:
        kwargs["max_records"] = max_records
    result = record_fn(sweep_like, **kwargs)
    result["experiment_arms_checked"] = checked
    result["experiment_actionable"] = len(actionable)
    return result


def record_shadow_candidates(
    sweep_report: dict,
    *,
    corpus_path: Path,
    actions: set[str] | None = None,
    max_records: int = DEFAULT_MAX_SHADOW_RECORDS,
    dedupe_hours: int = DEFAULT_SHADOW_DEDUPE_HOURS,
    dispatch: bool = False,
    proposal_json: dict | None = None,
    backend: str | None = None,
    timeout: int = 600,
    acceptance_criteria: str = "",
) -> dict:
    """Record actionable sweep rows into the RedirectAgent shadow corpus.

    This writes measurement evidence only. It never calls redirect_plan --apply,
    releases claims, kills processes, or delegates a replacement lane.
    """
    if not dispatch and proposal_json is None:
        raise ValueError("recording sweep candidates requires dispatch or proposal_json")

    import redirect_shadow

    selected_actions = set(DEFAULT_SHADOW_ACTIONS) if actions is None else set(actions)
    since_ts = None
    if dedupe_hours > 0:
        since_ts = int(time.time()) - int(dedupe_hours * 3600)
    seen = _existing_sweep_fingerprints(corpus_path, since_ts=since_ts)
    result = {
        "enabled": True,
        "dispatch": bool(dispatch),
        "proposal_replay": proposal_json is not None and not dispatch,
        "mutates_lane_state": False,
        "applies_redirect_plan": False,
        "corpus": str(corpus_path),
        "actions": sorted(selected_actions),
        "max_records": max_records,
        "dedupe_hours": dedupe_hours,
        "attempted_count": 0,
        "recorded_count": 0,
        "recorded": [],
        "skipped": [],
        "errors": [],
    }
    for report in sweep_report.get("actionable") or []:
        action = _policy_action(report)
        target = report.get("target") or ""
        agent = report.get("agent") or ""
        if action not in selected_actions:
            result["skipped"].append(
                {
                    "target": target,
                    "agent": agent,
                    "action": action,
                    "reason": "action-not-selected",
                }
            )
            continue
        fingerprint = _stable_sweep_fingerprint(report)
        if fingerprint in seen:
            result["skipped"].append(
                {
                    "target": target,
                    "agent": agent,
                    "action": action,
                    "reason": "duplicate-within-window",
                    "sweep_fingerprint": fingerprint,
                }
            )
            continue
        if result["recorded_count"] >= max(0, max_records):
            result["skipped"].append(
                {
                    "target": target,
                    "agent": agent,
                    "action": action,
                    "reason": "max-records-reached",
                }
            )
            continue

        report_for_record = dict(report)
        report_for_record["sweep_fingerprint"] = fingerprint
        ac = acceptance_criteria or _acceptance_criteria_for_report(report)
        result["attempted_count"] += 1
        try:
            recorded = redirect_shadow.record_redirect(
                report_for_record,
                ac,
                backend=backend,
                dispatch=dispatch,
                proposal_json=proposal_json,
                lane=report.get("lane") or None,
                task_type=report.get("task_type") or None,
                timeout=timeout,
                corpus_path=corpus_path,
                source="redirect-sweep-live" if dispatch else "redirect-sweep-replay",
            )
        except Exception as exc:
            result["errors"].append(
                {
                    "target": target,
                    "agent": agent,
                    "action": action,
                    "error": str(exc),
                }
            )
            continue
        entry = recorded.get("entry") or {}
        seen.add(fingerprint)
        result["recorded_count"] += 1
        result["recorded"].append(
            {
                "target": target,
                "agent": agent,
                "action": action,
                "entry_id": entry.get("entry_id"),
                "source": entry.get("source"),
                "valid_proposal": entry.get("valid_proposal"),
                "proposal_action": entry.get("proposal_action"),
                "baseline_action": entry.get("baseline_action"),
                "sweep_fingerprint": fingerprint,
            }
        )
    return result


def sweep(
    *,
    stale_seconds: int = watch.DEFAULT_STALE_SECONDS,
    ttl: int = claims.CLAIM_TTL_DEFAULT,
    max_reports: int = DEFAULT_MAX_REPORTS,
    now: float | None = None,
) -> dict:
    """Classify active claims and return an advisory report. No side effects."""
    active = claims.active_claims(ttl=ttl, include_meta=True)
    reports: list[dict] = []
    unwatchable: list[dict] = []
    action_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}

    # THE CAP MUST NOT DROP CLAIMS SILENTLY. `max_reports` truncates, and a truncated sweep reports
    # a smaller world than exists -- the same shape as a collection floor nobody watches. Count what
    # was skipped and return it, so "50 watched" can never quietly mean "50 of 300".
    ordered = list(active.items())
    truncated = ordered[max_reports:]
    for target, meta in ordered[:max_reports]:
        inputs = _claim_watch_inputs(target, meta)
        if inputs["pid"] is None and not inputs["log"] and not inputs["worktree"]:
            unwatchable.append(
                {
                    "target": target,
                    "agent": inputs["agent"],
                    "reason": "claim lacks pid/log/worktree metadata",
                }
            )
            continue
        report = watch.classify_lane(
            agent=inputs["agent"],
            target=target,
            lane=inputs["lane"],
            task_type=inputs["task_type"],
            pid=inputs["pid"],
            log=inputs["log"] or None,
            worktree=inputs["worktree"] or None,
            base_ref=inputs["base_ref"] or None,
            expected_paths=inputs["expected_paths"],
            stale_seconds=stale_seconds,
            now=now,
        )
        reports.append(report)
        action = (
            (report.get("policy_decision") or {}).get("action")
            or report.get("recommended_action")
            or "unknown"
        )
        state = report.get("state") or "unknown"
        action_counts[action] = action_counts.get(action, 0) + 1
        state_counts[state] = state_counts.get(state, 0) + 1

    actionable = [r for r in reports if _is_actionable(r)]
    return {
        "generated_at": int(time.time()),
        "dry_run_only": True,
        "mutates_state": False,
        "active_claim_count": len(active),
        "watched_count": len(reports),
        "truncated_count": len(truncated),
        "truncated_targets": [t for t, _ in truncated[:20]],
        "unwatchable_count": len(unwatchable),
        "actionable_count": len(actionable),
        "action_counts": action_counts,
        "state_counts": state_counts,
        "reports": reports,
        "actionable": actionable,
        "unwatchable": unwatchable,
    }


def write_report(report: dict, path: Path = DEFAULT_REPORT) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return {"path": str(path), "bytes": path.stat().st_size}


def _read_report_status(path: Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    out = {
        "path": str(path),
        "exists": path.exists(),
        "generated_at": None,
        "age_seconds": None,
        "active_claim_count": None,
        "watched_count": None,
        "actionable_count": None,
        "shadow_recording": None,
        "load_error": None,
    }
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        out["load_error"] = str(exc)
        return out
    generated = data.get("generated_at")
    out.update(
        {
            "generated_at": generated,
            "age_seconds": int(now - int(generated)) if generated else None,
            "active_claim_count": data.get("active_claim_count"),
            "watched_count": data.get("watched_count"),
            "actionable_count": data.get("actionable_count"),
            "shadow_recording": data.get("shadow_recording"),
        }
    )
    return out


def doctor(
    *,
    env: dict | None = None,
    state_dir: Path | None = None,
    corpus_path: Path | None = None,
    orchestrate_path: Path | None = None,
    run_sweep: bool = True,
    now: float | None = None,
) -> dict:
    """Read-only preflight for the redirect sweep and shadow-corpus bridge."""
    env = os.environ if env is None else env
    state_dir = state_dir or Path(env.get("ORCH_STATE_DIR", STATE_DIR))
    report_path = state_dir / "redirect-sweep.json"
    orchestrate_path = orchestrate_path or Path(__file__).with_name("orchestrate.sh")
    try:
        orchestrate_text = orchestrate_path.read_text(encoding="utf-8")
        cadence_step_present = "redirect_sweep.py" in orchestrate_text
        corpus_gate_present = "ORCH_REDIRECT_SWEEP_RECORD_CORPUS" in orchestrate_text
        orchestrate_error = None
    except OSError as exc:
        cadence_step_present = False
        corpus_gate_present = False
        orchestrate_error = str(exc)

    import redirect_shadow

    corpus_path = corpus_path or Path(
        env.get("ORCH_REDIRECT_SHADOW_CORPUS", redirect_shadow.CORPUS_PATH)
    )
    corpus_summary = redirect_shadow.summarize(corpus_path)
    live_sweep = sweep(now=now) if run_sweep else None
    recording_enabled = _env_flag(env, "ORCH_REDIRECT_SWEEP_RECORD_CORPUS")

    if not cadence_step_present:
        recommendation = "Redirect sweep cadence is not wired in orchestrate.sh."
    elif not recording_enabled:
        recommendation = (
            "Redirect sweep is cadence-wired in advisory mode; RedirectAgent shadow-corpus "
            "dispatch remains disabled until ORCH_REDIRECT_SWEEP_RECORD_CORPUS=1."
        )
    elif live_sweep and live_sweep.get("actionable_count", 0) == 0:
        recommendation = (
            "RedirectAgent shadow-corpus dispatch is enabled, but the current sweep has no "
            "actionable local claims to record."
        )
    else:
        recommendation = (
            "RedirectAgent shadow-corpus dispatch is enabled; the next cadence run can record "
            "capped shadow proposals for eligible actionable claims."
        )

    return {
        "read_only": True,
        "mutates_lane_state": False,
        "applies_redirect_plan": False,
        "autonomous_redirect_enabled": False,
        "cadence": {
            "orchestrate_sh": str(orchestrate_path),
            "orchestrate_load_error": orchestrate_error,
            "redirect_sweep_step_present": cadence_step_present,
            "record_corpus_env_gate_present": corpus_gate_present,
            "record_corpus_env_enabled": recording_enabled,
            "backend": env.get("ORCH_REDIRECT_SWEEP_BACKEND", ""),
            "actions": env.get("ORCH_REDIRECT_SWEEP_ACTIONS", ",".join(DEFAULT_SHADOW_ACTIONS)),
            "max_records": env.get("ORCH_REDIRECT_SWEEP_MAX_RECORDS", DEFAULT_MAX_SHADOW_RECORDS),
            "dedupe_hours": env.get(
                "ORCH_REDIRECT_SWEEP_DEDUPE_HOURS", DEFAULT_SHADOW_DEDUPE_HOURS
            ),
        },
        "last_report": _read_report_status(report_path, now=now),
        "live_sweep": (
            {
                "active_claim_count": live_sweep.get("active_claim_count"),
                "watched_count": live_sweep.get("watched_count"),
                "actionable_count": live_sweep.get("actionable_count"),
                "action_counts": live_sweep.get("action_counts"),
                "state_counts": live_sweep.get("state_counts"),
            }
            if live_sweep
            else None
        ),
        "shadow_corpus": {
            "corpus": corpus_summary.get("corpus"),
            "proposals": corpus_summary.get("n"),
            "valid_proposals": corpus_summary.get("valid_proposals"),
            "linked_proposals": corpus_summary.get("linked_proposals"),
            "historical_linked_proposals": corpus_summary.get("historical_linked_proposals"),
            "disagreements": corpus_summary.get("disagreements"),
            "linked_disagreements": corpus_summary.get("linked_disagreements"),
            "historical_linked_disagreements": corpus_summary.get(
                "historical_linked_disagreements"
            ),
            "ready_for_analysis": corpus_summary.get("ready_for_analysis"),
            "ready_for_supervised_apply": corpus_summary.get("ready_for_supervised_apply"),
        },
        "recommendation": recommendation,
    }


def format_doctor(report: dict) -> str:
    cadence = report["cadence"]
    sweep_report = report.get("live_sweep") or {}
    corpus = report.get("shadow_corpus") or {}
    lines = [
        "redirect_sweep doctor:",
        f"  cadence_wired={cadence['redirect_sweep_step_present']} "
        f"record_corpus_enabled={cadence['record_corpus_env_enabled']} "
        f"autonomous_redirect_enabled={report['autonomous_redirect_enabled']}",
        f"  live_sweep active={sweep_report.get('active_claim_count')} "
        f"watched={sweep_report.get('watched_count')} "
        f"actionable={sweep_report.get('actionable_count')}",
        f"  corpus proposals={corpus.get('proposals')} "
        f"valid={corpus.get('valid_proposals')} "
        f"linked={corpus.get('linked_proposals')} "
        f"ready_supervised={corpus.get('ready_for_supervised_apply')}",
        f"  recommendation: {report['recommendation']}",
    ]
    return "\n".join(lines)


def _selftest() -> None:
    import tempfile
    import shutil

    old_handoff = os.environ.get("HANDOFF_DIR")
    tmp = Path(tempfile.mkdtemp(prefix="redirect-sweep-"))
    os.environ["HANDOFF_DIR"] = str(tmp)
    global HANDOFF
    old_handoff_path = HANDOFF
    HANDOFF = tmp
    try:
        log_dir = tmp / "dispatch-logs"
        log_dir.mkdir(parents=True)

        # Truncation is COUNTED, not silent: 3 claims with a cap of 2 leaves exactly 1 reported.
        # A sweep that reports only `watched_count` claims a smaller world than exists.
        _fake = {
            f"o/r#{i}": {"agent": "codex", "pid": None, "log": "", "worktree": ""} for i in range(3)
        }
        _orig_active = claims.active_claims
        try:
            claims.active_claims = lambda **kw: _fake
            _t = sweep(max_reports=2)
            assert _t["active_claim_count"] == 3 and _t["truncated_count"] == 1, _t
            assert len(_t["truncated_targets"]) == 1, _t["truncated_targets"]
        finally:
            claims.active_claims = _orig_active
        log = log_dir / "o__r_1.cursor.log"
        log.write_text("HTTP 401 Unauthorized\n", encoding="utf-8")
        now = 1_800_000_000.0
        os.utime(log, (now - 900, now - 900))
        assert claims.claim("o/r#1", "cursor", pid=os.getpid())
        assert claims.update_metadata(
            "o/r#1",
            "cursor",
            pid=os.getpid(),
            log=str(log),
            lane="opener",
            task_type="implement",
            run_id="run-1",
        )
        out = sweep(stale_seconds=600, now=now)
        assert out["dry_run_only"] is True and out["mutates_state"] is False, out
        assert out["active_claim_count"] == 1 and out["watched_count"] == 1, out
        report = out["reports"][0]
        assert report["state"] == "stalled", report
        assert report["policy_decision"]["action"] in {"inspect", "redirect"}, report[
            "policy_decision"
        ]
        assert report["redirect_plan"]["dry_run_only"] is True, report["redirect_plan"]
        proposal = {
            "action": "redirect",
            "reason": "auth failure needs a fresh delegated attempt",
            "confidence": "high",
            "corrected_prompt": (
                "Take over o/r#1 after an auth-stalled cursor attempt. Keep the change in scope, "
                "refresh credentials or use an authenticated path, validate, commit, push, and open/update the PR."
            ),
            "switch_agent": "codex",
        }
        corpus = tmp / "shadow.jsonl"
        shadow = record_shadow_candidates(
            out,
            corpus_path=corpus,
            actions={"inspect", "redirect"},
            max_records=1,
            proposal_json=proposal,
        )
        assert shadow["recorded_count"] == 1 and shadow["mutates_lane_state"] is False, shadow
        assert corpus.exists() and "redirect-sweep-replay" in corpus.read_text(
            encoding="utf-8"
        ), shadow
        duplicate = record_shadow_candidates(
            out,
            corpus_path=corpus,
            actions={"inspect", "redirect"},
            max_records=1,
            proposal_json=proposal,
        )
        assert duplicate["recorded_count"] == 0, duplicate
        assert duplicate["skipped"][0]["reason"] == "duplicate-within-window", duplicate
        out["shadow_recording"] = shadow
        written = write_report(out, tmp / "redirect-sweep.json")
        assert Path(written["path"]).exists() and written["bytes"] > 0, written
        orchestrate = tmp / "orchestrate.sh"
        orchestrate.write_text(
            "python3 redirect_sweep.py\nORCH_REDIRECT_SWEEP_RECORD_CORPUS\n",
            encoding="utf-8",
        )
        diagnostic = doctor(
            env={"ORCH_STATE_DIR": str(tmp)},
            state_dir=tmp,
            corpus_path=corpus,
            orchestrate_path=orchestrate,
            run_sweep=False,
            now=now,
        )
        assert diagnostic["read_only"] is True, diagnostic
        assert diagnostic["mutates_lane_state"] is False, diagnostic
        assert diagnostic["cadence"]["redirect_sweep_step_present"] is True, diagnostic
        assert diagnostic["cadence"]["record_corpus_env_enabled"] is False, diagnostic
        assert diagnostic["last_report"]["exists"] is True, diagnostic
        assert diagnostic["shadow_corpus"]["proposals"] == 1, diagnostic
        assert "advisory mode" in diagnostic["recommendation"], diagnostic
        diagnostic_enabled = doctor(
            env={
                "ORCH_STATE_DIR": str(tmp),
                "ORCH_REDIRECT_SWEEP_RECORD_CORPUS": "1",
            },
            state_dir=tmp,
            corpus_path=corpus,
            orchestrate_path=orchestrate,
            run_sweep=False,
            now=now,
        )
        assert (
            diagnostic_enabled["cadence"]["record_corpus_env_enabled"] is True
        ), diagnostic_enabled
    finally:
        HANDOFF = old_handoff_path
        if old_handoff is None:
            os.environ.pop("HANDOFF_DIR", None)
        else:
            os.environ["HANDOFF_DIR"] = old_handoff
        shutil.rmtree(tmp, ignore_errors=True)

    # item 11: event-driven corpus intake from a completed experiment — only FAILED arms
    # (nonzero marker rc / no diff) are classified, and only actionable reports reach the
    # record path (fns injected; offline).
    etmp = Path(tempfile.mkdtemp(prefix="redirect-exp-selftest-"))
    try:
        exp_id = "EXPR1"
        edir = etmp / exp_id
        (edir / "done").mkdir(parents=True)
        meta = {
            "repo": "o/r",
            "base": "main",
            "agents": ["good", "killed", "silent"],
            "task_type": "implement",
        }
        for a in ("good", "killed", "silent"):
            (edir / f"{a}.log").write_text("log")
        (edir / "diff-good.patch").write_text("+++ b/x.py\n")  # succeeded: rc 0 + diff
        (edir / "done" / f"{exp_id}:good.json").write_text(
            json.dumps({"run_id": f"{exp_id}:good", "rc": 0, "ts": 1})
        )
        (edir / "done" / f"{exp_id}:killed.json").write_text(
            json.dumps({"run_id": f"{exp_id}:killed", "rc": 137, "ts": 1})
        )
        # 'silent': no marker, no diff -> failure-shaped
        classified: list[str] = []

        def fake_classify(**kw):
            classified.append(kw["agent"])
            return {"agent": kw["agent"], "target": kw["target"], "action": "retry"}

        captured: dict = {}

        def fake_record(sweep_like, **kw):
            captured["report"] = sweep_like
            captured["kwargs"] = kw
            return {"recorded_count": len(sweep_like["actionable"]), "skipped": []}

        def actionable_yes(report):
            return True

        global _is_actionable
        old_actionable = _is_actionable
        _is_actionable = actionable_yes
        try:
            res = record_experiment_candidates(
                exp_id,
                meta,
                edir,
                corpus_path=etmp / "shadow.jsonl",
                backend="cursor",
                classify_fn=fake_classify,
                record_fn=fake_record,
            )
        finally:
            _is_actionable = old_actionable
        assert sorted(classified) == ["killed", "silent"], classified  # 'good' untouched
        assert res["experiment_actionable"] == 2 and res["recorded_count"] == 2, res
        assert captured["report"]["source"] == "experiment-followup", captured
        assert captured["kwargs"]["dispatch"] is True, captured
        arm_map = {c["agent"]: c for c in res["experiment_arms_checked"]}
        assert arm_map["killed"]["rc"] == 137 and arm_map["killed"]["failed"], arm_map
        assert arm_map["good"]["failed"] is False, arm_map
    finally:
        shutil.rmtree(etmp, ignore_errors=True)
    print(
        "redirect_sweep.py selftest: OK (active-claim watch sweep, advisory report, no mutation, "
        "event-driven experiment corpus intake)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shadow-only automatic watch sweep for active claims."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="read-only redirect sweep/corpus preflight",
    )
    parser.add_argument("--stale-seconds", type=int, default=watch.DEFAULT_STALE_SECONDS)
    parser.add_argument("--ttl", type=int, default=claims.CLAIM_TTL_DEFAULT)
    parser.add_argument("--max-reports", type=int, default=DEFAULT_MAX_REPORTS)
    parser.add_argument("--write", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--record-corpus",
        action="store_true",
        help="append actionable sweep rows to the RedirectAgent shadow corpus",
    )
    parser.add_argument(
        "--dispatch-redirect-agent",
        action="store_true",
        help="call RedirectAgent for corpus recording; omitted means no live backend call",
    )
    parser.add_argument(
        "--proposal-json",
        default="",
        help="offline/replay RedirectAgent proposal JSON for corpus recording",
    )
    parser.add_argument("--corpus", default="", help="optional redirect shadow corpus path")
    parser.add_argument(
        "--shadow-actions",
        default=",".join(DEFAULT_SHADOW_ACTIONS),
        help="comma-separated policy actions eligible for corpus recording",
    )
    parser.add_argument("--max-shadow-records", type=int, default=DEFAULT_MAX_SHADOW_RECORDS)
    parser.add_argument("--dedupe-hours", type=int, default=DEFAULT_SHADOW_DEDUPE_HOURS)
    parser.add_argument(
        "--backend", default="", help="force a RedirectAgent backend when dispatching"
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--ac", "--acceptance-criteria", dest="acceptance_criteria", default="")
    parser.add_argument("--ac-file", default="")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.doctor and args.record_corpus:
        parser.error("--doctor cannot be combined with --record-corpus")
    if args.doctor:
        report = doctor(run_sweep=True)
        print(json.dumps(report, indent=2, default=str) if args.as_json else format_doctor(report))
        return 0
    if args.dispatch_redirect_agent and args.proposal_json:
        parser.error("--dispatch-redirect-agent and --proposal-json are mutually exclusive")
    if args.record_corpus and not args.dispatch_redirect_agent and not args.proposal_json:
        parser.error("--record-corpus requires --dispatch-redirect-agent or --proposal-json")
    report = sweep(stale_seconds=args.stale_seconds, ttl=args.ttl, max_reports=args.max_reports)
    if args.record_corpus:
        import redirect_shadow

        proposal = (
            json.loads(Path(args.proposal_json).read_text(encoding="utf-8"))
            if args.proposal_json
            else None
        )
        ac = args.acceptance_criteria
        if args.ac_file:
            ac = Path(args.ac_file).read_text(encoding="utf-8")
        corpus_path = Path(args.corpus) if args.corpus else redirect_shadow.CORPUS_PATH
        report["shadow_recording"] = record_shadow_candidates(
            report,
            corpus_path=corpus_path,
            actions=_parse_action_set(args.shadow_actions),
            max_records=args.max_shadow_records,
            dedupe_hours=args.dedupe_hours,
            dispatch=args.dispatch_redirect_agent,
            proposal_json=proposal,
            backend=(args.backend or None),
            timeout=args.timeout,
            acceptance_criteria=ac,
        )
    if args.write:
        written = write_report(report, Path(args.write))
        report["written"] = written
    if args.as_json or args.write:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"redirect_sweep: active={report['active_claim_count']} watched={report['watched_count']} "
            f"actionable={report['actionable_count']} unwatchable={report['unwatchable_count']}"
        )
        for item in report["actionable"]:
            policy = item.get("policy_decision") or {}
            print(
                f"  {item.get('target')} {item.get('agent')} state={item.get('state')} "
                f"action={policy.get('action') or item.get('recommended_action')}"
            )
        if report.get("shadow_recording"):
            shadow = report["shadow_recording"]
            print(
                f"  shadow_recording recorded={shadow['recorded_count']} "
                f"attempted={shadow['attempted_count']} corpus={shadow['corpus']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
