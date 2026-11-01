"""#1050 owner-side pass number grades coordinator receipts without live provenance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import closer_stale_scoped_root_pass as grade

MERGED_AT = 1793621234.0
CHECK_AT = MERGED_AT + 24 * 60 * 60
CHECK_DATE = "2026-11-03T12:07:14+00:00"

MODULE = Path(__file__).resolve().parents[1] / "src" / "closer_stale_scoped_root_pass.py"


def test_missing_merge_timestamp_stays_pending_even_after_old_calendar_deadline():
    receipt = {"authority": True, "reason": "ok"}
    for result in (
        grade.stale_scoped_root_pass_number(receipt, now=CHECK_AT + 86400),
        grade.grade_receipts([receipt, receipt], now=CHECK_AT + 86400),
        grade.grade_receipts([], now=CHECK_AT + 86400),
    ):
        assert result["status"] == "pending"
        assert result["check_date"] is None
        assert result["check_at"] is None


def test_merge_time_sets_exact_twenty_four_hour_boundary():
    # A delayed merge must move the check, including its time of day.
    merged_at = MERGED_AT + 7 * 86400
    check_at = merged_at + 86400
    receipt = {"authority": True, "reason": "ok"}
    before = grade.stale_scoped_root_pass_number(receipt, now=check_at - 0.25, merged_at=merged_at)
    at = grade.stale_scoped_root_pass_number(receipt, now=check_at, merged_at=merged_at)
    assert before["status"] == "pending"
    assert at["status"] == "pass"
    assert at["check_at"] == check_at
    assert at["merged_at"] == merged_at
    assert at["check_date"] == "2026-11-10T12:07:14+00:00"


def test_nonfinite_timestamps_cannot_bypass_check_gate():
    for merged_at in (True, -1, float("nan"), float("inf"), -float("inf"), 1e300):
        try:
            grade.stale_scoped_root_pass_number(
                {"authority": True}, now=CHECK_AT, merged_at=merged_at
            )
        except ValueError as exc:
            assert "merged_at" in str(exc)
        else:
            raise AssertionError(f"invalid merge timestamp accepted: {merged_at}")
    for now in (float("nan"), float("inf"), -float("inf")):
        try:
            grade.grade_receipts([{"authority": True}], now=now, merged_at=MERGED_AT)
        except ValueError as exc:
            assert "now" in str(exc)
        else:
            raise AssertionError(f"invalid current timestamp accepted: {now}")


def test_cli_pending_without_merge_and_refuses_invalid_timestamp(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"authority": True}), encoding="utf-8")
    for extra, expected_status, expected_code in (
        ([], "pending", 0),
        (["--merged-at", "nan"], "refused", 78),
        (["--merged-at", "inf"], "refused", 78),
        (["--merged-at", "1e300"], "refused", 78),
        (["--merged-at", str(MERGED_AT), "--now", "nan"], "refused", 78),
    ):
        result = subprocess.run(
            [sys.executable, str(MODULE), str(path), "--now", str(CHECK_AT), *extra],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected_code, result.stderr
        assert json.loads(result.stdout)["status"] == expected_status


def test_stale_scoped_root_pass_pending_before_check_date():
    pending = grade.stale_scoped_root_pass_number(
        {"authority": True, "reason": "ok"},
        now=CHECK_AT - 1,
        merged_at=MERGED_AT,
    )
    assert pending["status"] == "pending"
    assert pending["check_date"] == CHECK_DATE
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
        now=CHECK_AT,
        merged_at=MERGED_AT,
    )
    assert passed["status"] == "pass"
    assert passed["grade_reason"] == "authority true"
    assert passed["next_break_issue"] is None


def test_stale_scoped_root_pass_fails_while_prior_stop_latched():
    failed = grade.stale_scoped_root_pass_number(
        {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
        now=CHECK_AT,
        merged_at=MERGED_AT,
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
        now=CHECK_AT,
        merged_at=MERGED_AT,
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
        now=CHECK_AT,
        merged_at=MERGED_AT,
    )
    assert nxt["status"] == "next_break"
    assert nxt["next_break_issue"] == 1049
    assert "blocking 11" in nxt["grade_reason"]
    assert "drainable 0" in nxt["grade_reason"]


def test_changed_reason_with_failed_reservation_diagnostics_is_unmeasured():
    receipt = {
        "authority": False,
        "reason": "admission_deadline_expired",
        "reservation_diagnostics_error": "OSError: reservation state unavailable",
    }
    result = grade.stale_scoped_root_pass_number(receipt, now=CHECK_AT, merged_at=MERGED_AT)
    assert result["status"] == "unmeasured"
    assert result["grade_reason"] == "reservation drain diagnostics unavailable"
    assert result["reservation_diagnostics_error"] == receipt["reservation_diagnostics_error"]
    assert result["next_break_issue"] is None


def test_diagnostic_failure_does_not_override_explicit_authority_or_prior_stop_latch():
    for authority, expected in ((True, "pass"), (False, "fail")):
        result = grade.stale_scoped_root_pass_number(
            {
                "authority": authority,
                "reason": grade.PRIOR_STOP_LATCH_REASON,
                "reservation_diagnostics_error": "OSError: reservation state unavailable",
            },
            now=CHECK_AT,
            merged_at=MERGED_AT,
        )
        assert result["status"] == expected


def test_cli_diagnostic_failure_cannot_supply_a_passing_receipt(tmp_path):
    path = tmp_path / "diagnostics-unavailable.json"
    path.write_text(
        json.dumps(
            {
                "authority": False,
                "reason": "admission_deadline_expired",
                "reservation_diagnostics_error": "OSError: reservation state unavailable",
            }
        ),
        encoding="utf-8",
    )
    # Both the single-receipt path and batch ranking must preserve the missing evidence.
    (tmp_path / "unread.json").write_text("{}", encoding="utf-8")
    for source in (path, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE),
                str(source),
                "--now",
                str(CHECK_AT),
                "--merged-at",
                str(MERGED_AT),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 78, result.stdout
        assert json.loads(result.stdout)["status"] == "unmeasured"


def test_partial_reservation_drain_remains_the_next_break():
    for drainable in (1, 5, 10):
        result = grade.stale_scoped_root_pass_number(
            {
                "authority": False,
                "reason": "admission_deadline_expired",
                "blocking_reservations": 11,
                "drainable_reservations": drainable,
            },
            now=CHECK_AT,
            merged_at=MERGED_AT,
        )
        assert result["status"] == "next_break", result
        assert result["next_break_issue"] == 1049
        assert f"blocking 11 with drainable {drainable}" in result["grade_reason"]


def test_fully_drainable_reservations_allow_stop_latch_clearance():
    for blocking, drainable in ((0, 0), (11, 11)):
        result = grade.stale_scoped_root_pass_number(
            {
                "authority": False,
                "reason": "legacy_window_expired_fresh_admission",
                "blocking_reservations": blocking,
                "drainable_reservations": drainable,
            },
            now=CHECK_AT,
            merged_at=MERGED_AT,
        )
        assert result["status"] == "pass", result
        assert result["next_break_issue"] is None


def test_cli_partial_drain_does_not_claim_a_passing_receipt_set(tmp_path):
    receipts = (
        {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
        {
            "authority": False,
            "reason": "admission_deadline_expired",
            "blocking_reservations": 11,
            "drainable_reservations": 5,
        },
    )
    for index, receipt in enumerate(receipts):
        (tmp_path / f"receipt-{index}.json").write_text(json.dumps(receipt), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE),
            str(tmp_path),
            "--now",
            str(CHECK_AT),
            "--merged-at",
            str(MERGED_AT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "next_break"
    assert payload["next_break_issue"] == 1049
    assert payload["receipt_count"] == 2
    assert payload["winning_source"].endswith("receipt-1.json")


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
        row = grade.stale_scoped_root_pass_number(receipt, now=CHECK_AT, merged_at=MERGED_AT)
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
        now=CHECK_AT,
        merged_at=MERGED_AT,
    )
    assert passed["status"] == "pass"
    assert passed["next_break_issue"] is None


def test_grade_receipts_at_least_one_pass_wins_over_older_failures():
    aggregate = grade.grade_receipts(
        [
            {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
            {"authority": True, "reason": grade.PRIOR_STOP_LATCH_REASON},
        ],
        now=CHECK_AT,
        merged_at=MERGED_AT,
        sources=["old.json", "new.json"],
    )
    assert aggregate["status"] == "pass"
    assert aggregate["scope"] == "receipt_set_at_least_one"
    assert aggregate["receipt_count"] == 2
    assert aggregate["winning_source"] == "new.json"
    assert "at least one receipt passed" in aggregate["grade_reason"]


def test_grade_receipts_next_break_beats_still_latched():
    aggregate = grade.grade_receipts(
        [
            {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
            {
                "authority": False,
                "reason": "admission_deadline_expired",
                "blocking_reservations": 11,
                "drainable_reservations": 0,
            },
        ],
        now=CHECK_AT,
        merged_at=MERGED_AT,
        sources=["latched.json", "drain.json"],
    )
    assert aggregate["status"] == "next_break"
    assert aggregate["next_break_issue"] == 1049
    assert aggregate["winning_source"] == "drain.json"


def test_grade_receipts_all_latched_fails():
    aggregate = grade.grade_receipts(
        [
            {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
            {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
        ],
        now=CHECK_AT,
        merged_at=MERGED_AT,
    )
    assert aggregate["status"] == "fail"
    assert "all 2 receipt(s) still latched" in aggregate["grade_reason"]


def test_grade_receipts_does_not_report_unread_receipts_as_latched():
    latched = {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON}
    for unread in (None, {}, {"authority": False, "reason": " "}):
        for receipts in ([latched, unread], [unread, latched]):
            aggregate = grade.grade_receipts(receipts, now=CHECK_AT, merged_at=MERGED_AT)
            assert aggregate["status"] == "fail"
            assert aggregate["grade_reason"] == (
                f"1 of 2 receipt(s) still latched on {grade.PRIOR_STOP_LATCH_REASON}; "
                "1 receipt(s) unmeasured"
            )
            assert sorted(row["grade"]["status"] for row in aggregate["grades"]) == [
                "fail",
                "unmeasured",
            ]


def test_cli_mixed_latched_and_unread_receipts_reports_measured_counts(tmp_path):
    receipts = (
        {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
        {},
        {"authority": False, "reason": None},
    )
    for index, receipt in enumerate(receipts):
        (tmp_path / f"receipt-{index}.json").write_text(json.dumps(receipt), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE),
            str(tmp_path),
            "--now",
            str(CHECK_AT),
            "--merged-at",
            str(MERGED_AT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["receipt_count"] == 3
    assert payload["grade_reason"] == (
        f"1 of 3 receipt(s) still latched on {grade.PRIOR_STOP_LATCH_REASON}; "
        "2 receipt(s) unmeasured"
    )


def test_grade_receipts_empty_set_unmeasured_after_check_date():
    empty = grade.grade_receipts([], now=CHECK_AT, merged_at=MERGED_AT)
    assert empty["status"] == "unmeasured"
    assert empty["receipt_count"] == 0


def test_grade_receipts_uses_one_check_time_when_batch_crosses_deadline():
    receipts = [
        {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
        {"authority": True, "reason": "ok"},
    ]
    for check_time, expected_status in ((CHECK_AT - 0.25, "pending"), (CHECK_AT, "pass")):
        with patch("time.time", side_effect=[check_time, CHECK_AT + 0.25]) as clock:
            result = grade.grade_receipts(receipts, merged_at=MERGED_AT)
        assert result["status"] == expected_status
        assert clock.call_count == 1
        assert all(
            (item["grade"]["status"] == "pending") == (expected_status == "pending")
            for item in result["grades"]
        )


def test_grade_receipts_explicit_check_time_does_not_read_clock():
    with patch("time.time", side_effect=AssertionError("explicit now must be used")):
        result = grade.grade_receipts(
            [{"authority": True}, {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON}],
            now=CHECK_AT,
            merged_at=MERGED_AT,
        )
    assert result["status"] == "pass"


def test_cli_grades_directory_with_at_least_one_pass(tmp_path):
    older = tmp_path / "older.json"
    newer = tmp_path / "newer.json"
    older.write_text(
        json.dumps({"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON}),
        encoding="utf-8",
    )
    newer.write_text(json.dumps({"authority": True, "reason": "ok"}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE),
            str(tmp_path),
            "--now",
            str(CHECK_AT),
            "--merged-at",
            str(MERGED_AT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["receipt_count"] == 2
    assert payload["winning_source"].endswith("newer.json")


def test_cli_single_receipt_fail_exits_one(tmp_path):
    path = tmp_path / "latched.json"
    path.write_text(
        json.dumps({"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE),
            str(path),
            "--now",
            str(CHECK_AT),
            "--merged-at",
            str(MERGED_AT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["source"].endswith("latched.json")
