#!/usr/bin/env python3
"""capability_advisor.py — "Is the Orchestrator useful for this task, and which capabilities apply?"

THE FRONT DOOR. Everything else in the capability stack makes the answer CORRECT (matchers,
attribution, readiness); nothing asked the question. This does, from free text, at the moment a task
arrives — so a session handed "please use Orchestrator if appropriate" has something concrete to
consult instead of guessing.

DESIGN COMMITMENTS, each learned from a failure in this subsystem:

1. IT MUST BE ABLE TO SAY NO. An advisor that always finds something to recommend is worthless —
   worse, it trains you to ignore it. `useful: false` is a first-class, common answer.

2. ADVICE IS NOT DISPATCH. `capabilities.capability_routing_decision` additionally requires
   status=='active' AND immutable version lineage, and today 0 of 33 capabilities have either — so a
   dispatch-strict query returns an empty set for every task, permanently. Advice asks the weaker,
   useful question ("would this capability's declared trigger match this work, and is it usable?")
   and labels each answer with what would have to be true to actually run it. The two are never
   conflated: `dispatch_ready` is reported per capability and is presently false everywhere.

3. LOW CONFIDENCE IS REPORTED, NOT HIDDEN. Task classification is keyword-based and deterministic
   (no model call — this must be fast, offline, and reproducible). When nothing classifies well the
   answer says so rather than guessing a task_type and confidently matching against it.

    python3 capability_advisor.py "add tests for the retry helper"
    python3 capability_advisor.py --json "audit the whole repo for dead code"
    python3 capability_advisor.py --selftest

Also exposed as the `capability_advice` MCP tool, so any session can ask at task initiation.
"""

from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import sys
import time
from typing import Any

import capabilities
import env_prereq

# Free text -> the task_type vocabulary the fleet actually records. Deterministic and inspectable;
# a model call here would make the same task classify differently on different days, which would
# make the historical match counts meaningless.
TASK_SIGNALS: dict[str, tuple[str, ...]] = {
    # "testgen" is spelled out because the trailing word boundary stops `test` matching it, and
    # the lane's own name is exactly how the work gets described ("run the testgen lane").
    "testgen": ("test", "tests", "unit test", "coverage", "pytest", "test case", "testgen"),
    # `offload` was ABSENT until 2026-08-19 while being the fleet's most-used capability by 20x
    # (196 runs/week vs 2 for the next). So the front door could never recommend the one thing the
    # Orchestrator is used for most: spending a cheap agent's context instead of this seat's.
    "offload": (
        "offload",
        "summarise",
        "summarize",
        "read these",
        "large context",
        "200 pages",
        "delegate this",
        "conserve capacity",
        "cheap agent",
        "token-heavy",
        "big read",
    ),
    # Real campaign language, taken from the Trend_Model_Project legacy-removal issues that this
    # advisor previously failed to classify at all ("remove the legacy config shapes ... phase 6").
    # The old signals ("rename across", "mass change", "sweep") matched none of the actual work.
    "codemod": (
        "codemod",
        "refactor",
        "rename across",
        "mass change",
        "sweep",
        "migrate all",
        "legacy removal",
        "remove legacy",
        "remove remaining",
        "remove retired",
        "remove duplicate",
        "consolidate",
        "deduplicate",
        "dedupe",
        "extract shared",
        "across the whole repo",
        "phase 2",
        "phase 3",
        "phase 4",
        "phase 5",
        "phase 6",
        "phase 7",
        "phase 8",
    ),
    "review": ("review", "critique", "assess", "evaluate", "audit", "check quality"),
    # "screenshot" spelled out for the same reason as "testgen". The boundary plus the
    # no-inflection rule for initialisms is what stops `ui` matching "uid" and `ux` "uxbridge".
    "ux_review": ("ux", "usability", "frontend", "ui", "screen", "user interface", "screenshot"),
    "epic": ("epic", "break down", "decompose", "roadmap", "plan out", "vague goal"),
    "cross_repo": ("cross-repo", "across repos", "consumer repos", "fleet-wide", "both repos"),
    "runtime_ac": ("acceptance criteria", "runtime check", "verify behaviour", "verify behavior"),
    "docs": ("docs", "documentation", "readme", "changelog", "docstring"),
    # "formatting" doubles the final consonant, so `format` + an inflection cannot reach it.
    "mechanical": ("lint", "format", "formatting", "dependency bump", "bump version", "typo"),
    "implement": ("implement", "add feature", "build", "fix bug", "fix the", "write code"),
}
# A task must clear this to be treated as classified at all.
MIN_SIGNAL_HITS = 1

# Inflections that keep a signal's INTENT, so they still count as a hit: plurals, participles and
# agent/result nouns. What is deliberately EXCLUDED is derivational drift — above all `-ation`,
# which turns a verb into the name of a thing that already exists. That distinction is the whole
# point: "implement the exporter" is work to do, "the implementation of the loader" is a noun in a
# READ-ONLY audit, and with MIN_SIGNAL_HITS = 1 the substring was enough to bind a code-mutating
# lane to an audit that must not touch code. Observed in a real run: experiment advice:a6cc531b8010
# classified "a read-only audit of the implementation ..." as task_type `implement`.
SIGNAL_INFLECTIONS = ("s", "es", "ing", "ed", "d", "er", "ers", "ment", "ments")


def _signal_pattern(signal: str) -> str:
    """Whole-word-with-intent match for one signal.

    Bounded on BOTH sides. The leading `(?<![a-z])` was already here; the trailing boundary is what
    was missing, so every signal matched as a bare prefix — `ui` hit "uid", `test` hit "testgen",
    and `implement` hit "implementation". Inflections listed in SIGNAL_INFLECTIONS still count,
    because they preserve intent; anything else does not. Where tightening would have lost a form
    that genuinely IS a signal, the form is spelled out in TASK_SIGNALS instead of loosened here —
    the same way that table already lists "tests" beside "test" and "documentation" beside "docs".

    Two-letter signals take NO inflection, because they are initialisms and initialisms do not
    inflect. Without that carve-out `ui` still matched "uid" through the bare `d` ending (which is
    there for the -e verbs: dedupe -> deduped), so the trailing boundary would have LOOKED like it
    fixed a false positive it had not fixed.
    """
    endings = "" if len(signal) <= 2 else rf"(?:{'|'.join(SIGNAL_INFLECTIONS)})?"
    return rf"(?<![a-z]){re.escape(signal)}{endings}(?![a-z])"


def classify_task(text: str) -> list[dict]:
    """Candidate task types with the phrases that triggered them. Deterministic, order-stable."""
    low = f" {(text or '').lower()} "
    found = []
    for task_type, signals in TASK_SIGNALS.items():
        hits = [s for s in signals if re.search(_signal_pattern(s), low)]
        if len(hits) >= MIN_SIGNAL_HITS:
            found.append({"task_type": task_type, "hits": hits, "score": len(hits)})
    return sorted(found, key=lambda c: (-c["score"], c["task_type"]))


def _usability(cap: dict) -> dict:
    """What it would take to actually use this capability right now."""
    unblock = capabilities.unblock(cap)
    gate = capabilities.gate_readiness(cap)
    dispatch_ready = bool(
        cap.get("status") == "active"
        and cap.get("capability_version_id")
        and cap.get("artifact_hash")
        and cap.get("lifecycle_policy_hash")
        and not cap.get("rollback_pending")
    )
    return {
        "status": cap.get("status"),
        "dispatch_ready": dispatch_ready,
        "blocker": unblock["blocker"],
        "next_step": unblock["action"],
        "gate_ready": gate.get("ready") if gate.get("gated") else None,
    }


# ---------------------------------------------------------------------------
# ENTRY MODES. `capabilities._matches_trigger` matches a `{"kind": k, "name": n}` matcher against a
# SAME-NAMED FIELD THE CALLER SUPPLIES, and says so in its own comment: "Adding a new trigger kind
# is then a caller-side change, not an edit here." So the 35 capabilities this advisor could not
# name were never structurally unreachable — THIS CALLER was passing a three-field trigger
# (repository/task_type/lane) and no kind fields. Two consequences, both fixed below:
#
#   1. A caller that knows its context can supply it (`context=`) and those capabilities match.
#   2. For the rest, `_matches_trigger` ALREADY RETURNS the named reason for every non-match
#      (`closer_gate_not_in_trigger`, `env_mismatch:ORCH_X`, ...) and this function used to throw
#      all of them away. Discarding them is what turned 35 capabilities into silence — the exact
#      failure mode this project keeps re-committing. They are now reported.
# ---------------------------------------------------------------------------


def _dispatcher_task_type_capability() -> dict:
    """The dispatcher's task_type -> capability map, read from the dispatcher itself.

    Lazy + defensive on purpose: this advisor must keep answering even if the dispatcher cannot be
    imported. Returning {} then degrades reach, and the selftest that compares the two maps is what
    makes that degradation visible instead of silent.
    """
    try:
        import dispatcher

        table = getattr(dispatcher, "TASK_TYPE_CAPABILITY", None)
        return dict(table) if isinstance(table, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def direct_entry() -> dict:
    """task_type -> capability entered DIRECTLY rather than matched by a declared trigger.

    Derived from `dispatcher.TASK_TYPE_CAPABILITY` so the two halves of the system cannot disagree.
    It used to be the literal `{"offload": "offload"}` under a comment claiming it mirrored that
    map -- it did not, and the drift was load-bearing: the dispatcher routed `runtime_ac` to
    `runtime-ac-checks` while this advisor named `deliberate-break-verifier` for the same work.
    ONE constant, defined once and consumed by both, is the only shape that cannot drift.

    Module-level and returned rather than inlined so the agreement is assertable as CODE, with no
    populated ledger required -- the ledger is machine-local, and a check that needs it can only
    skip on a clean runner, which is where drift would land unnoticed.
    """
    table = dict(_dispatcher_task_type_capability())
    # `offload` is transport-kind: entered directly, but never a prompt-built lane task, so it is
    # deliberately NOT in the dispatcher's map. Adding it there would make the dispatcher record an
    # offload match while building a lane prompt.
    table.setdefault("offload", "offload")
    return table


def entry_requirement(cap: dict) -> dict:
    """What would have to be true for this capability to engage? Derived from its OWN matcher.

    Nothing is invented here: every value comes from the declared matcher. A capability that
    declares `{"kind": "ci_workflow", "name": "maint-87-docs-drift-fix-agent"}` is telling us
    exactly where it engages, and a session that is told that can act on it. Silence cannot.
    """
    m = cap.get("matcher") or {}
    if not m:
        return {"mode": "undeclared", "detail": "declares no trigger, so nothing can route to it"}
    if "field" in m:
        values = m.get("value")
        values = values if isinstance(values, list) else [values]
        return {
            "mode": "task_routed",
            "field": str(m.get("field") or ""),
            "values": [str(v) for v in values],
            "detail": f"routed by {m.get('field')} in {[str(v) for v in values]}",
        }
    if "kind" in m:
        kind = str(m.get("kind") or "").lower()
        if kind == "env":
            return {
                "mode": "env_gated",
                "flag": str(m.get("name") or ""),
                "equals": str(m.get("equals")),
                "detail": f"gated by {m.get('name')}={m.get('equals')}",
            }
        name = m.get("equals", m.get("name"))
        return {
            "mode": "entered_at",
            "kind": kind,
            "name": None if name is None else str(name),
            "detail": f"entered at {kind} {name!r}, not selected by task type",
        }
    return {
        "mode": "legacy",
        "keys": sorted(str(k) for k in m),
        "detail": f"legacy matcher over {sorted(str(k) for k in m)}",
    }


# The trigger fields this advisor will forward from `context=` into the matcher trigger. Confined to
# kinds actually declared in the ledger so a typo cannot silently become a new matching dimension.
CONTEXT_FIELDS = (
    "closer_gate",
    "role",
    "tick_phase",
    "lane_event",
    "ci_workflow",
    "test_gate",
    "feedback_event",
    "experiment_phase",
    "prompt_phase",
    "adapter",
    "transport",
    "evidence_gate",
    "supervised_trial",
    "compiled_workflow",
    "cli_subcommand",
    "issue_readiness",
    "tick_preflight",
)


def reachable_set(*, path=None) -> dict:
    """Every capability this advisor can name from free text alone, and how.

    Pinned by a selftest. `adversarial-review` and `docs-drift-fix-agent` were once reachable --
    their advisory match history proves it -- and silently dropped out when their matchers were
    tightened to `closer_gate` / `ci_workflow` shapes. Nothing noticed, because reach was never
    measured. A shrinking front door now fails a test instead of going quiet.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    out: dict[str, list[str]] = {}
    for task_type, signals in TASK_SIGNALS.items():
        advice = advise(signals[0], record=False, path=path)
        for entry in advice.get("capabilities") or []:
            out.setdefault(entry["capability_id"], []).append(task_type)
    return {
        "reachable": {k: sorted(set(v)) for k, v in sorted(out.items())},
        "reachable_count": len(out),
        "ledger_count": len(caps),
    }


def _annotate_contraindications(entries: list[dict], repository: str) -> list[str]:
    """Flag candidates this repository's own record says do not work here. Returns the flagged ids.

    THE TWO CAPABILITIES MUST DISAGREE OUT LOUD. A real audit run was offered `frontend-verifier`
    and `repo-playbook` in the same response for a repo whose own audit record says
    `frontend_verify.py` does not work against its Streamlit SPA -- and the reconciliation existed
    only in the auditor's memory. This is the seam where the playbook answers back.

    It ANNOTATES, never removes: a concealed candidate can never be selected, so it can never earn
    the evidence that would clear the contraindication -- the same reason binding does not conceal.
    Repo-scoped on purpose, too. A per-surface demotion learned here would unbind the capability for
    every OTHER repo, which is the wrong granularity for "broken against this one app".
    """
    if not repository or not entries:
        return []
    try:
        import repo_knowledge

        notes = repo_knowledge.contraindications_for(repository)
    except Exception:  # noqa: BLE001
        # Advice must still be given when the registry is unreadable, exactly like propensity.
        return []
    flagged = []
    for entry in entries:
        note = notes.get(entry["capability_id"])
        if not note:
            continue
        entry["contraindicated"] = True
        entry["contraindication_reason"] = note.get("reason") or ""
        entry["contraindication_evidence"] = note.get("evidence") or ""
        if note.get("instead"):
            entry["use_instead"] = note["instead"]
        flagged.append(entry["capability_id"])
    return sorted(flagged)


def _attach_how_to_use(entries: list[dict]) -> list[str]:
    """Stamp each entry with its `how_to_use`, and return the ids that got one.

    ONE stamping site, consumed by both the answer and `format_advice`, because `HOW_TO_USE` used to
    be read only by the text rendering — so the MCP callers, which are every real consult, never saw
    it. A second lookup at the render site is how the two would drift back apart.

    Absent is not an error: most capabilities have no entry, and inventing one would be worse than
    silence. The field is always PRESENT (None when unknown) so a caller can test it.
    """
    named = []
    for entry in entries:
        how = HOW_TO_USE.get(entry["capability_id"])
        entry["how_to_use"] = how
        if how:
            named.append(entry["capability_id"])
    return named


def guidance_summary(entries: list[dict]) -> dict:
    """How much of THIS answer carries guidance, and which entries do not.

    THE DRAINABLE QUANTITY BESIDE THE BLOCKING ONE, and it exists because the alternative reading
    was tried and was wrong. A 2026-08-24 audit saw `how_to_use: null` on every capability whose
    precondition had failed and inferred a suppression branch — that the answer withholds guidance
    when a precondition fails. There is no such branch: `_attach_how_to_use` stamps unconditionally
    on both return paths. The real cause was a plain table gap (29 of 39 bound capabilities had no
    entry), and per-entry `null` cannot tell those two apart, so the wrong fix was the reasonable
    inference. A count states which one it is in the answer itself: `2 of 5 documented` is a gap in
    a table, and nothing about it suggests a mechanism to go hunting for.

    CONSUMES `_attach_how_to_use`'s own stamping rather than re-reading `HOW_TO_USE`; a second
    lookup here is how the render and the answer came apart in the first place.
    """
    undocumented = sorted(e["capability_id"] for e in entries if not e.get("how_to_use"))
    return {
        "offered": len(entries),
        "documented": len(entries) - len(undocumented),
        "undocumented": undocumented,
    }


def advise(
    text: str,
    *,
    repository: str = "",
    lane: str = "opener",
    skill: str = "",
    record: bool = True,
    path=None,
    context: dict | None = None,
    surface: str = "",
    repo_path: str = "",
) -> dict:
    """Should the Orchestrator be used for this task, and which capabilities apply?

    `skill` names the skill that surfaced this work, if any; it is recorded with each match so the
    skill -> capability association can be LEARNED rather than declared. `record=False` makes the
    call a pure query (used by tests and by dry inspection).

    `repo_path` is a checkout of `repository`, when the caller has one. It is the input that lets a
    declared repo-fact precondition actually be EVALUATED rather than merely stated — the defect
    three audit rounds hit was a conditional binding reason ("when observable surfaces exist") that
    nothing could even attempt to check. Absent it, such a precondition reports UNEVALUATED with the
    missing input named. It never guesses, and it never withholds or reorders the offer.
    """
    caps = capabilities.load(path or capabilities.REG)
    # THE SURFACE'S OWN STATE, computed once and reported on every branch. Purely additive: it
    # changes neither the candidate set nor its order, exactly like the precondition axis. What it
    # removes is one specific wrong reading — an invented name answering "nothing applies here".
    surface_state = surface_status(surface or skill, path=path)
    unknown_surface_note = (
        (
            f" — AND {surface_state['surface']!r} IS NOT A SURFACE THIS TOOL DECLARES, so this is "
            f"'no such surface', not 'nothing applies'. Declared surfaces closest to it: "
            + ", ".join(repr(s) for s in surface_state["did_you_mean"])
        )
        if surface_state["status"] == "unknown"
        else ""
    )

    # A SUPPRESSED SURFACE MUST BE ACTUALLY QUIET. `NO_BINDING` used to suppress only the DECLARED
    # half, so `repo-audit:phase-1` — whose whole point is that the playbook says "Orient (bash only,
    # NO agents)" — still offered `deliberate-break-verifier` and `testgen-lane` from the keyword
    # classifier. Found by the first real audit run against this system: "an empty-by-design surface
    # is not actually quiet". The selftest missed it because it asserted `binding_for(...) == {}` —
    # the binding — and never what a CALLER receives. Suppression now covers the whole answer,
    # because "no agents here" is a statement about the context, not about one code path.
    suppressed = binding_suppressed(surface) if surface else ""
    if suppressed:
        return {
            "task": text,
            "experiment_id": experiment_id(text),
            "useful": False,
            "confidence": "suppressed",
            "skill": skill or None,
            "surface": surface,
            "repository": repository,
            "contraindicated": [],
            "task_types": [],
            "capabilities": [],
            "dispatch_ready_count": 0,
            "bound_count": 0,
            "bound_capabilities": [],
            "not_applicable": [],
            "coverage": {
                "ledger_count": len(caps),
                "matched": 0,
                "not_applicable": 0,
                "by_entry_mode": {},
            },
            "precondition": _annotate_preconditions([], repository, repo_path),
            "surface_template": unsubstituted_surface(surface) or None,
            "surface_status": surface_state,
            "reason": f"surface {surface!r} deliberately takes no capabilities: {suppressed}",
        }

    candidates = classify_task(text)
    if not candidates:
        # A DECLARED BINDING MUST SURVIVE A CLASSIFICATION MISS. This early return used to drop
        # straight out with an empty answer, so a surface with five declared capabilities got NOTHING
        # whenever its own words did not hit the keyword vocabulary -- the binding depending on the
        # classifier, which is the single thing it exists not to do. Free text that classifies badly
        # is the common case, not the edge case.
        declared = binding_for(surface or skill, path=path)
        live = [
            (cid, why)
            for cid, why in sorted(declared.items())
            if cid in caps and caps[cid].get("status") not in {"retired", "superseded"}
        ]
        if live:
            entries = [
                {
                    "capability_id": cid,
                    "matched_task_type": None,
                    "bound": True,
                    "bound_only": True,
                    "binding_reason": why,
                    "entrypoint": caps[cid].get("entrypoint"),
                    **_usability(caps[cid]),
                }
                for cid, why in live
            ]
            precondition = _annotate_preconditions(entries, repository, repo_path)
            _attach_how_to_use(entries)
            try:
                import capability_propensity

                entries = capability_propensity.rank(entries, path=path)
            except Exception:  # noqa: BLE001
                pass
            # The classification-miss path takes the SAME contraindication pass as the main one --
            # it is the path a free-text audit consult actually lands on, so annotating only the
            # other one would leave the reported case uncovered. Runs BEFORE the result dict is
            # built and before `_record_matches`, so the recorded candidate set is the same list
            # the caller was shown.
            warned = _annotate_contraindications(entries, repository)
            if warned:
                entries.sort(key=lambda e: 1 if e.get("contraindicated") else 0)
            result = {
                "task": text,
                "experiment_id": experiment_id(text),
                "useful": True,
                "confidence": "binding_only",
                "skill": skill or None,
                "surface": (surface or skill) or None,
                "repository": repository,
                "contraindicated": warned,
                "task_types": [],
                "capabilities": entries,
                "dispatch_ready_count": sum(1 for e in entries if e["dispatch_ready"]),
                "bound_count": len(live),
                "bound_capabilities": sorted(c for c, _ in live),
                "not_applicable": [],
                "precondition": precondition,
                "guidance": guidance_summary(entries),
                "surface_template": unsubstituted_surface(surface or skill) or None,
                "surface_status": surface_state,
                "coverage": {
                    "ledger_count": len(caps),
                    "matched": len(entries),
                    "not_applicable": 0,
                    "by_entry_mode": {},
                },
                "reason": (
                    f"could not classify this task, but {len(live)} capability(ies) are "
                    f"DECLARED for surface {surface or skill!r} and apply regardless of "
                    f"classification"
                ),
            }
            if record and entries:
                # A BINDING-ONLY ANSWER IS STILL AN OBSERVATION. This branch used to return real
                # capabilities with `useful: true` and record NOTHING, so the fix that made a
                # declared binding survive a classification miss covered the ANSWER and not the
                # EVIDENCE. The consequence is a latched gate one layer down: a surface whose words
                # never hit the keyword vocabulary — the tick's cadence pass is exactly that — could
                # never accumulate a candidate set, so `capability_propensity.experiments()` saw
                # triggers and outcomes belonging to trials with zero candidates, no control arm and
                # no attributable skill. Found 2026-08-22 while wiring the tick: the trial existed
                # and was unreadable. Same call as the classified branch, same idempotency, so a
                # repeated identical question still does not inflate anything.
                #
                # AND IT MUST CARRY THE SURFACE. This branch passed `skill` only, so a caller that
                # supplied `--surface` and no skill -- which is every CLI caller, since there is no
                # `--skill` flag -- wrote `skill: null, surface: null` and its whole candidate set
                # was unattributable to the surface that produced it. That is the same
                # "recorded but unusable" defect the classified branch below already fixed, still
                # live on the branch an unclassifiable cadence consult ALWAYS takes.
                result["recorded_matches"] = _record_matches(
                    result, skill=skill, surface=surface or skill, path=path
                )
            return result
        return {
            "task": text,
            "experiment_id": experiment_id(text),
            "useful": False,
            "confidence": "none",
            "skill": skill or None,
            "repository": repository,
            "contraindicated": [],
            "task_types": [],
            "capabilities": [],
            "dispatch_ready_count": 0,
            "not_applicable": [],
            "surface": (surface or skill) or None,
            "bound_count": 0,
            "bound_capabilities": [],
            "precondition": _annotate_preconditions([], repository, repo_path),
            "surface_template": unsubstituted_surface(surface or skill) or None,
            "surface_status": surface_state,
            "coverage": {
                "ledger_count": len(caps),
                "matched": 0,
                "not_applicable": 0,
                "by_entry_mode": {},
            },
            # THE BRANCH AN INVENTED SURFACE LANDS ON. Nothing classified AND nothing was bound, so
            # the old sentence blamed the task; when the surface does not exist, the task was never
            # the problem and the caller needs the other sentence.
            "reason": (
                "could not classify this task into any work type the fleet records; "
                "no capability can be matched to it" + unknown_surface_note
            ),
        }

    # Infrastructure capabilities are ENTERED DIRECTLY, never routed by task_type, so their
    # kind-based matchers ({"kind": "transport"}) cannot match a {task_type} trigger. This map is
    # how `offload` stopped being invisible.
    #
    # It used to be a one-entry literal whose comment claimed it "Mirrors
    # dispatcher.TASK_TYPE_CAPABILITY" -- it did not, and the drift was load-bearing: the dispatcher
    # routed runtime_ac to `runtime-ac-checks` while this advisor named `deliberate-break-verifier`
    # for the same work, so the two halves of the system disagreed about the same task type. Now
    # there is ONE constant, defined in the dispatcher and consumed here, plus an explicit local
    # addition -- and `_selftest_direct_entry_tracks_dispatcher` fails if they diverge again.
    DIRECT_ENTRY = direct_entry()

    matched: list[dict] = []
    unmatched: dict[str, dict] = {}
    for candidate in candidates:
        direct = DIRECT_ENTRY.get(candidate["task_type"])
        if direct and direct in caps and not any(m["capability_id"] == direct for m in matched):
            cap = caps[direct]
            matched.append(
                {
                    "capability_id": direct,
                    "matched_task_type": candidate["task_type"],
                    "entrypoint": cap.get("entrypoint"),
                    "entered_directly": True,
                    **_usability(cap),
                }
            )
        trigger = {"repository": repository, "task_type": candidate["task_type"], "lane": lane}
        # Forward whatever context the CALLER actually knows. Absent context still fails closed --
        # this widens what CAN be answered, never what is assumed.
        for field in CONTEXT_FIELDS:
            value = (context or {}).get(field)
            if value not in (None, ""):
                trigger[field] = value
        for cap_id, cap in sorted(caps.items()):
            if cap.get("status") in {"retired", "superseded"}:
                continue
            ok, reasons = capabilities._matches_trigger(cap, trigger)
            if not ok:
                # NOT a match, and NOT silence. The reason names the entry point.
                if cap_id not in unmatched:
                    unmatched[cap_id] = {
                        "capability_id": cap_id,
                        "why_not": sorted(set(reasons)),
                        "requirement": entry_requirement(cap),
                        "status": cap.get("status"),
                    }
                continue
            entry = {
                "capability_id": cap_id,
                "matched_task_type": candidate["task_type"],
                "entrypoint": cap.get("entrypoint"),
                **_usability(cap),
            }
            if not any(m["capability_id"] == cap_id for m in matched):
                matched.append(entry)

    # Confidence describes the CLASSIFICATION, not the recommendation — a strong keyword match on a
    # capability that cannot run yet is still high-confidence advice to not bother.
    top = candidates[0]["score"]
    confidence = "high" if top >= 2 else "low"
    # RANK BY MEASURED USEFULNESS. This is the edge that makes "recommend the useful ones more
    # often" mechanical rather than aspirational: `capability_propensity` reads the
    # candidate->trigger->outcome trail this module's own `match` events start, and returns a
    # propensity that rises with the share of trials where triggering actually helped. Order only --
    # the SET is unchanged, so a bad propensity can misorder a list and nothing else. Never-tried
    # capabilities carry an optimistic prior and an unconditional floor, so ranking cannot starve
    # the capabilities that have no evidence yet; that would be a gate blocking its own drain.
    # DECLARED BINDING FIRST. A bound capability is added even if the keyword classifier missed it --
    # that is the whole point: the binding must not depend on classification working. Unbound matches
    # are kept and ranked after, never dropped, or the binding becomes a gate that starves its own
    # promotion path.
    bound = binding_for(surface or skill, path=path)
    if bound:
        present = {m["capability_id"] for m in matched}
        for cap_id, reason in bound.items():
            bound_cap: dict[str, Any] | None = caps.get(cap_id)
            if bound_cap is None or bound_cap.get("status") in {"retired", "superseded"}:
                continue
            if cap_id not in present:
                matched.append(
                    {
                        "capability_id": cap_id,
                        "matched_task_type": None,
                        "entrypoint": bound_cap.get("entrypoint"),
                        "bound_only": True,
                        **_usability(bound_cap),
                    }
                )
                unmatched.pop(cap_id, None)
        for entry in matched:
            entry["bound"] = entry["capability_id"] in bound
            if entry["bound"]:
                entry["binding_reason"] = bound[entry["capability_id"]]
    # PRECONDITIONS. Pure annotation: this adds a verdict and a reason to each entry and changes
    # neither the SET nor the ORDER. That restraint is the finding, not an oversight — the same
    # capability that was noise on two frontend-less repositories produced the highest
    # evidence-to-effort finding of a third audit on a repository that has a display surface, so two
    # negatives are not a verdict on a binding. The sort key below is deliberately unchanged.
    precondition = _annotate_preconditions(matched, repository, repo_path)
    _attach_how_to_use(matched)
    try:
        import capability_propensity

        matched = capability_propensity.rank(matched, path=path)
    except Exception:  # noqa: BLE001
        # Ranking is an enhancement, never a dependency: advice must still be given when the
        # propensity store is unreadable.
        pass
    warned = _annotate_contraindications(matched, repository)
    if bound or warned:
        # Stable partition: bound set first in propensity order, then the rest in propensity order.
        # Small-and-specific beats long-and-general -- that is the measured effect this implements.
        # A documented per-repo contraindication ranks LAST WITHIN its partition: still offered, so
        # it can still be chosen and still earn evidence, but no longer the first thing read.
        matched.sort(
            key=lambda m: (
                0 if (m.get("bound") or not bound) else 1,
                1 if m.get("contraindicated") else 0,
            )
        )
    usable = [m for m in matched if m["dispatch_ready"]]
    # A capability that matched for ANY classified task type is not "not applicable".
    for entry in matched:
        unmatched.pop(entry["capability_id"], None)
    not_applicable = sorted(unmatched.values(), key=lambda r: r["capability_id"])
    # REPORT THE WHOLE DENOMINATOR (ADDING_CAPABILITIES.md standing rule 5). The old response
    # returned only the matches, so 35 of 41 capabilities were absent with no reason given -- which
    # reads identically to "there was nothing else". Grouping by entry mode turns that silence into
    # an inspectable answer: what exists, and what would make each one engage.
    by_mode: dict[str, list[str]] = {}
    for row in not_applicable:
        by_mode.setdefault(row["requirement"]["mode"], []).append(row["capability_id"])
    result = {
        "task": text,
        "experiment_id": experiment_id(text),
        "useful": bool(matched),
        "confidence": confidence,
        "skill": skill or None,
        "repository": repository,
        "task_types": [c["task_type"] for c in candidates],
        "classification_evidence": {c["task_type"]: c["hits"] for c in candidates},
        "capabilities": matched,
        "dispatch_ready_count": len(usable),
        "surface": (surface or skill) or None,
        "contraindicated": warned,
        "bound_count": len(bound),
        "bound_capabilities": sorted(bound),
        "not_applicable": not_applicable,
        # THE AXIS, reported rather than acted on. `unmet` names what was offered anyway and can now
        # be dismissed in one line instead of investigated — the cost three audit rounds actually
        # paid. `unevaluated` names what could not be checked AND the input that was missing, so
        # "nothing failed" and "nothing was checked" can never read alike.
        "precondition": precondition,
        "guidance": guidance_summary(matched),
        "surface_template": unsubstituted_surface(surface or skill) or None,
        "surface_status": surface_state,
        "coverage": {
            "ledger_count": len(caps),
            "matched": len(matched),
            "not_applicable": len(not_applicable),
            "by_entry_mode": {k: sorted(v) for k, v in sorted(by_mode.items())},
        },
        "reason": (
            f"{len(matched)} capability(ies) declare a trigger matching "
            f"{', '.join(c['task_type'] for c in candidates)}"
            + (
                "; none is dispatch-ready yet, so treat these as advisory"
                if matched and not usable
                else ""
            )
            if matched
            else f"classified as {', '.join(c['task_type'] for c in candidates)}, but no capability "
            f"declares a trigger for that work"
        )
        + unknown_surface_note,
    }
    if record and matched:
        # Asking the question is itself the observation that improves the answer.
        result["recorded_matches"] = _record_matches(
            result, skill=skill, surface=surface or skill, path=path
        )
    return result


# ---------------------------------------------------------------------------
# RE-ASK TRIGGERS. The question is "is the Orchestrator still the right tool", and it must be asked
# again when the WORK CHANGES, never on a clock. A timer-driven advisory fires during stretches
# where nothing has changed, which is how a surface trains you to ignore it. Every condition below
# is derived from observable state, so it fires on change and is silent otherwise.
# ---------------------------------------------------------------------------


def should_reask(previous: dict | None, current_context: dict) -> dict:
    """Should the advisor be consulted again? Returns {reask, reasons}.

    Stateless: the caller holds the last advice and the current context, so this stays testable and
    imposes no storage. `current_context` carries {task, repository, skill, capabilities_ready}.
    """
    reasons: list[str] = []
    if not previous:
        return {"reask": True, "reasons": ["no_advice_yet"]}

    # The work reclassified — e.g. an "implement" task that has moved on to writing tests. This is
    # the highest-value trigger: it is exactly when a different capability becomes relevant.
    now_types: set[str] = {
        c["task_type"] for c in classify_task(str(current_context.get("task") or ""))
    }
    was_types = set(previous.get("task_types") or [])
    if now_types and now_types != was_types:
        reasons.append(f"task_reclassified:{','.join(sorted(now_types - was_types)) or 'narrowed'}")

    # A skill starting is a strong, explicit statement about the kind of work now underway.
    if current_context.get("skill") and current_context.get("skill") != previous.get("skill"):
        reasons.append(f"skill_invoked:{current_context['skill']}")

    # Scope moved to a different repository.
    if (current_context.get("repository") or "") != (previous.get("repository") or ""):
        reasons.append("scope_changed")

    # Something previously advisory can now actually run — worth surfacing once.
    was_ready = int(previous.get("dispatch_ready_count") or 0)
    now_ready = int(current_context.get("capabilities_ready") or 0)
    if now_ready > was_ready:
        reasons.append("capability_became_dispatch_ready")

    return {"reask": bool(reasons), "reasons": reasons}


# ---------------------------------------------------------------------------
# DECLARED SURFACE BINDINGS — layer 1 of three, and the one that works on day one.
#
# WHY THIS EXISTS, from the published measurements rather than taste. Tool-selection accuracy falls
# with catalog size: ~84-95% at 50 tools, 41-83% at 200, near zero at 740, with a practical safe zone
# of 10-20 per reasoning context, plus a "lost in the middle" effect that drops mid-list selection to
# 22-52%. RAG-MCP measured the fix directly: exposing the FULL catalog gave 13.62% selection accuracy
# and showing only the top-3 of 15 gave 43.13% -- triple, at half the tokens. Anthropic's own
# subagent guidance names the failure too: "auto-selection is unreliable, and Claude frequently
# handles tasks in the main session even when a sub-agent's description matches the work cleanly."
#
# So a 43-capability catalog queried generically is the 13.62% condition. `learned_associations()`
# fixes it eventually but needs volume it does not have (11 observations). A DECLARED binding fixes
# it immediately, with no classifier and no history, which is why it is layer 1.
#
# THE THREE LAYERS, in order of when they start working:
#   1. this table            -- declared, per surface, 3-7 entries. Works on day one.
#   2. capability_propensity -- orders WITHIN the bound set by measured usefulness.
#   3. learned_associations  -- corrects the table over time from what a surface actually reaches for.
#
# LATCHED-GATE ANSWERS (a binding is a gate on selection, so it needs all three in writing):
#   1. WHAT DECREMENTS IT? Unbound capabilities are still RETURNED, ranked after the bound ones and
#      flagged `bound: false`. Binding is prioritisation, never concealment -- a hidden capability
#      could never be selected, so it could never earn the evidence that would bind it.
#   2. CAN THE DRAIN RUN WHILE CLOSED? Yes: an unbound capability can be selected on any round, and
#      `learned_associations()` reads what was actually used, so the promotion path runs continuously.
#   3. SAME WINDOW BOTH WAYS? Promotion and demotion both read `learned_associations()` over the same
#      history. One source, so the two directions cannot drift apart.
#
# NOT PROSE, DELIBERATELY. The recursive loop must be able to change a binding without rewriting an
# automation's prompt: `CLAUDE.md` §1 makes the manual mirror sync "the only circuit breaker between
# an agent's change and the dispatcher that dispatches those agents", and a loop that edits lane
# prompts is a self-modifying dispatch path. A surface's prompt says "consult your bound set"; the
# bound set is data. Seed lives here (committed, diffable, generalises); instance promotions live in
# the ledger (machine-local evidence), per the tool-vs-evidence split.
#
# Every entry carries WHY, because a binding with no rationale cannot be argued with later. The
# reasons below are the lane audit's findings over 4,211 recorded rounds.
# ---------------------------------------------------------------------------

# A phase that should bind NOTHING declares it, with the reason. Silent absence and deliberate
# emptiness must not look alike -- that is this repo's founding defect. `repo-audit:phase-1` is the
# case: the playbook says "Orient (bash only, NO agents)", so inheriting the surface-wide `offload`
# would contradict the skill's own instruction.
NO_BINDING = "__none__"

SURFACE_BINDINGS: dict[str, dict[str, str]] = {
    "closer-lane": {
        "adversarial-review": "its matcher IS {kind: closer_gate, name: high_stakes_review} -- built "
        "for this lane's complex-target selection, 0 invocations in 1,766 rounds",
        "partitioned-review": "the ten-class batch sweep (a-j) is a partition adjudicated in prose; "
        "review-thread work in 1,022 of 1,766 rounds",
        "runtime-ac-checks": "sweep classes (b)(c)(d) are merged-but-unverified, verifier non-PASS, "
        "and PASS-with-issue-open -- 30 fleet issues exist because merged work "
        "missed its own criteria",
        "cross-repo-coordination": "the batch sweep is cross-repo by construction; 312 rounds",
        "offload": "13 repos x 10 candidate classes every round is the largest read in the system, "
        "and the lanes account for ~0 of offload's 63 invocations",
        # THE TWO `lane_event` CAPABILITIES LAND HERE AND NOT ON THE OPENER, deliberately. Both
        # matchers are lane events about a target that has STOPPED moving, and draining a stalled
        # target is the closer's job by construction: the opener's own TOML raises `drain_needed`
        # and relays a stalled PR to the closer rather than recovering it in place, so binding a
        # stall capability to the opener would offer it at the surface that hands the work away.
        "redirect-policy": "its matcher IS `{kind: lane_event, name: stall_detected}` -- the "
        "closer's fleet discovery already classifies capacity-stuck and "
        "no-commits-for-4-hours targets by hand every round",
        "redirect-plan": "`{kind: lane_event, name: redirect_decision}` -- once a stall is "
        "classified this is the only module that turns it into a corrected "
        "prompt/switch plan instead of a fresh guess",
    },
    "opener-lane": {
        "deliberate-break-verifier": "the lane performs this exact break-then-revert proof in 271 of "
        "2,445 rounds, instructed nowhere -- 0 hits in its TOML, its "
        "rendered prompt and ~/.codex/bin, 10 in its rolling memory",
        "testgen-lane": "writes regression coverage by hand in 339 rounds",
        "codemod-campaign": "materialises phase series one issue at a time with no campaign identity "
        "(Trend #5935-#5942 is eight issues)",
        "runtime-ac-checks": "stale-checkbox defects on its own PRs are unverified acceptance criteria",
        "offload": "scans 40 durable holders and full review-thread sets per round",
    },
    # `repo-audit` is SIX PHASES, not one context, so it binds per phase. Phase attribution comes
    # from the skill's own playbook text; volume from 177 audit documents across the four
    # substantively-audited non-Orchestrator repos (deliberate-break appears in 56 of them,
    # adversarial in 25, offload in 21).
    #
    # PHASE 1 IS DELIBERATELY ABSENT. The playbook says "Orient (bash only, NO agents)" — binding a
    # capability there would contradict the skill's own instruction, and an empty binding is the
    # correct answer, not an oversight.
    # ---- the remaining surfaces. An empty binding with a REASON is a real verdict here; five of
    # the twelve skills were excluded from the advisor consult entirely, and two of those were
    # excluded precisely because a keyword classifier would misfire on them.
    "ux-review": {
        "frontend-verifier": "Gate 1 is the deterministic assert-click-assert pass that must precede "
        "the panel; the skill drives every primary surface",
        "adversarial-review": "the panel is >=4 evaluators plus an adversarial critic — the critic "
        "role is this capability",
        "offload": "mining full per-evaluator output is a large read by construction",
    },
    "implementation-verification": {
        "runtime-ac-checks": "the skill's whole job is proving each acceptance criterion landed — "
        "that IS the runtime-AC contract",
        "deliberate-break-verifier": "it checks the named test gate is present AND passing, which is "
        "the break-then-revert proof",
        "offload": "reading real squash diffs across many merged PRs is a large read",
        "role-adjudicator": "the skill's step 6 is the owner-decision check — a 'do not merge as-is' "
        "comment is a hard blocker even on a green PR — and this role's whole "
        "contract is weighing ONE blocker/veto against cited ground truth",
    },
    "file-agent-issue": {
        "deliberate-break-verifier": "AGENT_ISSUE_FORMAT requires a named test gate with a "
        "deliberate-break→revert demonstration in every filed issue",
        "runtime-ac-checks": "the issue's acceptance criteria are what a runtime-AC plan is built from",
        "role-prompt": "the role's `validate()` requires exactly what this format requires — a "
        "standalone scoped prompt, definition_of_done, acceptance criteria, "
        "validation, expected paths and out-of-scope boundaries — because an "
        "AGENT_ISSUE_FORMAT issue IS a cold-start prompt",
    },
    "cross-env-test-doctor": {
        "deliberate-break-verifier": "prescribing the canonical fix per failure class needs the fix "
        "proven to fail without it",
        "testgen-lane": "cross-env failures usually resolve into added or corrected coverage",
    },
    "latched-gate-check": {
        "switch-review": "the repo's own gate sweep already audits the held switches weekly; this "
        "skill and that capability are the same question asked two ways",
    },
    "orchestrate": {
        "offload": "the skill's prime directive is 'do the thinking; hand off the typing and the "
        "reading' — offload is that mechanism and its most-used capability",
        "windowed-capacity-policy": "it assesses capacity before routing each sub-task",
        "role-decomposer": "decomposing the request across agents is the skill's core move",
        "role-triage": "choosing which piece goes to which agent is triage",
        "role-redirect": "the skill's own description ends 'then coordinate, monitor, and redirect' — "
        "redirect under an open action space is precisely this role",
        "role-prompt": "it hands each sub-task to a cheaper agent, and the generic delegation "
        "template this role replaces is exactly what that hand-off uses today",
        "agy-runtime-isolation": "its matcher is `{kind: adapter, name: gemini}` and fires on every "
        "gemini dispatch — this skill is the surface that dispatches to "
        "gemini/cursor/vibe/codex, so it is the one that can be told the "
        "runtime is isolated",
    },
    # SUPPRESSED, each with the reason — these are verdicts, not gaps.
    "human-involvement-check": {
        NO_BINDING: "produces an attention-cost analysis and never dispatchable work; its "
        "Orchestrator mentions cite the owner-question protocol as a reference "
        "implementation, not as something to invoke.",
    },
    "scheduled-checkin": {
        NO_BINDING: "the work is ~/.codex/bin/checkin.py, a different subsystem. The Orchestrator is "
        "a CONSUMER of this skill, not a provider to it.",
    },
    "platform-handoff-brief": {
        NO_BINDING: "binding anything here would manufacture false positives: 'write a Windows TEST "
        "brief' classifies as testgen on the word 'test' alone, seeding a steady stream "
        "of associations describing work that never happened.",
    },
    "fast-venv": {
        NO_BINDING: "same failure, worse: 'pytest takes 26 minutes' classifies as testgen every "
        "time. The skill diagnoses a filesystem problem, not a coverage problem.",
    },
    "deploy-recovery": {
        NO_BINDING: "Render incident response. The advisor's own probe returns useful:false, "
        "confidence:none, and that is the correct answer.",
    },
    # ---- non-skill surfaces
    #
    # THE TICK IS FIVE PHASES, not one context, and it is sub-surfaced for the same measured reason
    # `repo-audit` is: 18 of the 43 capabilities live on this tick, and binding all 18 to `tick`
    # would recreate the too-many-tools problem INSIDE the tick. The phase names below are the
    # tick's OWN, not a taxonomy invented here -- `orchestrate.sh`'s first line reads "one
    # orchestrator tick: capacity -> discover -> plan -> dispatch", line 236 heads its second half
    # "--- Learning cadence ---", and the redirect/experiment blocks announce themselves as
    # `[cadence] redirect watch sweep` / `[cadence] redirect apply/link` and `[cadence] experiment
    # follow-up`. Several of the capabilities below carry a `{"kind": "tick_phase", "name": ...}`
    # matcher naming the very phase they are bound to, which is where the triage came from.
    #
    # THE BARE `tick` SET DOES NOT MOVE, and that is a constraint rather than a preference.
    # `capability_propensity.TICK_SURFACE` is `"tick"`, `tick_evidence()` grades exactly
    # `binding_for("tick")`, and `_selftest_tick_evidence` asserts every capability with a
    # `TICK_FINDING_FIELDS` projection is in that set. Moving these four down into a phase would
    # silently zero the only producer of layer-2 usefulness evidence in the system. They are also
    # genuinely surface-wide: "can it fire / does it fire / was it worth firing / is its switch
    # held" are questions about ANY phase of a self-observing cadence, which is the same reason
    # `offload` is declared surface-wide for `repo-audit`.
    "tick": {
        "switch-review": "already a weekly tick cadence step; bound so the tick can consult rather "
        "than only be scheduled",
        "capability-firing-monitor": "the tick is where does-it-fire is observed",
        "capability-activation-audit": "and where can-it-fire is observed",
        "capability-propensity": "the tick is the highest-volume unattended surface (~91 writes/day), "
        "so it is where propensity evidence should accrue fastest",
        "evidence-acquisition": "a `tick_phase` matcher whose declared consumer IS the "
        "`orchestrate.sh:evidence-acquisition` cadence step — the same shape as `switch-review` "
        "above, bound so the tick can consult it rather than only schedule it. Added 2026-08-23 "
        "because the findability requirement blocked it: registered after that cutoff and bound "
        "nowhere, it is the first row the new gate actually drained",
    },
    # RESOLVED 2026-08-23: two sessions disagreed about `ci`. #68's verdict is kept (below); the
    # tick sub-surfaces from the binding work are kept too — they do not overlap. A `verify.py`
    # consult was NOT added: verify.py does not CHOOSE to run the admission gate, so a consult
    # there would change nothing. That correction came from the findability requirement itself.
    # `ci` WAS A BINDING TO A SURFACE NOTHING CONSULTS, which is the first finding the findability
    # requirement produced about the tree it was added to (2026-08-23). Both former entries were
    # right about the mechanism and wrong about the axis: neither capability is ever OFFERED to a
    # reasoning context, so no amount of binding could raise its selection odds. `verify.py` runs
    # the admission gate on every PR unconditionally, and `docs-drift-fix-agent` is a Workflows
    # workflow whose invocations arrive through `capability_outcome_bridge`. Both now declare
    # `findability_category: no_surface` in `capabilities.KNOWN_DECLARATIONS`, which is the
    # honest statement, and this entry records why the surface is deliberately empty rather than
    # leaving a deleted key that reads as an oversight.
    "ci": {
        NO_BINDING: "no caller anywhere consults a `ci` surface — not verify.py, not a workflow, "
        "not a skill — so a binding here could never reach a reasoning context. Both "
        "capabilities that used to sit here are invoked UNCONDITIONALLY by a rail and "
        "declare `findability_category: no_surface` instead. If a CI-side consult is "
        "ever added, this is the entry to restore.",
    },
    "tick:capacity": {
        "issue-readiness": "the phase asks what the tick may work on at all -- `issue_readiness` is "
        "the rail that answers it, and its own `{kind: issue_readiness}` matcher "
        "fires nowhere else",
        "thompson-hybrid-routing": "the exploration policy read once capacity is known, per route "
        "decision; its matcher is the ORCH_EXPLORATION_MODE switch this "
        "phase consults",
    },
    "tick:dispatch": {
        "live-keepalive-supervisor": "its matcher IS `{kind: tick_phase, name: keepalive-stage2-plan}` "
        "-- the remote keepalive that `tick.py --active`'s label apply "
        "drives is this phase's whole output",
        "range-lane-rollout": "the daily range-lane slot is the one dispatch decision this phase "
        "makes outside the label path; gated by the switch its matcher names",
    },
    "tick:experiments": {
        "abcd-experiment": "its matcher is `{kind: experiment_phase, name: abcd}` and the phase IS "
        "`[cadence] experiment follow-up (collect+evaluate finished A/B/C runs)`",
        "synthesis-promotion": "`{kind: experiment_phase, equals: evaluated}` -- the promotion step "
        "that reads what the followup above just evaluated",
        "research-scheduler": "`{kind: tick_phase, name: research}`; the research arm fires on spare "
        "capacity inside this same block",
        "strategy-experiments": "campaign-scale experiments share the phase's arm/member identity "
        "and its capacity reservation, so they are the same reasoning context",
    },
    "tick:redirect": {
        "stall-watcher": "`{kind: tick_phase, name: watch}` -- `redirect_sweep.py` runs "
        "unconditionally every tick and stall classification is its input",
        "redirect-apply-bootstrap": "the `[cadence] redirect apply/link` step is the only consumer "
        "of a redirect plan, and the bootstrap is what authorises one",
    },
    "tick:learning": {
        "feedback-store": "the `--- Learning cadence ---` block exists to write it; every ingest "
        "step in the phase ends in `feedback.record_*`",
        "completion-event-lineage": "the pattern-miner step's first half is "
        "`feedback.py completion-events`, which is this capability's producer",
        "evidence-acquisition": "`{kind: tick_phase, name: evidence-acquisition}` names its own "
        "cadence step in this block",
        "feature-reflection-cli": "`{kind: tick_phase, name: reflection}`; the daily `feature-scan` "
        "step is the reflection pass over reusable structures",
    },
    # DELIBERATELY UNBOUND, DECLARED RATHER THAN ABSENT. `local-model-profile-trial` is the one
    # ledger row no surface may offer, and silence would be indistinguishable from an oversight --
    # this repo's founding defect. Expressed as a suppressed surface (the trial's own quarantine
    # context) so the reason is DATA that `binding_suppressed()` returns, not a comment.
    "local-model-profile-trial": {
        NO_BINDING: "quarantine-only by design (CLAUDE.md 2): the model-profile trial transport runs "
        "only through model_profile_trial_bridge.py on the pinned read-only Workflows "
        "runner, normal keepalive must REJECT a trial profile, and Brain ingestion is off "
        "until its multi-row write is atomic. Offering it at any surface would invite "
        "routing learning through unquarantined execution, so no surface binds the "
        "`local-model-profile-trial` capability and this states that as the verdict.",
    },
    "repo-audit": {
        "offload": "whole-repo reads are the canonical offload case, and the audit is the biggest "
        "read in the system; applies across phases, so declared surface-wide",
    },
    "repo-audit:phase-1": {
        NO_BINDING: "the playbook says 'Orient (bash only, NO agents)'. Binding anything here would "
        "contradict the skill's own instruction; the empty set is the correct answer.",
    },
    "repo-audit:phase-2": {
        "role-decomposer": "phase 2 splits the work across 8 named dimensions — that split IS "
        "decomposition, currently done by hand in the prompt",
        # `partitioned-review` USED TO BE BOUND HERE, on the word "partition". It moved to phase 3
        # (2026-08-23) after a real audit run declined it as "wrong phase, not wrong tool". The
        # binding matched a NAME, not a SHAPE: `partitioned_review.validate_corpus` takes a list of
        # `assertion` items with `source_refs` and disposes each one
        # `satisfied|remaining|partial|intentional|historical_only|unresolved|not_applicable`
        # against `current_code`. That is a corpus of prior CLAIMS being reconciled, and phase 2
        # discovers defects in source — there is no claim list yet for it to dispose.
        "repo-playbook": "the audit runs against 13 repos with different conventions; "
        "repo_knowledge.py is exactly that per-repo context",
        "frontend-verifier": "dimension 4 uses the ux-review-overlay when observable surfaces "
        "exist, which is what this gate checks",
    },
    # THE 8 DIMENSIONS OF PHASE 2, bound individually — and modeled as SIBLINGS of `phase-2`, not
    # children of it. The playbook splits phase 2 into "subsystem agents + cross-cutting agents, each
    # path-scoped": one ORCHESTRATING context that does the splitting (that is `phase-2`, which binds
    # role-decomposer and partitioned-review) and EIGHT WORKER contexts that each analyse one
    # dimension. A worker has no business inheriting the splitter's capabilities, and making
    # dimensions children of `phase-2` would push every worker's context to 9-10 entries — back
    # toward the problem the binding removes.
    #
    # Dimensions with no entry here inherit only the surface-wide `offload`, which is the right
    # answer for them: d1 (code quality) finds defects rather than proving fixes — the proof is
    # phase 4 — and d7 (tools worth integrating) is cost/benefit research, which offload already
    # covers. Absent is a verdict here, not an omission.
    "repo-audit:dimension-2": {
        "codemod-campaign": "the dimension IS 'duplication & consolidation ... root-vs-template "
        "drift'; consolidating a duplicated shape across a repo is codemod work",
    },
    "repo-audit:dimension-3": {
        "runtime-ac-checks": "'trace config key -> loader -> consumer' with direction checks "
        "('tighter limit => more breaches') is behavioural verification of "
        "wiring, which is what a runtime-AC plan encodes",
    },
    "repo-audit:dimension-4": {
        "frontend-verifier": "the dimension requires rendered evidence and driven scenarios on any "
        "observable surface — Gate 1's assert-click-assert pass",
        "adversarial-review": "the ux-review overlay's panel includes an adversarial critic",
    },
    "repo-audit:dimension-5": {
        "offload": "the playbook names the mechanism outright: 'research briefs (offload to web "
        "agents; good ROI)'",
    },
    "repo-audit:dimension-6": {
        "feature-scan": "'adjacent problems the existing machinery almost solves' is exactly what "
        "feature_scan.py reports — reusable structures the registry has never seen",
    },
    "repo-audit:dimension-8": {
        "capability-activation-audit": "the dimension audits 'local skills/automations — efficiency, "
        "token-sinks, human-touchpoints', which is can-it-fire",
        "capability-firing-monitor": "and did-it-fire",
        "capability-propensity": "and was-it-worth-firing; this dimension is where a surface's own "
        "false negatives surface",
        "switch-review": "held switches are the canonical automation token-sink and human-touchpoint",
    },
    "repo-audit:phase-3": {
        "adversarial-review": "the phase IS 'adversarially verify each finding against the live "
        "tip'; appears in 25 of 177 audit documents, done by hand",
        "partitioned-review": "the phase takes N candidate findings and disposes each against the "
        "live tip, which IS this module's corpus shape — `assertion` items "
        "with `source_refs`, dispositions satisfied/remaining/partial/"
        "intentional/historical_only/unresolved/not_applicable, evidence "
        "typed `current_code`, and a `confirmed_defects` category. Moved "
        "here from phase-2, where it matched the word 'partition' and not "
        "the shape; the 2026-08-22 audit of Trend used it for exactly this",
    },
    "repo-audit:phase-4": {
        "deliberate-break-verifier": "phase 4 REQUIRES 'a named test gate + deliberate-break→revert' "
        "on every filed issue — it appears in 56 of 177 audit "
        "documents, the dominant pattern, and never as an invocation",
        "testgen-lane": "the named test gate phase 4 demands is testgen work",
        "role-triage": "'prioritize + file' is triage over verified findings",
    },
    "repo-audit:phase-5": {
        "capability-propensity": "phase 5 reconciles and proves nothing was silently dropped; "
        "recording which capabilities helped belongs here",
    },
    # THE FIX ARC, and the two entries added 2026-08-25 are the measured half of it. Three
    # independent implementation runs entered this surface for real — the first runs ever to supply
    # the filed issue and commit target the delivery capabilities need — and both additions come
    # from what those runs DID, not from what looked plausible.
    "repo-audit:fix": {
        "codemod-campaign": "the fix arc is where consolidation findings become sweeping changes",
        "epic-decomposition": "a large audit finding becomes an epic before it becomes PRs",
        "testgen-lane": "fixes need the coverage the audit said was missing",
        # TRIGGERED AND USEFUL HERE SIX TIMES ACROSS THREE RUNS, while reaching the caller only
        # through the keyword classifier — so the one consult whose free text missed the vocabulary
        # (`verbatim console record`, `red`, `green`, no `pytest`) was not offered the capability it
        # then used successfully on that very issue. The binding is the layer that does NOT depend
        # on classification, and this is what its absence costs. The matcher was never widened:
        # widening `TASK_SIGNALS` to raise a hit rate corrupts the learned associations.
        "deliberate-break-verifier": "a fix arc must prove its gate fails without the fix; used and "
        "scored useful on every implementation issue this surface has ever seen",
        # A UI FIX AT THIS SURFACE CLASSIFIES AS `ux_review` AND HAD NOWHERE TO GO. `frontend-verifier`
        # was bound to `ux-review`, `repo-audit:phase-2` and `repo-audit:dimension-4` only, so the
        # run had to consult a DIFFERENT surface to be offered the one instrument that verifies UI.
        # Its own precondition (an observable surface must exist) still annotates the offer, so a
        # repo with no UI dismisses it in one line rather than investigating it.
        "frontend-verifier": "a fix whose finding was OBSERVED needs its proof observed too; the fix "
        "arc is exactly where rendered-output evidence belongs",
    },
}


def binding_for(surface: str, *, path=None, promoted: dict | None = None) -> dict[str, str]:
    """The declared bound set for a surface, plus any promotions this instance has learned.

    PHASE-SCOPED. A surface may be a bare name (`closer-lane`) or a phase within a long process
    (`repo-audit:phase-3`). A phase key resolves to the phase's own entries MERGED with the bare
    surface's, so a capability needed throughout is declared once.

    Why phases exist at all: `repo-audit` runs six phases and legitimately wants ~12 capabilities
    across the whole arc. Binding all 12 to `repo-audit` would recreate the too-many-tools problem
    INSIDE the skill — the measured safe zone is 10-20 per reasoning CONTEXT, and each phase is a
    context. Binding 2-4 per phase keeps every context small while covering the whole process.

    Seed comes from the committed table; promotions are read from the ledger, so an instance can grow
    its own bindings without a code change and without touching any prompt.

    `promoted` is an OPTIONAL pre-read promotion index (see `_promoted_index`). Passing it makes a
    sweep over every surface read the ledger once instead of once per prefix per call — a 43x30
    inverse lookup took minutes against the live ledger without it. Omitted, the behaviour is
    exactly as before.
    """
    if not surface:
        return {}
    # EVERY prefix, least specific first. `split(":", 1)` skipped the middle level, so a
    # three-part key like `a:b:c` inherited from `a` and silently missed `a:b`.
    parts = surface.split(":")
    keys = [":".join(parts[: i + 1]) for i in range(len(parts))]
    # A phase declaring NO_BINDING suppresses inheritance entirely, so "deliberately empty" can
    # actually be expressed. Checked before merging, or the surface-wide entries would leak in.
    if NO_BINDING in (SURFACE_BINDINGS.get(keys[-1]) or {}):
        return {}
    out: dict[str, str] = {}
    # Phase entries win on conflict: the more specific declaration is the more considered one.
    for key in keys:
        for cap_id, reason in (SURFACE_BINDINGS.get(key) or {}).items():
            if cap_id == NO_BINDING:
                continue
            out[cap_id] = reason
        for cap_id, reason in _promoted_bindings(key, path=path, index=promoted).items():
            out.setdefault(cap_id, reason)
    return out


def suppressed_reason_in(advice: dict) -> bool:
    """Does this advice explain that its surface is deliberately empty?"""
    return "deliberately takes no capabilities" in str(advice.get("reason") or "")


def binding_suppressed(surface: str) -> str:
    """Why this surface deliberately binds nothing, or '' if it is not suppressed.

    Exposed so a caller can report "nothing here, on purpose, because X" rather than reporting the
    same empty answer it would give for a surface nobody has bound yet.
    """
    entry = SURFACE_BINDINGS.get(surface) or {}
    return str(entry.get(NO_BINDING) or "")


def _promoted_index(path=None) -> dict[str, dict[str, str]]:
    """Every promotion this instance has learned, surface -> {capability: reason}. ONE ledger read.

    Split out of `_promoted_bindings` so a sweep over every surface pays the ledger cost once. The
    per-surface function still exists and still answers identically; this is the shared read, not a
    second source.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    index: dict[str, dict[str, str]] = {}
    for cap_id, cap in caps.items():
        for event in cap.get("event_history") or []:
            meta = event.get("metadata") or {}
            if meta.get("source") == "binding_promotion" and meta.get("surface"):
                index.setdefault(str(meta["surface"]), {})[cap_id] = str(
                    meta.get("reason") or "promoted from observed use"
                )
    return index


def _promoted_bindings(surface: str, *, path=None, index: dict | None = None) -> dict[str, str]:
    """Bindings this instance promoted from observed use. Machine-local evidence, never committed."""
    idx = _promoted_index(path) if index is None else index
    return dict(idx.get(surface) or {})


# ---------------------------------------------------------------------------
# WHO ACTUALLY ASKS — the other half of a binding, and the half nothing declared.
#
# The table above says which capabilities a surface should be OFFERED. Nothing said which surfaces
# are ever ASKED, and the two are independent: from a capability's point of view, a binding to a
# surface no caller consults is indistinguishable from no binding at all. Measured 2026-08-23 over
# the 43-row ledger, and all three shapes are live:
#
#   * `ci` binds `capability-admission-gate` and `docs-drift-fix-agent`, and NOTHING consults a `ci`
#     surface anywhere — not `verify.py`, not a workflow, not a skill.
#   * `opener-lane` and `closer-lane` bind ten capabilities between them, and both lane prompts DO
#     consult the advisor — with no surface at all:
#     `capability_advisor.py --json --lane opener --repository <r> '<work>'`. `binding_for("")` is
#     `{}`, so the declared set never reaches the caller the declaration was written for.
#   * `repo-audit` binds `offload` surface-wide and is never consulted under its bare name — and
#     that one is CORRECT, because every consult happens at a phase key whose resolution merges the
#     parent's entries. So "not consulted" is a defect only for a key that is not a PREFIX of a
#     consulted key, and `consulting_surfaces()` encodes exactly that difference.
#
# DECLARED, AND FALSIFIABLE. A consult site is a claim about a file, so each entry names the caller
# and the surface literal it passes, and `_selftest_findability` opens the file and checks. In-tree
# callers are verified on every machine, CI included. External callers — skill prompts under
# `~/.claude/skills`, lane prompts under `~/.codex/automations` — are verified where they exist and
# reported UNVERIFIED where they do not. Absence is never read as refutation: doing so would strand
# every skill-bound capability on a fresh clone, which is a gate red on arrival. Same three-valued
# discipline as the precondition axis, same rule as `capability_admission.commitments()` — no
# ledger, no verdict. A caller that is PRESENT and no longer names its surface is different: that is
# DRIFT, it drops out of `reached`, and the selftest fails on it.
#
# WHY NOT DERIVE IT ENTIRELY. The consulting callers are prompts OUTSIDE this repository, so a fresh
# clone can derive nothing at all. A committed table answers the same way on every machine and shows
# a change in a diff; a derivation that silently returned the empty set on CI would make the
# findability gate inert exactly where it has to bite.
# ---------------------------------------------------------------------------
CONSULT_SITES: dict[str, dict] = {
    "tick": {
        "caller": "capability_propensity.py",
        "literal": "TICK_SURFACE",
        "how": "`tick-evidence` consults on every tick with surface=TICK_SURFACE. IN-TREE, so this "
        "site is verified on every machine, CI included",
    },
    # A DERIVED FAMILY, declared once. `tick_phase_surfaces()` enumerates these from
    # TICK_PHASE_PREFIX, so no caller names them literally and a per-surface entry could never be
    # verified. `instances` is the mechanism for exactly that: the family is the claim, the instances
    # are what it covers, and orchestrate.sh's `ORCH-ANCHOR: tick-phase-consult` iterates the same
    # function below the heartbeat export. IN-TREE, so verified on every machine including CI.
    "tick:*": {
        "caller": "capability_advisor.py",
        "literal": "TICK_PHASE_PREFIX",
        "instances": [
            "tick:capacity",
            "tick:dispatch",
            "tick:experiments",
            "tick:learning",
            "tick:redirect",
        ],
        "how": "orchestrate.sh iterates tick_phase_surfaces() and consults each phase",
    },
    "orchestrate": {
        "caller": "~/.claude/skills/orchestrate/SKILL.md",
        "how": "task-initiation consult naming its own surface",
    },
    "ux-review": {
        "caller": "~/.claude/skills/ux-review/SKILL.md",
        "how": "task-initiation consult naming its own surface",
    },
    "file-agent-issue": {
        "caller": "~/.claude/skills/file-agent-issue/SKILL.md",
        "how": "task-initiation consult naming its own surface",
    },
    "implementation-verification": {
        "caller": "~/.claude/skills/implementation-verification/SKILL.md",
        "how": "task-initiation consult naming its own surface",
    },
    "cross-env-test-doctor": {
        "caller": "~/.claude/skills/cross-env-test-doctor/SKILL.md",
        "how": "task-initiation consult naming its own surface",
    },
    "latched-gate-check": {
        "caller": "~/.claude/skills/latched-gate-check/SKILL.md",
        "how": "task-initiation consult naming its own surface",
    },
    # THE PHASE FAMILIES. The skill's own surface table says "pass the phase as the surface" and
    # "pass this in each dimension AGENT's prompt, N=1..8", so the literal in the file is the family
    # name while the surfaces actually passed are its instances. The instances are enumerated rather
    # than matched by prefix, so a diff reviews them — a prefix rule would silently admit a phase
    # nobody consults, which is the very thing being measured.
    "repo-audit:phase-N": {
        "caller": "~/.claude/skills/repo-audit/SKILL.md",
        "instances": [f"repo-audit:phase-{n}" for n in range(1, 6)],
        "how": "the skill consults once per phase, passing `repo-audit:phase-N`",
    },
    "repo-audit:dimension-N": {
        "caller": "~/.claude/skills/repo-audit/SKILL.md",
        "instances": [f"repo-audit:dimension-{n}" for n in range(1, 9)],
        "how": "phase 2 fans out to eight dimension agents, each passed its own surface",
    },
    "repo-audit:fix": {
        "caller": "~/.claude/skills/repo-audit/SKILL.md",
        "how": "named in the skill's surface table for the follow-up arc, and ENTERED FOR REAL "
        "since 2026-08-25 — three independent implementation runs consulted it nine times "
        "between them, with filed issues and commit targets. This note previously recorded the "
        "opposite ('named but no run reaches it, since an audit ends at phase 5 and hands "
        "implementation to the lanes'), which was true when written and became a cached reason "
        "outliving its evidence. The limit it described is still the real one: only "
        "`capability_propensity`'s trials can tell NAMED from ENTERED, never a table of files",
    },
}


# BOUND, AND KNOWN NOT TO BE CONSULTED — the third declared state, and an incident record rather
# than a permission. A surface has exactly three honest states: a caller consults it
# (`CONSULT_SITES`), it deliberately binds nothing (`NO_BINDING`), or it holds bindings that nothing
# can ever reach. The third one is what `ci` was, silently, and `_selftest_findability` now fails on
# any surface in that state which is not listed here — so restoring a binding to an unconsulted
# surface is a loud failure at the point of the change instead of a wrong verdict months later.
#
# NOT A WAIVER. Capabilities bound only to a surface listed here still fail `req_findable` as
# `bound_to_unconsulted_surface`; this table records that WE KNOW, with the reason and the fix, so
# the difference between an acknowledged defect and an oversight is legible. When a consult is added,
# MOVE the entry into `CONSULT_SITES` — the selftest will tell you if you forget.
KNOWN_UNCONSULTED: dict[str, str] = {
    # THE DEFECT IS FIXED; THE ENTRIES STAY, AND THE REASON IS DIFFERENT NOW. #68 recorded these
    # because the lane TOMLs consulted with `--lane` and no `--surface`, so `binding_for("")`
    # returned {} and eleven bindings never reached the two highest-volume surfaces in the system.
    # The TOMLs now pass `--surface`, re-rendered and verified: bound_count 0 -> 5 (opener) and
    # 0 -> 6 (closer).
    #
    # They remain here because `consulting_surfaces()` verifies a caller by READING THE FILE THAT
    # NAMES THE SURFACE, and these callers are `~/.codex/automations/*/automation.toml` — machine-
    # local, outside this repository, unreadable from any checkout. That is not a defect the tree can
    # ever clear, so an unqualified in-tree assertion would fail forever. Retiring the entries on the
    # grounds that the defect was fixed was attempted on 2026-08-23 and correctly rejected by this
    # module's own findability selftest.
    "opener-lane": "consults with `--surface opener-lane` as of 2026-08-23 (verified: bound_count 5, "
    "survives render-claude-prompts.sh). Unverifiable in-tree: the caller is "
    "~/.codex/automations/pd-workloop-resume/automation.toml, outside this repository. "
    "FIX: none needed in-tree — the TOML already carries the flag. Re-verify by hand: run "
    "~/.codex/bin/render-claude-prompts.sh and grep the rendered prompt for "
    "`--surface opener-lane`; a missing flag means the TOML was overwritten.",
    "closer-lane": "consults with `--surface closer-lane` as of 2026-08-23 (verified: bound_count 6, "
    "survives render-claude-prompts.sh). Unverifiable in-tree: the caller is "
    "~/.codex/automations/imi-merge-verify-closer/automation.toml, outside this repository. "
    "FIX: none needed in-tree — the TOML already carries the flag. Re-verify by hand: run "
    "~/.codex/bin/render-claude-prompts.sh and grep the rendered prompt for "
    "`--surface closer-lane`; a missing flag means the TOML was overwritten.",
}


def consult_keys() -> set[str]:
    """Every concrete surface some caller passes, families expanded to their instances."""
    out: set[str] = set()
    for key, site in CONSULT_SITES.items():
        out |= set(site.get("instances") or [key])
    return out


def consulting_surfaces() -> dict:
    """Which surfaces a caller actually NAMES, and which bound surfaces none of them does.

    Returns `reached` (the surfaces an offer can travel through), `verified` / `unverified` /
    `drifted` (the three states of a declared claim), and `bound_unconsulted` — the bound surfaces
    that strand whatever is declared on them.
    """
    reached: set[str] = set()
    verified: list[str] = []
    unverified: list[dict] = []
    drifted: list[dict] = []
    here = pathlib.Path(__file__).resolve().parent
    for key, site in sorted(CONSULT_SITES.items()):
        instances = set(site.get("instances") or [key])
        literal = str(site.get("literal") or key)
        caller = str(site.get("caller") or "")
        target = pathlib.Path(caller).expanduser()
        if not target.is_absolute():
            target = here / target
        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            unverified.append(
                {"surface": key, "caller": caller, "why": "caller not present on this machine"}
            )
            reached |= instances
            continue
        if literal in text:
            verified.append(key)
            reached |= instances
        else:
            drifted.append(
                {"surface": key, "caller": caller, "why": f"caller no longer names {literal!r}"}
            )
    promoted = _promoted_index()
    stranded: list[str] = []
    for surface in sorted(SURFACE_BINDINGS):
        if not binding_for(surface, promoted=promoted):
            continue  # suppressed, or empty — there is nothing here to strand
        if surface in reached:
            continue
        # A PARENT WHOSE PHASES ARE CONSULTED IS NOT STRANDED. `repo-audit` is the case: its
        # surface-wide entries are resolved at every `repo-audit:phase-N` consult.
        if any(r.startswith(surface + ":") for r in reached):
            continue
        stranded.append(surface)
    return {
        "reached": sorted(reached),
        "verified": sorted(verified),
        "unverified": unverified,
        "drifted": drifted,
        "bound_unconsulted": stranded,
        "site_count": len(CONSULT_SITES),
    }


def surfaces_binding(capability_ids, *, path=None) -> dict[str, list[str]]:
    """The inverse of `binding_for`: which surfaces bind each of these capabilities.

    CONSUMES `binding_for`, so prefix inheritance, `NO_BINDING` suppression and this instance's
    ledger promotions are resolved by exactly ONE model — a second resolver here would be the
    parallel inventory this tree keeps paying for. The promotion index is read once and handed to
    every resolution.
    """
    wanted = set(capability_ids)
    promoted = _promoted_index(path)
    out: dict[str, list[str]] = {cap_id: [] for cap_id in wanted}
    for surface in sorted(set(SURFACE_BINDINGS) | consult_keys()):
        for cap_id in binding_for(surface, promoted=promoted):
            if cap_id in wanted:
                out[cap_id].append(surface)
    return {cap_id: sorted(surfaces) for cap_id, surfaces in out.items()}


# ---------------------------------------------------------------------------
# THE PHASE CONSULT — a caller for every sub-surface of an unattended cadence.
#
# WHY. A binding with no caller cannot be selected, and a capability that cannot be selected cannot
# earn the evidence that would rank it: the gate starves its own drain. PR #37 gave the BARE `tick`
# surface a caller (`capability_propensity.tick_evidence`, at `ORCH-ANCHOR:
# tick-capability-evidence`) and that closed the loop for four capabilities. The fourteen bound to
# the tick's PHASES below had the same problem one level down — declared, and consulted by nothing.
#
# THIS IS THE SAME MECHANISM, NOT A SECOND ONE. It calls `advise()` per surface exactly as #37 does,
# writes to the same `match` heartbeat, and records no verdict of its own: usefulness verdicts stay
# the single responsibility of `capability_propensity.tick_evidence`, whose ~1.3/day ceiling
# therefore survives sub-surfacing untouched (it reads `binding_for("tick")`, which does not move).
#
# BOUNDED BY CONSTRUCTION. The consult text is stable per (surface, UTC day) and `_record_matches`
# is idempotent per (capability, task digest), so the whole added write volume is one `match` event
# per bound capability per phase per day — landing on the first tick of each day and costing nothing
# on the other 23. Nothing here dispatches, opens a network connection, spawns a subprocess or
# writes outside the ledger.
# ---------------------------------------------------------------------------

TICK_PHASE_PREFIX = "tick:"
# Wall-clock budget for a whole phase-consult run, mirroring `capability_propensity`'s: the only
# unbounded wait in the path is the ledger flock, and the tick drives real dispatch.
CONSULT_BUDGET_S = 30


# AN UNSUBSTITUTED TEMPLATE IN A SURFACE NAME FAILS SILENTLY, AND THAT IS THE WORST CASE.
# `binding_for` resolves by PREFIX, so a caller that sends the literal `repo-audit:phase-N` (the
# string a skill's instructions used to print) resolves to `repo-audit`'s surface-wide set alone --
# one capability instead of the phase's four -- and gets a plausible non-empty answer with no
# indication anything went wrong. Measured 2026-08-23: three audit runs under identical instructions
# consulted 13, 9 and 2 distinct surfaces. Naming it is the fix; it must NOT change the set or the
# order, for the same reason the precondition axis does not.
SURFACE_TEMPLATE = re.compile(r"-N\b|<[^>]*>|\{[^}]*\}|%s|\$\{?\w+|\bN\b")


def unsubstituted_surface(surface: str) -> str:
    """The unsubstituted placeholder in this surface name, or '' if it looks like a real one."""
    match = SURFACE_TEMPLATE.search(str(surface or ""))
    return match.group(0) if match else ""


# AN INVENTED SURFACE NAME RETURNS "NOTHING APPLIES", WHICH IS A DIFFERENT SENTENCE FROM
# "NO SUCH SURFACE" (2026-08-25).
#
# Measured: a run opened with `--surface 'audit-implementation-run'`, a name that does not exist.
# `binding_for` returned `{}`, the free text did not classify, and the answer came back
# `bound_count: 0`, `useful: false`, `capabilities: []` with a long `not_applicable` list — which
# reads as "the advisor has nothing for issue-filing work". It has three capabilities for exactly
# that, at `file-agent-issue`. The caller acted on the wrong sentence and filed no record at all.
#
# Silent absence again, in the advisor itself: the same class as a binding with no caller and a
# capability bound nowhere. So the surface gets a STATE, and it is reported rather than acted on —
# the set and the order are untouched, exactly as the precondition and contraindication axes are.
# Unknown is a diagnosis, so it carries its remedy: the nearest declared surface names.
SURFACE_STATUSES = ("unspecified", "declared", "inherited", "unknown")
_SURFACE_SUGGESTIONS = 4


def known_surfaces(*, path=None) -> set[str]:
    """Every surface name the tables know: declared, consulted, recorded, or promoted here.

    DERIVED FROM THE SAME TABLES `binding_for` RESOLVES AGAINST — a hand-kept list of valid names
    would be free to drift from the names that actually resolve, which is the parallel-inventory
    defect this tree keeps paying for.
    """
    out = set(SURFACE_BINDINGS) | consult_keys() | set(KNOWN_UNCONSULTED)
    out |= set(_promoted_index(path))
    return {s for s in out if s}


def surface_status(surface: str, *, path=None) -> dict:
    """Is this a surface the tables know, one that inherits, or a name nobody has ever declared?

    Three-valued for the same reason the precondition axis is: an empty surface was not asked
    about, and answering "unknown" for it would manufacture a defect out of a caller who simply did
    not pass `--surface`.
    """
    name = str(surface or "").strip()
    if not name:
        return {"surface": "", "status": "unspecified", "resolved_from": None, "did_you_mean": []}
    known = known_surfaces(path=path)
    if name in known:
        return {"surface": name, "status": "declared", "resolved_from": name, "did_you_mean": []}
    # A PHASE OF A KNOWN SURFACE IS NOT UNKNOWN. `binding_for` resolves every prefix, so
    # `repo-audit:phase-9` legitimately inherits `repo-audit`'s surface-wide set; calling that
    # "unknown" would fire on the normal case and the check would be switched off within a week.
    parts = name.split(":")
    for i in range(len(parts) - 1, 0, -1):
        parent = ":".join(parts[:i])
        if parent in known:
            return {
                "surface": name,
                "status": "inherited",
                "resolved_from": parent,
                "did_you_mean": [],
            }
    return {
        "surface": name,
        "status": "unknown",
        "resolved_from": None,
        "did_you_mean": difflib.get_close_matches(
            name, sorted(known), n=_SURFACE_SUGGESTIONS, cutoff=0.4
        )
        or sorted(known)[:_SURFACE_SUGGESTIONS],
    }


def tick_phase_surfaces() -> list[str]:
    """Every declared phase of the tick. DERIVED from the table, never a second list.

    A hand-maintained list of phase names would be free to drift from the bindings it consults, and
    a phase missing from it would be silently unreachable — the exact defect this consult exists to
    remove, one level up.
    """
    return sorted(k for k in SURFACE_BINDINGS if k.startswith(TICK_PHASE_PREFIX))


def consult_text(surface: str, day: str) -> str:
    """The advisory question a cadence surface asks, stable per (surface, UTC day).

    TWO PROPERTIES, both load-bearing:

    * STABLE PER DAY, so the `match` heartbeat's idempotency key (a digest of this text) coalesces 24
      ticks into one observation instead of inflating frequency 24-fold.
    * DISTINCT PER SURFACE, so each phase gets its own experiment id and its own control arm. A
      shared digest would merge five phases into one trial whose candidate set is the union — which
      is precisely the too-many-tools condition sub-surfacing exists to avoid, recreated in the
      evidence.

    It must ALSO stay unclassifiable by `classify_task`, for the same reason
    `capability_propensity.tick_task` does: a cadence is not one free-text task, so the DECLARED
    binding must be the whole answer and a stray keyword would silently widen it.
    `_selftest_phase_consult` asserts that for every declared phase, so a phase renamed to something
    the classifier hits fails a test instead of drifting.
    """
    phase = surface.split(":", 1)[1] if ":" in surface else "cadence"
    return f"orchestrator tick {phase} pass {day}"


def consult_phases(
    *,
    day: str | None = None,
    surfaces: list[Any] | None = None,
    record: bool = True,
    path=None,
) -> dict:
    """Consult every declared tick phase. Advisory, read-only apart from the `match` heartbeat.

    FAILS PER SURFACE, so one broken phase cannot silence the other four: an exception becomes an
    `error` field on that row and the loop continues. The report always names both quantities — how
    many phases were consulted and how many capabilities were offered — because "0 offered" and "0
    consulted" are opposite readings that would otherwise print the same.
    """
    day = time.strftime("%Y-%m-%d", time.gmtime()) if day is None else day
    phases = tick_phase_surfaces() if surfaces is None else list(surfaces)
    started = time.monotonic()
    rows: list[dict] = []
    for surface in phases:
        if time.monotonic() - started > CONSULT_BUDGET_S:
            rows.append(
                {
                    "surface": surface,
                    "error": f"consult budget of {CONSULT_BUDGET_S}s exhausted before this phase",
                    "offered": 0,
                    "recorded": 0,
                }
            )
            continue
        try:
            advice = advise(
                consult_text(surface, day),
                surface=surface,
                skill=surface,
                lane="tick",
                record=record,
                path=path,
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "surface": surface,
                    "error": f"{type(exc).__name__}: {exc}",
                    "offered": 0,
                    "recorded": 0,
                }
            )
            continue
        rows.append(
            {
                "surface": surface,
                "experiment_id": advice.get("experiment_id"),
                "offered": len(advice.get("capabilities") or []),
                "bound": sorted(advice.get("bound_capabilities") or []),
                "recorded": int(advice.get("recorded_matches") or 0),
                "suppressed": bool(binding_suppressed(surface)),
            }
        )
    return {
        "day": day,
        "phases": len(rows),
        "rows": rows,
        "offered": sum(int(r.get("offered") or 0) for r in rows),
        "recorded": sum(int(r.get("recorded") or 0) for r in rows),
        "errors": [r["surface"] for r in rows if r.get("error")],
    }


def consult_phases_guarded(**kwargs) -> dict:
    """`consult_phases` that cannot take the tick down. THE ONLY entry point the shell calls.

    Same shape and same reason as `capability_propensity.tick_evidence_guarded`: a SIGALRM backstop
    over the one syscall that can block indefinitely (the ledger flock), and any exception at all
    becomes a reported field rather than a non-zero exit.
    """
    import signal

    try:
        previous = signal.signal(signal.SIGALRM, _consult_expired)
        signal.alarm(CONSULT_BUDGET_S + 5)
        armed = True
    except (ValueError, AttributeError, OSError):
        armed, previous = False, None
    try:
        return consult_phases(**kwargs)
    except BaseException as exc:  # noqa: BLE001
        return {
            "day": kwargs.get("day") or time.strftime("%Y-%m-%d", time.gmtime()),
            "phases": 0,
            "rows": [],
            "offered": 0,
            "recorded": 0,
            "errors": ["*"],
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if armed:
            try:
                signal.alarm(0)
                if previous is not None:
                    signal.signal(signal.SIGALRM, previous)
            except (ValueError, OSError):
                pass


def _consult_expired(_signum, _frame):
    raise TimeoutError(f"capability phase consult exceeded {CONSULT_BUDGET_S}s")


def format_phase_consult(rep: dict) -> str:
    """One line per run plus one per phase. BOTH quantities, never just the reassuring one."""
    head = (
        f"  PHASE-CONSULT: {rep.get('phases', 0)} phase(s), "
        f"{rep.get('offered', 0)} capability offer(s), "
        f"{rep.get('recorded', 0)} new match event(s) [{rep.get('day')}]"
    )
    if rep.get("error"):
        head += f" — FAILED: {rep['error']} (the tick is unaffected)"
    lines = [head]
    for row in rep.get("rows") or []:
        if row.get("error"):
            lines.append(f"    {row['surface']}: ERROR {row['error']}")
        else:
            lines.append(
                f"    {row['surface']}: offered {row['offered']} "
                f"(bound {len(row.get('bound') or [])}), recorded {row['recorded']}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# THE `applies_to` AXIS, AND PRECONDITIONS GENERALLY — evaluate the condition; do not weaken the
# binding.
#
# WHY, from three independent audit rounds on 2026-08-23 which between them ruled out the obvious fix:
#
#   * FALSE POSITIVE. `frontend-verifier` was offered at `repo-audit:phase-2` and
#     `repo-audit:dimension-4` on two repositories with no application UI at all. Its own binding
#     reason is CONDITIONAL — "dimension 4 uses the ux-review-overlay WHEN OBSERVABLE SURFACES
#     EXIST" — and the condition lived in prose that nothing read.
#   * FALSE NEGATIVE, the mirror image. `capability:reference-sync-hygiene-test-gate` was filtered
#     out as not-applicable during an audit OF SYNC HYGIENE. Both rounds diagnosed one cause: the
#     capability is scoped to the Orchestrator's own runtime while the audit target is another repo,
#     and `repo-audit:dimension-8` was the clearest case — four well-chosen capabilities whose
#     concepts transferred to the audited repo and whose INSTRUMENTS did not.
#   * AND THE REFUTATION, which is why this annotates instead of suppressing. On a third repo, one
#     that does have a display surface, `frontend-verifier` was READY on its first `--doctor` call
#     and produced the finding with the highest evidence-to-effort ratio of that audit — a provenance
#     banner promising "every number below was measured on your Mac" three lines above fabricated
#     grant scope, which the code-reading path had already missed. Its propensity moved off the floor
#     onto real positive evidence. That round's own conclusion: "removing or down-weighting the
#     binding on the strength of the two negative observations alone would have cost this audit its
#     second-strongest finding."
#
# SO THIS AXIS CHANGES NO ORDER AND NO MEMBERSHIP. It turns "investigate this offer in order to find
# out that it cannot apply" into "dismiss it in one line" — which is the cost those rounds actually
# paid — and where the precondition HOLDS the capability is offered exactly as it was before.
#
# DEFAULT IS CONSERVATIVE, AND THAT IS THE WHOLE DESIGN. An UNDECLARED capability behaves exactly as
# it did before this axis existed: `precondition_met` is None, no note. So the 43 existing
# capabilities are not silently reclassified, and the set that DOES declare is a diffable table
# rather than a heuristic. A consult naming no repository is `unknown` and never a mismatch either;
# widening what can be ANSWERED must never widen what is ASSUMED.
#
# LATCHED-GATE ANSWERS. A precondition gates how an offer READS, never whether it is made:
#   1. WHAT DECREMENTS IT? Nothing needs to. A capability whose precondition does not hold is still
#      returned, ranked identically, selectable, and able to earn a trigger or a classified decline —
#      which are the evidence that would correct the declaration.
#   2. CAN THE DRAIN RUN WHILE CLOSED? There is nothing to drain, because nothing is withheld. A
#      concealed or down-ranked capability could not earn the evidence that would un-conceal it, and
#      that starvation is exactly the false negative above — and on the third repo it would have cost
#      a real finding.
#   3. SAME WINDOW BOTH WAYS? No window exists: a declaration evaluated per consult from the
#      consult's own inputs. There is no counter that can drift.
#
# AND IT NAMES THE DECLINE KIND IT IMPLIES (`precondition_unmet`), which
# `capability_propensity.DECLINE_KINDS` marks NON-DEMOTABLE on purpose. The two halves have to agree:
# an axis that explained a mismatch while the ledger quietly demoted the binding for it would be the
# forbidden correction taking the long way round.
#
# DEDUP (CLAUDE.md §0, checked against the tip before writing this). `_annotate_contraindications` +
# `repo_knowledge.contraindications_for` ALREADY annotate a candidate the repository's own record says
# does not work there, and they cover the `frontend-verifier`-against-a-Streamlit-SPA case exactly.
# This is NOT a second copy of that, and the two must not become one:
#
#   * A CONTRAINDICATION is a RECORDED, per-(repo, capability) human judgement — "broken against THIS
#     app" — read from the repo registry. It is per-REPO on purpose, and it ranks last within its
#     partition, because a recorded judgement about one app is high-confidence and should not lead.
#   * A PRECONDITION is an INTRINSIC, per-capability declaration evaluated per consult — "acts on the
#     Orchestrator's own runtime", "needs an observable surface at all". `switch-review` is
#     Orchestrator-scoped for EVERY audited repo, so expressing it as a contraindication would mean a
#     hand-written note in all 13 repo records: an N x M table nobody maintains, which is the
#     parallel-inventory defect this project forbids. And the Workflows false positive happened
#     precisely BECAUSE no note existed; a mechanism that requires someone to have written the note
#     cannot catch the case where nobody did.
#
# So: recorded-and-per-repo ranks last; declared-and-derived only annotates. A capability can be both,
# and both reach the caller — the selftest pins that, because silently letting one shadow the other is
# how two mechanisms become one broken one.
# ---------------------------------------------------------------------------

APPLIES_SELF = "self"
APPLIES_AUDITED_REPO = "audited_repo"
APPLIES_BOTH = "both"
APPLIES_TO_VALUES = (APPLIES_SELF, APPLIES_AUDITED_REPO, APPLIES_BOTH)
# What a consult with no `repository` targets. NOT `self`: guessing would make every bare consult
# report mismatches against `audited_repo` capabilities, which is reclassification by default.
TARGET_UNKNOWN = "unknown"

# This repository, as the fleet names it. A consult whose `repository` is this one is about the
# Orchestrator's own runtime; any OTHER named repository is a repo under audit.
SELF_REPOSITORY = "stranske/Orchestrator"

# THE DECLARED SET. Committed and diffable like `SURFACE_BINDINGS`, and every entry cites the audit
# row that establishes it. Absence is the default and is not an omission: a capability never observed
# to care about the distinction should not be given an opinion about it.
#
#   `applies_to` — which SYSTEM it acts on. Evaluable from `repository` alone.
#   `requires`   — a named one-time repo FACT from `REPO_FACT_PROBES`, evaluated against a checkout
#                  when the caller supplies `repo_path`, and reported as UNEVALUATED with the missing
#                  input NAMED when it does not. That naming is the fix: the defect was a condition
#                  nothing could even attempt to check.
#   `concept`    — THE CONTENT BEHIND "the concept may transfer". Required beside
#                  `applies_to: self` and refused as an empty string, because the note without it
#                  is an invitation with nothing behind it: the Counter_Risk audit read
#                  "the concept may transfer; the instrument does not" against `feature-scan`,
#                  transferred the concept by hand, produced two findings with it — and had to
#                  RECONSTRUCT what the capability's question even was from its name. So the
#                  question is written down here, once, in words that do not name this repository,
#                  and `evaluate_precondition` hands it back exactly when the scope mismatch fires.
#                  It is NOT a second `how_to_use`: that field says how to invoke the instrument,
#                  this one says what to ask when the instrument is pointed at the wrong system.
CAPABILITY_PRECONDITIONS: dict[str, dict] = {
    # Workflows consult #10 (`repo-audit:dimension-8`): "switch_review.py audits ORCHESTRATOR
    # switches, and the gate under audit is config/template-drift-allowlist.txt in another repo. ...
    # Same for the two capability monitors. The concepts transferred; the instruments did not."
    # Independently reproduced by the Fine-Art-Archive round, 4 declines in one run:
    # "Orchestrator-side monitor, and the task forbids touching that repo."
    "switch-review": {
        "applies_to": APPLIES_SELF,
        "concept": (
            "which of this repository's own flags, held gates and bounded trials passed their "
            "stated window with no decision recorded — and for each, name what would clear it. A "
            "deferral nobody re-raises is indistinguishable from a decision to revert"
        ),
    },
    "capability-activation-audit": {
        "applies_to": APPLIES_SELF,
        "concept": (
            "for each thing this repository claims it can do: could it fire AT ALL today — trigger "
            "reachable, entrypoint present, consumer wired — as opposed to whether anyone used it? "
            "'nobody needs this' and 'the trigger physically cannot fire' look identical in a usage "
            "count and need opposite fixes"
        ),
    },
    "capability-firing-monitor": {
        "applies_to": APPLIES_SELF,
        "concept": (
            "which of this repository's recurring producers USED to emit output and has gone "
            "silent, and since when? Ask it per producer against its own history: silence reads as "
            "normal, so a report that stopped being written looks exactly like one with nothing to "
            "say"
        ),
    },
    # Same rows; and it reads this instance's own ledger by construction.
    "capability-propensity": {
        "applies_to": APPLIES_SELF,
        "concept": (
            "for each thing this repository offers its callers: is there recorded evidence it "
            "HELPED, what is that evidence's provenance, and how many INDEPENDENT judges does it "
            "rest on? A rate computed from one judge's self-assessments is an opinion with a "
            "denominator"
        ),
    },
    # Workflows consult #8 (`dimension-6`): "feature_scan.py reports reusable structures IN THE
    # ORCHESTRATOR'S OWN REGISTRY ... Wrong repository, right concept."
    "feature-scan": {
        "applies_to": APPLIES_SELF,
        "concept": (
            "which reusable structures exist in this repository's code three or more times and are "
            "registered nowhere a future author would look — so the next one rebuilds a fourth "
            "instead of finding the third?"
        ),
    },
    # Gate 1 drives the AUDITED repo's application surface, never this tool's — and its condition is
    # that such a surface exists. This is the capability the whole axis is about, and the declaration
    # makes "no observable surface here" a one-line dismissal WITHOUT touching a binding that earned
    # a real finding on the repo where the condition held.
    "frontend-verifier": {"applies_to": APPLIES_AUDITED_REPO, "requires": "observable_surface"},
    # `both` is behaviourally identical to undeclared. It is here to record that the most-offered
    # capability in the catalogue was CONSIDERED, not overlooked: one round declined it at nine
    # surfaces and another used it successfully against an audited repo.
    "offload": {"applies_to": APPLIES_BOTH},
}


def applies_to(capability_id: str) -> str | None:
    """The declared system this capability acts on, or None when it declares none."""
    return (CAPABILITY_PRECONDITIONS.get(capability_id) or {}).get("applies_to")


def required_repo_fact(capability_id: str) -> str | None:
    """The named repo fact this capability requires, or None."""
    return (CAPABILITY_PRECONDITIONS.get(capability_id) or {}).get("requires")


def transferable_concept(capability_id: str) -> str | None:
    """The question this capability asks, in words that do not name a repository — or None.

    ONE reader for the declaration, so the answer and the render cannot consult the table twice and
    drift, which is exactly how `HOW_TO_USE` came apart from `format_advice`.
    """
    declared = (CAPABILITY_PRECONDITIONS.get(capability_id) or {}).get("concept")
    return declared or None


def consult_target(repository: str | None) -> str:
    """What this consult is about: `self`, `audited_repo`, or `unknown` when no repo was named."""
    repo = str(repository or "").strip()
    if not repo:
        return TARGET_UNKNOWN
    return APPLIES_SELF if repo == SELF_REPOSITORY else APPLIES_AUDITED_REPO


# ---- REPO FACTS. Deterministic probes over a checkout, so the answer is EVIDENCE rather than a
# guess: each returns the markers it matched, and a wrong verdict is inspectable instead of silent. A
# bare boolean from a heuristic is how the false positive this replaces got believed in the first
# place. Validated against the three repositories the audits actually ran on: false for the two with
# no application UI (and for this repository), true for the Streamlit SPA and for the one with
# `src/fine_art_archive/ui/index.html` — which are exactly the two negative and one positive
# observations on record.

_SURFACE_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "docs",
        "site",
        ".github",
        "htmlcov",
        "build",
        "dist",
        ".mypy_cache",
        "coverage",
        ".tox",
        ".eggs",
        "vendor",
        "third_party",
        "examples",
        "fixtures",
    }
)
# `docs/` and `site/` are skipped deliberately: a generated API-docs tree is full of HTML and is not
# an application surface. Counting it is precisely what would turn this probe back into the false
# positive it exists to remove.
_SURFACE_FRAMEWORKS = (
    "streamlit",
    "flask",
    "fastapi",
    "django",
    "dash",
    "gradio",
    "panel",
    "nicegui",
)
_SURFACE_DEP_FILES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "setup.cfg",
        "Pipfile",
        "environment.yml",
    }
)
_SURFACE_COMPONENT = re.compile(r"\.(jsx|tsx|vue|svelte)$")
# Bounded on both axes, so a probe can never become the expensive part of an advisory call: it stops
# at the first few markers and gives up rather than walking an unbounded tree.
_SURFACE_MAX_MARKERS = 6
_SURFACE_MAX_FILES = 40000


def detect_observable_surface(root) -> dict:
    """Does this checkout contain an application surface a browser could drive?

    A ONE-TIME REPO FACT, in the audit's own words, not a per-task classification. `observable` is
    None when there is nothing to look at — never False, because "no checkout" and "no surface" are
    opposite findings and conflating them would re-create the defect in the other direction.
    """
    import os
    import pathlib as _pathlib

    base = _pathlib.Path(str(root)).expanduser()
    if not base.is_dir():
        return {
            "observable": None,
            "markers": [],
            "detail": f"no readable checkout at {str(root)!r}",
        }
    markers: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SURFACE_SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            seen += 1
            if seen > _SURFACE_MAX_FILES:
                return {
                    "observable": bool(markers),
                    "markers": markers,
                    "truncated": True,
                    "detail": f"stopped after {_SURFACE_MAX_FILES} files",
                }
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            low = name.lower()
            if low.endswith((".html", ".htm")):
                markers.append(f"html:{rel}")
            elif _SURFACE_COMPONENT.search(low):
                markers.append(f"component:{rel}")
            elif name in _SURFACE_DEP_FILES:
                try:
                    text = _pathlib.Path(dirpath, name).read_text(errors="ignore")[:200000].lower()
                except OSError:
                    continue
                for framework in _SURFACE_FRAMEWORKS:
                    if re.search(rf"(?<![a-z]){framework}(?![a-z])", text):
                        markers.append(f"dependency:{framework}:{rel}")
            if len(markers) >= _SURFACE_MAX_MARKERS:
                return {
                    "observable": True,
                    "markers": markers,
                    "detail": f"{len(markers)} surface marker(s) found",
                }
    return {
        "observable": bool(markers),
        "markers": markers,
        "detail": (
            f"{len(markers)} surface marker(s) found"
            if markers
            else "no HTML entrypoint, UI component or web-framework dependency found"
        ),
    }


REPO_FACT_PROBES = {"observable_surface": detect_observable_surface}


def evaluate_precondition(
    capability_id: str, *, repository: str = "", repo_path: str = "", facts: dict | None = None
) -> dict:
    """Does this capability's declared precondition hold for this consult?

    THREE-VALUED, and that is load-bearing. True and False are verdicts; None means NOT EVALUATED —
    the capability declares nothing, or the consult named no repository, or a repo fact needs a
    checkout nobody supplied. Collapsing None into False would silently reclassify the whole
    catalogue; collapsing it into True would restore the original defect, a condition never checked.

    `facts` is a per-call memo, so one probe answers for every candidate in one advisory call.
    """
    declared = applies_to(capability_id)
    needs = required_repo_fact(capability_id)
    target = consult_target(repository)
    out: dict[str, Any] = {
        "applies_to": declared,
        "scope_target": target,
        "scope_match": None,
        "requires": needs,
        "requirement_met": None,
        "requirement_evidence": None,
        "precondition_met": None,
        "precondition_note": None,
        # ALWAYS PRESENT, None until the scope mismatch below fires. Same discipline as
        # `how_to_use`: "no concept recorded" and "this answer does not carry the field" must not
        # look alike, or the next reader cannot tell a gap from a silence.
        "transferable_concept": None,
        "unevaluated_because": [],
    }

    if declared is not None:
        if target == TARGET_UNKNOWN:
            out["unevaluated_because"].append(
                f"applies_to={declared!r} needs a `repository`, and none was named"
            )
        else:
            out["scope_match"] = declared in (APPLIES_BOTH, target)
            if not out["scope_match"]:
                where = (
                    "the Orchestrator's own runtime"
                    if target == APPLIES_SELF
                    else f"the repository under audit, {str(repository)!r}"
                )
                out["precondition_note"] = (
                    f"declared applies_to={declared!r} but this consult targets {where} — the "
                    f"concept may transfer; the instrument does not"
                )
                # ...AND THE CONCEPT ITSELF, because the sentence above is an invitation and an
                # invitation with no content behind it is what the reader has to reconstruct. Only
                # on the SCOPE mismatch: a `requires` failure (no observable surface exists at all)
                # has no question left to ask by hand, and offering one there would rebuild the
                # defect in the other direction.
                out["transferable_concept"] = transferable_concept(capability_id)

    if needs:
        probe = REPO_FACT_PROBES.get(needs)
        if probe is None:
            out["unevaluated_because"].append(f"no probe is registered for {needs!r}")
        elif not repo_path:
            out["unevaluated_because"].append(
                f"{needs!r} is a one-time repo fact and needs `repo_path`, a checkout to look at"
            )
        else:
            memo = facts if facts is not None else {}
            key = (needs, str(repo_path))
            if key not in memo:
                try:
                    memo[key] = probe(repo_path)
                except Exception as exc:  # noqa: BLE001
                    memo[key] = {
                        "observable": None,
                        "markers": [],
                        "detail": f"probe failed: {type(exc).__name__}",
                    }
            result = memo[key]
            value = result.get("observable")
            out["requirement_met"] = None if value is None else bool(value)
            out["requirement_evidence"] = result
            if value is None:
                out["unevaluated_because"].append(f"{needs!r}: {result.get('detail')}")
            elif not value:
                out["precondition_note"] = (
                    f"requires {needs} and this repository has none: {result.get('detail')} — "
                    f"dismissible without investigating it"
                )

    verdicts = [v for v in (out["scope_match"], out["requirement_met"]) if v is not None]
    out["precondition_met"] = all(verdicts) if verdicts else None
    # THE DECLINE KIND THIS IMPLIES, handed to the caller so the RIGHT correction gets recorded.
    # `capability_propensity` marks `precondition_unmet` non-demotable on purpose: the fix is to
    # evaluate the condition, never to unbind a capability that fires where the condition holds.
    out["suggested_decline_kind"] = (
        "precondition_unmet" if out["precondition_met"] is False else None
    )
    return out


# WHICH CONSULT INPUT ANSWERS WHICH DECLARATION. Declared once so the answer's remedy cannot drift
# from the thing `evaluate_precondition` actually reads: `applies_to` is decided by `repository`
# alone, a named repo FACT additionally needs a checkout.
PRECONDITION_INPUT_FOR = {"applies_to": "repository", "requires": "repo_path"}


def missing_precondition_inputs(
    capability_ids, *, repository: str = "", repo_path: str = ""
) -> list:
    """The consult inputs that would turn an UNEVALUATED precondition into a real verdict.

    THE DRAINABLE QUANTITY, beside the blocking one. `unevaluated_because` already named what was
    missing per capability; three audit rounds read it and re-asked nothing, because a diagnosis is
    not an instruction. `frontend-verifier` was declined four times as "the binding's own precondition
    is never evaluated" while the probe, the declaration and the two parameters that drive them all
    existed — nothing in the answer said a caller could supply them.
    """
    needed: set[str] = set()
    for cap_id in capability_ids:
        if applies_to(cap_id) and not str(repository).strip():
            needed.add(PRECONDITION_INPUT_FOR["applies_to"])
        if required_repo_fact(cap_id) and not str(repo_path).strip():
            needed.add(PRECONDITION_INPUT_FOR["requires"])
    return sorted(needed)


def _annotate_preconditions(entries: list[dict], repository: str, repo_path: str) -> dict:
    """Stamp every entry with its precondition verdict. ORDER AND MEMBERSHIP ARE UNTOUCHED.

    Returns a summary: which capabilities declared a precondition, which failed it, and which could
    not be evaluated with the inputs this consult supplied. All three populations are reported,
    because "nothing failed" and "nothing was checked" must never look alike — that identity IS the
    original defect, one level up.

    ...and `missing_inputs` / `how_to_evaluate`, which are the fourth population and the one that
    makes the third one ACTIONABLE rather than merely honest.
    """
    facts: dict = {}
    declared, unmet, unevaluated = [], [], {}
    for entry in entries:
        verdict = evaluate_precondition(
            entry["capability_id"], repository=repository, repo_path=repo_path, facts=facts
        )
        entry.update(verdict)
        if verdict["applies_to"] or verdict["requires"]:
            declared.append(entry["capability_id"])
        if verdict["precondition_met"] is False:
            unmet.append(entry["capability_id"])
        if verdict["unevaluated_because"]:
            unevaluated[entry["capability_id"]] = list(verdict["unevaluated_because"])
    missing = missing_precondition_inputs(declared, repository=repository, repo_path=repo_path)
    return {
        "repository": repository,
        "target": consult_target(repository),
        "repo_path": repo_path or None,
        "declared": sorted(declared),
        "unmet": sorted(unmet),
        "unevaluated": dict(sorted(unevaluated.items())),
        "declared_capabilities": sorted(CAPABILITY_PRECONDITIONS),
        # THE REMEDY, named in the same place as the gap. Empty when this consult already supplied
        # everything the offered set declares — so an empty list is a real "nothing left to supply",
        # not an absence of opinion.
        "missing_inputs": missing,
        "how_to_evaluate": (
            "re-ask this consult with "
            + ", ".join(f"`{name}`" for name in missing)
            + " and the declared precondition(s) above are answered for you instead of "
            "investigated; a failed one is then dismissible in one line, recorded as decline "
            "kind 'precondition_unmet', which never counts against the binding"
            if missing
            else None
        ),
        "note": (
            "a failed precondition is REPORTED, never enforced: the capability is offered "
            "and ranked exactly as it would be without this axis, because a binding that "
            "fires where the condition holds must not be weakened by the cases where it "
            "does not"
        ),
    }


def experiment_id(task: str) -> str:
    """The natural-experiment id for this task text.

    A caller that receives advice and then USES one of the candidates has to be able to say so
    against the same key the advice was recorded under. Without this the loop cannot be closed from
    outside this module: `_record_matches` derived the digest privately and threw it away.
    """
    import hashlib

    return "advice:" + hashlib.sha1(str(task or "").encode()).hexdigest()[:12]


def _record_matches(advice: dict, *, skill: str = "", surface: str = "", path=None) -> int:
    """Record that these capabilities matched REAL work, with the skill that surfaced it.

    Uses the existing `match` heartbeat rather than a new store: a capability whose declared trigger
    matched an actual task genuinely experienced a match, and recording it moves the capability out
    of `no_matching_work` into `matched_not_invoked` — which is the honest classification for
    "work of your kind occurred and you still did not run".

    The skill is carried in the event metadata, so `learned_associations()` can later aggregate
    skill -> capability from accumulated observations. Idempotent per (capability, exact task), so
    repeating the same query does not inflate frequency while distinct tasks still accumulate.

    THE SURFACE IS RECORDED TOO (2026-08-23). It was not, and the CLI has no `--skill` flag at all —
    so every `--surface` consult wrote `skill: null`, and the two audit rounds of 2026-08-23 produced
    33 candidate-offers that `propose_demotions` and `missed_selection` could not attribute to any
    surface. The control arm existed in the ledger and was unreachable, which is the same
    "recorded but unusable" defect one level down from the declines this change is about.
    """
    import hashlib

    digest = hashlib.sha1(str(advice.get("task") or "").encode()).hexdigest()[:12]
    # exposed via experiment_id() so a caller can record trigger/outcome against it
    written = 0
    for entry in advice.get("capabilities") or []:
        ok = capabilities.heartbeat(
            entry["capability_id"],
            "match",
            ref=f"advice:{digest}",
            path=path or capabilities.REG,
            idempotency_key=f"advice:{entry['capability_id']}:{digest}",
            metadata={
                "source": "capability_advisor",
                "skill": skill or None,
                "surface": surface or None,
                "task_type": entry.get("matched_task_type"),
            },
        )
        written += 1 if ok else 0
    return written


def learned_associations(*, path=None) -> dict:
    """skill -> capabilities, learned from accumulated advisory matches.

    This is the emergent half of the design: declared matchers tie capabilities to WORK TYPES, while
    repeated observation ties them to the SKILLS that surface that work. Nothing is hand-declared
    here — if invoking the audit skill keeps surfacing adversarial-review, that association shows up
    on its own once the observations exist.

    EVERY COUNT NAMES ITS OWN POPULATION (FM5, fixed 2026-08-22). `observations` used to be the sum
    over `by_skill` ALONE, so an advisory match recorded WITHOUT a skill was invisible in the count
    while still landing in `by_task_type` — the live ledger read "observations: 3" beside 12
    task-type observations, and the difference looked like a bug in the ledger rather than in the
    denominator. A convenient denominator in the reporting path of the very tool meant to measure
    skill wiring would make "the skills are wired now" unfalsifiable. The three counts below
    reconcile by construction: with_skill + without_skill == observations.
    """
    caps = capabilities.load(path or capabilities.REG)
    by_skill: dict[str, dict[str, int]] = {}
    by_task_type: dict[str, dict[str, int]] = {}
    total = 0
    with_skill = 0
    for cap_id, cap in caps.items():
        for event in cap.get("event_history") or []:
            meta = event.get("metadata") or {}
            if meta.get("source") != "capability_advisor":
                continue
            total += 1
            skill = meta.get("skill")
            if skill:
                with_skill += 1
                by_skill.setdefault(str(skill), {}).setdefault(cap_id, 0)
                by_skill[str(skill)][cap_id] += 1
            tt = meta.get("task_type")
            if tt:
                by_task_type.setdefault(str(tt), {}).setdefault(cap_id, 0)
                by_task_type[str(tt)][cap_id] += 1
    return {
        "by_skill": {
            s: dict(sorted(v.items(), key=lambda kv: -kv[1])) for s, v in sorted(by_skill.items())
        },
        "by_task_type": {
            t: dict(sorted(v.items(), key=lambda kv: -kv[1]))
            for t, v in sorted(by_task_type.items())
        },
        "observations": total,
        "observations_with_skill": with_skill,
        "observations_without_skill": total - with_skill,
        "populations": {
            "observations": "every capability_advisor match event in the ledger",
            "observations_with_skill": "the subset naming a skill — the ONLY population by_skill can cover",
            "observations_without_skill": "matches with no skill attributed: counted in by_task_type, absent from by_skill",
            "by_task_type": "every match event carrying a task_type, skill-attributed or not",
        },
    }


# How to actually USE each capability from a chat session. The ledger's `next_step` answers a
# lifecycle question ("lift the gate, or accept it is deliberately off"), which is the wrong answer
# for someone who just wants to route the task in front of them. This maps capability -> the concrete
# thing to run or do. Absent entries fall back to the lifecycle text rather than inventing a command.
# HOW TO USE, AND — JUST AS LOAD-BEARING — WHAT IT CANNOT TAKE.
#
# THIS TABLE WAS PROSE NOTHING READ, which is this tree's named defect wearing the same hat as the
# conditional binding reason one axis over. It was consumed ONLY by `format_advice`, the text
# rendering, while every real consult arrives through the `capability_advice` MCP tool and receives
# the RESULT DICT — which carried `entrypoint`, `blocker` and `next_step` and never this. So a caller
# was offered `adversarial-review` with `entrypoint: adversarial.py`, `blocker: "matched but a gate
# blocked invocation"` and no gate NAMED, went and read the ledger row itself, found
# `{kind: closer_gate, name: high_stakes_review}` and declined `wrong_match` — "a lane gate, not an
# audit dimension" — while THIS TABLE held the direct call that answers it. `_attach_how_to_use`
# stamps it onto every entry, so the answer a caller receives carries it on both branches.
#
# AND THE BOUNDARY BELONGS HERE TOO. Six `offload` declines in one window were one sentence repeated:
# the work had to be first-person (run the code and read exit codes, re-run a guard with the break in
# place, hold a whole grep trace, drive a browser). None of that is a scope judgement and none of it
# is a defect in the dispatcher — it is offload's intrinsic boundary, and it was written down nowhere
# a caller could see. A capability that cannot say what it cannot take is investigated and declined
# once per surface, forever.
#
# CORRECTED 2026-08-24, AND THE CORRECTION IS THE POINT OF THIS PARAGRAPH. The six declines above were
# real, but the sentence written from them over-generalised into a claim about the MECHANISM that is
# false: it said offload was "a read-only, text-returning hand-off" that "cannot run code and read
# exit codes". Offload has never been read-only. `adapters` sends `--sandbox workspace-write` unless
# `mode="assess"` (adapters.py, the `sandbox = "read-only" if mode == "assess"` line), and can send
# `--dangerously-bypass-approvals-and-sandbox` with no sandbox at all; `dispatcher.offload`'s own
# docstring gives `isolate=True` the job of letting "multiple code-building offloads run in parallel";
# and the orchestrate skill's pitfall #1 says the DEFAULT is the coding mode. The offloaded agent runs
# code, edits files and reads exit codes routinely.
#
# WHY THE ERROR MATTERED, since it read as helpful either way: this table is stamped onto every
# advisor answer, so a false narrowing does not merely mis-describe — it removes the capability from
# consideration for exactly the work it is best at. It did: a caller reading this entry planned a
# 12-module type-drain around classification passes instead of write-mode offloads, which is the
# capability being talked out of its own job by its own documentation.
#
# SO THE ENTRY NOW LEADS WITH THE RANGE, not with the caveat, and that ordering is the fix rather
# than a style choice. Deleting the false sentence would have left the entry SILENT on scope, and a
# capability whose description mentions only reading gets selected only for reading — the same
# outcome by omission. The narrowing was never confined to this table either: `ORCHESTRATOR.md`
# called it "one synchronous read/proposal", `dispatcher.offload`'s own docstring said it was for
# "token-heavy READING", and the orchestrate skill listed it as "read / summarize / research". Four
# surfaces, one story, none of them true — so all four were corrected together. A per-surface
# correction would have left the others to re-teach the same narrowing to the next caller.
#
# THE TWO REAL BOUNDARIES ARE STATED AS A CLOSED SET, deliberately. An open-ended list of things
# offload "cannot" do invites the reader to keep extending it, which is how six correct declines
# became a false claim about the mechanism. Naming exactly two — fleet actions, which belong to
# `delegate`, and first-person observation, which cannot be delegated to anything — and saying
# outright that everything else is scoping, gives the reader a rule that resolves the next case
# instead of a list that grows.
#
# WHAT IS ACTUALLY TRUE, and what the six declines were really about, is one step in from the
# mechanism: FIRST-PERSON OBSERVATION does not survive the hand-off. The agent can run the guard; the
# orchestrator does not WITNESS it, and gets a report instead of evidence. That is why a deliberate
# break→revert demonstration and a split grep trace are still declines — not because the sandbox
# forbids the typing, but because the seeing is the deliverable. State the boundary at that level; a
# boundary stated one level too coarse is indistinguishable from the capability being unavailable.
HOW_TO_USE = {
    "offload": (
        "dispatcher.offload('gemini', prompt, cwd=repo) — hands a WHOLE TASK to another LLM "
        "working in a real workspace, and returns its result, spending that agent's context "
        "instead of this seat's. THE FULL RANGE OF WHAT AN LLM CAN DO IS IN SCOPE: writing and "
        "editing code across many files, running builds/tests/linters and reading their exit "
        "codes, iterating until something passes, debugging, refactoring, migrations, design "
        "work, code review, research and web reading, long analysis — anything you would give a "
        "capable engineer with a shell. It is WRITE-CAPABLE BY DEFAULT (adapters sends --sandbox "
        "workspace-write, and can drop the sandbox entirely); mode='assess' NARROWS it to a "
        "no-write sandbox when that is what you want, and isolate=True copies the cwd to a local "
        "workspace so several code-building offloads run at once without same-dir races. "
        "SCOPE THE BRIEF TO THE TASK, NOT TO A QUESTION: the usual reason a result comes back "
        "thin is a brief that asked for a summary when it could have asked for the work. "
        "BOUNDARY — and it is exactly two things, everything else being a scoping decision "
        "rather than a limit. (1) FLEET ACTIONS: it takes no claim, opens no PR and applies no "
        "label; when the deliverable IS a PR, that is dispatcher.delegate. (2) first-person "
        "OBSERVATION: you get the agent's report, not your own witness, so work whose value IS "
        "the seeing — re-running a guard with a deliberate break in place and watching it go "
        "RED, driving a browser yourself, holding one whole grep trace whose halves must be "
        "compared — stays in this seat, and whatever the agent does write is re-verified here "
        "rather than believed. Splitting such a trace is how a 'no caller' refutation goes "
        "wrong. Those two are the decline to make in one line rather than investigate; a task "
        "being large, code-shaped, or multi-step is not"
    ),
    "codemod-campaign": (
        "label the issue `refactor` (or let the daily issue_readiness task-label "
        "step do it) so classify() routes it to the codemod lane; the lane hands "
        "an agent the codemod_lane.py plan schema"
    ),
    # TWO ROUTES, AND ONLY ONE WAS DOCUMENTED. Until 2026-08-25 this entry described the lane route
    # alone — and the lane route is unavailable to a seat that is implementing rather than
    # dispatching, because labelling the issue hands the work to a remote agent and races it against
    # the PR being written. The declared entrypoint has always been `testgen_lane.py/testgen_gate.py`
    # and `testgen_gate.py --help` has always shown a complete standalone CLI. One run checked the
    # binary rather than trusting the prose and turned a decline into the capability's FIRST trigger
    # in 44 offers; another declined it. Guidance that names one of two routes actively steers a
    # caller away from the other.
    "testgen-lane": (
        "TWO ROUTES, and which one applies depends on whether you are DISPATCHING or IMPLEMENTING. "
        "(1) THE LANE: label the issue `testing`; the lane adds the testgen_gate.py acceptance gate "
        "to the prompt and requires it to pass before the PR body is accepted. Not available when "
        "you are writing the PR yourself — labelling hands the work to a remote agent and races it "
        "against you. (2) THE GATE, DIRECTLY: `testgen_gate.py --repo <r> --source <importable "
        "module> --baseline-pytest-args '<pre-existing tests>' --candidate-pytest-args '<with the "
        "new ones>'` is a complete standalone CLI and is the half you want in-seat. It runs "
        "collect/import -> baseline non-regression -> repeated reliability -> covered-lines delta, "
        "so it turns a hand-measured coverage claim into an attributed delta with flake protection. "
        "TWO INVOCATION RULES that have each cost a wasted run: `--source` takes an IMPORTABLE "
        "module or package resolved from --repo, not a file path (`src/pkg/mod.py` normalises to "
        "`src.pkg.mod`, which imports only if `src` is a package); and the pytest-arg strings are "
        "split shell-style, so a `-k` expression containing spaces must be quoted INSIDE the string "
        "or it is shredded into tokens that select nothing. BOUNDARY: the coverage-delta check "
        "measures covered lines in a PRODUCTION source module, so a change touching only test files "
        "has nothing for `--source` to point at and the gate cannot return a meaningful verdict"
    ),
    "adversarial-review": (
        "adversarial.review(worktree, reviewers=['vibe','gemini']) — refute-mode "
        "minority-veto panel; use when being wrong is expensive, not for routine "
        "review. CALLABLE AT ANY SURFACE: its `{kind: closer_gate, name: "
        "high_stakes_review}` matcher is how the CLOSER LANE enters it automatically, "
        "not a precondition for calling it — an audit phase invokes this function "
        "directly and needs no gate lifted. BOUNDARY: it buys JUDGEMENT under "
        "disagreement. Where the refutation is mechanical — re-run the guard with the "
        "break in place and read the exit code — a panel adds nothing a shell does not"
    ),
    "frontend-verifier": (
        "frontend_verify.py --doctor first (it reports its own readiness), then drive the "
        "audited repo's surface. BOUNDARY: it acts on the AUDITED repo, never this tool, and "
        "needs an observable surface to exist at all — pass `repository` and `repo_path` to the "
        "consult and that condition is answered for you instead of investigated"
    ),
    "repo-playbook": (
        "repo_knowledge.context_for(repo, task_type=...) — the per-repo conventions, gotchas, "
        "base branch and recorded per-capability contraindications for one fleet repo; a repo "
        "invariant carries no task_type scope, so a review consult sees the same set an "
        "implement consult does"
    ),
    "ux-review": "run the /ux-review skill; drives every primary surface, not the happy path",
    "epic-decomposition": (
        "only for a PARENT epic ([Epic] with no #NNN parent ref); produces a "
        "subtask plan, does not implement"
    ),
    "cross-repo-coordination": (
        "label `consumer-sync`/`cross-repo`; produces a dry-run rollout plan "
        "with barrier ordering, creates nothing"
    ),
    "deliberate-break-verifier": (
        "local_verify.py --worktree . --test-cmd '<the gate>' --test-path <file> — proves a test "
        "gate actually fails when the behaviour is broken, so a vacuous gate cannot pass. Add "
        "`--transcript` for the QUOTABLE red/green block an issue or PR body needs; without it the "
        "answer is a JSON verdict and the raw console output stays escaped inside it. PRECONDITION: "
        "the fix must already be in the worktree — it grades a gate against its implementation, so "
        "pointing it at a bare finding makes step 1 fail and that failure is not a verdict on the "
        "finding"
    ),
    "docs-drift-fix-agent": (
        "bounded docs-drift repair BATCHES from an existing drift scan; it does "
        "not do a semantic docs review and edits nothing itself"
    ),
    "runtime-ac-checks": (
        "runtime_ac.py — turns acceptance criteria into a structured evidence plan; "
        "execution is opt-in via --confirm-run and mutates nothing"
    ),
    # ---- THE SELF-SCOPED FIVE, added 2026-08-24. Every one of these came back to the
    # Counter_Risk audit with `how_to_use: null`, and the auditor read that beside a failed
    # precondition and concluded the answer SUPPRESSES guidance when a precondition fails. It does
    # not — `_attach_how_to_use` stamps unconditionally on both branches. The correlation was a
    # coincidence of populations: these five are exactly the `applies_to='self'` rows AND they were
    # among the 29 of 39 bound capabilities this table had no entry for. Recorded because the
    # mis-diagnosis would have sent the next fixer hunting a branch that does not exist.
    #
    # `how_to_use` answers "how do I invoke the instrument"; `CAPABILITY_PRECONDITIONS[...]
    # ["concept"]` answers "what question does it ask, if I have to ask it by hand elsewhere".
    # Different questions, so they are declared in different places and neither restates the other.
    "feature-scan": (
        "feature_scan.py --json — scans this tool's own tree for reusable structures that exist in "
        "code and were never logged in features.py, so the rule-of-three ladder stops depending on "
        "someone remembering to add an entry; --apply logs the unlogged ones at `ad-hoc`. Reports "
        "only, and it edits no source"
    ),
    "capability-activation-audit": (
        "capability_activation_audit.py --json — asks whether each capability CAN fire (matcher "
        "reachable, entrypoint present, consumer wired), never whether it did; --snapshot records "
        "today's state and --progress diffs against the recorded ones, which is what makes a "
        "reachability regression visible. BOUNDARY: can-fire only — pair it with "
        "capability-firing-monitor for does-fire, because a dispatch count cannot tell 'nobody "
        "needs this' from 'the trigger physically cannot fire' and those demand opposite fixes"
    ),
    "capability-firing-monitor": (
        "capability_firing_monitor.py --json — the does-fire counterpart: it PERSISTS per-capability "
        "firing history, so one that fired last week and went silent this week stops looking "
        "identical to one that has been healthy throughout. BOUNDARY: it detects the silence and "
        "cannot say why; capability-activation-audit answers whether it still could fire"
    ),
    "capability-propensity": (
        "capability_propensity.py report --json for the standing picture; `useful` / `not-useful` / "
        "`decline` / `find` each record one verdict, and every one is refused without evidence. "
        "`useful` also REFUSES without --provenance {defect_found | outcome_corroborated | "
        "machine_observed | self_reported} — it had a silent default of self_reported at weight "
        "0.25 until 2026-08-25, and this very sentence warned about it in prose while two more runs "
        "walked into it. Judge on the FIRST recording: recording APPENDS, so a verdict filed at the "
        "wrong tier can only be diluted by a second observation that double-counts the trial, never "
        "upgraded. Name --judge in the same command for the same reason"
    ),
    "switch-review": (
        "switch_review.py --json — the weekly re-raise for held switches: it re-reports every "
        "bounded trial and gated flag whose window expired with no decision recorded, so a deferral "
        "cannot decay into a silent revert. Reports only; it never flips a switch"
    ),
    # ---- AND THE THREE WITH NO PRECONDITION AT ALL, which the same audit also saw as null. Their
    # `precondition_met` was unset, so the note said nothing either: no verdict AND no guidance.
    "partitioned-review": (
        "partitioned_review.py prepare --corpus <json> --plan <path>, then run --plan <path> "
        "--agent <cursor|vibe|gemini|codex> --results-dir <dir> — deterministic partitioning for a "
        "corpus too large for one hand-off, with schema validation and fail-closed synthesis so a "
        "timed-out partition can never pass as a result. BOUNDARY: the transport is "
        "dispatcher.offload, so offload's own boundary applies to every partition"
    ),
    "role-triage": (
        "roles.py triage --backlog-json <path> — a router-chosen LLM sorts a backlog snapshot into "
        "work-now / defer / needs-scope / skip / monitor, with optional batches. SHADOW ONLY: "
        "advisory, it selects no worker, writes no claim and mutates no backlog state"
    ),
    "role-decomposer": (
        "roles.py decompose --goal '<the goal>' --repo owner/repo — a router-chosen LLM turns one "
        "large or vague goal into an epic_lane plan: subtasks, dependencies, integration order, "
        "final verification. SHADOW ONLY: it returns validated dispatch prompts and dispatches "
        "nothing. BOUNDARY: for a PARENT goal — a well-scoped issue needs no decomposition, and "
        "asking for one produces a plan-shaped restatement of it"
    ),
}


def format_advice(a: dict) -> str:
    verdict = "USE THE ORCHESTRATOR" if a["useful"] else "NO ORCHESTRATOR CAPABILITY APPLIES"
    lines = [f"{verdict} — {a['reason']}", ""]
    if a.get("surface_template"):
        # LOUD, because the alternative is a plausible wrong answer. `repo-audit:phase-N` resolves
        # by prefix to `repo-audit` alone -- one capability where the phase declares four -- and
        # nothing about the response would otherwise say so.
        lines += [
            f"!! SURFACE LOOKS LIKE AN UNSUBSTITUTED TEMPLATE ({a['surface_template']!r}) in "
            f"{a.get('surface')!r}. It resolved by PREFIX, so you got the surface-wide set, not "
            f"the phase's. Substitute the real value (e.g. `repo-audit:phase-3`) and re-ask.",
            "",
        ]
    state = a.get("surface_status") or {}
    if state.get("status") == "unknown":
        # LOUD, AND ABOVE THE ANSWER, because the answer below is about the TASK and the problem is
        # the SURFACE. `bound_count: 0` from a name nobody declares reads as "nothing applies here",
        # and a caller acted on exactly that reading while three capabilities sat at the surface it
        # meant. Printed, never acted on: the set and the order are unchanged.
        lines += [
            f"!! NO SUCH SURFACE: {state.get('surface')!r} is not declared anywhere, so nothing "
            f"could be bound to it. This is 'no such surface', NOT 'nothing applies'. Closest "
            f"declared surfaces: " + ", ".join(repr(s) for s in state.get("did_you_mean") or []),
            "",
        ]
    remedy = (a.get("precondition") or {}).get("how_to_evaluate")
    if remedy:
        # THE DRAINABLE QUANTITY, printed beside the blocking one. Without this the reader sees only
        # "precondition unevaluated" per entry and has no reason to think they could change that —
        # which is exactly what happened across three audit rounds.
        lines += [f"?? PRECONDITION(S) NOT EVALUATED — {remedy}", ""]
    guidance = a.get("guidance") or {}
    if guidance.get("undocumented"):
        # STATE THE CAUSE, because the alternative reading is a mechanism that does not exist. A
        # reader who sees `how_to_use: null` on three entries and a failed precondition on the same
        # three concludes the answer withholds guidance on a failed precondition; it does not, and
        # naming the table is what distinguishes a gap from a suppression.
        lines += [
            f"-- guidance: {guidance['documented']} of {guidance['offered']} offered "
            f"capability(ies) carry `how_to_use`. No entry in capability_advisor.HOW_TO_USE for: "
            f"{', '.join(guidance['undocumented'])}. That is a GAP IN THE TABLE, not a rule about "
            f"preconditions — guidance is stamped on every entry unconditionally.",
            "",
        ]
    if a["task_types"]:
        lines.append(f"classified as: {', '.join(a['task_types'])} (confidence: {a['confidence']})")
        for tt, hits in (a.get("classification_evidence") or {}).items():
            lines.append(f"  {tt}: matched on {', '.join(repr(h) for h in hits)}")
        lines.append("")
    for cap in a["capabilities"]:
        flag = "READY" if cap["dispatch_ready"] else "advisory"
        lines.append(f"- {cap['capability_id']}  [{flag}] via {cap['matched_task_type']}")
        lines.append(f"    entrypoint: {cap['entrypoint']}")
        lines.append(f"    blocker:    {cap['blocker']}")
        # READ FROM THE ENTRY, not from the table: the entry is what a caller receives, so a second
        # lookup here is how the rendering and the answer drifted apart in the first place.
        how = cap.get("how_to_use")
        lines.append(f"    how to use: {how}" if how else f"    next step:  {cap['next_step']}")
        if cap.get("entered_directly"):
            lines.append("    note:       entered directly in code — not routed by task type")
        if cap.get("contraindicated"):
            # A recorded per-repo gotcha that only appears under `--json` is prose nothing reads,
            # which is the defect one level up. Print it where a reader will see it.
            lines.append(f"    CONTRAINDICATED HERE: {cap.get('contraindication_reason')}")
            if cap.get("use_instead"):
                lines.append(f"    use instead:          {cap['use_instead']}")
        if cap.get("precondition_note"):
            lines.append(f"    PRECONDITION NOT MET: {cap['precondition_note']}")
            # READ FROM THE ENTRY, for the same reason `how_to_use` is: the entry is what an MCP
            # caller receives, and a field only this renderer looked up is invisible to every real
            # consult. Printed immediately under the note it completes.
            if cap.get("transferable_concept"):
                lines.append(f"    ASK IT BY HAND:       {cap['transferable_concept']}")
            lines.append(
                f"    if you decline:       record kind "
                f"{cap.get('suggested_decline_kind')!r} — it never counts against the "
                f"binding"
            )
        elif cap.get("unevaluated_because"):
            lines.append("    precondition unevaluated: " + "; ".join(cap["unevaluated_because"]))
    return "\n".join(lines) + "\n"


def _selftest_how_to_use() -> None:
    """`how_to_use` must reach a CALLER, and it must state the boundary as well as the call.

    ASSERTED ON THE ANSWER, NOT ON THE TABLE, because the table was never the problem: `HOW_TO_USE`
    was correct and complete and read by exactly one function — `format_advice`, the text rendering —
    while every real consult arrives through the MCP tool and receives the result DICT. The
    `adversarial-review` and `offload` repair proposals are what that costs: six offload declines all
    saying the work had to be first-person, and an adversarial-review decline naming a closer-lane
    matcher, both answered by text in this table that the decliner could not see.

    Every assertion below was written by breaking it first:
      * dropping `_attach_how_to_use` from the classified branch -> the classified probe fails;
      * dropping it from the classification-miss branch -> the binding-only probe fails (that branch
        is the one a free-text audit consult actually lands on, so covering only the other would
        leave the reported case untested);
      * deleting either boundary clause from the table -> part 1 fails.

    The ENTRY assertion is the load-bearing one and the render assertion is not a second copy of it:
    reverting `format_advice` to its own `HOW_TO_USE` lookup while the answer stops carrying the
    field leaves the printed line intact and fails only on the entry — which is the right way round,
    because the entry is what a caller receives and the text is what nobody consulting by MCP sees.
    """
    import tempfile
    from pathlib import Path

    # ---- PART 1: THE DATA. Code vs code, no ledger, so it runs on any machine. Each clause below is
    # a specific standing repair proposal's evidence, which is why the words are asserted and not
    # merely the presence of a key.
    off = HOW_TO_USE["offload"]
    assert "BOUNDARY" in off and "first-person" in off, off
    # CORRECTED 2026-08-24. This line used to assert "read-only" and "text-returning" — it pinned a
    # FALSE claim, so the test actively defended the defect and would have failed the fix. It now
    # pins the true mechanical facts instead (workspace-write default, isolate for parallel code
    # offloads), which is strictly stronger: re-introducing the read-only wording cannot satisfy it.
    assert "workspace-write" in off and "isolate=True" in off, off
    assert "read-only" not in off, (
        "offload is not read-only; see adapters sandbox selection: " + off
    )
    # BREADTH IS PINNED AS WELL AS THE CAVEAT, because the failure this entry keeps having is
    # narrowing by OMISSION, not by false statement. An entry that merely dropped the read-only
    # claim would say nothing about scope, and a capability described only in terms of reading is
    # selected only for reading -- the same lost work, with nothing wrong on the page to notice.
    assert "FULL RANGE OF WHAT AN LLM CAN DO" in off, (
        "the entry must state offload's breadth outright, not only what it cannot take: " + off
    )
    # BOTH boundaries, and only both. Naming just one re-opens the other as a thing readers guess
    # at; `delegate` is what a caller needs when the deliverable is a PR, so the entry has to say
    # so rather than leave "opens no PR" reading like a shortcoming.
    assert "FLEET ACTIONS" in off and "dispatcher.delegate" in off, (
        "the fleet-action boundary must name delegate as its answer: " + off
    )
    adv = HOW_TO_USE["adversarial-review"]
    assert "CALLABLE AT ANY SURFACE" in adv, adv
    assert "closer_gate" in adv and "not a precondition for calling it" in adv, adv
    fev = HOW_TO_USE["frontend-verifier"]
    assert "repo_path" in fev and "observable surface" in fev, fev
    # A CAPABILITY WITH TWO ROUTES MUST DOCUMENT BOTH (2026-08-25). `testgen-lane`'s declared
    # entrypoint is `testgen_lane.py/testgen_gate.py` and this entry described only the lane half —
    # the half unavailable to a seat that is implementing rather than dispatching. Guidance that
    # names one of two routes does not merely omit, it STEERS AWAY: the capability was declined for
    # "the lane route is not available here" while the gate half was a complete standalone CLI. The
    # general rule is stated here and pinned in the specific case, in the same style as the offload
    # and adversarial-review clauses above: each is a standing repair proposal's evidence.
    tgl = HOW_TO_USE["testgen-lane"]
    assert "label the issue `testing`" in tgl, "the lane route must survive: " + tgl
    assert "testgen_gate.py --repo" in tgl, (
        "the DIRECT route must be named — the lane route is unavailable in-seat, and an entry that "
        "names only it converts a usable capability into a decline: " + tgl
    )
    # THE TWO INVOCATION RULES THAT EACH COST A WASTED RUN, pinned so a later tidy cannot drop them.
    assert "IMPORTABLE" in tgl and "not a file path" in tgl, tgl
    assert "shell-style" in tgl and "quoted INSIDE" in tgl, tgl
    assert "BOUNDARY" in tgl and "PRODUCTION source module" in tgl, tgl
    for cap_id, how in sorted(HOW_TO_USE.items()):
        assert isinstance(how, str) and how.strip(), cap_id

    # ---- PART 2: WHAT A CALLER RECEIVES. Synthetic ledger and a synthetic table entry, so this
    # asserts the ANSWER on any machine rather than this instance's rows.
    with tempfile.TemporaryDirectory(prefix="howto-advise-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("aaa-documented", "zzz-undocumented"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            cap["entrypoint"] = f"{cid}.py"
            rows[cid] = cap
        capabilities.save(rows, ledger)
        real_surface = SURFACE_BINDINGS.get("t-howto")
        SURFACE_BINDINGS["t-howto"] = {
            "aaa-documented": "bound, documented",
            "zzz-undocumented": "bound, undocumented",
        }
        HOW_TO_USE["aaa-documented"] = "call aaa_documented.run(); BOUNDARY: it cannot do zzz"
        try:
            # Both return branches: a task that CLASSIFIES, and one that does not.
            for task, branch in (
                ("add unit tests for the retry helper", "classified"),
                ("xyzzy plugh frobnicate", "binding_only"),
            ):
                got = advise(task, surface="t-howto", path=ledger, record=False)
                by_id = {c["capability_id"]: c for c in got["capabilities"]}
                assert "aaa-documented" in by_id, (branch, sorted(by_id))
                assert by_id["aaa-documented"]["how_to_use"] == HOW_TO_USE["aaa-documented"], (
                    f"the {branch} branch did not hand the caller `how_to_use`; the MCP tool "
                    "returns this dict, so a field only `format_advice` reads is invisible to "
                    "every real consult",
                    branch,
                    by_id["aaa-documented"],
                )
                # PRESENT AND None, never absent: a caller must be able to tell "no guidance
                # recorded" from "this answer does not carry the field at all".
                assert "how_to_use" in by_id["zzz-undocumented"], by_id["zzz-undocumented"]
                assert by_id["zzz-undocumented"]["how_to_use"] is None, by_id["zzz-undocumented"]
                # ...and the RENDER must agree with the ANSWER. Reading the table twice is how the
                # two drifted apart, so the printed line has to come from the entry.
                text = format_advice(got)
                assert "how to use: call aaa_documented.run()" in text, (branch, text)
                assert "next step:" in text, (branch, text)
                # ...and the GAP is reported as a gap. `zzz-undocumented` has no table entry, and
                # the count plus the named id is what stops the next reader inferring a rule about
                # preconditions from a per-entry null. That inference was actually made.
                assert got["guidance"]["offered"] == 2, got["guidance"]
                assert got["guidance"]["documented"] == 1, got["guidance"]
                assert got["guidance"]["undocumented"] == ["zzz-undocumented"], got["guidance"]
                assert "GAP IN THE TABLE" in text and "zzz-undocumented" in text, (branch, text)
        finally:
            HOW_TO_USE.pop("aaa-documented", None)
            if real_surface is None:
                SURFACE_BINDINGS.pop("t-howto", None)
            else:
                SURFACE_BINDINGS["t-howto"] = real_surface

    # ---- PART 3: THE CONCEPT BEHIND "the concept may transfer". Code vs code first, then the
    # ANSWER. `precondition_note` invites a reader to transfer the concept by hand and, until
    # 2026-08-24, said nothing about what the concept WAS -- an audit did transfer `feature-scan`'s,
    # produced two findings with it, and had to reconstruct the question from the capability's name.
    for cap_id, decl in sorted(CAPABILITY_PRECONDITIONS.items()):
        if decl.get("applies_to") != APPLIES_SELF:
            continue
        concept = decl.get("concept")
        assert isinstance(concept, str) and concept.strip(), (
            f"{cap_id} declares applies_to='self', so every consult from an audit of another "
            "repository gets 'the concept may transfer' -- an invitation with nothing behind it "
            "unless `concept` says what to ask",
            cap_id,
        )
        # Repository-neutral by construction: the point is that it transfers.
        assert SELF_REPOSITORY not in concept, (cap_id, concept)
        assert cap_id in HOW_TO_USE, (
            f"{cap_id} is self-scoped, so it needs BOTH: `concept` for the audit that must ask the "
            "question by hand, and `how_to_use` for the consult where the instrument does apply. "
            "The two answer different questions and neither substitutes for the other",
            cap_id,
        )

    # ---- PART 4: WHAT A CALLER RECEIVES, and the discrimination that matters -- delivered on a
    # SCOPE mismatch, withheld on a REQUIREMENT failure, where there is no question left to ask.
    with tempfile.TemporaryDirectory(prefix="howto-concept-") as td:
        root = Path(td)
        ledger = root / "capabilities.json"
        bare = root / "bare-checkout"  # no HTML, no framework: `observable_surface` is False here
        bare.mkdir()
        rows = {}
        for cid in ("aaa-selfscoped", "bbb-needs-surface", "zzz-nodeclaration"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            cap["entrypoint"] = f"{cid}.py"
            rows[cid] = cap
        capabilities.save(rows, ledger)
        real_surface = SURFACE_BINDINGS.get("t-concept")
        SURFACE_BINDINGS["t-concept"] = {cid: "bound for the concept probe" for cid in rows}
        CAPABILITY_PRECONDITIONS["aaa-selfscoped"] = {
            "applies_to": APPLIES_SELF,
            "concept": "ask which of this repository's own gates expired with no decision",
        }
        CAPABILITY_PRECONDITIONS["bbb-needs-surface"] = {
            "applies_to": APPLIES_BOTH,
            "requires": "observable_surface",
            "concept": "declared, and it must still NOT be delivered on a requirement failure",
        }
        try:
            got = advise(
                "add unit tests for the retry helper",
                surface="t-concept",
                repository="stranske/Counter_Risk",
                repo_path=str(bare),
                path=ledger,
                record=False,
            )
            by_id = {c["capability_id"]: c for c in got["capabilities"]}
            self_scoped = by_id["aaa-selfscoped"]
            assert self_scoped["precondition_met"] is False, self_scoped
            assert self_scoped["transferable_concept"] == (
                CAPABILITY_PRECONDITIONS["aaa-selfscoped"]["concept"]
            ), (
                "the scope mismatch fired and the caller got the invitation without the content; "
                "the MCP tool returns this dict, so a concept only `format_advice` looked up is "
                "invisible to every real consult",
                self_scoped,
            )
            # THE DISCRIMINATOR. Same failed precondition, different reason: this repository has no
            # observable surface at all, so there is no question to transfer and offering one would
            # rebuild the empty-invitation defect facing the other way.
            needs_surface = by_id["bbb-needs-surface"]
            assert needs_surface["precondition_met"] is False, needs_surface
            assert needs_surface["scope_match"] is True, needs_surface
            assert needs_surface["transferable_concept"] is None, (
                "a REQUIREMENT failure has no transferable question -- the concept must ride on "
                "the scope mismatch only",
                needs_surface,
            )
            # PRESENT AND None, never absent, on an entry that declares nothing at all.
            assert "transferable_concept" in by_id["zzz-nodeclaration"], by_id["zzz-nodeclaration"]
            assert by_id["zzz-nodeclaration"]["transferable_concept"] is None, by_id[
                "zzz-nodeclaration"
            ]
            # ...and the RENDER agrees with the ANSWER, reading the entry rather than the table.
            text = format_advice(got)
            assert "ASK IT BY HAND:" in text, text
            assert CAPABILITY_PRECONDITIONS["aaa-selfscoped"]["concept"] in text, text
            assert CAPABILITY_PRECONDITIONS["bbb-needs-surface"]["concept"] not in text, text
        finally:
            CAPABILITY_PRECONDITIONS.pop("aaa-selfscoped", None)
            CAPABILITY_PRECONDITIONS.pop("bbb-needs-surface", None)
            if real_surface is None:
                SURFACE_BINDINGS.pop("t-concept", None)
            else:
                SURFACE_BINDINGS["t-concept"] = real_surface
    print(
        "capability_advisor how-to-use selftest: OK (delivered on both branches, boundary clauses "
        "present, render reads the entry, self-scoped rows carry both concept and call, concept "
        "rides the scope mismatch only, table gaps reported as gaps)"
    )


def _selftest_front_door() -> None:
    """The front door must name the RIGHT capability for the tasks the fleet actually does.

    Each case here is a real failure observed on 2026-08-19: `offload` (196 runs/week, the most-used
    capability) was unreachable because TASK_SIGNALS had no entry for it AND its kind-based matcher
    can never match a task_type trigger; and the legacy-removal campaign — proven-valuable codemod
    work — classified as nothing at all.

    Every case names a capability the advisor must FIND, so the whole function needs those rows
    present. They are registered by running the system, not by checking out the tree.
    """
    gaps: list[str] = []
    if not env_prereq.runnable(
        gaps, env_prereq.ledger_rows_absent("offload", "codemod-campaign", "testgen-lane")
    ):
        env_prereq.report_gaps("capability_advisor.py front-door", gaps)
        return
    cases = [
        ("summarise these 200 pages of docs", "offload"),
        ("offload this big read to a cheap agent", "offload"),
        ("remove the legacy config shapes across the whole repo, phase 6", "codemod-campaign"),
        ("Legacy removal Phase 8: enforce zero-reference gates", "codemod-campaign"),
        ("add pytest coverage for the retry helper", "testgen-lane"),
    ]
    for text, expected in cases:
        r = advise(text, record=False)
        ids = [c["capability_id"] for c in r["capabilities"]]
        assert r["useful"], f"front door said NO to real work: {text!r}"
        assert expected in ids, f"{text!r} -> {ids}, expected {expected}"
    # It must still be able to say NO.
    for text in ("what did I eat for lunch", "book a flight to Lisbon"):
        assert not advise(text, record=False)["useful"], text
    # And the advice must be actionable, not a lifecycle instruction.
    text = format_advice(advise("summarise these 200 pages", record=False))
    assert "how to use:" in text and "dispatcher.offload" in text, text
    assert "entered directly" in text, text
    print(
        "capability_advisor front-door selftest: OK (offload + campaign reachable, says no, "
        "advice is actionable)"
    )


def _selftest_reach() -> None:
    """The front door's REACH is measured, so it cannot shrink in silence.

    SPLIT DELIBERATELY. The first version of this asserted a reach floor against the LIVE ledger and
    passed on this machine (41 rows) while failing CI (14 rows) -- a machine-local assertion wearing
    the clothes of a correctness test. The mechanism assertions are now built on a SYNTHETIC ledger
    so they run everywhere, and only the live-instance claims are prerequisite-gated with the
    missing thing NAMED. Isolation, not a skip: this makes CI check more, not less.

    Both live regressions this guards already happened:

    1. DRIFT. `DIRECT_ENTRY` was a one-entry literal whose comment claimed it mirrored
       `dispatcher.TASK_TYPE_CAPABILITY`. It did not, so the dispatcher routed `runtime_ac` to
       `runtime-ac-checks` while this advisor named `deliberate-break-verifier` for the same work.

    2. SILENT SHRINKAGE. `adversarial-review` and `docs-drift-fix-agent` both carry advisory match
       history, proving they were once reachable from free text. Their matchers were later tightened
       to `closer_gate` / `ci_workflow` shapes and they left the front door with no signal at all.
       Reach had never been measured, so it fell in silence.
    """
    import tempfile
    from pathlib import Path

    # ---- PART 1: THE MECHANISM. Synthetic ledger, no instance state, runs on every machine.
    with tempfile.TemporaryDirectory(prefix="advisor-reach-") as td:
        ledger = Path(td) / "capabilities.json"
        routed = capabilities._blank_capability("routed-lane")
        routed["status"] = "generated"
        routed["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
        gated = capabilities._blank_capability("gate-cap")
        gated["status"] = "generated"
        gated["matcher"] = {"kind": "closer_gate", "name": "high_stakes_review"}
        flagged = capabilities._blank_capability("flag-cap")
        flagged["status"] = "generated"
        flagged["matcher"] = {"kind": "env", "name": "ORCH_ADVISOR_REACH_SELFTEST", "equals": "1"}
        capabilities.save({"routed-lane": routed, "gate-cap": gated, "flag-cap": flagged}, ledger)

        task = "add unit tests for the retry helper"
        plain = advise(task, path=ledger, record=False)
        ids = {m["capability_id"] for m in plain["capabilities"]}
        assert ids == {"routed-lane"}, ids

        # THE POINT: a kind-based capability is NOT a silent absence. It reports the reason
        # `_matches_trigger` already returned, plus what would make it engage.
        # CONTAINMENT, not equality: `capabilities.load` seeds KNOWN_DECLARATIONS into any ledger
        # it reads, so a temp ledger is never only what was written to it. Asserting equality here
        # passed locally and would have broken the moment the declaration set changed -- a test
        # coupled to an unrelated constant. The mechanism is what matters, so assert that.
        na = {r["capability_id"]: r for r in plain["not_applicable"]}
        assert {"gate-cap", "flag-cap"} <= set(na), (
            "kind-based capabilities came back as SILENCE, not as named non-matches; "
            f"not_applicable held {sorted(na)}"
        )
        assert na["gate-cap"]["why_not"] == ["closer_gate_not_in_trigger"], na["gate-cap"]
        assert na["gate-cap"]["requirement"]["mode"] == "entered_at", na["gate-cap"]
        assert na["gate-cap"]["requirement"]["kind"] == "closer_gate", na["gate-cap"]
        assert na["gate-cap"]["requirement"]["name"] == "high_stakes_review", na["gate-cap"]
        assert na["flag-cap"]["requirement"]["mode"] == "env_gated", na["flag-cap"]
        assert na["flag-cap"]["requirement"]["flag"] == "ORCH_ADVISOR_REACH_SELFTEST"
        for row in plain["not_applicable"]:
            assert row["why_not"], row
            assert row["requirement"]["detail"], row

        # WHOLE DENOMINATOR: every live capability in THIS ledger either matched or was named
        # with a reason. Computed from the ledger, never hardcoded -- a literal here would be the
        # "convenient denominator" this assertion exists to forbid.
        live = {
            cid
            for cid, cap in capabilities.load_declared(ledger).items()
            if cap.get("status") not in {"retired", "superseded"}
        }
        assert live <= (ids | set(na)), f"unaccounted: {sorted(live - (ids | set(na)))}"
        assert plain["coverage"]["ledger_count"] >= 3, plain["coverage"]

        # CONTEXT IS THE MECHANISM. `capabilities._matches_trigger` matches a kind against a
        # same-named field THE CALLER SUPPLIES, so supplying it reaches further -- this is what
        # makes "structurally unreachable" false.
        rich = advise(
            task, path=ledger, record=False, context={"closer_gate": "high_stakes_review"}
        )
        assert "gate-cap" in {m["capability_id"] for m in rich["capabilities"]}, rich

        # ...and it must still FAIL CLOSED on absent, empty, or WRONG context. Widening what can
        # be answered must never widen what is assumed.
        for ctx in (
            {},
            {"closer_gate": ""},
            {"closer_gate": None},
            {"closer_gate": "some_other_gate"},
        ):
            got = {
                m["capability_id"]
                for m in advise(task, path=ledger, record=False, context=ctx)["capabilities"]
            }
            assert "gate-cap" not in got, (ctx, got)

    # ---- PART 2: THE DRIFT GUARD, code vs code. No ledger, so it runs on every machine --
    # including the clean runner, which is exactly where this drift would otherwise land unseen.
    table = _dispatcher_task_type_capability()
    assert table, "dispatcher.TASK_TYPE_CAPABILITY unreadable; advisor reach would silently degrade"
    entry = direct_entry()
    for task_type, cap_id in table.items():
        assert entry.get(task_type) == cap_id, (
            f"dispatcher routes {task_type!r} to {cap_id!r} but the advisor's direct-entry map says "
            f"{entry.get(task_type)!r}; the two halves of the system disagree about one task type"
        )
    assert entry.get("offload") == "offload", entry
    # Every task_type the dispatcher knows must be a task_type this advisor can classify, or the
    # mapping is unreachable in practice.
    for task_type in table:
        assert (
            task_type in TASK_SIGNALS
        ), f"dispatcher routes {task_type!r}, advisor cannot classify it"

    # REACH SHRINKAGE IS NOT CHECKED HERE ON PURPOSE. `capability_activation_audit.advisor_reach`
    # owns it: it holds the declared-reach baseline and raises `advisor_reach_regression`, so a
    # second copy here would be a parallel inventory -- the thing this project forbids. Matchers
    # live only in the machine-local ledger, so a reach floor asserted here could only ever skip
    # on a clean runner, spending a skip ceiling to check nothing where it matters.


def _selftest_contraindications() -> None:
    """A repo's recorded contraindication must reach the CALLER, on both answer paths.

    Reproduces the reported case exactly: `repo-audit:phase-2` on a repo whose own audit record says
    `frontend_verify.py` does not work against its Streamlit SPA. The candidate must still be
    offered (concealing it would deny it the evidence that could clear it), must carry the reason
    and the alternative, and must rank last within its partition.
    """
    import json
    import tempfile
    from pathlib import Path

    import repo_knowledge

    with tempfile.TemporaryDirectory(prefix="contra-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("frontend-verifier", "repo-playbook"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"kind": "closer_gate", "name": "g"}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        registry = Path(td) / "repo_knowledge.json"
        registry.write_text(
            json.dumps(
                {
                    "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
                    "repos": {
                        "o/spa": {
                            "summary": "s",
                            "contraindications": [
                                {
                                    "capability": "frontend-verifier",
                                    "reason": "snapshots before the websocket render completes",
                                    "instead": "drive a real browser",
                                    "evidence": "the repo's own audit record",
                                }
                            ],
                        },
                        "o/plain": {"summary": "s"},
                    },
                },
                indent=2,
            )
            + "\n"
        )

        real_reg, real_binding = repo_knowledge.REG, SURFACE_BINDINGS.get("t-contra")
        repo_knowledge.REG = registry
        SURFACE_BINDINGS["t-contra"] = {
            "frontend-verifier": "bound in general",
            "repo-playbook": "carries the per-repo gotchas",
        }
        try:
            for task in (
                "xyzzy plugh frobnicate",  # binding_only path
                "review the closer gate",
            ):  # classified path
                got = advise(
                    task, surface="t-contra", repository="o/spa", path=ledger, record=False
                )
                ids = [m["capability_id"] for m in got["capabilities"]]
                assert "frontend-verifier" in ids, (task, ids)  # offered, never concealed
                assert got["contraindicated"] == ["frontend-verifier"], (
                    task,
                    got["contraindicated"],
                )
                flagged = [
                    m for m in got["capabilities"] if m["capability_id"] == "frontend-verifier"
                ][0]
                assert flagged["contraindicated"] is True, flagged
                assert "websocket" in flagged["contraindication_reason"], flagged
                assert flagged["use_instead"] == "drive a real browser", flagged
                assert flagged["contraindication_evidence"], flagged
                assert ids[-1] == "frontend-verifier", (task, ids)  # last within its partition

            # A repo with no recorded contraindication is untouched, and so is a missing repository.
            for repository in ("o/plain", ""):
                quiet = advise(
                    "xyzzy plugh frobnicate",
                    surface="t-contra",
                    repository=repository,
                    path=ledger,
                    record=False,
                )
                assert quiet["contraindicated"] == [], (repository, quiet["contraindicated"])
                assert not any(m.get("contraindicated") for m in quiet["capabilities"]), quiet

            # DELIBERATE BREAK -> REVERT: an unreadable registry must degrade to plain advice, not
            # to an exception -- the same discipline as the propensity import. The path must be
            # genuinely UNUSABLE, not merely absent: repo_knowledge.load() CREATES an absent
            # registry from its SEED, so a missing path would return [] because the seed has no such
            # repo, and the assertion would pass without ever reaching the failure branch. Parenting
            # it under a regular file makes the mkdir raise for real.
            blocker = Path(td) / "not-a-directory"
            blocker.write_text("")
            repo_knowledge.REG = blocker / "x.json"
            degraded = advise(
                "xyzzy plugh frobnicate",
                surface="t-contra",
                repository="o/spa",
                path=ledger,
                record=False,
            )
            assert degraded["contraindicated"] == [], degraded["contraindicated"]
            assert len(degraded["capabilities"]) == 2, degraded
            repo_knowledge.REG = registry
            assert advise(
                "xyzzy plugh frobnicate",
                surface="t-contra",
                repository="o/spa",
                path=ledger,
                record=False,
            )["contraindicated"] == ["frontend-verifier"]
        finally:
            repo_knowledge.REG = real_reg
            if real_binding is None:
                SURFACE_BINDINGS.pop("t-contra", None)
            else:
                SURFACE_BINDINGS["t-contra"] = real_binding
    print(
        "capability_advisor contraindication selftest: OK (offered not concealed, reason + "
        "alternative reach the caller on both paths, ranks last, degrades quietly)"
    )


def _selftest_bindings() -> None:
    """The declared binding must work where the classifier does not, and must never conceal.

    Both halves are failures this implementation actually had. The first version returned NOTHING for
    a surface with five declared capabilities whenever the task text missed the keyword vocabulary --
    the binding depending on the classifier, which is the one thing it exists not to do.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="binding-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid, matcher in (
            ("bound-a", {"kind": "closer_gate", "name": "g"}),
            ("bound-b", {"kind": "closer_gate", "name": "g"}),
            ("unbound-testgen", {"field": "task_type", "operator": "in", "value": ["testgen"]}),
            ("gone", {"kind": "closer_gate", "name": "g"}),
        ):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "retired" if cid == "gone" else "generated"
            cap["matcher"] = matcher
            rows[cid] = cap
        capabilities.save(rows, ledger)

        real = SURFACE_BINDINGS.get("t-surface")
        SURFACE_BINDINGS["t-surface"] = {
            "bound-a": "because a",
            "bound-b": "because b",
            "gone": "retired, must be filtered",
        }
        try:
            # 1. CLASSIFICATION MISS: the binding still answers. This is the whole point.
            miss = advise("xyzzy plugh frobnicate", surface="t-surface", path=ledger, record=False)
            ids = [m["capability_id"] for m in miss["capabilities"]]
            assert sorted(ids) == ["bound-a", "bound-b"], ids
            assert miss["confidence"] == "binding_only", miss["confidence"]
            assert miss["bound_count"] == 2, miss
            assert all(m["binding_reason"] for m in miss["capabilities"]), miss["capabilities"]
            # A retired capability must never be bound in.
            assert "gone" not in ids, ids
            # ...and with NO surface the same text still answers nothing, so the binding is what
            # made the difference rather than a loosened classifier.
            bare = advise("xyzzy plugh frobnicate", path=ledger, record=False)
            assert bare["capabilities"] == [], bare

            # 1b. AND IT MUST RECORD. A binding-only answer that records nothing is a latched gate
            # one layer down: the surface gets its capabilities, but `capability_propensity` sees a
            # trial with no candidates, so it has no control arm, no attributable skill, and no
            # denominator for a trigger rate. Measured 2026-08-22 while wiring the tick — whose
            # cadence text classifies as nothing, so it takes this branch on EVERY consult.
            # Asserted through the LEDGER, which is what a downstream reader actually sees.
            rec = advise(
                "plugh xyzzy nothing classifies here",
                surface="t-surface",
                skill="t-surface",
                path=ledger,
            )
            assert rec["confidence"] == "binding_only", rec["confidence"]
            assert rec.get("recorded_matches") == 2, rec.get("recorded_matches")
            import capability_propensity as _prop

            trial = next(
                t
                for t in _prop.experiments(path=ledger)
                if t["experiment_id"] == rec["experiment_id"]
            )
            assert sorted(trial["candidates"]) == ["bound-a", "bound-b"], trial
            assert trial["skills"] == ["t-surface"], trial
            # 1c. SURFACE-ONLY IS STILL ATTRIBUTABLE. The CLI has no `--skill` flag, so every
            # `--surface` consult that misses the classifier lands here with `skill=""` -- and this
            # branch used to pass only `skill` to `_record_matches`, writing `surface: null` too.
            # The candidate set then existed in the ledger and belonged to nobody, so
            # `propose_demotions` and `missed_selection` could never drain the surface that produced
            # it. Asserted through `experiments()`, which reads BOTH keys as one attribution axis.
            surf_only = advise(
                "plugh xyzzy surface only no skill given",
                surface="t-surface",
                path=ledger,
            )
            assert surf_only["confidence"] == "binding_only", surf_only["confidence"]
            assert surf_only.get("recorded_matches") == 2, surf_only.get("recorded_matches")
            so_trial = next(
                t
                for t in _prop.experiments(path=ledger)
                if t["experiment_id"] == surf_only["experiment_id"]
            )
            assert so_trial["skills"] == ["t-surface"], (
                f"a surface-only binding-only consult is unattributable: {so_trial['skills']} -- "
                f"the candidate set is recorded and no drain can locate it"
            )
            # ...and the same question again must not inflate the count.
            again = advise(
                "plugh xyzzy nothing classifies here",
                surface="t-surface",
                skill="t-surface",
                path=ledger,
            )
            assert again.get("recorded_matches") == 0, again.get("recorded_matches")
            # record=False stays a pure query on this branch too.
            pure = advise(
                "plugh xyzzy nothing classifies here either",
                surface="t-surface",
                path=ledger,
                record=False,
            )
            assert "recorded_matches" not in pure, pure
            assert not any(
                t["experiment_id"] == pure["experiment_id"] for t in _prop.experiments(path=ledger)
            ), "record=False wrote a trial on the binding-only branch"

            # 2. NEVER CONCEAL. A classifying task must still return the unbound match, ranked after
            # the bound ones -- a hidden capability can never earn the evidence that would bind it.
            hit = advise(
                "add unit tests for the retry helper",
                surface="t-surface",
                path=ledger,
                record=False,
            )
            hid = [m["capability_id"] for m in hit["capabilities"]]
            assert "unbound-testgen" in hid, hid
            assert set(hid) >= {"bound-a", "bound-b", "unbound-testgen"}, hid
            first_unbound = next(i for i, m in enumerate(hit["capabilities"]) if not m.get("bound"))
            assert all(hit["capabilities"][i].get("bound") for i in range(first_unbound)), hid
            assert not any(
                hit["capabilities"][i].get("bound") for i in range(first_unbound, len(hid))
            ), hid

            # 3. SIZE, on the RESOLVED set. Asserting on the table entries would miss the case that
            # matters: a phase key merges with its surface, so the context a caller actually sees can
            # exceed the safe zone even when every table entry is small.
            for surface in SURFACE_BINDINGS:
                entries = SURFACE_BINDINGS[surface]
                assert all(str(why).strip() for why in entries.values()), surface
                if binding_suppressed(surface):
                    assert binding_for(surface) == {}, surface
                    continue
                resolved = binding_for(surface)
                assert 1 <= len(resolved) <= 10, (surface, len(resolved))

            # 4. PHASE RESOLUTION. A phase merges with its surface; a phase that declares NO_BINDING
            # suppresses the merge entirely. Both are real: `repo-audit` binds `offload` surface-wide,
            # and `repo-audit:phase-1` must still be EMPTY because the playbook says "bash only, NO
            # agents" -- inheriting into it would contradict the skill's own instruction.
            SURFACE_BINDINGS["t-proc"] = {"bound-a": "surface-wide"}
            SURFACE_BINDINGS["t-proc:p2"] = {"bound-b": "phase only"}
            SURFACE_BINDINGS["t-proc:p1"] = {NO_BINDING: "deliberately empty, for a stated reason"}
            try:
                assert sorted(binding_for("t-proc:p2")) == [
                    "bound-a",
                    "bound-b",
                ], "phase must merge"
                assert binding_for("t-proc:p1") == {}, "NO_BINDING must suppress inheritance"
                # AND THE WHOLE ANSWER, not just the declared half. Asserting only the binding is
                # what let phase-1 keep offering classifier matches at a bash-only phase.
                quiet = advise(
                    "add unit tests for the retry helper",
                    surface="t-proc:p1",
                    path=ledger,
                    record=False,
                )
                assert quiet["capabilities"] == [], quiet["capabilities"]
                assert quiet["confidence"] == "suppressed", quiet["confidence"]
                assert quiet["useful"] is False and suppressed_reason_in(quiet), quiet["reason"]
                assert binding_suppressed("t-proc:p1"), "and must SAY why it is empty"
                assert not binding_suppressed("t-proc:p2")
                # An unknown phase of a known surface still gets the surface-wide set.
                assert sorted(binding_for("t-proc:p9")) == ["bound-a"], binding_for("t-proc:p9")
                # EVERY PREFIX, not just the first split. `split(":", 1)` inherited from `a` and
                # silently skipped `a:b` for a three-part key -- a middle level that resolved to
                # nothing while looking like it resolved.
                SURFACE_BINDINGS["t-proc:p2:sub"] = {"bound-c": "deepest"}
                deep = binding_for("t-proc:p2:sub")
                assert sorted(deep) == ["bound-a", "bound-b", "bound-c"], deep
                # A phase-scoped advise() call must actually reach the phase's binding.
                ph = advise("xyzzy plugh", surface="t-proc:p2", path=ledger, record=False)
                assert ph["surface"] == "t-proc:p2", ph["surface"]
            finally:
                for k in ("t-proc", "t-proc:p1", "t-proc:p2", "t-proc:p2:sub"):
                    SURFACE_BINDINGS.pop(k, None)

            # 5. THE REAL TABLE, pinned where it is load-bearing.
            assert binding_for("repo-audit:phase-1") == {}, "phase 1 is bash-only by playbook"
            assert "deliberate-break-verifier" in binding_for(
                "repo-audit:phase-4"
            ), "phase 4 requires a named test gate with a deliberate-break proof"
            assert "adversarial-review" in binding_for(
                "repo-audit:phase-3"
            ), "phase 3 IS adversarial verification"
            # A BINDING MUST MATCH THE SHAPE, NOT THE WORD. `partitioned-review` sat at phase 2 on
            # the token "partition" while its corpus is a list of prior ASSERTIONS disposed against
            # current code -- so it was offered where no claim list exists yet and withheld from the
            # phase that produces one. Pinned in BOTH directions: dropping the phase-3 half would
            # silently restore the useless offer, and dropping the phase-2 half would let it come
            # back as a duplicate.
            assert "partitioned-review" in binding_for(
                "repo-audit:phase-3"
            ), "phase 3 disposes N candidate findings against the live tip -- the corpus shape"
            assert "partitioned-review" not in binding_for(
                "repo-audit:phase-2"
            ), "phase 2 discovers defects in source; there is no claim corpus to reconcile yet"
            # Dimensions are SIBLINGS of the phase, so a worker context must NOT inherit the
            # splitter's capabilities -- that inheritance is what would push a worker to 9-10.
            for d in range(1, 9):
                ctx = binding_for(f"repo-audit:dimension-{d}")
                assert "role-decomposer" not in ctx, (d, sorted(ctx))
                assert "partitioned-review" not in ctx, (d, sorted(ctx))
                assert 1 <= len(ctx) <= 6, (d, len(ctx))
            assert "offload" in binding_for(
                "repo-audit:dimension-5"
            ), "the playbook names offload outright for the public-field research dimension"
            # THE FIX ARC REACHES ITS OWN INSTRUMENTS (2026-08-25). Both were needed here, both
            # were absent, and the two absences had different symptoms: the verifier arrived only
            # via the keyword classifier, so a consult whose free text missed the vocabulary was
            # offered a fix arc with no way to PROVE its gate; the UI verifier was not reachable at
            # all and the run had to consult a different surface for it. Pinned on the RESOLVED set,
            # which is what a caller receives.
            fix_arc = binding_for("repo-audit:fix")
            assert "deliberate-break-verifier" in fix_arc, (
                "a fix must prove its gate fails without it, and the binding is the layer that "
                "does not depend on the classifier finding the right words"
            )
            assert "frontend-verifier" in fix_arc, (
                "a UI fix at this surface classifies as ux_review and had no UI verifier to be "
                "offered"
            )

            # 7. AN INVENTED SURFACE SAYS SO (2026-08-25). `bound_count: 0` from a name nobody
            #    declares used to read as "nothing applies here", and a run acted on that reading
            #    while three capabilities sat at the surface it meant. Asserted on the ANSWER and
            #    the RENDER, because the MCP tool returns the dict and the CLI prints the text.
            made_up = "totally-made-up-surface"
            assert surface_status(made_up)["status"] == "unknown", surface_status(made_up)
            for task in ("xyzzy plugh frobnicate", "add unit tests for the retry helper"):
                got = advise(task, surface=made_up, path=ledger, record=False)
                state = got["surface_status"]
                assert state["status"] == "unknown", (task, state)
                assert state["did_you_mean"], "a diagnosis must carry its remedy: " + str(state)
                assert "IS NOT A SURFACE" in got["reason"], (task, got["reason"])
                assert "NO SUCH SURFACE" in format_advice(got), task
            # A DECLARED SURFACE THAT SIMPLY BINDS NOTHING IS NOT UNKNOWN, and this is the
            # distinction the whole check exists to make. `t-empty` is declared and its resolved
            # set is empty; it must never print the unknown banner.
            real_empty = SURFACE_BINDINGS.get("t-empty")
            SURFACE_BINDINGS["t-empty"] = {NO_BINDING: "declared, deliberately empty"}
            try:
                assert surface_status("t-empty")["status"] == "declared", surface_status("t-empty")
                quiet = advise("xyzzy plugh", surface="t-empty", path=ledger, record=False)
                assert quiet["surface_status"]["status"] == "declared", quiet["surface_status"]
                assert "NO SUCH SURFACE" not in format_advice(quiet), format_advice(quiet)
            finally:
                if real_empty is None:
                    SURFACE_BINDINGS.pop("t-empty", None)
                else:
                    SURFACE_BINDINGS["t-empty"] = real_empty
            # A PHASE OF A KNOWN SURFACE INHERITS AND IS NOT UNKNOWN. `binding_for` resolves every
            # prefix, so firing here would fire on the normal case and the check would be switched
            # off within a week.
            assert surface_status("repo-audit:phase-9")["status"] == "inherited", surface_status(
                "repo-audit:phase-9"
            )
            # ...and NO surface at all was never asked about: three-valued, like the precondition
            # axis, so a caller that passed no `--surface` is not told it invented one.
            assert surface_status("")["status"] == "unspecified", surface_status("")
            none_given = advise("xyzzy plugh frobnicate", path=ledger, record=False)
            assert none_given["surface_status"]["status"] == "unspecified", none_given[
                "surface_status"
            ]
            assert "IS NOT A SURFACE" not in none_given["reason"], none_given["reason"]
            # AND THE AXIS CHANGES NEITHER THE SET NOR THE ORDER — the same restraint the
            # precondition and contraindication axes keep. Both consults bind nothing, so the two
            # candidate lists must be identical in membership AND order.
            classified_unknown = advise(
                "add unit tests for the retry helper", surface=made_up, path=ledger, record=False
            )
            classified_none = advise(
                "add unit tests for the retry helper", path=ledger, record=False
            )
            assert [c["capability_id"] for c in classified_unknown["capabilities"]] == [
                c["capability_id"] for c in classified_none["capabilities"]
            ], (classified_unknown["capabilities"], classified_none["capabilities"])

            # 6. THE TICK'S PHASES, and the ONE thing that must not move. `tick_evidence` grades
            #    `binding_for("tick")` and `_selftest_tick_evidence` requires every capability with
            #    a finding projection to be in it, so sub-surfacing the tick must ADD phase keys
            #    without draining the bare one. Asserted through a SYNTHETIC ledger path so a
            #    machine-local `binding_promotion` cannot make this pass or fail by accident.
            bare = binding_for("tick", path=ledger)
            assert bare, "the bare tick surface went empty; tick_evidence would grade nothing"
            for cap_id in (
                "switch-review",
                "capability-firing-monitor",
                "capability-activation-audit",
                "capability-propensity",
            ):
                assert cap_id in bare, (
                    f"{cap_id} left the bare `tick` binding. `capability_propensity.TICK_SURFACE` "
                    f"is 'tick' and grades exactly that set, so moving it into a phase silently "
                    f"stops the only producer of layer-2 usefulness evidence: {sorted(bare)}"
                )
            phases = tick_phase_surfaces()
            assert len(phases) >= 2, phases
            for phase in phases:
                ctx = binding_for(phase, path=ledger)
                # A phase must ADD to the surface-wide set, never merely restate it.
                own = {k for k in SURFACE_BINDINGS[phase] if k != NO_BINDING}
                assert own - set(bare), (
                    f"{phase} declares nothing the bare tick surface does not already bind, so it "
                    f"is a context with no reason to exist"
                )
                assert 1 <= len(ctx) <= 10, (phase, len(ctx), sorted(ctx))
        finally:
            if real is None:
                SURFACE_BINDINGS.pop("t-surface", None)
            else:
                SURFACE_BINDINGS["t-surface"] = real
    print(
        "capability_advisor binding selftest: OK (survives a classification miss, never conceals "
        "an unbound match, filters retired, bound sets stay small, the fix arc reaches its own "
        "instruments, and an invented surface says so instead of reading as 'nothing applies')"
    )


def _selftest_phase_consult() -> None:
    """The phase consult: a real caller for every sub-surface, bounded, and never a verdict.

    ASSERTS ON WHAT A CALLER RECEIVES, not on `SURFACE_BINDINGS` or on an internal helper. A prior
    binding bug in this module passed a table-shaped assertion while the answer a caller got was
    wrong, and an audit found it rather than the test.

    SYNTHETIC LEDGER THROUGHOUT. This machine's ledger holds 43 rows and a clean runner holds ~14,
    so nothing below names a real capability id or asserts that one comes back.
    """
    import tempfile
    from pathlib import Path

    # ---- PART 1: code vs code. Runs anywhere: no ledger, no state directory.
    #
    # THE CONSULT TEXT MUST STAY UNCLASSIFIABLE for every declared phase. A cadence is not one
    # free-text task, so the declared binding has to be the whole answer; a phase renamed to a word
    # in TASK_SIGNALS ("review", "audit", "test", "phase 4"...) would silently widen both the offer
    # and the recorded candidate set to whatever the keyword classifier happened to hit.
    for surface in tick_phase_surfaces():
        text = consult_text(surface, "2026-01-02")
        assert classify_task(text) == [], (
            f"the consult text for {surface} now hits the keyword classifier "
            f"({classify_task(text)}); rename the phase or the phrase, or the declared binding "
            f"stops being the whole answer"
        )
    # DISTINCT PER SURFACE. A shared digest would merge every phase into ONE trial whose candidate
    # set is the union of all of them -- the too-many-tools condition recreated inside the evidence,
    # with no per-phase control arm left.
    ids = [experiment_id(consult_text(s, "2026-01-02")) for s in tick_phase_surfaces()]
    assert len(set(ids)) == len(ids), f"phase consult ids collide: {ids}"
    # ...and STABLE PER DAY, distinct across days: that pairing is what bounds the write volume to
    # one match per capability per phase per day rather than one per tick.
    assert consult_text("tick:x", "2026-01-02") == consult_text("tick:x", "2026-01-02")
    assert consult_text("tick:x", "2026-01-02") != consult_text("tick:x", "2026-01-03")
    # AN UNSUBSTITUTED TEMPLATE MUST BE NAMED, on every answer path. `binding_for` resolves by
    # prefix, so `repo-audit:phase-N` silently returns the surface-wide set -- a plausible, wrong,
    # SMALLER answer with nothing to distinguish it from a correct one. Three audit runs under
    # identical instructions consulted 13, 9 and 2 distinct surfaces; this is what that looks like
    # from the inside. It reports and changes nothing else: same set, same order.
    for bad in ("repo-audit:phase-N", "repo-audit:dimension-N", "<surface>", "tick:{phase}"):
        assert unsubstituted_surface(bad), bad
        answer = advise("xyzzy plugh frobnicate", surface=bad, record=False)
        assert answer["surface_template"], (bad, answer.get("surface_template"))
        assert "UNSUBSTITUTED TEMPLATE" in format_advice(answer), bad
    for good in ("repo-audit:phase-3", "repo-audit:dimension-4", "closer-lane", "ci", "tick"):
        assert not unsubstituted_surface(good), good
        answer = advise("xyzzy plugh frobnicate", surface=good, record=False)
        assert answer["surface_template"] is None, (good, answer.get("surface_template"))
        assert "UNSUBSTITUTED TEMPLATE" not in format_advice(answer), good
    for phase in tick_phase_surfaces():
        assert not unsubstituted_surface(phase), f"a declared phase reads as a template: {phase}"
    # ...and the SUPPRESSED path reports it too: `repo-audit:phase-1` returns early, so a template
    # arriving there used to skip the check entirely.
    assert advise("x", surface="repo-audit:phase-1", record=False)["surface_template"] is None

    # DERIVED FROM THE TABLE, never a second list: a phase absent from the caller's list is exactly
    # the unreachable-binding defect this consult exists to remove, one level up.
    SURFACE_BINDINGS["tick:selftest-probe"] = {"probe-a": "temporary, selftest only"}
    try:
        assert "tick:selftest-probe" in tick_phase_surfaces(), tick_phase_surfaces()
    finally:
        SURFACE_BINDINGS.pop("tick:selftest-probe", None)
    assert "tick:selftest-probe" not in tick_phase_surfaces()

    # ---- PART 2: what a CALLER receives, on a synthetic ledger and synthetic phases.
    with tempfile.TemporaryDirectory(prefix="phase-consult-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("wide-1", "own-a", "own-b"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"kind": "tick_phase", "name": "probe"}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        saved = {k: SURFACE_BINDINGS.get(k) for k in ("tick", "tick:p-one", "tick:p-two")}
        try:
            SURFACE_BINDINGS["tick"] = {"wide-1": "surface-wide for the probe"}
            SURFACE_BINDINGS["tick:p-one"] = {"own-a": "phase one only"}
            SURFACE_BINDINGS["tick:p-two"] = {"own-b": "phase two only"}
            phases = ["tick:p-one", "tick:p-two"]

            rep = consult_phases(day="2026-01-02", surfaces=phases, path=ledger)
            assert rep["phases"] == 2, rep
            assert not rep["errors"], rep
            by_surface = {r["surface"]: r for r in rep["rows"]}
            # EACH PHASE GETS ITS OWN SET: its own declaration plus the surface-wide one, and NOT
            # the sibling phase's. That separation is the whole point of sub-surfacing.
            assert sorted(by_surface["tick:p-one"]["bound"]) == ["own-a", "wide-1"], by_surface
            assert sorted(by_surface["tick:p-two"]["bound"]) == ["own-b", "wide-1"], by_surface
            assert rep["recorded"] == 4, rep  # 2 capabilities x 2 phases, first run of the day
            # ...and the same day again records NOTHING. The idempotency key is a digest of the
            # consult text, so 24 ticks a day cost one write set, not 24.
            again = consult_phases(day="2026-01-02", surfaces=phases, path=ledger)
            assert again["recorded"] == 0, again
            assert again["offered"] == rep["offered"], "the OFFER must not shrink with the writes"
            # A NEW DAY IS A NEW OBSERVATION. If the day ever left the consult text this would drop
            # to 0 and the surface would be measured once, forever.
            tomorrow = consult_phases(day="2026-01-03", surfaces=phases, path=ledger)
            assert tomorrow["recorded"] == 4, tomorrow

            # THE VERDICT CEILING SURVIVES SUB-SURFACING. `capability_propensity.tick_evidence` is
            # the ONLY writer of usefulness verdicts and it reads `binding_for("tick")`, which this
            # change does not touch; the phase consult must add none of its own, or the ~1.3/day
            # bound #37 established would become 5x that overnight.
            import capability_propensity as _prop

            rows_after = _prop.usefulness(path=ledger)["rows"]
            resolved = {c: r["resolved"] for c, r in rows_after.items() if r["resolved"]}
            assert not resolved, f"the phase consult recorded usefulness verdicts: {resolved}"

            # record=False is a pure query: an offer, and no trial at all.
            pure = consult_phases(day="2026-01-04", surfaces=phases, path=ledger, record=False)
            assert pure["offered"] == rep["offered"], pure
            assert pure["recorded"] == 0, pure
            trials = {t["experiment_id"] for t in _prop.experiments(path=ledger)}
            for surface in phases:
                assert experiment_id(consult_text(surface, "2026-01-04")) not in trials, surface

            # ONE BROKEN PHASE MUST NOT SILENCE THE OTHERS. A surface whose name is not a string
            # blows up inside `advise`; the row carries the error and the loop continues.
            mixed = consult_phases(
                day="2026-01-05", surfaces=["tick:p-one", 42, "tick:p-two"], path=ledger
            )
            assert mixed["errors"] == [42] or 42 in mixed["errors"], mixed
            assert sum(1 for r in mixed["rows"] if not r.get("error")) == 2, mixed
        finally:
            for key, value in saved.items():
                if value is None:
                    SURFACE_BINDINGS.pop(key, None)
                else:
                    SURFACE_BINDINGS[key] = value

    # ---- PART 3: THE GUARD. A capability consult must never be able to fail the tick, so the
    # OUTER handler has to catch what the per-phase one cannot -- a bad call shape, an import
    # failure, a signal. Exercised with a kwarg `consult_phases` does not accept, which raises
    # before any phase loop runs.
    broken = consult_phases_guarded(day="2026-01-02", not_a_real_kwarg=True)
    assert broken.get("error"), broken
    assert broken["errors"] == ["*"], broken
    assert broken["recorded"] == 0 and broken["offered"] == 0, broken
    assert "FAILED" in format_phase_consult(broken), format_phase_consult(broken)
    # ...and a healthy run must NOT claim failure, or the marker above means nothing.
    assert "FAILED" not in format_phase_consult({"phases": 0, "rows": [], "day": "d"})
    print(
        "capability_advisor phase-consult selftest: OK (every declared phase is consulted, one "
        "match per capability per phase per day, no verdicts, fails open per phase)"
    )


def _selftest_preconditions() -> None:
    """The `applies_to` axis EXPLAINS an offer and changes nothing about it.

    The invariance assertion is the important one, and it is the coordinate the third audit round
    forced: the identical capability that was noise on two frontend-less repositories produced the
    highest evidence-to-effort finding of an audit on a repository that has a display surface. So
    this axis may add a verdict and a reason, and may NOT change the returned SET or its ORDER.

    Every assertion below was written by breaking it first:
      * making a failed precondition drop or sink the entry -> caught by the invariance checks;
      * treating an undeclared capability as a mismatch -> caught;
      * treating an unnamed repository as `self` -> caught;
      * treating `both` as a mismatch -> caught;
      * conflating "no checkout" with "no surface" -> caught.
    """
    import tempfile
    from pathlib import Path

    # ---- PART 1: THE VOCABULARY AND THE TABLE, code vs code, no ledger. Runs on any machine.
    for cap_id, spec in sorted(CAPABILITY_PRECONDITIONS.items()):
        # A CLOSED KEY SET, so a typo cannot become a declaration nothing reads. `concept` joined it
        # on 2026-08-24; `_selftest_how_to_use` holds the rule that makes it obligatory on a
        # self-scoped row, and this one only holds its shape.
        assert set(spec) <= {"applies_to", "requires", "concept"}, (cap_id, sorted(spec))
        if "applies_to" in spec:
            assert spec["applies_to"] in APPLIES_TO_VALUES, (cap_id, spec)
        if "requires" in spec:
            assert spec["requires"] in REPO_FACT_PROBES, (cap_id, spec)
        if "concept" in spec:
            assert isinstance(spec["concept"], str) and spec["concept"].strip(), (cap_id, spec)
            # ONE reader for the declaration, and it is the one `evaluate_precondition` calls.
            assert transferable_concept(cap_id) == spec["concept"], (cap_id, spec)
    assert CAPABILITY_PRECONDITIONS, "the declared set must not be empty, or nothing is evaluated"
    # The three-way target, and the conservative default that keeps the axis from reclassifying.
    assert consult_target(SELF_REPOSITORY) == APPLIES_SELF
    assert consult_target("stranske/Workflows") == APPLIES_AUDITED_REPO
    for blank in ("", "   ", None):
        assert consult_target(blank) == TARGET_UNKNOWN, blank

    # UNDECLARED IS UNTOUCHED: this is the "does not silently reclassify the 43" guarantee.
    v = evaluate_precondition("no-such-capability", repository="stranske/Workflows")
    assert v["applies_to"] is None and v["requires"] is None, v
    assert v["precondition_met"] is None and v["precondition_note"] is None, v
    assert v["unevaluated_because"] == [] and v["suggested_decline_kind"] is None, v

    # A `self` instrument aimed at an audited repo: FALSE, with a reason, and the decline kind that
    # `capability_propensity` treats as NON-DEMOTABLE. The two halves must agree or the axis explains
    # a mismatch while the ledger quietly demotes the binding for it.
    mism = evaluate_precondition("switch-review", repository="stranske/Workflows")
    assert mism["scope_match"] is False and mism["precondition_met"] is False, mism
    assert "the concept may transfer" in mism["precondition_note"], mism
    assert mism["suggested_decline_kind"] == "precondition_unmet", mism
    import capability_propensity

    assert capability_propensity.DECLINE_KINDS[mism["suggested_decline_kind"]]["demotable"] is False

    # Same instrument, this repository: TRUE.
    assert (
        evaluate_precondition("switch-review", repository=SELF_REPOSITORY)["precondition_met"]
        is True
    )
    # `both` matches either target; declaring it is behaviourally identical to declaring nothing.
    for repo in (SELF_REPOSITORY, "stranske/Workflows"):
        assert evaluate_precondition("offload", repository=repo)["precondition_met"] is True, repo
    # NO REPOSITORY NAMED must be UNEVALUATED, never a mismatch -- guessing `self` here would make
    # every bare consult report failures against every `audited_repo` capability.
    bare = evaluate_precondition("switch-review", repository="")
    assert bare["scope_match"] is None and bare["precondition_met"] is None, bare
    assert bare["unevaluated_because"] and "repository" in bare["unevaluated_because"][0], bare
    assert bare["suggested_decline_kind"] is None, bare

    # ---- PART 2: THE REPO FACT. A real probe over real trees, so the verdict is evidence.
    with tempfile.TemporaryDirectory(prefix="surface-probe-") as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "thing.py").write_text("x = 1\n")
        (root / "docs").mkdir()
        # A generated docs tree is FULL of HTML and is not an application surface. Counting it is
        # exactly what would turn this probe back into the false positive it exists to remove.
        (root / "docs" / "index.html").write_text("<html>api docs</html>")
        none = detect_observable_surface(root)
        assert none["observable"] is False, none
        assert "no HTML entrypoint" in none["detail"], none
        # ...now give it a real one.
        (root / "src" / "index.html").write_text("<html><body>app</body></html>")
        some = detect_observable_surface(root)
        assert some["observable"] is True, some
        assert any(m.startswith("html:") for m in some["markers"]), some
        # A DEPENDENCY on a web framework counts, and the marker says which.
        (root / "src" / "index.html").unlink()
        (root / "pyproject.toml").write_text('dependencies = ["streamlit>=1.0"]\n')
        dep = detect_observable_surface(root)
        assert dep["observable"] is True and any("streamlit" in m for m in dep["markers"]), dep

    # NO CHECKOUT is None, never False. "No checkout" and "no surface" are opposite findings, and
    # conflating them would re-create the defect in the other direction.
    absent = detect_observable_surface("/nonexistent/path/for/the/selftest")
    assert absent["observable"] is None, absent
    unev = evaluate_precondition(
        "frontend-verifier", repository="stranske/X", repo_path="/nonexistent/path/for/the/selftest"
    )
    assert unev["requirement_met"] is None, unev
    assert unev["precondition_met"] is True, (
        "a scope match with an UNEVALUATED repo fact must not become a failure",
        unev,
    )
    assert unev["unevaluated_because"], unev
    # And with no `repo_path` at all, the MISSING INPUT IS NAMED. A condition nothing can attempt to
    # check is the original defect; naming the input is the fix.
    silent = evaluate_precondition("frontend-verifier", repository="stranske/X")
    assert any("repo_path" in why for why in silent["unevaluated_because"]), silent

    # ---- PART 3: WHAT A CALLER RECEIVES. Synthetic ledger, so this asserts the ANSWER on any
    # machine rather than this instance's 43 rows -- asserting the table instead of the answer is how
    # a suppression bug shipped one function over.
    with tempfile.TemporaryDirectory(prefix="precond-advise-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        # NAMES CHOSEN SO A SINK IS OBSERVABLE. With no usefulness evidence every candidate carries
        # the same propensity, so the tie-break is the capability id -- and the FIRST attempt at this
        # fixture used `switch-review`, which is already LAST alphabetically. A break that sank a
        # failing entry therefore changed nothing and stayed green. The failing capability must sort
        # FIRST for the invariance assertion below to be able to fail.
        for cid in ("aaa-self-only", "mmm-both", "zzz-undeclared"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        real = SURFACE_BINDINGS.get("t-precond")
        real_pre = {k: dict(v) for k, v in CAPABILITY_PRECONDITIONS.items()}
        SURFACE_BINDINGS["t-precond"] = {
            "aaa-self-only": "bound, self-scoped",
            "mmm-both": "bound, both",
            "zzz-undeclared": "bound, undeclared",
        }
        CAPABILITY_PRECONDITIONS["aaa-self-only"] = {"applies_to": APPLIES_SELF}
        CAPABILITY_PRECONDITIONS["mmm-both"] = {"applies_to": APPLIES_BOTH}
        try:
            task = "add unit tests for the retry helper"
            got = advise(
                task,
                surface="t-precond",
                repository="stranske/Workflows",
                path=ledger,
                record=False,
            )
            ids = [m["capability_id"] for m in got["capabilities"]]
            assert set(ids) >= {"aaa-self-only", "mmm-both", "zzz-undeclared"}, ids

            # THE VERDICT REACHES THE CALLER, per entry.
            sr = next(m for m in got["capabilities"] if m["capability_id"] == "aaa-self-only")
            assert sr["precondition_met"] is False, sr
            assert sr["suggested_decline_kind"] == "precondition_unmet", sr
            assert "the concept may transfer" in sr["precondition_note"], sr
            pt = next(m for m in got["capabilities"] if m["capability_id"] == "zzz-undeclared")
            assert pt["precondition_met"] is None and pt["precondition_note"] is None, pt

            # NO RANK PENALTY, asserted directly and not only by comparison: the capability whose
            # precondition FAILED must still be FIRST, because that is where its propensity and its
            # id put it. Any sink moves it, and this fails.
            assert ids[0] == "aaa-self-only", (
                "a failed precondition changed the position a caller receives; the axis may only "
                f"annotate. order={ids}"
            )

            # AND THE SUMMARY BLOCK names all three populations, so "nothing failed" and "nothing
            # was checked" cannot read alike.
            block = got["precondition"]
            assert block["target"] == APPLIES_AUDITED_REPO, block
            assert block["unmet"] == ["aaa-self-only"], block
            assert {"aaa-self-only", "mmm-both"} <= set(block["declared"]), block
            assert "zzz-undeclared" not in block["declared"], block

            # ---- THE REMEDY, beside the gap. `unevaluated_because` already named the missing input
            # per capability and three audit rounds re-asked nothing, because a diagnosis is not an
            # instruction: `frontend-verifier` was declined four times for a precondition "never
            # evaluated" while the declaration, the probe and both parameters existed. So the answer
            # now carries the DRAINABLE quantity — which inputs would turn UNEVALUATED into a
            # verdict — and it must go EMPTY once they are supplied, or it is noise a reader learns
            # to skip. Break->revert: hard-coding `missing_inputs` to [] loses the first assertion;
            # hard-coding it non-empty loses the second.
            CAPABILITY_PRECONDITIONS["aaa-self-only"]["requires"] = "observable_surface"
            try:
                bare = advise(task, surface="t-precond", path=ledger, record=False)
                bare_block = bare["precondition"]
                assert bare_block["missing_inputs"] == ["repo_path", "repository"], bare_block
                assert bare_block["how_to_evaluate"], bare_block
                assert "`repository`" in bare_block["how_to_evaluate"], bare_block
                assert "`repo_path`" in bare_block["how_to_evaluate"], bare_block
                # It must be RENDERED too, not only carried: a remedy that appears under --json
                # alone is prose nothing reads, which is the defect one level up.
                assert "PRECONDITION(S) NOT EVALUATED" in format_advice(bare), format_advice(bare)
                # SUPPLY BOTH -> nothing left to ask for. `repo_path` points at a real directory so
                # the probe answers rather than reporting no checkout.
                full = advise(
                    task,
                    surface="t-precond",
                    repository="stranske/Workflows",
                    repo_path=td,
                    path=ledger,
                    record=False,
                )
                assert full["precondition"]["missing_inputs"] == [], full["precondition"]
                assert full["precondition"]["how_to_evaluate"] is None, full["precondition"]
                assert "PRECONDITION(S) NOT EVALUATED" not in format_advice(full), format_advice(
                    full
                )
            finally:
                CAPABILITY_PRECONDITIONS["aaa-self-only"].pop("requires", None)

            # ---- THE INVARIANCE. Neither the SET nor the ORDER may differ from what the same call
            # returns with the axis emptied. This is the assertion the third audit round demands:
            # explain the mismatch, never down-weight the binding.
            saved = {k: dict(v) for k, v in CAPABILITY_PRECONDITIONS.items()}
            CAPABILITY_PRECONDITIONS.clear()
            try:
                without = advise(
                    task,
                    surface="t-precond",
                    repository="stranske/Workflows",
                    path=ledger,
                    record=False,
                )
            finally:
                CAPABILITY_PRECONDITIONS.clear()
                CAPABILITY_PRECONDITIONS.update(saved)
            assert [m["capability_id"] for m in without["capabilities"]] == ids, (
                "the applies_to axis changed the ORDER or the SET a caller receives; it may only "
                f"annotate. with={ids} without={[m['capability_id'] for m in without['capabilities']]}"
            )
            assert without["bound_capabilities"] == got["bound_capabilities"], got
            assert without["dispatch_ready_count"] == got["dispatch_ready_count"], got
            # ...and the same for a classification MISS, which takes the other return branch.
            miss_with = advise(
                "xyzzy plugh frobnicate",
                surface="t-precond",
                repository="stranske/Workflows",
                path=ledger,
                record=False,
            )
            CAPABILITY_PRECONDITIONS.clear()
            try:
                miss_without = advise(
                    "xyzzy plugh frobnicate",
                    surface="t-precond",
                    repository="stranske/Workflows",
                    path=ledger,
                    record=False,
                )
            finally:
                CAPABILITY_PRECONDITIONS.clear()
                CAPABILITY_PRECONDITIONS.update(saved)
            assert [m["capability_id"] for m in miss_with["capabilities"]] == [
                m["capability_id"] for m in miss_without["capabilities"]
            ], "the axis reordered the binding-only branch"
            assert [m["capability_id"] for m in miss_with["capabilities"]][0] == "aaa-self-only", [
                m["capability_id"] for m in miss_with["capabilities"]
            ]
            assert miss_with["precondition"]["unmet"] == ["aaa-self-only"], miss_with[
                "precondition"
            ]

            # EVERY return branch carries the key, so a caller can rely on it.
            for probe in (
                advise("xyzzy plugh", path=ledger, record=False),
                advise(task, surface="repo-audit:phase-1", path=ledger, record=False),
            ):
                assert "precondition" in probe, sorted(probe)

            # ---- THE TWO MECHANISMS ARE ORTHOGONAL AND BOTH REACH THE CALLER. A recorded per-repo
            # CONTRAINDICATION ("broken against THIS app", ranks last) and a declared PRECONDITION
            # ("acts on another system", annotates only) answer different questions, and letting one
            # silently shadow the other is how two mechanisms become one broken one.
            import json as _json

            import repo_knowledge

            registry = Path(td) / "repo_knowledge.json"
            registry.write_text(
                _json.dumps(
                    {
                        "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
                        "repos": {
                            "stranske/Workflows": {
                                "summary": "s",
                                "contraindications": [
                                    {
                                        "capability": "aaa-self-only",
                                        "reason": "recorded as broken against this repo",
                                        "instead": "do it by hand",
                                    }
                                ],
                            }
                        },
                    },
                    indent=2,
                )
                + "\n"
            )
            real_reg = repo_knowledge.REG
            repo_knowledge.REG = registry
            try:
                both = advise(
                    task,
                    surface="t-precond",
                    repository="stranske/Workflows",
                    path=ledger,
                    record=False,
                )
                row = next(m for m in both["capabilities"] if m["capability_id"] == "aaa-self-only")
                # `.get`, not `[]`: a suppressed annotation must fail as a legible ASSERTION about
                # the answer, not as a KeyError that reads like a crash.
                assert row.get("contraindicated") is True, (
                    "the precondition pass suppressed the recorded contraindication",
                    row,
                )
                assert row.get("precondition_met") is False, row
                assert row.get("contraindication_reason") and row.get("precondition_note"), row
                # ONE DIRECTION IS ASSERTED AND THE OTHER IS STRUCTURAL. `_annotate_preconditions`
                # runs BEFORE `_annotate_contraindications` on both answer paths, so a precondition
                # pass cannot see (and therefore cannot suppress) a contraindication that has not
                # been stamped yet. Saying so beats asserting it: a break in that direction cannot
                # be written, and an assertion that cannot fail is decoration.
                assert both["contraindicated"] == ["aaa-self-only"], both["contraindicated"]
                assert both["precondition"]["unmet"] == ["aaa-self-only"], both["precondition"]
                # Still OFFERED: neither mechanism conceals.
                assert "aaa-self-only" in [m["capability_id"] for m in both["capabilities"]]
                # And BOTH reasons reach a human reader, not only the JSON.
                text = format_advice(both)
                assert "CONTRAINDICATED HERE" in text and "PRECONDITION NOT MET" in text, text
            finally:
                repo_knowledge.REG = real_reg
        finally:
            if real is None:
                SURFACE_BINDINGS.pop("t-precond", None)
            else:
                SURFACE_BINDINGS["t-precond"] = real
            CAPABILITY_PRECONDITIONS.clear()
            CAPABILITY_PRECONDITIONS.update(real_pre)
    print(
        "capability_advisor precondition selftest: OK (applies_to explains an offer and changes "
        "neither the set nor the order; undeclared and unevaluated are never failures)"
    )


def _selftest_findability() -> None:
    """The consult table and the inverse lookup — the two inputs the admission gate reads.

    SYNTHETIC SURFACES, not the live table, per the standing rule to test the mechanism rather than
    the bug: `ci` is stranded today and `repo-audit` is not, but asserting on those two would make
    this selftest fail the moment either is fixed, which is the opposite of a regression guard.
    Every assertion below was written by breaking it first — see the docstring of each block.
    """
    import tempfile
    from pathlib import Path

    # ---- 1. THE INVERSE LOOKUP RESOLVES THROUGH `binding_for`, INHERITANCE INCLUDED.
    # Broken first by having `surfaces_binding` read `SURFACE_BINDINGS[surface]` directly: the
    # parent-inherited capability then reported ZERO surfaces, which would have failed every
    # surface-wide binding as `bound_nowhere` — a second resolver disagreeing with the first.
    saved = {k: dict(v) for k, v in SURFACE_BINDINGS.items() if k.startswith("t-find")}
    saved_sites = {k: dict(v) for k, v in CONSULT_SITES.items() if k.startswith("t-find")}
    SURFACE_BINDINGS["t-find"] = {"wide-cap": "declared surface-wide"}
    SURFACE_BINDINGS["t-find:asked"] = {"asked-cap": "phase that a caller consults"}
    SURFACE_BINDINGS["t-find:silent"] = {"silent-cap": "phase nobody consults"}
    SURFACE_BINDINGS["t-find:empty"] = {NO_BINDING: "deliberately empty, with a reason"}
    try:
        with tempfile.TemporaryDirectory(prefix="adv-find-") as td:
            ledger = Path(td) / "capabilities.json"
            capabilities.save({}, ledger)
            site_path = Path(td) / "fake-skill.md"
            site_path.write_text('consult with surface: "t-find:asked"\n')
            CONSULT_SITES["t-find:asked"] = {"caller": str(site_path), "how": "synthetic"}

            inv = surfaces_binding(
                ["wide-cap", "asked-cap", "silent-cap", "absent-cap"], path=ledger
            )
            assert "t-find:asked" in inv["wide-cap"], inv["wide-cap"]
            assert "t-find:silent" in inv["wide-cap"], inv["wide-cap"]
            assert inv["asked-cap"] == ["t-find:asked"], inv["asked-cap"]
            assert inv["silent-cap"] == ["t-find:silent"], inv["silent-cap"]
            # A SUPPRESSED PHASE MUST NOT INHERIT. Broken first by dropping the NO_BINDING check
            # from `binding_for`, which made every deliberately-empty surface look bound.
            assert "t-find:empty" not in inv["wide-cap"], inv["wide-cap"]
            # A capability nothing declares gets an EMPTY LIST, not a KeyError — the caller reads
            # this as `bound_nowhere` and must not have to guard.
            assert inv["absent-cap"] == [], inv

            # ---- 2. A DECLARED CONSULT SITE IS A FALSIFIABLE CLAIM ABOUT A FILE.
            reach = consulting_surfaces()
            assert "t-find:asked" in reach["reached"], reach["reached"][:8]
            assert "t-find:asked" in reach["verified"], reach["verified"]
            # PRESENT BUT NO LONGER NAMING ITS SURFACE IS DRIFT, and drift must LEAVE `reached`.
            # Broken first by treating any readable file as verification, which let a renamed
            # surface keep counting as consulted forever.
            site_path.write_text("this file no longer mentions the surface at all\n")
            drifted = consulting_surfaces()
            assert "t-find:asked" not in drifted["reached"], drifted["reached"][:8]
            assert any(d["surface"] == "t-find:asked" for d in drifted["drifted"]), drifted
            # ABSENT IS NOT REFUTED. A caller this machine does not have stays reached and is
            # reported unverified — the same "no ledger, no verdict" rule the commitment check
            # uses. Broken first by treating absence as drift, which stranded every skill-bound
            # capability on a fresh clone: a gate red on arrival.
            CONSULT_SITES["t-find:asked"] = {
                "caller": str(Path(td) / "does-not-exist.md"),
                "how": "synthetic",
            }
            absent = consulting_surfaces()
            assert "t-find:asked" in absent["reached"], absent["reached"][:8]
            assert any(u["surface"] == "t-find:asked" for u in absent["unverified"]), absent

            # ---- 3. A PARENT WHOSE PHASES ARE CONSULTED IS NOT STRANDED.
            # This is the `repo-audit` case and the assertion that keeps the check from crying wolf
            # about every surface-wide declaration. Broken first by testing plain membership in
            # `reached`, which reported `t-find` stranded while its own phase was being consulted.
            stranded = set(absent["bound_unconsulted"])
            assert "t-find" not in stranded, sorted(stranded)
            assert "t-find:silent" in stranded, sorted(stranded)
            # ...and a deliberately-empty surface strands nothing, so it is not reported either.
            assert "t-find:empty" not in stranded, sorted(stranded)

            # ---- 4. FAMILIES EXPAND TO THE SURFACES ACTUALLY PASSED, not to a prefix rule.
            fam_site = Path(td) / "fam.md"
            fam_site.write_text("pass t-find:step-N as the surface\n")
            CONSULT_SITES["t-find:step-N"] = {
                "caller": str(fam_site),
                "instances": ["t-find:step-1", "t-find:step-2"],
                "how": "synthetic family",
            }
            fam = consulting_surfaces()
            assert {"t-find:step-1", "t-find:step-2"} <= set(fam["reached"]), fam["reached"][:12]
            assert (
                "t-find:step-3" not in fam["reached"]
            ), "a family must not admit an un-listed step"
            # The family KEY is a label for the declaration, never a surface anyone passes — so it
            # must not leak into the reachable set and quietly satisfy a binding to it.
            assert "t-find:step-N" not in consult_keys(), sorted(consult_keys())[:8]
            assert "t-find:step-1" in consult_keys(), sorted(consult_keys())[:8]
    finally:
        for key in [k for k in SURFACE_BINDINGS if k.startswith("t-find")]:
            SURFACE_BINDINGS.pop(key)
        SURFACE_BINDINGS.update(saved)
        for key in [k for k in CONSULT_SITES if k.startswith("t-find")]:
            CONSULT_SITES.pop(key)
        CONSULT_SITES.update(saved_sites)

    # ---- 5. THE REAL TABLE MUST NOT HAVE DRIFTED, and the in-tree site must verify EVERYWHERE.
    live = consulting_surfaces()
    assert not live[
        "drifted"
    ], f"a consult site's caller no longer names its surface: {live['drifted']}"
    # EVERY BOUND SURFACE IS IN ONE OF THREE DECLARED STATES. A fourth — bindings that nothing can
    # reach and nobody wrote down — is precisely what `ci` was, and it is invisible until a
    # capability is stranded on it. Broken first by adding a binding to a surface with no consult
    # site and no entry below: without this the gate reports `bound_to_unconsulted_surface` on the
    # capability and never on the SURFACE that caused it.
    unaccounted = set(live["bound_unconsulted"]) - set(KNOWN_UNCONSULTED)
    assert not unaccounted, (
        f"surface(s) {sorted(unaccounted)} hold bindings that no caller can reach. Either declare "
        "the consult in CONSULT_SITES, make the binding deliberately empty with NO_BINDING, or "
        "record the defect in KNOWN_UNCONSULTED with its reason and fix"
    )
    # ...and the reverse: an acknowledged defect that has been FIXED must not linger, or the record
    # outlives the evidence — the prose-cache failure this workspace names explicitly.
    stale = set(KNOWN_UNCONSULTED) - set(live["bound_unconsulted"])
    assert not stale, (
        f"KNOWN_UNCONSULTED still lists {sorted(stale)}, which is no longer stranded — move the "
        "entry to CONSULT_SITES (or drop it) rather than leaving a cached reason behind"
    )
    for surface, why in KNOWN_UNCONSULTED.items():
        assert "FIX:" in why, f"{surface} records the defect without naming the fix"
    assert "tick" in live["verified"], (
        "the in-tree consult site must verify on every machine, CI included; "
        f"verified={live['verified']}"
    )
    for key, site in CONSULT_SITES.items():
        assert str(site.get("caller") or "").strip(), key
        assert str(site.get("how") or "").strip(), f"{key} declares no reason"
        assert set(site.get("instances") or [key]) <= set(live["reached"]) | {
            u["surface"] for u in live["unverified"]
        } | {d["surface"] for d in live["drifted"]}, key
    print(
        "capability_advisor findability selftest: OK (inverse lookup reuses binding_for, drift "
        "leaves reach, absence is not refutation, consulted parents are not stranded)"
    )


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    # Classification is deterministic and evidence-bearing.
    c = classify_task("please add unit tests for the retry helper")
    assert c and c[0]["task_type"] == "testgen", c
    assert c[0]["hits"], "classification must report WHY it classified"
    assert classify_task("please add unit tests for the retry helper") == c, "must be deterministic"

    # WHOLE-WORD WITH INTENT, not substring. Observed in a real run (experiment
    # advice:a6cc531b8010): a READ-ONLY audit classified as `implement` because the noun
    # "implementation" contains the verb. With MIN_SIGNAL_HITS = 1 that one substring was enough to
    # offer a code-mutating lane to work that must not touch code.
    audit_text = "a read-only audit of the implementation of the config loader; do not change code"
    types = [d["task_type"] for d in classify_task(audit_text)]
    assert "implement" not in types, types
    assert "review" in types, types  # ...and the RIGHT one still fires
    # Inflections preserve intent, so they must still hit.
    for verb_form in (
        "implement the exporter",
        "implementing the exporter",
        "this implements the spec",
        "implemented the exporter",
    ):
        assert "implement" in [d["task_type"] for d in classify_task(verb_form)], verb_form
    # Forms the boundary would otherwise drop are spelled out in TASK_SIGNALS, so they still hit.
    assert "testgen" in [d["task_type"] for d in classify_task("run the testgen lane")]
    assert "ux_review" in [d["task_type"] for d in classify_task("screenshot the output")]
    assert "mechanical" in [d["task_type"] for d in classify_task("formatting only")]
    # Initialisms do not inflect, so `ui` must not reach "uid" via the bare -d ending.
    assert classify_task("check the uid field") == [], classify_task("check the uid field")
    assert "ux_review" in [d["task_type"] for d in classify_task("the ui is broken")]
    assert "codemod" in [
        d["task_type"] for d in classify_task("deduped the rows")
    ]  # -e verb keeps -d

    # DELIBERATE BREAK -> REVERT on the trailing boundary — the half that was missing.
    _saved_pattern = _signal_pattern
    try:
        globals()["_signal_pattern"] = lambda s: rf"(?<![a-z]){re.escape(s)}"  # the old prefix rule
        broken = [d["task_type"] for d in classify_task(audit_text)]
        assert "implement" in broken, "break did not change behaviour — test is vacuous"
    finally:
        globals()["_signal_pattern"] = _saved_pattern
    assert "implement" not in [
        d["task_type"] for d in classify_task(audit_text)
    ], "revert did not restore the whole-word boundary"

    # IT MUST BE ABLE TO SAY NO — both when unclassifiable and when nothing declares a trigger.
    with tempfile.TemporaryDirectory(prefix="advisor-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        lane = capabilities._blank_capability("testgen-lane")
        lane["status"] = "generated"
        lane["entrypoint"] = "testgen_lane.py"
        lane["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
        unrelated = capabilities._blank_capability("some-other")
        unrelated["status"] = "generated"
        unrelated["matcher"] = {"field": "task_type", "operator": "in", "value": ["codemod"]}
        retired = capabilities._blank_capability("gone")
        retired["status"] = "retired"
        retired["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
        capabilities.save({"testgen-lane": lane, "some-other": unrelated, "gone": retired}, ledger)

        hit = advise("add unit tests for the retry helper", path=ledger)
        assert hit["useful"] is True, hit
        ids = [m["capability_id"] for m in hit["capabilities"]]
        assert ids == ["testgen-lane"], ids  # only the matching, non-retired one
        assert hit["capabilities"][0]["dispatch_ready"] is False, hit
        assert "advisory" in hit["reason"], hit  # never implies it will actually run

        # Unclassifiable free text => a clean NO, with the reason.
        none = advise("xyzzy plugh frobnicate", path=ledger)
        assert none["useful"] is False and none["confidence"] == "none", none
        assert "could not classify" in none["reason"], none

        # Classified but nothing declares that trigger => also a clean NO.
        gap = advise("rewrite the documentation and the readme", path=ledger)
        assert gap["useful"] is False and "docs" in gap["task_types"], gap
        assert "no capability declares a trigger" in gap["reason"], gap

        # A capability with NO matcher must never be advised (fail-closed matching). Kept in the
        # SAME ledger — saving a single-capability dict would replace the whole file.
        bare = capabilities._blank_capability("bare")
        bare["status"] = "generated"
        capabilities.save(
            {"testgen-lane": lane, "some-other": unrelated, "gone": retired, "bare": bare}, ledger
        )
        advised = [
            m["capability_id"]
            for m in advise("add unit tests", path=ledger, record=False)["capabilities"]
        ]
        assert "bare" not in advised, advised

        text = format_advice(hit)
        assert "USE THE ORCHESTRATOR" in text and "testgen-lane" in text
        assert "NO ORCHESTRATOR CAPABILITY APPLIES" in format_advice(none)

        # --- RE-ASK TRIGGERS: fire on CHANGE, never on a clock -----------------------------
        first = advise("add unit tests", path=ledger, record=False)
        # Same work, same scope => silent. This is the case a timer would get wrong.
        quiet = should_reask(
            first, {"task": "add unit tests", "repository": "", "capabilities_ready": 0}
        )
        assert quiet["reask"] is False and quiet["reasons"] == [], quiet
        # The work reclassified — the highest-value trigger.
        moved = should_reask(
            first,
            {"task": "now refactor every call site", "repository": "", "capabilities_ready": 0},
        )
        assert moved["reask"] and any(
            r.startswith("task_reclassified") for r in moved["reasons"]
        ), moved
        # A skill starting is an explicit statement about the kind of work now underway.
        sk = should_reask(
            first,
            {
                "task": "add unit tests",
                "skill": "repo-audit",
                "repository": "",
                "capabilities_ready": 0,
            },
        )
        assert "skill_invoked:repo-audit" in sk["reasons"], sk
        # Scope moved, and something became runnable.
        assert (
            "scope_changed"
            in should_reask(
                first, {"task": "add unit tests", "repository": "o/other", "capabilities_ready": 0}
            )["reasons"]
        )
        assert (
            "capability_became_dispatch_ready"
            in should_reask(
                first, {"task": "add unit tests", "repository": "", "capabilities_ready": 1}
            )["reasons"]
        )
        # No prior advice always asks.
        assert should_reask(None, {"task": "anything"})["reasons"] == ["no_advice_yet"]

        # --- LEARNED skill -> capability association ---------------------------------------
        assert learned_associations(path=ledger)["observations"] == 0, "nothing learned yet"
        advise("add unit tests for the parser", skill="repo-audit", path=ledger)
        advise("write tests for the loader", skill="repo-audit", path=ledger)
        assoc = learned_associations(path=ledger)
        assert assoc["by_skill"]["repo-audit"]["testgen-lane"] == 2, assoc
        assert assoc["by_task_type"]["testgen"]["testgen-lane"] == 2, assoc
        # Repeating the SAME task must not inflate frequency; a distinct task must count.
        advise("write tests for the loader", skill="repo-audit", path=ledger)
        assert learned_associations(path=ledger)["by_skill"]["repo-audit"]["testgen-lane"] == 2
        # record=False stays a pure query.
        before = learned_associations(path=ledger)["observations"]
        advise("add tests for the writer", skill="repo-audit", path=ledger, record=False)
        assert learned_associations(path=ledger)["observations"] == before, "record=False wrote"
        # Recording a match moves the capability out of no_matching_work — the honest reading of
        # "work of your kind occurred and you still did not run".
        after = capabilities.load(ledger)["testgen-lane"]
        assert after["last_match"], after
        assert capabilities.classify_liveness(after) == "matched_not_invoked", after

        # --- DENOMINATORS: a skill-less match must not be invisible (FM5) -------------------
        # THE BUG THIS CATCHES: `observations` was the sum over by_skill alone, so an advisory
        # match with no skill attributed vanished from the count while still being counted in
        # by_task_type. Every skill-wiring claim measured with that number was unfalsifiable.
        # An advisory call with NO skill — exactly what a session that forgets `skill=` produces.
        advise("add unit tests for the anonymous caller", path=ledger)
        d = learned_associations(path=ledger)
        # by_skill is blind to it, by design; the totals are not, and they reconcile.
        assert "" not in d["by_skill"] and None not in d["by_skill"], d["by_skill"]
        assert d["observations_with_skill"] == sum(
            sum(v.values()) for v in d["by_skill"].values()
        ), d
        assert d["observations_without_skill"] >= 1, d  # the skill-less match IS visible
        assert d["observations"] == (
            d["observations_with_skill"] + d["observations_without_skill"]
        ), d
        # The old definition would have made these two equal; they must differ here, or the
        # convenient denominator is back.
        assert d["observations"] > d["observations_with_skill"], d
        # Every count states which population it covers, so a subset cannot pass as the set.
        assert set(d["populations"]) >= {
            "observations",
            "observations_with_skill",
            "observations_without_skill",
            "by_task_type",
        }, d

    print(
        "capability_advisor.py selftest: OK (deterministic classification, says NO, "
        "advice never implies dispatch, retired/unmatched excluded, denominators named)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", nargs="*", help="the task, in plain words")
    ap.add_argument("--repository", default="")
    ap.add_argument("--lane", default="opener")
    ap.add_argument(
        "--surface",
        default="",
        help="the skill or automation asking (e.g. closer-lane); selects its declared binding",
    )
    ap.add_argument(
        "--repo-path",
        default="",
        help="a checkout of --repository, if you have one; lets a declared repo-fact "
        "precondition (e.g. frontend-verifier's observable surface) actually be "
        "EVALUATED instead of reported as unevaluated",
    )
    ap.add_argument(
        "--context",
        default="",
        help="JSON of trigger context you actually know, e.g. "
        '\'{"closer_gate":"high_stakes_review"}\'',
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument(
        "--consult-tick-phases",
        action="store_true",
        help="consult every declared `tick:<phase>` surface once for today (the tick's caller for "
        "its own sub-surfaces). Advisory and read-only apart from the match heartbeat; always "
        "exits 0 so it cannot stall the tick",
    )
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        _selftest_front_door()
        _selftest_how_to_use()
        _selftest_bindings()
        _selftest_phase_consult()
        _selftest_contraindications()
        _selftest_preconditions()
        _selftest_findability()
        _selftest_reach()
        return 0
    if args.consult_tick_phases:
        # ALWAYS 0. This is called from the hourly tick, which drives real dispatch; a capability
        # consult must never be able to fail it. `consult_phases_guarded` turns every error into a
        # reported field, and the line printed below always states what happened.
        report = consult_phases_guarded()
        print(json.dumps(report, indent=2) if args.json else format_phase_consult(report))
        return 0
    if not args.task:
        ap.error("give the task in plain words, use --consult-tick-phases, or use --selftest")
    result = advise(
        " ".join(args.task),
        repository=args.repository,
        lane=args.lane,
        context=json.loads(args.context) if args.context else None,
        surface=args.surface,
        repo_path=args.repo_path,
    )
    print(json.dumps(result, indent=2) if args.json else format_advice(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
