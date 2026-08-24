#!/usr/bin/env python3
"""Ingest LangSmith fleet trace artifacts into the Orchestrator feedback store.

First increment: consume safe local NDJSON records that conform to Workflows'
`langsmith-fleet/v1` contract. This avoids depending on LangSmith API shape while
still joining trace refs, provider/model, latency, cost, and token usage to the
Orchestrator's durable `run_id` dataset before external trace retention expires.

Usage:
  python3 langsmith_pull.py --ndjson path/to/langsmith-fleet.ndjson --dry-run --json
  python3 langsmith_pull.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import feedback

SCHEMA_VERSION = "langsmith-fleet/v1"
TOKEN_IN_KEYS = ("tokens_in", "input_tokens", "prompt_tokens", "input_token_count")
TOKEN_OUT_KEYS = ("tokens_out", "output_tokens", "completion_tokens", "output_token_count")
TOKEN_TOTAL_KEYS = ("total_tokens", "tokens_total")
LATENCY_MS_KEYS = ("latency_ms",)
LATENCY_S_KEYS = ("latency_s", "latency_seconds")
COST_KEYS = ("cost_usd", "usd_cost")
NESTED_METRIC_KEYS = ("usage", "token_usage", "metrics", "domain")
GITHUB_REF_RE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)?#?(?P<num>\d+)$")
GITHUB_FULL_REF_RE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<num>\d+)$")
DURABILITY_VALUES = {
    "pending",
    "durable",
    "reverted",
    "reworked",
    "reopened",
    "broke_later",
    "abandoned",
}


def known_run_ids() -> set[str]:
    with feedback._conn() as c:
        return {str(r[0]) for r in c.execute("SELECT run_id FROM runs").fetchall()}


def _normalize_repo(repo: str | None) -> str | None:
    if not repo:
        return None
    repo = repo.strip()
    return repo.lower() if "/" in repo else None


def _parse_github_ref(value: Any, *, default_repo: str | None = None) -> tuple[str, int] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    full = GITHUB_FULL_REF_RE.match(text)
    if full:
        repo = _normalize_repo(full.group("repo"))
        return (repo, int(full.group("num"))) if repo else None
    compact = GITHUB_REF_RE.match(text)
    if compact:
        repo = _normalize_repo(compact.group("repo") or default_repo)
        return (repo, int(compact.group("num"))) if repo else None
    return None


def _run_indexes() -> dict[str, Any]:
    by_ref: dict[tuple[str, int], list[str]] = defaultdict(list)
    by_ref_agent: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    known: set[str] = set()
    with feedback._conn() as c:
        rows = c.execute("SELECT run_id, target, agent, pr_number FROM runs").fetchall()
    for run_id, target, agent, pr_number in rows:
        run_id = str(run_id)
        known.add(run_id)
        ref = _parse_github_ref(target)
        if ref is None and pr_number is not None:
            target_repo = str(target).split("#", 1)[0] if isinstance(target, str) else None
            ref = _parse_github_ref(str(pr_number), default_repo=target_repo)
        if ref is None:
            continue
        by_ref[ref].append(run_id)
        if agent:
            by_ref_agent[(ref[0], ref[1], str(agent).strip().lower())].append(run_id)
    return {"known": known, "by_ref": by_ref, "by_ref_agent": by_ref_agent}


def _nested_dicts(record: dict[str, Any]):
    yield record
    for key in NESTED_METRIC_KEYS:
        value = record.get(key)
        if isinstance(value, dict):
            yield value


def _coerce_nonnegative_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _first_number(record: dict[str, Any], keys: tuple[str, ...]) -> tuple[float, bool]:
    for container in _nested_dicts(record):
        for key in keys:
            if key in container:
                value = _coerce_nonnegative_number(container.get(key))
                if value is None:
                    return 0.0, False
                return value, True
    return 0.0, False


def _measurements(record: dict[str, Any]) -> tuple[dict[str, float | int], bool]:
    tokens_in, has_tokens_in = _first_number(record, TOKEN_IN_KEYS)
    tokens_out, has_tokens_out = _first_number(record, TOKEN_OUT_KEYS)
    total_tokens, has_total_tokens = _first_number(record, TOKEN_TOTAL_KEYS)
    if has_total_tokens and not (has_tokens_in or has_tokens_out):
        tokens_in = total_tokens
        has_tokens_in = True

    cost_usd, has_cost = _first_number(record, COST_KEYS)
    latency_ms, has_latency_ms = _first_number(record, LATENCY_MS_KEYS)
    latency_s, has_latency_s = _first_number(record, LATENCY_S_KEYS)
    if has_latency_ms:
        latency_s = latency_ms / 1000.0
        has_latency_s = True

    has_measurement = has_tokens_in or has_tokens_out or has_cost or has_latency_s
    return {
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_usd": float(cost_usd),
        "latency_s": float(latency_s),
    }, has_measurement


def _text(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _profile_text(record: dict[str, Any], key: str) -> str | None:
    """Read the Workflows profile/fallback contract without conflating trace model.

    Workflows emits worker ``profile_id``, requested/selected/reported/resolved
    model, fallback reason, and runner/CLI versions either at the top level or
    inside ``domain``. ``model`` remains generic trace telemetry and is never
    promoted into worker-reported or provider-resolved identity here.
    """
    direct = _text(record, key)
    if direct:
        return direct
    domain = record.get("domain")
    if isinstance(domain, dict):
        value = domain.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _operation_role(record: dict[str, Any]) -> str:
    explicit = _text(record, "operation_role")
    if explicit is None:
        domain = record.get("domain")
        if isinstance(domain, dict):
            value = domain.get("operation_role")
            explicit = value.strip() if isinstance(value, str) and value.strip() else None
    return feedback.derive_operation_role(_text(record, "operation"), explicit)


def _attempt_ordinal(record: dict[str, Any]) -> int | None:
    value = record.get("attempt_ordinal", record.get("attempt"))
    if value is None and isinstance(record.get("domain"), dict):
        value = record["domain"].get("attempt_ordinal", record["domain"].get("attempt"))
    try:
        ordinal = int(value) if value is not None else None
        return ordinal if ordinal is not None and ordinal > 0 else None
    except (TypeError, ValueError):
        return None


def _record_agent(record: dict[str, Any]) -> str | None:
    direct = _text(record, "agent")
    if direct:
        return direct.lower()
    domain = record.get("domain")
    if isinstance(domain, dict):
        agent = domain.get("agent")
        if isinstance(agent, str) and agent.strip():
            return agent.strip().lower()
    return None


def _durability_label(record: dict[str, Any]) -> tuple[str | None, str]:
    """Return a fleet-exported durability label, if this record carries one."""
    operation = _text(record, "operation")
    domain = record.get("domain")
    if operation != "durability" or not isinstance(domain, dict):
        return None, ""
    raw = domain.get("durability") or domain.get("result")
    label = str(raw).strip().lower() if raw is not None else ""
    if label not in DURABILITY_VALUES:
        return None, f"invalid durability label: {label or '<missing>'}"
    reason = domain.get("reason")
    reason_text = str(reason).strip() if reason is not None else ""
    return label, reason_text


def _record_ref(record: dict[str, Any]) -> tuple[tuple[str, int], str] | tuple[None, None]:
    default_repo = _text(record, "repo")
    for field in ("github_pr", "github_issue"):
        ref = _parse_github_ref(record.get(field), default_repo=default_repo)
        if ref:
            return ref, field
    return None, None


def _resolve_run_id(record: dict[str, Any], indexes: dict[str, Any]) -> tuple[str | None, str, str]:
    source_run_id = _text(record, "run_id")
    if source_run_id and source_run_id in indexes["known"]:
        return source_run_id, "run_id", ""
    ref, ref_field = _record_ref(record)
    if ref is None:
        return None, "unmatched_run_id", "no github_pr/github_issue bridge field"
    agent = _record_agent(record)
    if agent:
        agent_matches = indexes["by_ref_agent"].get((ref[0], ref[1], agent), [])
        if len(agent_matches) == 1:
            return (
                agent_matches[0],
                "github_ref_agent",
                f"{ref_field}:{ref[0]}#{ref[1]} agent:{agent}",
            )
        if len(agent_matches) > 1:
            return None, "ambiguous_github_ref_agent", f"{ref[0]}#{ref[1]} agent:{agent}"
    ref_matches = indexes["by_ref"].get(ref, [])
    if len(ref_matches) == 1:
        return ref_matches[0], "github_ref", f"{ref_field}:{ref[0]}#{ref[1]}"
    if len(ref_matches) > 1:
        return None, "ambiguous_github_ref", f"{ref[0]}#{ref[1]}"
    return None, "unmatched_github_ref", f"{ref[0]}#{ref[1]}"


def _record_error(summary: dict[str, Any], message: str):
    summary["errors"].append(message)


def _skip(summary: dict[str, Any], reason: str):
    summary["skipped"][reason] += 1


def _iter_records(path: Path, summary: dict[str, Any]):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    _record_error(summary, f"{path}:{line_no}: invalid JSON: {exc.msg}")
                    continue
                if not isinstance(record, dict):
                    _record_error(summary, f"{path}:{line_no}: record is not an object")
                    continue
                yield path, line_no, record
    except OSError as exc:
        _record_error(summary, f"{path}: cannot read: {exc}")


def _trace_key(source: str, run_id: str, record: dict[str, Any], raw_ref: str) -> str:
    trace_id = _text(record, "trace_id")
    if trace_id:
        suffix = trace_id
    else:
        suffix = f"{_text(record, 'operation') or 'unknown'}:{raw_ref}"
    return f"{source}:{run_id}:{suffix}"


def ingest_files(
    paths: list[Path],
    *,
    dry_run: bool = False,
    strict: bool = False,
    allow_unmatched: bool = False,
    source: str = "langsmith",
) -> dict[str, Any]:
    indexes = _run_indexes()
    cost_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_s": 0.0}
    )
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "files": [str(path) for path in paths],
        "records_read": 0,
        "matched_records": 0,
        "trace_records": 0,
        "cost_records": 0,
        "durability_records": 0,
        "matched_by": defaultdict(int),
        "written_trace_records": 0,
        "written_cost_records": 0,
        "written_durability_records": 0,
        "operation_role_counts": defaultdict(int),
        "worker_profile_records": 0,
        "skipped": defaultdict(int),
        "errors": [],
    }

    for path in paths:
        for record_path, line_no, record in _iter_records(path, summary):
            summary["records_read"] += 1
            raw_ref = _text(record, "artifact_ref") or f"{record_path}:{line_no}"

            if record.get("schema_version") != SCHEMA_VERSION:
                _skip(summary, "wrong_schema")
                continue
            run_id = _text(record, "run_id")
            if not run_id:
                _skip(summary, "missing_run_id")
                continue
            match_method = "allow_unmatched"
            bridge_detail = ""
            if not allow_unmatched:
                resolved_run_id, match_method, bridge_detail = _resolve_run_id(record, indexes)
                if not resolved_run_id:
                    _skip(summary, match_method)
                    continue
                run_id = resolved_run_id

            durability, durability_reason = _durability_label(record)
            measurement, has_measurement = _measurements(record)
            has_trace_ref = bool(_text(record, "trace_id") or _text(record, "trace_url"))
            if not durability and not has_trace_ref and not has_measurement:
                _skip(summary, "no_trace_or_measurement")
                continue
            try:
                operation_role = _operation_role(record)
            except ValueError:
                _skip(summary, "invalid_operation_role")
                continue

            summary["matched_records"] += 1
            summary["matched_by"][match_method] += 1
            summary["operation_role_counts"][operation_role] += 1
            if operation_role == "worker" and _profile_text(record, "profile_id"):
                summary["worker_profile_records"] += 1
            if bridge_detail:
                raw_ref = (
                    f"{raw_ref}; source_run_id={_text(record, 'run_id')}; bridge={bridge_detail}"
                )
            if durability:
                summary["durability_records"] += 1
                if not dry_run:
                    notes = "langsmith durability export"
                    if durability_reason:
                        notes = f"{notes}: {durability_reason}"
                    feedback.record_outcome(
                        run_id,
                        merged=True,
                        durability=durability,
                        notes=notes,
                    )
                    summary["written_durability_records"] += 1
                if not has_trace_ref and not has_measurement:
                    continue

            summary["trace_records"] += 1
            if not dry_run:
                feedback.record_execution_trace(
                    run_id,
                    trace_id=_text(record, "trace_id"),
                    trace_url=_text(record, "trace_url"),
                    provider=_text(record, "provider"),
                    model=_text(record, "model"),
                    operation=_text(record, "operation"),
                    status=_text(record, "status"),
                    latency_s=measurement["latency_s"],
                    cost_usd=measurement["cost_usd"],
                    source=source,
                    raw_ref=raw_ref,
                    trace_key=_trace_key(source, run_id, record, raw_ref),
                    operation_role=operation_role,
                    profile_id=_profile_text(record, "profile_id"),
                    requested_provider=_profile_text(record, "requested_provider"),
                    requested_model=_profile_text(record, "requested_model"),
                    selected_model=_profile_text(record, "selected_model"),
                    reported_model=_profile_text(record, "reported_model"),
                    resolved_provider=_profile_text(record, "resolved_provider")
                    or _text(record, "provider"),
                    resolved_model=(
                        _profile_text(record, "resolved_model")
                        if operation_role == "worker"
                        else _text(record, "resolved_model") or _text(record, "model")
                    ),
                    fallback_reason=_profile_text(record, "fallback_reason"),
                    runner_version=_profile_text(record, "runner_version"),
                    cli_version=_profile_text(record, "cli_version"),
                    attempt_ordinal=_attempt_ordinal(record),
                    tokens_in=measurement["tokens_in"],
                    tokens_out=measurement["tokens_out"],
                )
                summary["written_trace_records"] += 1

            if has_measurement:
                totals = cost_totals[run_id]
                totals["tokens_in"] += measurement["tokens_in"]
                totals["tokens_out"] += measurement["tokens_out"]
                totals["cost_usd"] += measurement["cost_usd"]
                totals["latency_s"] += measurement["latency_s"]

    summary["cost_records"] = len(cost_totals)
    if not dry_run:
        for run_id, totals in sorted(cost_totals.items()):
            feedback.record_cost(
                run_id,
                tokens_in=int(totals["tokens_in"]),
                tokens_out=int(totals["tokens_out"]),
                cost_usd=totals["cost_usd"],
                latency_s=totals["latency_s"],
                source=source,
            )
            summary["written_cost_records"] += 1

    summary["skipped"] = dict(sorted(summary["skipped"].items()))
    summary["matched_by"] = dict(sorted(summary["matched_by"].items()))
    summary["operation_role_counts"] = dict(sorted(summary["operation_role_counts"].items()))
    if strict and (
        summary["errors"]
        or summary["skipped"].get("wrong_schema")
        or summary["skipped"].get("missing_run_id")
        or summary["skipped"].get("invalid_operation_role")
    ):
        summary["strict_failed"] = True
    else:
        summary["strict_failed"] = False
    return summary


def _print_summary(summary: dict[str, Any], *, as_json: bool):
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    action = "would write" if summary["dry_run"] else "wrote"
    trace_count = (
        summary["trace_records"] if summary["dry_run"] else summary["written_trace_records"]
    )
    cost_count = summary["cost_records"] if summary["dry_run"] else summary["written_cost_records"]
    durability_count = (
        summary["durability_records"]
        if summary["dry_run"]
        else summary["written_durability_records"]
    )
    print(
        f"langsmith_pull: read {summary['records_read']} record(s), matched "
        f"{summary['matched_records']}, {action} {trace_count} trace "
        f"record(s), {cost_count} cost row(s), and {durability_count} durability row(s)"
    )
    if summary["skipped"]:
        print(f"skipped: {json.dumps(summary['skipped'], sort_keys=True)}")
    if summary["errors"]:
        print("errors:")
        for error in summary["errors"]:
            print(f"- {error}")


def _selftest():
    tmp = Path(tempfile.mkdtemp(prefix="langsmith-pull-selftest-"))
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp / "orchestrator.db"
    try:
        feedback.record_run("orch-1", "stranske/Workflows#1", "implement", "codex")
        feedback.record_run(
            "remote:stranske/Workflows#2151:codex",
            "stranske/Workflows#2151",
            "implement",
            "codex",
            mode="remote",
            pr_number=2151,
        )
        fixture = tmp / "langsmith-fleet.ndjson"
        records = [
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "stranske/Workflows",
                "surface": "orchestrator",
                "operation": "evaluate_pr_compare",
                "run_id": "orch-1",
                "status": "success",
                "github_issue": "stranske/Workflows#1",
                "domain": {"phase": "selftest"},
                "trace_id": "trace-1",
                "trace_url": "https://smith.langchain.com/r/trace-1",
                "provider": "openai",
                "model": "gpt-test",
                "latency_ms": 1500,
                "cost_usd": 0.25,
                "tokens_in": 100,
                "tokens_out": 20,
            },
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "stranske/Workflows",
                "surface": "agent-automation",
                "operation": "verifier",
                "run_id": "workflows-run-123",
                "status": "success",
                "github_issue": "stranske/Workflows#2150",
                "github_pr": "stranske/Workflows#2151",
                "domain": {"workflow": "agents-verifier", "agent": "codex", "step": "compare"},
                "trace_id": "trace-remote",
                "provider": "openai",
                "model": "gpt-test",
                "latency_ms": 250,
                "cost_usd": 0.07,
                "input_tokens": 10,
                "output_tokens": 3,
            },
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "stranske/Workflows",
                "surface": "orchestrator",
                "operation": "summarize",
                "run_id": "orch-1",
                "status": "fallback",
                "github_issue": "stranske/Workflows#1",
                "domain": {"phase": "selftest"},
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "latency_s": 0.5,
                "cost_usd": 0.05,
            },
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "stranske/Workflows",
                "surface": "orchestrator",
                "operation": "verify",
                "run_id": "other-run",
                "status": "success",
                "github_issue": "stranske/Workflows#404",
                "domain": {"phase": "selftest"},
                "trace_id": "trace-other",
                "cost_usd": 1.0,
            },
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "stranske/Workflows",
                "surface": "orchestrator",
                "operation": "noop",
                "run_id": "orch-1",
                "status": "skipped",
                "github_issue": "stranske/Workflows#1",
                "domain": {"phase": "selftest"},
            },
            {
                "schema_version": SCHEMA_VERSION,
                "repo": "stranske/Workflows",
                "surface": "agent-automation",
                "operation": "durability",
                "run_id": "durability:stranske/Workflows#2151",
                "status": "error",
                "github_issue": "stranske/Workflows#2150",
                "github_pr": "stranske/Workflows#2151",
                "domain": {
                    "workflow": "maint-85-keepalive-durability-export",
                    "agent": "codex",
                    "step": "post-merge-durability",
                    "attempt": 1,
                    "result": "reverted",
                    "durability": "reverted",
                    "reason": "matching_revert_pr",
                },
            },
            {"schema_version": "older", "run_id": "orch-1"},
        ]
        with fixture.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            handle.write("{bad json}\n")

        dry = ingest_files([fixture], dry_run=True)
        assert dry["cost_records"] == 2 and dry["written_cost_records"] == 0, dry
        assert dry["durability_records"] == 1 and dry["written_durability_records"] == 0, dry
        assert dry["matched_by"] == {"github_ref_agent": 2, "run_id": 2}, dry
        with feedback._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0] == 0

        summary = ingest_files([fixture])
        assert summary["records_read"] == 7, summary
        assert summary["matched_records"] == 4, summary
        assert summary["matched_by"] == {"github_ref_agent": 2, "run_id": 2}, summary
        assert summary["trace_records"] == 3 and summary["written_trace_records"] == 3, summary
        assert summary["cost_records"] == 2 and summary["written_cost_records"] == 2, summary
        assert (
            summary["durability_records"] == 1 and summary["written_durability_records"] == 1
        ), summary
        assert summary["skipped"] == {
            "no_trace_or_measurement": 1,
            "unmatched_github_ref": 1,
            "wrong_schema": 1,
        }, summary
        assert len(summary["errors"]) == 1, summary
        with feedback._conn() as c:
            cost = c.execute(
                "SELECT tokens_in, tokens_out, cost_usd, latency_s, source FROM costs "
                "WHERE run_id='orch-1'"
            ).fetchone()
            remote_cost = c.execute(
                "SELECT tokens_in, tokens_out, cost_usd, latency_s, source FROM costs "
                "WHERE run_id='remote:stranske/Workflows#2151:codex'"
            ).fetchone()
            traces = c.execute(
                "SELECT COUNT(*) FROM execution_traces WHERE run_id='orch-1'"
            ).fetchone()[0]
            remote_trace = c.execute(
                "SELECT raw_ref FROM execution_traces "
                "WHERE run_id='remote:stranske/Workflows#2151:codex'"
            ).fetchone()
            remote_outcome = c.execute(
                "SELECT merged, durability, notes FROM outcomes "
                "WHERE run_id='remote:stranske/Workflows#2151:codex'"
            ).fetchone()
            role_counts = dict(
                c.execute(
                    "SELECT operation_role,COUNT(*) FROM execution_attempts "
                    "GROUP BY operation_role"
                ).fetchall()
            )
        assert cost == (150, 30, 0.3, 2.0, "langsmith"), cost
        assert remote_cost == (10, 3, 0.07, 0.25, "langsmith"), remote_cost
        assert traces == 2, traces
        assert remote_trace and "source_run_id=workflows-run-123" in remote_trace[0], remote_trace
        assert remote_outcome == (
            1,
            "reverted",
            "langsmith durability export: matching_revert_pr",
        ), remote_outcome
        assert summary["operation_role_counts"] == {
            "evaluator": 1,
            "synthesizer": 1,
            "unknown": 1,
            "verifier": 1,
        }, summary
        assert role_counts == {"evaluator": 1, "synthesizer": 1, "verifier": 1}, role_counts
        assert (
            feedback.resolved_worker_model_for_run("orch-1") is None
        ), "evaluate_pr_compare evaluator changed worker-profile attribution"
        print(
            "langsmith_pull.py selftest: OK (fleet NDJSON parse, run_id/github_pr+agent bridge, "
            "validated operation roles, evaluator-safe worker attribution, trace retention, "
            "cost aggregation, durability labels, dry-run, skip/error accounting)"
        )
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ndjson",
        action="append",
        type=Path,
        default=[],
        help="langsmith-fleet/v1 NDJSON artifact to ingest; repeatable",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="additional NDJSON artifacts")
    parser.add_argument(
        "--dry-run", action="store_true", help="parse and join without writing feedback DB"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero on malformed JSON, wrong schema, or missing run_id",
    )
    parser.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="write rows even when run_id is not already present in feedback.runs",
    )
    parser.add_argument("--source", default="langsmith", help="source label for costs/traces")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    parser.add_argument("--selftest", action="store_true", help="run offline selftest")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    paths = [*args.ndjson, *args.paths]
    if not paths:
        parser.error("provide at least one --ndjson PATH or positional PATH")

    summary = ingest_files(
        paths,
        dry_run=args.dry_run,
        strict=args.strict,
        allow_unmatched=args.allow_unmatched,
        source=args.source,
    )
    _print_summary(summary, as_json=args.json)
    return 2 if summary["strict_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
