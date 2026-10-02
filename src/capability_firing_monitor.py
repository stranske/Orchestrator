#!/usr/bin/env python3
"""capability_firing_monitor.py — DOES each capability fire, and did one stop?

The does-fire counterpart to `capability_activation_audit`'s can-fire. Both questions were needed
and only one was instrumented:

  * `capability_activation_audit` answers "CAN it fire" and persists snapshots, so a reachability
    regression is visible.
  * `capabilities usage` answers "did it fire in the last 28 days" and gives a next action — but
    only as a SNAPSHOT. Nothing was stored, so a capability that fired last week and went silent
    this week looked identical to one that has been healthy all along.
  * `switch_review` detects exactly that silence, but only for the gated switches in
    `SWITCH_CAPABILITY`. The other thirty-odd capabilities had no such watch.

The gap this closes is therefore narrow and specific: **persisted per-capability firing history, and
a regression alarm when a capability that used to fire stops.** That is the failure this project
keeps paying for — `range-lane-rollout` went quiet for 36 days, `consumer_sync_artifact_ingest`
failed every run for four weeks, and the role-lineage stamp was never emitted at all. None of those
announced itself; each was found by someone going to look.

Two design choices worth stating, both learned the hard way here:

**Observers are held to a different standard.** A cadence step that emits a report can never produce
a merged PR, so `capabilities.is_observer` capabilities are judged on whether they RAN, never on
outcomes. Judging them on delivery is the category error that had 8 capabilities sitting in a
"measurement gap" they could never leave.

**Silence is only meaningful against a promise.** A capability with no `trigger_cadence` has
promised nothing, so calling it "overdue" would be noise. Those are reported as `no_cadence_declared`
— a documentation gap, not an alarm. This is the same reason `switch_review` refuses to nag about a
switch with no recorded switch-on criterion.

**Only a live row is judged.** A retired or superseded row (`capabilities.NOT_LIVE_STATES`) is not
expected to fire, so "does it fire?" does not apply to it. A row retired on 2026-09-03, the day it was
registered, was listed `never_fired` on every run from then on, and each of those listings was a
false positive. Such a row is still NAMED, under `not_monitored` beside `ledger_total`, so the report
accounts for every ledger row; it is kept out of every finding list and every count. The report also
declares that rule as `finding_population`, because a finding that disappears when the rule changes
was not resolved by anything: the tick's output-change grader re-baselines on a changed population
instead of crediting the monitor with the edit.

**A deliberately-off capability is HELD OFF, not overdue.** Silence against a promise the system
itself withdrew is not a finding. Until 2026-10-02 every run listed `issue-readiness` (its cadence
step retired by default on 2026-09-15) and `range-lane-rollout` (its declared gate blocks the
delivering path, and the tick has held its flag at 0 since the trial window closed on 2026-07-22)
under both `overdue` and `regressed`: two permanent alarms nobody could act on, beside the real
ones. A live row whose silence would be a finding (`SILENCE_FINDINGS`) and which a DECLARED hold
keeps off is named under `held_off` instead, with the hold and what lifts it. Two holds count:
the row's `gate_blocks_execution` gate, which is the declared branch of
`capabilities.classify_liveness` and never the weaker `gate_reason`-only branch (that one does not
say the delivering path is blocked); and the retirement of every cadence step that declares the
row under `capabilities` (`cadence_registry.retirement_holds`), never a match on names. Nothing is
hidden: each entry names the findings it would otherwise be, and the report states both counts.
A declared-off row that is evidently firing has no silence to explain, so it is not listed:
`thompson-hybrid-routing` declares its gate and fired the day this was written. And
`no_cadence_declared` is untouched, because a hold explains a silence, not a missing promise. The
report declares this rule as its population (`finding_population`), so the grader re-baselines
instead of crediting the edit, and a hold added or lifted LATER is graded like a retirement.

Dedup for that change (2026-10-02): read this module, `switch_review` (whose `held_off` covers the
five env switches in `SWITCH_CAPABILITY` and raises owner questions, a different concept),
`cadence_registry.inspect_cadence` (the step-level `retired` verdict this follows),
`capabilities.classify_liveness`, `capability_activation_audit` (not-live rows only), the
improvement log (PR #372's note names this gap as left open), open PRs and recent branches. No
capability-level hold existed and nothing declared a capability-to-step link, so this module was
extended and the link declared on the cadence row; nothing new was registered.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import time
from collections.abc import Mapping
from typing import Any

import cadence_registry
import capabilities

STATE_DIR = pathlib.Path(
    os.environ.get("ORCH_STATE_DIR", str(pathlib.Path.home() / ".codex/orchestrator"))
)
HISTORY = STATE_DIR / "capability-firing-history.json"
DISABLED = os.environ.get("ORCH_FIRING_MONITOR_DISABLED", "").strip() == "1"
MAX_SNAPSHOTS = 52  # a year of weekly runs; enough to see a slow decay

# `trigger_cadence` is free prose across the ledger ("daily", "weekly (cron '0 8 * * 1')",
# "every suite run", "supervised CLI and focused activation probe"). Parse it into a tolerance
# rather than demanding a schema migration for 20 existing records.
CADENCE_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"every tick|hourly|per tick", 0.25),
    (r"every suite run|every run", 7.0),  # bounded by how often anyone runs the suite
    (r"\bdaily\b|every day|1 ?/ ?day", 2.0),
    (r"\bweekly\b|per week|mondays?", 10.0),
    (r"\bmonthly\b|per month", 40.0),
    (r"quarterly", 120.0),
)
# Cadences that describe a HUMAN or ad-hoc trigger promise nothing about elapsed time. Treating
# these as overdue would manufacture an alarm nobody can act on — and per the owner's attention
# budget, a "supervised CLI" step is precisely the thing that will not happen on a schedule.
ONDEMAND_RE = re.compile(
    r"supervised|on demand|ad hoc|manual|dispatch|when |focused activation", re.IGNORECASE
)


# `capabilities.production_heartbeat` is a NO-OP unless ORCH_CAPABILITY_HEARTBEATS=1, which only
# orchestrate.sh sets, at `ORCH-ANCHOR: heartbeat-export`, inside an active tick. (That said "line
# ~152" until 2026-08-22, by which point the export was at 190 — hence the anchor. Everything
# invoked ABOVE that anchor records nothing at all; `capability_activation_audit.heartbeat_env_gate`
# is what watches for that, and it caught two live cases.) So the firing record measures TICK activity
# and nothing else. A capability whose caller is the test suite or a hand-run CLI will therefore read
# "never fired" forever while working perfectly — an artifact of where heartbeats are enabled, not a
# fact about the capability. Reporting those in the same column as a genuinely dormant lane would be
# the same category error as judging an observer on deliveries.
SUITE_OR_CLI_MATCHERS = frozenset({"test_gate"})
SUITE_CADENCE_RE = re.compile(r"every suite run|every run|supervised CLI|CLI", re.IGNORECASE)

NOT_MONITORED_REASON = (
    "a retired or superseded row is not a live capability: nothing is expected to fire, so "
    "'does it fire?' does not apply and it is not monitored"
)

# THE DECLARED HOLDS, and the findings a hold keeps a row out of. Both tuples are consumed by
# `review()`, which routes a held row away from exactly these lists, AND by `finding_population()`,
# which tells the tick's grader the rule; one constant each, so the rule the grader is told cannot
# drift from the rule the report applied. `no_cadence_declared` is deliberately not a silence
# finding: it reports a missing promise, and a hold does not explain that.
DECLARED_GATE = "declared_gate"
RETIRED_STEP = "retired_cadence_step"
HOLD_KINDS = (DECLARED_GATE, RETIRED_STEP)
SILENCE_FINDINGS = ("never_fired", "overdue", "regressed")
# What ends a declared-gate hold, read off the branch of `classify_liveness` that declares one.
DECLARED_GATE_LIFTS = (
    "the row stops declaring gate_blocks_execution, or leaves the gated statuses "
    "(promoted to active, or retired)"
)


def finding_population() -> dict[str, Any]:
    """The rule deciding which ledger rows this report's findings may name.

    Live rows only (`capabilities.live_finding_population`, the rule the activation audit declares
    too), and no row in a SILENCE finding while a declared hold keeps it off. The rule, never the
    rows: a hold declared or lifted later moves a finding exactly as a retirement does, and is graded.
    """
    return {
        **capabilities.live_finding_population(),
        "held_off_kinds": list(HOLD_KINDS),
        "held_off_excluded_from": list(SILENCE_FINDINGS),
    }


def holds_for(
    cap_id: str,
    cap: dict[str, Any],
    *,
    liveness: str,
    step_holds: Mapping[str, list[dict[str, Any]]],
    now: int,
) -> list[dict[str, Any]]:
    """Every DECLARED reason this live row is deliberately off; [] when nothing holds it.

    Two declarations count, and nothing is inferred. The gate is `classify_liveness`'s DECLARED
    branch: `deliberately_gated` together with `gate_blocks_execution`, because the same label is
    also reached by a `gate_reason` alone, which says a gate exists and not that it blocks the path
    that fires (on 2026-10-02 the five `role-*` rows read `deliberately_gated` that way, and every
    tick-observable one had fired). The retirement is `step_holds`, the run's
    `cadence_registry.retirement_holds`.
    """
    holds: list[dict[str, Any]] = []
    if liveness == "deliberately_gated" and cap.get("gate_blocks_execution"):
        expiry = int(cap.get("expiry") or 0)
        expires_on = capabilities.utc_date(expiry) if expiry else None
        lifts = DECLARED_GATE_LIFTS
        if expires_on:
            # The expiry is the hold's mechanical drain: a writing load retires a live row at its
            # expiry unless it is renewed, and a retired row leaves the monitor as not live.
            lifts += (
                f"; its expiry passed on {expires_on}, so the next writing load retires it "
                "unless it is renewed"
                if expiry <= now
                else f"; its expiry retires it on {expires_on} unless it is renewed"
            )
        holds.append(
            {
                "kind": DECLARED_GATE,
                "reason": str(cap.get("gate_reason")),
                "lifts_when": lifts,
                "expires_on": expires_on,
            }
        )
    for step in step_holds.get(cap_id) or []:
        holds.append(
            {
                "kind": RETIRED_STEP,
                "step": step["step"],
                "reason": step["line"],
                "lifts_when": f"{step['re_enable_when']} in the tick's environment",
            }
        )
    return holds


def heartbeat_observable(cap: dict[str, Any]) -> bool:
    """Would a TICK ever credit this capability? If not, its firing record is uninformative."""
    if str((cap.get("matcher") or {}).get("kind") or "") in SUITE_OR_CLI_MATCHERS:
        return False
    return not SUITE_CADENCE_RE.search(str(cap.get("trigger_cadence") or ""))


def expected_interval_days(cap: dict[str, Any]) -> float | None:
    """How long may this capability stay silent before that means something? None = no promise."""
    cadence = str(cap.get("trigger_cadence") or "").strip()
    if not cadence:
        return None
    if ONDEMAND_RE.search(cadence):
        return None
    for pattern, tolerance in CADENCE_PATTERNS:
        if re.search(pattern, cadence, re.IGNORECASE):
            return tolerance
    return None


def _load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    try:
        data = json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else list(data.get("snapshots") or [])


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Credit this capability when it runs.

    Non-negotiable here: this module's whole subject is capabilities that fire without being
    recorded. `issue-readiness` and `switch-review` both shipped without a heartbeat and read as
    dormant for it; a firing monitor with no firing record of its own would be self-refuting.
    """
    try:
        capabilities.production_heartbeat(
            "capability-firing-monitor", event_type, ref="capability_firing_monitor.review"
        )
    except Exception:  # noqa: BLE001
        pass


def review(
    *,
    now: int | None = None,
    path: pathlib.Path | None = None,
    environ: Mapping[str, str] | None = None,
    registry: tuple[dict[str, Any], ...] | None = None,
) -> dict:
    """Current firing state for EVERY capability, plus regressions against stored history.

    `environ` decides whether a retired cadence step's re-enable flag holds; it defaults to this
    process's environment, which inside the tick is the tick's own. `registry` defaults to
    `cadence_registry.CADENCE_STEPS`.
    """
    _capability_heartbeat()
    now = int(now if now is not None else time.time())
    # `load_declared`, not `load`: this is a REPORT. The writing loader creates a missing ledger,
    # seeds declared gate rows, reconciles declarations and expires rows, and writes the result into
    # the shared ledger, while this step promises to write only its own history and, in a live
    # tick, the invocation heartbeat above (the kill switch stops the history write). `load_declared`
    # reconciles an in-memory copy and writes nothing. It seeds and expires nothing, even in memory:
    # until a writer does, the ledger is judged as it is.
    ledger = capabilities.load_declared(path or capabilities.REG)
    history = _load_history()
    previous = {row["capability_id"]: row for row in (history[-1]["rows"] if history else [])}
    # ONE READ of the registry's retirements for the whole run. A malformed declaration holds
    # NOTHING and is named: every row it would have held stays judged, so the failure is loud in
    # the findings and in `hold_unevaluated` rather than a silence. "Evaluated, nothing held" and
    # "could not evaluate" must not share the empty map.
    try:
        step_holds = cadence_registry.retirement_holds(environ, registry)
        hold_unevaluated = None
    except ValueError as exc:
        step_holds, hold_unevaluated = {}, f"cadence registry: {exc}"

    rows: list[dict[str, Any]] = []
    no_cadence: list[str] = []
    held_off: list[dict[str, Any]] = []
    found: dict[str, list[Any]] = {key: [] for key in SILENCE_FINDINGS}
    not_monitored: dict[str, list[str]] = {}
    for cap_id in sorted(ledger):
        cap = ledger[cap_id]
        last = int(cap.get("last_invocation") or 0)
        silent_days = None if not last else round((now - last) / 86400, 1)
        observer = capabilities.is_observer(cap)
        tolerance = expected_interval_days(cap)
        liveness = capabilities.classify_liveness(cap, now=now)
        observable = heartbeat_observable(cap)
        monitored = cap.get("status") not in capabilities.NOT_LIVE_STATES
        row = {
            "capability_id": cap_id,
            "status": cap.get("status"),
            "observer": observer,
            "last_invocation": last,
            "silent_days": silent_days,
            "tolerance_days": tolerance,
            "liveness": liveness,
            "ever_fired": bool(last),
            "tick_observable": observable,
            "monitored": monitored,
        }
        rows.append(row)
        if not monitored:
            # LIVE ROWS ONLY. The row stays in `rows`, and so in the history snapshot, which keeps
            # the ledger accounted for and lets a row revived out of `retired` be compared against
            # its past; it reaches no finding list and no count.
            row["not_monitored_because"] = f"status {cap.get('status')!r}: {NOT_MONITORED_REASON}"
            not_monitored.setdefault(str(cap.get("status")), []).append(cap_id)
            continue
        # Every live row carries its declared holds, whether or not it is silent, so a reader can
        # see a hold on a row that is evidently firing; only a SILENT held row leaves the findings.
        row["holds"] = holds_for(cap_id, cap, liveness=liveness, step_holds=step_holds, now=now)
        if not observable:
            # Its caller is the suite or a CLI; tick heartbeats cannot see it either way.
            continue

        if tolerance is None and not cap.get("trigger_cadence"):
            # An on-demand cadence promises nothing and is not a gap; NO cadence is a gap.
            no_cadence.append(cap_id)

        # What this row's silence would be reported as, keyed by finding list.
        silence: dict[str, Any] = {}
        if not last:
            silence["never_fired"] = cap_id
        elif tolerance is not None and silent_days is not None and silent_days > tolerance:
            silence["overdue"] = {
                "capability_id": cap_id,
                "silent_days": silent_days,
                "tolerance_days": tolerance,
                "cadence": cap.get("trigger_cadence"),
                "observer": observer,
            }

        # REGRESSION: it fired by the previous snapshot and has not fired since. This is the case no
        # existing instrument covered, and the reason range-lane's 36-day silence went unremarked.
        prior = previous.get(cap_id)
        if (
            prior
            and prior.get("ever_fired")
            and last
            and last == int(prior.get("last_invocation") or 0)
        ):
            elapsed = (now - int(history[-1]["generated_at"])) / 86400
            if tolerance is not None and elapsed > tolerance:
                silence["regressed"] = {
                    "capability_id": cap_id,
                    "unchanged_for_days": round(elapsed, 1),
                    "tolerance_days": tolerance,
                    "last_invocation": last,
                }

        if silence and row["holds"]:
            # DELIBERATELY OFF: named with what holds it and which findings it would otherwise be,
            # and counted beside `overdue`. Moved, never dropped.
            held_off.append(
                {
                    "capability_id": cap_id,
                    "held_from": [key for key in SILENCE_FINDINGS if key in silence],
                    "holds": row["holds"],
                    "silent_days": silent_days,
                    "tolerance_days": tolerance,
                    "cadence": cap.get("trigger_cadence"),
                    "last_invocation": last,
                }
            )
            continue
        for key in SILENCE_FINDINGS:
            if key in silence:
                found[key].append(silence[key])

    live = [r for r in rows if r["monitored"]]
    return {
        "generated_at": now,
        # `total` is the LIVE denominator every count below shares; `ledger_total` is every row, so
        # `ledger_total == total + not_monitored_count` and nothing leaves the report unnamed.
        "total": len(live),
        "ledger_total": len(rows),
        "not_monitored": {status: sorted(ids) for status, ids in sorted(not_monitored.items())},
        "not_monitored_count": sum(len(ids) for ids in not_monitored.values()),
        capabilities.FINDING_POPULATION_KEY: finding_population(),
        "fired_ever": sum(1 for r in live if r["ever_fired"]),
        "never_fired": found["never_fired"],
        "not_tick_observable": [r["capability_id"] for r in live if not r["tick_observable"]],
        "observers": sum(1 for r in live if r["observer"]),
        "overdue": found["overdue"],
        "regressed": found["regressed"],
        "no_cadence_declared": no_cadence,
        # BOTH QUANTITIES, always and side by side: the silences that are findings, and the ones a
        # declared hold explains. `overdue_count: 0` beside `held_off_count: 2` is a drained alarm;
        # either number alone could be read as the whole story.
        "overdue_count": len(found["overdue"]),
        "held_off": held_off,
        "held_off_count": len(held_off),
        "hold_unevaluated": hold_unevaluated,
        "snapshots_stored": len(history),
        "rows": rows,
    }


def record(rep: dict) -> dict:
    """Append this review to history so the NEXT run can see a regression.

    Without this the monitor would be another snapshot, which is what already existed.
    """
    if DISABLED:
        return {"recorded": False, "reason": "ORCH_FIRING_MONITOR_DISABLED=1"}
    history = _load_history()
    history.append(
        {
            "generated_at": rep["generated_at"],
            "rows": [
                {k: r[k] for k in ("capability_id", "last_invocation", "ever_fired", "liveness")}
                for r in rep["rows"]
            ],
        }
    )
    history = history[-MAX_SNAPSHOTS:]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, indent=1) + "\n", encoding="utf-8")
    return {"recorded": True, "snapshots": len(history), "path": str(HISTORY)}


def routing_hint(cap: dict[str, Any]) -> str | None:
    """For a capability starved of work, which label would route some to it?

    Deliberately a HINT, not a dispatch. The fleet's true backlog is 0 and the repo-review queue
    holds specified candidates behind a gate; naming the label is useful, silently filing issues
    from here would be a second work queue nobody asked for.
    """
    matcher = cap.get("matcher") or {}
    task = str(matcher.get("task_type") or matcher.get("name") or "")
    known = {
        "testgen": "testgen",
        "epic": "epic",
        "codemod": "refactor",
        "cross_repo": "cross-repo",
        "runtime_ac": "runtime-ac",
    }
    for key, label in known.items():
        if key in task or key in str(cap.get("capability_id") or ""):
            return label
    return None


def _format_held_off(held: list[dict], unevaluated: str | None) -> list[str]:
    """The held-off section. It always prints, so a drained list says so in words."""
    out = [""]
    if unevaluated:
        out.append(
            f"  RETIREMENT HOLDS NOT EVALUATED ({unevaluated}): no retired step holds anything "
            "off this run, so every capability one would hold is judged above as live"
        )
    if not held:
        out.append(
            "  held off: none — no live capability's silence is explained by a declared gate or a "
            "retired cadence step"
        )
        return out
    out.append(
        f"  HELD OFF ({len(held)}) — deliberately off, so the silence is named here and is not a "
        "finding:"
    )
    for r in held:
        silent = "never fired" if r["silent_days"] is None else f"silent {r['silent_days']}d"
        tolerance = "" if r["tolerance_days"] is None else f" (tolerance {r['tolerance_days']}d)"
        out.append(
            f"    {r['capability_id']:<38} {silent}{tolerance}; would be "
            f"{' and '.join(r['held_from'])}"
        )
        for hold in r["holds"]:
            if hold["kind"] == RETIRED_STEP:
                out.append(f"      held by cadence step {hold['step']}: {hold['reason']}")
            else:
                out.append(f"      held by its declared gate: {hold['reason']}")
            out.append(f"      lifts when: {hold['lifts_when']}")
    return out


def format_report(rep: dict) -> str:
    out = [
        "# Capability firing monitor",
        "",
        f"  {rep['fired_ever']} of {rep['total']} live capabilities have ever fired "
        f"({rep['observers']} are observers, judged on running rather than delivering)",
    ]
    not_monitored_count = rep.get("not_monitored_count", 0)
    if not_monitored_count:
        statuses = "; ".join(
            f"{status}: {', '.join(ids)}" for status, ids in rep.get("not_monitored", {}).items()
        )
        ledger_total = rep.get("ledger_total", rep["total"] + not_monitored_count)
        out += [
            f"  not live: {not_monitored_count} (not monitored — {statuses})",
            f"  ledger rows: {ledger_total} = {rep['total']} monitored + "
            f"{not_monitored_count} not live",
        ]
    held = rep.get("held_off") or []
    out.append(
        f"  overdue: {len(rep['overdue'])} · held off: {len(held)} (deliberately off by a declared "
        "hold, so named below with what holds it and never counted as overdue)"
    )
    out.append(f"  history: {rep['snapshots_stored']} prior snapshot(s)")
    if rep["snapshots_stored"] == 0:
        out.append(
            "  NOTE: no prior snapshot, so regressions cannot be computed yet — this run "
            "establishes the baseline"
        )
    out.append("")
    if rep["regressed"]:
        out.append("  REGRESSED (fired before, unchanged since the last snapshot):")
        for r in rep["regressed"]:
            out.append(
                f"    {r['capability_id']:<38} unchanged {r['unchanged_for_days']}d "
                f"(tolerance {r['tolerance_days']}d)"
            )
    else:
        out.append("  no regressions: nothing that used to fire has gone quiet")
    if rep["overdue"]:
        out += ["", "  OVERDUE against its own declared cadence:"]
        for r in rep["overdue"]:
            out.append(
                f"    {r['capability_id']:<38} silent {r['silent_days']}d "
                f"(tolerance {r['tolerance_days']}d; cadence {r['cadence']!r})"
            )
    else:
        out += ["", "  nothing overdue: no judged capability is silent past its declared cadence"]
    out += _format_held_off(held, rep.get("hold_unevaluated"))
    if rep.get("not_tick_observable"):
        out += [
            "",
            f"  not observable via tick heartbeats ({len(rep['not_tick_observable'])}) — "
            "their caller is the suite or a CLI, where production_heartbeat is a no-op, so "
            "their firing record says nothing either way:",
        ]
        out.append("    " + ", ".join(rep["not_tick_observable"]))
    if rep["never_fired"]:
        out += [
            "",
            f"  NEVER FIRED IN A TICK ({len(rep['never_fired'])}) — can-fire is not " "does-fire:",
        ]
        for cap_id in rep["never_fired"]:
            out.append(f"    {cap_id}")
    if rep["no_cadence_declared"]:
        out += [
            "",
            f"  no cadence declared ({len(rep['no_cadence_declared'])}) — silence cannot be "
            "judged, which is a documentation gap rather than an alarm:",
        ]
        out.append("    " + ", ".join(rep["no_cadence_declared"][:12]))
    return "\n".join(out) + "\n"


def _selftest() -> None:
    now = 1_800_000_000

    # Cadence parsing must handle the prose actually in the ledger, and must refuse to invent a
    # deadline for an on-demand trigger.
    assert expected_interval_days({"trigger_cadence": "daily"}) == 2.0
    assert expected_interval_days({"trigger_cadence": "weekly (cron '0 8 * * 1', Mondays"}) == 10.0
    assert expected_interval_days({"trigger_cadence": "every suite run"}) == 7.0
    assert expected_interval_days({"trigger_cadence": "monthly"}) == 40.0
    assert expected_interval_days({}) is None
    # DISCRIMINATING CASE. The first version of this assertion used "supervised CLI and focused
    # activation probe", which matches no CADENCE_PATTERN either — so it returned None with OR
    # without the on-demand guard and the test could not fail. Deleting the guard passed it. The
    # cadence below contains BOTH an on-demand marker AND a period word, so only the guard can
    # produce None; without it the answer is 10.0 and a false "overdue" alarm follows.
    both = {"trigger_cadence": "supervised CLI, nominally weekly"}
    assert (
        expected_interval_days(both) is None
    ), "an on-demand trigger promises no interval even when it mentions a period"
    assert expected_interval_days({"trigger_cadence": "nominally weekly"}) == 10.0, (
        "...and the same wording without the on-demand marker MUST still yield a tolerance, or the "
        "guard is just swallowing everything"
    )

    # TICK-OBSERVABILITY. production_heartbeat is a no-op outside a tick, so a suite-triggered
    # capability can never accrue a firing record. Reporting it as "never fired" alongside a truly
    # dormant lane would be a false alarm about the instrument rather than the subject.
    # Each clause must be decided by ONE mechanism, or removing that mechanism goes unnoticed. The
    # first version paired `test_gate` with "every suite run", so the cadence regex answered it and
    # deleting the matcher check still passed.
    assert not heartbeat_observable(
        {"matcher": {"kind": "test_gate"}, "trigger_cadence": "daily"}
    ), "a test_gate is decided by its MATCHER; pairing it with a suite cadence hides that"
    assert not heartbeat_observable(
        {"matcher": {"kind": "transport"}, "trigger_cadence": "every suite run"}
    ), "...and a suite CADENCE decides it independently of the matcher"
    assert heartbeat_observable({"matcher": {"kind": "tick_phase"}, "trigger_cadence": "daily"})
    assert heartbeat_observable({"matcher": {"kind": "transport"}, "trigger_cadence": "weekly"})

    # OBSERVERS ARE JUDGED ON RUNNING, NOT DELIVERING. Guards the category error that parked 8
    # capabilities in a measurement gap they could never leave.
    observer = {
        "matcher": {"kind": "tick_phase", "name": "x"},
        "status": "wired",
        "last_invocation": now - 3600,
        "event_history": [],
        "trigger_cadence": "daily",
    }
    assert capabilities.is_observer(observer)
    assert capabilities.classify_liveness(observer, now=now) == "observing"

    import tempfile

    with tempfile.TemporaryDirectory(prefix="firing-") as td:
        reg = pathlib.Path(td) / "capabilities.json"
        fresh = capabilities._blank_capability("cap-fresh")
        fresh.update(
            {
                "status": "wired",
                "last_invocation": now - 86400,
                "trigger_cadence": "daily",
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        stale = capabilities._blank_capability("cap-stale")
        stale.update(
            {
                "status": "wired",
                "last_invocation": now - 30 * 86400,
                "trigger_cadence": "daily",
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        silent = capabilities._blank_capability("cap-never")
        silent.update(
            {
                "status": "generated",
                "last_invocation": None,
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        # Fired once, promises NOTHING about cadence. It must never be called a regression: the
        # `tolerance is not None` guard is the only thing preventing that, and a first-run
        # assertion cannot reach the guard at all, so this fixture is what makes it testable.
        nocadence = capabilities._blank_capability("cap-nocadence")
        nocadence.update(
            {
                "status": "wired",
                "last_invocation": now - 86400,
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        # Fired once, promises MONTHLY. Eight days of silence is well inside its tolerance, so it
        # must not regress either — that exercises the `elapsed > tolerance` half.
        monthly = capabilities._blank_capability("cap-monthly")
        monthly.update(
            {
                "status": "wired",
                "last_invocation": now - 86400,
                "trigger_cadence": "monthly",
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        # NOT LIVE. Each is shaped so that, judged as live, it would land in every finding list:
        # the retired one never fired and declares no cadence (`never_fired`, `no_cadence_declared`),
        # the superseded one is a month past a daily cadence (`overdue`, and a regression once there
        # is history). So each list below is proven to exclude them, not merely not to contain them.
        retired = capabilities._blank_capability("cap-retired")
        retired.update(
            {
                "status": "retired",
                "last_invocation": None,
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        superseded = capabilities._blank_capability("cap-superseded")
        superseded.update(
            {
                "status": "superseded",
                "last_invocation": now - 30 * 86400,
                "trigger_cadence": "daily",
                "matcher": {"kind": "transport", "name": "t"},
            }
        )
        capabilities.save(
            {
                "cap-fresh": fresh,
                "cap-stale": stale,
                "cap-never": silent,
                "cap-nocadence": nocadence,
                "cap-monthly": monthly,
                "cap-retired": retired,
                "cap-superseded": superseded,
            },
            reg,
        )

        saved_hist = globals()["HISTORY"]
        globals()["HISTORY"] = pathlib.Path(td) / "hist.json"
        saved_ledger = reg.read_bytes()
        try:
            # Scope every assertion to the fixtures. `capabilities.load_declared` seeds nothing, so
            # this temp file reads as exactly its rows today; but it reconciles whatever the code
            # declares, so a total would be an assertion about the declaration tables, not this.
            mine = {
                "cap-fresh",
                "cap-stale",
                "cap-never",
                "cap-nocadence",
                "cap-monthly",
                "cap-retired",
                "cap-superseded",
            }
            rep = review(now=now, path=reg)
            rows = {r["capability_id"]: r for r in rep["rows"] if r["capability_id"] in mine}
            assert set(rows) == mine, f"a not-live row must stay in `rows`, accounted for: {rows}"
            assert rows["cap-fresh"]["ever_fired"] and rows["cap-stale"]["ever_fired"]
            assert "cap-nocadence" in rep["no_cadence_declared"], rep["no_cadence_declared"]
            assert not rows["cap-never"]["ever_fired"]
            assert "cap-never" in rep["never_fired"], rep["never_fired"]
            ov = {r["capability_id"] for r in rep["overdue"]} & mine
            assert ov == {"cap-stale"}, f"only the stale daily capability is overdue: {ov}"
            # LIVE ROWS ONLY: named with the reason, and in no finding list.
            for cap_id in ("cap-retired", "cap-superseded"):
                assert rows[cap_id]["monitored"] is False, rows[cap_id]
                assert rows[cap_id]["status"] in rows[cap_id]["not_monitored_because"]
                for key in ("never_fired", "no_cadence_declared"):
                    assert cap_id not in rep[key], (key, rep[key])
            assert rows["cap-never"]["monitored"] is True
            named = {s: sorted(set(ids) & mine) for s, ids in rep["not_monitored"].items()}
            assert {s: ids for s, ids in named.items() if ids} == {
                "retired": ["cap-retired"],
                "superseded": ["cap-superseded"],
            }, rep["not_monitored"]
            assert rep["ledger_total"] == rep["total"] + rep["not_monitored_count"], rep
            assert rep[capabilities.FINDING_POPULATION_KEY] == {
                "excluded_statuses": ["retired", "superseded"],
                "held_off_kinds": ["declared_gate", "retired_cadence_step"],
                "held_off_excluded_from": ["never_fired", "overdue", "regressed"],
            }, "the report must declare the rule it applied, or the tick cannot see it change"
            # Nothing here is held, and the report says so with a count, not an absent key.
            assert rep["held_off"] == [] and rep["held_off_count"] == 0, rep["held_off"]
            assert rep["hold_unevaluated"] is None, rep["hold_unevaluated"]
            # No history yet, so no regression can be claimed. Claiming one would be fabrication.
            assert rep["regressed"] == [], rep
            assert rep["snapshots_stored"] == 0, rep

            rec = record(rep)
            assert rec["recorded"] and rec["snapshots"] == 1, rec

            # REGRESSION DETECTION: a week later, cap-fresh has not fired again.
            later = now + 8 * 86400
            rep2 = review(now=later, path=reg)
            reg_ids = {r["capability_id"] for r in rep2["regressed"]} & mine
            assert (
                "cap-fresh" in reg_ids
            ), f"a capability that fired then went quiet must regress: {rep2['regressed']}"
            # cap-never never fired, so it cannot REGRESS — it is a never-fired, not a regression.
            assert "cap-never" not in reg_ids, rep2
            # A capability that promised no cadence cannot be late for anything.
            assert (
                "cap-nocadence" not in reg_ids
            ), f"no declared cadence means no promise to break: {rep2['regressed']}"
            # ...and one still inside its tolerance is not late either.
            assert (
                "cap-monthly" not in reg_ids
            ), f"8d of silence is inside a monthly tolerance: {rep2['regressed']}"
            # Fired before the snapshot and unchanged 8d later against a daily cadence: exactly a
            # regression, if it were live. A superseded row cannot go quiet; it was replaced.
            assert (
                "cap-superseded" not in reg_ids
            ), f"a superseded row is not monitored, so it cannot regress: {rep2['regressed']}"

            # The kill switch must stop writes, not just reads.
            globals()["DISABLED"] = True
            try:
                assert record(rep2)["recorded"] is False
            finally:
                globals()["DISABLED"] = False
            # ...and the history is the ONLY write. This temp ledger lacks every declared gate row,
            # so the writing loader would have seeded them all into it on the first review.
            assert reg.read_bytes() == saved_ledger, "review() wrote the capability ledger it reads"

            # HELD OFF, NOT OVERDUE. Each row below is a month past a daily cadence, so judged
            # live it is overdue. `cap-gate` declares that its gate blocks execution; `cap-step`
            # is run only by a retired step of a fixture registry, linked by `capabilities`;
            # `cap-reason` reaches `deliberately_gated` through a gate_reason ALONE, which does not
            # say the path that fires is blocked, so it must stay overdue.
            def silent_row(cap_id: str, **fields: Any) -> dict:
                row = capabilities._blank_capability(cap_id)
                row.update(
                    status="wired",
                    last_invocation=now - 30 * 86400,
                    trigger_cadence="daily",
                    matcher={"kind": "transport", "name": "t"},
                )
                row.update(fields)
                return row

            gated = {
                "gate_evidence": "fixture",
                "evidence_threshold": "fixture",
                "expiry": now + 5 * 86400,
                "next_transition": "retired",
            }
            held_reg = pathlib.Path(td) / "held.json"
            capabilities.save(
                {
                    "cap-gate": silent_row(
                        "cap-gate",
                        status="canary",
                        gate_reason="fixture gate",
                        gate_blocks_execution=True,
                        **gated,
                    ),
                    "cap-reason": silent_row(
                        "cap-reason",
                        gate_reason="fixture reason",
                        outcome_links=["advice:fixture"],
                        **gated,
                    ),
                    "cap-step": silent_row("cap-step"),
                },
                held_reg,
            )
            step = {
                "key": "fixture-step",
                "success_stamp": ".last-fixture-step",
                "cadence_days": 0,
                "capabilities": ("cap-step",),
                "retired": {
                    "since": "2026-10-02",
                    "reason": "fixture",
                    "re_enable_env": {"ORCH_FIXTURE_ON": "1"},
                },
            }
            held_rep = review(now=later, path=held_reg, environ={}, registry=(step,))
            held_ids = {r["capability_id"]: r for r in held_rep["held_off"]}
            assert set(held_ids) == {"cap-gate", "cap-step"}, held_rep["held_off"]
            assert [r["capability_id"] for r in held_rep["overdue"]] == ["cap-reason"], held_rep
            assert held_rep["held_off_count"] == 2 and held_rep["overdue_count"] == 1, held_rep
            assert held_ids["cap-gate"]["holds"][0]["reason"] == "fixture gate"
            assert held_ids["cap-step"]["holds"][0]["step"] == "fixture-step"
            assert held_ids["cap-step"]["held_from"] == ["overdue"], held_ids["cap-step"]
            # Lift the retirement and the row is judged again, at once: re-enabled and silent is
            # late, the same verdict `inspect_cadence` gives a lifted step.
            lifted = review(
                now=later, path=held_reg, environ={"ORCH_FIXTURE_ON": "1"}, registry=(step,)
            )
            assert {r["capability_id"] for r in lifted["held_off"]} == {"cap-gate"}, lifted
            assert {r["capability_id"] for r in lifted["overdue"]} == {"cap-reason", "cap-step"}
        finally:
            globals()["HISTORY"] = saved_hist

    print(
        "capability_firing_monitor.py selftest: OK (cadence parsing incl. on-demand refusal, "
        "observers judged on running, regression needs history, only live rows judged and "
        "not-live rows named, kill switch blocks writes, review writes no ledger, a declared "
        "gate or retired step holds a silent row off and a gate_reason alone does not)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--record",
        action="store_true",
        help="append this review to history (the weekly cadence step does this)",
    )
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = review()
    if args.record:
        rep["recorded"] = record(rep)
    print(json.dumps(rep, indent=2, sort_keys=True) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
