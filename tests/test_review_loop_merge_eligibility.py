import json
import sqlite3

import claims
import feedback
import review_loop


def _db(tmp_path, monkeypatch):
    path = tmp_path / "feedback.db"
    conn = sqlite3.connect(path)
    conn.executescript(feedback.SCHEMA)
    conn.close()
    monkeypatch.setattr(feedback, "DB_PATH", path)
    monkeypatch.setattr(claims, "holder", lambda _: None)
    return path


def _run(path, run_id="root", *, ts=1, target="o/r#671", metadata=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO runs(run_id,ts,target,routing_metadata) VALUES (?,?,?,?)",
        (run_id, ts, target, json.dumps(metadata) if metadata is not None else None),
    )
    conn.commit()
    conn.close()


def _round(
    path,
    run_id="root",
    *,
    verdict="MERGE-WHEN-GREEN",
    head="a" * 40,
    ts=2,
    number=1,
    pr="https://github.com/o/r/pull/9"
):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO review_rounds(run_id,pr,round,verdict,reviewer_agent,first_pass,recorded_ts,head_sha) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, pr, number, verdict, "vibe", 1, ts, head),
    )
    conn.commit()
    conn.close()


def test_unopted_target_is_not_required(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"other": True})
    assert review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",)) == {
        "required": False,
        "eligible": False,
        "reason": "no-review-loop-marker",
    }


def test_pending_opted_run_fails_closed(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"review_loop": True, "review_run_id": "root"})
    out = review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))
    assert (
        out["required"] is True
        and out["eligible"] is False
        and out["reason"] == "review-loop-pending"
    )


def test_exact_head_merge_when_green_is_the_only_pass(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"review_loop": True, "review_run_id": "root"})
    _round(path)
    assert (
        review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))["eligible"]
        is True
    )
    assert (
        review_loop.merge_eligibility("o/r#9", "b" * 40, source_targets=("o/r#671",))["reason"]
        == "review-loop-head-changed"
    )


def test_latest_root_run_and_latest_verdict_win(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, "old", ts=1, metadata={"review_loop": True, "review_run_id": "old"})
    _round(path, "old")
    _run(path, "new", ts=3, metadata={"review_loop": True, "review_run_id": "new"})
    _round(path, "new", verdict="FIX-FIRST", ts=4)
    _round(path, "new", verdict="MERGE-WHEN-GREEN", ts=2, number=0)
    out = review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))
    assert (
        out["run_id"] == "new"
        and out["eligible"] is False
        and out["reason"] == "review-loop-fix-first"
    )


def test_branch_source_issue_is_an_explicit_match(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(
        path,
        target="o/r#671",
        metadata={
            "review_loop": True,
            "review_run_id": "root",
            "branch": "codex/issue-671-review-loop",
        },
    )
    _round(path)
    assert (
        review_loop.merge_eligibility("o/r#9", "a" * 40, head_branch="codex/issue-671-review-loop")[
            "eligible"
        ]
        is True
    )


def test_malformed_metadata_and_unreadable_db_fail_closed(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"review_loop": True, "review_run_id": 2})
    assert (
        review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))["reason"]
        == "malformed-review-loop-metadata"
    )
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite")
    monkeypatch.setattr(feedback, "DB_PATH", bad)
    out = review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))
    assert (
        out["required"] is True
        and out["eligible"] is False
        and out["reason"] == "feedback-db-unreadable"
    )


def test_legacy_brain_without_review_table_or_metadata_stays_unopted(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata=None)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE review_rounds")
    result = review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))
    assert result["required"] is False


def test_review_of_a_different_pr_never_authorizes_same_source_or_head(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"review_loop": True, "review_run_id": "root"})
    _round(path, pr="https://github.com/o/r/pull/10")
    assert not review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))[
        "eligible"
    ]


def test_new_claim_invalidates_a_previously_approved_review(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"review_loop": True, "review_run_id": "root"})
    _round(path)
    monkeypatch.setattr(
        claims, "holder", lambda key: {"agent": "incumbent"} if key == "o/r#671" else None
    )
    result = review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))
    assert result["reason"] == "review-loop-writer-held" and not result["eligible"]


def test_higher_round_refusal_wins_even_when_clock_moves_backwards(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    _run(path, metadata={"review_loop": True, "review_run_id": "root"})
    _round(path, number=1, ts=100)
    _round(path, number=2, ts=50, verdict="FIX-FIRST")
    result = review_loop.merge_eligibility("o/r#9", "a" * 40, source_targets=("o/r#671",))
    assert result["reason"] == "review-loop-fix-first" and not result["eligible"]
