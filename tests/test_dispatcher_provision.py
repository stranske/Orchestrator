"""Offline branch-policy wiring and real disposable-git stale-worktree recovery."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import dispatcher
import provision


@pytest.fixture
def dispatch_world(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(dispatcher.provision, "worktree_path", lambda *_: workspace)
    monkeypatch.setattr(dispatcher.repo_knowledge, "append_context", lambda prompt, *a, **k: prompt)
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *_: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "workspace_path", lambda path: path)
    monkeypatch.setattr(dispatcher.adapters, "broad_workspace_reason", lambda *_: None)
    monkeypatch.setattr(
        dispatcher.adapters, "build_command", lambda agent, prompt, *a, **k: [agent, prompt]
    )
    import roles

    monkeypatch.setattr(roles, "activate_dispatch_roles", lambda a, prompt, **k: {"prompt": prompt})
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(dispatcher.claims, "claim", lambda *_: True)
    monkeypatch.setattr(dispatcher.claims, "release", lambda *_: None)

    def spawn(plan):
        plan["run_id"] = "test-run"
        return 123

    monkeypatch.setattr(dispatcher, "_spawn", spawn)
    monkeypatch.setattr(dispatcher, "_delegate_lane_heartbeat", lambda _: None)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    return workspace


@pytest.mark.parametrize("repo", ["stranske/Workflows", "stranske/Orchestrator"])
@pytest.mark.parametrize("agent", ["codex", "gemini", "cursor"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_infra_prefix(dispatch_world, monkeypatch, repo, agent, dry_run):
    calls = []
    monkeypatch.setattr(provision, "provision", lambda *a: calls.append(a) or dispatch_world)
    plan = dispatcher.plan_dispatch(
        {"agent": agent, "target": f"{repo}#7", "task_type": "implement", "prompt": "Do work"},
        dry_run=dry_run,
    )
    assert "error" not in plan, plan
    assert plan["branch"] == "orch/issue-7"
    assert "default `orch/issue-7`" in plan["argv"][1]
    assert calls == ([] if dry_run else [(f"{repo}#7", "opener", "orch/issue-7")])


@pytest.mark.parametrize("agent", ["codex", "gemini", "cursor", "claude", "vibe", "aider"])
def test_product_prefix(dispatch_world, monkeypatch, agent):
    calls = []
    monkeypatch.setattr(provision, "provision", lambda *a: calls.append(a) or dispatch_world)
    plan = dispatcher.plan_dispatch(
        {"agent": agent, "target": "stranske/Product#7", "task_type": "implement", "prompt": "Work"}
    )
    assert "error" not in plan, plan
    assert plan["branch"] == f"{agent}/issue-7"
    assert calls == [("stranske/Product#7", "opener", f"{agent}/issue-7")]


@pytest.mark.parametrize("repo", ["stranske/Orchestrator", "stranske/Product"])
@pytest.mark.parametrize("prefix", ["custom", "custom/", "team/custom/"])
def test_override_honoured(dispatch_world, monkeypatch, repo, prefix):
    calls = []
    monkeypatch.setattr(provision, "provision", lambda *a: calls.append(a) or dispatch_world)
    out = dispatcher.delegate("cursor", f"{repo}#7", "opener", "Work", branch_prefix=prefix)
    assert "error" not in out, out
    assert calls == [(f"{repo}#7", "opener", f"{prefix.rstrip('/')}/issue-7")]


def test_cli_override_reaches_delegate(monkeypatch, capsys):
    seen = {}

    def delegate(*args, **kwargs):
        seen.update(kwargs)
        return {"pid": 123}

    monkeypatch.setattr(dispatcher, "delegate", delegate)
    assert (
        dispatcher.main(
            [
                "delegate",
                "--agent",
                "cursor",
                "--target",
                "stranske/Workflows#7",
                "--prompt",
                "Work",
                "--branch-prefix",
                "custom/",
            ]
        )
        == 0
    )
    assert seen["branch_prefix"] == "custom/"
    assert '"pid": 123' in capsys.readouterr().out


@pytest.mark.parametrize("prefix", ["", "/", "../bad", "bad ref", "-b/other", "refs//bad"])
def test_invalid_prefix_never_provisions(dispatch_world, monkeypatch, prefix):
    def forbidden(*args):
        pytest.fail("invalid prefix reached provisioning")

    monkeypatch.setattr(provision, "provision", forbidden)
    plan = dispatcher.plan_dispatch(
        {
            "agent": "cursor",
            "target": "stranske/Workflows#7",
            "task_type": "implement",
            "branch_prefix": prefix,
        }
    )
    assert "branch prefix" in plan["error"]


def test_closer_keeps_pr_branch(dispatch_world, monkeypatch):
    calls = []
    monkeypatch.setattr(provision, "provision", lambda *a: calls.append(a) or dispatch_world)
    plan = dispatcher.plan_dispatch(
        {
            "agent": "cursor",
            "target": "stranske/Workflows#7",
            "lane": "closer",
            "task_type": "review",
            "prompt": "Review",
            "branch_prefix": "custom",
        }
    )
    assert "error" not in plan, plan
    assert calls == [("stranske/Workflows#7", "closer")]
    assert plan["branch"] is None
    assert "BRANCH POLICY" not in plan["argv"][1]


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    ).stdout.strip()


@pytest.fixture
def git_world(tmp_path, monkeypatch):
    canon = tmp_path / "repo"
    canon.mkdir()
    git(canon, "init", "-b", "main")
    git(
        canon,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    git(canon, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(canon, "remote", "add", "origin", str(canon))
    monkeypatch.setattr(provision, "ensure_canonical", lambda _: canon)
    monkeypatch.setattr(provision, "base_branch", lambda _: "main")
    monkeypatch.setattr(provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.delenv("ORCH_ALLOW_STALE_WORKTREE_REUSE", raising=False)
    return canon


def test_stale_branch_retry_preserves_commits(git_world, capsys):
    target, branch = "stranske/Workflows#7", "orch/issue-7"
    wt = provision.provision(target, "opener", branch)
    git(
        wt,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "finished work",
    )
    head = git(wt, "rev-parse", "HEAD")
    shutil.rmtree(wt)  # Finished run removed the directory, leaving its registration and branch.
    assert provision.provision(target, "opener", branch) == wt
    assert git(wt, "branch", "--show-current") == branch
    assert git(wt, "rev-parse", "HEAD") == head
    event = json.loads(provision.PROVISION_EVENTS_PATH.read_text().splitlines()[-1])
    assert event["outcome"] == "reused" and event["cause"] == "branch-exists"


def test_live_branch_is_not_reset_or_stolen(git_world, monkeypatch):
    branch = "orch/issue-7"
    live = git_world.parent / "live"
    git(git_world, "worktree", "add", "-b", branch, str(live), "main")
    head = git(live, "rev-parse", "HEAD")
    monkeypatch.setattr(provision, "_live_holder_pids", lambda _: {"4242"})
    with pytest.raises(RuntimeError, match="live pid 4242"):
        provision.provision("stranske/Workflows#7", "opener", branch)
    assert git(live, "rev-parse", "HEAD") == head
    assert git(live, "branch", "--show-current") == branch


def test_existing_mismatched_worktree_is_refused(git_world):
    target = "stranske/Workflows#7"
    wt = provision.provision(target, "opener", "codex/issue-7")
    with pytest.raises(RuntimeError, match="does not match"):
        provision.provision(target, "opener", "orch/issue-7")
    assert git(wt, "branch", "--show-current") == "codex/issue-7"


@pytest.mark.parametrize("detail", ["permission denied", "invalid reference: origin/main"])
def test_unrelated_failure_does_not_retry(tmp_path, monkeypatch, detail):
    calls = []

    def fail(args):
        calls.append(args)
        raise subprocess.CalledProcessError(128, args, stderr=detail)

    monkeypatch.setattr(provision, "_run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        provision._add_opener_worktree(tmp_path, tmp_path / "wt", "orch/issue-7", "main")
    assert len(calls) == 1


def test_retry_is_bounded(tmp_path, monkeypatch, capsys):
    calls = []

    def run(args):
        calls.append(args)
        if "add" in args:
            raise subprocess.CalledProcessError(
                128, args, stderr="fatal: a branch named 'orch/issue-7' already exists"
            )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(provision, "_run", run)
    with pytest.raises(subprocess.CalledProcessError):
        provision._add_opener_worktree(tmp_path, tmp_path / "wt", "orch/issue-7", "main")
    assert len(calls) == 3  # initial add, prune, single retry
    assert calls[1][-4:] == ["worktree", "prune", "--expire", "now"]
    assert "-b" not in calls[2] and "-B" not in calls[2]
    assert capsys.readouterr().err.count("retrying once") == 1
