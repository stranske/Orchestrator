#!/usr/bin/env python3
"""capability_effectiveness.py — is using a capability actually making outcomes better?

The measurable question, at last. Everything upstream established that capabilities RUN
(heartbeats) and that their work is ATTRIBUTED (capability-tagged influence edges). This asks the
only question that justifies any of it: when a capability influenced a run, did the run turn out
better than when it did not?

WHY NOT "GATES LIFTING". Gate promotion is unreachable as a metric: of 14 gated capabilities, 3
carry an encoded threshold with a `requires` clause naming an observation the causal record cannot
supply (a rejected counterfactual, a role disagreement, an exploration-review recommendation), and
11 state their threshold as prose no code can check. `ready_to_lift` therefore stays 0 no matter how
good the evidence is. Effectiveness has to be measured directly.

THE MEASURE. `influence_edges` carries, per (capability, run): `accepted` (the capability's
contribution was taken up) and `counterfactual` (it was considered and rejected — the natural control
arm). What became of the run is read from `outcomes`, its authoritative table, when the report is
made. So:

    durable_rate(accepted)  vs  durable_rate(counterfactual)

is a real within-capability comparison rather than a cross-agent guess.

THE EDGE SAYS WHO, THE OUTCOME SAYS WHAT HAPPENED. An edge also stores `outcome_verdict` and
`durability`, but only as a copy of the target's `outcomes` row taken when it was last propagated,
and nothing keeps that copy current. This reader therefore takes the verdict and durability from
`outcomes`, as `feedback.capability_causal_evidence` already does for the same edges.

AN OUTCOME NO LEARNER MAY SCORE IS NOT EVIDENCE HERE EITHER. A target whose `failure_class` is in
`feedback.LEARNING_EXCLUDED_FAILURE_CLASSES`, the ONE set every learner excludes (CLAUDE.md §2), says
nothing about the capability in either arm. It is counted in `excluded`, beside `terminal`, and
never scored.

WHAT COUNTS AS TERMINAL AND AS A DURABLE SUCCESS IS `feedback`'s, NOT THIS MODULE'S.
`feedback.outcome_is_terminal` and `feedback.outcome_is_durable_success` are the predicates
`capability_causal_evidence` tallies the lifecycle from, so the two readers of these edges cannot
disagree about what an outcome says. A merge the durability sweep has not judged yet is counted in
`awaiting_sweep`, and an unmerged run whose durability nothing will ever judge in `unresolvable`.
An edge onto a ROLE run is an association (`feedback.role_target_kind`), counted in
`associations` and never scored: the outcome a role run holds is a copy of its acting run's, and
the acting run carries the capability's own edge. An edge onto an abcd experiment MEMBER run
(`feedback.experiment_member_target`) is `no_outcome_path`, counted and never scored: members never
push, are judged in `evaluations_v2`, and a winner's delivery is credited to the synthesis run.

REFUSES TO MISLEAD. A rate computed from one or two observations is noise wearing a percentage sign,
and this subsystem has already produced several confident numbers that were artifacts. So every rate
carries its denominator, anything under MIN_SAMPLE reports `insufficient_evidence` instead of a
figure, and a capability with no terminal outcomes is reported as "not yet measurable" — never as 0%.
One whose only resolved outcomes are learning-excluded is `excluded_only`, a different state: waiting
will not change it, attribution has to. One whose edges can none of them ever be scored
(associations, member runs, unresolvable outcomes, excluded ones) is `unscoreable`, and never reads as awaiting.

    python3 capability_effectiveness.py            # human-readable
    python3 capability_effectiveness.py --json
    python3 capability_effectiveness.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import capabilities
import feedback

# Below this many terminal outcomes, report the count and refuse the rate.
MIN_SAMPLE = 5


def _edge_rows(conn=None) -> list[dict]:
    close = conn is None
    c = conn or feedback._conn()
    try:
        # `target_event_id IS NOT NULL` is the SAME GUARD `capability_causal_evidence` already
        # applies (`feedback.py:1430`: consumed requires accepted AND target_event_id AND an
        # accepted validation status). An edge with no completion-event envelope can never be
        # causally consumed, so treating it as measurable evidence here made two consumers of one
        # table disagree about what counts.
        #
        # THE HARM, MEASURED 2026-08-21. 296 edges carry a NULL target_event_id — advisory role runs
        # (`role:triage:*`, `role:redirect:*`) are written to `runs` but never emit a completion
        # envelope, so `_latest_completion_event_id` returns None and the edge is inert by design.
        # Five of them carry `accepted=1, verdict=PASS, durability=durable`: the links created BY
        # HAND with the link-outcome CLI during the Stage-2 deadlock. They were 5 of `offload`'s 5
        # durable outcomes — i.e. its ENTIRE durable signal was hand-made and envelope-less, giving
        # `durable_rate: 0.059` off no causally-linked evidence at all. Nothing consumed that yet
        # only because the verdict was still `no_control_arm`; the moment offload gains a
        # counterfactual arm it becomes a lift computation. Fixed before that, not after.
        #
        # Filtered in SQL rather than in `_arm_stats` so `attributed` (the numerator's denominator)
        # excludes them too — counting an edge as attributed while refusing it as terminal would
        # report a capability as busier than its measurable evidence supports.
        #
        # THE VERDICT AND DURABILITY COME FROM `outcomes`, NOT FROM THE EDGE. The edge's own two
        # columns are written only from the target's `outcomes` row, and only at two moments:
        # `_propagate_outcome_lineage_in_conn` when something re-records the target, and
        # `outcomes.backfill_triage_disagreements`, which grades a counterfactual edge once
        # (`WHERE outcome_verdict IS NULL`). A change to the outcome made any other way never
        # reaches the edge, and several writers make it another way: `durability_sweep`'s
        # truncated-recheck restore, `feedback.mark_transient_infra` (the edge has no class column
        # at all), and the data migrations. MEASURED 2026-10-11 on a copy of the live Brain: the
        # three 2026-10-04 relabels moved 53 outcomes into learning-excluded classes and refreshed
        # none of the 54 accepted capability edges on them. 2 of those still read PASS/durable for
        # role-triage and 2 for offload, on outcomes that now carry no verdict, and those 2 were
        # ALL of role-triage's durable subjects. No edge carried a terminal value without an
        # `outcomes` row, so reading the row loses nothing the edge had. The verdict is the
        # adjudicated one, else the verifier's: the reading `capability_causal_evidence` makes,
        # never CI alone.
        #
        # `runs.role_name` says whether the target is a ROLE run, the join
        # `capability_causal_evidence` makes for its `advisory_self` class. MEASURED 2026-10-11 on a
        # copy of the live Brain: 2,711 of the 3,653 edges read here target role runs. 27 of them had
        # an outcome, every one a propagated copy ("automatically influenced <run>") of an acting
        # run that carries the same capability's edge, so each delivery counted twice, and
        # `_target_of` pooled every `role:triage:*` run into one subject `triage`.
        rows = c.execute("""SELECT ie.capability_id, ie.accepted, ie.counterfactual,
                      o.adjudicated_verdict, o.verifier_verdict, o.durability, o.failure_class,
                      ie.target_run_id, ie.created_ts, o.merged, r.role_name,
                      r.target, r.routing_metadata
               FROM influence_edges ie
               LEFT JOIN outcomes o ON o.run_id = ie.target_run_id
               LEFT JOIN runs r ON r.run_id = ie.target_run_id
               WHERE ie.capability_id IS NOT NULL AND ie.target_event_id IS NOT NULL""").fetchall()
    finally:
        if close:
            c.close()
    return [
        {
            "capability_id": r[0],
            "accepted": bool(r[1]),
            "counterfactual": bool(r[2]),
            "verdict": str(r[3] or r[4] or "").upper() or None,
            "adjudicated_verdict": r[3],
            "verifier_verdict": r[4],
            "durability": str(r[5] or "").lower() or None,
            "failure_class": r[6],
            "run_id": r[7],
            "ts": r[8],
            "merged": r[9],
            "role_kind": feedback.role_target_kind(str(r[0]), r[10]),
            "subject": feedback.run_subject_id(r[12], r[11], r[7]),
            "experiment_member": feedback.experiment_member_target(r[12]),
        }
        for r in rows
    ]


def _outcome_fields(e: dict) -> dict:
    """The four `outcomes` fields `feedback`'s predicates read. A hand-built row may carry only the
    combined `verdict`; that is the adjudicated one."""
    return {
        "verifier_verdict": e.get("verifier_verdict"),
        "adjudicated_verdict": e.get("adjudicated_verdict", e.get("verdict")),
        "merged": e.get("merged"),
        "durability": e.get("durability"),
    }


def _classify(e: dict) -> str:
    """Exactly one of: association, no_outcome_path, excluded, terminal, awaiting_sweep,
    unresolvable, open.

    THE ORDER IS THE RULE. An association is never evidence whatever its target holds, and an
    excluded class is never scored whatever its verdict, so both come before `terminal`.

    AWAITING THE SWEEP vs UNRESOLVABLE (2026-10-11). Both are a durability still `pending` with no
    failure verdict, and they are opposites. `durability_sweep._pending_merged_runs` selects exactly
    `merged=1 AND durability pending`, judges each within its grace window, and printed
    `undrainable 0` on its 2026-10-11 run, so a merged one is `awaiting_sweep` and will resolve. An
    unmerged one is selected by nothing: on a copy of the live Brain those were 38 PASS outcomes of
    agy-runtime-isolation, all `offload:*` research-program runs that never open a PR. Counted as
    terminal they were 38 non-durable results that could never become anything else; counted as
    open they would read as awaiting forever. They are `unresolvable`: counted, never scored.

    NO OUTCOME PATH (2026-10-12). An abcd experiment member run never pushes, so nothing writes its
    `outcomes` row and it can only ever read as awaiting: abcd-experiment's 12 edges did, for six
    weeks. It comes before `terminal` for the reason an association does: a row another writer put
    on a member run is not the member's delivery, and crediting a non-winning arm is the error."""
    if e.get("role_kind"):
        return "association"
    if e.get("experiment_member"):
        return "no_outcome_path"
    if e.get("failure_class") in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
        return "excluded"
    outcome = _outcome_fields(e)
    if feedback.outcome_is_terminal(**outcome):
        return "terminal"
    if outcome["merged"]:
        return "awaiting_sweep"
    if any(v is not None for v in outcome.values()):
        return "unresolvable"
    return "open"


def _target_of(run_id: str) -> str:
    """A subject parsed from a run id, e.g. `remote:stranske/Workflows#2819:cursor` -> the issue.

    Only for a hand-built row with no `subject`. Every row `_edge_rows` returns carries the
    lifecycle's subject (`feedback.run_subject_id`): parsing the id never handled a local run id
    (`stranske__Repo_20-gemini-<ns>`), so each attempt at one issue was its own subject. MEASURED
    2026-10-12 on a copy of the live Brain: that held up agy-runtime-isolation's 0.8, whose four
    durable subjects were three issues, one attempted twice.
    """
    parts = str(run_id or "").split(":")
    return parts[1] if len(parts) > 2 else str(run_id or "")


def _subject(e: dict) -> str:
    """Run ids are per attempt; the subject is what repeats, so N retries of one stuck issue
    count once."""
    return e.get("subject") or _target_of(e["run_id"])


def _arm_stats(edges: list[dict]) -> dict:
    """Terminal-outcome counts and a durable rate, or a refusal when the sample is too small.

    COUNTS DISTINCT SUBJECTS, NOT ATTEMPTS. Repeated attempts on the same target are correlated, not
    independent evidence — the same rule the learning store already applies to research subjects. It
    matters concretely: on 2026-08-18 role-triage's first measurable sample was 77 terminal outcomes
    of which 51 were retries of just TWO stuck targets (`Workflows#2819` x33, `#2710` x18) left over
    from the scoped-blocker latch. Counting attempts gave a confident 0% durable rate that was really
    "the fleet kept retrying two blocked issues" — precisely the environment-noise-as-incapability
    error the learning rules forbid. Deduplicating by subject turns that into an honest
    `insufficient_evidence`.

    A LEARNING-EXCLUDED OUTCOME IS COUNTED, NEVER SCORED. Its class says the environment killed the
    run or the outcome cannot be shown to be the run's own work, so it is neither a durable success
    nor a failure of the capability. MEASURED 2026-10-11, after #525: all 22 of role-triage's
    accepted-arm terminal outcomes were in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` (14
    `unattributed_delegation`, 8 `unattributed_closing_pr`), as were 27 of offload's 73 and all 4 in
    the counterfactual arms. role-triage's durable rate of 0.167 rested on no attributable outcome
    at all. The set is read here, at call time, and nowhere else, so a class added to it is
    excluded without touching this module. `excluded` stands beside `terminal`, so the set-aside is
    a number on the page and not a quietly smaller denominator.
    """
    groups: dict[str, list[dict]] = {
        k: []
        for k in (
            "association",
            "no_outcome_path",
            "excluded",
            "terminal",
            "awaiting_sweep",
            "unresolvable",
            "open",
        )
    }
    for e in edges:
        groups[_classify(e)].append(e)
    excluded, terminal = groups["excluded"], groups["terminal"]
    associations, no_outcome_path = groups["association"], groups["no_outcome_path"]
    awaiting_sweep, unresolvable = groups["awaiting_sweep"], groups["unresolvable"]
    durable = [e for e in terminal if feedback.outcome_is_durable_success(**_outcome_fields(e))]
    # Distinct subjects — a target counts as durable if ANY attempt on it landed durably.
    durable_subjects = {_subject(e) for e in durable}
    terminal_subjects = {_subject(e) for e in terminal}
    excluded_subjects = {_subject(e) for e in excluded}
    out: dict[str, Any] = {
        "attributed": len(edges),
        "terminal": len(terminal),
        "excluded": len(excluded),
        "associations": len(associations),
        "advisory_self": sum(1 for e in associations if e["role_kind"] == "advisory_self"),
        "no_outcome_path": len(no_outcome_path),
        "awaiting_sweep": len(awaiting_sweep),
        "unresolvable": len(unresolvable),
        "durable": len(durable),
        "terminal_subjects": len(terminal_subjects),
        "excluded_subjects": len(excluded_subjects),
        "durable_subjects": len(durable_subjects),
        "attempts_per_subject": (
            round(len(terminal) / len(terminal_subjects), 2) if terminal_subjects else None
        ),
    }
    evidence = len(edges) - len(associations) - len(no_outcome_path)
    if not terminal and not groups["open"] and not awaiting_sweep and edges:
        out["durable_rate"] = None
        if excluded and len(excluded) == evidence:
            # NOT `not_yet_measurable`: outcomes DID resolve, and every one is in a class no
            # learner may score. Waiting will not change that, so one sentinel for both would hide
            # which. Associations are not evidence, so they never turn this into `unscoreable`.
            out["status"] = (
                f"excluded_only ({len(excluded)} attempts on {len(excluded_subjects)} subjects, "
                "every outcome learning-excluded)"
            )
        else:
            # NOT `not_yet_measurable` either: nothing here can ever be scored, so waiting is not
            # the remedy. role-redirect's 762 edges are all on its own role runs.
            out["status"] = (
                f"unscoreable ({len(edges)} edges, none can be scored: "
                + ", ".join(
                    f"{n} {label}"
                    for n, label in (
                        (len(associations), "associations onto role runs"),
                        (len(no_outcome_path), "onto experiment member runs, no outcome path"),
                        (len(unresolvable), "never resolvable"),
                        (len(excluded), "learning-excluded"),
                    )
                    if n
                )
                + ")"
            )
    elif not terminal:
        out["durable_rate"] = None
        out["status"] = "not_yet_measurable"  # NOT 0% — nothing has resolved yet
    elif len(terminal_subjects) < MIN_SAMPLE:
        out["durable_rate"] = None
        out["status"] = (
            f"insufficient_evidence ({len(terminal_subjects)}/{MIN_SAMPLE} distinct "
            f"subjects from {len(terminal)} attempts"
            + (f"; {len(excluded)} learning-excluded" if excluded else "")
            + (f"; {len(awaiting_sweep)} awaiting the durability sweep" if awaiting_sweep else "")
            + (f"; {len(unresolvable)} never resolvable" if unresolvable else "")
            + ")"
        )
    else:
        out["durable_rate"] = round(len(durable_subjects) / len(terminal_subjects), 3)
        out["status"] = "measured"
    return out


def measure(*, path=None, conn=None) -> dict:
    # A report, so `load_declared`: the writing `load` would seed, reconcile and expire rows and
    # write the result into the shared ledger. This reconciles a copy and writes nothing.
    caps = capabilities.load_declared(path or capabilities.REG)
    rows = _edge_rows(conn)
    by_cap: dict[str, list[dict]] = {}
    for row in rows:
        by_cap.setdefault(row["capability_id"], []).append(row)

    out = []
    for cap_id, cap in sorted(caps.items()):
        edges = by_cap.get(cap_id, [])
        accepted = [e for e in edges if e["accepted"] and not e["counterfactual"]]
        control = [e for e in edges if e["counterfactual"]]
        usage = capabilities.usage_rate(cap)
        entry = {
            "capability_id": cap_id,
            "status": cap.get("status"),
            "invocations_per_week": usage["invocations_per_week"],
            "attributed_edges": len(edges),
            "accepted": _arm_stats(accepted),
            "counterfactual": _arm_stats(control),
        }
        a, c = entry["accepted"], entry["counterfactual"]
        if a["durable_rate"] is not None and c["durable_rate"] is not None:
            entry["lift"] = round(a["durable_rate"] - c["durable_rate"], 3)
            entry["verdict"] = (
                "helps" if entry["lift"] > 0 else "hurts" if entry["lift"] < 0 else "neutral"
            )
        else:
            entry["lift"] = None
            # No control arm is the COMMON case and must not read as a result.
            entry["verdict"] = "no_control_arm" if a["durable_rate"] is not None else a["status"]
        out.append(entry)

    attributed = [e for e in out if e["attributed_edges"]]
    return {
        "min_sample": MIN_SAMPLE,
        "capabilities": len(out),
        "with_attribution": len(attributed),
        "measured": [
            e["capability_id"] for e in out if e["verdict"] in ("helps", "hurts", "neutral")
        ],
        "awaiting_outcomes": [
            e["capability_id"]
            for e in attributed
            if e["accepted"]["status"] == "not_yet_measurable"
        ],
        # Accepted arms resolved with no scoreable outcomes; the control arm is independent.
        "excluded_only": [
            e["capability_id"]
            for e in attributed
            if e["accepted"]["status"].startswith("excluded_only")
        ],
        # Accepted arms none of whose edges can ever be scored. Not awaiting anything.
        "unscoreable": [
            e["capability_id"]
            for e in attributed
            if e["accepted"]["status"].startswith("unscoreable")
        ],
        "rows": out,
    }


def _arm_cell(arm: dict, *, attempts: bool) -> str:
    cell = f"{arm['durable_subjects']}/{arm['terminal_subjects']} subj" if arm["terminal"] else "—"
    if arm["terminal"] and attempts:
        cell += f" ({arm['terminal']} att)"
    for key, label in (
        ("excluded", "excl"),
        ("awaiting_sweep", "sweep"),
        ("unresolvable", "unres"),
        ("associations", "assoc"),
        ("no_outcome_path", "no-path"),
    ):
        if arm[key]:
            cell += f", {arm[key]} {label}"
    return cell


def format_report(rep: dict) -> str:
    lines = [
        "# Capability effectiveness — does using it improve outcomes?",
        "",
        f"{rep['with_attribution']} of {rep['capabilities']} capabilities have attributed evidence; "
        f"{len(rep['measured'])} have enough of it to state a rate "
        f"(minimum {rep['min_sample']} terminal outcomes).",
        "",
    ]
    if not rep["with_attribution"]:
        lines += [
            "No capability has attributed evidence yet. Nothing to measure — this is the",
            "honest state, not a zero score.",
            "",
        ]
    lines += [
        "| Capability | Inv/wk | Edges | Accepted (dur/term) | Control | Lift | Verdict |",
        "|---|---:|---:|---|---|---:|---|",
    ]
    for row in rep["rows"]:
        if not row["attributed_edges"]:
            continue
        arm = _arm_cell(row["accepted"], attempts=True)
        ctl = _arm_cell(row["counterfactual"], attempts=False)
        lift = "—" if row["lift"] is None else f"{row['lift']:+.3f}"
        lines.append(
            f"| {row['capability_id']} | {row['invocations_per_week']} | "
            f"{row['attributed_edges']} | {arm} | {ctl} | {lift} | {row['verdict']} |"
        )
    lines += [
        "",
        "`excl`: attempts whose outcome is in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES. "
        "They are set aside in both arms and never scored.",
        "`sweep`: merges the durability sweep has not judged yet; they resolve, so they wait.",
        "`unres`: unmerged runs whose durability nothing will ever judge; never scored.",
        "`assoc`: edges onto role runs, whose outcome is a copy of the acting run's; never scored.",
        "`no-path`: edges onto experiment member runs, which never push; a winner's delivery is "
        "credited to its synthesis run. Never scored.",
    ]
    if rep["awaiting_outcomes"]:
        lines += [
            "",
            "Awaiting outcome resolution (edges exist, nothing terminal yet): "
            + ", ".join(rep["awaiting_outcomes"]),
        ]
    if rep["excluded_only"]:
        lines += [
            "",
            "Accepted arms resolved, but every outcome is learning-excluded, "
            "so that arm is not scored: " + ", ".join(rep["excluded_only"]),
        ]
    if rep["unscoreable"]:
        lines += [
            "",
            "Accepted arms none of whose edges can ever be scored (not awaiting anything): "
            + ", ".join(rep["unscoreable"]),
        ]
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    # A capability with a real sample on both arms yields a lift.
    edges = (
        [
            {
                "capability_id": "c1",
                "accepted": True,
                "counterfactual": False,
                "verdict": "PASS",
                "durability": "durable",
                "merged": 1,
                "run_id": f"a{i}",
                "ts": 0,
            }
            for i in range(4)
        ]
        + [
            {
                "capability_id": "c1",
                "accepted": True,
                "counterfactual": False,
                "verdict": "FAIL",
                "durability": "abandoned",
                "run_id": "a9",
                "ts": 0,
            }
        ]
        + [
            {
                "capability_id": "c1",
                "accepted": False,
                "counterfactual": True,
                "verdict": "PASS",
                "durability": "durable",
                "merged": 1,
                "run_id": f"b{i}",
                "ts": 0,
            }
            for i in range(2)
        ]
        + [
            {
                "capability_id": "c1",
                "accepted": False,
                "counterfactual": True,
                "verdict": "FAIL",
                "durability": "abandoned",
                "run_id": f"c{i}",
                "ts": 0,
            }
            for i in range(3)
        ]
    )
    a = _arm_stats([e for e in edges if e["accepted"]])
    assert a["terminal"] == 5 and a["durable"] == 4 and a["durable_rate"] == 0.8, a
    c = _arm_stats([e for e in edges if e["counterfactual"]])
    assert c["terminal"] == 5 and c["durable_rate"] == 0.4, c

    # AN EDGE WITH NO COMPLETION ENVELOPE IS NOT MEASURABLE EVIDENCE. `capability_causal_evidence`
    # already refuses to consume one (feedback.py:1430); this module must agree, or the same table
    # means two different things to two consumers. Regression for the live case: 5 hand-created
    # `link-outcome` edges targeting advisory `role:redirect:*` runs were 100% of `offload`'s
    # durable signal at a reported rate of 0.059, with no causal link behind any of them.
    import sqlite3 as _sqlite3

    _c = _sqlite3.connect(":memory:")
    _c.executescript(feedback.SCHEMA)
    feedback._migrate_schema(_c)
    for _eid, _tev in (("e-linked", "ev-1"), ("e-orphan", None)):
        _c.execute(
            "INSERT INTO influence_edges (edge_id,schema_version,influence_type,influence_id,"
            "source_run_id,target_event_id,target_run_id,capability_id,accepted,counterfactual,"
            "outcome_verdict,durability,created_ts,metadata_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _eid,
                1,
                "capability",
                "offload",
                "src",
                _tev,
                f"remote:o/r#{_eid}:codex",
                "offload",
                1,
                0,
                "PASS",
                "durable",
                0,
                "h",
            ),
        )
    _rows = _edge_rows(_c)
    _ids = {r["run_id"] for r in _rows}
    assert any("e-linked" in i for i in _ids), _ids
    # The orphan must be absent from `attributed` as well as from `terminal` — reporting a
    # capability as busy on evidence it cannot be measured by is the same lie one layer up.
    assert not any("e-orphan" in i for i in _ids), _ids
    assert _arm_stats(_rows)["attributed"] == 1, _arm_stats(_rows)

    # WHAT HAPPENED IS READ FROM `outcomes`, AND AN EXCLUDED OUTCOME IS COUNTED, NEVER SCORED. The
    # live case: a relabel moved the outcome into `unattributed_delegation` and cleared its verdict,
    # while the edge kept the PASS/durable it was given before. And the reverse: an outcome that
    # resolved durable after its edge was last propagated. `e-linked` above has no outcome row, so
    # the PASS/durable written onto it directly is not an outcome either.
    for _eid, _cf, _snapshot, _outcome in (
        (
            "e-relabelled",
            0,
            ("PASS", "durable"),
            (None, "abandoned", "unattributed_delegation", None),
        ),
        ("e-resolved", 0, ("PASS", "pending"), ("PASS", "durable", None, 1)),
        (
            "e-control",
            1,
            ("UNATTRIBUTED", "abandoned"),
            (None, "abandoned", "transient_infra", None),
        ),
    ):
        _run = f"remote:o/r#{_eid}:codex"
        _c.execute(
            "INSERT INTO influence_edges (edge_id,schema_version,influence_type,influence_id,"
            "source_run_id,target_event_id,target_run_id,capability_id,accepted,counterfactual,"
            "outcome_verdict,durability,created_ts,metadata_hash) "
            "VALUES (?,1,'capability','offload','src','ev-1',?,'offload',?,?,?,?,0,'h')",
            (_eid, _run, 1 - _cf, _cf, *_snapshot),
        )
        _c.execute(
            "INSERT INTO outcomes (run_id,adjudicated_verdict,durability,failure_class,merged) "
            "VALUES (?,?,?,?,?)",
            (_run, *_outcome),
        )
    _rows = _edge_rows(_c)
    _acc = _arm_stats([r for r in _rows if not r["counterfactual"]])
    assert (_acc["attributed"], _acc["terminal"], _acc["durable"]) == (3, 1, 1), _acc
    assert (_acc["excluded"], _acc["excluded_subjects"]) == (1, 1), _acc
    _ctl = _arm_stats([r for r in _rows if r["counterfactual"]])
    assert (_ctl["terminal"], _ctl["excluded"]) == (0, 1), _ctl
    assert _ctl["status"].startswith("excluded_only"), "resolved-but-excluded is not 'pending'"

    # A ROLE RUN'S OUTCOME IS AN ASSOCIATION, A MERGE THE SWEEP HAS NOT JUDGED WAITS, AND AN
    # UNMERGED PENDING RUN IS UNRESOLVABLE. Each was a terminal non-durable result before
    # 2026-10-11: the role run's copy of its acting run's outcome, a merge in the sweep's grace
    # window, and an offload run nothing will ever judge. A verifier NON_PASS under an adjudicated
    # PASS is terminal and is not a durable success (feedback's predicates).
    _c.execute("INSERT INTO runs (run_id, role_name) VALUES ('role:triage:x:1', 'triage')")
    for _eid, _cap, _run, _fields in (
        ("e-self", "role-triage", "role:triage:x:1", ("PASS", None, "durable", 1)),
        ("e-assoc", "offload", "role:triage:x:1", ("PASS", None, "durable", 1)),
        ("e-sweep", "c9", "remote:o/r#1:codex", ("PASS", None, "pending", 1)),
        ("e-unres", "c9", "offload:gemini:2", ("PASS", "PASS", "pending", None)),
        ("e-nonpass", "c9", "remote:o/r#3:codex", ("PASS", "NON_PASS", "durable", 1)),
    ):
        _c.execute(
            "INSERT INTO influence_edges (edge_id,schema_version,influence_type,influence_id,"
            "source_run_id,target_event_id,target_run_id,capability_id,accepted,counterfactual,"
            "created_ts,metadata_hash) "
            "VALUES (?,1,'capability',?,'src','ev-1',?,?,1,0,0,'h')",
            (_eid, _cap, _run, _cap),
        )
        _c.execute(
            "INSERT OR REPLACE INTO outcomes "
            "(run_id,adjudicated_verdict,verifier_verdict,durability,merged) VALUES (?,?,?,?,?)",
            (_run, *_fields),
        )
    _rows = _edge_rows(_c)
    _self = _arm_stats([r for r in _rows if r["capability_id"] == "role-triage"])
    assert (_self["terminal"], _self["associations"], _self["advisory_self"]) == (0, 1, 1), _self
    assert _self["status"].startswith("unscoreable (1 edges"), _self["status"]
    _off = _arm_stats([r for r in _rows if r["run_id"] == "role:triage:x:1"])
    assert (_off["terminal"], _off["associations"], _off["advisory_self"]) == (0, 2, 1), _off
    _c9 = _arm_stats([r for r in _rows if r["capability_id"] == "c9"])
    assert (_c9["terminal"], _c9["durable"]) == (1, 0), _c9
    assert (_c9["awaiting_sweep"], _c9["unresolvable"]) == (1, 1), _c9
    # AN EXPERIMENT MEMBER RUN HAS NO OUTCOME PATH. It never pushes, so it read as awaiting
    # forever (abcd-experiment's 12 edges, to 2026-10-12); an outcome another writer put on it is
    # not the member's delivery. Its synthesis run carries the member identity and is not one.
    _member_meta = '{"experiment_identity_version": 2, "experiment_arm_id": "a", "experiment_member_id": "a--m"}'
    _c.execute(
        "INSERT INTO runs (run_id, routing_metadata) VALUES ('x:member:a--m', ?), ('x:synth:a', ?)",
        (_member_meta, _member_meta[:-1] + ', "synthesis_base_member_id": "a--m"}'),
    )
    for _eid, _run in (("e-mem", "x:member:a--m"), ("e-mem2", "x:member:a--m")):
        _c.execute(
            "INSERT INTO influence_edges (edge_id,schema_version,influence_type,influence_id,"
            "source_run_id,target_event_id,target_run_id,capability_id,accepted,counterfactual,"
            "created_ts,metadata_hash) "
            "VALUES (?,1,'capability','abcd-experiment','src','ev-1',?,'abcd-experiment',1,0,0,'h')",
            (_eid, _run),
        )
    _c.execute(
        "INSERT OR REPLACE INTO outcomes (run_id,adjudicated_verdict,durability,merged) "
        "VALUES ('x:member:a--m','PASS','durable',1)"
    )
    _rows = _edge_rows(_c)
    _ab = _arm_stats([r for r in _rows if r["capability_id"] == "abcd-experiment"])
    assert (_ab["no_outcome_path"], _ab["terminal"], _ab["durable"]) == (2, 0, 0), _ab
    assert _ab["status"].startswith("unscoreable (2 edges"), _ab["status"]
    _synth_meta = _c.execute("SELECT routing_metadata FROM runs WHERE run_id='x:synth:a'")
    assert not feedback.experiment_member_target(_synth_meta.fetchone()[0]), "synthesis run"

    # THE SUBJECT IS THE LIFECYCLE'S (`feedback.run_subject_id`), NOT A PARSE OF THE RUN ID. Two
    # attempts at one issue under local run ids are one subject; parsing the id made them two.
    for _n in (1, 2):
        _run = f"stranske__Repo_20-gemini-{_n}"
        _c.execute("INSERT INTO runs (run_id, target) VALUES (?, 'stranske/Repo#20')", (_run,))
        _c.execute(
            "INSERT INTO influence_edges (edge_id,schema_version,influence_type,influence_id,"
            "source_run_id,target_event_id,target_run_id,capability_id,accepted,counterfactual,"
            "created_ts,metadata_hash) "
            "VALUES (?,1,'capability','c8','src','ev-1',?,'c8',1,0,0,'h')",
            (f"e-local-{_n}", _run),
        )
        _c.execute(
            "INSERT INTO outcomes (run_id,adjudicated_verdict,durability,merged) "
            "VALUES (?,'PASS','durable',1)",
            (_run,),
        )
    _c8 = _arm_stats([r for r in _edge_rows(_c) if r["capability_id"] == "c8"])
    assert (_c8["terminal"], _c8["terminal_subjects"], _c8["durable_subjects"]) == (2, 1, 1), _c8
    _c.close()

    # CORRELATED ATTEMPTS MUST NOT INFLATE THE SAMPLE. 30 retries of one stuck target is one
    # observation, not 30 — the confound that produced a false 0% for role-triage on 2026-08-18.
    stuck = [
        {
            "capability_id": "c2",
            "accepted": True,
            "counterfactual": False,
            "verdict": "FAIL",
            "durability": "abandoned",
            "run_id": f"remote:o/r#2819:cursor:{i}",
            "ts": 0,
        }
        for i in range(30)
    ]
    s = _arm_stats(stuck)
    assert s["terminal"] == 30 and s["terminal_subjects"] == 1, s
    assert s["durable_rate"] is None, "30 attempts at ONE target must not yield a rate"
    assert "1/5 distinct subjects from 30 attempts" in s["status"], s["status"]
    assert s["attempts_per_subject"] == 30.0, s
    # Five DISTINCT targets do earn a rate.
    spread = [
        {
            "capability_id": "c3",
            "accepted": True,
            "counterfactual": False,
            "verdict": "FAIL",
            "durability": "abandoned",
            "run_id": f"remote:o/r#{900+i}:cursor:1",
            "ts": 0,
        }
        for i in range(5)
    ]
    sp = _arm_stats(spread)
    assert sp["terminal_subjects"] == 5 and sp["durable_rate"] == 0.0, sp
    assert sp["status"] == "measured", sp
    # A subject counts durable if ANY attempt on it landed durably.
    mixed = spread + [
        {
            "capability_id": "c3",
            "accepted": True,
            "counterfactual": False,
            "verdict": "PASS",
            "durability": "durable",
            "merged": 1,
            "run_id": "remote:o/r#900:codex:2",
            "ts": 0,
        }
    ]
    mx = _arm_stats(mixed)
    assert mx["terminal_subjects"] == 5 and mx["durable_subjects"] == 1, mx
    assert mx["durable_rate"] == 0.2, mx
    assert _target_of("remote:stranske/Workflows#2819:cursor") == "stranske/Workflows#2819"
    assert _target_of("plain-run-id") == "plain-run-id"

    # REFUSALS: a small sample reports its denominator, never a rate.
    small = _arm_stats(edges[:2])
    assert small["durable_rate"] is None and "insufficient_evidence" in small["status"], small
    # No terminal outcome is "not yet measurable", NEVER 0%.
    pending = _arm_stats(
        [
            {
                "capability_id": "c",
                "accepted": True,
                "counterfactual": False,
                "verdict": None,
                "durability": None,
                "run_id": "p",
                "ts": 0,
            }
        ]
    )
    assert pending["durable_rate"] is None and pending["status"] == "not_yet_measurable", pending
    assert pending["durable"] == 0 and pending["attributed"] == 1, pending

    # An accepted arm with no control must not be reported as a comparison.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="cap-eff-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rec = capabilities._blank_capability("c1")
        rec["status"] = "shadow"
        capabilities.save({"c1": rec}, ledger)

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class FakeConn:
            def execute(self, *_a, **_k):
                return _Cursor(
                    [
                        (
                            e["capability_id"],
                            e["accepted"],
                            e["counterfactual"],
                            e["verdict"],  # adjudicated
                            None,  # verifier
                            e["durability"],
                            None,  # failure_class
                            e["run_id"],
                            e["ts"],
                            e.get("merged"),
                            None,  # runs.role_name: a work run
                            None,  # runs.target
                            None,  # runs.routing_metadata: not an experiment member
                        )
                        for e in edges
                        if e["accepted"]
                    ]
                )

            def close(self):
                pass

        rep = measure(path=ledger, conn=FakeConn())
        row = rep["rows"][0]
        assert row["lift"] is None and row["verdict"] == "no_control_arm", row
        assert rep["measured"] == [], "no control arm is not a measured verdict"
        text = format_report(rep)
        assert "no_control_arm" in text

        # Empty evidence must say so rather than score zero.
        class EmptyConn:
            def execute(self, *_a, **_k):
                return _Cursor([])

            def close(self):
                pass

        empty = measure(path=ledger, conn=EmptyConn())
        assert empty["with_attribution"] == 0
        assert "not a zero score" in format_report(empty)

    print(
        "capability_effectiveness.py selftest: OK (lift on real samples, refuses small samples, "
        "pending != 0%, no-control-arm is not a verdict, outcomes read current, "
        "learning-excluded counted not scored, role-run edges are associations, "
        "an unjudged merge waits and an unmerged pending run is unresolvable, "
        "subjects are the lifecycle's, an experiment member run has no outcome path)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = measure()
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
