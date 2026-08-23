#!/usr/bin/env python3
"""capability_firing_monitor.py — DOES each capability fire, and did one stop?

The does-fire counterpart to `capability_activation_audit`'s can-fire. Both questions were needed
and only one was instrumented:

  * `capability_activation_audit` answers "CAN it fire" and persists snapshots, so a reachability
    regression is visible.
  * `capabilities usage` answers "did it fire in the last 28 days" and gives a next action — but
    only as a SNAPSHOT. Nothing was stored, so a capability that fired last week and went silent
    this week looked identical to one that has been healthy all along.
  * `switch_review` detects exactly that silence, but only for the five entries in
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
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import time
from typing import Any

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
    r"supervised|on demand|ad hoc|manual|dispatch|when |focused activation", re.I
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
SUITE_CADENCE_RE = re.compile(r"every suite run|every run|supervised CLI|CLI", re.I)


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
        if re.search(pattern, cadence, re.I):
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


def review(*, now: int | None = None, path: pathlib.Path | None = None) -> dict:
    """Current firing state for EVERY capability, plus regressions against stored history."""
    _capability_heartbeat()
    now = int(now if now is not None else time.time())
    ledger = capabilities.load(path or capabilities.REG)
    history = _load_history()
    previous = {row["capability_id"]: row for row in (history[-1]["rows"] if history else [])}

    rows, overdue, regressed, no_cadence = [], [], [], []
    for cap_id in sorted(ledger):
        cap = ledger[cap_id]
        last = int(cap.get("last_invocation") or 0)
        silent_days = None if not last else round((now - last) / 86400, 1)
        observer = capabilities.is_observer(cap)
        tolerance = expected_interval_days(cap)
        liveness = capabilities.classify_liveness(cap, now=now)
        observable = heartbeat_observable(cap)
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
        }
        rows.append(row)
        if not observable:
            # Its caller is the suite or a CLI; tick heartbeats cannot see it either way.
            continue

        if tolerance is None:
            if cap.get("trigger_cadence"):
                pass  # an on-demand cadence promises nothing; not a gap
            else:
                no_cadence.append(cap_id)
        elif last and silent_days is not None and silent_days > tolerance:
            overdue.append(
                {
                    "capability_id": cap_id,
                    "silent_days": silent_days,
                    "tolerance_days": tolerance,
                    "cadence": cap.get("trigger_cadence"),
                    "observer": observer,
                }
            )

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
                regressed.append(
                    {
                        "capability_id": cap_id,
                        "unchanged_for_days": round(elapsed, 1),
                        "tolerance_days": tolerance,
                        "last_invocation": last,
                    }
                )

    return {
        "generated_at": now,
        "total": len(rows),
        "fired_ever": sum(1 for r in rows if r["ever_fired"]),
        "never_fired": [
            r["capability_id"] for r in rows if not r["ever_fired"] and r["tick_observable"]
        ],
        "not_tick_observable": [r["capability_id"] for r in rows if not r["tick_observable"]],
        "observers": sum(1 for r in rows if r["observer"]),
        "overdue": overdue,
        "regressed": regressed,
        "no_cadence_declared": no_cadence,
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


def format_report(rep: dict) -> str:
    out = [
        "# Capability firing monitor",
        "",
        f"  {rep['fired_ever']} of {rep['total']} capabilities have ever fired "
        f"({rep['observers']} are observers, judged on running rather than delivering)",
        f"  history: {rep['snapshots_stored']} prior snapshot(s)",
    ]
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
        capabilities.save(
            {
                "cap-fresh": fresh,
                "cap-stale": stale,
                "cap-never": silent,
                "cap-nocadence": nocadence,
                "cap-monthly": monthly,
            },
            reg,
        )

        saved_hist = globals()["HISTORY"]
        globals()["HISTORY"] = pathlib.Path(td) / "hist.json"
        try:
            # Scope every assertion to the fixtures. `capabilities.load` reconciles DECLARED gated
            # capabilities into the ledger, so a temp file with three rows loads as ~17 — asserting
            # on totals would be asserting about the ambient ledger, not about this mechanism.
            mine = {"cap-fresh", "cap-stale", "cap-never", "cap-nocadence", "cap-monthly"}
            rep = review(now=now, path=reg)
            rows = {r["capability_id"]: r for r in rep["rows"] if r["capability_id"] in mine}
            assert set(rows) == mine, rows
            assert rows["cap-fresh"]["ever_fired"] and rows["cap-stale"]["ever_fired"]
            assert "cap-nocadence" in rep["no_cadence_declared"], rep["no_cadence_declared"]
            assert not rows["cap-never"]["ever_fired"]
            assert "cap-never" in rep["never_fired"], rep["never_fired"]
            ov = {r["capability_id"] for r in rep["overdue"]} & mine
            assert ov == {"cap-stale"}, f"only the stale daily capability is overdue: {ov}"
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

            # The kill switch must stop writes, not just reads.
            globals()["DISABLED"] = True
            try:
                assert record(rep2)["recorded"] is False
            finally:
                globals()["DISABLED"] = False
        finally:
            globals()["HISTORY"] = saved_hist

    print(
        "capability_firing_monitor.py selftest: OK (cadence parsing incl. on-demand refusal, "
        "observers judged on running, regression needs history, kill switch blocks writes)"
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
