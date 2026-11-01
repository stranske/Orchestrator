"""#1050 owner-side pass number grades coordinator receipts without live provenance."""

from __future__ import annotations

import closer_stale_scoped_root_pass as grade


def test_stale_scoped_root_pass_pending_before_check_date():
    pending = grade.stale_scoped_root_pass_number(
        {"authority": True, "reason": "ok"},
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS - 1,
    )
    assert pending["status"] == "pending"
    assert pending["check_date"] == grade.STALE_SCOPED_ROOT_PASS_CHECK_LABEL
    assert pending["issue"] == 1050
    assert pending["next_break_issue"] is None


def test_stale_scoped_root_pass_authority_true_passes():
    passed = grade.stale_scoped_root_pass_number(
        {
            "authority": True,
            "reason": grade.PRIOR_STOP_LATCH_REASON,
            "blocking_reservations": 11,
            "drainable_reservations": 0,
        },
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
    )
    assert passed["status"] == "pass"
    assert passed["grade_reason"] == "authority true"
    assert passed["next_break_issue"] is None


def test_stale_scoped_root_pass_fails_while_prior_stop_latched():
    failed = grade.stale_scoped_root_pass_number(
        {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
    )
    assert failed["status"] == "fail"
    assert "prior_run_has_no_valid_stop" in failed["grade_reason"]


def test_stale_scoped_root_pass_other_reason_passes_when_drain_not_stuck():
    passed = grade.stale_scoped_root_pass_number(
        {
            "authority": False,
            "reason": "legacy_window_expired_fresh_admission",
            "blocking_reservations": 0,
            "drainable_reservations": 0,
        },
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
    )
    assert passed["status"] == "pass"
    assert passed["next_break_issue"] is None


def test_stale_scoped_root_pass_records_reservation_drain_as_next_break():
    nxt = grade.stale_scoped_root_pass_number(
        {
            "authority": False,
            "reason": "admission_deadline_expired",
            "blocking_reservations": 11,
            "drainable_reservations": 0,
        },
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
    )
    assert nxt["status"] == "next_break"
    assert nxt["next_break_issue"] == 1049
    assert "blocking 11" in nxt["grade_reason"]
    assert "drainable 0" in nxt["grade_reason"]


def test_stale_scoped_root_pass_unmeasured_without_reason():
    for receipt in (
        None,
        {},
        {"authority": False},
        {"authority": False, "reason": ""},
        {"authority": False, "reason": " "},
        {"authority": False, "reason": None},
        {"authority": False, "reason": 12},
    ):
        row = grade.stale_scoped_root_pass_number(
            receipt, now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS
        )
        assert row["status"] == "unmeasured", receipt


def test_stale_scoped_root_pass_ignores_bool_reservation_counts():
    """Bool must not count as an int reservation measurement."""
    passed = grade.stale_scoped_root_pass_number(
        {
            "authority": False,
            "reason": "other",
            "blocking_reservations": True,
            "drainable_reservations": False,
        },
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
    )
    assert passed["status"] == "pass"
    assert passed["next_break_issue"] is None
