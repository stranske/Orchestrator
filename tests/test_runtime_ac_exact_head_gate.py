"""The runtime-AC gate checks a PR's own executable proof on exactly the commit that will merge.

stranske/Orchestrator#1356 (2026-11-12): Gate labels a PR `acceptance-criteria` when it carries a
deliberate-break marker. The gate then ran its spec outside any checkout (the break failed
"not a git repository") and required 100% over nine manual items it cannot grade: 1 of 12, FAIL,
for every such PR. It now runs the spec in a checkout of the exact head, measures the break
against the merge base with main, and blocks only on executable checks.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import feedback
import runtime_ac
import runtime_ac_gate

TARGET = "owner/repo#7"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def pr_checkout(tmp_path: Path):
    """A repository whose HEAD is a PR commit on top of origin/main's tip."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "main")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    for n in (1, 2):  # two PR commits: HEAD^ is NOT the pre-change code
        (repo / "a.py").write_text(f"change {n}\n")
        _git(repo, "commit", "-qam", f"pr {n}")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _gate(tmp_path, monkeypatch, checkout, head, recheck_result):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    spec_dir = tmp_path / "specs"
    spec = json.loads(json.dumps(runtime_ac._valid_spec()))
    spec["verification"]["target"] = TARGET
    spec["verification"]["repo"] = TARGET.split("#", 1)[0]
    installed = runtime_ac_gate.materialize_range_spec(TARGET, spec, spec_dir=spec_dir)
    assert installed["status"] == "materialized", installed
    seen = {}

    @contextlib.contextmanager
    def worktree(target, sha):
        seen["worktree"] = (target, sha)
        yield checkout

    def recheck(spec_arg, wt, *, allow_command_checks, base_ref):
        seen.update(worktree_path=wt, base_ref=base_ref, allow=allow_command_checks)
        return dict(recheck_result)

    result = runtime_ac_gate.gate_status(
        {"target": TARGET, "lane": "closer", "labels": ["acceptance-criteria"], "head": head},
        dry_run=False,
        env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1"},
        spec_dir=spec_dir,
        latest_run_fn=lambda target, mode=None: None,
        worktree_fn=worktree,
        recheck_fn=recheck,
    )
    return result, seen


def test_executable_proof_on_the_exact_head_passes_against_the_merge_base(
    tmp_path, monkeypatch, pr_checkout
):
    checkout, base, head = pr_checkout
    result, seen = _gate(tmp_path, monkeypatch, checkout, head, {"result": "PASS"})
    assert result["status"] == "executed" and result["blocks"] is False, result
    assert result["verdict"] == "PASS"
    assert seen["worktree"] == (TARGET, head)
    assert seen["base_ref"] == base  # the pre-change code, not HEAD^ (the PR's previous commit)
    assert seen["base_ref"] != _git(checkout, "rev-parse", "HEAD^")
    assert seen["allow"] is True


def test_manual_only_spec_is_reported_not_blocked(tmp_path, monkeypatch, pr_checkout):
    checkout, _, head = pr_checkout
    result, _ = _gate(
        tmp_path, monkeypatch, checkout, head, {"result": "UNRUNNABLE", "reason": "manual_only"}
    )
    assert result["blocks"] is False and result["verdict"] == "MANUAL_ONLY", result


@pytest.mark.parametrize(
    "recheck",
    [
        {"result": "FAIL"},
        {"result": "UNRUNNABLE", "reason": "commands_disabled"},
        {"result": "UNRUNNABLE", "reason": "hollow_break"},
        {"result": "UNRUNNABLE", "reason": "tests_skipped"},
        {"result": "UNRUNNABLE", "reason": "tool_error"},
    ],
)
def test_anything_short_of_executable_pass_blocks(tmp_path, monkeypatch, pr_checkout, recheck):
    checkout, _, head = pr_checkout
    result, _ = _gate(tmp_path, monkeypatch, checkout, head, recheck)
    assert result["blocks"] is True, result
    assert result["blocking"], result


def test_a_checkout_of_the_wrong_commit_fails_closed(tmp_path, monkeypatch, pr_checkout):
    checkout, base, _ = pr_checkout
    result, _ = _gate(tmp_path, monkeypatch, checkout, base, {"result": "PASS"})
    assert result["status"] == "failed" and result["blocks"] is True, result


def test_recheck_measures_the_break_against_the_given_base(monkeypatch, tmp_path):
    captured = {}

    def run_verification(spec, **kwargs):
        captured["spec"] = spec
        return {"check_results": [], "gate": {"verdict": "PASS"}}

    monkeypatch.setattr(runtime_ac, "run_verification", run_verification)
    spec = {
        "acceptance_criteria": [
            {"id": "AC1", "checks": [{"type": "deliberate_break", "test_cmd": "pytest -q"}]}
        ]
    }
    runtime_ac_gate.recheck_merged(spec, tmp_path, allow_command_checks=True, base_ref="c" * 40)
    check = captured["spec"]["acceptance_criteria"][0]["checks"][0]
    assert check["base_ref"] == "c" * 40
    runtime_ac_gate.recheck_merged(spec, tmp_path, allow_command_checks=True)
    assert captured["spec"]["acceptance_criteria"][0]["checks"][0]["base_ref"] == "HEAD^"


def _transcript(rows):
    return json.dumps({"check_results": rows, "gate": {"verdict": "NEEDS_REVIEW"}})


def test_checks_run_state_free_like_ci_and_the_environment_is_restored(
    tmp_path, monkeypatch, pr_checkout
):
    """stranske/Orchestrator#1354 (2026-11-12): a test CI passes failed here against the live ledger."""
    checkout, _, head = pr_checkout
    monkeypatch.setenv("ORCH_STATE_DIR", "/live/state")
    monkeypatch.delenv("ORCH_LOCAL_RUNTIME", raising=False)
    seen = {}

    def recheck(spec_arg, wt, *, allow_command_checks, base_ref):
        seen.update(
            {
                k: os.environ.get(k)
                for k in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME", "PYTEST_ADDOPTS")
            }
        )
        return {"result": "PASS"}

    @contextlib.contextmanager
    def worktree(target, sha):
        yield checkout

    runtime_ac_gate._exact_head_gate(
        {}, TARGET, head, allow_command_checks=True, worktree_fn=worktree, recheck_fn=recheck
    )
    assert seen["ORCH_STATE_DIR"] not in (None, "/live/state")
    assert seen["ORCH_LOCAL_RUNTIME"] and "-rs" in seen["PYTEST_ADDOPTS"]
    assert os.environ["ORCH_STATE_DIR"] == "/live/state"
    assert "ORCH_LOCAL_RUNTIME" not in os.environ


def test_a_machine_state_skip_goes_to_the_judge_not_a_block(tmp_path, monkeypatch, pr_checkout):
    checkout, _, head = pr_checkout
    skipped = _transcript(
        [
            {
                "check_id": "AC1-CMD",
                "type": "command",
                "status": "PASS",
                "stdout_tail": "SKIPPED [1] x.py:9: capability ledger has no row\n24 passed, 1 skipped",
            },
        ]
    )
    result, _ = _gate(
        tmp_path,
        monkeypatch,
        checkout,
        head,
        {"result": "UNRUNNABLE", "reason": "tests_skipped", "transcript": skipped},
    )
    assert result["blocks"] is False and result["verdict"] == "UNVERIFIABLE_BEFORE_MERGE", result


def test_a_hollow_break_whose_tests_ran_still_blocks(tmp_path, monkeypatch, pr_checkout):
    checkout, _, head = pr_checkout
    ran = _transcript(
        [
            {
                "check_id": "AC1-CMD",
                "type": "command",
                "status": "PASS",
                "stdout_tail": "30 passed",
            },
        ]
    )
    result, _ = _gate(
        tmp_path,
        monkeypatch,
        checkout,
        head,
        {"result": "UNRUNNABLE", "reason": "hollow_break", "transcript": ran},
    )
    assert result["blocks"] is True and result["verdict"] == "UNRUNNABLE", result
