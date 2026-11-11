"""Hermetic unit tests for judge_reliability score bounds, human anchors, and CLI reporting."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path

import pytest

import capabilities
import feedback
import judge_reliability as jr


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    db_path = runtime / "feedback.db"

    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(db_path))

    monkeypatch.setattr(feedback, "DB_PATH", db_path)
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)

    attempted: list[tuple] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("judge_reliability tests must not spawn processes or sockets")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield runtime
    assert attempted == [], f"unexpected external calls: {attempted}"


def test_score_rejects_non_numeric_and_out_of_range_bounds() -> None:
    assert jr._score(0) == 0.0
    assert jr._score(str(feedback.QUALITY_MAX)) == float(feedback.QUALITY_MAX)
    assert jr._score(-0.01) is None
    assert jr._score(feedback.QUALITY_MAX + 0.01) is None
    assert jr._score("not-a-score") is None
    assert jr._score(None) is None


def test_human_anchor_rows_supports_dict_inputs_and_ignores_malformed_verdicts() -> None:
    rows = [
        {"ref": "", "human_verdict": "8.0"},
        {"ref": "exp1:impl_a", "human_verdict": None},
        ("ts-only",),
        {"ref": "exp1:impl_a", "human_verdict": "{invalid json"},
        {"ref": "exp1:impl_a", "human_verdict": "free-form note without a numeric score"},
        {"ref": "exp_missing_colon", "human_verdict": "8.5"},
        {
            "ref": "exp1",
            "human_verdict": {
                "experiment_id": "exp1",
                "implementer": "impl_a",
                "score": 8.5,
                "scores": {"impl_b": 4.25, "impl_bad": feedback.QUALITY_MAX + 5},
            },
        },
        {"ref": "exp2:impl_c", "human_verdict": "7.5"},
    ]
    anchors = jr._human_anchor_rows(rows)
    assert dict(anchors) == {
        ("exp1", "impl_a"): [8.5],
        ("exp1", "impl_b"): [4.25],
        ("exp2", "impl_c"): [7.5],
    }


def test_compute_skips_incomplete_or_out_of_range_evaluation_rows() -> None:
    eval_rows = [
        {"experiment_id": "", "implementer": "impl_a", "evaluator": "ghost", "score": 8.0},
        {"experiment_id": "exp1", "implementer": None, "evaluator": "ghost", "score": 8.0},
        {"experiment_id": "exp1", "implementer": "impl_a", "evaluator": "", "score": 8.0},
        {"experiment_id": "exp1", "implementer": "impl_a", "evaluator": "ghost", "score": -1.0},
        ("exp1", "impl_a"),
        ("exp1", "impl_a", "judge_a", 9.0),
        ("exp1", "impl_a", "judge_b", 8.0),
        ("exp2", "impl_a", "judge_a", 7.0),
        ("exp2", "impl_a", "judge_b", 7.0),
    ]
    report = jr.compute(eval_rows, min_comparisons=2, min_experiments=2, generated_at=1234)
    assert set(report["judges"]) == {"judge_a", "judge_b"}
    assert report["ready"] is True
    assert report["ready_judge_count"] == 2
    assert report["judges"]["judge_a"]["comparisons"] == 2


def test_main_formats_json_and_text_summaries_from_feedback_store(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback, "model_epoch_floor", lambda _agent, default=0: default)
    now = int(time.time())
    with feedback._conn() as conn:
        for exp, score_a, score_b in (("exp-1", 9.0, 9.0), ("exp-2", 4.0, 4.5)):
            conn.execute(
                "INSERT INTO evaluations (experiment_id, implementer, evaluator, score, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (exp, "impl_a", "judge_ready_a", score_a, now),
            )
            conn.execute(
                "INSERT INTO evaluations (experiment_id, implementer, evaluator, score, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (exp, "impl_a", "judge_ready_b", score_b, now),
            )
        conn.execute(
            "INSERT INTO evaluations (experiment_id, implementer, evaluator, score, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("exp-solo", "impl_solo", "judge_sparse", 8.0, now),
        )
        conn.execute(
            "INSERT INTO human_calibration (ts, ref, human_verdict, note) VALUES (?, ?, ?, ?)",
            (now, "exp-1:impl_a", "9.0", "anchor"),
        )

    assert (
        jr.main(
            [
                "--json",
                "--window-days",
                "30",
                "--min-comparisons",
                "2",
                "--min-experiments",
                "2",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["window_days"] == 30
    assert payload["judge_count"] == 3
    assert payload["ready_judge_count"] == 2
    assert payload["judges"]["judge_ready_a"]["ready"] is True
    assert payload["judges"]["judge_sparse"]["ready"] is False
    assert payload["judges"]["judge_sparse"]["weight"] == 1.0

    assert (
        jr.main(
            [
                "--window-days",
                "30",
                "--min-comparisons",
                "2",
                "--min-experiments",
                "2",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "judge_reliability: judges=3 ready=2 window_days=30" in text
    assert "  judge_ready_a: ready weight=" in text
    assert "  judge_sparse: not-ready weight=1.000 comparisons=0 mae=None" in text
