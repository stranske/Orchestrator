#!/usr/bin/env python3
"""Reconcile local Orchestrator ledger/log evidence into feedback.costs.

LangSmith artifacts and ccusage sessions cover richer execution telemetry. Local CLI
delegates sometimes do not emit either, so dispatcher writes start/complete rows into
the capacity ledger. This command turns those rows plus any JSON usage events in the
delegate log into `costs(source="ledger")`, without overwriting richer
`source="langsmith"` or `source="ccusage"` rows.

Usage:
  python3 ledger_reconcile.py complete --run-id ... --agent codex --log-file ...
  python3 ledger_reconcile.py reconcile --dry-run --json
  python3 ledger_reconcile.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import adapters
import credential_redaction
import execution_profiles
import feedback
import pushed_branches
import rate_incidents

# A RUN THE PROVIDER REFUSED BEFORE IT DID ANYTHING IS INFRASTRUCTURE, NOT CAPABILITY (§2).
# Measured 2026-10-04: five codex testgen delegates dispatched 2026-09-15 11:31-11:33Z each died in
# 4-5 s on "You've hit your usage limit" with no command run. A refusal is not a signal death, so the
# rc>128 rule below could never see them. (Their done markers said rc 0, but that was the claims
# release's status, which the dispatch wrapper read instead of the agent's until 2026-10-04; codex
# itself exits 1 on a refused turn, 10 of 10 in the offload ledger, which records the agent's own
# status.) Outcome ingest then scored them from whatever their issues' PRs did: two
# abandoned FAILs, and three PASSes credited through `orchestrator/issue-N` PRs another run had
# merged, two of them a day before the run started.
#
# Rows are classified only for runs STARTED at or after this instant. Older rows are the population
# the owner's 2026-10-04 decision covers (improvement log item 0), and this rule never rewrites them.
PROVIDER_LIMIT_INFRA_SINCE = 1791126000  # 2026-10-04T15:00:00Z, after that decision's measurement

# The completion step masks credentials only in the segments of runs STARTED at or after this
# instant. Logs that already held a token when the leak was found (2026-10-05) are the owner's to
# rotate and clean, and a `complete` re-run by hand for an old run must not edit them.
CREDENTIAL_SCRUB_SINCE = 1791244800  # 2026-10-06T00:00:00Z

# Why a run whose own log shows a provider refusal before any work was, or was not, classified.
# Every run counted in `seen` lands in exactly one of the others, so the counts are a partition.
PROVIDER_LIMIT_BUCKETS = (
    "classified",  # marked transient_infra now (in a dry run: would be)
    "excluded",  # already in a class no learner scores
    "merged",  # recorded as merged: a PASS for a run that ran nothing; mark_transient_infra refuses it
    "predates_rule",  # started before PROVIDER_LIMIT_INFRA_SINCE: the owner's decision governs it
    "no_outcome_row",  # nothing recorded for any learner to score; a later pass rechecks it
)


def provider_limit_before_work(lines: list[str]) -> dict | None:
    """The provider refused this run before it did anything: the evidence, or None.

    Exact or nothing. The evidence is the harness's own terminal `turn.failed` event, whose message
    the one text authority (`rate_incidents.classify_provider_failure`) rates as a high-confidence
    provider limit, in a segment where no work event appears at all. A phrase in the transcript is
    never enough: on 2026-10-04, 233 dispatch-log segments held a usage-limit phrase; of the 106
    codex ones, 15 carried the refusal event and 91 had the phrase only in prose or command output.

    Only codex's `exec --json` stream can say that nothing ran, so this reads that schema and no
    other. claude (`-p`), cursor and vibe log only their final text, and gemini records its tool
    steps in a separate per-run agy log. For those the answer is None, which means UNKNOWN, not "did
    work": two of eight gemini runs that printed "Individual quota reached" with no output had run
    tools for six minutes first.
    """
    failure = None
    for line in lines:
        event = rate_incidents.json_event(line)
        if event is None:
            continue
        if rate_incidents.is_codex_work_event(event):
            return None  # the run did something, so whatever ended it is not this rule's
        if event.get("type") == "turn.failed" and failure is None:
            error = event.get("error")
            message = str(error.get("message") or "") if isinstance(error, dict) else ""
            category, subcategory, confidence = rate_incidents.classify_provider_failure(message)
            if confidence == "high":
                failure = {"category": category, "subcategory": subcategory, "message": message}
    return failure


def _settle_provider_limit_death(run_id: str, evidence: dict, *, dry_run: bool) -> str:
    """Classify a run the provider refused before any work, when this rule owns its row.

    Returns the PROVIDER_LIMIT_BUCKETS entry that says why it was or was not classified. Only a row
    some learner still scores, recorded as not merged, for a run started under this rule, is
    written; everything else is left exactly as recorded and counted under its reason.
    """
    with feedback._conn() as c:
        row = c.execute(
            "SELECT r.ts, o.run_id, o.merged, o.failure_class FROM runs r "
            "LEFT JOIN outcomes o ON o.run_id=r.run_id WHERE r.run_id=?",
            (run_id,),
        ).fetchone()
    if row is None or row[1] is None:
        return "no_outcome_row"
    started, _, merged, failure_class = row
    if str(failure_class or "") in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
        return "excluded"
    if merged:
        return "merged"
    if int(started or 0) < PROVIDER_LIMIT_INFRA_SINCE:
        return "predates_rule"
    if dry_run:
        return "classified"
    reason = f"provider limit before any work: {evidence['subcategory']}"
    # False only when another writer classified (or merged) the row since the read above.
    return "classified" if feedback.mark_transient_infra(run_id, reason=reason) else "excluded"


def _ledger_path(path: Path | None = None) -> Path:
    return Path(path or adapters.LEDGER)


def _read_ledger(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return rows, errors
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}:{line_no}: invalid JSON: {exc.msg}")
                continue
            if not isinstance(row, dict):
                errors.append(f"{path}:{line_no}: row is not an object")
                continue
            rows.append(row)
    return rows, errors


def _known_runs() -> set[str]:
    with feedback._conn() as c:
        return {str(row[0]) for row in c.execute("SELECT run_id FROM runs").fetchall()}


def _cost_sources() -> dict[str, str]:
    with feedback._conn() as c:
        return {
            str(row[0]): str(row[1] or "")
            for row in c.execute("SELECT run_id, source FROM costs").fetchall()
        }


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _usage_from_obj(obj: dict[str, Any]) -> tuple[int, int]:
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
    if not isinstance(usage, dict):
        return 0, 0
    in_value = (
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("tokens_in")
        or usage.get("input_token_count")
        or 0
    )
    out_value = (
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("tokens_out")
        or usage.get("output_token_count")
        or 0
    )
    total_value = usage.get("total_tokens") or usage.get("tokens_total")
    tokens_in = _number(in_value) or 0
    tokens_out = _number(out_value) or 0
    total = _number(total_value)
    if total and not (tokens_in or tokens_out):
        tokens_in = total
    return int(tokens_in), int(tokens_out)


def _log_segment(path: Path, run_id: str) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    marker = f"run_id={run_id}"
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("===") and marker in line:
            start = idx + 1
    if start is None:
        # A dedicated legacy/external log may have no run delimiter at all. It is safe to use the
        # whole file only when no other run markers exist; mixed logs remain fail-closed.
        return lines if not any(line.startswith("===") for line in lines) else []
    end = len(lines)
    for idx in range(start, len(lines)):
        if lines[idx].startswith("==="):
            end = idx
            break
    return lines[start:end]


def _classify_run_log_segment(
    lines: list[str],
    agent: str,
    run_id: str,
    target: str | None = None,
    log_file: Path | None = None,
    *,
    shed: bool = True,
    successful: bool | None = None,
) -> dict | None:
    """Classify only this detached run's bounded log segment (fail-open)."""
    try:
        combined_text = "\n".join(lines)
        # The harness's own refusal event is provider evidence whatever the exit code: the dispatch
        # wrapper handed the claims release's status (0) to all five 2026-09-15 refusals, so the
        # gate below took them for ordinary output, and none recorded an incident or shed the seat.
        refusal = provider_limit_before_work(lines)
        if refusal is not None:
            combined_text = refusal["message"]
        # A FAILED run's log is error evidence, less the agent's own record of its work, which is
        # never the provider talking (rate_incidents.failure_evidence).
        elif successful is False:
            combined_text = "\n".join(rate_incidents.failure_evidence(lines))
        # Successful or provenance-unknown task logs are ordinary model output. Only the strict
        # successful-stdout envelope may promote their text to provider evidence; otherwise test
        # fixtures and reviews that discuss HTTP 429/resource exhaustion become incidents.
        elif not rate_incidents.stdout_carries_capacity_evidence(combined_text):
            return None
        evidence_result = rate_incidents.get_structured_evidence(
            error_text=combined_text,
            agent=agent,
            surface="ledger_reconcile.completion",
            run_id=run_id,
            target=target,
        )
    except Exception as exc:
        print(
            f"warn: rate-incident classification failed for {agent}/{run_id}: {exc}",
            file=sys.stderr,
        )
        return None
    if not evidence_result.get("is_authoritative"):
        return None
    try:
        rate_incidents.record_incident(
            agent=agent,
            surface="ledger_reconcile.completion",
            category=evidence_result["category"],
            status="recorded",
            target=target,
            run_id=run_id,
            shed=shed,
            evidence=combined_text,
            # The provider names when it will serve again; shed until then, not 6 h. Read from
            # codex's harness events by the one reader every observer of a run uses, so a limit hit
            # after work carries its reset as a refusal does.
            reset_at=rate_incidents.provider_reset_at(agent, lines),
            extra={
                "subcategory": evidence_result["subcategory"],
                "source": "turn_failed_event" if refusal else "log_segment",
                "log_file": str(log_file) if log_file else None,
            },
        )
    except Exception as exc:
        print(f"warn: rate-incident recording failed for {agent}/{run_id}: {exc}", file=sys.stderr)
    return evidence_result


def _log_usage(path: Path | None, run_id: str) -> tuple[int, int]:
    if path is None:
        return 0, 0
    tokens_in = 0
    tokens_out = 0
    for line in _log_segment(path, run_id):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        tin, tout = _usage_from_obj(obj)
        tokens_in += tin
        tokens_out += tout
    return tokens_in, tokens_out


def _log_cost_usd(path: Path | None, run_id: str) -> float:
    """item 16(j) (2026-07-08): real dollars from the run's own log. claude -p JSON results carry
    total_cost_usd (native telemetry, no OTel collector needed at solo scale — deliberate decision
    over standing up an OTLP pipeline for one Mac); other agents may emit cost_usd. Max over the
    segment (agents print a running figure; the last/largest is the total)."""
    if path is None:
        return 0.0
    best = 0.0
    for line in _log_segment(path, run_id):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("total_cost_usd", "cost_usd", "total_cost"):
            value = _number(obj.get(key))
            if value and value > best:
                best = value
    return round(best, 6)


def _latency_s(rows: list[dict[str, Any]]) -> float:
    starts = [int(r.get("ts") or 0) for r in rows if r.get("event") == "start" and r.get("ts")]
    completes = [
        int(r.get("ts") or 0) for r in rows if r.get("event") == "complete" and r.get("ts")
    ]
    if not starts or not completes:
        return 0.0
    return float(max(0, max(completes) - min(starts)))


def _latest_log_file(rows: list[dict[str, Any]]) -> Path | None:
    for row in reversed(rows):
        value = row.get("log_file")
        if isinstance(value, str) and value.strip():
            return Path(value)
    return None


# item 16f: CLI resume identifiers, harvested from each run's log segment. First match wins;
# patterns are a living registry — extend as agents' output formats reveal themselves.
RESUME_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "claude_session",
        re.compile(
            r'"session_id"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
        ),
    ),
    (
        "codex_session",
        re.compile(
            r"session id:?\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            re.IGNORECASE,
        ),
    ),
    ("codex_rollout", re.compile(r'"rollout_path"\s*:\s*"([^"]+\.jsonl)"')),
    ("cursor_chat", re.compile(r'"chat_?[iI]d"\s*:\s*"([A-Za-z0-9_-]{8,})"')),
)


def _resume_token_from_segment(lines: list[str]) -> tuple[str, str] | None:
    for line in lines:
        for kind, pattern in RESUME_PATTERNS:
            m = pattern.search(line)
            if m:
                return kind, m.group(1)
    return None


# item 16h: agents record product-level owner questions as a single structured log line and KEEP
# WORKING with their stated default; reconcile harvests them into feedback.owner_questions where
# defaults auto-ratify at expiry (never a blocking backlog).
_OWNER_QUESTION_RE = re.compile(r"OWNER_QUESTION:\s*(\{.*\})\s*$")


def _owner_questions_from_segment(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        m = _OWNER_QUESTION_RE.search(line)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("question") and obj.get("default"):
            out.append(obj)
    return out


def _done_marker(log_file: Path | None, run_id: str) -> dict[str, Any] | None:
    """Read the shell-native completion marker (adapters.done_marker_cmd) for a run whose python
    completion step never ran — observed SIGKILLed mid-write 522x (2026-07-03 audit F2). The
    marker's {"run_id","rc","ts"} lets reconcile recover latency/exit instead of dropping the
    run's telemetry; its "rc_of" says whose status "rc" is, and a marker without it predates the
    dispatch wrapper reading the agent's (see the signal-death rule in reconcile)."""
    if log_file is None:
        return None
    path = log_file.parent / "done" / f"{run_id}.json"
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) and obj.get("run_id") == run_id else None


def _signal_death_rc(marker: dict[str, Any] | None) -> int | None:
    """The marker's rc when it reads as death by signal (128 + the signal, so 129-255), else None.
    999 is the marker's own sentinel for an rc variable the wrapper never set, not a signal."""
    try:
        rc = int(marker["rc"]) if marker is not None and marker.get("rc") is not None else None
    except (TypeError, ValueError):
        return None
    return rc if rc is not None and 128 < rc < 256 else None


def resolve_unresolved_worker_attempts(*, apply: bool = False, limit: int = 5000) -> dict:
    """Resolve worker attempts that were completed `unresolved` before a reader existed.

    The normal completion path only visits a run ONCE: `reconcile`'s marker branch fires for runs
    with no `complete` event, and after it runs the attempt is `unresolved` forever. So every
    attempt recorded before `adapters.cli_reported_model` existed is stranded with
    `resolved_model_not_reported_by_completion` even though its CLI log is still on disk and still
    says exactly what served it.

    Reads the same single source as the live path and applies the same refusals — a seat whose CLI
    keeps no per-session log stays unresolved with its reason NAMED, and nothing is ever inferred
    from the requested model. Dry-run by default; reports the per-agent breakdown either way, so
    "resolved 37" always arrives next to "16 cannot report and here is why".

    Only TERMINAL attempts are eligible: `status='unresolved'`. An in-flight (`started`)
    attempt is never completed here, and neither is one that never ran (`failed`) — see the
    eligibility comment below for why each exclusion is required and why neither starves the
    drain.
    """
    # TERMINAL ROWS ONLY -- `status='unresolved'` is the whole eligibility rule, and it is doing two
    # jobs. A worker attempt's profile row is written `started` BEFORE the subprocess is spawned
    # (`dispatcher`/`exp_abcd` pre-dispatch), so an IN-FLIGHT attempt matches every other clause
    # here: role worker, profile set, resolved_model still NULL. `cli_reported_model` reads the
    # FIRST model in the session log within a 2h window of `started_ts`, and that log exists as soon
    # as the CLI starts -- so a run still executing probes clean, and `--apply` stamped it
    # `complete` with a resolved model and a `completed_ts` of the sweep's own clock. That is the
    # one row shape allowed to support an exact-model claim, manufactured for a worker that had not
    # finished and could still fall back, retry onto another model, or fail outright.
    # It also excludes `failed` (dispatcher's `profile_process_start_failed`), which is terminal but
    # never ran: there is no served model to recover, so resolving it from a neighbouring session in
    # the window would be pure invention.
    # Not a starved drain (the trap this repo keeps falling into): a `started` row is excluded only
    # while it is in flight. Its own completion closes it to `complete` (resolved -- no sweep
    # needed) or `unresolved` (eligible on the next pass), so the exclusion clears itself without
    # the sweep's help. Measured on the live ledger when this filter landed: 56 candidates before,
    # 56 after -- every genuinely drainable row is already `unresolved`.
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT ea.run_id, ea.profile_id, r.agent, r.target, r.ts "
            "FROM execution_attempts ea JOIN runs r ON r.run_id=ea.run_id "
            "WHERE ea.operation_role='worker' AND ea.status='unresolved' "
            "AND ea.resolved_model IS NULL "
            "AND ea.profile_id IS NOT NULL ORDER BY r.ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        # Counted, not silently narrowed. `candidates: 0` beside `not_terminal: {started: 3}` reads
        # as "wait for those runs to finish"; `candidates: 0` alone reads as "the sweep is broken".
        not_terminal = {
            str(status or "<null>"): int(count)
            for status, count in c.execute(
                "SELECT ea.status, COUNT(*) "
                "FROM execution_attempts ea JOIN runs r ON r.run_id=ea.run_id "
                "WHERE ea.operation_role='worker' AND ea.resolved_model IS NULL "
                "AND ea.profile_id IS NOT NULL "
                "AND (ea.status IS NULL OR ea.status<>'unresolved') GROUP BY ea.status"
            ).fetchall()
        }
    resolved: dict[str, int] = {}
    blocked: dict[str, int] = {}
    failed: dict[str, str] = {}
    # STRUCTURALLY UNRESOLVABLE ROWS ARE NOT A BACKLOG. As first written this swept every unresolved
    # worker attempt, and 23 of 23 candidates belonged to seats whose CLI keeps no per-session log:
    # a candidate set that grows with every offload and can never drain, re-probing the filesystem
    # for each one on every run. The gate's own drain was the capability the seat lacks. Excluding
    # them is what makes the remaining count mean "not yet resolved" instead of "never will be",
    # and it is reported separately below rather than silently dropped.
    unreportable: dict[str, int] = {}
    live_rows = []
    for row in rows:
        capable, reason = adapters.can_report_cli_identity(str(row[2] or ""))
        if capable:
            live_rows.append(row)
        else:
            key = f"{str(row[2] or '').lower()}:{str(reason or 'unknown').split(':')[0]}"
            unreportable[key] = unreportable.get(key, 0) + 1
    rows = live_rows
    for run_id, profile_id, agent, target, ts in rows:
        agent = str(agent or "").lower()
        workspace = (
            str(target)[len("offload:") :] if str(target or "").startswith("offload:/") else None
        )
        probe = adapters.cli_reported_model(agent, workspace, started_ts=int(ts or 0))
        if not probe.get("model"):
            key = f"{agent}:{str(probe.get('reason') or 'unknown').split(':')[0]}"
            blocked[key] = blocked.get(key, 0) + 1
            continue
        if not apply:
            resolved[agent] = resolved.get(agent, 0) + 1
            continue
        try:
            feedback.complete_profile_attempt(
                run_id,
                selected_profile_id=str(profile_id),
                resolved_provider=_provider_for_profile(str(profile_id)),
                resolved_model=probe["model"],
            )
        except Exception as exc:  # noqa: BLE001 - counted, never fatal to the sweep
            failed[run_id] = f"{type(exc).__name__}: {exc}"
            continue
        resolved[agent] = resolved.get(agent, 0) + 1
    # A ROW CANNOT BE BOTH RESOLVED AND FALLEN BACK. `complete_profile_attempt` did not clear
    # `fallback_reason`, so an attempt closed unresolved and later resolved kept the stale string --
    # and `resolved_model_coverage` counts `fallback_reason IS NOT NULL`, so codex-5.6-terra-high
    # reported coverage 1.00 AND fallback_rate 1.00 at the same time. A metric that contradicts
    # itself is not a small blemish here: fallback_rate is how a profile's health is read.
    contradictions = 0
    with feedback._conn() as c:
        stale = c.execute(
            "SELECT COUNT(*) FROM execution_attempts "
            "WHERE resolved_model IS NOT NULL AND fallback_reason IS NOT NULL"
        ).fetchone()[0]
        if stale and apply:
            c.execute(
                "UPDATE execution_attempts SET fallback_reason=NULL "
                "WHERE resolved_model IS NOT NULL AND fallback_reason IS NOT NULL"
            )
        contradictions = int(stale or 0)
    return {
        "applied": apply,
        # DRAINABLE candidates only -- rows a reader could still resolve. This number can reach 0.
        "candidates": len(rows),
        "resolved_rows_with_stale_fallback": contradictions,
        "resolved_by_agent": resolved,
        "blocked_by_reason": blocked,
        # Reported beside it, never inside it: rows excluded because the seat can never report.
        # Naming them keeps the exclusion auditable, and keeps `candidates` an honest backlog.
        "excluded_unreportable": unreportable,
        # Rows excluded as not-terminal, keyed by the status that excluded them. `started` clears
        # itself when the run completes; `failed` never ran and is permanently and correctly out.
        "excluded_not_terminal": not_terminal,
        "failed": failed,
    }


def _profile_id_from_attempt(run_id: str) -> str | None:
    """The profile this run's own worker attempt recorded, when the LEDGER did not carry it.

    The capacity ledger's `start` row has no `selected_profile_id` field at all — its keys are
    (agent, cost_usd, count, event, log_file, mode, model, run_id, started_ts, target, task_type,
    ts). So `profile_ids` below was ALWAYS empty and the resolution branch was structurally dead:
    250 marker backfills, 0 resolved, 0 unresolved, every time. Another gate whose drain could
    never run.

    `execution_attempts` already knows, because `offload` wrote the profile onto the attempt row it
    created. Reading it there needs no dispatcher change AND works retroactively on attempts already
    on record, which a new ledger field could never do.
    """
    with feedback._conn() as c:
        row = c.execute(
            "SELECT profile_id FROM execution_attempts WHERE run_id=? AND operation_role='worker' "
            "AND profile_id IS NOT NULL AND resolved_model IS NULL "
            "ORDER BY attempt_ordinal DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _provider_for_profile(profile_id: str) -> str:
    """The profile's own provider. Read from the immutable registry, never guessed from the model
    string -- `complete_profile_attempt` refuses a resolved model with no provider, and inventing
    one would put a fabricated field beside a real one."""
    return str(execution_profiles.get_profile(profile_id)["provider"])


def _workspace_from_rows(run_rows) -> str | None:
    """The run's own workspace, recovered from the `offload:<path>` target it already records.

    `dispatcher.offload` has always written `target = f"offload:{run_cwd}"`, so the join key to the
    agent's session log was in the database the whole time -- 1,614 offload runs carry it. Nothing
    read it, which is why `resolved_model` had no writer outside the quarantined trial bridge.
    """
    for row in run_rows:
        value = str(row.get("target") or "")
        if value.startswith("offload:/"):
            return value[len("offload:") :]
    return None


def _cli_identity_for_run(run_rows, started_ts: int | None = None, log_file=None) -> dict:
    """CLI-reported identity for a run, or a NAMED reason there is none.

    Never guesses. A seat whose CLI leaves no per-session log returns the reason and the attempt
    stays unresolved -- `fallback_reason` then says which seat and why, instead of the single
    undifferentiated `resolved_model_not_reported_by_completion` that every seat used to get.

    `log_file` is the run's own dispatch log, which is what lets the gemini seat read the model from
    THIS run's agy log instead of matching a conversation store by workspace and window.
    """
    agent = next((str(row.get("agent")) for row in run_rows if row.get("agent")), "")
    if not agent:
        return {"model": None, "cli_version": None, "source": None, "reason": "no_agent_on_run"}
    return adapters.cli_reported_model(
        agent, _workspace_from_rows(run_rows), started_ts=started_ts, log_file=log_file
    )


def _scrub_run_segment(log_file: str | None, run_id: str, started_ts: int | None) -> int:
    """Mask credentials in THIS run's own segment of its log, in place, and say so in the log.

    A detached wrapper sends the agent's stdout straight into the file, so no Python reads it
    before it lands; this step runs right after the agent exits, the first moment the segment can
    be masked. Two experiment arms of 2026-07-09 printed the live GitHub token into exactly such a
    log. The scrub runs from this run's header to the end of the file
    (credential_redaction.segment_span), never over what earlier runs wrote, and a run started
    before CREDENTIAL_SCRUB_SINCE (or with no start at all) is never scrubbed: files that already
    held a token are the owner's to clean, not this step's. Returns how many strings were masked;
    never fatal to the completion step."""
    if not log_file:
        return 0
    if started_ts is None or started_ts < CREDENTIAL_SCRUB_SINCE:
        return 0
    result = credential_redaction.scrub_file(log_file, run_id=run_id)
    if result["status"] == "disabled":
        print(
            f"credential redaction DISABLED by {credential_redaction.REDACTION_DISABLED_ENV}=1: "
            "this run's log segment is unmasked",
            file=sys.stderr,
        )
    elif result["status"] in ("error", "too_large"):
        print(
            f"warn: this run's log segment was not scrubbed ({result['status']}"
            f"{': ' + result['error'] if result.get('error') else ''})",
            file=sys.stderr,
        )
    elif result["redacted"]:
        print(
            "credentials masked in this run's log segment: "
            + credential_redaction.describe(result["kinds"]),
            file=sys.stderr,
        )
    return int(result.get("redacted") or 0)


def record_completion(
    run_id: str,
    agent: str,
    target: str | None = None,
    mode: str | None = None,
    task_type: str | None = None,
    log_file: str | None = None,
    started_ts: int | None = None,
    selected_profile_id: str | None = None,
    requested_model: str | None = None,
    resolved_provider: str | None = None,
    resolved_model: str | None = None,
    policy_version: str | None = None,
    propensity: float | None = None,
    subject_id: str | None = None,
    arm_id: str | None = None,
    exit_code: int | None = None,
    workspace: str | None = None,
) -> None:
    # FIRST, before anything below reads the log: the incident classifier, the reconcile pass and
    # anyone who opens the file all see the masked segment.
    credentials_redacted = _scrub_run_segment(log_file, run_id, started_ts)
    if selected_profile_id:
        probe_reason = None
        if not resolved_model and str(target or "").startswith("offload:/"):
            # Last chance to record real provenance before the attempt completes unresolved
            # forever: ask the agent's own CLI log what served this run. Only fills a field the
            # caller left EMPTY -- an explicitly supplied resolved_model always wins, because the
            # caller may have provenance this reader cannot see.
            probed = adapters.cli_reported_model(
                agent, str(target)[len("offload:") :], started_ts=started_ts, log_file=log_file
            )
            probe_reason = probed.get("reason")
            if probed.get("model"):
                resolved_model = probed["model"]
                resolved_provider = resolved_provider or _provider_for_profile(selected_profile_id)
        if resolved_model:
            if not resolved_provider:
                raise ValueError("resolved_model completion evidence requires resolved_provider")
            feedback.complete_profile_attempt(
                run_id,
                selected_profile_id=selected_profile_id,
                resolved_provider=resolved_provider,
                resolved_model=resolved_model,
                completed_ts=int(time.time()),
            )
        else:
            feedback.complete_profile_attempt_unresolved(
                run_id,
                selected_profile_id=selected_profile_id,
                # NAME THE SEAT. `resolved_model_not_reported_by_completion` was identical for a
                # seat whose CLI cannot report and a seat whose log simply was not found -- one is
                # permanent and one is a bug, and they read the same. The suffix separates them.
                fallback_reason=(
                    f"resolved_model_not_reported_by_completion:{probe_reason}"
                    if probe_reason
                    else "resolved_model_not_reported_by_completion"
                )[:200],
                completed_ts=int(time.time()),
            )
    # Hook: Classify log file for rate-limit/capacity incidents on completion
    if log_file:
        log_path = Path(log_file)
        if log_path.exists():
            seg = _log_segment(log_path, run_id)
            _classify_run_log_segment(
                seg,
                agent,
                run_id,
                target,
                log_path,
                successful=(exit_code == 0 if exit_code is not None else None),
            )
    adapters.record_ledger(
        agent,
        count=0,
        cost_usd=0.0,
        event="complete",
        run_id=run_id,
        target=target,
        mode=mode,
        task_type=task_type,
        log_file=log_file,
        started_ts=started_ts,
        selected_profile_id=selected_profile_id,
        requested_model=requested_model,
        resolved_provider=resolved_provider,
        resolved_model=resolved_model,
        policy_version=policy_version,
        propensity=propensity,
        causal_context={
            key: value
            for key, value in {"subject_id": subject_id, "arm_id": arm_id}.items()
            if value
        }
        or None,
        credentials_redacted=credentials_redacted or None,
    )
    try:
        feedback.record_completion_event(
            run_id,
            event_type="completion",
            phase="execution",
            producer="ledger_reconcile",
            status="succeeded",
            payload={
                "workflow_ids": ["local-execution-ledger"],
                "result": {"status": "complete"},
            },
        )
    except Exception:
        # Capacity accounting must survive a best-effort evidence sink failure.
        pass
    # The branches this run pushed from its own worktree, read NOW because the next `fetch --prune`
    # deletes a merged branch's remote-tracking reflog (pushed_branches.py). Last and best-effort:
    # the accounting above never waits on it, and a failure is reported in the run's own log.
    if workspace:
        try:
            pushed_branches.record_at_completion(run_id, workspace, started_ts)
        except Exception as exc:  # noqa: BLE001 - never fatal to the completion step
            print(f"warn: push record not written for {run_id}: {exc}", file=sys.stderr)


def reconcile(
    ledger: Path | None = None,
    *,
    dry_run: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    path = _ledger_path(ledger)
    rows, errors = _read_ledger(path)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    for row in rows:
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            skipped["missing_run_id"] += 1
            continue
        grouped[run_id].append(row)

    known = _known_runs()
    sources = _cost_sources()
    written = 0
    marker_backfills = 0
    profile_unresolved_backfills = 0
    profile_resolved_backfills = 0
    infra_classified = 0
    infra_unattributed_marker_rc = 0
    resume_tokens_captured = 0
    owner_questions_recorded = 0
    log_costs_harvested = 0
    telemetry_runs_backfilled = 0
    rate_incident_classified = 0
    # Every key present from the start, so a pass with nothing to classify prints zeros: an absent
    # key would read as "not measured", which is the opposite of good news.
    provider_limit_deaths = {"seen": 0, **{bucket: 0 for bucket in PROVIDER_LIMIT_BUCKETS}}
    prepared: list[dict[str, Any]] = []

    for run_id, run_rows in sorted(grouped.items()):
        if run_id not in known:
            telemetry_rows = [row for row in run_rows if row.get("event") == "telemetry_error"]
            if telemetry_rows and not dry_run:
                telemetry = telemetry_rows[-1]
                try:
                    feedback.record_run(
                        run_id,
                        telemetry.get("target") or "unknown-target",
                        telemetry.get("task_type") or "delegated",
                        telemetry.get("agent") or "unknown",
                        mode=telemetry.get("mode") or "local",
                        reasoning_level=telemetry.get("reasoning_level"),
                        model=telemetry.get("model"),
                        rationale="ledger telemetry_error decision backfill",
                    )
                    known.add(run_id)
                    telemetry_runs_backfilled += 1
                except Exception:
                    skipped["telemetry_error_backfill_failed"] += 1
                    continue
            elif telemetry_rows:
                skipped["telemetry_error_backfill_ready"] += 1
                continue
            else:
                skipped["unknown_run_id"] += 1
                continue
        log_file = _latest_log_file(run_rows)
        # Before the cost-source skip below, on purpose: whether a run did any work has nothing to
        # do with which cost row it got, and that skip ends the run's whole pass.
        if log_file is not None:
            refusal = provider_limit_before_work(_log_segment(log_file, run_id))
            if refusal is not None:
                provider_limit_deaths["seen"] += 1
                bucket = _settle_provider_limit_death(run_id, refusal, dry_run=dry_run)
                provider_limit_deaths[bucket] += 1
        existing_source = sources.get(run_id)
        if existing_source in {"langsmith", "ccusage"}:
            skipped[f"{existing_source}_cost_exists"] += 1
            continue
        # 16f/16h harvest from the run's log segment (same segment _log_usage scans).
        if log_file is not None and not dry_run:
            seg = _log_segment(log_file, run_id)
            agent = next((str(r.get("agent")) for r in run_rows if r.get("agent")), "")
            target = next((str(r.get("target")) for r in run_rows if r.get("target")), "")
            completion_exit = next(
                (r.get("exit") for r in reversed(run_rows) if r.get("event") == "complete"), None
            )
            try:
                successful = int(completion_exit) == 0 if completion_exit is not None else None
            except (TypeError, ValueError):
                successful = None
            # Hook: Classify log segment for rate-limit/capacity incidents
            rate_evidence = _classify_run_log_segment(
                seg, agent, run_id, target, log_file, shed=False, successful=successful
            )
            if rate_evidence:
                rate_incident_classified += 1
            tok = _resume_token_from_segment(seg)
            if tok is not None:
                kind, token = tok
                try:
                    feedback.record_resume_token(
                        run_id, agent, kind, token, cwd=str(log_file.parent)
                    )
                    resume_tokens_captured += 1
                except Exception:
                    pass
            for q in _owner_questions_from_segment(seg):
                try:
                    res = feedback.record_owner_question(
                        q.get("question"),
                        q.get("default"),
                        run_id=run_id,
                        target=target or None,
                        repo=(target.split("#")[0].split(" ")[0] if "/" in target else None),
                        options=q.get("options"),
                        expires_days=float(q.get("expires_days") or 7),
                    )
                    if not res.get("deduped"):
                        owner_questions_recorded += 1
                except Exception:
                    pass
        tokens_in, tokens_out = _log_usage(log_file, run_id)
        cost_usd = sum(float(_number(r.get("cost_usd")) or 0.0) for r in run_rows)
        log_cost = _log_cost_usd(log_file, run_id)
        if log_cost > cost_usd:  # 16(j): the agent's own reported dollars beat ledger zeros
            cost_usd = log_cost
            log_costs_harvested += 1
        latency_s = _latency_s(run_rows)
        marker = _done_marker(log_file, run_id)
        marker_rc = marker.get("rc") if marker is not None else None
        if marker is not None and not any(r.get("event") == "complete" for r in run_rows):
            # No ndjson complete event — the python completion step was likely killed (audit F2).
            # Fall back to the shell-native done marker for latency/exit before giving up.
            starts = [
                int(r.get("ts") or 0) for r in run_rows if r.get("event") == "start" and r.get("ts")
            ]
            marker_ts = int(_number(marker.get("ts")) or 0)
            if starts and marker_ts:
                latency_s = float(max(0, marker_ts - min(starts)))
            marker_backfills += 1
            profile_ids = {
                str(row.get("selected_profile_id"))
                for row in run_rows
                if row.get("event") == "start" and row.get("selected_profile_id")
            }
            if len(profile_ids) > 1:
                raise ValueError(f"run {run_id} changed selected profile in capacity ledger")
            if not profile_ids:
                # Fall back to what the attempt itself recorded; see _profile_id_from_attempt.
                from_attempt = _profile_id_from_attempt(run_id)
                if from_attempt:
                    profile_ids = {from_attempt}
            if profile_ids and not dry_run:
                # RESOLVE IF THE CLI SAID SO, otherwise stay unresolved with the seat NAMED.
                # This branch used to complete every seat unresolved unconditionally, so
                # `execution_attempts.resolved_model` had no writer at all and
                # `unresolved_model_provenance` was unavoidable on every event in the system.
                started = min(
                    (
                        int(r.get("ts") or 0)
                        for r in run_rows
                        if r.get("event") == "start" and r.get("ts")
                    ),
                    default=None,
                )
                identity = _cli_identity_for_run(run_rows, started_ts=started, log_file=log_file)
                selected = next(iter(profile_ids))
                if identity.get("model"):
                    feedback.complete_profile_attempt(
                        run_id,
                        selected_profile_id=selected,
                        resolved_provider=_provider_for_profile(selected),
                        resolved_model=identity["model"],
                        completed_ts=marker_ts or int(time.time()),
                    )
                    profile_resolved_backfills += 1
                else:
                    feedback.complete_profile_attempt_unresolved(
                        run_id,
                        selected_profile_id=selected,
                        fallback_reason=(
                            f"resolved_model_not_reported_marker_backfill:"
                            f"{identity.get('reason') or 'unknown'}"
                        )[:200],
                        completed_ts=marker_ts or int(time.time()),
                    )
                    profile_unresolved_backfills += 1
        # item 9 two-tier enum: rc>128 means the AGENT process died by SIGNAL — its non-merged
        # outcome is infrastructure noise, not capability evidence; classify so learners skip it.
        # Eventual-consistent: if the outcome row doesn't exist yet, a later daily pass catches it.
        # Only a marker that says its rc is the agent's may say so. A dispatch marker written
        # before 2026-10-04 holds the claims release's status under the same key: all 31 live
        # rc=137 markers were the release SIGKILLed after its agent had finished, and the six rows
        # they classified were every row this rule had ever classified. Such a marker is counted,
        # never classified: unknown is not a signal death.
        signal_rc = _signal_death_rc(marker)
        if signal_rc is not None and (marker or {}).get("rc_of") != adapters.MARKER_RC_OF_AGENT:
            infra_unattributed_marker_rc += 1
        elif signal_rc is not None and not dry_run:
            if feedback.mark_transient_infra(run_id, reason=f"marker rc={signal_rc}"):
                infra_classified += 1
        if not (tokens_in or tokens_out or cost_usd or latency_s):
            skipped["no_measurement"] += 1
            continue
        record = {
            "run_id": run_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost_usd, 6),
            "latency_s": latency_s,
            "source": "ledger",
            "marker_rc": marker_rc,
            "log_file": str(log_file) if log_file else None,
        }
        prepared.append(record)
        if not dry_run:
            feedback.record_cost(
                run_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                latency_s=latency_s,
                source="ledger",
            )
            written += 1

    return {
        "dry_run": dry_run,
        "ledger": str(path),
        "rows_read": len(rows),
        "run_ids_seen": len(grouped),
        "prepared": len(prepared),
        "written_cost_rows": written,
        "marker_backfills": marker_backfills,
        "profile_unresolved_backfills": profile_unresolved_backfills,
        # Reported BESIDE the unresolved count, never instead of it: `resolved 2 /
        # unresolved 1414` is a coverage statement, while either number alone is not.
        "profile_resolved_backfills": profile_resolved_backfills,
        "infra_classified": infra_classified,
        # BESIDE it, never instead: markers whose rc reads as a signal death but is not the agent's
        # (written before the wrapper read the agent's status), so the rule above cannot use them.
        "infra_unattributed_marker_rc": infra_unattributed_marker_rc,
        # Runs whose own log shows the provider refusing them before any work, partitioned by why
        # each was or was not classified transient_infra (PROVIDER_LIMIT_BUCKETS).
        "provider_limit_deaths": provider_limit_deaths,
        "resume_tokens_captured": resume_tokens_captured,
        "owner_questions_recorded": owner_questions_recorded,
        "log_costs_harvested": log_costs_harvested,
        "telemetry_runs_backfilled": telemetry_runs_backfilled,
        "rate_incident_classified": rate_incident_classified,
        "owner_questions_expired": (0 if dry_run else feedback.expire_owner_questions()),
        "costs": prepared,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors,
        "strict_failed": strict and bool(errors),
    }


def _print_summary(summary: dict[str, Any], *, as_json: bool):
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    action = "would write" if summary["dry_run"] else "wrote"
    print(
        f"ledger_reconcile: read {summary['rows_read']} row(s), saw "
        f"{summary['run_ids_seen']} run_id(s), {action} {summary['written_cost_rows']} cost row(s)"
    )
    deaths = summary["provider_limit_deaths"]
    print(
        "provider refusals before any work: "
        + ", ".join(f"{key} {deaths[key]}" for key in ("seen", *PROVIDER_LIMIT_BUCKETS))
    )
    print(
        f"agent signal deaths: classified {summary['infra_classified']}, marker rc not the "
        f"agent's {summary['infra_unattributed_marker_rc']}"
    )
    if summary["skipped"]:
        print(f"skipped: {json.dumps(summary['skipped'], sort_keys=True)}")
    if summary["errors"]:
        print("errors:")
        for error in summary["errors"]:
            print(f"- {error}")


def _selftest():
    tmp = Path(tempfile.mkdtemp(prefix="ledger-reconcile-selftest-"))
    old_db = feedback.DB_PATH
    old_handoff, old_ledger = adapters.HANDOFF, adapters.LEDGER
    incident_paths = ("HANDOFF", "INCIDENT_FILE", "LOCK_FILE", "SHED_DIR")
    old_incident_paths = {name: getattr(rate_incidents, name) for name in incident_paths}
    try:
        feedback.DB_PATH = tmp / "orchestrator.db"
        adapters.HANDOFF = tmp
        adapters.LEDGER = tmp / "capacity-ledger.ndjson"
        # A refusal records an incident and may shed the seat: never in the real handoff dir.
        rate_incidents.HANDOFF = tmp
        rate_incidents.INCIDENT_FILE = tmp / "rate-limit-incidents.ndjson"
        rate_incidents.LOCK_FILE = tmp / "rate-limit-incidents.ndjson.lock"
        rate_incidents.SHED_DIR = tmp / "capacity-shed"
        feedback.record_run("local-1", "stranske/Repo#1", "implement", "codex", mode="local")
        feedback.record_run("remote-1", "stranske/Repo#2", "implement", "codex", mode="remote")
        feedback.record_run("ccusage-1", "stranske/Repo#3", "implement", "codex", mode="local")
        feedback.record_cost("remote-1", tokens_in=10, tokens_out=5, source="langsmith")
        feedback.record_cost("ccusage-1", tokens_in=20, tokens_out=10, source="ccusage")

        log = tmp / "local.log"
        log.write_text(
            "=== 2026-06-16T00:00:00Z dispatch codex/full -> stranske/Repo#1 "
            "[implement] cwd=/tmp run_id=local-1 ===\n"
            + json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"prompt_tokens": 200, "completion_tokens": 100},
                }
            )
            + "\n"
        )
        adapters.record_ledger(
            "codex",
            count=1,
            event="start",
            run_id="local-1",
            target="stranske/Repo#1",
            log_file=str(log),
            ts=100,
        )
        record_completion(
            "local-1", "codex", "stranske/Repo#1", "full", "implement", str(log), started_ts=100
        )
        adapters.record_ledger(
            "codex",
            count=1,
            event="start",
            run_id="remote-1",
            target="stranske/Repo#2",
            log_file=str(log),
            ts=100,
        )
        adapters.record_ledger(
            "codex",
            count=1,
            event="start",
            run_id="ccusage-1",
            target="stranske/Repo#3",
            log_file=str(log),
            ts=100,
        )
        adapters.record_ledger(
            "codex",
            count=1,
            event="start",
            run_id="unknown-1",
            target="stranske/Repo#3",
            log_file=str(log),
            ts=100,
        )

        dry = reconcile(adapters.LEDGER, dry_run=True)
        assert dry["prepared"] == 1 and dry["written_cost_rows"] == 0, dry
        cost = dry["costs"][0]
        assert (
            cost["run_id"] == "local-1" and cost["tokens_in"] == 300 and cost["tokens_out"] == 150
        ), cost
        assert (
            dry["skipped"]["langsmith_cost_exists"] == 1
            and dry["skipped"]["ccusage_cost_exists"] == 1
        ), dry
        assert dry["skipped"]["unknown_run_id"] == 1, dry

        # F2 (2026-07-03 audit): a run whose python completion was SIGKILLed leaves NO complete
        # event — the shell-native done marker (adapters.done_marker_cmd) must still let reconcile
        # recover latency/exit instead of dropping the run's telemetry.
        feedback.record_run("killed-1", "stranske/Repo#4", "implement", "codex", mode="local")
        klog = tmp / "killed.log"
        klog.write_text(
            "=== 2026-06-16T00:00:00Z dispatch codex/full -> stranske/Repo#4 "
            "[implement] cwd=/tmp run_id=killed-1 ===\n"
        )
        adapters.record_ledger(
            "codex",
            count=1,
            event="start",
            run_id="killed-1",
            target="stranske/Repo#4",
            log_file=str(klog),
            ts=100,
        )
        done_dir = klog.parent / "done"
        done_dir.mkdir(exist_ok=True)
        (done_dir / "killed-1.json").write_text(
            json.dumps({"run_id": "killed-1", "rc": 137, "rc_of": "agent", "ts": 160})
        )
        # The same rc in a marker that does not say whose it is: the dispatch wrapper wrote these
        # until 2026-10-04 with its claim release's status, so it is never read as the agent's.
        feedback.record_run("released-1", "stranske/Repo#5", "implement", "cursor", mode="local")
        adapters.record_ledger(
            "cursor",
            count=1,
            event="start",
            run_id="released-1",
            target="stranske/Repo#5",
            log_file=str(klog),
            ts=100,
        )
        (done_dir / "released-1.json").write_text(
            json.dumps({"run_id": "released-1", "rc": 137, "ts": 170})
        )

        # item 9: the killed run's non-merged outcome must get classified transient_infra from
        # the marker's rc=137 during the real (non-dry) reconcile below.
        feedback.record_outcome(
            "killed-1", adjudicated_verdict="FAIL", merged=False, durability="abandoned"
        )
        feedback.record_outcome(
            "released-1", adjudicated_verdict="FAIL", merged=False, durability="abandoned"
        )

        summary = reconcile(adapters.LEDGER)
        assert summary["written_cost_rows"] == 3, summary
        assert summary["marker_backfills"] == 2, summary
        assert summary["infra_classified"] == 1, summary
        assert summary["infra_unattributed_marker_rc"] == 1, summary
        with feedback._conn() as c:
            fc = c.execute(
                "SELECT failure_class, notes FROM outcomes WHERE run_id='killed-1'"
            ).fetchone()
            unattributed = c.execute(
                "SELECT failure_class FROM outcomes WHERE run_id='released-1'"
            ).fetchone()
        assert fc and fc[0] == "transient_infra" and "marker rc=137" in (fc[1] or ""), fc
        assert unattributed == (None,), ("a release's rc classified the agent", unattributed)
        with feedback._conn() as c:
            row = c.execute(
                "SELECT tokens_in, tokens_out, source FROM costs WHERE run_id='local-1'"
            ).fetchone()
            remote = c.execute("SELECT source FROM costs WHERE run_id='remote-1'").fetchone()
            ccusage = c.execute("SELECT source FROM costs WHERE run_id='ccusage-1'").fetchone()
            killed = c.execute(
                "SELECT latency_s, source FROM costs WHERE run_id='killed-1'"
            ).fetchone()
        assert row == (300, 150, "ledger"), row
        assert remote == ("langsmith",), remote
        assert ccusage == ("ccusage",), ccusage
        assert killed == (60.0, "ledger"), killed
        killed_prepared = [r for r in summary["costs"] if r["run_id"] == "killed-1"]
        assert killed_prepared and killed_prepared[0]["marker_rc"] == 137, killed_prepared

        # 16f/16h harvest: the run's log segment yields a resume token and an owner question;
        # the question's default auto-ratifies once expired.
        feedback.record_run("harvest-1", "stranske/Repo#5", "implement", "claude", mode="local")
        hlog = tmp / "harvest.log"
        hlog.write_text(
            "=== 2026-06-16T00:00:00Z dispatch claude/full -> stranske/Repo#5 "
            "[implement] cwd=/tmp run_id=harvest-1 ===\n"
            + json.dumps({"type": "system", "session_id": "0a1b2c3d-1111-2222-3333-444455556666"})
            + "\n"
            + 'OWNER_QUESTION: {"question": "Rename the CLI flag?", "default": "keep old name", "expires_days": -1}\n'
            + json.dumps(
                {
                    "type": "result",
                    "total_cost_usd": 0.4321,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            )
            + "\n"
        )
        adapters.record_ledger(
            "claude",
            count=1,
            event="start",
            run_id="harvest-1",
            target="stranske/Repo#5",
            log_file=str(hlog),
            ts=100,
        )
        record_completion(
            "harvest-1", "claude", "stranske/Repo#5", "full", "implement", str(hlog), started_ts=100
        )
        hsum = reconcile(adapters.LEDGER)
        assert hsum["resume_tokens_captured"] == 1, hsum
        assert hsum["owner_questions_recorded"] == 1, hsum
        assert hsum["owner_questions_expired"] == 1, hsum  # expires_days=-1 -> ratified same pass
        hint = feedback.resume_hint("harvest-1")
        assert hint and hint["kind"] == "claude_session" and "0a1b2c3d" in hint["command"], hint
        # 16(j): the agent's own reported dollars land in the cost row (ledger rows were $0)
        assert hsum["log_costs_harvested"] == 1, hsum
        with feedback._conn() as c:
            hcost = c.execute("SELECT cost_usd FROM costs WHERE run_id='harvest-1'").fetchone()
        assert hcost and abs(hcost[0] - 0.4321) < 1e-9, hcost
        decisions = feedback.owner_decisions_for(repo="stranske/Repo")
        assert any(
            d["decision"] == "keep old name" and d["source"] == "default_ratified"
            for d in decisions
        ), decisions

        # A run the provider refused before any work (codex exits 0, so no rc>128 marker): its
        # FAIL leaves the learners, a merged PASS is left as recorded, and both are counted.
        refusal = "You've hit your usage limit. Try again at Jan 5th, 2099 3:11 AM."
        for run_id, merged in (("refused-1", False), ("refused-2", True)):
            feedback.record_run(run_id, "stranske/Repo#6", "testgen", "codex", mode="local")
            feedback.record_outcome(
                run_id,
                adjudicated_verdict="PASS" if merged else "FAIL",
                merged=merged,
                durability="durable" if merged else "abandoned",
            )
            rlog = tmp / f"{run_id}.log"
            rlog.write_text(
                f"=== 2026-10-05T00:00:00Z dispatch codex/full -> stranske/Repo#6 [testgen] "
                f"cwd=/tmp run_id={run_id} ===\n"
                + json.dumps({"type": "turn.started"})
                + "\n"
                + json.dumps({"type": "turn.failed", "error": {"message": refusal}})
                + "\n"
            )
            adapters.record_ledger(
                "codex", count=1, event="start", run_id=run_id, log_file=str(rlog), ts=100
            )
        rsum = reconcile(adapters.LEDGER)
        assert rsum["provider_limit_deaths"] == {
            "seen": 2,
            "classified": 1,
            "excluded": 0,
            "merged": 1,
            "predates_rule": 0,
            "no_outcome_row": 0,
        }, rsum["provider_limit_deaths"]
        with feedback._conn() as c:
            refused = dict(
                c.execute(
                    "SELECT run_id, COALESCE(failure_class,'') FROM outcomes "
                    "WHERE run_id LIKE 'refused-%'"
                ).fetchall()
            )
        assert refused == {"refused-1": "transient_infra", "refused-2": ""}, refused
        assert rate_incidents.INCIDENT_FILE.exists(), "the refusal must record an incident"
        print(
            "ledger_reconcile.py selftest: OK (completion rows, log usage parse, "
            "known-run guard, richer-source-preserving cost write, dry-run, "
            "done-marker backfill for killed completions, provider refusal before any work)"
        )
    finally:
        feedback.DB_PATH = old_db
        adapters.HANDOFF = old_handoff
        adapters.LEDGER = old_ledger
        for name, value in old_incident_paths.items():
            setattr(rate_incidents, name, value)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    complete = sub.add_parser(
        "complete", help="append a completion row to the local capacity ledger"
    )
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--agent", required=True)
    complete.add_argument("--target")
    complete.add_argument("--mode")
    complete.add_argument("--task-type")
    complete.add_argument("--log-file")
    complete.add_argument("--started-ts", type=int)
    complete.add_argument("--selected-profile-id")
    complete.add_argument("--requested-model")
    complete.add_argument("--resolved-provider")
    complete.add_argument("--resolved-model")
    complete.add_argument("--policy-version")
    complete.add_argument("--propensity", type=float)
    complete.add_argument("--subject-id")
    complete.add_argument("--arm-id")
    complete.add_argument("--exit-code", type=int)
    complete.add_argument("--workspace", help="the run's worktree; its pushes are recorded")

    rec = sub.add_parser(
        "reconcile", help="write feedback.costs rows from local ledger/log evidence"
    )
    rec.add_argument("--ledger", type=Path)
    rec.add_argument("--dry-run", action="store_true")
    rec.add_argument("--strict", action="store_true")
    rec.add_argument("--json", action="store_true")
    res = sub.add_parser(
        "resolve-unresolved",
        help="resolve worker attempts stranded unresolved before a CLI reader existed",
    )
    res.add_argument("--apply", action="store_true", help="write (default: dry run)")
    res.add_argument("--limit", type=int, default=5000)

    args = parser.parse_args(argv)
    if args.cmd == "resolve-unresolved":
        print(
            json.dumps(
                resolve_unresolved_worker_attempts(apply=args.apply, limit=args.limit),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.cmd == "complete":
        record_completion(
            args.run_id,
            args.agent,
            target=args.target,
            mode=args.mode,
            task_type=args.task_type,
            log_file=args.log_file,
            started_ts=args.started_ts,
            selected_profile_id=args.selected_profile_id,
            requested_model=args.requested_model,
            resolved_provider=args.resolved_provider,
            resolved_model=args.resolved_model,
            policy_version=args.policy_version,
            propensity=args.propensity,
            subject_id=args.subject_id,
            arm_id=args.arm_id,
            exit_code=args.exit_code,
            workspace=args.workspace,
        )
        return 0
    if args.cmd == "reconcile":
        summary = reconcile(args.ledger, dry_run=args.dry_run, strict=args.strict)
        _print_summary(summary, as_json=args.json)
        return 2 if summary["strict_failed"] else 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
