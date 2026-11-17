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
    assert rep["retired_but_invoked_count"] == 0
    assert rep["retired_but_invoked_measurement"] == "measured"
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


def test_invocation_after_retirement_is_named(tmp_path, monkeypatch):
    """Only execution after an unrenewed retirement belongs in this separate diagnostic."""
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(switch_review, "_acting_index", lambda *_: None)
    retirement = {
        "type": "transition",
        "from": "wired",
        "to": "retired",
        "timestamp": NOW - 100,
        "reason": "owner retired this capability",
    }
    invocation = {
        "type": "invocation",
        "timestamp": NOW - 50,
        "ref": "run:production",
        "metadata": {"source": "production"},
    }
    histories = {
        "retired-running": [retirement],
        "silent": [retirement],
        "fixture": [retirement, {**invocation, "metadata": {"source": "fixture"}}],
        "completion-only": [retirement, {**invocation, "type": "success"}],
        "failure-only": [retirement, {**invocation, "type": "failure"}],
        "outcome-only": [retirement, {**invocation, "type": "outcome"}],
        "pre-retirement-completion": [
            {**invocation, "timestamp": NOW - 200},
            retirement,
            {**invocation, "type": "success"},
        ],
        "renewed": [retirement, invocation, {"type": "renewed", "timestamp": NOW - 10}],
        "live-again": [
            retirement,
            invocation,
            {"type": "transition", "to": "observed", "timestamp": NOW - 10},
        ],
        "retired-again-clean": [
            retirement,
            invocation,
            {"type": "renewed", "timestamp": NOW - 20},
            {**retirement, "timestamp": NOW - 10},
        ],
        "no-transition": [invocation],
        "backdated": [retirement, {**invocation, "timestamp": NOW - 200}],
    }
    rows = {}
    for cap_id, history in histories.items():
        row = capabilities._blank_capability(cap_id)
        row.update(
            status="retired",
            event_history=history,
            kill_switch=f"disable {cap_id}",
            trigger_cadence="daily",
            # A summary timestamp without an invocation event must not manufacture evidence.
            last_invocation=NOW - 50,
        )
        rows[cap_id] = row
    ledger = tmp_path / "capabilities.json"
    capabilities.save(rows, ledger)
    # Exercise the real heartbeat writer: retired execution is observed, never blocked here.
    capabilities.heartbeat(
        "retired-running",
        "invocation",
        timestamp=invocation["timestamp"],
        ref=invocation["ref"],
        metadata=invocation["metadata"],
        path=ledger,
    )
    before = ledger.read_bytes()
    rep = monitor.review(now=NOW, path=ledger)
    expiry = switch_review.gate_expiry(now=NOW, path=ledger)
    expected = {
        "capability_id": "retired-running",
        "retirement": retirement,
        "invocations": [invocation],
        "kill_switch": "disable retired-running",
    }
    for report in (rep, expiry):
        assert report["retired_but_invoked_count"] == 1
        assert report["retired_but_invoked"] == [expected]
        assert report["retired_but_invoked_measurement"] == "measured"
    assert rep["not_monitored"] == {"retired": sorted(rows)}
    assert rep["total"] == 0 and rep["ledger_total"] == len(rows)
    assert rep[capabilities.FINDING_POPULATION_KEY] == monitor.finding_population()
    for key in (*monitor.SILENCE_FINDINGS, "no_cadence_declared", "not_tick_observable"):
        assert rep[key] == []
    for text in (monitor.format_report(rep), "\n".join(switch_review.format_gate_expiry(expiry))):
        assert "retired_but_invoked: 1" in text
        assert "disable retired-running" in text
        assert "run:production" in text and retirement["reason"] in text
    assert ledger.read_bytes() == before, "reports must not change the ledger"


def test_retirement_predicate_is_read_only_and_uses_latest_retirement():
    retirement = {"type": "transition", "to": "retired", "timestamp": NOW - 100}
    invocation = {"type": "invocation", "timestamp": NOW - 100}
    cap = {"status": "retired", "event_history": [retirement, invocation]}
    before = json.dumps(cap)
    assert capabilities.invoked_after_retirement(cap)["invocations"] == [invocation]
    assert json.dumps(cap) == before
    cap["event_history"] += [
        {"type": "renewed", "timestamp": NOW - 80},
        {**retirement, "timestamp": NOW - 50},
        {**invocation, "timestamp": NOW - 25},
    ]
    found = capabilities.invoked_after_retirement(cap)
    assert found["retirement"]["timestamp"] == NOW - 50
    assert found["invocations"] == [{**invocation, "timestamp": NOW - 25}]
    cap["status"] = "observed"
    assert capabilities.invoked_after_retirement(cap) is None


def test_retired_invocation_measurement_requires_a_readable_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    for index, contents in enumerate([None, "{not json", '{"schema_version": -1}']):
        ledger = tmp_path / f"capabilities-{index}.json"
        if contents is not None:
            ledger.write_text(contents)
        rep = monitor.review(now=NOW, path=ledger)
        assert rep["retired_but_invoked_count"] is None
        assert rep["retired_but_invoked"] == []
        assert "unmeasured" in rep["retired_but_invoked_measurement"]
        assert "retired_but_invoked: unmeasured" in monitor.format_report(rep)
        assert ledger.exists() is (contents is not None)
        if contents is not None:
            assert ledger.read_text() == contents


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
