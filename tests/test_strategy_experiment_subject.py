"""Subject strategy experiments freeze a live issue before preparing arm evidence."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

import exp_abcd
import feedback
import strategy_experiment
import synthesis_promotion


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def subject_harness(tmp_path, monkeypatch):
    """Keep real preparation, Git artifacts, evaluator writes, and promotion state local."""
    canon = tmp_path / "repo"
    canon.mkdir()
    git(canon, "init", "-b", "main")
    git(canon, "config", "user.name", "Subject Test")
    git(canon, "config", "user.email", "subject@example.invalid")
    (canon / "value.txt").write_text("base\n")
    git(canon, "add", "value.txt")
    git(canon, "commit", "-m", "base")
    git(canon, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda repo: canon)
    monkeypatch.setattr(exp_abcd.provision, "base_branch", lambda repo: "main")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(strategy_experiment.claims, "holder", lambda target: {})
    monkeypatch.setattr(strategy_experiment.claims, "update_metadata", lambda *a, **kw: True)
    spawned = []

    def spawn(agent, mode, prompt, wt, log, **kwargs):
        # Agent processes are the boundary: the rest of the experiment uses real Git and SQLite.
        run_id = kwargs["run_id"]
        reviewing = kwargs["task_type"] == "review"
        if reviewing:
            assert "implementation" in (wt / "value.txt").read_text()
        value = "corrected" if reviewing else "implementation"
        (wt / "value.txt").write_text(f"{value}: {run_id}\n")
        git(wt, "commit", "-am", value)
        log.write_text("finished\n")
        os.utime(log, (time.time() - 3600,) * 2)
        done = log.parent / "done"
        done.mkdir(exist_ok=True)
        (done / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "rc_of": "agent", "rc": 0})
        )
        feedback.record_cost(run_id, cost_usd=0.25, source="test-fixture")
        spawned.append({"agent": agent, "worktree": wt, "prompt": prompt, **kwargs})
        return None

    monkeypatch.setattr(exp_abcd, "_spawn", spawn)
    return spawned


def test_subject_path_freezes_the_spec_and_prepares_two_strategy_arms_with_three_instances(
    subject_harness,
) -> None:
    claimed = []
    prepared = []
    subject = {
        "target": "o/r#44",
        "repo": "o/r",
        "body": "## Frozen\n\nThe exact live issue body.",
        "shape": "tests|verify:compare|tests",
        "broke_later_rate": 0.33,
    }

    def claim(target, agent):
        claimed.append((target, agent))
        return True

    def prepare(repo, spec_file, exp_id, arms):
        assert claimed == [(subject["target"], "research")]
        assert subject["body"] == Path(spec_file).read_text()
        prepared.extend(arms)
        return exp_abcd.prepare_arms(repo, spec_file, exp_id, arms)

    out = strategy_experiment.prepare_subject_experiment(
        subject,
        exp_id="subject-test",
        agent="codex",
        reviewer="claude",
        exp_dir=exp_abcd.EXP_DIR,
        claim_fn=claim,
        prepare_fn=prepare,
    )

    assert claimed == [("o/r#44", "research")]
    edir = exp_abcd.exp_paths("subject-test")
    assert (edir / "spec.md").read_text() == subject["body"]
    assert len(prepared) == 6
    assert [arm["strategy"] for arm in prepared].count("single") == 3
    assert [arm["strategy"] for arm in prepared].count("pair") == 3
    pairs = [arm for arm in prepared if arm["strategy"] == "pair"]
    assert all(pair["members"][1]["role"] == "review" for pair in pairs)
    assert all(
        pair["members"][1]["review_of_member_id"] == pair["members"][0]["member_id"]
        for pair in pairs
    )
    assert json.loads((edir / "strategy.json").read_text())["subject"]["shape"] == subject["shape"]
    meta = json.loads((edir / "meta.json").read_text())
    assert meta["subject"]["target"] == subject["target"]
    assert meta["subject"]["shape"] == subject["shape"]
    assert meta["frozen_issue_body"] == str(edir / "spec.md")
    assert len(meta["arms"]) == 6 and len(meta["members"]) == 9
    assert len(subject_harness) == 6  # Reviewers wait for followup.
    assert all(subject["body"] in row["prompt"] for row in subject_harness)
    for key in ("member_id", "run_id", "worktree"):
        values = {str(row[key]) for row in out["prepared"]["launched"] if row.get(key)}
        assert len(values) == (9 if key == "member_id" else 6)
    assert out["prepared"]["exp_id"] == "subject-test"


@pytest.mark.parametrize("claim_failure", [False, True])
def test_prepared_subject_followup_evaluates_six_final_candidates_and_enters_promotion(
    tmp_path, subject_harness, monkeypatch, claim_failure
):
    subject = {
        "target": "o/r#44",
        "repo": "o/r",
        "body": "## Acceptance Criteria\nImplement the corrected value and test it.",
        "shape": "tests|verify:compare|tests",
        "broke_later_rate": 0.33,
    }
    strategy_experiment.prepare_subject_experiment(
        subject,
        exp_id="subject-followup",
        agent="codex",
        reviewer="claude",
        claim_fn=lambda target, agent: True,
    )
    edir = exp_abcd.exp_paths("subject-followup")
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")
    monkeypatch.setenv("ORCH_FOLLOWUP_SHIP_GATE", "1")
    monkeypatch.setenv("ORCH_FOLLOWUP_EVALUATORS", "vibe")
    monkeypatch.setenv("ORCH_OBJECTIVE_ANCHOR", "0")
    monkeypatch.setattr(exp_abcd.capabilities, "production_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(exp_abcd, "_record_execution_start", lambda *a, **kw: 1)
    monkeypatch.setattr(exp_abcd, "_record_execution_complete", lambda *a, **kw: None)
    judge = tmp_path / "judge-output.json"
    judge.write_text(json.dumps({"scores": {letter: 7 for letter in "ABCDEF"}}))
    judgments = []

    def judge_command(agent, prompt_path):
        assert subject["body"] in Path(prompt_path).read_text()
        judgments.append((agent, prompt_path))
        return shlex.join(["cat", str(judge)])

    monkeypatch.setattr(exp_abcd, "_eval_command", judge_command)
    syntheses = []

    def synthesize(repo, exp_id):
        syntheses.append((repo, exp_id))
        return {
            "pid": 12345,
            "run_id": f"{exp_id}:synth",
            "worktree": str(subject_harness[0]["worktree"]),
        }

    def followup():
        return exp_abcd.followup(
            synthesize_fn=synthesize,
            subject_lifecycle_fn=lambda *a, **kw: None,
            promotion_completion_fn=lambda state: {"status": "pending"},
        )

    if claim_failure:

        def fail_claim(*args, **kwargs):
            raise OSError("claim store unavailable after launch")

        monkeypatch.setattr(strategy_experiment.claims, "update_metadata", fail_claim)
    first = followup()
    assert first["processed"] == [] and first["skipped"][0]["reason"] == "pair-review-pending"
    assert len(subject_harness) == 9 and not judgments and not syntheses
    second = followup()
    assert second["processed"][0]["evaluated"] is True
    assert second["processed"][0]["diffs"] == 6
    assert judgments == [("vibe", str(edir / "eval-prompt-vibe.txt"))]
    assert syntheses == [(subject["repo"], edir.name)]
    metadata = json.loads((edir / "strategy.json").read_text())
    final_ids = {arm["final_artifact_id"] for arm in metadata["strategy_arms"]}
    maps = json.loads((edir / "eval-maps.json").read_text())
    assert set(maps["vibe"].values()) == final_ids
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT implementer_member_id,evaluator_id,score FROM evaluations_v2 WHERE experiment_id=?",
            (edir.name,),
        ).fetchall()
    assert {(row[0], row[1], row[2]) for row in rows} == {(mid, "vibe", 7) for mid in final_ids}
    receipt = json.loads(strategy_experiment.strategy_result_path().read_text())
    assert receipt["comparison"]["single"]["instances"] == 3
    assert receipt["comparison"]["pair"]["instances"] == 3
    assert receipt["comparison"]["single"]["cost_usd"] == 0.75
    assert receipt["comparison"]["pair"]["cost_usd"] == 1.5
    promotion = synthesis_promotion.load_state(edir)
    assert promotion["delivery_phase"] == "synth_running"
    assert promotion["lineage"]["evaluator_ids"] == ["vibe"]
    assert promotion["publication"]["direct_publication_allowed"] is False
    assert followup()["processed"] == []
    assert len(judgments) == len(syntheses) == 1


def test_prepare_refuses_without_the_confirm_flag(tmp_path, monkeypatch, capsys) -> None:
    shapes = tmp_path / "fleet-shapes.json"
    shapes.write_text(json.dumps({"shapes": []}))
    monkeypatch.setenv("ORCH_STRATEGY_EXPERIMENT", "1")
    assert (
        strategy_experiment.main(
            [
                "--subject",
                "o/r#44",
                "--exp-id",
                "guard-test",
                "--agent",
                "codex",
                "--reviewer",
                "claude",
                "--prepare",
                "--shapes-path",
                str(shapes),
            ]
        )
        == 2
    )
    assert (
        "requires --prepare --confirm-strategy and ORCH_STRATEGY_EXPERIMENT=1"
        in capsys.readouterr().err
    )
