#!/usr/bin/env python3
"""capability_admission.py — what a capability must bring WITH it, and what a promise must leave behind.

WHY THIS EXISTS. Every gate in this tree checks the capabilities that already exist:
`capabilities validate` checks schema, `capability_activation_audit` checks whether each can fire,
`capability_recurrence_check` replays history, `test_capability_set_coverage` insists the set is a
set. All of them are retrospective. Nothing ever stopped a capability from being ADDED without the
parts that make it work, and nothing ever noticed when a dated promise produced no artifact. Those
two holes account for essentially all of the wasted effort on this project:

  * 2026-07-03/08: six fully-built subsystems found dormant. Countermeasure applied: a prose rule in
    CLAUDE.md ("dedup-before-develop"). In August, MORE dormancy was found anyway — `watch.py` with
    no caller, `features.py` with no caller, `issue-readiness` and `switch-review` with no heartbeat.
    A documentation rule did not survive contact with the next session. This module is that rule
    expressed as a failing test.
  * 2026-07-08: the audit closed with "all findings resolved or deliberately deferred; nothing
    dropped." Six weeks later the first activation measurement was **21 of 34 reachable, 13
    blocked**. "Findings dispositioned" was being counted as "capabilities working".
  * 2026-07-15: the range-lane trial review FIRED (scheduled task `lastRunAt`
    2026-07-15T18:00:04Z) and left no artifact — no decision record, no backlog note. The flag
    auto-reverted the next day and stayed off for 36 days, while `orchestrate.sh:95` cites
    `2026-07-15-range-lane-trial-review.md`, a file that was never written. A safety backstop
    designed to prevent silent PERMANENCE instead produced silent ABANDONMENT.

So this module enforces two things the others cannot:

  A. ADMISSION — a capability must arrive with all NINE parts, or the suite fails. Checked from a
     proposed spec too (`preflight`), so the answer arrives before the code is written, not after.
     The ninth, findability, was added 2026-08-23: the first eight make a capability invocable and
     observable, and none of them makes it FINDABLE. 22 of 43 rows were bound to no surface at all,
     so nothing could offer them and no amount of running could produce evidence for them — the
     rule against that existed, in prose, in the very document that argues prose does not survive.
  B. COMMITMENTS — a dated promise must resolve to an artifact that exists. A citation to a
     decision record that was never written, or a trial deadline that passed with nothing recorded,
     fails here instead of rotting quietly.

Zero-owner by construction: every failure names a machine-checkable fix an agent performs. Nothing
here queues anything for a human.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
from typing import Any

import capabilities
import env_prereq
import paths

HERE = pathlib.Path(__file__).resolve().parent


def _audits_dir() -> pathlib.Path:
    """Where the dated decision records live, resolved rather than hardcoded.

    This was pinned to the Dropbox workspace layout, which is correct on the machine this tool grew
    up on and wrong on any other. Since the point of a repo is running instances in more than one
    place, the path resolves the same way `capability_activation_audit._fleet_roots` does: an
    explicit $ORCH_FLEET_ROOT first, then the sibling layout, then the home-anchored workspace.
    """
    override = os.environ.get("ORCH_AUDITS_DIR")
    if override:
        return pathlib.Path(override).expanduser()
    candidates = [
        (
            pathlib.Path(os.environ["ORCH_FLEET_ROOT"]).expanduser()
            if os.environ.get("ORCH_FLEET_ROOT")
            else None
        ),
        paths.FLEET_ROOT,
        pathlib.Path.home() / "Library/CloudStorage/Dropbox/Learning/Code",
    ]
    for root in candidates:
        if root and (root / "Audits" / "Orchestrator").is_dir():
            return root / "Audits" / "Orchestrator"
    return pathlib.Path.home() / "Library/CloudStorage/Dropbox/Learning/Code/Audits/Orchestrator"


AUDITS = _audits_dir()

# Capabilities registered before the gate existed cannot retroactively carry a dedup note. They are
# GRANDFATHERED, not exempt: the count is printed on every run so the debt stays visible instead of
# quietly becoming the norm. NOTE: the first value written here was four days in the
# FUTURE, so every capability — including brand-new ones — passed as grandfathered.
# The selftest below caught it, which is the whole argument for this file: a cutoff
# that silently admits everything is exactly the failure mode being guarded against.
GRANDFATHERED_BEFORE = 1787270400  # 2026-08-21T00:00:00Z, the day this gate landed

# A deliberate exception must name a capability, a reason, and a BOUNDED expiry. "Has an expiry" is
# not enough: a break-test set one to 9999999999 and every check still passed, which would have made
# a permanent exemption look like a temporary one — the same shape as a bounded trial that quietly
# became permanent. So an expiry must also be within MAX_WAIVER_DAYS.
MAX_WAIVER_DAYS = 90
WAIVERS: dict[str, dict] = {}


def waiver_problems() -> list[str]:
    """Every reason a waiver is not a legitimate, reviewable, time-boxed exception."""
    problems = []
    horizon = _now() + MAX_WAIVER_DAYS * 86400
    for cap_id, waiver in WAIVERS.items():
        expires = int(waiver.get("expires", 0) or 0)
        if not waiver.get("reason"):
            problems.append(f"{cap_id}: no reason")
        if expires <= 0:
            problems.append(f"{cap_id}: no expiry")
        elif expires > horizon:
            problems.append(
                f"{cap_id}: expiry is {(expires - _now()) // 86400}d out, "
                f"beyond the {MAX_WAIVER_DAYS}d limit — that is a permanent "
                f"exemption wearing a deadline"
            )
    return problems


# --------------------------------------------------------------------------------------------
# A. Admission requirements — the eight parts, each a machine-checkable predicate.
# --------------------------------------------------------------------------------------------


def _has(cap: dict, field: str) -> bool:
    value = cap.get(field)
    return value not in (None, "", [], {})


def req_dedup_recorded(cap: dict, ctx: dict) -> tuple[bool, str]:
    """CLAUDE.md 0 as a test, not a hope.

    The project's stated #1 failure mode is building what already exists. The rule said to record
    the dedup finding in the plan; plans are not durable, so it was recorded nowhere and the rule
    was re-broken. The ledger IS durable, so the finding goes there.
    """
    notes = str(cap.get("notes") or "")
    if re.search(
        r"\bdedup\b|already exists|not present.*build|checked .*(not|no) (present|match)",
        notes,
        re.IGNORECASE,
    ):
        return True, "dedup finding recorded in notes"
    return False, (
        "no dedup finding recorded. State in `notes` which existing concepts were "
        "searched and the verdict — 'checked X/Y/Z; not present; building new' or "
        "'exists at file:line, dormant behind FLAG; activating'"
    )


def req_caller_exists(cap: dict, ctx: dict) -> tuple[bool, str]:
    """Something must actually invoke it. This is the defect that produced six dormant subsystems."""
    row = ctx["audit_rows"].get(cap["capability_id"]) or {}
    if row.get("reachable"):
        return True, "entrypoint reachable"
    return False, f"unreachable: {', '.join(row.get('defects') or ['unknown'])}"


def req_heartbeat(cap: dict, ctx: dict) -> tuple[bool, str]:
    """It must be CREDITED when it runs, or it looks dormant forever and gets 'cleaned up'.

    `issue-readiness` and `switch-review` each shipped working code with no heartbeat, so neither
    could ever accrue evidence of its own usefulness.
    """
    import capability_activation_audit as audit

    hb = audit.heartbeat_reachable(cap)
    status = hb.get("status")
    if status == "reachable":
        return True, "heartbeat on the executed path"
    return False, f"heartbeat {status}: a capability that cannot be credited reads as dormant"


def req_fixture(cap: dict, ctx: dict) -> tuple[bool, str]:
    """A replayable historical instance, so 'would it fire?' is answerable without guessing."""
    if cap["capability_id"] in ctx["fixtures"]:
        return True, "recurrence fixture present"
    return False, "no recurrence fixture (see test_capability_set_coverage)"


def req_outcome_path(cap: dict, ctx: dict) -> tuple[bool, str]:
    """Evidence must be able to REACH the thing that judges it.

    This is the `capability:reference-sync-hygiene-test-gate` defect, and the subtlest one here. It
    had a producer, a consumer, a kill switch, a rollback and 367 recorded events — and its
    promotion gate still read an empty table, because its evidence went to the ledger while the gate
    read `influence_edges`. Waiting could never have fixed it. A declared consumer AND a declared
    learning sink is the minimum that makes "wait for evidence" an honest instruction.
    """
    if _has(cap, "downstream_consumer") and (
        _has(cap, "learning_sink") or _has(cap, "outcome_links")
    ):
        return True, "consumer + learning sink declared"
    missing = [f for f in ("downstream_consumer", "learning_sink") if not _has(cap, f)]
    return False, (
        f"no path from output to judgement (missing {', '.join(missing)}). Without it, "
        "'let evidence accumulate' is an instruction that can never be satisfied"
    )


def req_kill_switch(cap: dict, ctx: dict) -> tuple[bool, str]:
    """Something must be able to stop it without a code change.

    TWO NARROW EXEMPTIONS, both opt-in and DECLARED, following the `gate_blocks_execution` pattern:
    each needs a category AND a written rationale, so neither can be set in passing.

    `safety_guard` -- the capability IS a confinement, so its OFF state is strictly MORE dangerous
    than its ON state and demanding a switch asks for an anti-feature. `agy-runtime-isolation` forced
    this: it adds an absolute `--add-dir <cwd>` keeping gemini's writes inside the target worktree,
    and "disabling" it means letting an agent write outside its worktree.

    `compute_only` -- the capability COMPUTES rather than DOES, so stopping it halts no action, it
    blinds a consumer. Disabling capacity computation does not stop routing, it makes routing worse.
    The anti-abuse condition is the whole point: "read-only" is exactly what a capability would
    self-certify to clear a red, so it must NAME a `control_point` and that switch must actually be
    found in the tree. A category you can assert about yourself is one everything eventually joins.

    The control case for both is `offload`: it had the identical complaint on the same day and got a
    REAL switch (`ORCH_OFFLOAD_DISABLED`), because a transport genuinely should be stoppable.
    """
    if _has(cap, "kill_switch"):
        return True, "kill switch declared"
    if cap.get("kill_switch_category") == "safety_guard" and _has(cap, "kill_switch_rationale"):
        return True, (
            "safety guard: its OFF state is more dangerous than its ON state, so a kill "
            "switch would be an anti-feature"
        )
    if (
        cap.get("kill_switch_category") == "compute_only"
        and _has(cap, "kill_switch_rationale")
        and _has(cap, "control_point")
    ):
        control = str(cap.get("control_point") or "")
        if control in (ctx.get("known_controls") or set()):
            return True, f"compute-only: the acting consumer carries the control ({control})"
        return False, (
            f"compute-only capability names control_point '{control}', which is not a "
            "known switch in this tree -- an unverifiable control is not a control"
        )
    return False, "no kill switch: nothing can stop it without a code change"


def req_rollback(cap: dict, ctx: dict) -> tuple[bool, str]:
    if _has(cap, "rollback"):
        return True, "rollback declared"
    return False, "no rollback path declared"


def req_expiry_or_cadence(cap: dict, ctx: dict) -> tuple[bool, str]:
    """Nothing may sit unexamined forever.

    A capability with neither an expiry nor a cadence has no moment at which anyone asks whether it
    still earns its place — which is exactly how a dormant subsystem survives two audits.
    """
    if _has(cap, "expiry") or _has(cap, "activation_deadline") or _has(cap, "trigger_cadence"):
        return True, "expiry or cadence declared"
    return False, "neither expiry nor cadence: nothing will ever re-examine this"


# --------------------------------------------------------------------------------------------
# The ninth part — FINDABILITY. The eight above make a capability invocable and observable. None of
# them makes it FINDABLE, and a capability nothing can offer can never earn the evidence that would
# improve it: the gate would starve its own drain.
# --------------------------------------------------------------------------------------------

# `ADDING_CAPABILITIES.md` has carried "say which surfaces bind it (or why none does)" as PROSE since
# 2026-08-21. That document's own opening argues a rule living only in prose does not survive the
# next session, and THIS module exists because one did not. Measured 2026-08-23 over the 43-row
# ledger: 37 capabilities have no usefulness evidence at all, and 22 of those are bound to NO
# surface — nothing can offer them, so no amount of running will ever produce evidence for them.
# Every one of the 43 passed admission. That is the same failure the file warns about, one layer up.
FINDABILITY_ENFORCED_FROM = 1787443200  # 2026-08-23T00:00:00Z, the day findability became a gate

# PER-REQUIREMENT ENFORCEMENT DATES. The row-level `legacy` flag answers "was this capability
# registered before the GATE existed". A requirement added LATER needs its own date, or it is red on
# arrival for every capability that predates it — and this module already made that trade once, in
# writing: a gate red on arrival gets switched off, and then it protects nothing. ONE constant per
# requirement, defined here and read only by `admit()`, so the window a requirement MEASURES and the
# window it can be DRAINED over are the same window by construction.
REQUIREMENT_ENFORCED_FROM: dict[str, int] = {"findable": FINDABILITY_ENFORCED_FROM}

# The one declared exemption, shaped exactly like `kill_switch_category`: a category AND a written
# rationale, neither sufficient alone. It is for a capability that is INVOKED rather than OFFERED —
# a rail runs it unconditionally, so selection pressure cannot reach it and a binding would be
# theatre. Deliberately narrow, and a drift guard in `test_capability_admission.py` stops a live
# ledger from out-declaring the committed table, exactly as it does for the two kill-switch
# categories.
FINDABILITY_CATEGORY = "no_surface"
# The category that REPLACED the exemption on 2026-09-02: a rail declares `exercise_bound` and is
# bound on a `rail-exercise:<phase>` surface, so it can be consulted, triggered and scored as an
# EXERCISE while its live path stays with the rail that invokes it. `findability_cause` does not
# special-case it — the binding check is the whole point.
EXERCISE_CATEGORY = "exercise_bound"

# The verdicts that are not failures. `reach_not_evaluated` is here for the same reason
# `commitments()` returns "cannot judge" with no audit ledger: an unreadable advisor must not
# reclassify the whole catalogue as unfindable.
# `declared_no_surface` LEFT THIS SET on 2026-09-02. It was the exemption that let fifteen rails
# pass findability while no surface could ever offer them, which meant no consult could ever
# trigger them and no verdict could ever land — the gate was satisfied by the very condition that
# made measurement impossible. It is still DETECTED (so a stale ledger value gets a precise
# message) but it now fails, and its drain names the fix.
FINDABLE_OK = frozenset({"bound_and_consulted", "reach_not_evaluated"})

# Each detectable cause and the DATA EDIT that clears it. Declared, so `report()` can print a
# drainable count that is falsifiable rather than tautological: add a cause with no drain here and
# `drainable` drops below `failing`, which is the alarm. Both fixes are things an agent performs in
# the same PR, and neither requires the thing the gate forbids — so this gate fails toward motion.
FINDABILITY_DRAIN: dict[str, str] = {
    "bound_nowhere": "add one entry to a surface's 3-7 in capability_advisor.SURFACE_BINDINGS with "
    "its reason; for a rail nobody selects, bind it on a `rail-exercise:<phase>` surface with its "
    f"exercise as the reason and declare findability_category={EXERCISE_CATEGORY!r}",
    "declared_no_surface": f"findability_category={FINDABILITY_CATEGORY!r} no longer exempts "
    "(2026-09-02): bind the rail on a `rail-exercise:<phase>` surface in "
    f"capability_advisor.SURFACE_BINDINGS and declare findability_category={EXERCISE_CATEGORY!r}",
    "bound_to_unconsulted_surface": "bind a surface listed in capability_advisor.CONSULT_SITES, or "
    "make the bound surface consult (pass --surface / the `surface` field)",
}

# What this requirement DOES NOT CHECK, stated where the check is rather than nowhere. A gate that
# cannot say what would clear it is already defective; so is one that cannot say what it never
# looked at.
FINDABILITY_NOT_CHECKED = (
    "a surface that invokes the entrypoint DIRECTLY without surface attribution — the `orchestrate` "
    "skill already runs capacity.py (SKILL.md:31, listed as a tool at :116) while "
    "`windowed-capacity-policy`'s heartbeat sits in `capacity.build` behind "
    "ORCH_CAPABILITY_HEARTBEATS, which only a live tick sets, so the capability is used and "
    "entirely uncredited. `capability_activation_audit.heartbeat_reachable` was checked first and "
    "answers a DIFFERENT question — it reports that row `reachable` via `orchestrate.sh (CLI)`, "
    "because it asks whether SOME driver reaches the heartbeat, not whether THIS surface's "
    "invocation is attributed to the surface. Answering that needs the surface's own prompt, which "
    "lives outside this repository, so no predicate over the tree can see it"
)


def findability_cause(cap: dict, ctx: dict) -> tuple[str, str]:
    """Which sub-cause applies, and the sentence naming the fix. ONE classifier, two readers.

    `req_findable` turns this into a verdict; `report()` counts the causes. A second classifier
    would be the parallel inventory this tree keeps paying for.

    THE THREE SUB-CAUSES HAVE DIFFERENT FIXES, so they are never collapsed into one "unfindable":

      1. `bound_nowhere` — no surface declares it (22 of 43 on 2026-08-23). DETECTED EXACTLY: the
         binding table is committed data and `capability_advisor.surfaces_binding` inverts it.
      2. `bound_to_unconsulted_surface` — every binding names a surface no caller ever consults.
         `capability-admission-gate` and `docs-drift-fix-agent` are bound to `ci`, which nothing
         consults; ten more are bound to `opener-lane`/`closer-lane`, whose prompts consult with no
         surface at all. DETECTED from `capability_advisor.consulting_surfaces()`. One shape of this
         is NOT detectable and the table says so on the entry: `repo-audit:fix` is NAMED by the
         skill and never ENTERED by a run, which only trial records can show.
      3. Invoked without attribution — see `FINDABILITY_NOT_CHECKED`. Deliberately out of reach of
         any predicate over this tree, and named rather than omitted.
    """
    cap_id = cap.get("capability_id")
    # Checked FIRST so a stale `no_surface` value — in a live ledger reconciliation has not
    # refreshed, or in a new declaration written from an old example — gets the precise message,
    # not a generic bound_nowhere. Until 2026-09-02 this branch was an EXEMPTION; it is now a
    # failure with a named drain.
    if cap.get("findability_category") == FINDABILITY_CATEGORY and _has(
        cap, "findability_rationale"
    ):
        return "declared_no_surface", (
            "declared_no_surface: declared unofferable ("
            + str(cap.get("findability_rationale"))[:90]
            + "). "
            + FINDABILITY_DRAIN.get("declared_no_surface", "")
        )
    surfaces = list((ctx.get("bound_surfaces") or {}).get(cap_id) or [])
    if not surfaces:
        return "bound_nowhere", (
            "bound_nowhere: no surface in capability_advisor.SURFACE_BINDINGS offers it, so it is "
            "only ever drawn from the full catalogue queried generically — the measured 13.62% "
            "selection condition. " + FINDABILITY_DRAIN.get("bound_nowhere", "")
        )
    reached = ctx.get("reached_surfaces")
    if not reached:
        return "reach_not_evaluated", (
            f"bound to {len(surfaces)} surface(s); consult reach NOT EVALUATED "
            "(capability_advisor.consulting_surfaces unavailable) — never read as a pass or a fail"
        )
    consulted = [s for s in surfaces if s in reached]
    if consulted:
        return "bound_and_consulted", "offered at " + ", ".join(sorted(consulted)[:3])
    return "bound_to_unconsulted_surface", (
        "bound_to_unconsulted_surface: every binding names a surface no caller consults "
        f"({', '.join(sorted(surfaces)[:4])}). A binding nothing asks for is indistinguishable from "
        "no binding. " + FINDABILITY_DRAIN.get("bound_to_unconsulted_surface", "")
    )


def req_findable(cap: dict, ctx: dict) -> tuple[bool, str]:
    """Can any surface OFFER it? Thin wrapper; `findability_cause` owns the logic."""
    cause, detail = findability_cause(cap, ctx)
    return cause in FINDABLE_OK, detail


REQUIREMENTS = (
    ("dedup_recorded", req_dedup_recorded),
    ("caller_exists", req_caller_exists),
    ("heartbeat", req_heartbeat),
    ("fixture", req_fixture),
    ("outcome_path", req_outcome_path),
    ("kill_switch", req_kill_switch),
    ("rollback", req_rollback),
    ("expiry_or_cadence", req_expiry_or_cadence),
    ("findable", req_findable),
)


def _created_ts(cap: dict) -> int | None:
    events = cap.get("event_history") or []
    stamps = [int(e.get("timestamp") or 0) for e in events if e.get("timestamp")]
    return min(stamps) if stamps else None


def known_controls() -> set[str]:
    """Every switch a `compute_only` capability may legitimately point at as its real control.

    DISCOVERED FROM THE TREE, not hand-listed: a `control_point` is only accepted if the named
    switch actually appears in orchestrate.sh or a module, so a capability cannot clear the
    kill-switch requirement by naming a flag that does not exist. That is the difference between
    delegating a control and asserting one.
    """
    controls: set[str] = set()
    for name in ("orchestrate.sh", "dispatcher.py", "repo_knowledge.py", "router.py"):
        # orchestrate.sh is at the checkout root; the modules sit beside this one.
        base = paths.REPO_ROOT if name.endswith(".sh") else paths.MODULE_DIR
        try:
            text = (base / name).read_text()
        except OSError:
            continue
        controls |= set(re.findall(r"ORCH_[A-Z0-9_]+", text))
    return controls


def _findability_context(capability_ids, *, path: pathlib.Path | None = None) -> dict:
    """The two inputs `req_findable` reads, both CONSUMED from `capability_advisor`.

    Nothing here re-derives a binding or a reach: `surfaces_binding` inverts `binding_for`, and
    `consulting_surfaces` owns the consult table. If the advisor cannot be imported at all, the
    reach set is left empty and the predicate reports `reach_not_evaluated` rather than failing the
    whole catalogue on an ImportError.
    """
    try:
        import capability_advisor as advisor

        reach = advisor.consulting_surfaces()
        return {
            "bound_surfaces": advisor.surfaces_binding(capability_ids, path=path),
            "reached_surfaces": set(reach["reached"]),
            "consult_reach": reach,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "bound_surfaces": {},
            "reached_surfaces": set(),
            "consult_reach": {"unreadable": f"{type(exc).__name__}: {str(exc)[:80]}"},
        }


def _context(path: pathlib.Path | None = None) -> dict:
    # The recurrence-fixture roster lives with the tests, and this gate genuinely needs it. Since
    # the suite moved to `tests/` that directory is not importable by default, so it is added
    # EXPLICITLY rather than left to a sys.path accident — an accident is how this dependency would
    # rot into a silent skip.
    import sys

    import paths

    if str(paths.TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(paths.TESTS_DIR))
    import test_capability_set_coverage as coverage

    import capability_activation_audit as audit

    rows = {r["capability_id"]: r for r in audit.audit(use_cache=True)["rows"]}
    ledger = capabilities.load(path or capabilities.REG)
    return {
        "audit_rows": rows,
        "fixtures": coverage._fixture_capabilities(),
        "known_controls": known_controls(),
        **_findability_context(sorted(ledger), path=path),
    }


def admit(capability_id: str, *, path: pathlib.Path | None = None, ctx: dict | None = None) -> dict:
    """Does this capability carry everything it needs? Per-requirement, never a single verdict."""
    ledger = capabilities.load(path or capabilities.REG)
    cap = ledger.get(capability_id)
    if cap is None:
        raise ValueError(f"unknown capability: {capability_id}")
    ctx = ctx or _context(path)
    checks: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name, fn in REQUIREMENTS:
        try:
            ok, detail = fn(cap, ctx)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"check errored: {str(exc)[:80]}"
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            missing.append(name)
    waiver = WAIVERS.get(capability_id)
    waived = bool(waiver and int(waiver.get("expires", 0)) > _now())
    created = _created_ts(cap)
    legacy = bool(created and created < GRANDFATHERED_BEFORE)
    # PER-REQUIREMENT SCOPING, alongside the row-level kind. A requirement added after this
    # capability was registered is DEFERRED: still reported in `missing` so the debt is visible,
    # but excluded from `blocking` so a newly added rule is not red on arrival for the whole
    # catalogue. Same trade as `legacy`, one level finer, and it lives on the ROW rather than
    # inside a predicate for the same reason — debt must never read as compliance.
    deferred = [
        name
        for name in missing
        if created and created < REQUIREMENT_ENFORCED_FROM.get(name, GRANDFATHERED_BEFORE)
    ]
    blocking = [name for name in missing if name not in deferred]
    return {
        "capability_id": capability_id,
        "status": cap.get("status"),
        "admitted": not missing,
        "missing": missing,
        "deferred": deferred,
        "blocking": blocking,
        "checks": checks,
        "waived": waived,
        "waiver": waiver,
        "legacy": legacy,
        # ENFORCED is the field the test acts on. Scoping matters more than strictness here: a
        # gate that is red on arrival gets switched off, and then it protects nothing. So it
        # binds on capabilities added from now on, while legacy debt is printed on every run
        # instead of being silently forgiven.
        "enforced": (not legacy) and not waived,
    }


def preflight(spec: dict) -> dict:
    """Answer admission for a capability that does NOT exist yet, from a proposed record.

    The point is sequencing. Every requirement here has been discovered the expensive way — after
    the code was written, when the fix was a second project. Running this on a spec first makes the
    missing part a design question instead of a post-mortem.
    """
    stub = {
        **capabilities._blank_capability(spec.get("capability_id") or "capability:proposed"),
        **spec,
    }
    # FINDABILITY IS DECLARABLE, so preflight ANSWERS it rather than deferring it — which is the
    # whole point of running this before writing code. The binding table and the consult table are
    # both committed, so the question needs no ledger and no built module: "which surface will offer
    # this?" is a design question the author can settle now, and the alternative is discovering
    # after the build that nothing can reach it.
    ctx = {
        "audit_rows": {},
        "fixtures": set(),
        **_findability_context([stub["capability_id"]]),
    }
    checks: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    # Caller/heartbeat/fixture cannot be verified for code that does not exist; they are reported as
    # OBLIGATIONS rather than silently skipped, because silently skipping is how they got skipped.
    obligations = {"caller_exists", "heartbeat", "fixture"}
    # A RAIL DECLARES ITS EXERCISE BINDING AS INTENT. Since 2026-09-02 a rail nobody selects is not
    # exempt from findability; it is bound on a `rail-exercise:<phase>` surface, and that binding
    # is a code-table edit the same PR must carry. Preflight cannot see a table entry that does not
    # exist yet, so `exercise_bound` + a rationale makes `findable` an OBLIGATION the suite verifies
    # (`_selftest_rail_exercise` fails a declaration no phase binds) — never a pass.
    exercise_intent = stub.get("findability_category") == EXERCISE_CATEGORY and _has(
        stub, "findability_rationale"
    )
    if exercise_intent:
        obligations.add("findable")
    for name, fn in REQUIREMENTS:
        if name in obligations:
            detail = (
                "cannot verify pre-build — OBLIGATION: must be demonstrated before the "
                "capability is registered"
            )
            if name == "findable":
                detail = (
                    "declared exercise_bound — OBLIGATION: bind it on exactly one "
                    "`rail-exercise:<phase>` in capability_advisor.SURFACE_BINDINGS in the same "
                    "PR, with the exercise as the reason; capability_advisor --selftest verifies"
                )
            checks[name] = {"ok": None, "detail": detail}
            continue
        try:
            ok, detail = fn(stub, ctx)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"check errored: {str(exc)[:80]}"
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            missing.append(name)
    return {
        "capability_id": stub["capability_id"],
        "declarable_missing": missing,
        "obligations": sorted(obligations),
        "checks": checks,
        "ready_to_build": not missing,
    }


# --------------------------------------------------------------------------------------------
# B. Commitments — a dated promise must leave an artifact behind.
# --------------------------------------------------------------------------------------------

CITED_RECORD_RE = re.compile(r"(20\d\d-\d\d-\d\d-[A-Za-z0-9._-]+\.md)")
# Deliberately permissive between the name and the date: the FIRST version of this pattern
# required the date to follow almost immediately, so it missed
# `ORCH_RANGE_LANE_TRIAL_UNTIL="${ORCH_RANGE_LANE_TRIAL_UNTIL:-2026-07-22}"` — the exact line whose
# silent expiry this check exists to catch. A detector that misses the motivating case is worthless.
DEADLINE_RE = re.compile(
    r"([A-Z][A-Z_]*(?:UNTIL|DEADLINE|EXPIRES?)[A-Z_]*)[^\n]{0,60}?" r"(20\d\d-\d\d-\d\d)"
)
SCAN_SUFFIXES = (".py", ".sh", ".md")
# This module necessarily NAMES the records that were never written, because documenting them is the
# whole point; the usefulness log likewise narrates history and cites closed records. Both would
# otherwise report themselves forever, and a check that cries wolf about itself gets muted.
#
# `IMPROVEMENT_BACKLOG.md` was the third entry and is deliberately GONE from this set: the file that
# narrated history now lives outside the tree (`improvement_log.py`), and what remains at that path
# is a short pointer with nothing to exempt. A stale allowlist entry is an incident record for a
# condition that no longer exists, and it would silence the scan over a file this gate should read.
SKIP_NAMES = {"capability_admission.py", "CAPABILITY_USEFULNESS.md"}


def _now() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _subject_of(var: str) -> str:
    """`ORCH_RANGE_LANE_TRIAL_UNTIL` -> `range-lane`: the thing whose deadline passed."""
    parts = [
        p
        for p in var.lower().split("_")
        if p not in {"orch", "trial", "until", "deadline", "expires", "expire", "date"}
    ]
    return "-".join(parts[:2]) or var.lower()


def _decision_recorded_after(var: str, date: str) -> bool:
    """Did SOMETHING get written down after this deadline about the thing that expired?

    Matching on the deadline date alone would be wrong (the decision record is usually dated the
    REVIEW day, not the expiry day) and matching on nothing would be vacuous. So: any audit record
    dated on-or-after the deadline whose name or body mentions the subject. That is what was missing
    for range-lane — the newest audit record predates the expiry entirely.
    """
    subject = _subject_of(var)
    # A SHORT, GENERIC subject must not be matched loosely. The first version searched for the bare
    # token, so a var reducing to "thing" matched any record containing the word "thing" and
    # reported a decision that was never made — a false PASS, which is worse than no check at all.
    # Short subjects therefore fall back to the full variable name, which cannot collide.
    needle = subject if len(subject) >= 6 else var.lower()
    pattern = re.compile(r"\b" + re.escape(needle) + r"\b")
    if not AUDITS.is_dir():
        return True  # cannot judge without the ledger; never fail on absence
    for record in AUDITS.glob("20*.md"):
        if record.name[:10] < date or record.name.endswith("-PLAN.md"):
            continue
        if pattern.search(record.name.lower()):
            return True
        try:
            if pattern.search(record.read_text(encoding="utf-8", errors="ignore").lower()):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _record_exists(name: str) -> bool:
    if (AUDITS / name).exists():
        return True
    return any((HERE / name).exists() for _ in (0,)) or bool(list(HERE.glob(f"**/{name}")))


def commitments(*, root: pathlib.Path | None = None) -> dict:
    """Find dated promises and check that each produced something.

    Two shapes, both drawn from the same real failure:
      * a CITED decision record (`2026-07-15-range-lane-trial-review.md`) that does not exist —
        live code justifying itself with a document nobody wrote;
      * a DEADLINE that has passed with no corresponding record — a bounded trial that ended by
        timeout rather than by decision.
    """
    root = root or HERE
    # NO LEDGER, NO VERDICT. The dated decision records live in the workspace audit directory, which
    # a fresh clone or a CI runner does not have. Judging citations without it would fail every
    # public CI run for a reason that has nothing to do with the code — and a check that is red for
    # environmental reasons gets disabled, which is how the range-lane citation went unnoticed in
    # the first place. Absence of the ledger means "cannot judge", never "violation".
    if not AUDITS.is_dir():
        return {
            "dangling_citations": [],
            "overdue_without_record": [],
            "clean": True,
            "skipped": f"audit ledger not present at {AUDITS} — citations not judged",
        }
    dangling, overdue = [], []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES or path.name in SKIP_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for name in CITED_RECORD_RE.findall(line):
                if name.endswith("-PLAN.md"):
                    continue
                if not _record_exists(name):
                    dangling.append(
                        {
                            "file": path.name,
                            "line": line_no,
                            "record": name,
                            "text": line.strip()[:120],
                        }
                    )
            for var, date in DEADLINE_RE.findall(line):
                if date >= _today():
                    continue
                if not _decision_recorded_after(var, date):
                    overdue.append(
                        {
                            "file": path.name,
                            "line": line_no,
                            "var": var,
                            "date": date,
                            "text": line.strip()[:120],
                            "needs": f"an audit record dated >= {date} that names "
                            f"{_subject_of(var)}",
                        }
                    )
    return {
        "dangling_citations": dangling,
        "overdue_without_record": overdue,
        "clean": not dangling and not overdue,
    }


# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------


def _capability_heartbeat(event: str) -> None:
    """Credit this gate when it actually runs.

    Two capabilities in this tree — `issue-readiness` and `switch-review` — shipped working code
    with no heartbeat, so neither could ever accrue evidence of its own usefulness and both read as
    dormant. A module that enforces the heartbeat rule must not be the third.
    """
    try:
        capabilities.production_heartbeat("capability-admission-gate", event)
    except Exception:  # noqa: BLE001 — never break the gate
        pass


def findability_report(ledger: dict, ctx: dict, rows: list[dict] | None = None) -> dict:
    """Findability's own numbers: what it BLOCKS, what it defers, and what would drain each.

    BOTH QUANTITIES IN ONE PLACE, which is this workspace's standing rule for any gate: `25/43`
    reads as "be patient" indefinitely, while `25/43, drainable 25` says the fix is available now
    and `drainable 0` would be an instant deadlock report. `drainable` is computed from
    `FINDABILITY_DRAIN`, so a future cause with no declared fix makes it fall BELOW `failing`
    instead of silently inheriting a comfortable number.
    """
    by_cause: dict[str, list[str]] = {}
    for cap_id in sorted(ledger):
        cause, _detail = findability_cause(ledger[cap_id], ctx)
        if cause in FINDABLE_OK:
            continue
        by_cause.setdefault(cause, []).append(cap_id)
    failing = sorted(cap for ids in by_cause.values() for cap in ids)
    blocking = sorted(
        r["capability_id"] for r in (rows or []) if r["enforced"] and "findable" in r["blocking"]
    )
    drainable = sorted(
        cap for cause, ids in by_cause.items() if cause in FINDABILITY_DRAIN for cap in ids
    )
    reach = ctx.get("consult_reach") or {}
    return {
        "total": len(ledger),
        "failing": failing,
        "by_cause": {cause: sorted(ids) for cause, ids in sorted(by_cause.items())},
        "blocking": blocking,
        "deferred": sorted(set(failing) - set(blocking)),
        "drainable": drainable,
        "drain": dict(FINDABILITY_DRAIN),
        "not_checked": FINDABILITY_NOT_CHECKED,
        "bound_unconsulted_surfaces": list(reach.get("bound_unconsulted") or []),
        "consult_sites_unverified": list(reach.get("unverified") or []),
        "consult_sites_drifted": list(reach.get("drifted") or []),
        "enforced_from": FINDABILITY_ENFORCED_FROM,
    }


def report(*, path: pathlib.Path | None = None, ctx: dict | None = None) -> dict:
    _capability_heartbeat("invocation")
    ledger = capabilities.load(path or capabilities.REG)
    ctx = ctx or _context(path)
    rows = [admit(cid, path=path, ctx=ctx) for cid in sorted(ledger)]
    enforced = [r for r in rows if r["enforced"]]
    return {
        "total": len(rows),
        "admitted": sum(1 for r in rows if r["admitted"]),
        "enforced_total": len(enforced),
        # BLOCKING, not merely "not admitted": a requirement that postdates the capability is
        # reported below and does not fail the suite. Without the distinction, adding the ninth
        # part would have turned four rows red the day it landed.
        "enforced_failing": [r["capability_id"] for r in enforced if r["blocking"]],
        "legacy_debt": sorted(
            r["capability_id"] for r in rows if r["legacy"] and not r["admitted"]
        ),
        "findability": findability_report(ledger, ctx, rows),
        "rows": rows,
        "commitments": commitments(),
    }


def format_report(rep: dict) -> str:
    out = [
        "# Capability admission gate",
        "",
        f"  ADMITTED:        {rep['admitted']} of {rep['total']}",
        f"  ENFORCED (new):  {rep['enforced_total']} capability(ies) — "
        f"{len(rep['enforced_failing'])} failing",
        f"  LEGACY DEBT:     {len(rep['legacy_debt'])} pre-gate capability(ies) short of the "
        f"full nine (visible every run, not forgiven)",
        "",
    ]
    if rep["enforced_failing"]:
        out.append("  ENFORCED FAILURES — these block the suite:")
        for r in rep["rows"]:
            if r["capability_id"] not in rep["enforced_failing"]:
                continue
            out.append(f"    {r['capability_id']}")
            for name in r["missing"]:
                out.append(f"        {name}: {r['checks'][name]['detail'][:104]}")
    else:
        out.append(
            "  no enforced failures: every capability added since the gate carries its parts"
        )
    find = rep.get("findability") or {}
    if find:
        causes = ", ".join(f"{len(ids)} {cause}" for cause, ids in sorted(find["by_cause"].items()))
        out += [
            "",
            f"  FINDABILITY:     {len(find['failing'])} of {find['total']} cannot be OFFERED"
            + (f" — {causes}" if causes else ""),
            f"                   blocking {len(find['blocking'])}, "
            f"pre-cutoff debt {len(find['deferred'])}, "
            f"DRAINABLE {len(find['drainable'])} (a data edit each, no code)",
        ]
        if find["bound_unconsulted_surfaces"]:
            out.append(
                "                   surfaces bound but never consulted: "
                + ", ".join(find["bound_unconsulted_surfaces"])
            )
        for d in find["consult_sites_drifted"]:
            out.append(f"                   DRIFT {d['surface']}: {d['why']} ({d['caller']})")
        if find["consult_sites_unverified"]:
            out.append(
                "                   consult sites not verifiable here: "
                + ", ".join(u["surface"] for u in find["consult_sites_unverified"])
            )
        out.append(f"                   NOT CHECKED: {find['not_checked'][:96]}...")
    if rep["legacy_debt"]:
        out += ["", "  legacy debt (pay down opportunistically; never a human queue):"]
        counts: dict[str, int] = {}
        for r in rep["rows"]:
            if r["capability_id"] in rep["legacy_debt"]:
                for name in r["missing"]:
                    counts[name] = counts.get(name, 0) + 1
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            out.append(f"    {name}: {n}")
    com = rep["commitments"]
    out += ["", "  Commitments:"]
    if com["clean"]:
        out.append("    every dated promise resolves to an artifact that exists")
    for d in com["dangling_citations"]:
        out.append(f"    DANGLING  {d['file']}:{d['line']} cites {d['record']} — never written")
    for o in com["overdue_without_record"]:
        out.append(
            f"    OVERDUE   {o['file']}:{o['line']} {o['var']}={o['date']} "
            f"passed with no record"
        )
    return "\n".join(out) + "\n"


def _probe_commitments(probe: pathlib.Path) -> None:
    """Prove the commitment DETECTOR works, against synthetic files in `probe`.

    Split out of `_selftest` so `AUDITS` can be swapped for a synthetic empty record set around
    exactly this block and restored after — the assertions are verbatim.
    """
    (probe / "fake.sh").write_text("# see 2026-01-02-nonexistent-decision.md\n")
    got = commitments(root=probe)
    assert any(
        d["record"] == "2026-01-02-nonexistent-decision.md" for d in got["dangling_citations"]
    ), got
    assert not got["clean"]
    # A PLAN is not a decision record; citing one must not be flagged.
    (probe / "fake.sh").write_text("# see 2026-01-02-thing-PLAN.md\n")
    assert commitments(root=probe)["clean"], "a -PLAN citation must not be treated as a record"
    # An overdue deadline with no record is flagged.
    # The MOTIVATING SHAPE, verbatim: a shell default with the var repeated inside. The first
    # pattern here could not match this, which would have made the whole check theatre.
    (probe / "fake2.sh").write_text(
        'ORCH_THING_TRIAL_UNTIL="${ORCH_THING_TRIAL_UNTIL:-2020-01-01}"\n'
    )
    over = commitments(root=probe)
    assert any(o["date"] == "2020-01-01" for o in over["overdue_without_record"]), over
    # A future deadline is not overdue.
    (probe / "fake2.sh").write_text('ORCH_X_TRIAL_UNTIL="${ORCH_X_TRIAL_UNTIL:-2099-01-01}"\n')
    assert not commitments(root=probe)["overdue_without_record"]


def _probe_live_root(probe: pathlib.Path) -> None:
    """Prove the DEFAULT-root scan reaches THIS TREE, and reports what it finds accurately.

    Called with `AUDITS` already pointed at the synthetic empty record set, so every dated record
    cited in the tree dangles by construction and the verdicts are identical on every machine.

    WHY THIS EXISTS, and why it is not a second copy of `_probe_commitments`. `_selftest` used to
    compute `cited = {d["record"] for d in commitments()["dangling_citations"]}` and assert nothing
    on it; the dead binding was removed as an unused local, leaving the section header's claim —
    "the real historical failure must be detected, not hypothetically detectable" — unenforced.
    The assertion it once carried named the range-lane record cited by `orchestrate.sh`, and it
    CANNOT be restored as it stood: that record was eventually written, so the live dangling set is
    now empty where the ledger exists. Nor is `assert not cited` worth restoring — that is a weaker
    copy of `test_capability_admission.test_dated_promises_left_an_artifact`, which already asserts
    the strongest available claim on the live set, that BOTH lists are empty.

    What neither of those covers is the hole this closes. `_probe_commitments` always passes an
    explicit `root=`, so nothing in the suite exercises `root or HERE`. A default that stopped
    resolving to the checkout — module relocated, `SCAN_SUFFIXES` narrowed, `SKIP_NAMES` widened,
    `iterdir` over the wrong directory — returns `clean: True` over ZERO files. The live selftest
    assertion (`isinstance(com["clean"], bool)`) and the pytest emptiness assertion would BOTH stay
    green forever on a scan that examined nothing, which is `verify.py`'s vacuous zero-exit one
    level up: the check runs, reads green, and looks at nothing. So what has to surface here is the
    real tree's own citations — `orchestrate.sh`'s among them — not a synthetic `fake.sh`.
    """
    live = commitments()["dangling_citations"]
    # NON-VACUITY. Against a record set where nothing exists, every dated citation in the tree
    # dangles, so this set is empty only if the scan read no files. Should the tree ever
    # legitimately stop citing dated records, the live check has genuinely become vacuous and this
    # anchor needs re-pointing — that is the loud failure, and it is the intended one.
    assert live, (
        "the default-root scan surfaced no dated-record citations, against a record set in which "
        f"every one of them dangles — so `commitments()` examined nothing under {HERE}. Check "
        "`root or HERE`, SCAN_SUFFIXES and SKIP_NAMES before touching this assertion."
    )
    # PROVENANCE. `_probe_commitments` only ever checks `record`, so a report that names the right
    # record at the wrong file:line passes it. That report is unactionable — the whole output of
    # this gate is "go look here" — and misattribution is invisible from the record name alone.
    for d in live:
        src = HERE / d["file"]
        assert src.is_file(), f"dangling report names a file that is not in this tree: {d}"
        lines = src.read_text(encoding="utf-8", errors="ignore").splitlines()
        assert 1 <= d["line"] <= len(lines), f"line number falls outside {d['file']}: {d}"
        assert d["record"] in lines[d["line"] - 1], f"citation misattributed to a line: {d}"
    # THE SKIP MUST STILL HOLD on the live path, stated as a LITERAL rather than as
    # `reported & SKIP_NAMES` — that first draft compared the report against the very set whose
    # failure it was meant to catch, so emptying SKIP_NAMES made it vacuously true and the break
    # test caught nothing. This file cites two dated records in its own docstring because it
    # documents the detector; were it ever scanned it would report ITSELF, and a finding that can
    # only be cleared by deleting the detector's documentation is a permanently-red gate, which
    # gets switched off. So the concrete fact is asserted, independent of the mechanism.
    reported = {d["file"] for d in live}
    assert pathlib.Path(__file__).name not in reported, (
        f"the detector reported its own documented examples: {sorted(reported)}. "
        f"SKIP_NAMES must keep {pathlib.Path(__file__).name} out of its own scan."
    )
    # NO LEDGER, NO VERDICT — the other reason a live-set assertion cannot be restored, pinned so
    # it stays a deliberate fail-open rather than an accident. Absence must return "nothing found",
    # never a verdict it could not compute. Pointed at a path that does not exist, so this runs on
    # the ledger machine too instead of only where the ledger happens to be missing.
    saved = globals()["AUDITS"]
    globals()["AUDITS"] = probe / "no-such-ledger"
    try:
        absent = commitments()
    finally:
        globals()["AUDITS"] = saved
    assert absent.get("skipped") and absent["clean"], absent
    assert not absent["dangling_citations"] and not absent["overdue_without_record"], absent


def _selftest() -> None:
    ledger = capabilities.load_declared(capabilities.REG)
    assert ledger, "ledger must load"
    # Sections needing this instance's registration history are gated individually and named at
    # the end — gating the WHOLE selftest for one block would drop everything below it, which is
    # running less to report green.
    gaps: list[str] = []

    # Every requirement must be able to FAIL. A predicate that always passes is decoration.
    ctx: dict[str, Any] = {"audit_rows": {}, "fixtures": set()}
    empty = capabilities._blank_capability("capability:nothing-declared")
    empty["event_history"] = [{"timestamp": _now(), "type": "migrated"}]  # not grandfathered
    for name, fn in REQUIREMENTS:
        try:
            ok, _ = fn(empty, ctx)
        except Exception:  # noqa: BLE001
            ok = False
        assert not ok, f"requirement {name!r} passes an empty capability — it checks nothing"

    # ...and each must be able to PASS, or the gate is unsatisfiable and will be disabled.
    full = {
        **empty,
        "notes": "dedup: checked A/B/C; not present; building new",
        "downstream_consumer": "x.py:consume",
        "learning_sink": "feedback.outcomes",
        "kill_switch": "ORCH_X=0",
        "rollback": "revert PR",
        "trigger_cadence": "daily",
    }
    ctx_full = {
        "audit_rows": {"capability:nothing-declared": {"reachable": True, "defects": []}},
        "fixtures": {"capability:nothing-declared"},
        # Findability is satisfied by DATA, so the satisfiable case is a synthetic binding to a
        # synthetic consulted surface. Driving it from ctx rather than from the real table keeps
        # this assertion independent of whatever the live binding table happens to say.
        "bound_surfaces": {"capability:nothing-declared": ["t-consulted"]},
        "reached_surfaces": {"t-consulted"},
    }
    for name, fn in REQUIREMENTS:
        if name == "heartbeat":
            continue  # needs real module introspection; covered on live rows
        ok, detail = fn(full, ctx_full)
        assert ok, f"requirement {name!r} cannot be satisfied even when declared: {detail}"

    # EVERY CAUSE THE CLASSIFIER CAN RETURN IS EITHER A PASS OR CARRIES ITS OWN FIX. A gate that
    # cannot say what would clear it is already defective, and `FINDABILITY_DRAIN` is where that
    # answer lives — so the two must not be able to drift apart. Driven over a synthetic matrix that
    # walks every branch of `findability_cause`, so it holds on any machine and does not consult the
    # live ledger.
    probe_cap = {"capability_id": "t-probe"}
    exempt = {
        **probe_cap,
        "findability_category": FINDABILITY_CATEGORY,
        "findability_rationale": "a rail invokes it unconditionally",
    }
    bound = {"bound_surfaces": {"t-probe": ["t-x"]}}
    matrix = [
        (probe_cap, {}),  # bound_nowhere
        (probe_cap, {**bound, "reached_surfaces": {"t-x"}}),  # bound_and_consulted
        (probe_cap, {**bound, "reached_surfaces": {"t-other"}}),  # bound_to_unconsulted_surface
        (probe_cap, {**bound, "reached_surfaces": set()}),  # reach_not_evaluated
        (exempt, {}),  # declared_no_surface
    ]
    seen = set()
    for cap_probe, ctx_probe in matrix:
        cause, detail = findability_cause(cap_probe, ctx_probe)
        seen.add(cause)
        assert detail.strip(), f"cause {cause!r} returns no detail at all"
        assert cause in FINDABLE_OK or cause in FINDABILITY_DRAIN, (
            f"cause {cause!r} is a failure with no entry in FINDABILITY_DRAIN — the report would "
            "then name a backlog it cannot say how to clear"
        )
        if cause in FINDABILITY_DRAIN:
            assert (
                FINDABILITY_DRAIN[cause] in detail
            ), f"cause {cause!r} does not carry its own fix into the message a caller reads"
    assert len(seen) == len(matrix), f"the matrix does not reach every branch: {sorted(seen)}"

    # THE ENFORCEMENT DATE MUST NOT BE IN THE FUTURE. `GRANDFATHERED_BEFORE` was first written four
    # days ahead, which grandfathered brand-new capabilities and made the gate check nothing; the
    # same mistake in a per-requirement date would be invisible, since it silences only one
    # requirement rather than all of them.
    for name, when in REQUIREMENT_ENFORCED_FROM.items():
        assert when <= _now(), (
            f"{name!r} is enforced from {when}, which is in the FUTURE — every capability, "
            "including brand-new ones, would pass it as pre-cutoff"
        )
        assert dict(REQUIREMENTS).get(name), f"{name!r} has an enforcement date but no predicate"

    # GRANDFATHERING MUST BE VISIBLE, NOT SILENT, and must not weaken the predicates themselves.
    # Legacy scoping lives on the ROW (`legacy`/`enforced`), so a pre-gate capability still reports
    # exactly what it is missing — it simply does not block the suite. Were the exemption pushed
    # into the predicates instead, the debt would read as compliance and disappear.
    # "Pre-gate" means registered before 2026-08-21 on the running instance, so this block can
    # only be exercised where that history exists. A ledger bootstrapped from the committed tree
    # has no legacy population at all.
    if env_prereq.runnable(gaps, env_prereq.ledger_legacy_rows_absent()):
        ledger_rows = report()["rows"]
        legacy_rows = [r for r in ledger_rows if r["legacy"]]
        assert legacy_rows, "expected pre-gate capabilities to be marked legacy"
        assert any(
            r["missing"] for r in legacy_rows
        ), "legacy rows must still report their missing parts, not be silently passed"
        assert all(not r["enforced"] for r in legacy_rows), "legacy rows must not block the suite"

    # A waiver must EXPIRE. An exception with no end date is how "temporary" became a month.
    for cid, waiver in WAIVERS.items():
        assert "expires" in waiver and "reason" in waiver, f"waiver for {cid} needs expiry+reason"
        assert cid in ledger, f"waiver names unknown capability {cid}"

    # COMMITMENTS: the real historical failure must be detected, not hypothetically detectable.
    # The LIVE verdict is asserted in test_capability_admission.test_dated_promises_left_an_artifact
    # (both lists empty); here the live call only has to answer at all.
    com = commitments()
    assert isinstance(com["clean"], bool)
    # `orchestrate.sh` cites the range-lane review record. That record HAS since been written, so
    # the assertion that once named it here is retired on purpose rather than quietly dropped —
    # `_probe_live_root` carries what it was actually protecting: that the default-root scan reads
    # the real tree and attributes what it finds correctly. Read its docstring before changing
    # either block; between them they cover the detector (synthetic input) and the live wiring
    # (real input), and dropping one leaves the other passing over nothing.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="cap-adm-") as td:
        probe = pathlib.Path(td)
        # POINT `AUDITS` AT A SYNTHETIC, EMPTY RECORD SET for the duration of the probe. Two
        # reasons, and neither is a skip:
        #   1. `commitments()` returns "cannot judge" when the audit ledger is absent — correctly,
        #      since record existence is unanswerable without it. But that made this block, whose
        #      whole job is to prove the DETECTOR works either way, unprovable on a machine that
        #      has no audit ledger: the first CI run died right here.
        #   2. Even where the ledger exists, the verdicts below depended on which records happen
        #      to be in it. A synthetic empty set makes all four deterministic everywhere.
        # `_probe_live_root` runs off the SAME swap for the same reason, one input further out: it
        # scans the real tree, and only an empty record set makes "every dated citation dangles"
        # true on the owner's machine and a bare runner alike.
        # This is the harness, not an assertion: every assert below is unchanged, and now runs on
        # any machine instead of only on this one.
        audits_probe = probe / "audits"
        audits_probe.mkdir()
        saved_audits = globals()["AUDITS"]
        globals()["AUDITS"] = audits_probe
        try:
            _probe_commitments(probe)
            _probe_live_root(probe)
        finally:
            globals()["AUDITS"] = saved_audits

    # preflight must report obligations rather than pretending it verified them.

    spec = {
        "capability_id": "capability:proposed-thing",
        "notes": "dedup: checked X; absent",
        "downstream_consumer": "a.py:b",
        "learning_sink": "feedback.outcomes",
        "kill_switch": "F=0",
        "rollback": "revert",
        "trigger_cadence": "daily",
    }
    # FINDABILITY IS THE ONE THE AUTHOR MUST LEARN HERE, before writing code — a proposed
    # capability no surface will offer is the 13.62% case, and discovering that after the build is
    # a second project. So a spec with the first eight parts and no surface is NOT ready to build.
    pf_unbound = preflight(spec)
    assert not pf_unbound["ready_to_build"], pf_unbound
    assert pf_unbound["declarable_missing"] == ["findable"], pf_unbound
    assert "bound_nowhere" in pf_unbound["checks"]["findable"]["detail"], pf_unbound
    # ...and the OLD exemption (category AND rationale) no longer clears it: since 2026-09-02 a
    # rail is bound on an exercise phase, and the verdict names that fix.
    pf_exempt = preflight(
        {
            **spec,
            "findability_category": FINDABILITY_CATEGORY,
            "findability_rationale": "a rail invokes it unconditionally; it is never offered",
        }
    )
    assert not pf_exempt["ready_to_build"], pf_exempt
    assert pf_exempt["declarable_missing"] == ["findable"], pf_exempt
    assert "no longer exempts" in pf_exempt["checks"]["findable"]["detail"], pf_exempt
    # Declaring the exercise binding as INTENT makes findability an obligation, not a pass.
    pf = preflight(
        {
            **spec,
            "findability_category": EXERCISE_CATEGORY,
            "findability_rationale": "a rail invokes it; exercised read-only on a fixture",
        }
    )
    assert pf["ready_to_build"], pf
    assert set(pf["obligations"]) == {"caller_exists", "heartbeat", "fixture", "findable"}, pf
    assert all(pf["checks"][o]["ok"] is None for o in pf["obligations"]), pf
    assert "rail-exercise" in pf["checks"]["findable"]["detail"], pf
    # The category ALONE must not clear it, exactly as for the kill-switch categories.
    pf_bare_category = preflight({**spec, "findability_category": FINDABILITY_CATEGORY})
    assert not pf_bare_category["ready_to_build"], pf_bare_category
    pf2 = preflight({"capability_id": "capability:bare"})
    assert not pf2["ready_to_build"] and "kill_switch" in pf2["declarable_missing"], pf2

    env_prereq.report_gaps("capability_admission.py", gaps)
    print(
        "capability_admission.py selftest: OK (every requirement can fail and can pass, "
        "grandfathering visible, per-requirement cutoffs are in the past, findability is "
        "declarable pre-build, waivers expire, dangling + overdue commitments detected, "
        "live-tree scan proven non-vacuous and correctly attributed)"
        + (f" — {len(set(gaps))} section(s) skipped, see above" if gaps else "")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--commitments", action="store_true", help="only the dated-promise check")
    ap.add_argument(
        "--preflight",
        metavar="JSON",
        help="answer admission for a proposed capability spec (JSON string or @file)",
    )
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return 0
    if args.preflight:
        raw = args.preflight
        if raw.startswith("@"):
            raw = pathlib.Path(raw[1:]).read_text(encoding="utf-8")
        print(json.dumps(preflight(json.loads(raw)), indent=2, sort_keys=True))
        return 0
    if args.commitments:
        com = commitments()
        print(
            json.dumps(com, indent=2, sort_keys=True)
            if args.json
            else format_report(
                {
                    "total": 0,
                    "admitted": 0,
                    "enforced_total": 0,
                    "enforced_failing": [],
                    "legacy_debt": [],
                    "rows": [],
                    "commitments": com,
                }
            )
        )
        return 0 if com["clean"] else 1
    rep = report()
    print(json.dumps(rep, indent=2, sort_keys=True) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
