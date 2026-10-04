#!/usr/bin/env python3
"""pushed_branches.py — the branches a local run pushed, read from git when it completes (a rail).

WHY. Outcome ingest credits a local delegate with the PR on one of its candidate branches,
`orchestrator/issue-N` and `{agent}/issue-N`, but agents name their own branches. Measured
2026-10-04: local codex runs on Trend_Model_Project#5901, #5900, #5898 and Counter_Risk#991 pushed
`fix/5901-engine-nan-handlers`, `test/5900-exception-budget-ratchet`,
`feat/5898-deflated-sharpe-wiring` and `codex/issue-991-ppt-chart-link-uri`, the exact heads of the
PRs that closed their issues, and ingest could only record each one `unattributed_closing_pr`. Two
cheaper fixes were measured and rejected in #411: widening the name patterns credits runs with PRs
they did not make, and parsing the transcript is inexact across agents (a curl command quoting
`git push` matched a naive parse).

WHAT IT READS: machine state git itself wrote, never the transcript, and at completion, because a
merged branch's remote-tracking ref and its reflog are deleted by the next `fetch --prune`.

  * The run's OWN worktree's HEAD reflog, for the commits it CREATED inside its window: commit,
    amend, cherry-pick, revert, am, rebase, and a pull or merge that made a commit. A commit it only
    checked out, reset to or fast-forwarded to was made somewhere else.
  * Every `refs/remotes/origin/*` reflog, for `update by push` entries inside the window.

A branch is recorded when a push inside the window put one of those created commits on it. The
intersection is what makes the record exact. Every dispatch worktree of a repo hangs off one
canonical clone (`provision.py`), so the remote-tracking refs are shared, and TMP#5898/#5900/#5901
ran at the same time in three worktrees of one clone: a window-only read would hand each run all
three branches. A worktree's HEAD reflog is its own. Upstream configuration is not read at all:
Counter_Risk#991 pushed `HEAD:refs/heads/...` without `-u`, and git still logged `update by push`.
The "created" rule is for TMP#5918's shape: that run inspected PR #5923 in a detached temporary
worktree and pushed nothing, while #5923 was opened inside its window, so a check of the PR's
creation time alone would have credited it.

WHAT IT DOES NOT DO: ask GitHub. Outcome ingest checks a recorded branch's PR when it decides the
verdict (`pr_opened_in_window`: head == branch, created at or after the run started). Unknown is not
false: no record, an unreadable record and an empty one all leave ingest exactly as it was.

    python3 src/pushed_branches.py read --workspace WT --since TS [--until TS]
    python3 src/pushed_branches.py show RUN_ID
    python3 src/pushed_branches.py --selftest
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import feedback

# Kill switch. Set to 1 and completion records nothing, so ingest resolves every run as before.
DISABLE_ENV = "ORCH_PUSH_RECORD_DISABLED"
# provision.ensure_canonical clones with `gh repo clone`, so the target repo is always `origin`.
REMOTE = "origin"
# The reflog subject git writes on a remote-tracking ref when `git push` updates it.
PUSH_SUBJECT = "update by push"
GIT_TIMEOUT_S = 30
# HEAD reflog subjects whose commit this worktree MADE. An allowlist on purpose: a subject git adds
# later reads as "not made here", which costs a credit, where a denylist would cost a false one.
CREATING_VERBS = frozenset({"commit", "cherry-pick", "revert", "am", "rebase", "pull", "merge"})
_SELECTOR_TS = re.compile(r"@\{(\d+)\}$")


def created_here(subject: str) -> bool:
    """Did the HEAD reflog entry with this subject create its commit in this worktree?"""
    head, _, rest = (subject or "").strip().partition(":")
    words = head.split()
    if not words or words[0] not in CREATING_VERBS:
        return (
            False  # checkout:, reset:, branch:, Branch: renamed, and the empty worktree-add entry
        )
    if "(start)" in head or "(abort)" in head:
        return False  # rebase (start) checks out the upstream; (abort) returns to where it began
    if words[0] in ("merge", "pull") and rest.strip().startswith("Fast-forward"):
        return False  # moved onto someone else's commit
    return True


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )


def _reflog(workspace: Path, ref: str) -> list[tuple[int, str, str]] | None:
    """`(ts, sha, subject)` per reflog entry of `ref`; None when git could not answer.

    A ref with no reflog prints nothing and exits 0, which is an empty answer, not a failure.
    """
    r = _git(workspace, "reflog", "show", "--date=unix", "--format=%H%x09%gd%x09%gs", ref, "--")
    if r.returncode != 0:
        return None
    entries = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        match = _SELECTOR_TS.search(parts[1]) if len(parts) == 3 else None
        if not match:
            return None  # a line this parser cannot read makes the whole answer unknown
        entries.append((int(match.group(1)), parts[0], parts[2]))
    return entries


def _unreadable(reason: str) -> dict:
    return {"status": "unreadable", "reason": reason, "branches": []}


def read_pushes(workspace, start_ts: int, end_ts: int, *, remote: str = REMOTE) -> dict:
    """The branches pushed from `workspace` with a commit it created, both inside [start, end].

    Returns `{"status": "read", "reason": None, "branches": [...], "created_commits": n}` with the
    branches latest push first, or `{"status": "unreadable", "reason": ..., "branches": []}`.
    """
    ws = Path(workspace) if workspace else None
    if ws is None or not ws.is_dir():
        return _unreadable("workspace_missing")
    if start_ts > end_ts:
        return _unreadable("window_inverted")
    try:
        inside = _git(ws, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return _unreadable("not_a_git_worktree")
        # Without its own HEAD reflog, `reflog show HEAD` silently prints the checked-out BRANCH's
        # reflog instead (measured, git 2.49), so the absence must be asked about, not inferred.
        if _git(ws, "reflog", "exists", "HEAD").returncode != 0:
            return _unreadable("head_reflog_absent")
        head = _reflog(ws, "HEAD")
        if head is None:
            return _unreadable("head_reflog_unreadable")
        created = {
            sha for ts, sha, subject in head if start_ts <= ts <= end_ts and created_here(subject)
        }
        prefix = f"refs/remotes/{remote}/"
        refs = _git(ws, "for-each-ref", "--format=%(refname)", prefix)
        if refs.returncode != 0:
            return _unreadable("remote_refs_unreadable")
        branches: dict[str, dict] = {}
        for ref in refs.stdout.split():
            name = ref[len(prefix) :]
            if not ref.startswith(prefix) or not name or name == "HEAD":
                continue
            entries = _reflog(ws, ref)
            if entries is None:
                return _unreadable(f"remote_reflog_unreadable:{name}")
            for ts, sha, subject in entries:
                if not (start_ts <= ts <= end_ts and subject.startswith(PUSH_SUBJECT)):
                    continue
                if sha not in created:
                    continue
                if name not in branches or ts > branches[name]["pushed_ts"]:
                    branches[name] = {"branch": name, "sha": sha, "pushed_ts": ts}
    except (OSError, subprocess.SubprocessError) as exc:
        return _unreadable(f"git_failed:{type(exc).__name__}")
    return {
        "status": "read",
        "reason": None,
        "branches": sorted(branches.values(), key=lambda b: (-b["pushed_ts"], b["branch"])),
        "created_commits": len(created),
    }


def record_at_completion(run_id: str, workspace, started_ts, *, now=None) -> dict | None:
    """Read and store one finished run's push record. None when nothing was attempted (the kill
    switch is set, or the caller has no workspace), so the run keeps no row at all."""
    if os.environ.get(DISABLE_ENV) == "1" or not workspace:
        return None
    end = int(time.time() if now is None else now)
    if started_ts is None:
        start, result = end, _unreadable("no_window_start")
    else:
        start = int(started_ts)
        result = read_pushes(workspace, start, end)
        if result["reason"] == "window_inverted":
            start = end  # the stored window must still be a window
    feedback.record_run_pushes(
        run_id,
        status=result["status"],
        reason=result["reason"],
        workspace=str(workspace),
        window_start=start,
        window_end=end,
        branches=result["branches"],
    )
    return result


def recorded_branches(record: dict | None) -> list[str]:
    """The branch names ingest walks first: only a READ record has any."""
    if not record or record.get("status") != "read":
        return []
    return [str(b["branch"]) for b in record.get("branches") or [] if b.get("branch")]


def _parse_ts(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(_dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def pr_opened_in_window(pr: dict, branch: str, start_ts: int) -> bool | None:
    """Is `pr` the PR this run opened on `branch`: its head IS the branch and it was created at or
    after the run started? None when the PR record cannot say (no head or no creation time)."""
    head = pr.get("headRefName")
    if isinstance(head, str) and head != branch:
        return False
    created = _parse_ts(pr.get("createdAt"))
    if not isinstance(head, str) or created is None:
        return None
    return created >= int(start_ts)


# ---------------------------------------------------------------------------
def _selftest() -> None:
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pushed-branches-selftest-"))
    saved_env = dict(os.environ)
    saved_db = feedback.DB_PATH
    try:
        os.environ.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            }
        )
        os.environ.pop(DISABLE_ENV, None)
        feedback.DB_PATH = tmp / "brain.db"

        def git(cwd: Path, *args: str, at: int) -> None:
            env = {**os.environ, "GIT_COMMITTER_DATE": f"@{at} +0000"}
            subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

        git(tmp, "init", "-q", "--bare", "origin.git", at=1000)
        seed = tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main", at=1000)
        (seed / "a").write_text("a\n")
        git(seed, "add", "a", at=1000)
        git(seed, "commit", "-qm", "init", at=1000)
        git(seed, "push", "-q", str(tmp / "origin.git"), "main", at=1000)
        git(tmp, "clone", "-q", "origin.git", "canon", at=1100)
        canon = tmp / "canon"
        git(
            canon,
            "worktree",
            "add",
            "-q",
            "-b",
            "orchestrator/issue-1",
            "../wt1",
            "origin/main",
            at=1200,
        )
        git(
            canon,
            "worktree",
            "add",
            "-q",
            "-b",
            "orchestrator/issue-2",
            "../wt2",
            "origin/main",
            at=1200,
        )
        wt1, wt2 = tmp / "wt1", tmp / "wt2"
        # wt1: rename the branch, commit, push with -u (TMP#5901's shape)
        git(wt1, "branch", "-m", "orchestrator/issue-1", "fix/1-thing", at=1300)
        (wt1 / "b").write_text("b\n")
        git(wt1, "add", "b", at=1300)
        git(wt1, "commit", "-qm", "fix 1", at=1310)
        git(wt1, "push", "-q", "-u", "origin", "fix/1-thing", at=1320)
        # wt2, concurrently: refspec push with no -u (Counter_Risk#991's shape), then wt1 checks
        # out wt2's pushed commit, which is reading another run's work, not making it.
        (wt2 / "c").write_text("c\n")
        git(wt2, "add", "c", at=1330)
        git(wt2, "commit", "-qm", "fix 2", at=1340)
        git(wt2, "push", "-q", "origin", "HEAD:refs/heads/feat/2-other", at=1350)
        git(wt1, "fetch", "-q", "origin", at=1360)
        git(wt1, "checkout", "-q", "--detach", "origin/feat/2-other", at=1370)

        one = read_pushes(wt1, 1250, 1400)
        two = read_pushes(wt2, 1250, 1400)
        assert one["status"] == "read" and [b["branch"] for b in one["branches"]] == [
            "fix/1-thing"
        ], ("wt1 must record exactly its own push, not wt2's checked-out commit", one)
        assert [b["branch"] for b in two["branches"]] == ["feat/2-other"], two
        assert one["branches"][0]["pushed_ts"] == 1320, one
        assert read_pushes(wt1, 1325, 1400)["branches"] == [], "a push before the window counted"
        empty = read_pushes(wt1, 1400, 1500)
        assert (empty["status"], empty["branches"]) == ("read", []), empty
        assert read_pushes(tmp / "absent", 0, 1)["reason"] == "workspace_missing"
        assert read_pushes(tmp, 0, 1)["reason"] == "not_a_git_worktree"
        for subject, made in (
            ("commit: fix", True),
            ("commit (amend): fix", True),
            ("rebase (finish): returning to refs/heads/x", True),
            ("merge origin/main: Merge made by the 'ort' strategy.", True),
            ("merge origin/main: Fast-forward", False),
            ("pull: Fast-forward", False),
            ("rebase (start): checkout origin/main", False),
            ("checkout: moving from a to b", False),
            ("reset: moving to HEAD", False),
            ("Branch: renamed refs/heads/a to refs/heads/b", False),
            ("", False),
        ):
            assert created_here(subject) is made, (subject, made)

        # Stored at completion; the kill switch and a missing workspace store nothing.
        stored = record_at_completion("run-wt1", wt1, 1250, now=1400)
        wt1_record = feedback.run_pushes("run-wt1")
        assert stored and wt1_record is not None, "the completion read stored no record"
        assert wt1_record["branches"][0]["branch"] == "fix/1-thing", wt1_record
        assert recorded_branches(wt1_record) == ["fix/1-thing"]
        os.environ[DISABLE_ENV] = "1"
        assert record_at_completion("run-off", wt1, 1250, now=1400) is None
        os.environ.pop(DISABLE_ENV)
        assert record_at_completion("run-none", None, 1250, now=1400) is None
        assert feedback.run_pushes("run-off") is None and feedback.run_pushes("run-none") is None
        record_at_completion("run-gone", tmp / "absent", 1250, now=1400)
        gone = feedback.run_pushes("run-gone")
        assert gone is not None, "an attempted read must leave a record, even an unreadable one"
        assert (gone["status"], gone["reason"]) == ("unreadable", "workspace_missing"), gone
        assert recorded_branches(gone) == [], "an unreadable record must contribute no branch"

        pr = {"headRefName": "fix/1-thing", "createdAt": "1970-01-01T00:22:00Z"}  # ts 1320
        assert pr_opened_in_window(pr, "fix/1-thing", 1250) is True
        assert pr_opened_in_window(pr, "fix/1-thing", 1321) is False
        assert pr_opened_in_window(pr, "other", 1250) is False
        assert pr_opened_in_window({"headRefName": "fix/1-thing"}, "fix/1-thing", 1250) is None
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        feedback.DB_PATH = saved_db
        shutil.rmtree(tmp, ignore_errors=True)
    print(
        "pushed_branches.py selftest: OK (own push recorded across a rename and a refspec push, "
        "concurrent worktree and checked-out commit excluded, window bounds, read-empty vs "
        "unreadable, kill switch, PR window predicate)"
    )


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(description="Branches a local run pushed (read from git).")
    sub = parser.add_subparsers(dest="cmd")
    read = sub.add_parser("read", help="read a worktree's pushes inside a window (no write)")
    read.add_argument("--workspace", required=True)
    read.add_argument("--since", type=int, required=True)
    read.add_argument("--until", type=int)
    show = sub.add_parser("show", help="print a run's stored push record")
    show.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.cmd == "read":
        until = int(time.time()) if args.until is None else args.until
        print(json.dumps(read_pushes(args.workspace, args.since, until), indent=2))
        return 0
    if args.cmd == "show":
        print(json.dumps(feedback.run_pushes(args.run_id), indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
