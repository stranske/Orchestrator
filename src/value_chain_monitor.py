#!/usr/bin/env python3
"""Read demand independently of invocations and name the first broken value-chain step.

This is the data section of switch_review, not a second auditor or event store.
Collectors return None on absent/incomplete evidence; a measured zero requires a
complete population. Reads use existing ledger events, Brain rows and fleet issues.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import capabilities

CAPABILITY_ID = "value-chain-monitor"
WINDOW_DAYS = 90
# Explicit dependence, not a guess from a capability name or lack of heartbeats.
INPUT_FLAGS = {
    "role-triage": ("ORCH_DISPATCH_LANE",),
    "issue-readiness": ("ORCH_DISPATCH_LANE",),
}


def _prompt_batches(inputs: dict[str, Any]) -> int | None:
    issues = inputs.get("fleet_issues")
    if issues is None:
        return None
    hours = Counter(str(i["createdAt"])[:13] for i in issues if i.get("in_window", True))
    return sum(n for n in hours.values() if n >= 3)


def _large_issues(inputs: dict[str, Any]) -> int | None:
    issues = inputs.get("fleet_issues")
    if issues is None:
        return None
    return sum(
        i["state"] == "OPEN" and len(re.findall(r"(?m)^\s*- \[[ xX]\]", i["body"])) >= 12
        for i in issues
    )


def _open_issues(inputs: dict[str, Any]) -> int | None:
    issues = inputs.get("fleet_issues")
    return None if issues is None else sum(i["state"] == "OPEN" for i in issues)


def _disputes(inputs: dict[str, Any]) -> int | None:
    events = inputs.get("completion_events")
    if events is None:
        return None
    return sum(
        "adjudicator" in e["payload"].get("role_ids", [])
        and e["payload"].get("result", {}).get("disagreement") is True
        and e["payload"].get("result", {}).get("selector_reason_id")
        == "persisted_evidence_disagreement"
        for e in events
    )


# Callables can be replaced/extended without changing reporting or adding telemetry.
SITUATION_QUERIES: dict[str, Callable[[dict[str, Any]], int | None]] = {
    "role-prompt": _prompt_batches,
    "role-decomposer": _large_issues,
    "role-triage": _open_issues,
    "role-adjudicator": _disputes,
}


def situation_count(cap_id: str, inputs: dict[str, Any]) -> int | None:
    if cap_id in SITUATION_QUERIES:
        return SITUATION_QUERIES[cap_id](inputs)
    # Lane probes and rail inputs must carry an independent measured denominator.
    # Advisor offers and cadence invocations cannot substitute for that population.
    probes = inputs.get("precondition_probes", {}).get(cap_id)
    if probes is not None:
        return sum(p["precondition_met"] is True for p in probes)
    return inputs.get("cadence_inputs", {}).get(cap_id)


def first_break(row: Mapping[str, Any]) -> str | None:
    demand = row["situation_count"]
    if demand is None:
        return None
    if demand == 0:
        return "no_situation"
    for field, broken in (
        ("offered", "not_offered"),
        ("invocation_count", "not_invoked"),
        ("usable", "unusable"),
        ("acted_on", "not_acted_on"),
        ("outcome", "no_outcome"),
    ):
        if row[field] is None:
            return None
        if row[field] == 0:
            if field == "offered" and row.get("invocation_count"):
                continue
            return broken
    return "works"


def _brain(db: Path, since: int, now: int) -> dict[str, Any]:
    result: dict[str, Any] = {"completion_events": None, "edges": None, "errors": []}
    try:
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            result["completion_events"] = [
                {**dict(r), "payload": json.loads(r["payload_json"])}
                for r in con.execute(
                    "SELECT * FROM completion_events WHERE created_ts BETWEEN ? AND ? "
                    "AND validation_status='accepted'",
                    (since, now),
                )
            ]
            result["edges"] = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM influence_edges WHERE created_ts BETWEEN ? AND ?",
                    (since, now),
                )
            ]
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["errors"].append(f"Brain evidence unavailable: {exc}")
    return result


def fleet_issues(gh_fn: Callable, since: int, now: int, repos: list[str]) -> list[dict] | None:
    """Read all open issue pages plus every creation in the declared window.

    Recent creations sort newest first and stop only after crossing the window;
    older open issues come from a separate fully paginated connection. A partial
    connection, malformed body or failed read invalidates the whole population.
    """
    issues: dict[tuple[str, int], dict] = {}
    for repo in repos:
        owner, name = repo.split("/", 1)
        for population in ("recent", "open"):
            cursor = None
            seen: set[str] = set()
            while True:
                after = ",after:" + json.dumps(cursor) if cursor else ""
                states = ",states:OPEN" if population == "open" else ""
                query = (
                    "query {repository(owner:"
                    + json.dumps(owner)
                    + ",name:"
                    + json.dumps(name)
                    + "){issues(first:100,orderBy:{field:CREATED_AT,direction:DESC}"
                    + states
                    + after
                    + "){pageInfo{hasNextPage endCursor}nodes{number createdAt state body}}}}"
                )
                ok, raw, _err = gh_fn(["api", "graphql", "-f", "query=" + query])
                if not ok:
                    return None
                try:
                    data = json.loads(raw)
                    if data.get("errors"):
                        return None
                    connection = data["data"]["repository"]["issues"]
                    crossed = False
                    for issue in connection["nodes"]:
                        stamp = int(
                            datetime.fromisoformat(
                                issue["createdAt"].replace("Z", "+00:00")
                            ).timestamp()
                        )
                        if not isinstance(issue["body"], str):
                            return None
                        in_window = since <= stamp <= now
                        if population == "recent" and stamp < since:
                            crossed = True
                            break
                        if population == "open" or in_window:
                            issues[(repo, issue["number"])] = {
                                **issue,
                                "repository": repo,
                                "in_window": in_window,
                            }
                    page = connection["pageInfo"]
                    if crossed or not page["hasNextPage"]:
                        break
                    cursor = page["endCursor"]
                    if not cursor or cursor in seen:
                        return None
                    seen.add(cursor)
                except (KeyError, TypeError, ValueError):
                    return None
    return list(issues.values())


def collect_inputs(*, now: int, gh_fn: Callable, repos: list[str], db: Path) -> dict[str, Any]:
    inputs = _brain(db, now - WINDOW_DAYS * 86400, now)
    inputs["fleet_issues"] = fleet_issues(gh_fn, now - WINDOW_DAYS * 86400, now, repos)
    if inputs["fleet_issues"] is None:
        inputs["errors"].append("fleet issue population incomplete or inaccessible")
    return inputs


def _fixture(event: dict) -> bool:
    meta = event.get("metadata") or {}
    return (
        meta.get("verdict_provenance") == "fixture_observed"
        or meta.get("evidence_provenance") == "fixture_observed"
        or meta.get("evidence_source") == "fixture_observed"
        or meta.get("evidence_weight") == 0
    )


def report(
    *,
    now: int | None = None,
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict:
    now = int(time.time()) if now is None else now
    inputs = inputs or {}
    env = os.environ if env is None else env
    ledger = capabilities.load_declared(path or capabilities.REG)
    rows = []
    for cid, cap in sorted(ledger.items()):
        if cap.get("status") in capabilities.NOT_LIVE_STATES:
            continue
        events = [
            e
            for e in cap.get("event_history", [])
            if now - WINDOW_DAYS * 86400 <= int(e.get("timestamp") or 0) <= now and not _fixture(e)
        ]
        production = [
            e
            for e in events
            if not e.get("metadata", {}).get("experiment_id")
            and not str(e.get("ref") or "").startswith("advice:")
        ]
        offers = sum(
            e.get("type") == "match"
            and e.get("metadata", {}).get("source") == "capability_advisor"
            and not e.get("metadata", {}).get("fact_missing")
            for e in events
        )
        edges = inputs.get("edges")
        own_edges = (
            None
            if edges is None
            else [e for e in edges if e.get("capability_id") == cid and not e.get("counterfactual")]
        )
        row_inputs = dict(inputs)
        if cid not in SITUATION_QUERIES and cid not in inputs.get("precondition_probes", {}):
            probes = [
                e["metadata"]
                for e in events
                if e.get("metadata", {}).get("source") == "capability_advisor"
                and e["metadata"].get("precondition_met") is not None
            ]
            if probes:
                row_inputs["precondition_probes"] = {
                    **inputs.get("precondition_probes", {}),
                    cid: probes,
                }
        counts = {
            "capability": cid,
            "situation_count": situation_count(cid, row_inputs),
            "offered": offers,
            "invocation_count": sum(e.get("type") == "invocation" for e in production),
            "usable": sum(e.get("type") == "success" for e in production),
            "acted_on": (
                None if own_edges is None else sum(e.get("accepted") == 1 for e in own_edges)
            ),
            "outcome": (
                None
                if own_edges is None
                else sum(
                    e.get("accepted") == 1
                    and e.get("outcome_verdict")
                    in {"PASS", "FAIL", "CONCERNS", "approve", "needs-fix"}
                    and e.get("durability")
                    in {"durable", "broke_later", "reverted", "reworked", "reopened"}
                    for e in own_edges
                )
            ),
        }
        dependencies = set(INPUT_FLAGS.get(cid, ())) | set(cap.get("input_flags", []))
        # A row's explicitly default-off ORCH flag is a declaration of its own dependency.
        for flag, value in cap.get("flags_defaults", {}).items():
            if flag.startswith("ORCH_") and str(value).lower() in {"0", "false"}:
                dependencies.add(flag)
        off = [
            flag
            for flag in sorted(dependencies)
            if str(env.get(flag, "unknown")).lower() in {"0", "false"}
        ]
        counts["input_off"] = ["input_off:" + flag for flag in off]
        counts["first_break"] = first_break(counts)
        rows.append(counts)
    return {
        "generated_at": now,
        "window_days": WINDOW_DAYS,
        "total": len(rows),
        "rows": rows,
        "errors": inputs.get("errors", []),
        "population": "all live ledger rows; fleet issue creations in 90 days, current open issues, persisted verifier disagreements; independent probes/rail inputs when supplied",
    }


def format_lines(section: dict) -> list[str]:
    lines = ["## Capability value chain", ""]
    for row in section["rows"]:
        demand = (
            "demand unmeasured"
            if row["situation_count"] is None
            else f"demand {row['situation_count']}"
        )
        first = row["first_break"] or "unmeasured"
        lines.append(
            f"  {row['capability']}: {demand}; invocation {row['invocation_count']}; usable {row['usable']}; "
            f"acted_on {row['acted_on']}; outcome {row['outcome']}; first_break {first}"
            + ("; " + ", ".join(row["input_off"]) if row["input_off"] else "")
        )
    lines.extend(f"  evidence UNKNOWN: {e}" for e in section.get("errors", []))
    return lines + [""]


def recurrence_fixture() -> bool:
    row = {
        "situation_count": None,
        "offered": 0,
        "invocation_count": 0,
        "usable": 0,
        "acted_on": 0,
        "outcome": 0,
    }
    assert first_break(row) is None
    row["situation_count"] = 3
    assert first_break(row) == "not_offered"
    row.update(offered=1, invocation_count=1, usable=1, acted_on=1)
    assert first_break(row) == "no_outcome"
    return True


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        recurrence_fixture()
        print("value_chain_monitor selftest PASS")
    else:
        print(json.dumps(report(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
