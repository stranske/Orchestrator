"""Hermetic coverage for exp_abcd's pure metadata and evidence helpers."""

from __future__ import annotations

import socket
import subprocess

import pytest

import exp_abcd


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("exp_abcd coverage test attempted an external process or network connection")

    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


def test_member_identity_metadata_and_paths_reject_ambiguous_identifiers(monkeypatch, tmp_path):
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    assert exp_abcd.member_identity("arm / one", "codex!", 1) == "arm-one--member-02-codex"
    assert exp_abcd.exp_branch("trial one", "codex", "member two") == "exp/trial-one-member-two"
    assert exp_abcd.exp_diff_path("codex", "member two") == "diff-member-two.patch"
    assert exp_abcd.exp_log_path("codex") == "codex.log"
    assert exp_abcd.exp_paths("trial") == tmp_path / "experiments" / "trial"
    with pytest.raises(ValueError, match="identity cannot be empty"):
        exp_abcd.member_identity("***", "codex", 0)

    members = exp_abcd.experiment_members(
        {
            "members": [
                {
                    "arm_id": "arm-a",
                    "member_id": "member-a",
                    "agent": "codex",
                    "profile_id": "p1",
                    "role": "review",
                    "review_of_member_id": "member-0",
                }
            ]
        }
    )
    assert members[0]["legacy"] is False
    assert members[0]["role"] == "review"
    assert exp_abcd._member_run_id("trial", members[0]) == "trial:member:member-a"
    assert exp_abcd._member_routing_metadata(members[0])["experiment_identity_version"] == 2
    assert exp_abcd.experiment_members({"agents": ["codex"]})[0]["legacy"] is True
    with pytest.raises(ValueError, match="duplicate"):
        exp_abcd.experiment_members(
            {"members": [{"arm_id": "a", "member_id": "m", "agent": "codex"}] * 2}
        )


def test_prompts_and_evidence_parsers_preserve_contracts(monkeypatch):
    monkeypatch.setattr(
        exp_abcd.feedback,
        "active_evidence_types",
        lambda: [{"name": "ci", "rationale": "green run"}],
    )
    monkeypatch.setattr(
        "capability_compiler.evaluator_prompt_fragment", lambda plan: f"plan={plan['id']}"
    )
    prompt = exp_abcd.evaluate_prompt(
        "spec", {"A": "diff"}, evidence_contract_plan={"id": "shadow-1"}
    )
    assert "CANDIDATE A" in prompt
    assert "- ci - green run" in prompt
    assert "plan=shadow-1" in prompt
    assert "DO NOT push" in exp_abcd.implement_prompt("spec")
    assert exp_abcd._extract_json('noise {"scores": {"A": 1}} tail') == {"scores": {"A": 1}}
    assert exp_abcd._extract_json('{"other": 1}') is None
    assert exp_abcd._extract_json('{"scores": ') is None
    assert exp_abcd._extract_evidence_gaps(
        {"evidence_gaps": [" CI  gap ", "ci gap", {"missing": "log"}]}
    ) == [
        "CI gap",
        "log",
    ]
    assert exp_abcd._extract_evidence_gaps(None) == []
    assert exp_abcd._join_diffs("one\n", " two ", "") == "one\n two \n"
    assert exp_abcd._capped_diff("abcd", cap=2).startswith("ab\n[... diff truncated: 2")


def test_evaluator_normalization_adds_a_neutral_and_keeps_explicit_panels_exact(monkeypatch):
    class Choice:
        @staticmethod
        def choice(values):
            return values[0]

    assert exp_abcd._ensure_min_evaluators(["codex"], 3) == ["codex", "claude", "cursor"]
    assert (
        exp_abcd._ensure_min_evaluators(
            ["claude", "codex", "cursor", "vibe"],
            implementers=["claude", "codex", "cursor", "vibe"],
            capacity={"gemini": {"policy": "drain"}},
        )[-1]
        == "gemini"
    )
    assert exp_abcd._with_probation(
        ["claude", "codex"], protected=["claude"], rng=Choice(), unproven=["muse"]
    ) == [
        "claude",
        "muse",
    ]
    monkeypatch.setattr(exp_abcd, "_capacity_map", lambda: {})
    monkeypatch.setattr(exp_abcd, "_unproven_judges", lambda: [])
    default = exp_abcd._normalize_evaluator_specs(
        None, default_agents=["codex"], implementer_agents=["codex"]
    )
    assert len(default) == exp_abcd.MIN_EVALUATORS
    explicit = exp_abcd._normalize_evaluator_specs(
        [{"agent": "codex", "profile_id": "p1"}],
        default_agents=[],
        implementer_agents=["codex"],
    )
    assert explicit == [{"agent": "codex", "profile_id": "p1", "evaluator_id": "p1"}]
    with pytest.raises(ValueError, match="explicit evaluator list"):
        exp_abcd._normalize_evaluator_specs([], default_agents=[], implementer_agents=[])


def test_status_and_collect_keep_member_identity_and_review_replacement_hermetic(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    exp_dir = exp_abcd.exp_paths("trial")
    exp_dir.mkdir(parents=True)
    exp_dir.joinpath("meta.json").write_text(
        '{"repo": "owner/repo", "base": "main", "members": ['
        '{"arm_id": "a", "member_id": "implement", "agent": "codex"},'
        '{"arm_id": "a", "member_id": "review", "agent": "claude", "role": "review",'
        '"review_of_member_id": "implement"}]}'
    )
    monkeypatch.setattr(exp_abcd, "exp_worktree", lambda *_args: tmp_path / "missing-worktree")
    monkeypatch.setattr(
        exp_abcd.watch,
        "classify_lane",
        lambda **kwargs: {"signals": {"uncommitted_diff_stat": "1 file"}, "state": "idle"},
    )
    status = exp_abcd.status("trial", stale_seconds=7)
    assert status["members"][1]["member_id"] == "review"
    assert status["members"][1]["worktree_diff_stat"] == "1 file"

    import experiment_recovery

    monkeypatch.setattr(experiment_recovery, "arm_diff", lambda *args, **kwargs: "review diff\n")
    collected = exp_abcd.collect("owner/repo", "trial")
    assert collected["diffs"] == {
        "review": {
            "path": str(exp_dir / "diff-review.patch"),
            "bytes": len("review diff\n"),
            "source": "branch",
        }
    }
    assert (exp_dir / "diff-review.patch").read_text() == "review diff\n"
