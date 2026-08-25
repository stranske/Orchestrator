#!/usr/bin/env python3
"""capability_task_proposals.py — for a capability that has never been SCORED, what task would
give it a fair chance to be useful?

THE QUESTION THIS ANSWERS, and why the two existing modules do not answer it. Both of them look
BACKWARD at the run history:

  * `capability_opportunity.py`  — "was it not useful, or never considered?" (CAN work be routed
                                   here: is there a reachable trigger at all)
  * `capability_matcher_proposals.py`
                                 — "SHOULD work have been routed here?" (did the fleet repeatedly
                                   do work this capability exists to handle, while never invoking
                                   it)

Neither proposes anything to DO NOW. That gap is the whole reason 35 of 43 capabilities carry no
scored verdict while every measurement instrument around them keeps improving: the system can say
which capabilities are unexercised and cannot say what would exercise them. Measurement had
outrun task supply.

THE INSIGHT THIS RESTS ON: a reasoned decline is a task specification written in negative form.
`no_landing_zone` declares `fix: "nothing — the match was correct and the deliverable had nowhere
to put the result"`, and that "nothing" is true of the CAPABILITY and false of the WORK. Read as a
statement about the task it is precise and actionable: run the same work with a write target.
`scope_too_small`'s own comment says "a one-subsystem audit has no read big enough to pay for a
dispatch" — which names the task exactly, a multi-subsystem read. So the shapes are not invented
here; they are derived from `capability_propensity.DECLINE_KINDS`, where `task_shape` sits beside
`fix` in ONE table precisely so the two answers cannot drift apart.

WHY THIS IS NOT A RETIREMENT TOOL, and must never become one. `capability_opportunity` already
states the rule — "never proposes retirement — an unused capability is a capability we have not
yet learned how to use" — and the evidence backs it: `frontend-verifier` was declined on two
frontend-less repos and then produced the second-strongest finding of an audit on a repo that had
a display surface. Demoting on the two negatives would have cost that finding. Every defect this
project repaired on 2026-08-25 was found by USING an instrument, not by measuring it, so a
capability that has never run is not a candidate for removal, it is an untested hypothesis with no
experiment yet designed. `wrong_match` is the one kind whose task shape is deliberately NONE: when
the match itself was wrong, constructing a task to suit it would be fitting the work to the tool.

RANKING PREFERS THE RECURSIVE CASE. A self-scoped capability (`applies_to == "self"` in
`capability_advisor.CAPABILITY_PRECONDITIONS`) measures THIS system, so exercising it produces two
kinds of evidence at once: a verdict on itself, and a diagnosis of something else. Those are the
tasks worth constructing first, and `diagnostic_of_the_system` is derived from the declared
precondition rather than hand-listed so it cannot go stale.

LATCHED-GATE ANSWERS (CLAUDE.md requires all four in writing before any counter ships):
  1. WHAT DECREMENTS IT — a capability acquiring a scored verdict (`useful` / `not-useful`), which
     moves it out of `unexercised` on the next read. Named mechanism, not "time passes".
  2. CAN THE DRAIN RUN WHILE CLOSED — yes, and this is the important one. Proposing a task requires
     nothing from the capability being proposed for, and the proposal gates NOTHING: it is a report.
     A capability with zero evidence is exactly the case this must keep working for.
  3. MEASURING WINDOW == DRAINING WINDOW — both sides read `usefulness(window_days=...)` through
     the one `capability_propensity.WINDOW_DAYS`, consumed here rather than re-declared.
  4. WHAT IT PRINTS WHEN FULLY DRAINED — "every capability has been exercised", and that line is
     reachable: `unexercised_count == 0` renders it. It is kept DISTINCT from "the ledger could not
     be read", which returns None and prints NOT MEASURED. One sentinel must never mean both, which
     is the ninth latched-gate instance this repo has already paid for.

Report-only. It proposes; it never dispatches, never writes a verdict, and queues nothing for
anyone (0 minutes/week of owner attention).

    python3 capability_task_proposals.py                 # ranked proposals
    python3 capability_task_proposals.py --json
    python3 capability_task_proposals.py --capability testgen-lane
    python3 capability_task_proposals.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys

import capability_propensity as propensity

try:  # the advisor reaches the binding tables; absence must degrade, never crash
    import capability_advisor as advisor
except Exception:  # noqa: BLE001  # pragma: no cover
    advisor = None  # type: ignore[assignment]


# An unexercised capability is one with no SCORED verdict. Deliberately not "never invoked": a
# capability can be triggered and never judged, and that is precisely the state this exists to
# clear — the objective is a scored invocation, not an invocation (CLAUDE.md §-2).
def unexercised(*, path=None, window_days: int | None = None) -> dict | None:
    """Every ledger capability with no scored verdict, with the decline evidence that explains it.

    Returns None — never an empty dict — when the ledger cannot be read, so "nothing is unexercised"
    and "nothing could be measured" stay distinguishable at every caller.
    """
    days = propensity.WINDOW_DAYS if window_days is None else window_days
    try:
        rows = propensity.usefulness(path=path, window_days=days)["rows"]
    except Exception:  # noqa: BLE001
        return None
    out = {}
    for cap_id, row in rows.items():
        if row["resolved"]:
            continue
        out[cap_id] = {
            "capability_id": cap_id,
            "status": row["status"],
            "offered": row["candidates"],
            "triggered": row["triggered"],
            "declined": row["declined"],
            "declines_by_kind": dict(row["declines_by_kind"]),
        }
    return out


def _self_scoped(cap_id: str) -> bool:
    """Does this capability measure THIS system? Derived from the declared precondition."""
    if advisor is None:
        return False
    row = getattr(advisor, "CAPABILITY_PRECONDITIONS", {}).get(cap_id) or {}
    return str(row.get("applies_to")) in {"self", "both"}


def _declared_precondition(cap_id: str) -> dict | None:
    """The capability's own declared precondition, so a proposal names ITS subject not an example.

    Without this the `precondition_unmet` shape reads generically and the reader has to go and look
    up what condition actually failed — which is the same "go and reconstruct it by hand" gap that
    made `HOW_TO_USE` worth writing in the first place.
    """
    if advisor is None:
        return None
    row = getattr(advisor, "CAPABILITY_PRECONDITIONS", {}).get(cap_id)
    if not row:
        return None
    return {
        "applies_to": row.get("applies_to"),
        "concept": row.get("concept"),
        "requires": row.get("requires"),
    }


def _bound_surfaces(cap_id: str) -> list[str]:
    """Every surface whose declared binding names this capability."""
    if advisor is None:
        return []
    bindings = getattr(advisor, "SURFACE_BINDINGS", {}) or {}
    found = []
    for surface, entries in bindings.items():
        try:
            names = [e[0] if isinstance(e, (list, tuple)) else e for e in entries]
        except TypeError:  # pragma: no cover  # a NO_BINDING sentinel or similar
            continue
        if cap_id in names:
            found.append(str(surface))
    return sorted(found)


def _subject_hint(cap_id: str, kind: str | None) -> str | None:
    """For a `precondition_unmet` decline, WHERE to point the same work so the condition holds."""
    if kind != "precondition_unmet":
        return None
    pre = _declared_precondition(cap_id)
    if pre is None:
        return (
            "NO PRECONDITION IS DECLARED, which makes this decline unattributable: the caller said "
            "the condition did not hold and the capability never stated what the condition is. "
            "Declaring it in `capability_advisor.CAPABILITY_PRECONDITIONS` is the task."
        )
    if str(pre.get("applies_to")) in {"self", "both"}:
        return (
            "THIS SYSTEM. It is self-scoped, so every decline recorded during an audit of another "
            "repository is a subject mismatch rather than a failure — point the same work at the "
            "Orchestrator itself and the precondition holds by construction."
        )
    return f"a subject satisfying applies_to={pre.get('applies_to')!r}" + (
        f"; requires {pre['requires']!r}" if pre.get("requires") else ""
    )


def _dominant_kind(declines_by_kind: dict) -> str | None:
    """The decline kind that most often explains this capability's non-use.

    Ties break toward the kind whose task shape is ACTIONABLE, because a tie between `wrong_match`
    (shape: none) and `no_landing_zone` (shape: give it a write target) should propose the task
    rather than shrug. A tie broken toward "nothing to do" would make the proposer quietest exactly
    where it has the most to say.
    """
    if not declines_by_kind:
        return None
    best_n = max(declines_by_kind.values())
    tied = [k for k, n in declines_by_kind.items() if n == best_n]
    actionable = [k for k in tied if _shape_is_actionable(k)]
    return sorted(actionable or tied)[0]


def _shape_is_actionable(kind: str) -> bool:
    row = propensity.DECLINE_KINDS.get(kind) or {}
    return not str(row.get("task_shape", "")).startswith(("NONE", "ASK FIRST"))


def propose(*, path=None, window_days: int | None = None) -> dict:
    """Rank a task proposal for every unexercised capability. Report-only."""
    days = propensity.WINDOW_DAYS if window_days is None else days_or(window_days)
    pending = unexercised(path=path, window_days=days)
    if pending is None:
        return {
            "window_days": days,
            "unexercised_count": None,
            "measured": False,
            "why_not_measured": "the capability ledger could not be read",
            "proposals": [],
        }
    proposals = []
    for cap_id, row in sorted(pending.items()):
        kind = _dominant_kind(row["declines_by_kind"])
        kind_row = propensity.DECLINE_KINDS.get(kind or "", {})
        surfaces = _bound_surfaces(cap_id)
        if kind:
            why = (
                f"offered {row['offered']}x, declined {row['declined']}x, most often "
                f"{kind!r} — the declines say the TASK did not fit, not that the capability failed"
            )
            shape = str(kind_row.get("task_shape") or "")
        elif row["offered"]:
            why = (
                f"offered {row['offered']}x and never answered: {row['offered']} silent offers, "
                "so nothing says whether it was considered and passed over or never read"
            )
            shape = (
                "RECORD THE ANSWER FIRST. Silence is not evidence of a bad fit; until the surface "
                "records a reasoned decline there is no signal to derive a task from. Fix the "
                "consult before designing the task."
            )
        else:
            why = "never offered at any surface — no binding reaches it"
            shape = (
                "BIND IT, then design the task. A capability no surface offers cannot be chosen, "
                "so it can never earn the evidence that would justify keeping it."
            )
        proposals.append(
            {
                "capability_id": cap_id,
                "status": row["status"],
                "why_unexercised": why,
                "task_shape": shape,
                "shape_actionable": bool(kind and _shape_is_actionable(kind)),
                "dominant_decline_kind": kind,
                "declines_by_kind": row["declines_by_kind"],
                "bound_surfaces": surfaces,
                "consult_from": surfaces[0] if surfaces else None,
                # THE RECURSIVE PREFERENCE the owner asked for: a self-scoped capability produces a
                # verdict on itself AND a diagnosis of something else, so one task buys two kinds of
                # evidence. Derived from the declared precondition, never hand-listed.
                "diagnostic_of_the_system": _self_scoped(cap_id),
                # ITS OWN precondition, so `precondition_unmet` names the condition that failed
                # rather than leaving the reader to look it up. None where none is declared — and
                # an UNDECLARED precondition on a capability declining for `precondition_unmet` is
                # itself the finding, which is what the `subject_that_satisfies_it` line says.
                "declared_precondition": _declared_precondition(cap_id),
                "subject_that_satisfies_it": _subject_hint(cap_id, kind),
                # WHAT WOULD COUNT. Stated per proposal because "it ran" is not the objective; a
                # scored verdict is (CLAUDE.md §-2), and a proposal that does not say what would
                # close it is a suggestion rather than an experiment.
                "acceptance": (
                    "a scored verdict on this capability: `trigger` then `useful`/`--not-useful` "
                    "with evidence naming what it CHANGED, at a stated --provenance. A run that "
                    "invokes it and records nothing leaves it exactly as unexercised as before."
                ),
                "counts_as_progress_even_if_not_useful": True,
            }
        )
    proposals.sort(
        key=lambda p: (
            not p["shape_actionable"],
            not p["diagnostic_of_the_system"],
            -sum(p["declines_by_kind"].values()),
            p["capability_id"],
        )
    )
    for i, p in enumerate(proposals, 1):
        p["rank"] = i
    return {
        "window_days": days,
        "unexercised_count": len(pending),
        "measured": True,
        "actionable_now": sum(1 for p in proposals if p["shape_actionable"]),
        "diagnostics_of_the_system": sum(1 for p in proposals if p["diagnostic_of_the_system"]),
        "blocked_on_recording": sum(
            1 for p in proposals if p["dominant_decline_kind"] is None and p["bound_surfaces"]
        ),
        "unbound": sum(1 for p in proposals if not p["bound_surfaces"]),
        "proposals": proposals,
    }


def days_or(value: int) -> int:
    """`window_days` passthrough that refuses a nonsense window rather than silently widening it."""
    n = int(value)
    if n <= 0:
        raise ValueError(f"window_days must be positive, got {n!r}")
    return n


def format_report(rep: dict) -> str:
    lines = ["# capability task proposals", ""]
    if not rep["measured"]:
        lines.append(f"  NOT MEASURED — {rep['why_not_measured']}")
        lines.append("  (this is NOT the same as every capability having been exercised)")
        return "\n".join(lines) + "\n"
    n = rep["unexercised_count"]
    if n == 0:
        # REACHABLE, and deliberately so: the fully-drained line must be renderable by real input,
        # or it is a claim no run can ever make.
        lines.append("  every capability in the ledger has a scored verdict — fully exercised")
        return "\n".join(lines) + "\n"
    lines.append(
        f"  {n} unexercised in {rep['window_days']}d — {rep['actionable_now']} with an actionable "
        f"task shape, {rep['diagnostics_of_the_system']} of them diagnostics of this system"
    )
    lines.append(
        f"  {rep['blocked_on_recording']} blocked on the SURFACE recording an answer, "
        f"{rep['unbound']} reach no surface at all"
    )
    lines.append("")
    for p in rep["proposals"]:
        flag = " [diagnoses this system]" if p["diagnostic_of_the_system"] else ""
        lines.append(f"  {p['rank']:2d}. {p['capability_id']}{flag}")
        lines.append(f"      why: {p['why_unexercised']}")
        lines.append(f"      task: {p['task_shape'][:300]}")
        if p.get("subject_that_satisfies_it"):
            lines.append(f"      point it at: {p['subject_that_satisfies_it'][:220]}")
        if p["consult_from"]:
            lines.append(f"      consult from: {p['consult_from']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    """A PROPOSER THAT GOES QUIET ON A DRAINED LEDGER, AND A DRAINED LEDGER THAT CAN BE REACHED.

    BREAK -> REVERT, each confirmed to discriminate:
      * return {} instead of None when the ledger cannot be read -> the "unmeasurable is not
        drained" assertion fails, which is the ninth latched-gate instance in this repo's own
        history and the reason that assertion exists;
      * tie-break `_dominant_kind` toward the alphabetically-first kind instead of the actionable
        one -> the tie assertion fails, and the proposer would fall silent exactly where it has the
        most to say;
      * drop the `not shape_actionable` term from the sort key -> the ranking assertion fails and a
        `wrong_match` capability (task shape: NONE) outranks one with a real task.
    """
    import tempfile
    from pathlib import Path

    import capabilities

    # 1. UNMEASURABLE IS NOT DRAINED.
    missing = unexercised(path=Path("/nonexistent") / "no-such-ledger.json")
    assert missing is None, "an unreadable ledger must be None, never an empty result"
    rep = propose(path=Path("/nonexistent") / "no-such-ledger.json")
    assert rep["unexercised_count"] is None and rep["measured"] is False, rep
    assert "NOT MEASURED" in format_report(rep), format_report(rep)
    assert "fully exercised" not in format_report(rep)

    with tempfile.TemporaryDirectory(prefix="task-proposals-selftest-") as td:
        ledger = Path(td) / "capabilities.json"

        # 2. A DRAINED LEDGER RENDERS THE DRAINED LINE. An unreachable "fully drained" branch is
        #    the exact defect CLAUDE.md's fourth question exists to catch.
        cap = capabilities._blank_capability("scored")
        capabilities.save({"scored": cap}, ledger)
        x = "advice:taskprop0001"
        propensity.record_trigger("scored", x, path=ledger)
        propensity.record_usefulness(
            "scored",
            x,
            useful=True,
            evidence="it changed the outcome",
            provenance="self_reported",
            path=ledger,
        )
        drained = propose(path=ledger)
        assert drained["unexercised_count"] == 0, drained
        assert drained["measured"] is True
        assert "fully exercised" in format_report(drained), format_report(drained)

        # 3. A DECLINE BECOMES A TASK. The whole thesis: no_landing_zone is not a dead end.
        rows = {"scored": capabilities.load_declared(ledger)["scored"]}
        # NAMED so the alphabet OPPOSES the required order: without the `shape_actionable` term
        # in the sort key, "aaa-mismatched" (task shape NONE) would outrank "zzz-landless". The
        # first draft used "landless"/"mismatched", which sort in the desired order anyway, so the
        # break passed and the assertion proved nothing.
        for cid in ("zzz-landless", "aaa-mismatched"):
            rows[cid] = capabilities._blank_capability(cid)
        capabilities.save(rows, ledger)
        y = "advice:taskprop0002"
        propensity.record_decline(
            "zzz-landless",
            y,
            reason="read-only audit, no commit target",
            surface="repo-audit:fix",
            kind="no_landing_zone",
            path=ledger,
        )
        propensity.record_decline(
            "aaa-mismatched",
            y,
            reason="matched the noun, not the intent",
            surface="repo-audit:fix",
            kind="wrong_match",
            path=ledger,
        )
        rep = propose(path=ledger)
        by_id = {p["capability_id"]: p for p in rep["proposals"]}
        assert rep["unexercised_count"] == 2, rep["unexercised_count"]
        assert "scored" not in by_id, "a scored capability is not unexercised"
        assert by_id["zzz-landless"]["task_shape"].startswith("GIVE IT A LANDING ZONE"), by_id[
            "zzz-landless"
        ]
        assert by_id["zzz-landless"]["shape_actionable"] is True
        # AND THE ONE KIND THAT MUST NOT PRODUCE A TASK.
        assert by_id["aaa-mismatched"]["task_shape"].startswith("NONE"), by_id["aaa-mismatched"]
        assert by_id["aaa-mismatched"]["shape_actionable"] is False, (
            "wrong_match must NOT be actionable: fitting a task to a wrong match is fitting the "
            "work to the tool"
        )
        # RANKING: the actionable task outranks the one with no task.
        assert by_id["zzz-landless"]["rank"] < by_id["aaa-mismatched"]["rank"], rep["proposals"]

        # 3b. THE RECURSIVE PREFERENCE. A self-scoped capability outranks an equally-placed one
        #     that is not, because exercising it buys a verdict AND a diagnosis. Injected into the
        #     advisor's own table and named to OPPOSE the alphabet, so the preference is the only
        #     thing that can produce the order — without this fixture the ranking term was
        #     untested, and a break that removed it passed.
        if advisor is not None:
            rows2 = dict(capabilities.load_declared(ledger))
            rows2["zzz-selfscoped"] = capabilities._blank_capability("zzz-selfscoped")
            capabilities.save(rows2, ledger)
            propensity.record_decline(
                "zzz-selfscoped",
                "advice:taskprop0003",
                reason="read-only audit, no commit target",
                surface="repo-audit:fix",
                kind="no_landing_zone",
                path=ledger,
            )
            advisor.CAPABILITY_PRECONDITIONS["zzz-selfscoped"] = {
                "applies_to": "self",
                "concept": "synthetic, selftest only",
            }
            try:
                ranked = propose(path=ledger)["proposals"]
                order = [r["capability_id"] for r in ranked]
                assert "zzz-selfscoped" in order, order
                assert order.index("zzz-selfscoped") < order.index("zzz-landless"), (
                    "a self-scoped capability must outrank an equally-placed one that is not: "
                    f"got {order}"
                )
            finally:
                del advisor.CAPABILITY_PRECONDITIONS["zzz-selfscoped"]

        # 4. EVERY DECLINE KIND CARRIES A SHAPE, so a new kind cannot be added without saying what
        #    task would satisfy it. This is the durable half — it binds future edits, not this one.
        for kind, row in propensity.DECLINE_KINDS.items():
            assert str(row.get("task_shape", "")).strip(), f"{kind} has no task_shape"

        # 5. THE TIE-BREAK, tested against a kind that sorts FIRST. Every real non-actionable kind
        #    today is `unspecified` or `wrong_match`, both of which sort AFTER every actionable one,
        #    so alphabetical order happens to agree and a tie test using them proves nothing — the
        #    first draft did exactly that and the break passed. A synthetic kind named to sort first
        #    makes the tie-break the only thing that can produce the right answer, which also means
        #    this assertion survives a future kind being renamed into that position.
        propensity.DECLINE_KINDS["aaa_shapeless"] = {
            "demotable": False,
            "repairable": False,
            "fix": "n/a",
            "task_shape": "NONE — synthetic, selftest only",
        }
        try:
            tie = _dominant_kind({"aaa_shapeless": 3, "no_landing_zone": 3})
            assert (
                tie == "no_landing_zone"
            ), f"the tie must resolve toward the ACTIONABLE shape, got {tie!r}"
            assert (
                _dominant_kind({"aaa_shapeless": 5}) == "aaa_shapeless"
            ), "with no actionable kind present it must still answer, not return None"
        finally:
            del propensity.DECLINE_KINDS["aaa_shapeless"]
        tie = _dominant_kind({"wrong_match": 3, "no_landing_zone": 3})
        assert tie == "no_landing_zone", tie
        # ...and with no actionable kind present it still answers rather than returning None.
        assert _dominant_kind({"wrong_match": 2}) == "wrong_match"
        assert _dominant_kind({}) is None

        # 6. A NONSENSE WINDOW IS REFUSED, not silently widened into "everything ever".
        try:
            propose(path=ledger, window_days=0)
            raise AssertionError("a non-positive window must be refused")
        except ValueError:
            pass

    print(
        "capability_task_proposals selftest: OK (unmeasurable is not drained, the drained line is "
        "reachable, a no_landing_zone decline becomes a task while wrong_match deliberately does "
        "not, every decline kind carries a shape, ties resolve toward the actionable shape, and a "
        "nonsense window is refused)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--capability", default="", help="show only this capability's proposal")
    ap.add_argument("--window-days", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = propose(window_days=args.window_days)
    if args.capability:
        rep["proposals"] = [p for p in rep["proposals"] if p["capability_id"] == args.capability]
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    # NOT an error exit when nothing is proposed: this is a report, and a report that fails the
    # shell when the news is good would get switched off.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
