#!/usr/bin/env python3
"""Read demand independently of invocations and name the first broken value-chain step.

This is the data section of switch_review, not a second auditor or event store.
Collectors return None on absent/incomplete evidence; a measured zero requires a
complete population. Reads use existing ledger events, Brain rows and fleet issues.

The `outcome` step reads what became of an acted-on run from `outcomes` when the
report is made, never from the influence edge's cached copy (see `_brain`), and
classifies it as `capability_effectiveness` does, with `feedback`'s predicates. An
edge onto a role run is not acted on; it is counted beside `acted_on`. Only a terminal
outcome is the `outcome` step, and a learning-excluded one, one awaiting the sweep, one that never
merges and an open one are counted beside it. A run that never merges is scored by verdict in
`capability_effectiveness` (owner decision 2026-10-12), never at this merges-only step. `works` needs a durable
success (`outcome_success`). Relay-pacing edges in windowed-capacity-policy count the
observed lane-round terminal result; successful rounds require exit=0. Those operational
results sit beside durable-delivery successes on non-relay edges in the same total and
never synthesize a durable-delivery claim from a lane exit.
The tick writes the readable section to
`switch-review.md`, the file a capability verdict cites (CLAUDE.md §6).
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
import capability_effectiveness
import feedback

CAPABILITY_ID = "value-chain-monitor"
WINDOW_DAYS = 90
# What an accepted edge's target can say, as `capability_effectiveness._classify` names it, and the
# row field each class is counted in. An association is set aside at the `acted_on` step (2026-10-12,
# owner decision): an edge onto a role run is not a run acting on the capability's output, because
# the run that took the role's advice carries the capability's own edge. Every other class is an
# acted-on run, and `terminal` is the `outcome` step itself. An edge onto an abcd experiment MEMBER
# run is set aside at the same step (2026-10-12): the member is the experiment's own arm, not a run
# acting on its result. A winner is acted on through its synthesis run, and no experiment's has been.
CLASS_FIELDS = {
    "association": "acted_on_associations",
    "no_outcome_path": "acted_on_member_runs",
    "terminal": "outcome",
    "excluded": "outcome_excluded",
    "awaiting_sweep": "outcome_awaiting_sweep",
    "no_merge": "outcome_no_merge",
    "open": "outcome_open",
}
# The acted-on runs, partitioned by what became of them.
ACTED_ON_SET_ASIDE = ("association", "no_outcome_path")
OUTCOME_SPLIT = tuple(f for c, f in CLASS_FIELDS.items() if c not in ACTED_ON_SET_ASIDE)
# Printed beside a step: (step, row field, label, legend). Labels as `capability_effectiveness` prints.
SET_ASIDE = (
    (
        "acted_on",
        "acted_on_associations",
        "assoc",
        "edges onto role runs, whose outcome is a copy of the acting run's; never counted as "
        "acted on.",
    ),
    (
        "acted_on",
        "acted_on_member_runs",
        "member",
        "edges onto experiment member runs, which never push; a winner is acted on through its "
        "synthesis run. Never counted as acted on.",
    ),
    (
        "outcome",
        "outcome_excluded",
        "excl",
        "acted-on runs whose outcome is in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES; "
        "counted, never scored.",
    ),
    (
        "outcome",
        "outcome_awaiting_sweep",
        "sweep",
        "merges the durability sweep has not judged yet; they resolve, so they wait.",
    ),
    (
        "outcome",
        "outcome_no_merge",
        "nomerge",
        "runs that never merge; scored by capability_effectiveness's no-merge verdict rate, "
        "never at this step.",
    ),
    ("outcome", "outcome_open", "open", "acted-on runs with no outcome recorded yet."),
)
# Explicit dependence, not a guess from a capability name or lack of heartbeats.
INPUT_FLAGS = {
    "role-triage": ("ORCH_DISPATCH_LANE", "ORCH_TRIAGE_CONSUME"),
    "issue-readiness": ("ORCH_DISPATCH_LANE",),
    # The other judgment roles' declared consumers (dispatcher.plan_dispatch, epic_lane's
    # dispatch prompts, the redirect rails) run only inside the tick's dispatch lane, shadowed
    # since 2026-09-03. Until 2026-10-17 nothing printed that, so their rows read not_acted_on or
    # unmeasured instead of input_off, and the 2026-09-03 switch flip surfaced nowhere.
    # `tests/test_lane_moment_bindings.py` pins the rule: every role except the adjudicator,
    # whose consumer is the closer lane, names its switch here.
    "role-prompt": ("ORCH_DISPATCH_LANE",),
    "role-decomposer": ("ORCH_DISPATCH_LANE",),
    "role-redirect": ("ORCH_REDIRECT_APPLY_BOOTSTRAP",),
    "redirect-plan": ("ORCH_DISPATCH_LANE",),
    "redirect-policy": ("ORCH_DISPATCH_LANE",),
}
STALL_SWEEP_LOG = "redirect-sweep.log"


def _state_dir() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex" / "orchestrator")))


def _stall_sweep_targets(log: Path, start: int, end: int) -> set[str] | None:
    """Distinct targets the redirect sweep found actionable in [start, end]; None when unread.

    The sweep appends its whole JSON report to this log every hour, so the log is the one record
    of the stall population independent of whether any role ran: 1,844 sweeps in the 90 days to
    2026-10-17, 40 with a watched target, 6 with an actionable one (9 actionable in all).
    """
    if not log.exists():
        return None
    decoder = json.JSONDecoder()
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pos, targets = 0, set()
    while True:
        brace = text.find("{", pos)
        if brace < 0:
            break
        try:
            obj, stop = decoder.raw_decode(text, brace)
        except ValueError:
            pos = brace + 1
            continue
        # `raw_decode` returns the ABSOLUTE end index. Adding it to `brace` skipped most of the
        # log: the first production read decoded 10 of 2,319 sweeps and printed role-redirect's
        # demand as 0 (`no_situation`) against 9 actionable stalls in the window (2026-10-17).
        pos = stop  # never `end`: that name is the window's end, and shadowing it emptied every window
        if not isinstance(obj, dict):
            continue
        ts = obj.get("generated_at")
        if not isinstance(ts, (int, float)) or not start <= ts <= end:
            continue
        for report in obj.get("actionable") or []:
            if isinstance(report, dict) and report.get("target"):
                targets.add(str(report["target"]))
            elif isinstance(report, str) and report:
                targets.add(report)
    return targets


def _stall_situations(inputs: dict[str, Any]) -> int | None:
    targets = inputs.get("stall_targets")
    return None if targets is None else len(targets)


def _relay_situations(inputs: dict[str, Any]) -> int | None:
    return inputs.get("relay_decision_count")


def _relay_decision_population(log: Path, start: int, end: int) -> int | None:
    """Independent demand: valid distinct log decisions, including unjoined rounds."""
    try:
        if not log.is_file():
            return None
        decisions = feedback.read_relay_pacing_decisions(log, end=end)
    except (OSError, UnicodeError):
        return None
    return sum(start <= stamp <= end for stamp, _ in decisions.values())


def _relay_usable(inputs: dict[str, Any], start: int, end: int) -> int | None:
    """Accepted decision events prove usability even after the source log rotates."""
    events = inputs.get("completion_events")
    if events is None:
        return None
    return len(
        {
            e["run_id"]
            for e in events
            if e.get("capability_id") == feedback.RELAY_PACING_CAPABILITY
            and e.get("capability_version_id") == feedback.RELAY_PACING_VERSION
            and e.get("producer") == "feedback.relay_pacing"
            and e.get("phase") == "decision"
            and e.get("validation_status") == "accepted"
            and start <= e.get("created_ts", -1) <= end
        }
    )


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
    # The adjudicator's situation is the persisted verifier disputes in the window, counted from
    # the Brain (`_dispute_count`), never from events its own selector wrote: a selector that does
    # not fire would make the row read "no situation" however many disputes exist. None is unread,
    # 0 is a readable Brain holding none.
    return inputs.get("disputes")


# Callables can be replaced/extended without changing reporting or adding telemetry.
SITUATION_QUERIES: dict[str, Callable[[dict[str, Any]], int | None]] = {
    "role-prompt": _prompt_batches,
    "role-decomposer": _large_issues,
    "role-triage": _open_issues,
    "role-adjudicator": _disputes,
    "role-redirect": _stall_situations,
    feedback.RELAY_PACING_CAPABILITY: _relay_situations,
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
        # A terminal outcome is not yet value: `works` needs a durable success (2026-10-12, owner
        # decision). Three capabilities held one terminal outcome and no success each.
        ("outcome_success", "no_success"),
    ):
        if row.get(field) is None:
            return None
        if row[field] == 0:
            if field == "offered" and row.get("invocation_count"):
                continue
            if field == "outcome":
                return _no_outcome_break(row)
            return broken
    return "works"


def _no_outcome_break(row: Mapping[str, Any]) -> str:
    """Which break an acted-on capability with no terminal outcome has: waiting, or never.

    `no_outcome` only while something can still resolve: an acted-on run with no outcome yet, or a
    merge the durability sweep has not judged. Otherwise nothing here can ever be scored, and one
    sentinel for both would read a dead end as patience. `outcome_excluded`: every acted-on run
    that is evidence resolved into a class no learner may score; attribution, not time, would
    change it. `outcome_unscoreable`: the rest (runs that never merge, a mix with
    excluded ones). The same split `capability_effectiveness` reports as
    `excluded_only` and `unscoreable`. A row that does not carry the split stays `no_outcome`.
    """
    split = [row.get(f) for f in OUTCOME_SPLIT]
    if not all(isinstance(n, int) for n in split):
        return "no_outcome"
    if row["outcome_open"] or row["outcome_awaiting_sweep"]:
        return "no_outcome"
    excluded = row["outcome_excluded"]
    return "outcome_excluded" if 0 < excluded == row["acted_on"] else "outcome_unscoreable"


def outcome_counts(cid: str, own_edges: list[dict] | None) -> dict[str, int | None]:
    """The last three value-chain steps over one capability's non-counterfactual edges.

    Each accepted edge is classified by `capability_effectiveness._classify`, the one classifier of
    these edges, from `feedback`'s predicates (`role_target_kind`, `LEARNING_EXCLUDED_FAILURE_CLASSES`
    and `outcome_is_terminal`, all read at call time). An edge onto a role run is an association,
    set aside beside `acted_on`. `outcome` counts acted-on runs whose outcome is TERMINAL, good or
    bad, and `outcome_success` those of them that are a durable success
    (`feedback.outcome_is_durable_success`, the lifecycle's predicate). Every class has its own
    field, so nothing set aside is hidden in a smaller count. The capacity row also counts
    version-exact relay round completions (including failure), and completed exit=0 round
    successes, without inventing PR outcomes.
    None when the edges are unread:
    unmeasured, never zero.
    """
    fields = ("acted_on", *CLASS_FIELDS.values(), "outcome_success")
    if own_edges is None:
        return {field: None for field in fields}
    accepted = [e for e in own_edges if e.get("accepted") == 1]
    relay = [
        e
        for e in accepted
        if cid == feedback.RELAY_PACING_CAPABILITY
        and e.get("capability_version_id") == feedback.RELAY_PACING_VERSION
        and e.get("relay_round_status") in {"completed", "failed", "rate-limited"}
    ]
    accepted_delivery = [e for e in accepted if e not in relay]
    classified = [
        (
            capability_effectiveness._classify(
                {
                    **e,
                    "role_kind": feedback.role_target_kind(cid, e.get("target_role_name")),
                    "experiment_member": feedback.experiment_member_target(
                        e.get("target_routing_metadata")
                    ),
                    "delivery_eligibility": capability_effectiveness._delivery_eligibility(
                        e.get("target_mode"), e.get("target_pr_number"), e.get("merged")
                    ),
                }
            ),
            e,
        )
        for e in accepted_delivery
    ]
    classes = Counter(c for c, _ in classified)
    return {
        "acted_on": len(accepted) - sum(classes[c] for c in ACTED_ON_SET_ASIDE),
        **{
            field: classes[c] + (len(relay) if c == "terminal" else 0)
            for c, field in CLASS_FIELDS.items()
        },
        "outcome_success": sum(
            e.get("relay_round_status") == "completed" and e.get("relay_round_exit_status") == 0
            for e in relay
        )
        + sum(
            c == "terminal"
            and feedback.outcome_is_durable_success(**capability_effectiveness._outcome_fields(e))
            for c, e in classified
        ),
    }


def own_edges_of(edges: list[dict] | None, cid: str) -> list[dict] | None:
    """One capability's non-counterfactual edges; None when the edges were never read."""
    if edges is None:
        return None
    return [e for e in edges if e.get("capability_id") == cid and not e.get("counterfactual")]


def acted_on_index(db: Path, now: int, capability_ids) -> tuple[dict[str, int] | None, str]:
    """`acted_on` per capability over the window and edges the weekly report prints.

    ONE computation, consumed by `capability_admission.req_consumer_acts` (the tenth part): the
    gate and the printed row read the same Brain, the same window and the same classifier, so
    they cannot disagree about whether a declared consumer ever acted. None with the reason when
    the Brain is unreadable — unmeasured, never zero.
    """
    inputs = _brain(db, now - WINDOW_DAYS * 86400, now)
    edges = inputs.get("edges")
    if edges is None:
        return None, (inputs.get("errors") or ["Brain evidence unavailable"])[0]
    index = {
        cid: int(outcome_counts(cid, own_edges_of(edges, cid))["acted_on"] or 0)
        for cid in capability_ids
    }
    return index, f"value-chain monitor, {WINDOW_DAYS}d window, {db.name}"


def acted_on_window_index(
    db: Path, windows: dict[str, tuple[int, int]]
) -> tuple[dict[str, int] | None, str]:
    """Count the monitor's accepted, non-counterfactual acts inside fixed per-capability windows.

    Admission deadlines are relative to first invocation, so a rolling report window cannot
    establish them. Reuse the monitor's edge selection and classifier without expiring timely
    evidence or crediting acts after the deadline.
    """
    if not windows:
        return {}, "value-chain monitor, no invoked capabilities"
    inputs = _brain(
        db, min(start for start, _ in windows.values()), max(end for _, end in windows.values())
    )
    edges = inputs.get("edges")
    if edges is None:
        return None, (inputs.get("errors") or ["Brain evidence unavailable"])[0]
    index = {}
    for cid, (start, end) in windows.items():
        own = own_edges_of(edges, cid)
        bounded = [e for e in (own or []) if start <= int(e.get("created_ts") or 0) <= end]
        index[cid] = int(outcome_counts(cid, bounded)["acted_on"] or 0)
    return index, "value-chain monitor, first-invocation deadline windows"


def supervisor_closure_acceptance(
    enabled_at: int, *, now: int | None = None, db: Path | None = None
) -> dict[str, Any]:
    """Read closure credit within 24 hours of an operator-supplied enablement time.

    This does not enable the actor or write evidence. A rolling monitor count alone
    cannot establish the deadline: both the action edge and its recorded outcome
    must fall inside the enablement window. Unread evidence remains unmeasured.
    """
    now = int(time.time()) if now is None else now
    if enabled_at <= 0 or enabled_at > now:
        raise ValueError("enablement time must be positive and no later than now")
    deadline = enabled_at + 86400
    end = min(now, deadline)
    inputs = _brain(db or feedback.DB_PATH, enabled_at, end)
    return _supervisor_closure_result(enabled_at, now, inputs)


def _supervisor_closure_result(enabled_at: int, now: int, inputs: dict) -> dict[str, Any]:
    deadline = enabled_at + 86400
    end = min(now, deadline)
    cid = "live-keepalive-supervisor"
    own = own_edges_of(inputs["edges"], cid)
    bounded = (
        None
        if own is None
        else [
            edge
            for edge in own
            if str(edge.get("target_run_id") or "").startswith("supervisor-close:")
            and enabled_at <= (edge.get("outcome_recorded_ts") or 0) <= end
        ]
    )
    count = outcome_counts(cid, bounded)["outcome"]
    if count is None:
        status = "unmeasured"
    elif count >= 1:
        status = "met"
    else:
        status = "pending" if now < deadline else "deadline_missed"
    return {
        "capability": cid,
        "enabled_at": enabled_at,
        "deadline": deadline,
        "generated_at": now,
        "outcome": count,
        "status": status,
        "errors": inputs["errors"],
    }


def _dispute_count(db: Path, now: int) -> int | None:
    """Persisted verifier disputes in the window, from the one definition of a dispute.

    `adjudicator_retro.disputes` opens the Brain read-only, so this never creates the database
    or initialises a schema. None when the read fails: unknown, not zero.
    """
    import adjudicator_retro  # imported here, as switch_review does, to avoid an import cycle

    try:
        return len(adjudicator_retro.disputes(db=db, now=now, days=WINDOW_DAYS))
    except (sqlite3.Error, OSError):
        return None


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
    the outcome's, read now, and the raw fields `feedback`'s predicates read are added beside
    them: both verdicts, `merged`, `failure_class`, and the target's `runs.role_name`. An edge
    with no `outcomes` row has no outcome.
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
                "o.merged AS outcome_merged, o.failure_class AS outcome_failure_class, "
                "o.durability_checked_ts AS outcome_recorded_ts, "
                "r.role_name AS target_role_name, r.mode AS target_mode, "
                "r.pr_number AS target_pr_number, "
                "r.routing_metadata AS target_routing_metadata, "
                "CASE WHEN ce.producer='feedback.relay_pacing' "
                "AND ce.validation_status='accepted' AND ce.created_ts<=? "
                "AND ce.run_id=ie.target_run_id "
                "AND ce.capability_id=ie.capability_id "
                "AND ce.capability_version_id=ie.capability_version_id THEN "
                "json_extract(ce.payload_json,'$.relay_pacing.status') END AS relay_round_status, "
                "json_extract(ce.payload_json,'$.relay_pacing.exit_status') AS relay_round_exit_status "
                "FROM influence_edges ie "
                "LEFT JOIN outcomes o ON o.run_id = ie.target_run_id "
                "LEFT JOIN runs r ON r.run_id = ie.target_run_id "
                "LEFT JOIN completion_events ce ON ce.event_id = ie.target_event_id "
                "WHERE ie.created_ts BETWEEN ? AND ?",
                (now, since, now),
            ):
                edge = dict(r)
                adjudicated = edge["adjudicated_verdict"] = edge.pop("outcome_adjudicated")
                verifier = edge["verifier_verdict"] = edge.pop("outcome_verifier")
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


def collect_inputs(
    *,
    now: int,
    gh_fn: Callable,
    repos: list[str],
    db: Path,
    stall_log: Path | None = None,
    relay_log: Path | None = None,
) -> dict[str, Any]:
    inputs = _brain(db, now - WINDOW_DAYS * 86400, now)
    inputs["relay_decision_count"] = _relay_decision_population(
        relay_log
        or Path(
            os.environ.get(
                "HANDOFF_PACING_LOG", str(Path.home() / ".codex/handoff/relay-pacing.log")
            )
        ),
        now - WINDOW_DAYS * 86400,
        now,
    )
    if inputs["relay_decision_count"] is None:
        inputs["errors"].append("relay pacing population unavailable (decision log unread)")
    inputs["stall_targets"] = _stall_sweep_targets(
        stall_log or _state_dir() / STALL_SWEEP_LOG, now - WINDOW_DAYS * 86400, now
    )
    if inputs["stall_targets"] is None:
        inputs["errors"].append("stall population unavailable (redirect sweep log unread)")
    inputs["disputes"] = _dispute_count(db, now)
    if inputs["disputes"] is None:
        inputs["errors"].append("dispute population unavailable")
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
        own_edges = own_edges_of(inputs.get("edges"), cid)
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
            # Every class set aside from `outcome` has its own count beside it.
            **outcome_counts(cid, own_edges),
        }
        if cid == feedback.RELAY_PACING_CAPABILITY:
            usable = _relay_usable(inputs, now - WINDOW_DAYS * 86400, now)
            counts["usable"] = None if usable is None else counts["usable"] + usable
            # Relay exit=0 and durable-delivery edges both land in outcome_success;
            # the scope must name both so a mixed row is not misread as exit=0 alone.
            counts["success_scope"] = (
                "completed lane rounds (exit=0) and durable-delivery successes"
            )
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
        success_scope = f" ({row['success_scope']})" if row.get("success_scope") else ""

        def aside(step: str) -> str:
            parts = [
                f"+{row[f]} {label}" for s, f, label, _ in SET_ASIDE if s == step and row.get(f)
            ]
            return f" ({', '.join(parts)})" if parts else ""

        lines.append(
            f"  {row['capability']}: {demand}; invocation {row['invocation_count']}; usable {row['usable']}; "
            f"acted_on {row['acted_on']}{aside('acted_on')}; "
            f"outcome {row['outcome']}{aside('outcome')}; success {row.get('outcome_success')}{success_scope}; "
            f"first_break {first}"
            + ("; " + ", ".join(row["input_off"]) if row["input_off"] else "")
        )
    # One legend line per label that appears, so a count is never printed without its meaning.
    lines.extend(
        f"  `{label}`: {meaning}"
        for _, f, label, meaning in SET_ASIDE
        if any(row.get(f) for row in section["rows"])
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
    # Each class an accepted edge can be in has its own field; only a terminal one is the step,
    # and only a durable success among them makes the chain `works`.
    done = {"accepted": 1, "adjudicated_verdict": "PASS", "merged": 1, "durability": "durable"}
    row.update(outcome_counts("cap", [done]))
    assert (row["outcome"], row["outcome_success"], first_break(row)) == (1, 1, "works"), row
    row.update(outcome_counts("cap", [{**done, "adjudicated_verdict": "FAIL"}]))
    assert (row["outcome"], row["outcome_success"], first_break(row)) == (1, 0, "no_success"), row
    for failure_class in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
        counts = outcome_counts("cap", [{**done, "failure_class": failure_class}])
        assert (counts["outcome"], counts["outcome_excluded"]) == (0, 1), counts
        row.update(counts)
        assert first_break(row) == "outcome_excluded"
    # An edge onto a role run is an association, whatever its target holds: never acted on.
    counts = outcome_counts("cap", [{**done, "target_role_name": "triage"}])
    assert (counts["acted_on"], counts["acted_on_associations"], counts["outcome"]) == (0, 1, 0)
    row.update(counts)
    assert first_break(row) == "not_acted_on"
    # An edge onto an experiment member run has no outcome path: never acted on, whatever it holds.
    _member = '{"experiment_identity_version": 2, "experiment_member_id": "a--m"}'
    counts = outcome_counts("cap", [{**done, "target_routing_metadata": _member}])
    assert (counts["acted_on"], counts["acted_on_member_runs"], counts["outcome"]) == (0, 1, 0)
    row.update(counts)
    assert first_break(row) == "not_acted_on"
    row.update(outcome_counts("cap", [{"accepted": 1}]))  # acted on, nothing recorded yet
    assert first_break(row) == "no_outcome"
    assert all(v is None for v in outcome_counts("cap", None).values())
    # Exercise the same deadline/prefix reducer as the read-only Brain API.
    enabled = 100
    deadline = enabled + 86400
    closure = {
        **done,
        "capability_id": "live-keepalive-supervisor",
        "target_run_id": "supervisor-close:fixture",
        "outcome_recorded_ts": deadline,
    }
    inputs = {"edges": [closure], "errors": []}
    assert _supervisor_closure_result(enabled, deadline, inputs)["status"] == "met"
    assert _supervisor_closure_result(enabled, deadline + 1, inputs)["status"] == "met"
    for excluded in (
        {**closure, "target_run_id": "ordinary:fixture"},
        {**closure, "outcome_recorded_ts": enabled - 1},
        {**closure, "outcome_recorded_ts": deadline + 1},
    ):
        inputs = {"edges": [excluded], "errors": []}
        assert _supervisor_closure_result(enabled, deadline - 1, inputs)["status"] == "pending"
        assert _supervisor_closure_result(enabled, deadline, inputs)["status"] == "deadline_missed"
    unknown = _supervisor_closure_result(enabled, deadline, {"edges": None, "errors": ["unread"]})
    assert unknown["status"] == "unmeasured" and unknown["errors"] == ["unread"]
    return True


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument(
        "--supervisor-enabled-at",
        type=int,
        help="read-only closure acceptance check; enablement time as Unix seconds",
    )
    args = ap.parse_args(argv)
    if args.supervisor_enabled_at is not None:
        if args.selftest:
            ap.error("--selftest cannot be combined with --supervisor-enabled-at")
        try:
            acceptance = supervisor_closure_acceptance(args.supervisor_enabled_at)
        except ValueError as exc:
            ap.error(str(exc))
        print(json.dumps(acceptance, indent=2))
        return 0 if acceptance["status"] == "met" else 1
    if args.selftest:
        recurrence_fixture()
        print("value_chain_monitor selftest PASS")
    else:
        print(json.dumps(report(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
