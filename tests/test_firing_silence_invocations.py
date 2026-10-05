"""A step that RUNS must record an INVOCATION, or the firing monitor calls its capability silent.

Measured 2026-10-02 on the live ledger. Every weekly firing report since 2026-09-26 listed five rows
as overdue, and none of their steps had stopped:

* `rail-exercise-cadence`, `route-weights-export` and `research-usage-guard` recorded only their
  result. A `success` moves `last_success`; only an `invocation` moves `last_invocation`, which is
  what the monitor, `classify_liveness` and `switch_review` read. Each step ran on schedule (4, 22 and
  26 successes), had no production invocation, and showed a consult trial from 2026-09-03 as its
  "last invocation".
* `stall-watcher` was credited once per claim, inside `watch.classify_lane`, so a sweep with no
  claims recorded nothing. From 2026-09-14 every tick swept 0 claims, about 23 sweeps a day.
* `redirect-apply-bootstrap` is NOT one of these. Its invocation is an authorised apply by design,
  and nothing it ever saw was drainable. `switch_review` reads that invocation to raise "ON but idle"
  with the gate's drain, so a pass that applies nothing must still record nothing.

Each step's executed path runs here inside a simulated tick (ORCH_CAPABILITY_HEARTBEATS=1) against a
private ledger, and the monitor judges the result. The monitor's own annotation is pinned too: it
names a run recorded without an invocation and a trial that is not a tick, and moves no finding.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

import capabilities
import capability_firing_monitor as monitor
import capability_propensity
import claims
import rail_exercise
import redirect_apply
import redirect_sweep
import research_usage_guard
import route_weights_export
import watch

DAY = 86400
# The live row's own declarations. The other four are declared in code and reconciled by
# `load_declared`; stall-watcher's live in the ledger alone, so the fixture carries them.
STALL_WATCHER = {
    "status": "generated",
    "matcher": {"kind": "tick_phase", "name": "watch"},
    "trigger_cadence": "every tick — redirect_sweep runs unconditionally from orchestrate.sh",
}
SILENCE = monitor.SILENCE_FINDINGS


@pytest.fixture
def tick(tmp_path, monkeypatch):
    """A private ledger every production heartbeat lands in, as it would inside an active tick."""
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for cap_id in (
        "rail-exercise-cadence",
        "route-weights-export",
        "research-usage-guard",
        "redirect-apply-bootstrap",
        "stall-watcher",
    ):
        rows[cap_id] = capabilities._blank_capability(cap_id)
    rows["stall-watcher"].update(STALL_WATCHER)
    capabilities.save(rows, ledger)
    real = capabilities.heartbeat
    # `production_heartbeat` names no path, so its default is the machine's ledger: route every
    # heartbeat, from every module, to the private copy instead.
    monkeypatch.setattr(
        capabilities, "heartbeat", lambda *a, **kw: real(*a, **{**kw, "path": ledger})
    )
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "firing-history.json")
    return ledger


def _events(ledger, cap_id: str, kind: str) -> list[dict]:
    row = capabilities.load(ledger, create=False)[cap_id]
    return [e for e in row.get("event_history") or [] if e.get("type") == kind]


def _one(ledger, cap_id: str, kind: str) -> dict:
    events = _events(ledger, cap_id, kind)
    assert len(events) == 1, f"{cap_id}: the run must record exactly one {kind}, got {events}"
    return events[0]


def _silences(ledger, monkeypatch, cap_id: str) -> list[str]:
    """Which silence findings the monitor files this row under, judged a minute from now."""
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS")  # the monitor's own credit is not under test
    rep = monitor.review(now=int(time.time()) + 60, path=ledger, environ={}, registry=())
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    return [
        key
        for key in SILENCE
        if cap_id in [r["capability_id"] if isinstance(r, dict) else r for r in rep[key]]
    ]


def test_the_rail_exercise_cadence_records_its_run_as_an_invocation(tick, monkeypatch):
    assert _silences(tick, monkeypatch, "rail-exercise-cadence") == ["never_fired"]
    totals = {"contracts": 2, "passed": 2, "failed": 0}
    monkeypatch.setattr(rail_exercise, "report", lambda *_a: {"totals": totals, "tree": "fixture"})
    monkeypatch.setattr(sys, "argv", ["rail_exercise.py", "--json"])

    assert rail_exercise.main() == 0
    invocation = _one(tick, "rail-exercise-cadence", "invocation")
    success = _one(tick, "rail-exercise-cadence", "success")
    assert invocation["ref"] == "rail_exercise.main", invocation
    assert invocation["timestamp"] <= success["timestamp"], "the run is recorded before its result"
    assert _silences(tick, monkeypatch, "rail-exercise-cadence") == []


def test_the_route_weights_export_records_its_run_as_an_invocation(tick, monkeypatch, tmp_path):
    assert _silences(tick, monkeypatch, "route-weights-export") == ["never_fired"]
    monkeypatch.setattr(
        route_weights_export, "build_document", lambda *_a: {"source_version": "fixture"}
    )
    monkeypatch.setattr(route_weights_export, "write_document", lambda *_a: True)
    monkeypatch.setattr(sys, "argv", ["route_weights_export.py", "--state-dir", str(tmp_path)])

    assert route_weights_export.main() == 0
    invocation = _one(tick, "route-weights-export", "invocation")
    assert invocation["ref"] == "route_weights_export.main", invocation
    assert _events(tick, "route-weights-export", "success") == []
    assert _silences(tick, monkeypatch, "route-weights-export") == []


def test_the_research_usage_daily_report_records_its_run_as_an_invocation(
    tick, monkeypatch, tmp_path
):
    assert _silences(tick, monkeypatch, "research-usage-guard") == ["never_fired"]
    monkeypatch.setattr(
        research_usage_guard, "generate_usage_report", lambda **_k: {"health_status": "OK"}
    )

    research_usage_guard.write_usage_report(tmp_path / "research-usage-report.json")
    invocation = _one(tick, "research-usage-guard", "invocation")
    assert invocation["ref"] == "daily-report", invocation
    _one(tick, "research-usage-guard", "success")
    assert _silences(tick, monkeypatch, "research-usage-guard") == []


def test_a_sweep_that_finds_no_claims_still_credits_the_stall_watcher(tick, monkeypatch):
    assert _silences(tick, monkeypatch, "stall-watcher") == ["never_fired"]
    monkeypatch.setattr(claims, "active_claims", lambda **_k: {})

    report = redirect_sweep.sweep()
    assert report["active_claim_count"] == 0 and report["watched_count"] == 0, report
    invocation = _one(tick, "stall-watcher", "invocation")
    assert invocation["ref"] == "redirect_sweep.sweep", invocation
    # ONE per sweep: a second tick is a second invocation, never coalesced into the first, because
    # an every-tick promise is judged against a quarter-day tolerance.
    redirect_sweep.sweep()
    assert len(_events(tick, "stall-watcher", "invocation")) == 2, "two sweeps, two invocations"
    assert _silences(tick, monkeypatch, "stall-watcher") == []


def test_a_sweep_of_claims_is_one_invocation_and_the_classifier_keeps_its_own(
    tick, monkeypatch, tmp_path
):
    """The sweep credits itself once and tells the classifier not to, so N claims are one
    invocation, never N+1 (`usage_rate` counts events). A direct classifier call still credits."""
    log = tmp_path / "lane.log"
    log.write_text("working\n", encoding="utf-8")
    fake = {f"o/r#{i}": {"agent": "codex", "pid": 999_999_999, "log": str(log)} for i in (1, 2)}
    monkeypatch.setattr(claims, "active_claims", lambda **_k: fake)

    report = redirect_sweep.sweep()
    assert report["watched_count"] == 2, report
    invocation = _one(tick, "stall-watcher", "invocation")
    assert invocation["ref"] == "redirect_sweep.sweep", invocation
    watch.classify_lane(pid=999_999_999, log=str(log))
    refs = [e["ref"] for e in _events(tick, "stall-watcher", "invocation")]
    assert refs == ["redirect_sweep.sweep", "watch.main"], refs


def test_a_bootstrap_pass_that_applies_nothing_records_no_invocation(tick, monkeypatch, tmp_path):
    """The bootstrap's invocation means an authorised apply, and `switch_review` relies on that:
    its "ON but idle" row, and the drain that row carries, are the alarm for a gate that cannot
    drain. A pass over a current plan with no candidates must leave the field exactly as it was."""
    plan = tmp_path / "stage2-plan.json"
    plan.write_text(json.dumps({"generated_at": int(time.time()), "plans": []}), encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()

    def never(*_a, **_k):
        raise AssertionError("no candidate exists, so nothing may be judged or applied")

    out = redirect_apply.apply_candidates(
        report_dir=reports,
        plan_path=plan,
        corpus_path=tmp_path / "corpus.jsonl",
        env={redirect_apply.BOOTSTRAP_FLAG: "1"},
        role_runner=never,
        apply_runner=never,
    )
    assert out["passing_screen"] == 0 and out["applied"] == 0, out
    assert _events(tick, "redirect-apply-bootstrap", "invocation") == []
    row = capabilities.load(tick, create=False)["redirect-apply-bootstrap"]
    assert row["last_invocation"] is None, row["last_invocation"]


# ------------------------------------------------------------------------- the monitor's evidence


def _row(cap_id: str, *, last: int, events: list[tuple[str, int, str]]) -> dict:
    row = capabilities._blank_capability(cap_id)
    row.update(
        status="wired",
        last_invocation=last,
        trigger_cadence="daily",
        matcher={"kind": "transport", "name": "t"},
        event_history=[{"type": k, "timestamp": ts, "ref": ref} for k, ts, ref in events],
    )
    return row


def test_the_monitor_names_what_a_silent_rows_history_contradicts(tmp_path, monkeypatch):
    now = 1_800_000_000
    month = now - 30 * DAY
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "firing-history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    rows = {
        # Ran every day since its trial and recorded only the result: the 2026-10-02 shape.
        "recorder": _row(
            "recorder",
            last=month,
            events=[("invocation", month, monitor.TRIAL_REF_PREFIX + "abc")]
            + [("success", now - d * DAY, "daily-report") for d in range(1, 4)],
        ),
        # Ran properly once, a month ago, and stopped: a real silence the history must not excuse.
        "stopped": _row(
            "stopped",
            last=month,
            events=[("invocation", month, "x.main"), ("success", month + 30, "x.main")],
        ),
    }
    ledger = tmp_path / "capabilities.json"
    capabilities.save(rows, ledger)
    rep = monitor.review(now=now, path=ledger, environ={}, registry=())

    recorder, stopped = rep["silence_evidence"]["recorder"], rep["silence_evidence"]["stopped"]
    assert recorder["runs_after_last_invocation"] == 3, recorder
    assert recorder["last_run"] == {"timestamp": now - DAY, "type": "success", "age_days": 1.0}
    assert recorder["ran_within_tolerance"] is True, recorder
    assert recorder["last_invocation_from"] == "consult_trial", recorder
    assert recorder["non_trial_invocations"] == 0 and recorder["trial_invocations"] == 1
    # The result 30 s behind its own invocation is THAT invocation's result, never a second run.
    assert stopped["runs_after_last_invocation"] == 0, stopped
    assert stopped["last_invocation_from"] == "non_trial", stopped
    text = monitor.format_report(rep)
    assert (
        "RAN 3x with no invocation of its own, the latest a success 1.0d ago, inside its 2.0d"
        in (text)
    ), (text)
    assert "a recording defect, not a silence" in text, text
    assert text.count("its only invocations are consult trials") == 1, text

    # AN ANNOTATION MOVES NO FINDING: the same rows with their histories emptied are judged
    # identically, in membership and order, in every silence list.
    bare = {cap_id: {**row, "event_history": []} for cap_id, row in rows.items()}
    capabilities.save(bare, ledger)
    blind = monitor.review(now=now, path=ledger, environ={}, registry=())
    for key in SILENCE:
        assert rep[key] == blind[key], (key, rep[key], blind[key])
    assert {r["capability_id"] for r in rep["overdue"]} == {"recorder", "stopped"}, rep["overdue"]


def test_a_trial_the_propensity_writer_records_reads_as_a_trial(tmp_path):
    """Writer and reader share ONE constant, so a trial recorded the way every consult records one
    reads as a trial, never as a tick firing. A reader holding its own copy of the spelling would
    keep passing a synthetic test while counting every real trial as a tick."""
    ledger = tmp_path / "capabilities.json"
    capabilities.save({"cap": capabilities._blank_capability("cap")}, ledger)
    experiment = capability_propensity.ADVICE_REF_PREFIX + "0123456789ab"
    assert capability_propensity.record_trigger("cap", experiment, path=ledger)

    row = capabilities.load(ledger, create=False)["cap"]
    found = monitor.silence_evidence(row, 2.0, int(time.time()))
    assert found["last_invocation_from"] == "consult_trial", found
    assert (found["trial_invocations"], found["non_trial_invocations"]) == (1, 0), found
