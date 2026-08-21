#!/usr/bin/env python3
"""Attribute ccusage session totals to Orchestrator runs.

ccusage exposes useful per-session token/cost totals but not a stable Orchestrator
run_id. The dispatcher does have run windows in capacity-ledger.ndjson. This tool
joins the two conservatively: a ccusage session is attributed only when its
lastActivity timestamp lands inside exactly one completed run window for the
same agent. Ambiguous or active windows are skipped.

Usage:
  python3 ccusage_reconcile.py reconcile --dry-run --json
  python3 ccusage_reconcile.py reconcile --json
  python3 ccusage_reconcile.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import adapters
import feedback

ATTRIBUTABLE_AGENTS = {"codex", "claude"}
REPLACEABLE_SOURCES = {"", "ledger", "ccusage"}
CODEX_PERIOD_TS_RE = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})")


@dataclass(frozen=True)
class RunWindow:
    run_id: str
    agent: str
    start_ts: int
    end_ts: int

    @property
    def latency_s(self) -> float:
        return float(max(0, self.end_ts - self.start_ts))


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


def _known_run_agents() -> dict[str, str]:
    with feedback._conn() as c:
        return {
            str(run_id): str(agent or "").strip().lower()
            for run_id, agent in c.execute("SELECT run_id, agent FROM runs").fetchall()
        }


def _cost_sources() -> dict[str, str]:
    with feedback._conn() as c:
        return {
            str(run_id): str(source or "").strip().lower()
            for run_id, source in c.execute("SELECT run_id, source FROM costs").fetchall()
        }


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number >= 0 else 0.0


def _parse_iso_ts(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _parse_codex_period_ts(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = CODEX_PERIOD_TS_RE.search(value)
    if not match:
        return None
    try:
        dt = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None
    # Codex rollout periods are local wall-clock timestamps; ccusage lastActivity is UTC.
    local_tz = datetime.now().astimezone().tzinfo
    return int(dt.replace(tzinfo=local_tz).timestamp())


def _run_windows(rows: Iterable[dict[str, Any]], known_agents: dict[str, str]) -> tuple[list[RunWindow], dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    for row in rows:
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            skipped["missing_run_id"] += 1
            continue
        grouped[run_id].append(row)

    windows: list[RunWindow] = []
    for run_id, run_rows in grouped.items():
        known_agent = known_agents.get(run_id)
        if not known_agent:
            skipped["unknown_run_id"] += 1
            continue
        agent = known_agent or str(run_rows[0].get("agent") or "").strip().lower()
        if agent not in ATTRIBUTABLE_AGENTS:
            skipped["unsupported_agent"] += 1
            continue
        starts = [
            int(row.get("ts"))
            for row in run_rows
            if row.get("event") == "start" and isinstance(row.get("ts"), (int, float))
        ]
        completes = [
            int(row.get("ts"))
            for row in run_rows
            if row.get("event") == "complete" and isinstance(row.get("ts"), (int, float))
        ]
        if not starts or not completes:
            skipped["incomplete_run_window"] += 1
            continue
        start_ts, end_ts = min(starts), max(completes)
        if end_ts < start_ts:
            skipped["invalid_run_window"] += 1
            continue
        windows.append(RunWindow(run_id=run_id, agent=agent, start_ts=start_ts, end_ts=end_ts))
    return windows, dict(skipped)


def _ccusage_since_arg(since_days: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, since_days))
    return since.strftime("%Y%m%d")


def _load_ccusage_sessions(*, since_days: int = 7, timeout_s: int = 60) -> tuple[list[dict[str, Any]], list[str]]:
    exe = shutil.which("ccusage")
    cmd = [exe, "session", "-j", "--since", _ccusage_since_arg(since_days)] if exe else [
        "npx", "-y", "ccusage@latest", "session", "-j", "--since", _ccusage_since_arg(since_days)
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except Exception as exc:
        return [], [f"ccusage failed: {exc}"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return [], [f"ccusage exited {proc.returncode}: {detail}"]
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [], [f"ccusage JSON parse failed: {exc.msg}"]
    sessions = data.get("session") or data.get("sessions") or data.get("data") or []
    if not isinstance(sessions, list):
        return [], ["ccusage JSON did not contain a session list"]
    return [row for row in sessions if isinstance(row, dict)], []


def _session_measurement(session: dict[str, Any]) -> dict[str, Any]:
    tokens_in = int(
        _number(session.get("inputTokens"))
        + _number(session.get("cacheCreationTokens"))
        + _number(session.get("cacheReadTokens"))
    )
    tokens_out = int(_number(session.get("outputTokens")))
    cost_usd = float(_number(session.get("totalCost")))
    models = session.get("modelsUsed")
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "models": [str(m) for m in models] if isinstance(models, list) else [],
    }


def _session_ref(session: dict[str, Any]) -> str:
    agent = str(session.get("agent") or "unknown").strip().lower()
    period = str(session.get("period") or "unknown")
    return f"{agent}:{period}"


def _match_window(
    session: dict[str, Any],
    windows: list[RunWindow],
    *,
    slack_seconds: int,
) -> tuple[RunWindow | None, str | None]:
    agent = str(session.get("agent") or "").strip().lower()
    if agent not in ATTRIBUTABLE_AGENTS:
        return None, "unsupported_agent"
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    last_ts = _parse_iso_ts(metadata.get("lastActivity"))
    if last_ts is None:
        return None, "missing_last_activity"
    matches = [
        window
        for window in windows
        if window.agent == agent
        and (window.start_ts - slack_seconds) <= last_ts <= (window.end_ts + slack_seconds)
    ]
    if not matches:
        return None, "no_matching_run_window"
    if len(matches) > 1:
        return None, "ambiguous_run_window"
    match = matches[0]
    period_ts = _parse_codex_period_ts(session.get("period")) if agent == "codex" else None
    if period_ts is not None and not (
        (match.start_ts - slack_seconds) <= period_ts <= (match.end_ts + slack_seconds)
    ):
        return None, "period_outside_run_window"
    return match, None


def reconcile(
    ledger: Path | None = None,
    *,
    sessions: list[dict[str, Any]] | None = None,
    since_days: int = 7,
    slack_seconds: int = 60,
    dry_run: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    ledger_path = Path(ledger or adapters.LEDGER)
    ledger_rows, errors = _read_ledger(ledger_path)
    if sessions is None:
        sessions, ccusage_errors = _load_ccusage_sessions(since_days=since_days)
        errors.extend(ccusage_errors)

    known_agents = _known_run_agents()
    cost_sources = _cost_sources()
    windows, window_skips = _run_windows(ledger_rows, known_agents)
    skipped: dict[str, int] = defaultdict(int, window_skips)
    by_run: dict[str, dict[str, Any]] = {}

    for session in sessions:
        window, reason = _match_window(session, windows, slack_seconds=slack_seconds)
        if reason:
            skipped[reason] += 1
            continue
        assert window is not None
        measurement = _session_measurement(session)
        if not (measurement["tokens_in"] or measurement["tokens_out"] or measurement["cost_usd"]):
            skipped["no_measurement"] += 1
            continue
        rec = by_run.setdefault(
            window.run_id,
            {
                "run_id": window.run_id,
                "agent": window.agent,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "latency_s": window.latency_s,
                "source": "ccusage",
                "sessions": [],
                "models": [],
            },
        )
        rec["tokens_in"] += measurement["tokens_in"]
        rec["tokens_out"] += measurement["tokens_out"]
        rec["cost_usd"] += measurement["cost_usd"]
        rec["sessions"].append(_session_ref(session))
        for model in measurement["models"]:
            if model not in rec["models"]:
                rec["models"].append(model)

    prepared: list[dict[str, Any]] = []
    written = 0
    for run_id, rec in sorted(by_run.items()):
        existing_source = cost_sources.get(run_id, "")
        if existing_source not in REPLACEABLE_SOURCES:
            skipped[f"{existing_source or 'existing'}_cost_exists"] += 1
            continue
        rec["cost_usd"] = round(float(rec["cost_usd"]), 6)
        prepared.append(rec)
        if not dry_run:
            feedback.record_cost(
                run_id,
                tokens_in=int(rec["tokens_in"]),
                tokens_out=int(rec["tokens_out"]),
                cost_usd=float(rec["cost_usd"]),
                latency_s=float(rec["latency_s"]),
                source="ccusage",
            )
            written += 1

    return {
        "dry_run": dry_run,
        "ledger": str(ledger_path),
        "rows_read": len(ledger_rows),
        "sessions_read": len(sessions),
        "completed_windows": len(windows),
        "prepared": len(prepared),
        "written_cost_rows": written,
        "costs": prepared,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors,
        "strict_failed": strict and bool(errors),
    }


def _print_summary(summary: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    action = "would write" if summary["dry_run"] else "wrote"
    print(
        f"ccusage_reconcile: read {summary['sessions_read']} session(s), "
        f"saw {summary['completed_windows']} completed run window(s), "
        f"{action} {summary['written_cost_rows'] if not summary['dry_run'] else summary['prepared']} "
        "cost row(s)"
    )
    if summary["skipped"]:
        print("skipped:", json.dumps(summary["skipped"], sort_keys=True))
    for error in summary["errors"]:
        print(f"- error: {error}")


def _selftest() -> None:
    old_db, old_ledger = feedback.DB_PATH, adapters.LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="ccusage-reconcile-selftest-"))
    try:
        feedback.DB_PATH = tmp / "orchestrator.db"
        adapters.LEDGER = tmp / "capacity-ledger.ndjson"
        base = int(datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc).timestamp())

        feedback.record_run("run-codex", "o/r#1", "implement", "codex", mode="local", ts=base)
        feedback.record_run("run-langsmith", "o/r#2", "implement", "codex", mode="local", ts=base)
        feedback.record_run("run-a", "o/r#3", "implement", "codex", mode="local", ts=base)
        feedback.record_run("run-b", "o/r#4", "implement", "codex", mode="local", ts=base)
        feedback.record_run("run-vibe", "o/r#5", "implement", "vibe", mode="local", ts=base)
        feedback.record_cost("run-codex", tokens_in=1, tokens_out=1, source="ledger")
        feedback.record_cost("run-langsmith", tokens_in=9, tokens_out=9, source="langsmith")

        adapters.record_ledger("codex", event="start", run_id="run-codex", ts=base, started_ts=base)
        adapters.record_ledger("codex", event="complete", run_id="run-codex", ts=base + 100, started_ts=base)
        adapters.record_ledger("codex", event="start", run_id="run-langsmith", ts=base + 200, started_ts=base + 200)
        adapters.record_ledger("codex", event="complete", run_id="run-langsmith", ts=base + 260, started_ts=base + 200)
        adapters.record_ledger("codex", event="start", run_id="run-a", ts=base + 400, started_ts=base + 400)
        adapters.record_ledger("codex", event="complete", run_id="run-a", ts=base + 500, started_ts=base + 400)
        adapters.record_ledger("codex", event="start", run_id="run-b", ts=base + 450, started_ts=base + 450)
        adapters.record_ledger("codex", event="complete", run_id="run-b", ts=base + 550, started_ts=base + 450)
        adapters.record_ledger("vibe", event="start", run_id="run-vibe", ts=base + 600, started_ts=base + 600)
        adapters.record_ledger("vibe", event="complete", run_id="run-vibe", ts=base + 700, started_ts=base + 600)

        def iso(offset: int) -> str:
            return datetime.fromtimestamp(base + offset, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        sessions = [
            {
                "agent": "codex",
                "period": "codex-session-1",
                "metadata": {"lastActivity": iso(50)},
                "inputTokens": 10,
                "cacheCreationTokens": 3,
                "cacheReadTokens": 7,
                "outputTokens": 5,
                "totalCost": 0.25,
                "modelsUsed": ["gpt-test"],
            },
            {
                "agent": "codex",
                "period": "codex-session-2",
                "metadata": {"lastActivity": iso(90)},
                "inputTokens": 1,
                "cacheCreationTokens": 0,
                "cacheReadTokens": 4,
                "outputTokens": 2,
                "totalCost": 0.05,
                "modelsUsed": ["gpt-test"],
            },
            {
                "agent": "codex",
                "period": "protected",
                "metadata": {"lastActivity": iso(230)},
                "inputTokens": 100,
                "outputTokens": 100,
                "totalCost": 10,
            },
            {
                "agent": "codex",
                "period": "ambiguous",
                "metadata": {"lastActivity": iso(475)},
                "inputTokens": 10,
                "outputTokens": 1,
                "totalCost": 1,
            },
            {
                "agent": "codex",
                "period": "nomatch",
                "metadata": {"lastActivity": iso(900)},
                "inputTokens": 10,
                "outputTokens": 1,
                "totalCost": 1,
            },
            {
                "agent": "codex",
                "period": "rollout-2026-06-22T06-00-00-019eedce-false-positive",
                "metadata": {"lastActivity": iso(50)},
                "inputTokens": 10,
                "outputTokens": 1,
                "totalCost": 1,
            },
            {
                "agent": "vibe",
                "period": "unsupported",
                "metadata": {"lastActivity": iso(650)},
                "inputTokens": 10,
                "outputTokens": 1,
                "totalCost": 1,
            },
        ]

        dry = reconcile(adapters.LEDGER, sessions=sessions, dry_run=True, slack_seconds=0)
        assert dry["prepared"] == 1, dry
        assert dry["skipped"]["langsmith_cost_exists"] == 1, dry
        assert dry["skipped"]["ambiguous_run_window"] == 1, dry
        assert dry["skipped"]["no_matching_run_window"] == 1, dry
        assert dry["skipped"]["period_outside_run_window"] == 1, dry
        assert dry["skipped"]["unsupported_agent"] >= 1, dry
        rec = dry["costs"][0]
        assert rec["run_id"] == "run-codex", rec
        assert rec["tokens_in"] == 25 and rec["tokens_out"] == 7, rec
        assert abs(rec["cost_usd"] - 0.30) < 1e-9, rec

        live = reconcile(adapters.LEDGER, sessions=sessions, dry_run=False, slack_seconds=0)
        assert live["written_cost_rows"] == 1, live
        with feedback._conn() as c:
            row = c.execute(
                "SELECT tokens_in, tokens_out, cost_usd, latency_s, source FROM costs WHERE run_id='run-codex'"
            ).fetchone()
            protected = c.execute("SELECT source FROM costs WHERE run_id='run-langsmith'").fetchone()
        assert row == (25, 7, 0.3, 100.0, "ccusage"), row
        assert protected == ("langsmith",), protected

        reconcile(adapters.LEDGER, sessions=sessions, dry_run=False, slack_seconds=0)
        with feedback._conn() as c:
            again = c.execute("SELECT COUNT(*), cost_usd FROM costs WHERE run_id='run-codex'").fetchone()
        assert again == (1, 0.3), again
        print("ccusage_reconcile.py selftest: OK (unique window attribution, aggregation, "
              "source precedence, dry-run, idempotent re-ingest)")
    finally:
        feedback.DB_PATH = old_db
        adapters.LEDGER = old_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--selftest", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    rec = sub.add_parser("reconcile", help="write feedback.costs rows from ccusage sessions")
    rec.add_argument("--ledger", type=Path)
    rec.add_argument("--since-days", type=int, default=7)
    rec.add_argument("--slack-seconds", type=int, default=60)
    rec.add_argument("--dry-run", action="store_true")
    rec.add_argument("--json", action="store_true")
    rec.add_argument("--strict", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.cmd != "reconcile":
        p.error("choose a command or pass --selftest")
    summary = reconcile(
        args.ledger,
        since_days=args.since_days,
        slack_seconds=args.slack_seconds,
        dry_run=args.dry_run,
        strict=args.strict,
    )
    _print_summary(summary, as_json=args.json)
    return 2 if summary["strict_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
