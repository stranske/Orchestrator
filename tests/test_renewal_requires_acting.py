"""M1 restated (owner, 2026-10-17): acting in the monitor's trailing window is the renewal currency.

Measured before this change: 9 of 48 rows had acted-on edges in the rolling window, the admission
report stamped NEVER ACTED on the two most successful capabilities from an unobservable 14-day
window, and no renewal had ever consulted acting.
"""

from __future__ import annotations

import time

import capabilities
import switch_review


def _row(now: int, **extra) -> dict:
    cap = {"expiry": now + 10 * 86400, "status": "wired", "last_invocation": now - 86400}
    cap.update(extra)
    return cap


def test_a_row_that_is_invoked_but_not_acting_renews_only_with_a_fix_ref():
    now = int(time.time())
    cap = _row(now)
    assert capabilities.renewal_blocker(cap, now=now) is None, "unmeasured acting never blocks"
    assert capabilities.renewal_blocker(cap, now=now, acting=None) is None
    assert capabilities.renewal_blocker(cap, now=now, acting=3) is None
    blocked = capabilities.renewal_blocker(cap, now=now, acting=0)
    assert blocked and "fix ref" in blocked and "trailing window" in blocked
    assert (
        capabilities.renewal_blocker(cap, now=now, acting=0, evidence_refs=["run:abc"]) == blocked
    )
    for ref in ("stranske/Orchestrator#643", "https://github.com/stranske/Orchestrator/pull/656"):
        assert capabilities.renewal_blocker(cap, now=now, acting=0, evidence_refs=[ref]) is None
    never_invoked = _row(now, last_invocation=None)
    assert (
        capabilities.renewal_blocker(never_invoked, now=now, acting=0) is None
    ), "a row nothing invoked is not blocked by this rule; not_invoked is its own first break"


def test_fix_refs_are_issues_or_prs_not_run_ids():
    assert capabilities.names_a_fix(["stranske/Workflows#4051"])
    assert capabilities.names_a_fix(["https://github.com/stranske/Orchestrator/issues/643"])
    assert not capabilities.names_a_fix(["ledger:redirect", "run:keepalive:x", "fixed it"])
    assert not capabilities.names_a_fix(None)


def test_expiry_notice_carries_acting_and_agrees_with_renew():
    now = int(time.time())
    row = switch_review._expiry_row("t-cap", _row(now), now=now, acting=0)
    assert row["acting_trailing_window"] == 0 and row["renewable"] is False
    assert "fix ref" in row["blocker"]
    row = switch_review._expiry_row("t-cap", _row(now), now=now, acting=4)
    assert row["acting_trailing_window"] == 4 and row["renewable"] is True
    row = switch_review._expiry_row("t-cap", _row(now), now=now)
    assert row["acting_trailing_window"] is None and row["renewable"] is True


def test_acting_reader_returns_none_when_the_brain_is_unreadable(monkeypatch, tmp_path):
    import feedback

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "missing" / "brain.db")
    assert capabilities.acting_in_trailing_window("t-cap") is None
    assert switch_review._acting_index(int(time.time()), ["t-cap"]) is None
