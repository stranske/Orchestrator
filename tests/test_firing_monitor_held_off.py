"""A deliberately-off capability is reported HELD OFF, with what holds it, never as overdue.

Every weekly run listed `issue-readiness` (its cadence step retired by default on 2026-09-15) and
`range-lane-rollout` (its declared gate blocks the delivering path) under both `overdue` and
`regressed`: alarms that a decision already answered, sitting beside the real ones. The monitor now
moves a silent row that a DECLARED hold keeps off into `held_off`, with the hold and what lifts it.
These tests pin:

1. which declarations hold a row, and which do not: the declared gate holds, a `gate_reason` alone
   does not, a retired step holds only the capabilities it DECLARES (never a name match), and one
   live carrier keeps a capability judged;
2. that nothing is hidden: a held row says which findings it would be, both counts are reported,
   and every live row lands in exactly one of not-observable, held off, or judged;
3. that a firing row is not called held off, and `no_cadence_declared` is untouched by a hold;
4. the drained rendering, by construction, and a registry that cannot be read failing toward
   motion: it holds nothing and says so;
5. the grading side effect: the edit re-baselines the tick's grader instead of minting a "useful"
   verdict, while a hold lifted LATER is graded like any other change.
"""

from __future__ import annotations

import json
import os

import pytest

import cadence_registry
import capabilities
import capability_advisor
import capability_firing_monitor as monitor
import capability_propensity as cp

NOW = 1_800_000_000
DAY = 86400
CAP = "capability-firing-monitor"
FLAG = {"ORCH_FIXTURE_ON": "1"}
RETIREMENT = {"since": "2026-10-02", "reason": "fixture retirement", "re_enable_env": FLAG}
# What `validate_capability` demands of any row carrying a gate_reason.
GATED = {"gate_evidence": "fixture", "evidence_threshold": "fixture", "next_transition": "retired"}


def _row(cap_id: str, *, last: int | None = NOW - 30 * DAY, cadence="daily", **fields) -> dict:
    """A live, tick-observable row, by default a month past a daily cadence."""
    row = capabilities._blank_capability(cap_id)
    row.update(status="wired", last_invocation=last, matcher={"kind": "transport", "name": "t"})
    if cadence:
        row["trigger_cadence"] = cadence
    row.update(fields)
    return row


def _gate(cap_id: str, **fields) -> dict:
    """A row whose gate is DECLARED to block execution: `classify_liveness`'s declared branch."""
    fields = {
        "status": "canary",
        "gate_reason": f"{cap_id}: the gate is shut",
        "gate_blocks_execution": True,
        "expiry": NOW + 5 * DAY,
        **GATED,
        **fields,
    }
    return _row(cap_id, **fields)


def _step(key: str, *caps: str, retired: bool = True) -> dict:
    row = {"key": key, "success_stamp": f".last-{key}", "cadence_days": 0, "capabilities": caps}
    if retired:
        row["retired"] = RETIREMENT
    return row


@pytest.fixture
def review(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "firing-history.json")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    ledger = tmp_path / "capabilities.json"

    def run(rows, *, registry=(), environ=None, now=NOW):
        capabilities.save({row["capability_id"]: row for row in rows}, ledger)
        return monitor.review(now=now, path=ledger, environ=environ or {}, registry=registry)

    return run


def _ids(rep: dict, key: str) -> list[str]:
    return [r["capability_id"] if isinstance(r, dict) else r for r in rep[key]]


def _held(rep: dict) -> dict[str, dict]:
    return {r["capability_id"]: r for r in rep["held_off"]}


def test_a_declared_gate_holds_a_silent_row_off_and_names_the_gate(review):
    rows = [_gate("gated"), _row("late")]
    first = review(rows)
    monitor.record(first)
    rep = review(rows, now=NOW + 8 * DAY)  # a week on, nothing fired: overdue AND regressed

    assert capabilities.classify_liveness(rows[0], now=NOW) == "deliberately_gated"
    held = _held(rep)
    assert list(held) == ["gated"], rep["held_off"]
    assert held["gated"]["held_from"] == ["overdue", "regressed"], held["gated"]
    (hold,) = held["gated"]["holds"]
    assert hold["kind"] == monitor.DECLARED_GATE
    assert hold["reason"] == "gated: the gate is shut"
    assert hold["expires_on"] == capabilities.utc_date(NOW + 5 * DAY)
    assert monitor.DECLARED_GATE_LIFTS in hold["lifts_when"] and hold["expires_on"] in (
        hold["lifts_when"]
    ), hold
    # Moved out of every silence finding, and the real alarm beside it is untouched.
    for key in monitor.SILENCE_FINDINGS:
        assert "gated" not in _ids(rep, key), (key, rep[key])
    assert _ids(rep, "overdue") == ["late"] and _ids(rep, "regressed") == ["late"]
    # BOTH QUANTITIES, as numbers.
    assert (rep["overdue_count"], rep["held_off_count"]) == (1, 1), rep


def test_a_gate_reason_alone_does_not_hold_a_row_off(review):
    # `deliberately_gated` through the WEAKER branch: a gate_reason, outcome evidence, no
    # `gate_blocks_execution`. It says a gate exists, not that the path that fires is blocked, so
    # its silence is still a finding. Asserting the label first keeps this case discriminating:
    # dropping the `gate_blocks_execution` test from `holds_for` would hold it off.
    row = _row(
        "reason-only",
        gate_reason="writes are gated",
        outcome_links=["advice:fixture"],
        expiry=NOW + 5 * DAY,
        **GATED,
    )
    rep = review([row])
    assert capabilities.classify_liveness(row, now=NOW) == "deliberately_gated"
    assert _ids(rep, "overdue") == ["reason-only"], rep["overdue"]
    assert rep["held_off"] == [] and rep["held_off_count"] == 0
    assert next(r for r in rep["rows"] if r["capability_id"] == "reason-only")["holds"] == []


def test_a_retired_step_holds_only_what_it_declares_never_a_name_match(review):
    # `named-like-step` shares its id with a retired step that does NOT declare it, which is the
    # coincidence `issue-readiness` happens to have. A name match must hold nothing.
    registry = (_step("named-like-step"), _step("runner", "declared"))
    rows = [_row("declared"), _row("named-like-step")]
    rep = review(rows, registry=registry)

    held = _held(rep)
    assert list(held) == ["declared"], rep["held_off"]
    (hold,) = held["declared"]["holds"]
    assert hold["kind"] == monitor.RETIRED_STEP and hold["step"] == "runner"
    assert hold["reason"] == cadence_registry.retirement_line(registry[1])
    assert cadence_registry.re_enable_when(registry[1]) in hold["lifts_when"]
    assert _ids(rep, "overdue") == ["named-like-step"], rep["overdue"]


def test_a_lifted_retirement_is_judged_again_at_once(review):
    registry = (_step("runner", "declared"),)
    rep = review([_row("declared")], registry=registry, environ=FLAG)
    # Re-enabled and silent is late, the verdict `inspect_cadence` gives a lifted, aged step.
    assert rep["held_off"] == [], rep["held_off"]
    assert _ids(rep, "overdue") == ["declared"], rep["overdue"]
    # Exact string equality, as the generated shell tests it: a near miss lifts nothing.
    near = review([_row("declared")], registry=registry, environ={"ORCH_FIXTURE_ON": "1 "})
    assert list(_held(near)) == ["declared"], near["held_off"]


def test_one_live_carrier_keeps_the_capability_judged(review):
    live_and_retired = (_step("off", "shared"), _step("on", "shared", retired=False))
    rep = review([_row("shared")], registry=live_and_retired)
    assert rep["held_off"] == [] and _ids(rep, "overdue") == ["shared"], rep

    both_retired = (_step("off", "shared"), _step("also-off", "shared"))
    rep = review([_row("shared")], registry=both_retired)
    holds = _held(rep)["shared"]["holds"]
    assert [hold["step"] for hold in holds] == ["off", "also-off"], holds


def test_a_firing_row_is_not_called_held_off_but_carries_its_hold(review):
    # A declared gate on a row that fired an hour ago: there is no silence to explain, so it is in
    # no list at all, and its hold is still visible on its row.
    rep = review([_gate("gated-but-firing", last=NOW - 3600)])
    assert rep["held_off"] == [] and rep["overdue"] == [], rep
    row = next(r for r in rep["rows"] if r["capability_id"] == "gated-but-firing")
    assert [hold["kind"] for hold in row["holds"]] == [monitor.DECLARED_GATE], row


def test_a_never_fired_held_row_is_held_from_never_fired_and_keeps_its_cadence_gap(review):
    # Never fired AND no cadence: the hold explains the silence, never the missing promise, so the
    # row leaves `never_fired` and stays in `no_cadence_declared`.
    rep = review([_gate("gated-new", last=None, cadence=None)])
    held = _held(rep)
    assert held["gated-new"]["held_from"] == ["never_fired"], held
    assert held["gated-new"]["silent_days"] is None
    assert "gated-new" not in rep["never_fired"]
    assert rep["no_cadence_declared"] == ["gated-new"], rep["no_cadence_declared"]
    text = monitor.format_report(rep)
    assert "gated-new" in text and "never fired; would be never_fired" in text, text


def test_every_live_row_lands_in_exactly_one_place(review):
    suite = _row("suite-run", cadence="every suite run")  # not tick-observable
    registry = (_step("runner", "step-held"),)
    rows = [_gate("gated"), _row("step-held"), _row("late"), _row("fresh", last=NOW - 3600), suite]
    rep = review(rows, registry=registry)
    live = {r["capability_id"] for r in rep["rows"] if r["monitored"]}
    unobservable = set(rep["not_tick_observable"])
    held = set(_held(rep))
    judged = live - unobservable - held
    assert unobservable == {"suite-run"} and held == {"gated", "step-held"}, (unobservable, held)
    assert judged == {"late", "fresh"}, judged
    for key in monitor.SILENCE_FINDINGS:
        assert not set(_ids(rep, key)) & held, (key, rep[key])
    # Every live row carries its holds; not-live rows are accounted for elsewhere.
    assert all("holds" in r for r in rep["rows"] if r["monitored"])


def test_the_drained_report_says_so_in_words(review):
    # LATCHED-GATE Q4, by construction: render the drained state and read it. A section that prints
    # only when non-empty cannot say it has drained, and "nothing printed" reads as "not checked".
    drained = review([_row("fresh", last=NOW - 3600)])
    text = monitor.format_report(drained)
    assert "overdue: 0 · held off: 0" in text, text
    assert "nothing overdue: no judged capability is silent past its declared cadence" in text
    assert "held off: none — no live capability's silence is explained" in text, text
    assert "HELD OFF" not in text and "NOT EVALUATED" not in text

    registry = (_step("runner", "step-held"),)
    full = review([_gate("gated"), _row("step-held"), _row("late")], registry=registry)
    text = monitor.format_report(full)
    assert "overdue: 1 · held off: 2" in text, text
    assert "HELD OFF (2)" in text and "held off: none" not in text, text
    assert "held by its declared gate: gated: the gate is shut" in text, text
    assert f"held by cadence step runner: {cadence_registry.retirement_line(registry[0])}" in text
    assert text.count("lifts when:") == 2, text
    overdue_section = text.split("OVERDUE against its own declared cadence:", 1)[1]
    overdue_section = overdue_section.split("HELD OFF", 1)[0]
    assert "late" in overdue_section and "gated" not in overdue_section, overdue_section


def test_an_unreadable_registry_holds_nothing_and_says_so(review):
    # FAIL TOWARD MOTION. A malformed declaration must not hold anything off: the row it would
    # have held stays an alarm, and the report names why it was not evaluated. The declared gate
    # does not depend on the registry, so it is still held.
    broken = ({"key": "bad", "success_stamp": ".last-bad", "capabilities": "step-held"},)
    rep = review([_row("step-held"), _gate("gated")], registry=broken)
    assert rep["hold_unevaluated"] and "bad" in rep["hold_unevaluated"], rep["hold_unevaluated"]
    assert _ids(rep, "overdue") == ["step-held"], rep["overdue"]
    assert list(_held(rep)) == ["gated"], rep["held_off"]
    assert "RETIREMENT HOLDS NOT EVALUATED" in monitor.format_report(rep)
    # ...and a registry that reads states that it did: None, not an empty string or a falsy flag.
    assert review([_row("step-held")], registry=())["hold_unevaluated"] is None


def test_held_off_is_reported_not_graded():
    graded = cp.TICK_FINDING_FIELDS[CAP]
    # The held-off list is an EXPLANATION, and it moves when someone declares or lifts a hold,
    # which is the ledger changing rather than the monitor finding anything (the `reachable_ids`
    # lesson). Its effect on the graded lists is what gets graded.
    assert "held_off" not in graded, graded
    # And the population names lists the grader actually reads, so it describes real findings.
    assert set(monitor.SILENCE_FINDINGS) <= set(graded), (monitor.SILENCE_FINDINGS, graded)


def test_the_change_rebaselines_and_a_later_lift_is_graded(tmp_path, monkeypatch, review):
    """Grade the monitor's REAL report through its REAL projection, across the change."""
    registry = (_step("runner", "step-held"),)
    rows = [_gate("gated"), _row("step-held"), _row("late")]
    after = review(rows, registry=registry)
    # What main emitted for the same ledger: the held rows in `overdue`, the live-only population,
    # and no held-off keys. Derived from the real report, so the two differ ONLY by the change.
    before = {
        key: value
        for key, value in after.items()
        if key not in {"held_off", "held_off_count", "overdue_count", "hold_unevaluated"}
    }
    before[capabilities.FINDING_POPULATION_KEY] = capabilities.live_finding_population()
    before["overdue"] = sorted(
        [*after["overdue"], *({"capability_id": cid} for cid in _held(after))],
        key=lambda r: r["capability_id"],
    )
    lifted = review(rows, registry=registry, environ=FLAG)
    assert _ids(lifted, "overdue") == ["late", "step-held"], lifted["overdue"]

    ledger = tmp_path / "tick-ledger.json"
    obs = capabilities._blank_capability("obs")
    obs.update(status="wired", matcher={"kind": "tick_phase", "name": "obs"})
    capabilities.save({"obs": obs}, ledger)
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
        return cp.tick_evidence(now=NOW + day * DAY, state_dir=state, path=ledger, steps=steps)

    tick(before, 0)  # first sight: baseline
    assert tick(before, 1)["evaluated"][0]["useful"] is False  # grading works on this projection
    changed = tick(after, 2)
    assert changed["verdicts_recorded"] == 0 and changed["evaluated"] == [], changed
    assert changed["baselined"][0]["rebaselined"] is True
    assert "population" in changed["baselined"][0]["reason"], changed["baselined"][0]
    assert tick(after, 3)["evaluated"][0]["useful"] is False  # comparable again, and unchanged
    # A hold LIFTED later is a lifecycle action, graded like a retirement: the row returns to
    # `overdue` under the same declared population, and that is a real change.
    assert tick(lifted, 4)["evaluated"][0]["useful"] is True
