"""#1050 owner-side pass number grades coordinator receipts without live provenance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import closer_stale_scoped_root_pass as grade

MODULE = Path(__file__).resolve().parents[1] / "src" / "closer_stale_scoped_root_pass.py"


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


def test_grade_receipts_at_least_one_pass_wins_over_older_failures():
    aggregate = grade.grade_receipts(
        [
            {"authority": False, "reason": grade.PRIOR_STOP_LATCH_REASON},
            {"authority": True, "reason": grade.PRIOR_STOP_LATCH_REASON},
        ],
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
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
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
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
        now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS,
    )
    assert aggregate["status"] == "fail"
    assert "all 2 receipt(s) still latched" in aggregate["grade_reason"]


def test_grade_receipts_empty_set_unmeasured_after_check_date():
    empty = grade.grade_receipts([], now=grade.STALE_SCOPED_ROOT_PASS_CHECK_TS)
    assert empty["status"] == "unmeasured"
    assert empty["receipt_count"] == 0


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
            str(grade.STALE_SCOPED_ROOT_PASS_CHECK_TS),
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
            str(grade.STALE_SCOPED_ROOT_PASS_CHECK_TS),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["source"].endswith("latched.json")
