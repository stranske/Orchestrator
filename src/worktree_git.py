#!/usr/bin/env python3
"""worktree_git.py — the one way the tool runs git inside an agent's dispatch worktree (a rail).

WHY. Every dispatch worktree is a LINKED worktree of a canonical clone (`provision.py`). Its `.git`
is a file naming a private git dir, `<clone>/.git/worktrees/<name>`, and that dir holds the
`commondir` and `gitdir` pointer files git follows to find the repository. #452
(`adapters.codex_worktree_git_roots`) grants a committing codex run write access to that private
dir, so after such a run those pointer files are files the agent could have changed. The tool then
runs git there itself, outside any sandbox: `pushed_branches` at completion, `watch` on every
redirect sweep, `exp_abcd.collect`, `provision`'s reuse check and `adapters.dispatch`'s status.

THE RULE. Before git runs in a provisioned worktree (a direct child of `provision.WORKTREES_DIR`):

  * the worktree's `.git` must name a dir under `<REPOS_DIR>/<slug>/.git/worktrees/`, where
    `<slug>` is the clone the worktree's own name says it belongs to (`provision.canonical_path`);
  * that dir's `gitdir` must point back at this worktree's `.git`;
  * its `commondir` must resolve to `<REPOS_DIR>/<slug>/.git`;
  * it must hold no `config.worktree`.

Any mismatch REFUSES: git is not run, the result is a failed CompletedProcess whose stderr names the
file and what clears it, and the refusal is printed to stderr (the tick's and the dispatch
wrapper's existing logs capture it). When the check passes, git runs with GIT_DIR, GIT_COMMON_DIR
and GIT_WORK_TREE pinned to those known paths, so git reads no pointer file at all, and with
`-c core.fsmonitor=false -c core.hooksPath=/dev/null`. A path that is not a provisioned worktree
(an offload cwd, a local-verify checkout, a scratch clone) gets the `-c` flags only.

WHAT CLEARS A REFUSAL. Removing the worktree (`git -C <clone> worktree remove --force <wt>`, or
deleting it and `git worktree prune`) and letting `provision.provision` create it again. Nothing in
the refusal path writes the worktree, so that clear path is always open.

Kill switch: ORCH_WORKTREE_GIT_GUARD_DISABLED=1 runs `git -C <path> ...` exactly as before.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import provision

DISABLE_ENV = "ORCH_WORKTREE_GIT_GUARD_DISABLED"
# Disable the two configuration keys that make git run a program on a read-only command.
HARDENING_FLAGS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
# Exit status of a refused call. Distinct from git's own 128/129 so a reader of a log can tell them
# apart; every caller already treats any non-zero status as "git could not answer".
REFUSED_RC = 97
REFUSAL_PREFIX = "worktree_git refused"
_CLEAR = "remove the worktree and re-provision it (git worktree remove --force, then provision)"


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV) == "1"


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def is_provisioned(worktree: str | Path) -> bool:
    """Is `worktree` a direct child of the dispatch worktree dir (where every agent worktree lives)?"""
    try:
        return _real(worktree).parent == _real(provision.WORKTREES_DIR)
    except (OSError, ValueError):
        return False


def _read_pointer(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path} is missing or not a regular file")
    return path.read_text(encoding="utf-8").strip()


def expected_pins(worktree: str | Path, repo: str | None = None) -> tuple[Path, Path, Path] | str:
    """`(git_dir, common_dir, work_tree)` for a provisioned worktree, or the refusal reason.

    The common dir is never taken from `commondir`: it is derived from REPOS_DIR and the worktree's
    own name (or `repo` when the caller knows it), and `commondir` must merely agree with it.
    """
    wt = _real(worktree)
    repos = _real(provision.REPOS_DIR)
    dotgit = wt / ".git"
    try:
        key, colon, value = _read_pointer(dotgit).partition(":")
        if not colon or key.strip() != "gitdir" or not value.strip():
            return f"{dotgit} does not contain a 'gitdir:' line; {_CLEAR}"
        git_dir = _real(wt / value.strip())
        worktrees = git_dir.parent
        clone = worktrees.parent.parent
        if worktrees.name != "worktrees" or worktrees.parent.name != ".git" or clone.parent != repos:
            return (
                f"{dotgit} names {git_dir}, which is not under {repos}/<clone>/.git/worktrees/; "
                f"{_CLEAR}"
            )
        if repo is not None and clone != _real(provision.canonical_path(repo)):
            return f"{dotgit} names a git dir of {clone.name}, not of {repo}; {_CLEAR}"
        if not wt.name.startswith(clone.name + "__"):
            return f"{dotgit} names a git dir of {clone.name}, not of worktree {wt.name}; {_CLEAR}"
        if git_dir.is_symlink() or not git_dir.is_dir():
            return f"{git_dir} (named by {dotgit}) is not a directory; {_CLEAR}"
        common = clone / ".git"
        back = _real(Path(_read_pointer(git_dir / "gitdir")))
        if back != dotgit:
            return f"{git_dir / 'gitdir'} points at {back}, not {dotgit}; {_CLEAR}"
        named_common = _real(git_dir / _read_pointer(git_dir / "commondir"))
        if named_common != common:
            return f"{git_dir / 'commondir'} resolves to {named_common}, not {common}; {_CLEAR}"
        extra = git_dir / "config.worktree"
        if extra.exists() or extra.is_symlink():
            return f"{extra} exists; a dispatch worktree never has one; {_CLEAR}"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"cannot read the git pointer files of {wt}: {exc}; {_CLEAR}"
    return git_dir, common, wt


def hardened_argv(path: str | Path, args: list[str] | tuple[str, ...]) -> list[str]:
    """git argv for a path that is NOT a provisioned worktree: the `-c` hardening flags only."""
    if disabled():
        return ["git", "-C", str(path), *args]
    return ["git", *HARDENING_FLAGS, "-C", str(path), *args]


def plan(
    worktree: str | Path, args: list[str] | tuple[str, ...], *, repo: str | None = None
) -> tuple[list[str], dict[str, str] | None] | str:
    """The `(argv, env)` this module would run, or the refusal reason. env None = inherit."""
    if disabled() or not is_provisioned(worktree):
        return hardened_argv(worktree, args), None
    pins = expected_pins(worktree, repo)
    if isinstance(pins, str):
        return pins
    git_dir, common, wt = pins
    env = dict(os.environ)
    env.update(GIT_DIR=str(git_dir), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(wt))
    return ["git", *HARDENING_FLAGS, "-C", str(wt), *args], env


def run(
    worktree: str | Path,
    args: list[str] | tuple[str, ...],
    *,
    repo: str | None = None,
    text: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run git in `worktree`; a refused call returns REFUSED_RC and never starts git."""
    planned = plan(worktree, args, repo=repo)
    if isinstance(planned, str):
        reason = f"{REFUSAL_PREFIX}: {planned}"
        print(reason, file=sys.stderr)
        argv = ["git", "-C", str(worktree), *args]
        empty = "" if text else b""
        err = reason if text else reason.encode()
        return subprocess.CompletedProcess(argv, REFUSED_RC, stdout=empty, stderr=err)
    argv, env = planned
    return subprocess.run(argv, env=env, capture_output=True, text=text, timeout=timeout, check=False)


def _selftest() -> None:
    import tempfile
    from unittest import mock

    with tempfile.TemporaryDirectory() as td:
        root = _real(td)
        repos, wts = root / "repos", root / "worktrees"
        clone = repos / "o__r"
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "-c", "user.email=t@example.test", "-c", "user.name=T"]
            + ["commit", "-q", "--allow-empty", "-m", "base"],
            check=True,
        )
        wts.mkdir()
        wt = wts / "o__r__1__opener"
        subprocess.run(
            ["git", "-C", str(clone), "worktree", "add", "-q", "-b", "b1", str(wt)], check=True
        )
        with mock.patch.object(provision, "REPOS_DIR", repos), mock.patch.object(
            provision, "WORKTREES_DIR", wts
        ):
            ok = run(wt, ["rev-parse", "--is-inside-work-tree"])
            assert ok.returncode == 0 and ok.stdout.strip() == "true", ok
            commondir = clone / ".git" / "worktrees" / wt.name / "commondir"
            original = commondir.read_text()
            commondir.write_text(str(root) + "\n")
            refused = run(wt, ["status"])
            assert refused.returncode == REFUSED_RC and "commondir" in refused.stderr, refused
            commondir.write_text(original)
            assert run(wt, ["status"]).returncode == 0
    print("worktree_git.py selftest: OK (pinned run, tampered commondir refused, restore clears)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
