"""Subject strategy experiments freeze a live issue before preparing arm evidence."""

from __future__ import annotations

import json

import strategy_experiment


def test_subject_path_freezes_the_spec_and_prepares_two_strategy_arms_with_three_instances(
    tmp_path,
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
        prepared.extend(arms)
        return {"repo": repo, "exp_id": exp_id, "launched": []}

    out = strategy_experiment.prepare_subject_experiment(
        subject,
        exp_id="subject-test",
        agent="codex",
        reviewer="claude",
        exp_dir=tmp_path,
        claim_fn=claim,
        prepare_fn=prepare,
    )

    assert claimed == [("o/r#44", "research")]
    assert (tmp_path / "subject-test/spec.md").read_text() == subject["body"]
    assert len(prepared) == 6
    assert [arm["strategy"] for arm in prepared].count("single") == 3
    assert [arm["strategy"] for arm in prepared].count("pair") == 3
    pairs = [arm for arm in prepared if arm["strategy"] == "pair"]
    assert all(pair["members"][1]["role"] == "review" for pair in pairs)
    assert all(
        pair["members"][1]["review_of_member_id"] == pair["members"][0]["member_id"]
        for pair in pairs
    )
    assert (
        json.loads((tmp_path / "subject-test/strategy.json").read_text())["subject"]["shape"]
        == subject["shape"]
    )
    assert out["prepared"]["exp_id"] == "subject-test"


def test_prepare_refuses_without_the_confirm_flag(tmp_path, monkeypatch, capsys) -> None:
    shapes = tmp_path / "fleet-shapes.json"
    shapes.write_text(json.dumps({"shapes": []}))
    monkeypatch.delenv("ORCH_STRATEGY_EXPERIMENT", raising=False)
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
