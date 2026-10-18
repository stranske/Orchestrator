"""Isolated persistence and reporting coverage for delegated review rounds."""

from __future__ import annotations

import pytest

import feedback
import switch_review


def test_review_loop_share_is_na_without_first_round_evidence() -> None:
    assert (
        switch_review.review_loop_share_line([])
        == "  review loop first-pass MERGE-WHEN-GREEN: n/a (0 rounds)"
    )


def test_review_rounds_persist_in_isolated_brain_and_report_first_pass_fraction(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "review-rounds.db")

    feedback.record_review_round("run-1", "owner/repo#1", 1, "MERGE-WHEN-GREEN", "claude", "a" * 40)
    feedback.record_review_round("run-1", "owner/repo#1", 2, "FIX-FIRST", "claude")
    feedback.record_review_round("run-2", "owner/repo#2", 1, "FIX-FIRST", "gemini")
    feedback.record_review_round("run-3", "owner/repo#3", 1, "MERGE-WHEN-GREEN", "cursor")
    # A replay cannot rewrite the original first-pass observation or its head.
    feedback.record_review_round("run-1", "owner/repo#1", 1, "FIX-FIRST", "claude", "b" * 40)

    rows = feedback.review_round_rows()

    assert len(rows) == 4
    with feedback._conn() as conn:
        assert (
            conn.execute(
                "SELECT head_sha FROM review_rounds WHERE run_id='run-1' AND round=1"
            ).fetchone()[0]
            == "a" * 40
        )
    assert {(row[0], row[2], row[3], row[5]) for row in rows} == {
        ("run-1", 1, "MERGE-WHEN-GREEN", 1),
        ("run-1", 2, "FIX-FIRST", 0),
        ("run-2", 1, "FIX-FIRST", 1),
        ("run-3", 1, "MERGE-WHEN-GREEN", 1),
    }
    assert (
        switch_review.review_loop_share_line(rows)
        == "  review loop first-pass MERGE-WHEN-GREEN: 2/3 (67%)"
    )
    # Reporting must not enter the writing/migration connection path.
    monkeypatch.setattr(
        feedback, "_conn", lambda: (_ for _ in ()).throw(AssertionError("writing read"))
    )
    assert switch_review.review_round_snapshot() == {"status": "measured", "rows": rows}


def test_trial_reports_first_twenty_deliveries_without_later_dilution():
    rows = [(f"run-{i}", "owner/repo#1", 1, "FIX-FIRST", "vibe", 1) for i in range(20)]
    rows += [("later", "owner/repo#2", 1, "MERGE-WHEN-GREEN", "vibe", 1)] * 5
    assert switch_review.review_loop_share_line(rows).endswith("0/20 (0%)")


def test_missing_round_store_is_unmeasured_and_not_created(tmp_path, monkeypatch):
    path = tmp_path / "absent.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    assert switch_review.review_round_snapshot()["status"] == "unmeasured"
    assert not path.exists()


def test_tied_review_timestamps_keep_first_twenty_recorded_deliveries(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(feedback.time, "time", lambda: 100)
    for i in range(20):
        feedback.record_review_round(f"z-{i:02}", f"o/r#{i}", 1, "FIX-FIRST", "vibe")
    feedback.record_review_round("a-later", "o/r#99", 1, "MERGE-WHEN-GREEN", "vibe")
    assert switch_review.review_loop_share_line(feedback.review_round_rows()).endswith("0/20 (0%)")


@pytest.mark.parametrize("measured", [True, False])
def test_format_report_renders_review_measurement_state(tmp_path, monkeypatch, measured):
    import strategy_experiment

    monkeypatch.setattr(strategy_experiment, "strategy_result_path", lambda: tmp_path / "absent")
    from test_switch_review_strategy_line import MINIMAL_REPORT

    report = dict(MINIMAL_REPORT)
    report["review_loop"] = (
        {"status": "measured", "rows": [("run", "o/r#1", 1, "FIX-FIRST", "vibe", 1)]}
        if measured
        else {"status": "unmeasured", "reason": "SQLiteError"}
    )
    rendered = switch_review.format_report(report)
    assert ("0/1 (0%)" if measured else "UNMEASURED (SQLiteError)") in rendered
