"""Offline git witnesses for #718; all checkouts and event history are disposable."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import provision

TARGET = "example/repo#718"
OPENER_BRANCH = "orchestrator/issue-718"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    repos = tmp_path / "repos"
    repos.mkdir()
    canon = repos / provision.repo_slug("example/repo")
    git(tmp_path, "clone", str(origin), str(canon))
    git(canon, "config", "user.email", "test@example.com")
    git(canon, "config", "user.name", "Provision Test")
    git(canon, "commit", "--allow-empty", "-m", "base")
    git(canon, "push", "-u", "origin", "main")
    git(canon, "branch", "pr-head")
    git(canon, "push", "origin", "pr-head")
    monkeypatch.setattr(provision, "REPOS_DIR", repos)
    monkeypatch.setattr(provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(provision, "base_branch", lambda repo: "main")
    monkeypatch.setattr(provision, "_pr_head_branch", lambda repo, num: "pr-head")
    monkeypatch.delenv("ORCH_ALLOW_STALE_WORKTREE_REUSE", raising=False)
    return canon, tmp_path / "holder [old]+ round"


def hold(layout, branch="pr-head"):
    canon, holder = layout
    git(canon, "worktree", "add", str(holder), branch)
    return holder


def events():
    return [json.loads(line) for line in provision.PROVISION_EVENTS_PATH.read_text().splitlines()]


@pytest.mark.parametrize("lane", ["closer", "opener"])
def test_a_clean_idle_holder_is_reclaimed(layout, lane, capsys):
    canon, _ = layout
    branch = "pr-head" if lane == "closer" else OPENER_BRANCH
    if lane == "opener":
        git(canon, "branch", branch)
    holder = hold(layout, branch)
    try:
        wt = provision.provision(TARGET, lane)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr)  # Keep git's holder path visible in the deliberate-break receipt.
        raise
    assert (wt / ".git").is_file()
    assert git(wt, "branch", "--show-current") == branch
    assert not holder.exists()
    assert (
        f"provision: reclaimed stale worktree {holder} holding {branch}" in capsys.readouterr().out
    )
    rows = events()
    assert len(rows) == 1
    assert rows[0] == {
        "ts": rows[0]["ts"],
        "target": TARGET,
        "lane": lane,
        "outcome": "reclaimed",
        "cause": "branch-held",
        "holder_path": str(holder),
    }
    assert git(canon, "ls-remote", "--heads", "origin", "pr-head")


@pytest.mark.parametrize("lane", ["closer", "opener"])
def test_b_dirty_holder_is_refused(layout, lane):
    canon, _ = layout
    branch = "pr-head" if lane == "closer" else OPENER_BRANCH
    if lane == "opener":
        git(canon, "branch", branch)
    holder = hold(layout, branch)
    dirty = holder / "nested" / "keep.txt"
    dirty.parent.mkdir()
    dirty.write_text("uncommitted work")
    with pytest.raises(RuntimeError) as caught:
        provision.provision(TARGET, lane)
    assert str(holder) in str(caught.value)
    assert "nested/keep.txt" in str(caught.value)
    assert dirty.read_text() == "uncommitted work"
    assert len(events()) == 1
    assert events()[0]["outcome"] == "failed"
    assert events()[0]["cause"] == "branch-held"


def test_c_live_holder_is_refused_naming_pid(layout):
    holder = hold(layout)
    proc = subprocess.Popen(["/bin/sh", "-c", f"sleep 300 # {holder}"], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while True:
            seen = subprocess.run(
                ["pgrep", "-f", re.escape(str(holder))], capture_output=True, text=True
            )
            if str(proc.pid) in seen.stdout.split():
                break
            assert time.monotonic() < deadline, f"child not visible to pgrep: {seen.stderr}"
            time.sleep(0.01)
        with pytest.raises(RuntimeError) as caught:
            provision.provision(TARGET, "closer")
        assert str(holder) in str(caught.value)
        assert str(proc.pid) in str(caught.value)
        assert holder.exists()
        assert proc.poll() is None
        assert len(events()) == 1
        assert events()[0]["cause"] == "branch-held"
        assert events()[0]["outcome"] == "failed"
    finally:
        # A process group also kills sleep when the shell has kept it as a child.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


@pytest.mark.parametrize("ahead", [False, True])
def test_d_existing_opener_branch_preserves_commits(layout, ahead):
    canon, _ = layout
    git(canon, "switch", "-c", OPENER_BRANCH)
    if ahead:
        git(canon, "commit", "--allow-empty", "-m", "keep me")
    tip = git(canon, "rev-parse", "HEAD")
    git(canon, "switch", "main")
    wt = provision.provision(TARGET, "opener")
    assert git(wt, "rev-parse", "HEAD") == tip
    assert git(wt, "branch", "--show-current") == OPENER_BRANCH
    assert len(events()) == 1
    assert events()[0]["outcome"] == "reused"
    assert events()[0]["cause"] == "branch-exists"


def test_e_empty_and_missing_ledger_are_distinct(capsys):
    assert provision.main(["--report", "--days", "7"]) == 0
    missing = capsys.readouterr().out
    assert "unmeasured: no ledger" in missing
    assert "= 0 over 7d" not in missing
    assert "provision: failed(branch-held|branch-exists) = " in missing
    provision.PROVISION_EVENTS_PATH.touch()
    assert provision.main(["--report", "--days", "7"]) == 0
    empty = capsys.readouterr().out
    assert "provision: failed(branch-held|branch-exists) = 0 over 7d" in empty
    assert "unmeasured" not in empty


def test_missing_holder_directory_is_pruned(layout):
    holder = hold(layout)
    shutil.rmtree(holder)
    wt = provision.provision(TARGET, "closer")
    assert (wt / ".git").exists()
    assert str(holder) not in git(layout[0], "worktree", "list", "--porcelain")
    assert events()[0]["outcome"] == "ok"


@pytest.mark.parametrize("unique", [False, True])
def test_diverged_opener_only_resets_when_all_commits_are_on_remotes(layout, unique):
    canon, _ = layout
    git(canon, "switch", "-c", OPENER_BRANCH)
    git(canon, "commit", "--allow-empty", "-m", "old work")
    tip = git(canon, "rev-parse", "HEAD")
    if not unique:
        git(canon, "push", "origin", f"{OPENER_BRANCH}:archived-work")
    git(canon, "switch", "main")
    git(canon, "commit", "--allow-empty", "-m", "new base")
    git(canon, "push", "origin", "main")
    if unique:
        with pytest.raises(RuntimeError, match="commits absent from every remote ref"):
            provision.provision(TARGET, "opener")
        assert git(canon, "rev-parse", OPENER_BRANCH) == tip
        assert events()[0]["outcome"] == "failed"
        assert events()[0]["cause"] == "branch-exists"
    else:
        wt = provision.provision(TARGET, "opener")
        assert git(wt, "rev-parse", "HEAD") == git(canon, "rev-parse", "origin/main")
        assert git(canon, "rev-parse", "origin/archived-work") == tip


def test_process_probe_failure_refuses_reclaim(layout, monkeypatch):
    holder = hold(layout)
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 2, "", "process inspection denied")
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", probe)
    with pytest.raises(RuntimeError, match="cannot check live processes"):
        provision.provision(TARGET, "closer")
    assert holder.exists()
    assert events()[0]["outcome"] == "failed"


def test_live_probe_result_refuses_reclaim(layout, monkeypatch):
    """Exercise the live-PID branch even when the host cannot enumerate processes."""
    holder = hold(layout)
    run = provision._run

    def probe(args, **kwargs):
        if args[0] == "pgrep":
            assert args == ["pgrep", "-f", re.escape(str(holder))]
            return subprocess.CompletedProcess(args, 0, "12345\n", "")
        return run(args, **kwargs)

    monkeypatch.setattr(provision, "_run", probe)
    with pytest.raises(RuntimeError) as caught:
        provision.provision(TARGET, "closer")
    assert str(holder) in str(caught.value)
    assert "live pid 12345" in str(caught.value)
    assert holder.exists()
    assert events()[0]["outcome"] == "failed"


def test_fresh_and_existing_worktree_emit_one_event_each(layout, monkeypatch):
    wt = provision.provision(TARGET, "opener")
    assert provision.provision(TARGET, "opener") == wt
    (wt / "dirty.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="dirty.txt"):
        provision.provision(TARGET, "opener")
    monkeypatch.setenv("ORCH_ALLOW_STALE_WORKTREE_REUSE", "1")
    assert provision.provision(TARGET, "opener") == wt
    assert [e["outcome"] for e in events()] == ["ok", "reused", "failed", "reused"]


def test_clone_failure_is_recorded(monkeypatch):
    def fail(repo):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(provision, "ensure_canonical", fail)
    with pytest.raises(RuntimeError, match="clone failed"):
        provision.provision(TARGET, "closer")
    assert len(events()) == 1
    assert events()[0]["outcome"] == "failed"
    assert events()[0]["cause"] == "provision-error"


def test_report_counts_only_recent_branch_failures(capsys):
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": (now - timedelta(days=age)).isoformat(), "outcome": outcome, "cause": cause}
        for age, outcome, cause in [
            (1, "failed", "branch-held"),
            (2, "failed", "branch-exists"),
            (3, "failed", "provision-error"),
            (1, "reclaimed", "branch-held"),
            (8, "failed", "branch-held"),
        ]
    ]
    provision.PROVISION_EVENTS_PATH.write_text("".join(json.dumps(row) + "\n" for row in rows))
    provision.main(["--report", "--days", "7"])
    out = capsys.readouterr().out
    assert "provision: failed(branch-held|branch-exists) = 2 over 7d" in out
    assert "provision: failed = 3" in out
    assert "provision: reclaimed(branch-held) = 1" in out


def test_report_cli_honors_temporary_ledger_env():
    result = subprocess.run(
        [sys.executable, str(Path(provision.__file__)), "--report", "--days", "2"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "unmeasured: no ledger" in result.stdout
    assert "over 2d" in result.stdout
