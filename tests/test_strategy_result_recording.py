"""Only complete exact candidate scores and measured costs form a comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import exp_abcd
import feedback
import strategy_experiment


@pytest.fixture
def measured(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    exp_id = "measured-strategy"
    plan = strategy_experiment.build_strategy_plan(
        "o/r", "unused-spec", exp_id, strategy_experiment.subject_arms("cursor", "vibe")
    )
    edir = exp_abcd.exp_paths(exp_id)
    edir.mkdir(parents=True)
    (edir / "strategy.json").write_text(json.dumps(strategy_experiment.strategy_metadata(plan)))
    with feedback._conn() as conn:
        for arm in plan["arms"]:
            for run_id in arm["attempt_run_ids"]:
                conn.execute("INSERT INTO costs (run_id,cost_usd) VALUES (?,?)", (run_id, 0.25))
            conn.commit()
            for judge in ("j1", "j2"):
                feedback.record_evaluation_v2(
                    experiment_id=exp_id,
                    implementer_arm_id=arm["arm_id"],
                    implementer_member_id=arm["final_artifact_id"],
                    implementation_agent="cursor",
                    evaluator_id=judge,
                    evaluator_agent="vibe",
                    score=8 if arm["strategy"] == "pair" else 6,
                )
    return exp_id, plan, edir


def test_scored_result_compares_three_instances_and_all_pair_attempt_costs(measured):
    exp_id, _plan, _edir = measured
    result = json.loads(
        strategy_experiment.write_evaluation_result(exp_id, ["j1", "j2"]).read_text()
    )
    assert result["status"] == "completed"
    assert result["comparison"]["single"] == {
        **result["comparison"]["single"],
        "instances": 3,
        "score_mean": 6,
        "cost_usd": 0.75,
    }
    assert result["comparison"]["pair"] == {
        **result["comparison"]["pair"],
        "instances": 3,
        "score_mean": 8,
        "cost_usd": 1.5,
    }


@pytest.mark.parametrize("missing", ["judge", "cost"])
def test_partial_evaluation_or_missing_cost_stays_unknown(measured, missing):
    exp_id, plan, _edir = measured
    arm = plan["arms"][-1]
    with feedback._conn() as conn:
        if missing == "judge":
            conn.execute(
                "DELETE FROM evaluations_v2 WHERE experiment_id=? AND implementer_member_id=? AND evaluator_id=?",
                (exp_id, arm["final_artifact_id"], "j2"),
            )
        else:
            conn.execute(
                "UPDATE costs SET cost_usd=NULL WHERE run_id=?", (arm["attempt_run_ids"][-1],)
            )
    result = json.loads(
        strategy_experiment.write_evaluation_result(exp_id, ["j1", "j2"]).read_text()
    )
    assert result["status"] == "UNKNOWN"
    assert result["scores"] is None and result["comparison"] is None
    if missing == "cost":
        assert result["costs"][arm["arm_id"]]["cost_usd"] is None


def test_direct_evaluation_records_final_pair_candidates_and_state(measured, monkeypatch):
    exp_id, plan, edir = measured
    arms, members = exp_abcd._normalize_arm_members(plan["arms"])
    (edir / "meta.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repo": "o/r",
                "base": "main",
                "exp_id": exp_id,
                "arms": arms,
                "members": members,
            }
        )
    )
    spec = edir / "spec.md"
    spec.write_text("## Acceptance Criteria\nDeliver the requested behavior.")
    for member in members:
        (edir / exp_abcd.exp_diff_path(member["agent"], member["member_id"])).write_text(
            "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+corrected\n"
        )
    monkeypatch.setenv("ORCH_OBJECTIVE_ANCHOR", "0")
    monkeypatch.setattr(exp_abcd, "_record_execution_start", lambda *args, **kwargs: 1)
    monkeypatch.setattr(exp_abcd, "_record_execution_complete", lambda *args, **kwargs: None)
    monkeypatch.setattr(exp_abcd, "_eval_command", lambda *args: "fake-evaluation")

    class Judge:
        def __init__(self, argv, *, stdout, **kwargs):
            stdout.write(json.dumps({"scores": {letter: 7 for letter in "ABCDEF"}}))
            stdout.flush()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(exp_abcd.subprocess, "Popen", Judge)
    evaluated = exp_abcd.evaluate(
        "o/r", str(spec), exp_id, [{"agent": "vibe", "evaluator_id": "j1"}]
    )
    assert set(evaluated["implementers"]) == {arm["final_artifact_id"] for arm in plan["arms"]}
    assert len(evaluated["maps"]["j1"]) == 6
    result = json.loads(Path(evaluated["strategy_result"]).read_text())
    assert result["status"] == "completed"
    assert result["comparison"]["pair"]["score_mean"] == 7
