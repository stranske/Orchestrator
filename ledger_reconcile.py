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
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import adapters
import feedback


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
        return {str(row[0]): str(row[1] or "") for row in c.execute("SELECT run_id, source FROM costs").fetchall()}


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
        return lines
    end = len(lines)
    for idx in range(start, len(lines)):
        if lines[idx].startswith("==="):
            end = idx
            break
    return lines[start:end]


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
    completes = [int(r.get("ts") or 0) for r in rows if r.get("event") == "complete" and r.get("ts")]
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
    ("claude_session", re.compile(r'"session_id"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"')),
    ("codex_session", re.compile(r"session id:?\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)),
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
    run's telemetry."""
    if log_file is None:
        return None
    path = log_file.parent / "done" / f"{run_id}.json"
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) and obj.get("run_id") == run_id else None


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
) -> None:
    if selected_profile_id:
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
                fallback_reason="resolved_model_not_reported_by_completion",
                completed_ts=int(time.time()),
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
            key: value for key, value in {"subject_id": subject_id, "arm_id": arm_id}.items() if value
        } or None,
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
    infra_classified = 0
    resume_tokens_captured = 0
    owner_questions_recorded = 0
    log_costs_harvested = 0
    telemetry_runs_backfilled = 0
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
        existing_source = sources.get(run_id)
        if existing_source in {"langsmith", "ccusage"}:
            skipped[f"{existing_source}_cost_exists"] += 1
            continue
        log_file = _latest_log_file(run_rows)
        # 16f/16h harvest from the run's log segment (same segment _log_usage scans).
        if log_file is not None and not dry_run:
            seg = _log_segment(log_file, run_id)
            agent = next(
                (str(r.get("agent")) for r in run_rows if r.get("agent")), ""
            )
            target = next(
                (str(r.get("target")) for r in run_rows if r.get("target")), ""
            )
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
        if marker is not None and not any(
            r.get("event") == "complete" for r in run_rows
        ):
            # No ndjson complete event — the python completion step was likely killed (audit F2).
            # Fall back to the shell-native done marker for latency/exit before giving up.
            starts = [
                int(r.get("ts") or 0)
                for r in run_rows
                if r.get("event") == "start" and r.get("ts")
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
                raise ValueError(
                    f"run {run_id} changed selected profile in capacity ledger"
                )
            if profile_ids and not dry_run:
                feedback.complete_profile_attempt_unresolved(
                    run_id,
                    selected_profile_id=next(iter(profile_ids)),
                    fallback_reason="resolved_model_not_reported_marker_backfill",
                    completed_ts=marker_ts or int(time.time()),
                )
                profile_unresolved_backfills += 1
        # item 9 two-tier enum: rc>128 means the AGENT process died by SIGNAL — its non-merged
        # outcome is infrastructure noise, not capability evidence; classify so learners skip it.
        # Eventual-consistent: if the outcome row doesn't exist yet, a later daily pass catches it.
        try:
            rc_val = int(marker_rc) if marker_rc is not None else None
        except (TypeError, ValueError):
            rc_val = None
        if rc_val is not None and rc_val > 128 and not dry_run:
            if feedback.mark_transient_infra(run_id, reason=f"marker rc={rc_val}"):
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
        "infra_classified": infra_classified,
        "resume_tokens_captured": resume_tokens_captured,
        "owner_questions_recorded": owner_questions_recorded,
        "log_costs_harvested": log_costs_harvested,
        "telemetry_runs_backfilled": telemetry_runs_backfilled,
        "owner_questions_expired": (
            0 if dry_run else feedback.expire_owner_questions()
        ),
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
    try:
        feedback.DB_PATH = tmp / "orchestrator.db"
        adapters.HANDOFF = tmp
        adapters.LEDGER = tmp / "capacity-ledger.ndjson"
        feedback.record_run("local-1", "stranske/Repo#1", "implement", "codex", mode="local")
        feedback.record_run("remote-1", "stranske/Repo#2", "implement", "codex", mode="remote")
        feedback.record_run("ccusage-1", "stranske/Repo#3", "implement", "codex", mode="local")
        feedback.record_cost("remote-1", tokens_in=10, tokens_out=5, source="langsmith")
        feedback.record_cost("ccusage-1", tokens_in=20, tokens_out=10, source="ccusage")

        log = tmp / "local.log"
        log.write_text(
            "=== 2026-06-16T00:00:00Z dispatch codex/full -> stranske/Repo#1 "
            "[implement] cwd=/tmp run_id=local-1 ===\n"
            + json.dumps({"type": "turn.completed",
                          "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n"
            + json.dumps({"type": "turn.completed",
                          "usage": {"prompt_tokens": 200, "completion_tokens": 100}}) + "\n"
        )
        adapters.record_ledger("codex", count=1, event="start", run_id="local-1",
                               target="stranske/Repo#1", log_file=str(log), ts=100)
        record_completion("local-1", "codex", "stranske/Repo#1", "full", "implement",
                          str(log), started_ts=100)
        adapters.record_ledger("codex", count=1, event="start", run_id="remote-1",
                               target="stranske/Repo#2", log_file=str(log), ts=100)
        adapters.record_ledger("codex", count=1, event="start", run_id="ccusage-1",
                               target="stranske/Repo#3", log_file=str(log), ts=100)
        adapters.record_ledger("codex", count=1, event="start", run_id="unknown-1",
                               target="stranske/Repo#3", log_file=str(log), ts=100)

        dry = reconcile(adapters.LEDGER, dry_run=True)
        assert dry["prepared"] == 1 and dry["written_cost_rows"] == 0, dry
        cost = dry["costs"][0]
        assert cost["run_id"] == "local-1" and cost["tokens_in"] == 300 and cost["tokens_out"] == 150, cost
        assert dry["skipped"]["langsmith_cost_exists"] == 1 and dry["skipped"]["ccusage_cost_exists"] == 1, dry
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
        adapters.record_ledger("codex", count=1, event="start", run_id="killed-1",
                               target="stranske/Repo#4", log_file=str(klog), ts=100)
        done_dir = klog.parent / "done"
        done_dir.mkdir(exist_ok=True)
        (done_dir / "killed-1.json").write_text(
            json.dumps({"run_id": "killed-1", "rc": 137, "ts": 160})
        )

        # item 9: the killed run's non-merged outcome must get classified transient_infra from
        # the marker's rc=137 during the real (non-dry) reconcile below.
        feedback.record_outcome(
            "killed-1", adjudicated_verdict="FAIL", merged=False, durability="abandoned"
        )

        summary = reconcile(adapters.LEDGER)
        assert summary["written_cost_rows"] == 2, summary
        assert summary["marker_backfills"] == 1, summary
        assert summary["infra_classified"] == 1, summary
        with feedback._conn() as c:
            fc = c.execute(
                "SELECT failure_class, notes FROM outcomes WHERE run_id='killed-1'"
            ).fetchone()
        assert fc and fc[0] == "transient_infra" and "marker rc=137" in (fc[1] or ""), fc
        with feedback._conn() as c:
            row = c.execute("SELECT tokens_in, tokens_out, source FROM costs WHERE run_id='local-1'").fetchone()
            remote = c.execute("SELECT source FROM costs WHERE run_id='remote-1'").fetchone()
            ccusage = c.execute("SELECT source FROM costs WHERE run_id='ccusage-1'").fetchone()
            killed = c.execute("SELECT latency_s, source FROM costs WHERE run_id='killed-1'").fetchone()
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
            + json.dumps({"type": "system", "session_id": "0a1b2c3d-1111-2222-3333-444455556666"}) + "\n"
            + 'OWNER_QUESTION: {"question": "Rename the CLI flag?", "default": "keep old name", "expires_days": -1}\n'
            + json.dumps({"type": "result", "total_cost_usd": 0.4321,
                          "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n"
        )
        adapters.record_ledger("claude", count=1, event="start", run_id="harvest-1",
                               target="stranske/Repo#5", log_file=str(hlog), ts=100)
        record_completion("harvest-1", "claude", "stranske/Repo#5", "full", "implement",
                          str(hlog), started_ts=100)
        hsum = reconcile(adapters.LEDGER)
        assert hsum["resume_tokens_captured"] == 1, hsum
        assert hsum["owner_questions_recorded"] == 1, hsum
        assert hsum["owner_questions_expired"] == 1, hsum  # expires_days=-1 -> ratified same pass
        hint = feedback.resume_hint("harvest-1")
        assert hint and hint["kind"] == "claude_session" and "0a1b2c3d" in hint["command"], hint
        # 16(j): the agent's own reported dollars land in the cost row (ledger rows were $0)
        assert hsum["log_costs_harvested"] == 1, hsum
        with feedback._conn() as c:
            hcost = c.execute(
                "SELECT cost_usd FROM costs WHERE run_id='harvest-1'"
            ).fetchone()
        assert hcost and abs(hcost[0] - 0.4321) < 1e-9, hcost
        decisions = feedback.owner_decisions_for(repo="stranske/Repo")
        assert any(d["decision"] == "keep old name" and d["source"] == "default_ratified"
                   for d in decisions), decisions
        print("ledger_reconcile.py selftest: OK (completion rows, log usage parse, "
              "known-run guard, richer-source-preserving cost write, dry-run, "
              "done-marker backfill for killed completions)")
    finally:
        feedback.DB_PATH = old_db
        adapters.HANDOFF = old_handoff
        adapters.LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    complete = sub.add_parser("complete", help="append a completion row to the local capacity ledger")
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

    rec = sub.add_parser("reconcile", help="write feedback.costs rows from local ledger/log evidence")
    rec.add_argument("--ledger", type=Path)
    rec.add_argument("--dry-run", action="store_true")
    rec.add_argument("--strict", action="store_true")
    rec.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
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
