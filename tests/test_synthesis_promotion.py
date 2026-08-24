from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import exp_abcd
import feedback
import synthesis_promotion as promotion


@pytest.fixture(autouse=True)
def _disposable_feedback_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_experiment(tmp_path: Path, name: str = "exp-1") -> tuple[Path, Path, str]:
    exp = tmp_path / name
    exp.mkdir()
    repo = tmp_path / f"{name}-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    (repo / "math_utils.py").write_text("def add(a, b):\n    return 0\n")
    _git(repo, "add", "math_utils.py")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    meta = {
        "repo": "owner/repo",
        "base": "main",
        "base_sha": base_sha,
        "agents": ["codex", "cursor"],
        "exp_id": name,
        "task_type": "implement",
        "capacity_pool_id": "codex-subscription",
        "shared_capacity_cost": {"attempts": 2},
    }
    (exp / "meta.json").write_text(json.dumps(meta))
    (exp / "spec.md").write_text(
        "## Scope\n- Update math_utils.py\n\n"
        "## Acceptance Criteria\n- Deliberate-break test proves add behavior.\n"
    )
    (exp / "eval-maps.json").write_text(json.dumps({"judge-a": {"A": "codex", "B": "cursor"}}))
    return exp, repo, base_sha


def _to_synth_complete(exp: Path, repo: Path, commit: str, now: int = 100) -> dict:
    state = promotion.ensure_evaluated_state(exp, now=now)
    state["synthesis"] = {
        "root_run_id": f"{exp.name}:synth",
        "current_run_id": f"{exp.name}:synth",
        "run_ids": [f"{exp.name}:synth"],
        "pid": 999999,
        "worktree": str(repo),
        "log": str(exp / "synth.log"),
        "base": "codex",
        "synth_agent": "codex",
        "launch_head": "base-head",
        "commit": commit,
        "resume_history": [],
    }
    state, _ = promotion.transition(state, "synth_running", reason="fixture_launch", now=now + 1)
    state, _ = promotion.transition(state, "synth_complete", reason="fixture_complete", now=now + 2)
    promotion._atomic_json(promotion.state_path(exp), state)
    return state


def _passing_verification() -> dict:
    evidence = {
        "scope": {
            "ok": True,
            "changed_paths": ["math_utils.py", "tests/test_math_utils.py"],
            "changed_paths_hash": promotion._hash(["math_utils.py", "tests/test_math_utils.py"]),
        },
        "secret_scan": {"ok": True, "finding_ids": []},
        "local_verify": {
            "ok": True,
            "verdict": "PASS",
            "test_cmd": f"{sys.executable} -m unittest discover -s tests",
            "test_paths": ["tests/test_math_utils.py"],
        },
        "runtime_ac": {"ok": True, "verdict": "PASS"},
        "repo_gates": [{"ok": True, "argv": ["pytest", "tests/test_math_utils.py"]}],
        "deliberate_break_status": "PASS",
    }
    return {
        "passed": True,
        "transient": False,
        "evidence": evidence,
        "evidence_hash": promotion._hash(evidence),
    }


def _verified_candidate(tmp_path: Path, name: str = "exp-verified") -> tuple[Path, dict]:
    exp, repo, _base = _init_experiment(tmp_path, name)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_math_utils.py").write_text(
        "from math_utils import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    _git(repo, "add", "math_utils.py", "tests/test_math_utils.py")
    _git(repo, "commit", "-m", "synthesis")
    _to_synth_complete(exp, repo, _git(repo, "rev-parse", "HEAD"))
    result = promotion.reconcile(
        exp,
        verify_fn=lambda state, root: _passing_verification(),
        mirror_fn=lambda state, event: {"recorded": True, "event": event},
        now=200,
    )
    assert result["state"]["delivery_phase"] == "candidate_ready"
    return exp, result["state"]


def test_candidate_requires_verified_predecessor() -> None:
    assert (
        "candidate_ready" not in promotion.PROMOTION_TRANSITIONS["synth_complete"]
    ), "candidate_ready bypassed synth_verified"


def test_synthesis_launch_artifact_prevents_duplicate_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    exp = tmp_path / "exp-launch"
    exp.mkdir()
    launch = {
        "schema_version": 1,
        "status": "launched",
        "experiment_id": "exp-launch",
        "repo": "owner/repo",
        "base": "codex",
        "synth_agent": "codex",
        "run_id": "exp-launch:synth",
        "pid": 12345,
        "worktree": "/tmp/isolated-worktree",
        "log": "/tmp/synth.log",
        "ranking": [["codex", 9.0]],
        "gate": {"decision": "use"},
    }
    promotion._atomic_json(exp / "synthesis-launch.json", launch)
    monkeypatch.setattr(
        exp_abcd,
        "_spawn",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate synthesis spawn")),
    )
    first = exp_abcd.synthesize("owner/repo", "exp-launch")
    second = exp_abcd.synthesize("owner/repo", "exp-launch")
    assert first["recovered_launch"] is True and second["recovered_launch"] is True
    assert first["run_id"] == second["run_id"] == "exp-launch:synth"


def test_hollow_synthesis_never_becomes_candidate_ready(tmp_path: Path) -> None:
    exp, repo, base_sha = _init_experiment(tmp_path, "exp-hollow")
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_math_utils.py").write_text(
        "import unittest\n\nclass Hollow(unittest.TestCase):\n"
        "    def test_hollow(self):\n        self.assertTrue(True)\n"
    )
    _git(repo, "add", "math_utils.py", "tests/test_math_utils.py")
    _git(repo, "commit", "-m", "hollow synthesis")
    (exp / "promotion-verification.json").write_text(
        json.dumps(
            {
                "base_ref": base_sha,
                "local_verify": {
                    "base_ref": base_sha,
                    "test_cmd": f"{sys.executable} -m unittest discover -s tests",
                    "test_paths": ["tests/test_math_utils.py"],
                },
            }
        )
    )
    _to_synth_complete(exp, repo, _git(repo, "rev-parse", "HEAD"))
    result = promotion.reconcile(
        exp,
        mirror_fn=lambda state, event: {"recorded": True, "event": event},
        now=200,
    )
    state = result["state"]
    assert state["delivery_phase"] == "discarded"
    assert state["canonical_state"] == "retired"
    assert state["verification"]["evidence"]["local_verify"]["verdict"] == "FAIL_HOLLOW"
    assert state["verification"]["evidence"]["deliberate_break_status"] == "FAIL"
    assert not (exp / promotion.CANDIDATE_JSON).exists()


def test_secret_and_scope_gates_block_candidate_without_leaking_content(
    tmp_path: Path,
) -> None:
    exp, repo, _base_sha = _init_experiment(tmp_path, "exp-secret")
    (repo / ".env").write_text("API_KEY=super-secret-token-value\n")
    _git(repo, "add", ".env")
    _git(repo, "commit", "-m", "unsafe synthesis")
    _to_synth_complete(exp, repo, _git(repo, "rev-parse", "HEAD"))
    result = promotion.reconcile(
        exp,
        mirror_fn=lambda state, event: {"recorded": True, "event": event},
        now=200,
    )["state"]
    secret_evidence = result["verification"]["evidence"]["secret_scan"]
    assert result["delivery_phase"] == "discarded"
    assert secret_evidence["ok"] is False
    assert any(item.startswith("sensitive_path:") for item in secret_evidence["finding_ids"])
    assert "super-secret-token-value" not in json.dumps(result)
    assert not (exp / promotion.CANDIDATE_JSON).exists()


def test_verified_candidate_is_canonical_complete_and_exactly_once(tmp_path: Path) -> None:
    exp, state = _verified_candidate(tmp_path)
    candidate = state["candidate"]
    body = (exp / promotion.CANDIDATE_BODY).read_text()
    for heading in (
        "## Why",
        "## Scope",
        "## Non-Goals",
        "## Tasks",
        "## Acceptance Criteria",
        "## Implementation Notes",
    ):
        assert heading in body
    assert "unittest discover -s tests" in body
    assert candidate["delivery"]["direct_publication_allowed"] is False
    assert candidate["delivery"]["auto_merge_allowed"] is False
    assert candidate["lineage"]["experiment_id"] == exp.name
    assert candidate["lineage"]["evaluator_ids"] == ["judge-a"]
    assert candidate["lineage"]["shared_capacity_pool_id"] == "codex-subscription"
    first_id = candidate["candidate_id"]

    repeated = promotion.reconcile(
        exp,
        verify_fn=lambda state, root: _passing_verification(),
        mirror_fn=lambda state, event: {"recorded": True, "event": event},
        now=201,
    )["state"]
    assert repeated["candidate"]["candidate_id"] == first_id
    assert sum(row["to"] == "candidate_ready" for row in repeated["phase_history"]) == 1


def test_candidate_preserves_accepted_capability_and_workflow_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    for agent in ("codex", "cursor"):
        feedback.record_run(
            f"exp-lineage:{agent}",
            "owner/repo [exp exp-lineage]",
            "implement",
            agent,
            experiment_id="exp-lineage",
            capability_ids=["abcd-experiment"],
            influenced_by_workflow_ids=["exp_abcd"],
        )
    _exp, state = _verified_candidate(tmp_path, "exp-lineage")
    influences = state["candidate"]["lineage"]["accepted_influences"]
    assert any(
        row["influence_type"] == "capability" and row["influence_id"] == "abcd-experiment"
        for row in influences
    )
    assert any(
        row["influence_type"] == "workflow" and row["influence_id"] == "exp_abcd"
        for row in influences
    )


def test_merged_durable_mirrors_and_reverted_retires(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    exp, state = _verified_candidate(tmp_path, "exp-durable")
    source_ids = [row["source_run_id"] for row in state["lineage"]["members"]]
    for source_id, member in zip(source_ids, state["lineage"]["members"]):
        feedback.record_run(
            source_id,
            "owner/repo [exp exp-durable]",
            "implement",
            member["agent"],
            experiment_id="exp-durable",
        )
    feedback.record_run(
        "exp-durable:synth",
        "owner/repo [exp exp-durable synth]",
        "synthesize",
        "codex",
        experiment_id="exp-durable",
    )
    promotion.link_delivery(
        exp,
        delivery_run_id="delivery-run-1",
        delivery_ref="owner/repo#99",
        pr_number=99,
        now=300,
    )
    merged = promotion.reconcile(
        exp,
        outcome_lookup_fn=lambda state: {
            "verifier_verdict": "PASS",
            "adjudicated_verdict": "PASS",
            "merged": True,
            "ci_status": "success",
            "durability": "pending",
        },
        now=301,
    )["state"]
    assert merged["delivery_phase"] == "merged"
    assert merged["canonical_state"] == "canary"
    durable = promotion.reconcile(
        exp,
        outcome_lookup_fn=lambda state: {
            "verifier_verdict": "PASS",
            "adjudicated_verdict": "PASS",
            "merged": True,
            "ci_status": "success",
            "durability": "durable",
        },
        now=400,
    )["state"]
    assert durable["delivery_phase"] == "durable"
    assert durable["canonical_state"] == "active"
    assert durable["mirrored_events"] == ["merged", "durable"]
    conn = sqlite3.connect(feedback.DB_PATH)
    try:
        synth_outcome = conn.execute(
            "SELECT merged,durability FROM outcomes WHERE run_id='exp-durable:synth'"
        ).fetchone()
        source_delivery_events = conn.execute(
            "SELECT COUNT(*) FROM completion_events WHERE run_id IN (?,?) "
            "AND producer='exp_abcd' AND phase IN ('delivery','durability')",
            tuple(source_ids),
        ).fetchone()[0]
    finally:
        conn.close()
    assert synth_outcome == (1, "durable")
    assert source_delivery_events >= 4

    reverted_exp, _ = _verified_candidate(tmp_path, "exp-reverted")
    promotion.link_delivery(
        reverted_exp,
        delivery_run_id="delivery-run-2",
        delivery_ref="owner/repo#100",
        pr_number=100,
        now=500,
    )
    reverted = promotion.reconcile(
        reverted_exp,
        outcome_lookup_fn=lambda state: {
            "verifier_verdict": "PASS",
            "adjudicated_verdict": "FAIL",
            "merged": True,
            "ci_status": "success",
            "durability": "reverted",
        },
        mirror_fn=lambda state, event: {"recorded": True, "event": event},
        now=501,
    )["state"]
    assert reverted["delivery_phase"] == "discarded"
    assert reverted["canonical_state"] == "retired"
    assert reverted["rollback"]["executed"] is True
    assert reverted["rollback"]["remote_mutation"] is False
    assert reverted["mirrored_events"] == ["delivery_failed"]


def test_stale_candidate_retires_without_remote_mutation(tmp_path: Path) -> None:
    exp, state = _verified_candidate(tmp_path, "exp-stale")
    state["candidate_expires_ts"] = 10
    promotion._atomic_json(promotion.state_path(exp), state)
    retired = promotion.reconcile(
        exp,
        mirror_fn=lambda state, event: {"recorded": True, "event": event},
        now=20,
    )["state"]
    assert retired["delivery_phase"] == "discarded"
    assert retired["rollback"]["reason"] == "stale_candidate_retired"
    assert retired["rollback"]["remote_mutation"] is False
