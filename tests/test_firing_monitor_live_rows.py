"""The firing monitor judges LIVE rows only, names the rest, and is not credited for learning to.

A row retired on 2026-09-03, the day it was registered, was listed `never_fired` on every run from
then on: a retired or superseded row is not expected to fire, and the activation audit, the admission
report and the advisor already skipped it. These tests pin the rule where the monitor applies it,
pin that a not-live row is NAMED rather than silently dropped, and pin the grading side effect: the
fix changes the CONTENT of the monitor's finding set without changing its keys or its projection,
so without a declared population a pre-C8 grader would have minted a "useful" verdict for the fix
itself; observers are now verdict-exempt and only report deltas.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import partial

import pytest

import capabilities
import capability_advisor
import capability_firing_monitor as monitor
import capability_propensity as cp
import switch_review

NOW = 1_800_000_000
CAP = "capability-firing-monitor"
LIVE = ("live-silent", "live-stale")
NOT_LIVE = ("retired-silent", "retired-suite", "superseded-stale")


def _ledger(tmp_path):
    """Each not-live row is shaped to land in a finding list if it were judged as live."""
    shapes = {
        # never fired, tick-observable, cadence declared: `never_fired` and nothing else
        "live-silent": ("wired", None, "daily", "transport"),
        # a month past a daily cadence: `overdue`, and a regression once there is history
        "live-stale": ("wired", NOW - 30 * 86400, "daily", "transport"),
        # would be `never_fired` AND `no_cadence_declared`
        "retired-silent": ("retired", None, None, "transport"),
        # a suite-triggered matcher: would be `not_tick_observable`
        "retired-suite": ("retired", None, "daily", "test_gate"),
        # would be `overdue`, and `regressed` a week after a snapshot
        "superseded-stale": ("superseded", NOW - 30 * 86400, "daily", "transport"),
    }
    rows = {}
    for cap_id, (status, last, cadence, kind) in shapes.items():
        row = capabilities._blank_capability(cap_id)
        row.update(status=status, last_invocation=last, matcher={"kind": kind, "name": "t"})
        if cadence:
            row["trigger_cadence"] = cadence
        rows[cap_id] = row
    ledger = tmp_path / "capabilities.json"
    capabilities.save(rows, ledger)
    return ledger


def _review(tmp_path, monkeypatch, *, now=NOW):
    monkeypatch.setattr(monitor, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "firing-history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    return monitor.review(now=now, path=_ledger(tmp_path))


def _ids(rep, key):
    return {r["capability_id"] if isinstance(r, dict) else r for r in rep[key]}


def test_no_graded_finding_list_names_a_retired_or_superseded_row(tmp_path, monkeypatch):
    rep = _review(tmp_path, monkeypatch)
    graded = cp.TICK_FINDING_FIELDS[CAP]
    assert graded, "the monitor's findings are graded; an empty projection would test nothing"
    for key in [*graded, "not_tick_observable"]:
        assert not _ids(rep, key) & set(NOT_LIVE), (key, rep[key])
    # ...while the same lists still carry the live rows, so an empty report cannot pass this.
    assert "live-silent" in rep["never_fired"]
    assert "live-stale" in _ids(rep, "overdue")


def test_a_superseded_row_that_went_quiet_is_not_a_regression(tmp_path, monkeypatch):
    monitor.record(_review(tmp_path, monkeypatch))
    later = _review(tmp_path, monkeypatch, now=NOW + 8 * 86400)
    assert "live-stale" in _ids(later, "regressed"), later["regressed"]
    assert "superseded-stale" not in _ids(later, "regressed"), later["regressed"]


def test_the_whole_ledger_is_still_accounted_for(tmp_path, monkeypatch):
    rep = _review(tmp_path, monkeypatch)
    mine = set(LIVE) | set(NOT_LIVE)
    rows = {r["capability_id"]: r for r in rep["rows"] if r["capability_id"] in mine}
    assert set(rows) == mine
    named = {status: sorted(set(ids) & mine) for status, ids in rep["not_monitored"].items()}
    assert {status: ids for status, ids in named.items() if ids} == {
        "retired": ["retired-silent", "retired-suite"],
        "superseded": ["superseded-stale"],
    }
    assert rep["ledger_total"] == rep["total"] + rep["not_monitored_count"]
    for cap_id in NOT_LIVE:
        assert rows[cap_id]["monitored"] is False
        assert rows[cap_id]["status"] in rows[cap_id]["not_monitored_because"]
    for cap_id in LIVE:
        assert rows[cap_id]["monitored"] is True and "not_monitored_because" not in rows[cap_id]


def test_the_rendered_report_names_not_live_rows_without_calling_them_findings(
    tmp_path, monkeypatch
):
    text = monitor.format_report(_review(tmp_path, monkeypatch))
    header, never = text.split("NEVER FIRED IN A TICK", 1)
    assert "not live:" in header and "retired-silent" in header and "superseded-stale" in header
    assert "live-silent" in never
    assert "retired-silent" not in never


def test_the_report_declares_its_population_under_the_graders_key(tmp_path, monkeypatch):
    rep = _review(tmp_path, monkeypatch)
    declared = cp.declared_population(rep)
    assert declared == monitor.finding_population()
    # The live-row rule is still the shared one, and the held-off rule is stated beside it, so a
    # change to either reaches the grader as a changed population.
    assert {k: declared[k] for k in capabilities.live_finding_population()} == (
        capabilities.live_finding_population()
    )
    assert declared == {
        "excluded_statuses": sorted(capabilities.NOT_LIVE_STATES),
        "held_off_kinds": list(monitor.HOLD_KINDS),
        "held_off_excluded_from": list(monitor.SILENCE_FINDINGS),
    }


@pytest.mark.parametrize(
    "invocation_timestamp", [NOW - 100, NOW - 50], ids=["same-second", "later"]
)
def test_invocation_after_retirement_is_named(tmp_path, monkeypatch, invocation_timestamp):
    """A retired row can still run; keep that evidence outside the live silence findings."""
    ledger = _ledger(tmp_path)
    rows = capabilities.load(ledger, create=False)
    retirement = {
        "type": "transition",
        "from": "wired",
        "to": "retired",
        "timestamp": NOW - 100,
        "reason": "owner decision",
        "evidence_refs": ["owner/repo#1"],
    }
    kill_switch = "ORCH_RETIRED_EXAMPLE_DISABLED=1"
    scenarios = {
        "retired-but-running": [],
        "retired-clean": [],
        "retired-fixture": [],
        "retired-completion": [
            {"type": "invocation", "timestamp": NOW - 200, "ref": "started-before-retirement"},
            # Equal timestamps still require append order: this invocation preceded retirement.
            {"type": "invocation", "timestamp": NOW - 100, "ref": "same-second-before-retirement"},
        ],
        "retired-renewed": [],
        "retired-renewed-stale": [],
        "retired-reactivated-stale": [],
        "retired-again-clean": [],
    }
    for cap_id, earlier in scenarios.items():
        row = capabilities._blank_capability(cap_id)
        row.update(
            status="retired",
            kill_switch=kill_switch,
            matcher={"kind": "transport", "name": "example"},
            trigger_cadence="daily",
            event_history=[*earlier, deepcopy(retirement)],
        )
        rows[cap_id] = row
    capabilities.save(rows, ledger)
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.setattr(switch_review, "_acting_index", lambda *_: None)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    baseline = monitor.review(now=NOW, path=ledger)

    # Exercise the existing production emitter against this temporary ledger. Retirement
    # must not suppress evidence at the writer; the report is responsible for naming it.
    # A heartbeat appended after retirement still counts within the same timestamp second;
    # the same-second invocation appended before retirement above remains excluded.
    monkeypatch.setattr(
        capabilities,
        "heartbeat",
        partial(capabilities.heartbeat, path=ledger, timestamp=invocation_timestamp),
    )
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    assert capabilities.production_heartbeat(
        "retired-but-running", "invocation", ref="tick:production", metadata={"source": "tick"}
    )
    # A fixture on the same row must not hide real invocations or leak into their evidence.
    assert capabilities.production_heartbeat(
        "retired-but-running", "invocation", ref="tick:fixture", metadata={"source": "fixture"}
    )
    assert capabilities.production_heartbeat(
        "retired-but-running", "invocation", ref="tick:production-without-source"
    )
    assert capabilities.production_heartbeat(
        "retired-fixture", "invocation", ref="exercise", metadata={"source": "fixture"}
    )
    for event_type in ("success", "failure", "outcome"):
        assert capabilities.production_heartbeat(
            "retired-completion", event_type, ref="started-before-retirement"
        )
    for cap_id in (
        "retired-renewed",
        "retired-renewed-stale",
        "retired-reactivated-stale",
        "retired-again-clean",
    ):
        assert capabilities.production_heartbeat(cap_id, "invocation", ref="before-renewal")
    renewed = capabilities.load(ledger, create=False)
    renewed["retired-renewed"]["status"] = "wired"
    for cap_id in ("retired-renewed", "retired-renewed-stale", "retired-again-clean"):
        renewed[cap_id]["event_history"].append(
            {"type": capabilities.RENEWAL_EVENT, "timestamp": NOW - 25}
        )
    # History must cancel obsolete evidence even if the row's status is still retired.
    renewed["retired-reactivated-stale"]["event_history"].append(
        {"type": "transition", "from": "retired", "to": "wired", "timestamp": NOW - 25}
    )
    # A fresh retirement starts a new window; the old invocation cannot leak into it.
    renewed["retired-again-clean"]["event_history"].append({**retirement, "timestamp": NOW - 10})
    capabilities.save(renewed, ledger)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS")
    before = ledger.read_bytes()
    rep = monitor.review(now=NOW, path=ledger)
    expiry = switch_review.gate_expiry(now=NOW, path=ledger)
    for section in (rep, expiry):
        assert section["retired_but_invoked_count"] == 1, section["retired_but_invoked"]
        (entry,) = section["retired_but_invoked"]
        assert entry["capability_id"] == "retired-but-running"
        assert entry["kill_switch"] == kill_switch
        assert entry["retirement"] == retirement
        assert entry["invocations"] == [
            {
                "type": "invocation",
                "timestamp": invocation_timestamp,
                "ref": "tick:production",
                "metadata": {"source": "tick"},
            },
            {
                "type": "invocation",
                "timestamp": invocation_timestamp,
                "ref": "tick:production-without-source",
            },
        ]
        assert section["retired_but_invoked_measurement"] is None
    for text in (monitor.format_report(rep), "\n".join(switch_review.format_gate_expiry(expiry))):
        assert "retired_but_invoked: 1" in text
        assert "retired-but-running" in text and kill_switch in text
        assert "tick:production" in text and "owner decision" in text
    for key in (*monitor.SILENCE_FINDINGS, "no_cadence_declared", "not_tick_observable"):
        assert rep[key] == baseline[key], key
    assert rep[capabilities.FINDING_POPULATION_KEY] == baseline[capabilities.FINDING_POPULATION_KEY]
    assert "retired-but-running" in rep["not_monitored"]["retired"]
    assert rep["not_monitored"] == {
        status: [cap_id for cap_id in ids if cap_id != "retired-renewed"]
        for status, ids in baseline["not_monitored"].items()
    }
    assert rep["ledger_total"] == rep["total"] + rep["not_monitored_count"]
    assert ledger.read_bytes() == before, "a reporting predicate wrote lifecycle evidence"


def _check_retired_invocation_measurement(tmp_path, monkeypatch, state):
    ledger = tmp_path / f"{state}.json"
    if state == "corrupt":
        ledger.write_text("{not json")
    elif state == "unreadable":
        ledger.mkdir()
    elif state == "empty":
        capabilities.save({}, ledger)
    before = ledger.read_bytes() if ledger.is_file() else None
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    rep = monitor.review(now=NOW, path=ledger)
    assert rep["retired_but_invoked"] == []
    if state == "empty":
        assert rep["retired_but_invoked_count"] == 0
        assert rep["retired_but_invoked_measurement"] is None
        assert "retired_but_invoked: 0" in monitor.format_report(rep)
    else:
        assert rep["retired_but_invoked_count"] is None
        assert "unmeasured" in rep["retired_but_invoked_measurement"]
        assert "retired_but_invoked: unmeasured" in monitor.format_report(rep)
    assert (ledger.read_bytes() if ledger.is_file() else None) == before
    if state == "unreadable":
        assert ledger.is_dir()


def test_retired_invocation_measurement_distinguishes_unread_from_zero(tmp_path, monkeypatch):
    for state in ("missing", "corrupt", "unreadable", "empty"):
        _check_retired_invocation_measurement(tmp_path, monkeypatch, state)


def _exempt_observer_eval(rep, *, report_changed):
    ev = rep["evaluated"][0]
    assert ev["graded"] is False and ev["reason"] == "verdict_exempt", ev
    assert ev["report_changed"] is report_changed, ev
    assert rep["verdicts_recorded"] == 0, rep
    return ev


def test_the_repair_rebaselines_instead_of_minting_a_useful_verdict(tmp_path, monkeypatch):
    """Project the monitor's REAL report across the fix without recording usefulness verdicts."""
    after = _review(tmp_path, monkeypatch)
    # What the monitor emitted before the fix: the same report, with the retired row back in the
    # lists it used to reach and no declared population. Derived from the real report, so the two
    # productions differ ONLY by the fix.
    before = {
        key: value for key, value in after.items() if key != capabilities.FINDING_POPULATION_KEY
    }
    before["never_fired"] = sorted([*after["never_fired"], "retired-silent"])
    before["no_cadence_declared"] = sorted([*after["no_cadence_declared"], "retired-silent"])

    ledger = tmp_path / "tick-ledger.json"
    row = capabilities._blank_capability("obs")
    row.update(status="wired", matcher={"kind": "tick_phase", "name": "obs"})
    capabilities.save({"obs": row}, ledger)
    state = tmp_path / "state"
    state.mkdir()
    steps = {"obs": {"key": "obs", "artifact": "obs.json", "cadence_days": 0}}
    monkeypatch.setattr(cp, "TICK_FINDING_FIELDS", {"obs": cp.TICK_FINDING_FIELDS[CAP]})
    monkeypatch.setitem(
        capability_advisor.SURFACE_BINDINGS, cp.TICK_SURFACE, {"obs": "synthetic observer"}
    )

    def tick(report, day):
        artifact = state / "obs.json"
        artifact.write_text(json.dumps(report), encoding="utf-8")
        os.utime(artifact, (NOW + day, NOW + day))
        return cp.tick_evidence(now=NOW + day * 86400, state_dir=state, path=ledger, steps=steps)

    tick(before, 0)  # first sight: baseline
    _exempt_observer_eval(tick(before, 1), report_changed=False)
    repaired = tick(after, 2)
    assert repaired["verdicts_recorded"] == 0 and repaired["evaluated"] == [], repaired
    assert repaired["baselined"][0]["rebaselined"] is True
    assert "population" in repaired["baselined"][0]["reason"]
    _exempt_observer_eval(tick(after, 3), report_changed=False)
    # A genuinely new finding is still observed, but observers never earn a usefulness verdict.
    moved = {**after, "never_fired": sorted([*after["never_fired"], "live-new"])}
    _exempt_observer_eval(tick(moved, 4), report_changed=True)
