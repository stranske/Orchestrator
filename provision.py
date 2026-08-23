#!/usr/bin/env python3
"""provision.py — ensure a LOCAL-DISK git worktree for a target so the dispatcher's
agent has a real checkout to work in AND can push.

CRITICAL (lane memory, 2026-06): `git worktree add` and `git push` from the Dropbox-backed
canonical checkouts (~/Library/CloudStorage/Dropbox/.../Code/<Repo>) FAIL —
  - worktree add: ".git/worktrees/... Operation not permitted" (Dropbox rejects the write)
  - push:        "mmap failed: Resource deadlock avoided" (can't mmap the Dropbox objects)
So we keep a LOCAL-disk canonical clone per repo under ~/.codex/orchestrator/repos/ (normal
fs) and add worktrees off THAT — both worktree add and push work on normal fs.

  - closer target (PR repo#N): worktree on the PR's head branch (push updates the PR).
  - opener target (issue repo#N): worktree on a NEW branch off the base
    (phase-3 for Trend_Model_Project; the repo's default branch otherwise).

Pure helpers are selftested offline; `--smoke owner/repo#N opener|closer` provisions one
target live (clone + worktree) to verify the normal-fs path works, then cleans up.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parent
# Git checkouts MUST live on LOCAL disk: Dropbox rejects `git worktree add` ("Operation not permitted")
# and `git push` deadlocks ("mmap: Resource deadlock avoided"). The CODE now lives in the Dropbox
# Code/Orchestrator project dir, but these default to a fixed LOCAL runtime path regardless. Env-overridable.
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator"))
REPOS_DIR = Path(os.environ.get("ORCH_REPOS_DIR", LOCAL_RUNTIME / "repos"))            # local canonical clones
WORKTREES_DIR = Path(os.environ.get("ORCH_WORKTREE_BASE", LOCAL_RUNTIME / "worktrees"))  # per-target worktrees

# Base branch a NEW opener branch is cut from, when it must NOT be the repo's default branch.
# The Trend pin is currently a NO-OP and is kept deliberately: `phase-3` IS that repo's default
# branch (`gh repo view stranske/Trend_Model_Project --json defaultBranchRef` -> phase-3), so the
# override and the fallback agree today. The old comment here said Trend "uses phase-3" as though
# that were distinct from the default, and the repo playbook had copied the same wrong belief as
# "Trend opener work cuts from phase-3, not the default branch" -- which sent readers looking for a
# `main` that does not exist. Keeping the pin means the base stays phase-3 if the default ever moves.
# repo_knowledge's SEED reads this dict rather than repeating the branch name; a matching pair of
# literals would be free to drift, one name cannot.
BASE_BRANCH_OVERRIDES = {"stranske/Trend_Model_Project": "phase-3"}


def parse_target(target: str) -> tuple[str, int | None]:
    """'stranske/Repo#123' -> ('stranske/Repo', 123); 'stranske/Repo' -> (..., None)."""
    if "#" in target:
        repo, num = target.split("#", 1)
        return repo.strip(), (int(num) if num.strip().isdigit() else None)
    return target.strip(), None


def repo_slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", repo.replace("/", "__"))


def worktree_name(target: str, lane: str) -> str:
    repo, num = parse_target(target)
    return f"{repo_slug(repo)}__{num if num is not None else 'repo'}__{lane}"


def worktree_path(target: str, lane: str) -> Path:
    return WORKTREES_DIR / worktree_name(target, lane)


def canonical_path(repo: str) -> Path:
    return REPOS_DIR / repo_slug(repo)


# --- live git/gh ops --------------------------------------------------------
def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, check=check)


def _existing_worktree_reuse_error(wt: Path) -> str | None:
    """Return a reason an existing worktree is unsafe to reuse for a fresh dispatch."""
    status = _run(["git", "-C", str(wt), "status", "--porcelain"], check=False)
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "git status failed").strip()
        return f"existing worktree status failed: {detail[:300]}"
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        preview = "; ".join(changed[:5])
        suffix = "..." if len(changed) > 5 else ""
        return f"existing worktree has uncommitted changes: {preview}{suffix}"

    upstream = _run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        check=False,
    ).stdout.strip()
    if not upstream:
        return None
    counts = _run(
        ["git", "-C", str(wt), "rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
        check=False,
    )
    if counts.returncode != 0:
        detail = (counts.stderr or counts.stdout or "rev-list failed").strip()
        return f"existing worktree divergence check failed: {detail[:300]}"
    try:
        behind, ahead = [int(part) for part in counts.stdout.split()[:2]]
    except Exception:
        return f"existing worktree divergence check returned unexpected output: {counts.stdout.strip()!r}"
    if ahead or behind:
        return f"existing worktree diverges from {upstream}: ahead={ahead} behind={behind}"
    return None


def default_branch(repo: str) -> str:
    try:
        out = _run(["gh", "repo", "view", repo, "--json", "defaultBranchRef",
                    "-q", ".defaultBranchRef.name"], check=False).stdout.strip()
        return out or "main"
    except Exception:
        return "main"


def base_branch(repo: str) -> str:
    return BASE_BRANCH_OVERRIDES.get(repo) or default_branch(repo)


def ensure_canonical(repo: str) -> Path:
    """Clone the repo to a local-disk canonical mirror (once), else fetch --prune."""
    path = canonical_path(repo)
    if (path / ".git").is_dir():
        _run(["git", "-C", str(path), "fetch", "--prune", "origin"], check=False)
        return path
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    _run(["gh", "repo", "clone", repo, str(path)])     # uses gh auth; normal-fs => push works
    return path


def _pr_head_branch(repo: str, num: int) -> str:
    out = _run(["gh", "pr", "view", str(num), "-R", repo, "--json", "headRefName",
                "-q", ".headRefName"], check=False).stdout.strip()
    if not out:
        raise RuntimeError(f"could not resolve head branch for {repo}#{num}")
    return out


def provision(target: str, lane: str) -> Path:
    """Ensure a local-disk worktree for the target; return its path (idempotent)."""
    repo, num = parse_target(target)
    canon = ensure_canonical(repo)
    wt = worktree_path(target, lane)
    if (wt / ".git").exists():
        if os.environ.get("ORCH_ALLOW_STALE_WORKTREE_REUSE") == "1":
            return wt
        reuse_error = _existing_worktree_reuse_error(wt)
        if reuse_error:
            raise RuntimeError(
                f"refusing to reuse stale worktree for {target} ({lane}): "
                f"{reuse_error}; inspect or remove {wt}"
            )
        return wt                                      # already provisioned and clean
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    if lane == "closer" and num is not None:
        head = _pr_head_branch(repo, num)
        _run(["git", "-C", str(canon), "fetch", "origin", head], check=False)
        # worktree tracking origin/<head> so the agent's push updates the PR
        _run(["git", "-C", str(canon), "worktree", "add", "--checkout", "-B", head,
              str(wt), f"origin/{head}"])
    else:  # opener (issue) or repo-level: new branch off the base
        base = base_branch(repo)
        branch = f"orchestrator/issue-{num}" if num is not None else f"orchestrator/{lane}"
        _run(["git", "-C", str(canon), "fetch", "origin", base], check=False)
        _run(["git", "-C", str(canon), "worktree", "add", "-b", branch,
              str(wt), f"origin/{base}"])
    return wt


# ---------------------------------------------------------------------------
def _selftest() -> None:
    assert parse_target("stranske/Repo#123") == ("stranske/Repo", 123)
    assert parse_target("stranske/Repo") == ("stranske/Repo", None)
    assert parse_target("o/r#abc") == ("o/r", None)              # non-numeric => None
    assert repo_slug("stranske/Trend_Model_Project") == "stranske__Trend_Model_Project"
    assert worktree_name("stranske/Counter_Risk#42", "closer") == "stranske__Counter_Risk__42__closer"
    assert worktree_name("stranske/Workflows#7", "opener") == "stranske__Workflows__7__opener"
    assert worktree_path("o/r#1", "closer").name == "o__r__1__closer"
    assert canonical_path("stranske/Counter_Risk").name == "stranske__Counter_Risk"
    assert _existing_worktree_reuse_error(Path("/definitely/not/a/worktree")) is not None
    # base-branch override is pure (no network); Trend => phase-3
    assert BASE_BRANCH_OVERRIDES["stranske/Trend_Model_Project"] == "phase-3"
    assert base_branch.__name__ == "base_branch"   # the override is consulted before gh
    print("provision.py selftest: OK (target parse, slug, worktree naming/paths, "
          "Trend phase-3 base override) — live clone/worktree covered by --smoke")


def _smoke(target: str, lane: str) -> None:
    """Provision one target live, prove the worktree is a real git dir on normal fs, clean up."""
    repo, num = parse_target(target)
    print(f"smoke: provisioning {target} ({lane})")
    wt = provision(target, lane)
    assert (wt / ".git").exists(), f"worktree not created: {wt}"
    # prove it's a usable, writable, normal-fs checkout (git status works; not on Dropbox)
    st = _run(["git", "-C", str(wt), "status", "--porcelain"], check=False)
    assert st.returncode == 0, f"git status failed in worktree: {st.stderr}"
    assert "CloudStorage/Dropbox" not in str(wt.resolve()), "worktree must be on local disk, not Dropbox"
    branch = _run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip()
    print(f"  OK worktree={wt}")
    print(f"  branch={branch}  (normal-fs: worktree add succeeded — no Dropbox 'Operation not permitted')")
    # cleanup so the smoke leaves no junk branch/worktree
    canon = canonical_path(repo)
    _run(["git", "-C", str(canon), "worktree", "remove", "--force", str(wt)], check=False)
    if lane != "closer" and num is not None:
        _run(["git", "-C", str(canon), "branch", "-D", f"orchestrator/issue-{num}"], check=False)
    print("  cleaned up (worktree removed, opener branch deleted)")


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "--smoke":
        _smoke(argv[1], argv[2] if len(argv) > 2 else "opener")
        return 0
    if argv and argv[0] == "provision":
        print(provision(argv[1], argv[2] if len(argv) > 2 else "opener"))
        return 0
    print("usage: provision.py --selftest | --smoke <owner/repo#N> <opener|closer> | provision <target> <lane>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
