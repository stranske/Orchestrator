#!/usr/bin/env python3
"""Read demand independently of invocations and name the first broken value-chain step.

This is the data section of switch_review, not a second auditor or event store.
Collectors return None on absent/incomplete evidence; a measured zero requires a
complete population. Reads use existing ledger events, Brain rows and fleet issues.

The `outcome` step reads what became of an acted-on run from `outcomes` when the
report is made, never from the influence edge's cached copy, and counts a run whose
outcome is in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` in `outcome_excluded`,
never in `outcome` (see `_brain`).
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
import feedback

CAPABILITY_ID = "value-chain-monitor"
WINDOW_DAYS = 90
# A graded outcome: a verdict AND a judged durability, read from the target's `outcomes` row.
GRADED_VERDICTS = frozenset({"PASS", "FAIL", "CONCERNS", "APPROVE", "NEEDS-FIX"})
GRADED_DURABILITY = frozenset({"durable", "broke_later", "reverted", "reworked", "reopened"})
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
        measured = [
            p["precondition_met"] for p in probes if isinstance(p.get("precondition_met"), bool)
        ]
        # A supplied empty population is measured zero. Any unknown member
        # makes the population incomplete, even alongside measured results.
        if len(measured) != len(probes):
            return None
        return sum(measured)
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
            # Every acted-on run DID reach an outcome, and each is one no learner may score. Not
            # `no_outcome`: waiting will not change it, attribution has to.
            if field == "outcome" and 0 < (row.get("outcome_excluded") or 0) == row["acted_on"]:
                return "outcome_excluded"
            return broken
    return "works"


def outcome_counts(own_edges: list[dict] | None) -> dict[str, int | None]:
    """The last two value-chain steps over one capability's non-counterfactual edges.

    `outcome` is an acted-on run that reached a graded outcome no learner is barred from scoring;
    `outcome_excluded` is one whose outcome is in the one set. None when the edges are unread:
    unmeasured, never zero.
    """
    if own_edges is None:
        return {"acted_on": None, "outcome": None, "outcome_excluded": None}
    acted = [e for e in own_edges if e.get("accepted") == 1]
    # Read at call time, never copied: a class added to the one set is excluded here too.
    excluded_classes = feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    return {
        "acted_on": len(acted),
        "outcome": sum(
            e.get("failure_class") not in excluded_classes
            and str(e.get("outcome_verdict") or "").upper() in GRADED_VERDICTS
            and e.get("durability") in GRADED_DURABILITY
            for e in acted
        ),
        "outcome_excluded": sum(e.get("failure_class") in excluded_classes for e in acted),
    }


def _brain(db: Path, since: int, now: int) -> dict[str, Any]:
    """Accepted completion events and influence edges in the window, read-only.

    THE EDGE SAYS WHO, THE OUTCOME SAYS WHAT HAPPENED. An edge's `outcome_verdict`, `merged` and
    `durability` are a copy of its target's `outcomes` row, written only when propagation
    re-records the target (accepted edges only) or the triage grader grades a counterfactual
    edge once. `durability_sweep`'s truncated-recheck restore, `feedback.mark_transient_infra`
    and the data migrations write `outcomes` without refreshing it. MEASURED 2026-10-11 on a
    copy of the live Brain: role-triage's two counted outcomes were edges still reading
    PASS/durable on outcomes the 2026-10-04 relabels had cleared to `unattributed_delegation`,
    and they were the only `works` in the report. So each edge's copied columns are REPLACED by
    the outcome's, read now: the verdict adjudicated, else the verifier's (never CI alone), as
    `capability_effectiveness` and `capability_causal_evidence` read them, plus its
    `failure_class`. An edge with no `outcomes` row has no outcome.
    """
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
            edges = []
            for r in con.execute(
                "SELECT ie.*, o.adjudicated_verdict AS outcome_adjudicated, "
                "o.verifier_verdict AS outcome_verifier, o.durability AS outcome_durability, "
                "o.merged AS outcome_merged, o.failure_class AS outcome_failure_class "
                "FROM influence_edges ie "
                "LEFT JOIN outcomes o ON o.run_id = ie.target_run_id "
                "WHERE ie.created_ts BETWEEN ? AND ?",
                (since, now),
            ):
                edge = dict(r)
                adjudicated = edge.pop("outcome_adjudicated")
                verifier = edge.pop("outcome_verifier")
                edge["outcome_verdict"] = str(adjudicated or verifier or "").upper() or None
                edge["durability"] = str(edge.pop("outcome_durability") or "").lower() or None
                edge["merged"] = edge.pop("outcome_merged")
                edge["failure_class"] = edge.pop("outcome_failure_class")
                edges.append(edge)
            result["edges"] = edges
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
            # `outcome_excluded` is counted, never scored, and never hidden in a smaller `outcome`.
            **outcome_counts(own_edges),
        }
        dependencies = set(INPUT_FLAGS.get(cid, ())) | set(cap.get("input_flags", []))

        # Declared defaults can be grouped by caller (module/orchestrate/commands).
        # They identify dependencies; only an observed disabled environment value
        # below establishes input_off, so missing inputs remain unknown.
        def default_off_flags(defaults: Mapping[str, Any]) -> set[str]:
            flags: set[str] = set()
            for flag, value in defaults.items():
                if isinstance(value, Mapping):
                    flags.update(default_off_flags(value))
                elif flag.startswith("ORCH_") and str(value).lower() in {"0", "false"}:
                    flags.add(flag)
            return flags

        dependencies.update(default_off_flags(cap.get("flags_defaults", {})))
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
        "population": "all live ledger rows; fleet issue creations in 90 days, current open issues, persisted verifier disagreements; independent probes/rail inputs when supplied; acted-on edges' outcomes read from `outcomes` at report time",
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
        excluded = row.get("outcome_excluded")
        lines.append(
            f"  {row['capability']}: {demand}; invocation {row['invocation_count']}; usable {row['usable']}; "
            f"acted_on {row['acted_on']}; outcome {row['outcome']}"
            + (f" (+{excluded} excluded)" if excluded else "")
            + f"; first_break {first}"
            + ("; " + ", ".join(row["input_off"]) if row["input_off"] else "")
        )
    if any(row.get("outcome_excluded") for row in section["rows"]):
        lines.append(
            "  `excluded`: acted-on runs whose outcome is in "
            "feedback.LEARNING_EXCLUDED_FAILURE_CLASSES; counted, never scored."
        )
    lines.extend(f"  evidence UNKNOWN: {e}" for e in section.get("errors", []))
    return lines + [""]


def recurrence_fixture() -> bool:
    assert (
        situation_count(
            "generic-probe",
            {
                "precondition_probes": {
                    "generic-probe": [
                        {"precondition_met": False},
                        {"precondition_met": None},
                    ]
                }
            },
        )
        is None
    )
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
    # A durable PASS whose class no learner may score is counted beside `outcome`, never in it.
    edge = {"accepted": 1, "outcome_verdict": "PASS", "durability": "durable"}
    for failure_class in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
        counts = outcome_counts([{**edge, "failure_class": failure_class}])
        assert counts == {"acted_on": 1, "outcome": 0, "outcome_excluded": 1}, counts
        row.update(counts)
        assert first_break(row) == "outcome_excluded"
    assert outcome_counts([edge]) == {"acted_on": 1, "outcome": 1, "outcome_excluded": 0}
    assert outcome_counts(None) == {"acted_on": None, "outcome": None, "outcome_excluded": None}
    row.update(acted_on=2)  # one acted-on run still has no graded outcome of its own
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
