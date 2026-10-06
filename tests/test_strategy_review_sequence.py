"""Exercise pair sequencing and its final artifact with actual Git worktrees."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import exp_abcd
import strategy_experiment


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
def pair(tmp_path, monkeypatch):
    canon = tmp_path / "repo"
    canon.mkdir()
    git(canon, "init", "-b", "main")
    git(canon, "config", "user.name", "Sequence Test")
    git(canon, "config", "user.email", "sequence@example.invalid")
    (canon / "value.txt").write_text("base\n")
    git(canon, "add", "value.txt")
    git(canon, "commit", "-m", "base")
    base = git(canon, "rev-parse", "HEAD")
    exp_id = "pair-sequence"
    arms, members = exp_abcd._normalize_arm_members(
        [strategy_experiment.normalize_arm({"strategy": "pair", "agents": ["cursor", "vibe"]}, 0)]
    )
    meta = {
        "schema_version": 2,
        "repo": "o/r",
        "exp_id": exp_id,
        "base": "main",
        "base_sha": base,
        "arms": arms,
        "members": members,
    }
    edir = tmp_path / "experiments" / exp_id
    edir.mkdir(parents=True)
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text("The frozen requirement: deliver corrected value.\n")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", edir.parent)
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda _repo: canon)
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        exp_abcd.claims if hasattr(exp_abcd, "claims") else strategy_experiment.claims,
        "update_metadata",
        lambda *args, **kwargs: True,
    )
    spawned = []

    def spawn(agent, mode, prompt, wt, log, **kwargs):
        spawned.append({"agent": agent, "prompt": prompt, "worktree": wt, **kwargs})
        log.write_text("started\n")
        return 12345

    monkeypatch.setattr(exp_abcd, "_spawn", spawn)
    return canon, edir, meta, spawned


@pytest.mark.parametrize(
    "marker", [None, {"rc": 1, "rc_of": "agent"}, {"rc": 0, "rc_of": "release"}]
)
def test_review_waits_for_successful_agent_completion(pair, marker):
    _canon, edir, meta, spawned = pair
    impl = exp_abcd.experiment_members(meta)[0]
    if marker is not None:
        (edir / "done").mkdir()
        marker["run_id"] = exp_abcd._member_run_id(meta["exp_id"], impl)
        (edir / "done" / f"{marker['run_id']}.json").write_text(json.dumps(marker))
    result = exp_abcd.launch_pending_reviews("o/r", meta["exp_id"])
    assert not spawned
    assert result["pending"] or result["failed"]


def test_review_uses_frozen_delta_and_evaluates_the_corrected_pair_once(pair):
    canon, edir, meta, spawned = pair
    impl, reviewer = exp_abcd.experiment_members(meta)
    impl_wt = exp_abcd.exp_worktree("o/r", meta["exp_id"], impl["agent"], impl["member_id"])
    impl_wt.parent.mkdir(parents=True)
    git(
        canon,
        "worktree",
        "add",
        "-b",
        exp_abcd.exp_branch(meta["exp_id"], impl["agent"], impl["member_id"]),
        str(impl_wt),
        meta["base_sha"],
    )
    (impl_wt / "value.txt").write_text("implementation\n")
    (impl_wt / "new.txt").write_text("seeded new file\n")
    git(impl_wt, "add", "value.txt", "new.txt")
    git(impl_wt, "commit", "-m", "implementation")
    # A changing main must not alter the pair's seed or comparison base.
    (canon / "later.txt").write_text("concurrent main\n")
    git(canon, "add", "later.txt")
    git(canon, "commit", "-m", "concurrent main")
    run_id = exp_abcd._member_run_id(meta["exp_id"], impl)
    (edir / "done").mkdir()
    (edir / "done" / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "rc": 0, "rc_of": "agent"})
    )
    result = exp_abcd.launch_pending_reviews("o/r", meta["exp_id"])
    assert len(result["launched"]) == len(spawned) == 1
    wt = spawned[0]["worktree"]
    assert git(wt, "rev-parse", "HEAD^") == meta["base_sha"]
    assert (wt / "value.txt").read_text() == "implementation\n"
    assert not (wt / "later.txt").exists()
    assert (wt / "new.txt").read_text() == "seeded new file\n"
    assert "The frozen requirement: deliver corrected value." in spawned[0]["prompt"]
    assert exp_abcd.launch_pending_reviews("o/r", meta["exp_id"])["launched"] == []
    assert exp_abcd.launch_pending_reviews("o/r", meta["exp_id"])["pending"] == [
        reviewer["member_id"]
    ]
    (wt / "value.txt").write_text("corrected\n")
    git(wt, "add", "value.txt")
    git(wt, "commit", "-m", "review correction")
    git(canon, "update-ref", "refs/remotes/origin/main", meta["base_sha"])
    collected = exp_abcd.collect("o/r", meta["exp_id"])
    assert set(collected["diffs"]) == {reviewer["member_id"]}
    final = Path(collected["diffs"][reviewer["member_id"]]["path"]).read_text()
    assert "+corrected" in final and "+implementation" not in final and "later.txt" not in final
    assert "+seeded new file" in final


def test_launch_retry_reuses_the_committed_seed(pair, monkeypatch):
    canon, edir, meta, spawned = pair
    impl, reviewer = exp_abcd.experiment_members(meta)
    impl_wt = exp_abcd.exp_worktree("o/r", meta["exp_id"], impl["agent"], impl["member_id"])
    impl_wt.parent.mkdir(parents=True)
    git(
        canon,
        "worktree",
        "add",
        "-b",
        exp_abcd.exp_branch(meta["exp_id"], impl["agent"], impl["member_id"]),
        str(impl_wt),
        meta["base_sha"],
    )
    (impl_wt / "value.txt").write_text("implementation\n")
    git(impl_wt, "commit", "-am", "implementation")
    run_id = exp_abcd._member_run_id(meta["exp_id"], impl)
    (edir / "done").mkdir()
    (edir / "done" / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "rc": 0, "rc_of": "agent"})
    )
    original_spawn = exp_abcd._spawn

    def unavailable(*args, **kwargs):
        raise OSError("spawn unavailable")

    monkeypatch.setattr(exp_abcd, "_spawn", unavailable)
    assert exp_abcd.launch_pending_reviews("o/r", meta["exp_id"])["failed"]
    wt = exp_abcd.exp_worktree("o/r", meta["exp_id"], reviewer["agent"], reviewer["member_id"])
    seeded = git(wt, "rev-parse", "HEAD")
    monkeypatch.setattr(exp_abcd, "_spawn", original_spawn)
    assert exp_abcd.launch_pending_reviews("o/r", meta["exp_id"])["launched"]
    assert git(wt, "rev-parse", "HEAD") == seeded
    assert (wt / "value.txt").read_text() == "implementation\n"


def test_failed_pair_gets_terminal_unknown_and_is_not_rescanned(pair, monkeypatch):
    _canon, edir, meta, _spawned = pair
    monkeypatch.setenv("ORCH_STATE_DIR", str(edir.parent / "state"))
    (edir / "strategy.json").write_text(json.dumps({"created_ts": 1}))
    monkeypatch.setattr(exp_abcd, "ship_gate_stamp", lambda: edir.parent / "stamp")
    calls = []

    def failed(*args):
        calls.append(args)
        return {
            "failed": [{"member_id": "pair-1", "reason": "implementer-failed"}],
            "pending": [],
            "launched": [],
        }

    monkeypatch.setattr(exp_abcd, "launch_pending_reviews", failed)

    def refuse(*args, **kwargs):
        pytest.fail("failed pair dispatched a judge or synthesis")

    exp_abcd.followup(
        collect_fn=refuse,
        evaluate_fn=refuse,
        synthesize_fn=refuse,
        subject_lifecycle_fn=lambda *args, **kwargs: None,
    )
    assert (edir / "followup-skip.json").exists()
    assert json.loads((edir / "strategy-receipt.json").read_text())["status"] == "UNKNOWN"
    exp_abcd.followup(
        collect_fn=refuse,
        evaluate_fn=refuse,
        synthesize_fn=refuse,
        subject_lifecycle_fn=lambda *args, **kwargs: None,
    )
    assert len(calls) == 1
