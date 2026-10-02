"""An ON switch is idle until its capability does something, and a consult trial is not that.

THE DEFECT (2026-10-02, latent). `switch_review` raised "ON but <cap> recorded no invocation in the
last REVIEW_DAYS" from the ledger row's `last_invocation` FIELD, and a consult trial moves that field
from any session: `capability_propensity.record_trigger` writes an `invocation` under
`capabilities.ADVICE_REF_PREFIX` through `capabilities.heartbeat`, which no environment flag gates.
So one trial of a switch-mapped capability, a rail-exercise round on `redirect-apply-bootstrap` say,
made an idle ON switch read active for REVIEW_DAYS and hid the drain its row carries. Measured that
day on a read-only copy of the live ledger: four of the six switch-mapped capabilities had only trial
invocations, all older than the window, so no verdict had flipped yet.

The rule now counts the row's non-trial invocation EVENTS, through `capabilities.split_invocations`,
the split the firing monitor reads too, and names everything it left out beside `idle_days`. Each
trial here is recorded by its real writer under a controlled clock, so a writer and a reader that
stopped agreeing on what a trial is would fail here rather than pass a synthetic event.
"""

from __future__ import annotations

import pytest

import capabilities
import capability_firing_monitor as monitor
import capability_propensity
import switch_review

FLAG = "ORCH_RANGE_LANE_ROLLOUT"
CAP = switch_review.SWITCH_CAPABILITY[FLAG]
NOW = 1_800_000_000
DAY = 86400
# The range lane's own invocation ref, as its tick heartbeat writes it: not a trial.
TICK_REF = "routing-decision.json"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A private ledger holding every reviewed switch's capability, nothing invoked yet."""
    path = tmp_path / "capabilities.json"
    ids = sorted(set(switch_review.SWITCH_CAPABILITY.values()))
    capabilities.save({c: capabilities._blank_capability(c) for c in ids}, path)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    return path


def _trial(ledger, monkeypatch, at: int, digest: str = "0123456789ab") -> None:
    """A consult trial of the switch's capability, recorded by its real writer at `at`."""
    with monkeypatch.context() as clock:
        clock.setattr(capabilities, "_now", lambda: at)
        experiment = capability_propensity.ADVICE_REF_PREFIX + digest
        assert capability_propensity.record_trigger(CAP, experiment, path=ledger)


def _invoke(ledger, at: int) -> None:
    """A non-trial invocation, as the capability's own heartbeat records one."""
    assert capabilities.heartbeat(CAP, "invocation", ref=TICK_REF, timestamp=at, path=ledger)


def _field(ledger) -> int:
    return int(capabilities.load(ledger, create=False)[CAP]["last_invocation"] or 0)


def _idle_row(ledger) -> dict | None:
    """The switch's ON-but-idle row, or None when the review reads it active."""
    rows = switch_review.switch_states(now=NOW, env={FLAG: "1"}, path=ledger)
    assert FLAG not in {r["flag"] for r in rows["held_off"]}, rows
    found = [r for r in rows["on_but_idle"] if r["flag"] == FLAG]
    assert len(found) <= 1, rows
    return found[0] if found else None


def test_a_consult_trial_inside_the_window_leaves_an_on_switch_idle(ledger, monkeypatch):
    _trial(ledger, monkeypatch, NOW - DAY)
    # POSITIVE CONTROL: the trial moved the field the old rule read, or this proves nothing.
    assert _field(ledger) == NOW - DAY

    row = _idle_row(ledger)
    assert row is not None, "a consult trial made an idle ON switch read active"
    assert row["idle_days"] is None, row
    assert (row["trials_excluded"], row["newest_trial_days"]) == (1, 1.0), row
    assert row["no_event_days"] is None, row
    assert switch_review.idle_phrase(row) == "never invoked", row
    phrase = switch_review.not_counted_phrase(row)
    window = f"inside the {switch_review.REVIEW_DAYS}d window"
    assert f"1 consult trial, the newest 1.0d ago, {window}" in phrase, phrase


def test_a_non_trial_invocation_inside_the_window_makes_it_active(ledger, monkeypatch):
    _invoke(ledger, NOW - 2 * DAY)
    assert _idle_row(ledger) is None, "a non-trial invocation inside the window must read active"
    # A trial that is NEWER does not take that away: it is ignored, not held against the switch.
    _trial(ledger, monkeypatch, NOW - DAY)
    assert _idle_row(ledger) is None, "a newer trial hid a non-trial invocation inside the window"


def test_a_row_with_only_trials_outside_the_window_is_idle_as_before(ledger, monkeypatch):
    # The live bootstrap's shape: two trials, both a month old, and nothing else.
    _trial(ledger, monkeypatch, NOW - 30 * DAY, digest="aaaaaaaaaaaa")
    _trial(ledger, monkeypatch, NOW - 29 * DAY, digest="bbbbbbbbbbbb")
    row = _idle_row(ledger)
    assert row is not None, "trials older than the window left an ON switch active"
    # Idle as before, and now with the right age: the field said 29 days, but nothing the switch's
    # own path did was ever recorded, and the row says which two events it did not count.
    assert row["idle_days"] is None, f"the idle age was measured from a consult trial: {row}"
    assert (row["trials_excluded"], row["newest_trial_days"]) == (2, 29.0), row
    phrase = switch_review.not_counted_phrase(row)
    assert phrase.startswith("2 consult trials, the newest 29.0d ago (a trial"), phrase


def test_plain_idleness_says_nothing_was_left_out(ledger):
    _invoke(ledger, NOW - 10 * DAY)
    row = _idle_row(ledger)
    assert row is not None and row["idle_days"] == 10.0, row
    # A COUNT, asserted as one: zero is the finding, so truthiness would forbid it.
    assert row["trials_excluded"] == 0 and row["newest_trial_days"] is None, row
    assert row["no_event_days"] is None, row
    assert switch_review.not_counted_phrase(row) == "", row


def test_a_last_invocation_no_event_recorded_does_not_count_and_is_named(ledger):
    """A causal reconciliation sets the field with no event, from influence edges the outcome
    bridge draws from lane consult trials' verdicts: a trial through a second door."""
    rows = capabilities.load(ledger, create=False)
    rows[CAP]["last_invocation"] = NOW - DAY
    capabilities.save(rows, ledger)
    row = _idle_row(ledger)
    assert row is not None, "a last_invocation no invocation event recorded made it read active"
    assert (row["no_event_days"], row["trials_excluded"]) == (1.0, 0), row
    phrase = switch_review.not_counted_phrase(row)
    assert "a last_invocation 1.0d ago that no invocation event recorded" in phrase, phrase


def test_a_capability_with_no_ledger_row_is_unmeasured_not_never_invoked(ledger):
    """A fresh clone or CI's bootstrapped ledger may hold no row for the capability at all. Nothing
    it did can be read there, so the row is still raised, toward the alarm, and says it is blind."""
    rows = capabilities.load(ledger, create=False)
    del rows[CAP]
    capabilities.save(rows, ledger)
    row = _idle_row(ledger)
    assert row is not None, "a capability with no ledger row read as an active switch"
    assert row["trials_excluded"] is None, f"an unread row reported a measured count: {row}"
    assert switch_review.idle_phrase(row).startswith("UNMEASURED"), row
    assert switch_review.not_counted_phrase(row) == "", row


def test_the_review_and_the_firing_monitor_read_one_split(ledger, monkeypatch):
    """Two readers that each told trials apart in their own loop could disagree about which
    invocation was one; both go through `capabilities.split_invocations`, and say the same."""
    _invoke(ledger, NOW - 10 * DAY)
    _trial(ledger, monkeypatch, NOW - DAY)
    stored = capabilities.load(ledger, create=False)[CAP]
    seen: list = []
    real = capabilities.split_invocations
    monkeypatch.setattr(
        capabilities,
        "split_invocations",
        lambda cap: seen.append(cap["capability_id"]) or real(cap),
    )

    row = _idle_row(ledger)
    found = monitor.silence_evidence(stored, 2.0, NOW)
    assert seen == [CAP, CAP], f"each reader must take its split from the one helper: {seen}"
    assert row is not None, "the review counted an invocation the firing monitor calls a trial"
    assert (row["trials_excluded"], row["idle_days"]) == (1, 10.0), row
    assert (found["trial_invocations"], found["non_trial_invocations"]) == (1, 1), found
    assert found["last_invocation_from"] == "consult_trial", found
