"""The tool's own git reads in an agent's worktree use repository locations it already knows (2026-10-11).

THE RESIDUE. #452 (`adapters.codex_worktree_git_roots`) grants a committing codex run its linked
worktree's private git dir, `<clone>/.git/worktrees/<name>`, and with it the `commondir` and
`gitdir` pointer files git follows to find the repository. The tool later runs git in that worktree
itself, outside any sandbox (push record, redirect sweep, experiment collect, reuse check, dispatch
status), so those reads must not depend on files the agent could change.

THE RULE. `worktree_git.run` checks the pointer files against the locations derived from
`provision` (REPOS_DIR, or `canonical_path(repo)` when the caller knows the repo) and refuses on a
mismatch, without starting git. When they agree it pins GIT_DIR / GIT_COMMON_DIR / GIT_WORK_TREE
and disables `core.fsmonitor` and `core.hooksPath`. These tests assert refusal and argv/env only;
nothing here configures git to run a program.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest

import adapters
import exp_abcd
import provision
import pushed_branches
import watch
import worktree_git


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    """A canonical clone under REPOS_DIR and one dispatch worktree under WORKTREES_DIR."""
    monkeypatch.delenv(worktree_git.DISABLE_ENV, raising=False)
    root = Path(os.path.realpath(tmp_path))
    repos, wts = root / "repos", root / "worktrees"
    clone = repos / "o__r"
    _git("init", "-q", str(clone))
    _git(
        "-C",
        str(clone),
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    wts.mkdir()
    wt = wts / "o__r__1__opener"
    _git("-C", str(clone), "worktree", "add", "-q", "-b", "b1", str(wt))
    (wt / "note.txt").write_text("x\n")
    monkeypatch.setattr(provision, "REPOS_DIR", repos)
    monkeypatch.setattr(provision, "WORKTREES_DIR", wts)
    private = clone / ".git" / "worktrees" / wt.name
    return {"root": root, "clone": clone, "wt": wt, "private": private}


@pytest.fixture()
def no_git(monkeypatch):
    """Fail the test if worktree_git starts any process."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"git was invoked on a refused worktree: {args}")

    monkeypatch.setattr(worktree_git.subprocess, "run", refuse)
    return calls


def _tamper_commondir(lay):
    (lay["private"] / "commondir").write_text(str(lay["root"]) + "\n")
    return "commondir"


def _tamper_gitdir(lay):
    (lay["private"] / "gitdir").write_text(str(lay["root"] / "elsewhere" / ".git") + "\n")
    return "gitdir"


def _tamper_config_worktree(lay):
    (lay["private"] / "config.worktree").write_text("")
    return "config.worktree"


def _tamper_dotgit(lay):
    (lay["wt"] / ".git").write_text(f"gitdir: {lay['root'] / 'other' / '.git'}\n")
    return ".git"


TAMPERS = {
    "wrong_commondir": _tamper_commondir,
    "wrong_gitdir": _tamper_gitdir,
    "extra_config_worktree": _tamper_config_worktree,
    "dotgit_outside_clone": _tamper_dotgit,
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_a_tampered_pointer_file_is_refused_without_running_git(layout, no_git, tamper):
    named = TAMPERS[tamper](layout)
    result = worktree_git.run(layout["wt"], ["status", "--porcelain"])
    assert result.returncode == worktree_git.REFUSED_RC
    assert result.stdout == ""
    assert result.stderr.startswith(worktree_git.REFUSAL_PREFIX)
    assert named in result.stderr, result.stderr
    assert "re-provision" in result.stderr, result.stderr
    assert no_git == []


def test_the_clean_case_returns_what_plain_git_returned(layout):
    wt = layout["wt"]
    for args in (["rev-parse", "HEAD"], ["status", "--porcelain"], ["reflog", "show", "HEAD"]):
        plain = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True)
        guarded = worktree_git.run(wt, args)
        assert guarded.returncode == plain.returncode == 0, guarded.stderr
        assert guarded.stdout == plain.stdout


def test_the_argv_and_env_carry_the_pins_and_the_flags(layout, monkeypatch):
    seen = {}

    def spy(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(worktree_git.subprocess, "run", spy)
    worktree_git.run(layout["wt"], ["status", "--porcelain"])
    argv, env = seen["argv"], seen["env"]
    assert argv[0] == "git"
    assert argv[1:5] == list(worktree_git.HARDENING_FLAGS)
    assert "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv
    assert argv[-2:] == ["status", "--porcelain"]
    assert env["GIT_DIR"] == str(layout["private"])
    assert env["GIT_COMMON_DIR"] == str(layout["clone"] / ".git")
    assert env["GIT_WORK_TREE"] == str(layout["wt"])


def test_a_known_repo_is_checked_against_its_canonical_clone(layout, monkeypatch, no_git):
    monkeypatch.setattr(provision, "canonical_path", lambda _repo: layout["root"] / "other")
    result = worktree_git.run(layout["wt"], ["status"], repo="o/r")
    assert result.returncode == worktree_git.REFUSED_RC
    assert "not of" in result.stderr


def test_a_path_that_is_not_a_dispatch_worktree_gets_the_flags_only(layout):
    plain = layout["root"] / "scratch"
    argv, env = worktree_git.plan(plain, ["status"])
    assert env is None
    assert argv == ["git", *worktree_git.HARDENING_FLAGS, "-C", str(plain), "status"]
    assert worktree_git.hardened_argv(plain, ["status"]) == argv


def test_the_kill_switch_restores_the_old_command(layout, monkeypatch):
    _tamper_commondir(layout)
    monkeypatch.setenv(worktree_git.DISABLE_ENV, "1")
    argv, env = worktree_git.plan(layout["wt"], ["status", "--porcelain"])
    assert argv == ["git", "-C", str(layout["wt"]), "status", "--porcelain"]
    assert env is None
    assert worktree_git.hardened_argv(layout["wt"], ["status"]) == [
        "git",
        "-C",
        str(layout["wt"]),
        "status",
    ]


def test_the_call_sites_refuse_a_tampered_worktree(layout, no_git):
    _tamper_commondir(layout)
    wt = layout["wt"]
    assert pushed_branches._git(wt, "rev-parse", "HEAD").returncode == worktree_git.REFUSED_RC
    assert watch._git(wt, ["status"]).returncode == worktree_git.REFUSED_RC
    assert watch._is_git_worktree(wt) is False
    reason = provision._existing_worktree_reuse_error(wt)
    assert reason and worktree_git.REFUSAL_PREFIX in reason


def test_dispatch_status_and_experiment_collect_route_through_the_guard():
    # Wiring pins: the smallest fragment that is absent if the routing is removed, each unique.
    dispatch_src = inspect.getsource(adapters.dispatch)
    assert dispatch_src.count("worktree_git" + ".run(cwd") == 1
    assert '"git", "-C", cwd, "status"' not in dispatch_src
    collect_src = inspect.getsource(exp_abcd.collect)
    assert collect_src.count('"merge-base", "HEAD", base_ref], repo' + "=repo") == 1
    assert '"git", "-C", str(wt)' not in collect_src
