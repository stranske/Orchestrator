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
    monkeypatch.setattr(monitor, "STATE_DIR", tmp_path / "state")
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
    # Detect retired execution beside real live silences and a stored regression baseline.
    # The diagnostic must not change the findings, population, accounting or heartbeat call.
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(monitor, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monitor.record(monitor.review(now=NOW, path=ledger))
    later = NOW + 8 * 86400
    before = monitor.review(now=later, path=ledger)
    assert before["never_fired"] == ["live-silent"]
    assert _ids(before, "overdue") == {"live-stale"}
    assert _ids(before, "regressed") == {"live-stale"}
    assert before["retired_but_invoked_count"] == 0

    rows = capabilities.load_declared(ledger)
    retirement = {"type": "transition", "to": "retired", "timestamp": NOW}
    rows["retired-silent"]["event_history"].append(retirement)
    rows["retired-silent"]["kill_switch"] = "disable retired producer"
    capabilities.save(rows, ledger)
    capabilities.heartbeat(
        "retired-silent", "invocation", timestamp=later - 1, ref="run:late", path=ledger
    )
    ledger_before = ledger.read_bytes()
    history_before = monitor.HISTORY.read_bytes()
    heartbeat_calls = []
    monkeypatch.setattr(
        capabilities,
        "production_heartbeat",
        lambda *args, **kwargs: heartbeat_calls.append((args, kwargs)),
    )

    after = monitor.review(now=later, path=ledger)

    assert after["retired_but_invoked_count"] == 1
    assert after["retired_but_invoked_measurement"] == "measured"
    assert after["retired_but_invoked"] == [
        {
            "capability_id": "retired-silent",
            "retirement": retirement,
            "invocations": [{"type": "invocation", "timestamp": later - 1, "ref": "run:late"}],
            "kill_switch": "disable retired producer",
        }
    ]
    for key in (
        capabilities.FINDING_POPULATION_KEY,
        *monitor.SILENCE_FINDINGS,
        "no_cadence_declared",
        "not_tick_observable",
        "not_monitored",
        "not_monitored_count",
        "total",
        "ledger_total",
        "fired_ever",
        "overdue_count",
        "held_off",
        "held_off_count",
        "silence_evidence",
        "snapshots_stored",
    ):
        assert after[key] == before[key], key
    assert heartbeat_calls == [
        (("capability-firing-monitor", "invocation"), {"ref": "capability_firing_monitor.review"})
    ]
    assert ledger.read_bytes() == ledger_before
    assert monitor.HISTORY.read_bytes() == history_before


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
        # The real writer records whole seconds: append order must distinguish an
        # invocation after retirement from one before retirement at the same timestamp.
        "timestamp": retirement["timestamp"],
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
        "pre-retirement-invocation": [invocation, retirement],
        "pre-retirement-completion": [
            {**invocation, "timestamp": NOW - 200},
            retirement,
            {**invocation, "type": "success"},
        ],
        "renewed": [
            retirement,
            invocation,
            {"type": "renewed", "timestamp": NOW - 10},
            {**invocation, "timestamp": NOW - 5, "ref": "run:after-renewal"},
        ],
        "live-again": [
            retirement,
            invocation,
            {"type": "transition", "to": "observed", "timestamp": NOW - 10},
            {**invocation, "timestamp": NOW - 5, "ref": "run:after-live-transition"},
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
    # Exercise the production entry point with its real writer bound to this ledger.
    # Its existing emission switch still applies, including for retired capabilities.
    before = ledger.read_bytes()
    with monkeypatch.context() as production:
        production.setattr(
            capabilities,
            "heartbeat",
            partial(capabilities.heartbeat, path=ledger, timestamp=invocation["timestamp"]),
        )
        assert not capabilities.production_heartbeat("retired-running", "invocation")
        assert ledger.read_bytes() == before
        production.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
        assert capabilities.production_heartbeat(
            "retired-running",
            "invocation",
            ref=invocation["ref"],
            metadata=invocation["metadata"],
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

    # Exercise lifecycle changes through the real writer too: old execution must not leak
    # into a new retirement window, even after another invocation while the row is live.
    capabilities.transition(
        "retired-running", "observed", reason="revived", timestamp=NOW + 1, path=ledger
    )
    capabilities.heartbeat(
        "retired-running", "invocation", ref="run:live", timestamp=NOW + 2, path=ledger
    )

    def reports_at(timestamp):
        before = ledger.read_bytes()
        reports = (
            monitor.review(now=timestamp, path=ledger),
            switch_review.gate_expiry(now=timestamp, path=ledger),
        )
        assert ledger.read_bytes() == before, "reports must not change lifecycle evidence"
        return reports

    live, expiry = reports_at(NOW + 2)
    for report in (live, expiry):
        assert report["retired_but_invoked_count"] == 0
        assert report["retired_but_invoked"] == []
        assert report["retired_but_invoked_measurement"] == "measured"
    assert live["total"] == 1
    assert "retired-running" not in live["not_monitored"]["retired"]

    capabilities.transition(
        "retired-running", "retired", reason="retired again", timestamp=NOW + 3, path=ledger
    )
    new_retirement = capabilities.load_declared(ledger)["retired-running"]["event_history"][-1]
    capabilities.heartbeat(
        "retired-running",
        "invocation",
        ref="run:fixture",
        metadata={"source": "fixture"},
        timestamp=NOW + 4,
        path=ledger,
    )
    for report in reports_at(NOW + 4):
        assert report["retired_but_invoked_count"] == 0
        assert report["retired_but_invoked"] == []

    new_invocations = [
        {**invocation, "timestamp": NOW + offset, "ref": f"run:new-{offset}"} for offset in (5, 6)
    ]
    for event in new_invocations:
        capabilities.heartbeat(
            "retired-running",
            "invocation",
            timestamp=event["timestamp"],
            ref=event["ref"],
            metadata=event["metadata"],
            path=ledger,
        )
    for report in reports_at(NOW + 6):
        assert report["retired_but_invoked_count"] == 1, "count capabilities, not invocations"
        assert report["retired_but_invoked"] == [
            {**expected, "retirement": new_retirement, "invocations": new_invocations}
        ]

    # Legacy producers omit source metadata. Only the fixture source is excluded, so
    # their real heartbeat must be retained beside the explicitly production events.
    capabilities.heartbeat(
        "retired-running", "invocation", timestamp=NOW + 7, ref="run:legacy", path=ledger
    )
    legacy_invocation = {"type": "invocation", "timestamp": NOW + 7, "ref": "run:legacy"}
    for report in reports_at(NOW + 7):
        assert report["retired_but_invoked_count"] == 1
        assert report["retired_but_invoked"] == [
            {
                **expected,
                "retirement": new_retirement,
                "invocations": [*new_invocations, legacy_invocation],
            }
        ]

    # Independent retired producers retain their own evidence and kill switch. A second
    # anomaly must increase the capability count, even while the first has several events.
    second_invocation = {**invocation, "timestamp": NOW + 8, "ref": "run:second-producer"}
    capabilities.heartbeat(
        "silent",
        "invocation",
        timestamp=second_invocation["timestamp"],
        ref=second_invocation["ref"],
        metadata=second_invocation["metadata"],
        path=ledger,
    )
    second_expected = {
        "capability_id": "silent",
        "retirement": retirement,
        "invocations": [second_invocation],
        "kill_switch": "disable silent",
    }
    reports = reports_at(NOW + 8)
    for report in reports:
        assert report["retired_but_invoked_count"] == 2
        assert report["retired_but_invoked"] == [
            {
                **expected,
                "retirement": new_retirement,
                "invocations": [*new_invocations, legacy_invocation],
            },
            second_expected,
        ]
        assert report["retired_but_invoked_measurement"] == "measured"
    for text in (
        monitor.format_report(reports[0]),
        "\n".join(switch_review.format_gate_expiry(reports[1])),
    ):
        assert "retired_but_invoked: 2" in text
        for evidence in ("disable retired-running", "disable silent", "run:second-producer"):
            assert evidence in text

    # An unreadable ledger invalidates previously measured anomalies. Repairing the same
    # path restores the current evidence, rather than retaining an unknown or stale count.
    readable_ledger = ledger.read_bytes()
    try:
        ledger.write_text("{not json", encoding="utf-8")
        unreadable = reports_at(NOW + 8)
        for report in unreadable:
            assert report["retired_but_invoked_count"] is None
            assert report["retired_but_invoked"] == []
            assert report["retired_but_invoked_measurement"].startswith("unmeasured")
        for text in (
            monitor.format_report(unreadable[0]),
            "\n".join(switch_review.format_gate_expiry(unreadable[1])),
        ):
            assert "retired_but_invoked: unmeasured" in text
            assert "run:second-producer" not in text
    finally:
        ledger.write_bytes(readable_ledger)
    for previous, recovered in zip(reports, reports_at(NOW + 8)):
        assert recovered["retired_but_invoked_count"] == 2
        assert recovered["retired_but_invoked"] == previous["retired_but_invoked"]
        assert recovered["retired_but_invoked_measurement"] == "measured"

    # A later fixture heartbeat must neither hide production evidence nor become evidence.
    capabilities.heartbeat(
        "silent",
        "invocation",
        timestamp=NOW + 9,
        ref="run:later-fixture",
        metadata={"source": "fixture"},
        path=ledger,
    )
    for previous, current in zip(reports, reports_at(NOW + 9)):
        assert current["retired_but_invoked_count"] == 2
        assert current["retired_but_invoked"] == previous["retired_but_invoked"]

    # Readers recompute each producer independently after a lifecycle decision. Clearing
    # one anomaly must retain the other producer's evidence and declared kill switch.
    capabilities.transition(
        "retired-running", "observed", reason="revived again", timestamp=NOW + 10, path=ledger
    )
    for report in reports_at(NOW + 10):
        assert report["retired_but_invoked_count"] == 1
        assert report["retired_but_invoked"] == [second_expected]

    capabilities.transition(
        "silent", "observed", reason="revived second producer", timestamp=NOW + 11, path=ledger
    )
    cleared = reports_at(NOW + 11)
    for report in cleared:
        assert report["retired_but_invoked_count"] == 0
        assert report["retired_but_invoked"] == []
        assert report["retired_but_invoked_measurement"] == "measured"
    for text in (
        monitor.format_report(cleared[0]),
        "\n".join(switch_review.format_gate_expiry(cleared[1])),
    ):
        assert "retired_but_invoked: 0" in text
        assert "run:second-producer" not in text
        assert "run:later-fixture" not in text


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
    # An absent history and a stored empty list both still mean "no history", measured.
    assert capabilities.invoked_after_retirement({"status": "retired"}) is None
    assert capabilities.invoked_after_retirement({"status": "retired", "event_history": []}) is None


@pytest.mark.parametrize(
    "cancellation",
    [
        {"type": "renewed"},
        *[
            {"type": "transition", "to": status}
            for status in capabilities.CANONICAL_STATES
            if status not in capabilities.NOT_LIVE_STATES
        ],
    ],
    ids=lambda event: event.get("to", event["type"]),
)
def test_retirement_cancellation_requires_a_new_retirement(cancellation):
    """Later execution cannot revive an anomaly cancelled by a lifecycle decision."""
    retirement = {"type": "transition", "to": "retired", "timestamp": NOW - 100}
    old_invocation = {"type": "invocation", "timestamp": NOW - 75, "ref": "old"}
    later_invocation = {"type": "invocation", "timestamp": NOW - 25, "ref": "later"}
    cap = {
        "status": "retired",
        "kill_switch": "disable producer",
        "event_history": [
            retirement,
            old_invocation,
            {**cancellation, "timestamp": NOW - 50},
            later_invocation,
        ],
    }
    before = json.dumps(cap)
    assert capabilities.invoked_after_retirement(cap) is None
    assert json.dumps(cap) == before

    # Another retirement starts a fresh window; neither earlier invocation belongs to it.
    new_retirement = {**retirement, "timestamp": NOW - 10}
    cap["event_history"].append(new_retirement)
    assert capabilities.invoked_after_retirement(cap) is None
    new_invocation = {**later_invocation, "timestamp": NOW, "ref": "new"}
    cap["event_history"].append(new_invocation)
    before = json.dumps(cap)
    assert capabilities.invoked_after_retirement(cap) == {
        "retirement": new_retirement,
        "invocations": [new_invocation],
        "kill_switch": "disable producer",
    }
    assert json.dumps(cap) == before


def test_retirement_uses_append_order_and_metadata_source():
    """Same-second events use append order, and only metadata.source identifies fixtures."""
    retirement = {"type": "transition", "to": "retired", "timestamp": NOW}
    invocation = {"type": "invocation", "timestamp": NOW}
    production = {**invocation, "metadata": {"source": "production"}, "source": "fixture"}
    cap = {
        "status": "retired",
        "event_history": [
            {**invocation, "ref": "before retirement"},
            retirement,
            {**invocation, "metadata": {"source": "fixture"}, "source": "production"},
            production,
            invocation,
        ],
    }
    before = json.dumps(cap)
    assert capabilities.invoked_after_retirement(cap) == {
        "retirement": retirement,
        "invocations": [production, invocation],
        "kill_switch": None,
    }
    assert json.dumps(cap) == before
    cap["event_history"].append({"type": "renewed", "timestamp": NOW})
    assert capabilities.invoked_after_retirement(cap) is None


def test_retired_invocation_measurement_requires_a_readable_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(switch_review, "_acting_index", lambda *_: None)
    for index, contents in enumerate([None, "{not json", '{"schema_version": -1}']):
        ledger = tmp_path / f"capabilities-{index}.json"
        if contents is not None:
            ledger.write_text(contents)
        rep = monitor.review(now=NOW, path=ledger)
        assert rep["retired_but_invoked_count"] is None
        assert rep["retired_but_invoked"] == []
        assert "unmeasured" in rep["retired_but_invoked_measurement"]
        text = monitor.format_report(rep)
        assert "retired_but_invoked: unmeasured" in text
        # The live findings were not measured either: no zero counts and no all-clear sentences.
        assert "unmeasured" in rep["ledger_measurement"]
        assert "live findings: unmeasured" in text
        assert "no regressions" not in text
        assert "nothing overdue" not in text
        assert "overdue: 0" not in text
        assert "0 of 0 live capabilities" not in text
        expiry = switch_review.gate_expiry(now=NOW, path=ledger)
        assert expiry["status"] == "unknown"
        assert expiry["retired_but_invoked_count"] is None
        assert expiry["retired_but_invoked"] == []
        assert expiry["retired_but_invoked_measurement"] == "unmeasured"
        assert "retired_but_invoked: unmeasured" in "\n".join(
            switch_review.format_gate_expiry(expiry)
        )
        assert ledger.exists() is (contents is not None)
        if contents is not None:
            assert ledger.read_text() == contents


@pytest.mark.parametrize(
    "broken_history",
    [
        [{"type": "transition", "to": "retired"}],
        [{"type": "transition", "to": "retired", "timestamp": "invalid"}],
        [
            {"type": "transition", "to": "retired", "timestamp": NOW - 100},
            {"type": "invocation", "timestamp": None},
        ],
        [
            {"type": "transition", "to": "retired", "timestamp": NOW - 100},
            {"type": "invocation", "timestamp": NOW, "metadata": "invalid"},
        ],
        [None],
        None,
        0,
        "",
        {},
    ],
    ids=[
        "missing-timestamp",
        "invalid-timestamp",
        "null-timestamp",
        "invalid-metadata",
        "null-event",
        "null-history",
        "zero-history",
        "empty-string-history",
        "empty-object-history",
    ],
)
def test_unreadable_retirement_history_preserves_live_findings(
    tmp_path, monkeypatch, broken_history
):
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(switch_review, "_acting_index", lambda *_: None)
    rows = capabilities.load(_ledger(tmp_path), create=False)
    retirement = {"type": "transition", "to": "retired", "timestamp": NOW - 100}
    invocation = {"type": "invocation", "timestamp": NOW - 50}
    # Valid anomalies before AND after the corrupt row must not become a partial measured result.
    for cap_id, history in (
        ("a-running", [retirement, invocation]),
        ("b-unreadable", broken_history),
        ("z-running", [retirement, invocation]),
    ):
        row = capabilities._blank_capability(cap_id)
        row.update(status="retired", event_history=history, kill_switch=f"disable {cap_id}")
        rows[cap_id] = row
    ledger = tmp_path / "capabilities.json"
    capabilities.save(rows, ledger)
    before = ledger.read_bytes()

    rep = monitor.review(now=NOW, path=ledger)

    assert rep["retired_but_invoked"] == []
    assert rep["retired_but_invoked_count"] is None
    assert "unmeasured" in rep["retired_but_invoked_measurement"]
    assert "b-unreadable" in rep["retired_but_invoked_measurement"]
    text = monitor.format_report(rep)
    assert "retired_but_invoked: unmeasured" in text
    # The ledger itself was readable, so the live findings stay measured and visible.
    assert rep["ledger_measurement"] == "measured"
    assert "live findings: unmeasured" not in text
    assert "live-silent" in text and "live-stale" in text
    assert rep[capabilities.FINDING_POPULATION_KEY] == monitor.finding_population()
    assert rep["never_fired"] == ["live-silent"]
    assert _ids(rep, "overdue") == {"live-stale"}
    assert rep["not_monitored"] == {
        "retired": ["a-running", "b-unreadable", "retired-silent", "retired-suite", "z-running"],
        "superseded": ["superseded-stale"],
    }
    assert rep["total"] == len(LIVE)
    assert rep["ledger_total"] == rep["total"] + rep["not_monitored_count"] == len(rows)
    expiry = switch_review.gate_expiry(now=NOW, path=ledger)
    assert expiry["status"] == "unknown"
    assert "unreadable" in expiry["measurement"]
    assert expiry["retired_but_invoked"] == []
    assert expiry["retired_but_invoked_count"] is None
    assert expiry["retired_but_invoked_measurement"] == "unmeasured"
    text = "\n".join(switch_review.format_gate_expiry(expiry))
    assert "NOT MEASURED" in text and "retired_but_invoked: unmeasured" in text
    assert "disable a-running" not in text and "disable z-running" not in text
    assert ledger.read_bytes() == before


@pytest.mark.parametrize(
    "invocation_timestamp", [NOW - 100, NOW - 50], ids=["same-second", "later"]
)
def test_production_heartbeat_after_retirement_preserves_live_findings(
    tmp_path, monkeypatch, invocation_timestamp
):
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
    # Delayed recording of pre-retirement work updates the row's counters but must
    # not turn its later completion into a new invocation after retirement.
    with monkeypatch.context() as delayed:
        delayed.setattr(
            capabilities, "heartbeat", partial(capabilities.heartbeat, timestamp=NOW - 200)
        )
        assert capabilities.production_heartbeat(
            "retired-completion",
            "invocation",
            ref="late-recorded-before-retirement",
            metadata={"source": "tick"},
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
    # Even fresh production invocations after cancellation cannot revive the old
    # retirement window when a writer has left the row's status stale.
    with monkeypatch.context() as resumed:
        resumed.setattr(
            capabilities, "heartbeat", partial(capabilities.heartbeat, timestamp=NOW - 20)
        )
        for cap_id in ("retired-renewed-stale", "retired-reactivated-stale"):
            assert capabilities.production_heartbeat(
                cap_id, "invocation", ref="after-cancellation", metadata={"source": "tick"}
            )
    resumed_rows = capabilities.load(ledger, create=False)
    for cap_id in ("retired-renewed-stale", "retired-reactivated-stale"):
        assert resumed_rows[cap_id]["status"] == "retired"
        assert resumed_rows[cap_id]["event_history"][-1]["ref"] == "after-cancellation"
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
        assert section["retired_but_invoked_measurement"] == "measured"
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
        assert rep["retired_but_invoked_measurement"] == "measured"
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
