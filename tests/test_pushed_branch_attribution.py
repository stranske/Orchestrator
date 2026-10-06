"""A local run is credited with the PR on the branch it pushed, recorded when it completes (2026-10-04).

THE DEFECT. Outcome ingest resolved a local delegate's PR from name patterns (`orchestrator/issue-N`,
`{agent}/issue-N`), but agents name their own branches. Local codex runs on Trend_Model_Project
#5901, #5900, #5898 and Counter_Risk#991 pushed `fix/5901-engine-nan-handlers`,
`test/5900-exception-budget-ratchet`, `feat/5898-deflated-sharpe-wiring` and
`codex/issue-991-ppt-chart-link-uri`, the exact heads of the PRs that closed their issues, and
ingest could only record them `unattributed_closing_pr`. #411 measured and rejected widening the
patterns and parsing transcripts.

THE RULE. When the run completes, `pushed_branches` reads its own worktree from git: the commits it
CREATED in its window (its own HEAD reflog) that a push in the window put on a remote branch
(`update by push` on `refs/remotes/origin/*`). The record lands in `feedback.run_pushes`, and local
ingest asks those branches first. A PR there counts only if the run OPENED it (head == branch,
created at or after the run started); one that predates the run is rejected and its branch is not
asked again. No record, an unreadable one and an empty one leave ingest exactly as it was.

The git cases are real repositories built in a temp directory, with reflog times pinned through
GIT_COMMITTER_DATE. GitHub is a stub that answers by argv; nothing touches the network.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

import adapters
import dispatcher
import durability_sweep
import feedback
import ledger_reconcile
import outcomes
import pushed_branches

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
NO_PR = (0, "[]", "")
RATE_LIMIT = (1, "", "API rate limit exceeded")
RUN = "o__r_7-codex-1"
TARGET = "o/r#7"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.delenv(pushed_branches.DISABLE_ENV, raising=False)
    return tmp_path


class Clone:
    """A bare origin, one canonical clone, and dispatch worktrees hanging off it (provision.py)."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
        self.git(root, "init", "-q", "--bare", "origin.git", at=1000)
        seed = root / "seed"
        seed.mkdir()
        self.git(seed, "init", "-q", "-b", "main", at=1000)
        (seed / "a").write_text("a\n")
        self.git(seed, "add", "a", at=1000)
        self.git(seed, "commit", "-qm", "init", at=1000)
        self.git(seed, "push", "-q", str(root / "origin.git"), "main", at=1000)
        self.git(root, "clone", "-q", "origin.git", "canon", at=1100)
        self.canon = root / "canon"

    @staticmethod
    def git(cwd: Path, *args: str, at: int) -> str:
        env = {**os.environ, "GIT_COMMITTER_DATE": f"@{at} +0000"}
        done = subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        )
        return done.stdout.strip()

    def worktree(self, name: str, at: int = 1200) -> Path:
        branch = f"orchestrator/{name}"
        self.git(
            self.canon, "worktree", "add", "-q", "-b", branch, f"../{name}", "origin/main", at=at
        )
        return self.root / name

    def commit(self, wt: Path, name: str, at: int) -> str:
        (wt / name).write_text(f"{name}\n")
        self.git(wt, "add", name, at=at)
        self.git(wt, "commit", "-qm", name, at=at)
        return self.git(wt, "rev-parse", "HEAD", at=at)


@pytest.fixture
def clone(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return Clone(tmp_path / "git")


def _names(result: dict) -> list[str]:
    return [branch["branch"] for branch in result["branches"]]


# ---------------------------------------------------------------- reading machine state


def test_a_renamed_branch_pushed_with_upstream_is_recorded(clone):
    """TMP#5901's shape: rename the provisioned branch, commit, `git push -u`."""
    wt = clone.worktree("issue-1")
    clone.git(wt, "branch", "-m", "orchestrator/issue-1", "fix/1-engine", at=1300)
    sha = clone.commit(wt, "b", at=1310)
    clone.git(wt, "push", "-q", "-u", "origin", "fix/1-engine", at=1320)
    got = pushed_branches.read_pushes(wt, 1250, 1400)
    assert got["status"] == "read" and got["branches"] == [
        {"branch": "fix/1-engine", "sha": sha, "pushed_ts": 1320}
    ], got


def test_a_refspec_push_without_upstream_is_recorded(clone):
    """Counter_Risk#991's shape: `git push origin HEAD:refs/heads/...`, no -u, so no upstream config
    names the branch; git still logs `update by push` on the remote-tracking ref."""
    wt = clone.worktree("issue-2")
    clone.commit(wt, "c", at=1310)
    clone.git(wt, "push", "-q", "origin", "HEAD:refs/heads/codex/issue-2-own-name", at=1320)
    upstream = clone.git(wt, "rev-parse", "--abbrev-ref", "@{u}", at=1330)
    assert upstream == "origin/main", "the fixture must not have set an upstream"
    assert _names(pushed_branches.read_pushes(wt, 1250, 1400)) == ["codex/issue-2-own-name"]


def test_concurrent_worktrees_of_one_clone_each_record_only_their_own_push(clone):
    """TMP#5898/#5900/#5901 ran at once in three worktrees of ONE clone, which share every
    remote-tracking ref. Each run's record must name its own branch and nothing else."""
    wts = [clone.worktree(f"issue-{n}") for n in (5898, 5900, 5901)]
    for offset, wt in enumerate(wts):
        clone.commit(wt, f"work-{offset}", at=1300 + offset)
        clone.git(wt, "push", "-q", "origin", f"HEAD:refs/heads/own-{offset}", at=1310 + offset)
    for offset, wt in enumerate(wts):
        assert _names(pushed_branches.read_pushes(wt, 1250, 1400)) == [f"own-{offset}"]


def test_a_pushed_commit_the_worktree_only_checked_out_is_not_recorded(clone):
    """TMP#5918's shape: a run that inspects work it did not make. Another worktree pushed X inside
    this run's window and this worktree checked X out, so X is in its HEAD reflog, but it was not
    CREATED here, and the push is not this run's delivery."""
    reviewer, author = clone.worktree("issue-3"), clone.worktree("issue-4")
    clone.commit(author, "d", at=1300)
    clone.git(author, "push", "-q", "origin", "HEAD:refs/heads/author-branch", at=1310)
    clone.git(reviewer, "checkout", "-q", "--detach", "origin/author-branch", at=1320)
    assert pushed_branches.read_pushes(reviewer, 1250, 1400)["branches"] == []
    assert _names(pushed_branches.read_pushes(author, 1250, 1400)) == ["author-branch"]


def test_a_push_outside_the_window_is_not_recorded(clone):
    """provision.py reuses a clean worktree for the next run on the same target and lane, so its
    reflogs hold the previous run's commits and pushes. The window is what separates them."""
    wt = clone.worktree("issue-5")
    clone.commit(wt, "e", at=1300)
    clone.git(wt, "push", "-q", "origin", "HEAD:refs/heads/first-run", at=1310)
    assert _names(pushed_branches.read_pushes(wt, 1250, 1311)) == ["first-run"]
    later = pushed_branches.read_pushes(wt, 1311, 1500)
    assert (later["status"], later["branches"]) == ("read", []), later


def test_a_fetch_that_moves_a_remote_ref_is_not_a_push(clone):
    """Only `update by push` counts. Here the run's own commit X also reaches a second branch, but
    through someone else's push and this clone's fetch: that ref was not pushed by the run."""
    wt = clone.worktree("issue-6")
    clone.commit(wt, "f", at=1300)
    clone.git(wt, "push", "-q", "origin", "HEAD:refs/heads/own", at=1310)
    other = clone.root / "other"
    clone.git(clone.root, "clone", "-q", "origin.git", "other", at=1320)
    clone.git(other, "push", "-q", "origin", "origin/own:refs/heads/copied-by-a-bot", at=1330)
    clone.git(clone.canon, "fetch", "-q", "origin", at=1340)
    assert clone.git(wt, "rev-parse", "origin/copied-by-a-bot", at=1350)
    assert _names(pushed_branches.read_pushes(wt, 1250, 1400)) == ["own"]


def test_unreadable_is_named_and_never_reads_as_a_measured_zero(clone, tmp_path):
    """Three states, and only one of them is a measurement: a read with no pushes."""
    wt = clone.worktree("issue-7")
    assert pushed_branches.read_pushes(wt, 1250, 1400)["status"] == "read"
    assert pushed_branches.read_pushes(tmp_path / "absent", 0, 1)["reason"] == "workspace_missing"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert pushed_branches.read_pushes(plain, 0, 1)["reason"] == "not_a_git_worktree"
    # Without its own HEAD reflog, `git reflog show HEAD` quietly prints the BRANCH's reflog
    # instead, so the reader must refuse rather than read what git falls back to.
    clone.commit(wt, "g", at=1300)
    head_log = Path(clone.git(wt, "rev-parse", "--absolute-git-dir", at=1300)) / "logs" / "HEAD"
    head_log.unlink()
    got = pushed_branches.read_pushes(wt, 1250, 1400)
    assert (got["status"], got["reason"], got["branches"]) == (
        "unreadable",
        "head_reflog_absent",
        [],
    ), got


@pytest.mark.parametrize(
    ("subject", "made_here"),
    [
        ("commit: fix the thing", True),
        ("commit (amend): fix the thing", True),
        ("commit (merge): Merge branch 'x'", True),
        ("cherry-pick: fix the thing", True),
        ("revert: Revert 'x'", True),
        ("rebase (finish): returning to refs/heads/x", True),
        ("pull --rebase (pick): fix", True),
        ("merge origin/main: Merge made by the 'ort' strategy.", True),
        ("merge origin/main: Fast-forward", False),
        ("pull: Fast-forward", False),
        ("rebase (start): checkout origin/main", False),
        ("rebase (abort): returning to refs/heads/x", False),
        ("checkout: moving from a to b", False),
        ("reset: moving to origin/x", False),
        ("Branch: renamed refs/heads/a to refs/heads/b", False),
        ("branch: Created from origin/main", False),
        ("", False),
    ],
)
def test_created_here_counts_only_entries_that_made_their_commit(subject, made_here):
    assert pushed_branches.created_here(subject) is made_here


# ---------------------------------------------------------------- the store


def test_the_store_refuses_a_record_that_conflates_its_states(brain):
    window = {"window_start": 1, "window_end": 2}
    for bad in (
        {"status": "unreadable", **window},
        {"status": "read", "reason": "why", **window},
        {"status": "unreadable", "reason": "x", "branches": [{"branch": "b"}], **window},
        {"status": "maybe", **window},
        {"status": "read", "window_start": 3, "window_end": 2},
        {"status": "read", "branches": [{"sha": "x"}], **window},
    ):
        with pytest.raises(ValueError):
            feedback.record_run_pushes("r", **bad)
    assert feedback.run_pushes("r") is None, "a refused record must write nothing"


def test_the_kill_switch_records_nothing(brain, clone, monkeypatch):
    wt = clone.worktree("issue-8")
    monkeypatch.setenv(pushed_branches.DISABLE_ENV, "1")
    assert pushed_branches.record_at_completion(RUN, wt, 1250, now=1400) is None
    assert feedback.run_pushes(RUN) is None


# ---------------------------------------------------------------- the producer's wiring


def _spawn_capture(monkeypatch, tmp_path, cwd: Path) -> tuple[dict, str]:
    seen: dict = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(argv, **_kw):
        seen["wrapped"] = argv[-1]
        return FakeProcess()

    monkeypatch.setattr(dispatcher.claims, "update_metadata", lambda *a, **k: True)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch-logs")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    d = {
        "run_id": RUN,
        "agent": "codex",
        "mode": "full",
        "target": TARGET,
        "lane": "opener",
        "task_type": "implement",
        "model": "gpt-5.6-codex",
        "cwd": str(cwd),
        "wrapped": "true",
    }
    # `dispatcher.subprocess` IS the subprocess module, so the fake Popen is scoped to this one call:
    # left in place it would also answer every later `subprocess.run` in the test.
    with monkeypatch.context() as scoped:
        scoped.setattr(dispatcher.subprocess, "Popen", fake_popen)
        dispatcher._spawn(d)
    return d, seen["wrapped"]


def _completion_argv(wrapped: str) -> list[str]:
    """The completion step as the dispatcher wrote it, ready for `ledger_reconcile.main`."""
    step = next(part for part in wrapped.split("; ") if "ledger_reconcile.py" in part)
    argv = shlex.split(step.replace('"$orch_dispatch_rc"', "0"))
    return argv[argv.index("complete") :]


def test_spawn_hands_its_worktree_to_the_completion_step(brain, monkeypatch, tmp_path):
    _d, wrapped = _spawn_capture(monkeypatch, tmp_path, tmp_path / "wt")
    argv = _completion_argv(wrapped)
    assert "--workspace" in argv, ("the completion step is not told the run's worktree", argv)
    assert argv[argv.index("--workspace") + 1] == str(tmp_path / "wt"), argv


def test_the_dispatchers_completion_command_records_the_push_and_ingest_credits_it(
    brain, clone, monkeypatch, tmp_path
):
    """Producer to consumer with nothing simulated between them: the completion command the
    dispatcher wrote runs through the real CLI against a worktree that pushed, and outcome ingest
    reads the record it left. Reflog times here are the real clock, because `_spawn` stamps the
    run's start with it."""
    wt = clone.worktree("issue-9", at=1200)
    d, wrapped = _spawn_capture(monkeypatch, tmp_path, wt)
    subprocess.run(["git", "branch", "-m", "orchestrator/issue-9", "fix/9-own"], cwd=wt, check=True)
    (wt / "h").write_text("h\n")
    subprocess.run(["git", "add", "h"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "h"], cwd=wt, check=True)
    pushed = ["git", "push", "-q", "origin", "fix/9-own"]
    subprocess.run(pushed, cwd=wt, check=True, capture_output=True)
    assert ledger_reconcile.main(_completion_argv(wrapped)) == 0
    record = feedback.run_pushes(RUN)
    assert record is not None, "the dispatcher's completion command recorded nothing for its run"
    assert record["status"] == "read" and pushed_branches.recorded_branches(record) == [
        "fix/9-own"
    ], record
    assert record["window_start"] == d["started_ts"], "the window must start at the run's start"

    feedback.record_run(RUN, TARGET, "implement", "codex", mode="local")
    created = _iso(d["started_ts"] + 1)
    _gh(monkeypatch, prs={"fix/9-own": _merged(19, "fix/9-own", created)})
    result = outcomes.ingest_modes("local")
    assert result["push_records"]["credited"] == 1, result
    assert _row(RUN)[:2] == (1, "PASS")


# ---------------------------------------------------------------- the consumer


def _iso(ts: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merged(number: int, head: str, created: str | None, state: str = "MERGED") -> dict:
    pr = {"number": number, "state": state, "headRefName": head, "url": f"u/{number}"}
    if state == "MERGED":
        pr["mergedAt"] = "2026-10-04T12:00:00Z"
    if created is not None:
        pr["createdAt"] = created
    return pr


def _gh(monkeypatch, *, prs=None, unanswered=(), issue=None, closing=None):
    """Stub gh: `pr list --head B` answers prs[B] (or no PR, or a rate limit for `unanswered`),
    `issue view` answers `issue` (default OPEN), `pr view <issue#>` is not a PR, and the closing
    references' merge-time read answers `closing` when one is given."""
    asked: list[str] = []

    def fake_run(argv, capture_output=True, text=True, **_kw):
        verb = tuple(argv[1:3])
        if verb == ("pr", "view"):
            return subprocess.CompletedProcess(argv, 1, "", "Could not resolve to a PR")
        if verb == ("pr", "list"):
            head = argv[argv.index("--head") + 1]
            asked.append(head)
            if head in unanswered:
                return subprocess.CompletedProcess(argv, *RATE_LIMIT)
            if head in (prs or {}):
                return subprocess.CompletedProcess(argv, 0, json.dumps([prs[head]]), "")
            return subprocess.CompletedProcess(argv, *NO_PR)
        if verb == ("issue", "view"):
            body = issue or {"state": "OPEN", "closedByPullRequestsReferences": []}
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        if verb == ("api", "graphql") and closing is not None:
            return subprocess.CompletedProcess(argv, *closing)
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
    return asked


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability, failure_class, notes "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


def _local_run(branches: list[str] | None, *, status: str = "read", start: int = 1_700_000_000):
    feedback.record_run(RUN, TARGET, "implement", "codex", mode="local")
    if branches is None:
        return
    rows = [{"branch": b, "sha": "a" * 40, "pushed_ts": start + 60} for b in branches]
    feedback.record_run_pushes(
        RUN,
        status=status,
        reason="workspace_missing" if status == "unreadable" else None,
        window_start=start,
        window_end=start + 3600,
        branches=rows if status == "read" else [],
    )


def test_a_pr_the_run_opened_on_its_pushed_branch_is_credited(brain, monkeypatch):
    _local_run(["fix/7-own-name"])
    asked = _gh(
        monkeypatch, prs={"fix/7-own-name": _merged(8, "fix/7-own-name", _iso(1_700_000_300))}
    )
    result = outcomes.ingest_modes("local")
    assert result["push_records"] == {"credited": 1, "rejected": 0, "absent": 0, "unreadable": 0}
    merged, verdict, durability, failure_class, notes = _row(RUN)
    assert (merged, verdict, durability, failure_class) == (1, "PASS", "pending", None)
    assert asked == ["fix/7-own-name"], "the pushed branch answered; nothing else is asked"
    # The notes ARE the durability sweep's explicit-PR contract: it resolves the exact PR.
    assert durability_sweep.EXPLICIT_MERGED_PR_RE.search(notes), notes
    assert durability_sweep._explicit_merged_pr_target(TARGET, notes) == "o/r#8"


def test_a_pr_on_a_pushed_branch_that_predates_the_run_is_not_credited(brain, monkeypatch):
    """The run pushed onto a PR that already existed (another run's, or a person's). It did not
    OPEN it, so it is not this run's delivery, and that branch is not asked again by the name
    patterns, which would otherwise hand the same PR back as a match."""
    _local_run(["orchestrator/issue-7"])
    closing = {"number": 8, "repository": {"name": "r", "owner": {"login": "o"}}}
    asked = _gh(
        monkeypatch,
        prs={"orchestrator/issue-7": _merged(8, "orchestrator/issue-7", _iso(1_699_999_000))},
        issue={"state": "CLOSED", "closedByPullRequestsReferences": [closing]},
        # #8 merged at 12:00:00Z and the issue closed a second later: it counts.
        closing=outcomes._closing_read("2026-10-04T12:00:01Z", (8, "2026-10-04T12:00:00Z")),
    )
    result = outcomes.ingest_modes("local")
    assert result["push_records"]["rejected"] == 1 and result["push_records"]["credited"] == 0
    assert asked.count("orchestrator/issue-7") == 1, asked
    merged, verdict, _durability, failure_class, notes = _row(RUN)
    assert (merged, verdict, failure_class) == (None, None, feedback.UNATTRIBUTED_CLOSING_PR)
    assert "predates the run: #8 on orchestrator/issue-7" in notes, notes


def test_an_unanswered_lookup_on_a_pushed_branch_is_retried_not_resolved(brain, monkeypatch):
    _local_run(["fix/7-own-name"])
    _gh(monkeypatch, unanswered={"fix/7-own-name"})
    first = outcomes.ingest_modes("local")
    assert (first["recorded"], first["unanswered"]) == (0, 1) and _row(RUN) is None, first
    _gh(monkeypatch, prs={"fix/7-own-name": _merged(8, "fix/7-own-name", _iso(1_700_000_300))})
    second = outcomes.ingest_modes("local")
    assert (second["recorded"], second["push_records"]["credited"]) == (1, 1), second


def test_a_pr_record_that_cannot_be_checked_is_unknown_not_credited(brain, monkeypatch):
    """No createdAt: neither credited (unknown is not yes) nor rejected (unknown is not no)."""
    _local_run(["fix/7-own-name"])
    _gh(monkeypatch, prs={"fix/7-own-name": _merged(8, "fix/7-own-name", None)})
    result = outcomes.ingest_modes("local")
    assert (result["recorded"], result["unanswered"]) == (0, 1), result
    assert result["push_records"]["rejected"] == 0 and _row(RUN) is None


@pytest.mark.parametrize(
    ("record", "absent", "unreadable"),
    [(None, 1, 0), ("unreadable", 0, 1), ("empty", 0, 0)],
    ids=["no-record", "unreadable", "read-empty"],
)
def test_without_a_usable_record_ingest_asks_exactly_what_it_asked_before(
    brain, monkeypatch, record, absent, unreadable
):
    if record is None:
        _local_run(None)
    elif record == "unreadable":
        _local_run([], status="unreadable")
    else:
        _local_run([])
    asked = _gh(monkeypatch)
    result = outcomes.ingest_modes("local")
    assert asked == outcomes._local_candidate_branches(7, "codex"), asked
    assert result["push_records"] == {
        "credited": 0,
        "rejected": 0,
        "absent": absent,
        "unreadable": unreadable,
    }, result


def test_an_open_pr_on_a_pushed_branch_waits_and_a_closed_one_is_this_runs_failure(
    brain, monkeypatch
):
    _local_run(["fix/7-own-name"])
    opened = _merged(8, "fix/7-own-name", _iso(1_700_000_300), state="OPEN")
    _gh(monkeypatch, prs={"fix/7-own-name": opened})
    waiting = outcomes.ingest_modes("local")
    assert waiting["recorded"] == 0 and waiting["skipped_details"][0]["reason"] == "open_pr"
    closed = _merged(8, "fix/7-own-name", _iso(1_700_000_300), state="CLOSED")
    # Closed unmerged, it is read once more (outcomes.judge_replacement): no PR links issue #7.
    nothing_carries = outcomes._replacement_answer(8, "a" * 40)
    _gh(monkeypatch, prs={"fix/7-own-name": closed}, closing=nothing_carries)
    done = outcomes.ingest_modes("local")
    assert done["push_records"]["credited"] == 1, done
    assert done["replacements"] == {"credited": 0, "waiting": 0, "unattributable": 0, "failed": 1}
    merged, verdict, durability, _failure_class, notes = _row(RUN)
    assert (merged, verdict, durability) == (0, "FAIL", "abandoned")
    assert notes == (
        "local delegate PR #8 closed unmerged on the branch this run pushed (fix/7-own-name); "
        "replacement check: no PR linked to its issue carries the head aaaaaaaaaaaa of its closed "
        "PR #8 (fix/7-own-name); no other PR links it"
    )


def test_push_record_counts_read_zero_when_drained_and_none_without_a_local_path(brain):
    """What the counts print with nothing to count, proven by construction: four zeros for the local
    path, never a missing key, and None (not zeros) when no local path ran at all."""
    local = outcomes.ingest_modes("local")
    assert local["push_records"] == {"credited": 0, "rejected": 0, "absent": 0, "unreadable": 0}
    assert outcomes.ingest_modes("remote")["push_records"] is None
    both = outcomes.ingest_modes("both")
    assert both["push_records"] == local["push_records"]
    assert [row["push_records"] for row in both["results"]] == [None, local["push_records"]]
