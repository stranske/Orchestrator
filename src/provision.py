#!/usr/bin/env python3
"""provision.py — ensure a LOCAL-DISK git worktree for a target so the dispatcher's
agent has a real checkout to work in AND can push.

CRITICAL (lane memory, 2026-06): `git worktree add` and `git push` from the Dropbox-backed
canonical checkouts (~/Library/CloudStorage/Dropbox/.../Code/<Repo>) FAIL —
  - worktree add: ".git/worktrees/... Operation not permitted" (Dropbox rejects the write)
  - push:        "mmap failed: Resource deadlock avoided" (can't mmap the Dropbox objects)
So we keep a LOCAL-disk canonical clone per repo under ~/.codex/orchestrator/repos/ (normal
fs) and add worktrees off THAT — both worktree add and push work on normal fs.

A linked worktree's git dir lives in that clone, outside the worktree, and codex >= 0.158 refuses
writes there under `--sandbox workspace-write`. A codex run that commits is therefore granted
exactly that dir plus the clone's objects/, refs/ and logs/ (adapters.codex_worktree_git_roots).
The clone's config and hooks stay read-only. A standalone clone per target would not avoid it:
codex protects a clone's own `.git` too, and only a grant of all of it would let the run commit.

  - closer target (PR repo#N): worktree on the PR's head branch (push updates the PR).
  - opener target (issue repo#N): worktree on a NEW branch off the repo's live default branch,
    unless that repo has an explicit non-default base override.

Pure helpers are selftested offline; `--smoke owner/repo#N opener|closer` provisions one
target live (clone + worktree) to verify the normal-fs path works, then cleans up.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ORCH = Path(__file__).resolve().parent
# Git checkouts MUST live on LOCAL disk: Dropbox rejects `git worktree add` ("Operation not permitted")
# and `git push` deadlocks ("mmap: Resource deadlock avoided"). The CODE now lives in the Dropbox
# Code/Orchestrator project dir, but these default to a fixed LOCAL runtime path regardless. Env-overridable.
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator"))
REPOS_DIR = Path(
    os.environ.get("ORCH_REPOS_DIR", LOCAL_RUNTIME / "repos")
)  # local canonical clones
WORKTREES_DIR = Path(
    os.environ.get("ORCH_WORKTREE_BASE", LOCAL_RUNTIME / "worktrees")
)  # per-target worktrees
PROVISION_EVENTS_PATH = Path(
    os.environ.get("ORCH_PROVISION_EVENTS_PATH", LOCAL_RUNTIME / "provision-events.ndjson")
)

# Base branch a NEW opener branch is cut from only when it intentionally differs from the repo's
# default. Repos that follow their default must stay absent: default_branch() queries GitHub, while
# a redundant registration can outlive a branch rename and make every opener worktree fail.
BASE_BRANCH_OVERRIDES: dict[str, str] = {}


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
def _run(
    args: list[str], cwd: Path | None = None, check: bool = True, timeout: int | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _wt_git(wt: Path, args: list[str]) -> subprocess.CompletedProcess:
    """git in an existing dispatch worktree, whose git dir an earlier agent could write (#452)."""
    import worktree_git  # lazy: worktree_git imports this module

    return worktree_git.run(wt, args)


def _existing_worktree_reuse_error(wt: Path) -> str | None:
    """Return a reason an existing worktree is unsafe to reuse for a fresh dispatch."""
    status = _wt_git(wt, ["status", "--porcelain"])
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "git status failed").strip()
        return f"existing worktree status failed: {detail[:300]}"
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        preview = "; ".join(changed[:5])
        suffix = "..." if len(changed) > 5 else ""
        return f"existing worktree has uncommitted changes: {preview}{suffix}"

    upstream = _wt_git(
        wt, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    ).stdout.strip()
    if not upstream:
        return None
    counts = _wt_git(wt, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
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
        out = _run(
            [
                "gh",
                "repo",
                "view",
                repo,
                "--json",
                "defaultBranchRef",
                "-q",
                ".defaultBranchRef.name",
            ],
            check=False,
        ).stdout.strip()
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
    _run(["gh", "repo", "clone", repo, str(path)])  # uses gh auth; normal-fs => push works
    return path


def exact_head_worktree(target: str, head: str, *, purpose: str = "panel") -> str:
    """Share exact-commit provisioning without sharing a caller's mutable branch tree."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head or ""):
        raise RuntimeError("exact checkout requires a full commit SHA")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", purpose):
        raise RuntimeError("invalid exact checkout purpose")
    repo, num = parse_target(target)
    canon = ensure_canonical(repo)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    wt = WORKTREES_DIR / f"{purpose}-{repo_slug(repo)}-{num}-{head[:12]}"
    if wt.exists():
        current = _run(["git", "-C", str(wt), "rev-parse", "HEAD"], check=False, timeout=60)
        status = _run(
            ["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"],
            check=False,
            timeout=60,
        )
        if (
            current.returncode == status.returncode == 0
            and current.stdout.strip() == head
            and not status.stdout.strip()
        ):
            return str(wt)
        raise RuntimeError("existing exact worktree is dirty or at another head; refusing reuse")
    fetched = _run(
        ["git", "-C", str(canon), "fetch", "-q", "origin", head], check=False, timeout=60
    )
    if fetched.returncode != 0:
        raise RuntimeError("cannot fetch exact checkout commit")
    added = _run(
        ["git", "-C", str(canon), "worktree", "add", "-q", "--detach", str(wt), head],
        check=False,
        timeout=60,
    )
    if added.returncode != 0:
        raise RuntimeError(
            f"cannot check out {head[:12]} for {target}: {added.stderr.strip()[:300]}"
        )
    return str(wt)


@contextmanager
def temporary_exact_head_worktree(target: str, head: str):
    """Remove only this invocation's disposable checkout, including test-created caches."""
    wt = exact_head_worktree(target, head, purpose=f"runtime-ac-{uuid.uuid4().hex}")
    try:
        yield wt
    finally:
        body_error = sys.exc_info()[1]
        canon = canonical_path(parse_target(target)[0])
        removed = _run(
            ["git", "-C", str(canon), "worktree", "remove", "--force", wt],
            check=False,
            timeout=60,
        )
        prune_error = ""
        if removed.returncode != 0:
            shutil.rmtree(wt, ignore_errors=True)
            pruned = _run(["git", "-C", str(canon), "worktree", "prune"], check=False, timeout=60)
            if pruned.returncode != 0:
                prune_error = f"git worktree prune failed: {pruned.stderr.strip()[:300]}"
        if Path(wt).exists() or prune_error:
            cleanup_err = RuntimeError(
                f"cannot remove runtime-AC checkout {wt}: "
                f"{prune_error or removed.stderr.strip()[:300]}"
            )
            # Retain both failures rather than silently hiding cleanup behind verification.
            raise cleanup_err from body_error


def _pr_head_branch(repo: str, num: int) -> str:
    out = _run(
        ["gh", "pr", "view", str(num), "-R", repo, "--json", "headRefName", "-q", ".headRefName"],
        check=False,
    ).stdout.strip()
    if not out:
        raise RuntimeError(f"could not resolve head branch for {repo}#{num}")
    return out


class ProbeBlindError(RuntimeError):
    """Process inspection cannot establish its own visibility."""


def _pgrep_base() -> list[str]:
    """BSD/macOS pgrep excludes itself AND all of its ancestors unless `-a` is given, so without it
    the self-visibility check can never see the calling process and every reclaim is refused as
    probe-blind on the machine the dispatcher runs on. Linux procps pgrep uses `-a` for "print the
    full command line" instead, which would break the pid parsing, so add it only on Darwin."""
    return ["pgrep", "-a"] if sys.platform == "darwin" else ["pgrep"]


def _live_holder_pids(holder: Path) -> set[str]:
    """Inspect cwd and argv using canonical path components, never string prefixes."""
    root = Path(os.path.realpath(holder))
    roots = {holder, root}

    def within(path: str) -> bool:
        candidate = Path(os.path.realpath(path))
        return candidate == root or root in candidate.parents

    def probe(args: list[str], *, no_matches: bool = False) -> str:
        try:
            result = _run(args, check=False)
        except OSError as exc:
            raise RuntimeError(
                f"cannot check live processes for {holder}: {args[0]}: {exc}"
            ) from exc
        # lsof/pgrep use 1 for no matches; diagnostics make that an inspection failure.
        if result.returncode != 0 and not (
            no_matches
            and result.returncode == 1
            and not result.stdout.strip()
            and not result.stderr.strip()
        ):
            raise RuntimeError(
                f"cannot check live processes for {holder}: {args[0]} "
                f"exited {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout

    def require_visible(visible: bool, mechanism: str) -> None:
        if not visible:
            raise ProbeBlindError(
                f"cannot verify that no process holds {holder}: "
                f"process listing cannot see this process ({mechanism})"
            )

    self_pid = str(os.getpid())
    cwd_visible = False
    pids = set()
    # One scan for all processes, on macOS and Linux; -F also emits p<pid> records.
    pid = None
    for line in probe(["lsof", "-a", "-d", "cwd", "-Fn"], no_matches=True).splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            if pid == self_pid and Path(os.path.realpath(line[1:])) == Path.cwd().resolve():
                cwd_visible = True
            if within(line[1:]):
                pids.add(pid)
    if pids:
        return pids
    # pgrep is only a candidate search: its regex also finds sibling prefixes such
    # as wt__10 for wt__1. Validate every candidate through the argv scan below.
    # Scan all ps rows as well, since aliases need not contain either holder spelling.
    for spelling in sorted(roots):
        probe([*_pgrep_base(), "-f", re.escape(str(spelling))], no_matches=True)
    argv_visible = False
    # -ww prevents terminal/pipe width from silently cutting off path arguments.
    for line in probe(["ps", "-axww", "-o", "pid=,command="]).splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid, command = fields
        if pid == self_pid and command.strip():
            argv_visible = True
        try:
            args = shlex.split(command)
        except ValueError:
            args = command.split()
        # ps does not quote argv elements containing spaces. Preserve literal holder
        # spellings too, with component boundaries so wt__1 never matches wt__10.
        literal_holder = any(
            re.search(r"(?:^|[\s=\"'])" + re.escape(str(spelling)) + r"(?=$|[\s/\"'])", command)
            for spelling in roots
        )
        if literal_holder or any(within(arg.split("=", 1)[-1]) for arg in args if "/" in arg):
            pids.add(pid)
    if not pids:
        require_visible(cwd_visible, "lsof")
        require_visible(
            self_pid in probe([*_pgrep_base(), "-f", "."], no_matches=True).split(), "pgrep"
        )
        require_visible(argv_visible, "ps")
    return pids


def _reclaim_branch_holder(canon: Path, branch: str, wt: Path, event: dict) -> None:
    """Remove only a clean, idle holder of this branch, never the requested checkout."""
    _run(["git", "-C", str(canon), "worktree", "prune", "--expire", "now"])
    listed = _run(["git", "-C", str(canon), "worktree", "list", "--porcelain", "-z"])
    for record in listed.stdout.split("\0\0"):
        fields = record.split("\0")
        if f"branch refs/heads/{branch}" not in fields:
            continue
        holder = Path(next(field[9:] for field in fields if field.startswith("worktree ")))
        if holder.resolve() == wt.resolve():
            continue
        event.update(cause="branch-held", holder_path=str(holder))
        status = _wt_git(holder, ["status", "--porcelain", "--untracked-files=all"])
        if status.returncode != 0:
            raise RuntimeError(f"cannot inspect holder {holder}: {status.stderr.strip()}")
        if status.stdout.strip():
            raise RuntimeError(
                f"refusing to reclaim dirty worktree {holder}: {status.stdout.strip()}"
            )
        try:
            live = _live_holder_pids(holder)
        except ProbeBlindError:
            event.update(outcome="failed", cause="probe-blind")
            raise
        if live:
            raise RuntimeError(
                f"refusing to reclaim worktree {holder}: live pid {', '.join(sorted(live))}"
            )
        _run(["git", "-C", str(canon), "worktree", "remove", "--force", str(holder)])
        _run(["git", "-C", str(canon), "worktree", "prune"])
        event["outcome"] = "reclaimed"
        print(f"provision: reclaimed stale worktree {holder} holding {branch}")


def _add_opener_worktree(canon: Path, wt: Path, branch: str, base: str) -> None:
    """Retry an existing branch once after pruning dead registrations; never reset its commits."""
    git = ["git", "-C", str(canon)]
    try:
        _run([*git, "worktree", "add", "-b", branch, str(wt), f"origin/{base}"])
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or ""
        branch_exists = f"a branch named '{branch}' already exists" in detail
        if not branch_exists:
            raise
        print(
            f"provision: {branch} already exists; pruning stale worktree registrations "
            "and retrying once without resetting the branch",
            file=sys.stderr,
        )
        _run([*git, "worktree", "prune", "--expire", "now"])
        # Git still refuses a branch checked out in a live/locked worktree. No -B or --force.
        _run([*git, "worktree", "add", str(wt), branch])


def _provision(target: str, lane: str, event: dict, branch: str | None = None) -> Path:
    """Ensure a local-disk worktree for the target; return its path (idempotent)."""
    repo, num = parse_target(target)
    canon = ensure_canonical(repo)
    wt = worktree_path(target, lane)
    if (wt / ".git").exists():
        if branch is not None and lane != "closer":
            current = _wt_git(wt, ["branch", "--show-current"])
            name = current.stdout.strip()
            if current.returncode or not (name == branch or name.startswith(f"{branch}-")):
                raise RuntimeError(
                    f"existing worktree branch {name!r} does not match {branch!r}; "
                    f"inspect or remove {wt} before redispatch"
                )
        if os.environ.get("ORCH_ALLOW_STALE_WORKTREE_REUSE") == "1":
            event.update(outcome="reused", cause="existing-worktree")
            return wt
        reuse_error = _existing_worktree_reuse_error(wt)
        if reuse_error:
            raise RuntimeError(
                f"refusing to reuse stale worktree for {target} ({lane}): "
                f"{reuse_error}; inspect or remove {wt}"
            )
        event.update(outcome="reused", cause="existing-worktree")
        return wt  # already provisioned and clean
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    if lane == "closer" and num is not None:
        head = _pr_head_branch(repo, num)
        _run(["git", "-C", str(canon), "fetch", "origin", head], check=False)
        _reclaim_branch_holder(canon, head, wt, event)
        # worktree tracking origin/<head> so the agent's push updates the PR
        _run(
            [
                "git",
                "-C",
                str(canon),
                "worktree",
                "add",
                "--checkout",
                "-B",
                head,
                str(wt),
                f"origin/{head}",
            ]
        )
    else:  # opener (issue) or repo-level: new branch off the base
        base = base_branch(repo)
        branch = branch or (
            f"orchestrator/issue-{num}" if num is not None else f"orchestrator/{lane}"
        )
        _run(["git", "-C", str(canon), "fetch", "origin", base], check=False)
        _reclaim_branch_holder(canon, branch, wt, event)
        exists = _run(
            ["git", "-C", str(canon), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        if exists.returncode not in (0, 1):
            exists.check_returncode()
        if exists.returncode == 0:
            if event["outcome"] != "reclaimed":
                event.update(outcome="reused", cause="branch-exists")
            ancestor = _run(
                ["git", "-C", str(canon), "merge-base", "--is-ancestor", f"origin/{base}", branch],
                check=False,
            )
            if ancestor.returncode == 0:
                _run(["git", "-C", str(canon), "worktree", "add", str(wt), branch])
            else:
                if ancestor.returncode != 1:
                    ancestor.check_returncode()
                unique = _run(
                    ["git", "-C", str(canon), "rev-list", branch, "--not", "--remotes"]
                ).stdout.strip()
                if unique:
                    raise RuntimeError(
                        f"refusing to reset existing branch {branch}: commits absent from every "
                        f"remote ref: {unique}"
                    )
                _run(
                    [
                        "git",
                        "-C",
                        str(canon),
                        "worktree",
                        "add",
                        "-B",
                        branch,
                        str(wt),
                        f"origin/{base}",
                    ]
                )
                event.update(outcome="reclaimed", cause="branch-reset")
        else:
            _add_opener_worktree(canon, wt, branch, base)
    return wt


def provision(target: str, lane: str, branch: str | None = None) -> Path:
    """Provision a checkout and record exactly one result, including caller-visible failures."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "lane": lane,
        "outcome": "ok",
        "cause": "provision-error",
        "holder_path": None,
    }
    try:
        wt = (
            _provision(target, lane, event, branch=branch)
            if branch is not None
            else _provision(target, lane, event)
        )
        if event["outcome"] == "ok":
            event["cause"] = "new-worktree"
        return wt
    except BaseException as exc:
        event["outcome"] = "failed"
        if not isinstance(exc, Exception):
            event["cause"] = "interrupted"
        raise
    finally:
        try:
            PROVISION_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PROVISION_EVENTS_PATH.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(event) + "\n")
        except OSError as exc:
            # A full disk must not hide the actual provisioning error from dispatcher.py.
            print(
                f"provision: cannot append event to {PROVISION_EVENTS_PATH}: {exc}", file=sys.stderr
            )


def report(days: int) -> None:
    """Count the measured window; an absent instrument is never a measured zero."""
    summary = f"provision: failed(branch-held|branch-exists) = {{}} over {days}d"
    if not PROVISION_EVENTS_PATH.exists():
        print("unmeasured: no ledger")
        print("provision: malformed_lines=0")
        print(summary.format("unmeasured: no ledger"))
        return
    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts: dict[tuple[str, str], int] = {}
    malformed_lines = 0
    with PROVISION_EVENTS_PATH.open(encoding="utf-8") as ledger:
        for line in ledger:
            try:
                event = json.loads(line)
                ts = datetime.fromisoformat(event["ts"])
                key = (event["outcome"], event["cause"])
                if ts.tzinfo is None or not all(isinstance(value, str) for value in key):
                    raise ValueError("invalid event fields")
            except (ValueError, TypeError, KeyError):
                malformed_lines += 1
                continue
            if since <= ts <= datetime.now(timezone.utc):
                counts[key] = counts.get(key, 0) + 1
    print(f"provision: malformed_lines={malformed_lines}")
    for outcome in ("ok", "reused", "reclaimed", "failed"):
        print(f"provision: {outcome} = {sum(n for (o, _), n in counts.items() if o == outcome)}")
    for (outcome, cause), count in sorted(counts.items()):
        print(f"provision: {outcome}({cause}) = {count}")
    print(
        summary.format(
            sum(counts.get(("failed", cause), 0) for cause in ("branch-held", "branch-exists"))
        )
    )


# ---------------------------------------------------------------------------
def _selftest() -> None:
    assert parse_target("stranske/Repo#123") == ("stranske/Repo", 123)
    assert parse_target("stranske/Repo") == ("stranske/Repo", None)
    assert parse_target("o/r#abc") == ("o/r", None)  # non-numeric => None
    assert repo_slug("stranske/Trend_Model_Project") == "stranske__Trend_Model_Project"
    assert (
        worktree_name("stranske/Counter_Risk#42", "closer") == "stranske__Counter_Risk__42__closer"
    )
    assert worktree_name("stranske/Workflows#7", "opener") == "stranske__Workflows__7__opener"
    assert worktree_path("o/r#1", "closer").name == "o__r__1__closer"
    assert canonical_path("stranske/Counter_Risk").name == "stranske__Counter_Risk"
    assert _existing_worktree_reuse_error(Path("/definitely/not/a/worktree")) is not None
    # Trend follows its live default. Prove the fallback is consulted instead of pinning the current
    # name into BASE_BRANCH_OVERRIDES, where a future default-branch rename could strand dispatch.
    trend_repo = "stranske/Trend_Model_Project"
    assert trend_repo not in BASE_BRANCH_OVERRIDES
    with mock.patch.object(sys.modules[__name__], "default_branch", return_value="main") as queried:
        assert base_branch(trend_repo) == "main"
        queried.assert_called_once_with(trend_repo)
    print(
        "provision.py selftest: OK (target parse, slug, worktree naming/paths, "
        "Trend live-default fallback) — live clone/worktree covered by --smoke"
    )


def _smoke(target: str, lane: str) -> None:
    """Provision one target live, prove the worktree is a real git dir on normal fs, clean up."""
    repo, num = parse_target(target)
    print(f"smoke: provisioning {target} ({lane})")
    wt = provision(target, lane)
    assert (wt / ".git").exists(), f"worktree not created: {wt}"
    # prove it's a usable, writable, normal-fs checkout (git status works; not on Dropbox)
    st = _run(["git", "-C", str(wt), "status", "--porcelain"], check=False)
    assert st.returncode == 0, f"git status failed in worktree: {st.stderr}"
    assert "CloudStorage/Dropbox" not in str(
        wt.resolve()
    ), "worktree must be on local disk, not Dropbox"
    branch = _run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], check=False
    ).stdout.strip()
    print(f"  OK worktree={wt}")
    print(
        f"  branch={branch}  (normal-fs: worktree add succeeded — no Dropbox 'Operation not permitted')"
    )
    # cleanup so the smoke leaves no junk branch/worktree
    canon = canonical_path(repo)
    _run(["git", "-C", str(canon), "worktree", "remove", "--force", str(wt)], check=False)
    if lane != "closer" and num is not None:
        _run(["git", "-C", str(canon), "branch", "-D", f"orchestrator/issue-{num}"], check=False)
    print("  cleaned up (worktree removed, opener branch deleted)")


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--report":
        parser = argparse.ArgumentParser()
        parser.add_argument("--report", action="store_true")
        parser.add_argument("--days", type=int, default=7)
        args = parser.parse_args(argv)
        if args.days <= 0:
            parser.error("--days must be positive")
        report(args.days)
        return 0
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "--smoke":
        _smoke(argv[1], argv[2] if len(argv) > 2 else "opener")
        return 0
    if argv and argv[0] == "provision":
        print(provision(argv[1], argv[2] if len(argv) > 2 else "opener"))
        return 0
    print(
        "usage: provision.py --selftest | --report --days N | --smoke <owner/repo#N> <opener|closer> | provision <target> <lane>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


def provision_absent_owned_head(target: str, head: str, branch: str, *, git_policy: dict) -> dict:
    """Prepare one absent local named worktree, never reset/reclaim/fetch/push.

    The caller supplies a freshly native-observed owned PR tuple. This local
    DATA receipt is not native ownership or controller admission. The existing
    canonical clone must already contain H; Root may populate a fresh namespace
    separately. No existing branch/worktree/index is adopted or modified.
    """
    import closer_coding_publication as publication
    import worktree_git

    repo, number = parse_target(target)
    if number is None or repo != repo.lower() or not re.fullmatch(r"[a-f0-9]{40}", head):
        raise ValueError("exact lowercase owned PR target/head required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch):
        raise ValueError("bounded existing owned branch required")
    canon = canonical_path(repo).resolve(strict=True)
    wt = worktree_path(target, "closer")
    if wt.exists() or wt.is_symlink():
        raise RuntimeError("owned canary worktree must be absent; never reclaim")
    if not (canon / ".git").is_dir() or (canon / ".git").is_symlink():
        raise RuntimeError("existing canonical local Git repository required")
    # Validate genuine configured namespaces rather than forging is_provisioned.
    if canon.parent != REPOS_DIR.resolve(strict=True) or wt.parent != WORKTREES_DIR.resolve(
        strict=True
    ):
        raise RuntimeError("canonical repository/worktree namespace differs")
    original = dict(os.environ)
    environment = publication.private_git_environment_data(git_policy, original)
    executable = publication._PhysicalFile(git_policy["git_executable"]["path"], 256 * 1048576)
    controls: list[publication._PhysicalFile | publication._PhysicalDirectory] = []
    try:
        if executable.sha256 != git_policy["git_executable"]["sha256"]:
            raise RuntimeError("source-pinned local Git executable differs")

        def run(args, *, check=True):
            if dict(os.environ) != original:
                raise RuntimeError("local provision environment changed")
            executable.check()
            for item in controls:
                item.check()
            if environment != publication.private_git_environment_data(git_policy, original):
                raise RuntimeError("private local Git environment changed")
            return subprocess.run(
                [str(executable.path), "-C", str(canon), *args],
                env=environment,
                capture_output=True,
                text=True,
                check=check,
                timeout=30,
            )

        names = publication._configuration_names(
            run(
                ["config", "--null", "--show-scope", "--show-origin", "--name-only", "--list"]
            ).stdout.encode(),
            git_policy["max_ack_bytes"],
        )
        for name in sorted({str((canon / origin).resolve()) for _scope, origin, _key in names}):
            controls.append(publication._PhysicalFile(name, git_policy["max_ack_bytes"]))
        hooks = canon / ".git/hooks"
        controls.append(publication._PhysicalDirectory(hooks, closed_entries=True))
        if any(
            p.is_symlink() or not p.is_file() or not p.name.endswith(".sample")
            for p in hooks.iterdir()
        ):
            raise RuntimeError(
                "active canonical hooks require source closure before local provisioning"
            )
        run(["check-ref-format", "--branch", branch])
        absent = run(["show-ref", "--verify", "--quiet", "refs/heads/" + branch], check=False)
        if absent.returncode != 1:
            raise RuntimeError("owned local branch must be absent; never reset or adopt")
        run(["cat-file", "-e", head + "^{commit}"])
        # -b is Git's own create-only branch lock. Existing holder/name races
        # refuse without -B/--force/prune/remove or retrying an uncertain result.
        run(["worktree", "add", "--checkout", "-b", branch, str(wt), head])
        pins = worktree_git.expected_pins(wt, repo)
        if not worktree_git.is_provisioned(wt) or not isinstance(pins, tuple):
            raise RuntimeError("actual linked worktree metadata refused; preserve for inspection")
        observed = subprocess.run(
            [str(executable.path), "-C", str(wt), "rev-parse", "HEAD"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        named = subprocess.run(
            [str(executable.path), "-C", str(wt), "symbolic-ref", "HEAD"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        if observed != head or named != "refs/heads/" + branch:
            raise RuntimeError("prepared owned worktree changed; preserve without retry")
        return {
            "schema": "closer-owned-existing-worktree-preparation-data/v1",
            "target": target,
            "head": head,
            "branch": branch,
            "worktree": str(wt),
            "topology": [str(p) for p in pins],
            "managed_metadata_verified": True,
            "native_ownership_proven": False,
            "runtime_admission": False,
        }
    finally:
        publication._close_physical_controls([executable, *controls])
