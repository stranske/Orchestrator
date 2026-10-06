"""A read-only agent is told, in its prompt, not to act on GitHub. Owner decision 2026-10-05.

WHY NOW. Until the gh config pin (#461), gh inside an agent started from a session failed closed:
it found no hosts file and stopped at "gh auth login". The pin gives those agents their
dispatcher's gh, which can write. Read-only agents (offloads, adversarial reviewers, exp_abcd and
ux_review evaluators) need only READS: every gh call that stopped at the login error in 90 days of
offload logs was a read. Their prompts forbade `gh pr create` and nothing else, so the owner chose
to extend that line to merging, closing, label edits, comments and reviews.

One sentence, `dispatcher.GH_READ_ONLY_RULE`, reaches both delivery paths: the offload rules every
offload carries, and the prompt word every evaluator command passes.
"""

from __future__ import annotations

import subprocess

import pytest

import adapters
import dispatcher
import exp_abcd

# exp_abcd's evaluator seats, the keys of the command table in `_eval_command`.
EVALUATORS = ("claude", "codex", "cursor", "gemini", "vibe")


def test_the_rule_names_every_decided_action():
    rule = dispatcher.GH_READ_ONLY_RULE
    for action in (
        "open, merge or close pull requests",
        "close issues",
        "edit labels",
        "comments or reviews",
        "Reading with gh is fine",
    ):
        assert action in rule, (action, rule)


@pytest.mark.parametrize("git_workspace", [True, False])
def test_every_offload_prompt_carries_the_rule(tmp_path, git_workspace):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if git_workspace:
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    prepared = dispatcher._offload_prompt("Review the change.", workspace, "cursor")
    assert prepared.count(dispatcher.GH_READ_ONLY_RULE) == 1, prepared
    assert "Do not run git commit or git push" in prepared, prepared


def test_an_evaluator_receives_its_prompt_then_the_rule(tmp_path):
    """Expanded by bash exactly as the evaluator's argv is, quotes and dollars intact."""
    body = 'Score this diff.\nIt says "$HOME", `uname` and it\'s 100% literal.'
    promptfile = tmp_path / "prompt.txt"
    promptfile.write_text(body)
    word = exp_abcd._eval_prompt_word(str(promptfile))
    out = subprocess.run(
        ["bash", "-c", f"printf %s {word}"], capture_output=True, text=True, check=True
    ).stdout
    assert out == f"{body}\n\nREAD-ONLY EVALUATION: {dispatcher.GH_READ_ONLY_RULE}", out


@pytest.mark.parametrize("agent", EVALUATORS)
def test_every_evaluator_command_passes_that_one_prompt_word(monkeypatch, tmp_path, agent):
    # `_eval_command` resolves gemini's model for every seat; keep that a pinned lookup, never a
    # catalog probe (the off switch tests/test_experiment_arm_identity.py uses for the same call).
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(adapters, "_ADVERTISED_MEMO", {})
    # Its prelude creates the agent's runtime dirs; keep them in the sandbox.
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "REAL_HOME", tmp_path / "home")
    command = exp_abcd._eval_command(agent, "/tmp/p.txt")
    assert command.count(exp_abcd._eval_prompt_word("/tmp/p.txt")) == 1, command
