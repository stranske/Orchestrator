"""Hermetic coverage for the recovery and rail execution boundaries in issue #828."""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import exp_abcd
import experiment_recovery
import rail_exercise


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """New coverage tests must never reach the real runtime or launch a process."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "feedback.db"))
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(tmp_path / "capabilities.json"))
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("coverage test attempted an external process or network call")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    yield
    assert attempted == []


def test_arm_diff_uses_merge_base_and_refuses_missing_branch(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(experiment_recovery, "repo_store", lambda _repo: store)

    def fake_git(_store: Path, *args: str, **_kwargs: object) -> str:
        calls.append(args)
        if args[:2] == ("rev-parse", "--verify"):
            return "arm-sha\n"
        if args[0] == "merge-base":
            return "base-sha\n"
        if args[:2] == ("--no-pager", "diff"):
            return "diff --git a/a b/a\n+candidate\n"
        raise AssertionError(args)

    monkeypatch.setattr(experiment_recovery, "_git", fake_git)
    assert experiment_recovery.arm_diff("owner/repo", "tick-1-a", "codex") == (
        "diff --git a/a b/a\n+candidate\n"
    )
    assert ("merge-base", "exp/tick-1-a-codex", "origin/main") in calls
    assert ("--no-pager", "diff", "base-sha", "exp/tick-1-a-codex") in calls

    monkeypatch.setattr(experiment_recovery, "_git", lambda *_args, **_kwargs: "")
    assert experiment_recovery.arm_diff("owner/repo", "tick-1-a", "codex") is None


def test_restore_partial_experiment_preserves_missing_arm_and_stub_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    row = {
        "exp_id": "tick-1782960020-owner-repo-1",
        "repo": "owner/repo",
        "target": "owner/repo#1",
        "agents": ["codex", "vibe"],
        "task_type": "testgen",
        "ts": 1_700_000_000,
    }
    monkeypatch.setattr(
        experiment_recovery,
        "arm_diff",
        lambda _repo, _exp_id, agent, **_kwargs: (
            "diff --git a/a b/a\n+x\n" if agent == "codex" else None
        ),
    )
    result = experiment_recovery.restore_experiment(
        row, apply=True, exp_dir=tmp_path / "experiments", gh_fn=lambda *_args: ""
    )
    recovered = tmp_path / "experiments" / row["exp_id"]
    meta = json.loads((recovered / "meta.json").read_text())
    assert result == {
        "exp_id": row["exp_id"],
        "repo": "owner/repo",
        "subject": "owner-repo-1",
        "arms_recovered": ["codex"],
        "arms_missing": ["vibe"],
        "nonempty_arms": ["codex"],
        "status": "partial",
        "applied": True,
    }
    assert meta["agents"] == ["codex"]
    assert meta["task_type"] == "testgen"
    assert meta["recovery_missing_arms"] == ["vibe"]
    assert meta["missing_spec"] is True
    assert not (recovered / exp_abcd.exp_diff_path("vibe")).exists()
    assert (recovered / exp_abcd.exp_log_path("codex")).stat().st_mtime == row["ts"]


def test_recovery_plan_applies_limit_and_reports_subject_correlation(monkeypatch) -> None:
    rows = [
        {"exp_id": "tick-1-owner-repo-1"},
        {"exp_id": "tick-2-owner-repo-1"},
    ]
    monkeypatch.setattr(experiment_recovery, "unevaluated_experiments", lambda _conn: rows)
    restored = []

    def fake_restore(row, **_kwargs):
        restored.append(row["exp_id"])
        return {
            "status": "recoverable",
            "subject": experiment_recovery.subject_of(row["exp_id"]),
            "arms_recovered": ["codex"],
            "arms_missing": [],
        }

    monkeypatch.setattr(experiment_recovery, "restore_experiment", fake_restore)
    report = experiment_recovery.plan(limit=1)
    assert restored == ["tick-1-owner-repo-1"]
    assert report["experiments"] == report["distinct_subjects"] == 1
    assert report["counts"] == {"recoverable": 1}
    assert report["subject_histogram"] == [("owner-repo-1", 1)]


def test_rail_break_result_requires_a_failure_or_explicit_check() -> None:
    passed, reason = rail_exercise._break_ok({}, [{"rc": 0, "output": ""}], [])
    assert (passed, reason) == (False, "nonzero break run required")
    passed, reason = rail_exercise._break_ok(
        {"break_case": {"run": "false"}}, [{"rc": 0, "output": ""}], [{"rc": 0, "output": ""}]
    )
    assert (passed, reason) == (True, "break check rc=0")
    passed, reason = rail_exercise._break_ok(
        {"break_case": {"run": "false"}}, [{"rc": 0, "output": ""}], [{"rc": 1, "output": "failed"}]
    )
    assert (passed, reason) == (False, "break check rc=1")


def test_recovery_does_not_launch_processes_when_all_dependencies_are_injected(
    tmp_path: Path, monkeypatch
) -> None:
    attempted: list[object] = []

    def forbidden(*args: object, **_kwargs: object) -> None:
        attempted.append(args)
        raise AssertionError("coverage test must not launch subprocesses")

    monkeypatch.setattr(experiment_recovery.subprocess, "run", forbidden)
    monkeypatch.setattr(experiment_recovery, "arm_diff", lambda *_args, **_kwargs: "")
    result = experiment_recovery.restore_experiment(
        {
            "exp_id": "plain",
            "repo": "owner/repo",
            "target": "owner/repo#1",
            "agents": ["codex"],
        },
        apply=False,
        exp_dir=tmp_path,
    )
    assert result["status"] == "recoverable"
    assert attempted == []
