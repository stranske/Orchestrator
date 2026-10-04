"""The durability sweep finds THE merge a row recorded, or closes the row; it never skips forever.

THE LATCH (measured 2026-10-04). The sweep selected every merged outcome still `pending` and could
judge only a direct PR target. For an issue target it fetched the merge commit by the ISSUE number,
it searched a legacy local delegate's branches in the remote order, it read the newest PR on a
reused branch instead of the merged one, and a role run has no repository target at all. So 111
rows were re-skipped on every run ("missing merge commit SHA" 101, "missing or invalid mergedAt" 10)
inside one `skipped` count shared with rows merely waiting out the grace period, while
`feedback._is_success('pending', 'PASS')` scored 27 of them as successes.

THE RULE. `find_merge` answers found / unanswered / unjudgeable. Found is a direct PR target, an
outcome note naming "PR #N merged", or exactly ONE merge on the run's own branch inside the run's
window. Unanswered (GitHub did not answer) stays pending and is retried. Unjudgeable is closed for
good: durability `unjudgeable`, failure class `unjudgeable_merge`, which every learner excludes.
Unknown is not false, so it is never a FAIL. A role run waits for its acting run, whose verdict
propagates. Every row left pending names its drain, and the summary prints pending beside drainable.

No real API: the resolver's gh is an injected fake that answers by argv, and the revert and fix
searches go through a stubbed `_run_json`.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

import durability_sweep
import feedback
import outcomes

NOW = 1_790_000_000  # 2026-09-21T14:13:20Z
DAY = 86400
NOT_A_PR = (
    "GraphQL: Could not resolve to a PullRequest with the number of {n}. (repository.pullRequest)"
)


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(
    number: int, *, merged_at: int | None, state: str = "MERGED", files=("src/app.py",)
) -> dict:
    return {
        "number": number,
        "state": state,
        "mergedAt": _iso(merged_at) if merged_at else None,
        "mergeCommit": {"oid": f"sha{number}"} if merged_at else None,
        "baseRefName": "main",
        "headRefName": f"head-{number}",
        "files": [{"path": path} for path in files],
    }


class FakeGh:
    """Answers `gh pr view N` from `prs`, `gh pr list --head B` from `branches`, and records argv."""

    def __init__(self, prs=None, branches=None, fail=None):
        self.prs = prs or {}
        self.branches = branches or {}
        self.fail = fail or set()
        self.calls: list[list[str]] = []

    def __call__(self, args, **_kw):
        self.calls.append(args)
        if args[1:3] == ["pr", "view"]:
            number = int(args[3])
            if ("view", number) in self.fail:
                return None, "API rate limit exceeded for user ID 23046322."
            if number in self.prs:
                return self.prs[number], None
            return None, NOT_A_PR.format(n=number)
        if args[1:3] == ["pr", "list"]:
            branch = args[args.index("--head") + 1]
            if ("list", branch) in self.fail:
                return None, "API rate limit exceeded for user ID 23046322."
            return [
                {key: pr[key] for key in ("number", "state", "mergedAt")}
                for pr in self.branches.get(branch, [])
            ], None
        raise AssertionError(f"unexpected gh call: {args}")

    def heads(self) -> list[str]:
        return [args[args.index("--head") + 1] for args in self.calls if "--head" in args]


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    # Revert and fix searches answer "nothing found, search complete"; the base-commit scan finds
    # no revert commit. Every verdict below is therefore decided by find_merge alone.
    monkeypatch.setattr(durability_sweep, "_run_json", lambda args, **_kw: [])
    return tmp_path


def _merged_run(
    run_id, target, *, mode, agent="codex", ts=NOW - 40 * DAY, notes=None, recorded=None
):
    feedback.record_run(run_id, target, "implement", agent, mode=mode, ts=ts)
    feedback.record_outcome(
        run_id,
        adjudicated_verdict="PASS",
        merged=True,
        durability="pending",
        notes=notes or "remote keepalive PR merged; durability pending sweep",
    )
    if recorded is not None:
        with feedback._conn() as c:
            c.execute(
                "UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?", (recorded, run_id)
            )


def _sweep(gh, **kw):
    return durability_sweep.sweep_durability(
        _gh=gh, _now=NOW, _verifier_fetch_fn=lambda _repo, _nums: {}, **kw
    )


def _row(run_id):
    with feedback._conn() as c:
        return c.execute(
            "SELECT durability, failure_class, notes FROM outcomes WHERE run_id=?", (run_id,)
        ).fetchone()


# --- found: the merge the row recorded -----------------------------------------------------------


def test_direct_pr_target_is_judged_from_one_lookup(brain):
    """A local run on an existing PR: the target IS the PR. 28 of the 111 were this shape."""
    _merged_run("local-on-pr", "o/r#572", mode="local", agent="cursor")
    gh = FakeGh(prs={572: _pr(572, merged_at=NOW - 20 * DAY)})
    res = _sweep(gh)
    assert (res["checked"], res["judged"], res["durable"], res["skipped"]) == (1, 1, 1, 0)
    assert _row("local-on-pr")[0] == "durable"
    assert len(gh.calls) == 1, gh.calls  # the view carries the merge commit, files and base


def test_legacy_local_issue_target_judges_the_one_merge_on_its_own_branch(brain):
    """The three measured defects at once: issue number, branch order, reused branch.

    A `full`-mode local delegate (a legacy local mode) worked issue #978 on orchestrator/issue-978.
    That branch later carried a second, CLOSED PR (#993, newer). The merge this row recorded is
    #979, and only #979 may be judged.
    """
    _merged_run("legacy-full", "o/r#978", mode="full", ts=NOW - 40 * DAY, recorded=NOW - 39 * DAY)
    merged = _pr(979, merged_at=NOW - 39 * DAY - 3600)
    closed_newer = _pr(993, merged_at=None, state="CLOSED")
    gh = FakeGh(
        prs={979: merged, 993: closed_newer},
        branches={
            "orchestrator/issue-978": [closed_newer, merged],
            "codex/issue-978": [_pr(990, merged_at=None, state="CLOSED")],
        },
    )
    res = _sweep(gh)
    assert gh.heads() == ["orchestrator/issue-978"], "a legacy local run was looked up as remote"
    assert res["durable"] == 1 and res["skipped"] == 0, res
    durability, failure_class, notes = _row("legacy-full")
    assert durability == "durable" and failure_class is None
    assert "judged #979 (the one merge on orchestrator/issue-978)" in notes, notes
    assert ["gh", "pr", "view", "979"] == gh.calls[-1][:4], "details fetched by the issue number"


def test_remote_issue_target_uses_the_labelled_agents_branch(brain):
    _merged_run("remote:o/r#1262:codex", "o/r#1262", mode="remote", recorded=NOW - 30 * DAY)
    merged = _pr(1265, merged_at=NOW - 31 * DAY)
    gh = FakeGh(prs={1265: merged}, branches={"codex/issue-1262": [merged]})
    res = _sweep(gh)
    assert gh.heads() == ["codex/issue-1262"] and res["durable"] == 1, (gh.calls, res)


def test_outcome_note_naming_the_pr_is_looked_up_directly(brain):
    """The pushed-branch contract: a note "PR #N merged" names the merge, so N is viewed directly."""
    _merged_run(
        "local-noted",
        "o/r#5901",
        mode="local",
        notes="local delegate PR #5908 merged on the branch this run pushed (fix/5901-x)",
    )
    gh = FakeGh(prs={5908: _pr(5908, merged_at=NOW - 20 * DAY)})
    res = _sweep(gh)
    assert res["durable"] == 1 and gh.heads() == [], (res, gh.calls)
    assert "judged #5908 (named in the outcome notes)" in _row("local-noted")[2]


def test_missing_merge_sha_costs_the_commit_scan_not_the_verdict():
    """A completed "no revert PR" search answers alone; a missing SHA must not turn it to unknown."""
    pr = {"repo": "o/r", "number": 7, "mergedAt": _iso(NOW - 20 * DAY), "baseRefName": "main"}
    cache = {"o/r": ([], False)}  # the revert search completed and found nothing
    assert durability_sweep._live_revert_status(pr, revert_cache=cache) == (
        False,
        "no revert PR or base-branch revert commit found",
    )


# --- unjudgeable: closed for good, excluded, never a failure --------------------------------------


@pytest.mark.parametrize(
    "branches, why",
    [
        ({}, "no PR on orchestrator/issue-2672"),
        ({"orchestrator/issue-2672": [_pr(2700, merged_at=None, state="CLOSED")]}, "none merged"),
        (
            {"orchestrator/issue-2672": [_pr(2701, merged_at=NOW - 60 * DAY)]},
            "land before the run started",
        ),
        (
            {
                "orchestrator/issue-2672": [
                    _pr(2702, merged_at=NOW - 39 * DAY),
                    _pr(2703, merged_at=NOW - 39 * DAY - 60),
                ]
            },
            "2 merges on orchestrator/issue-2672 fall in the run's window",
        ),
        (
            {
                "orchestrator/issue-2672": [
                    _pr(3000 + i, merged_at=None, state="CLOSED") for i in range(30)
                ]
            },
            "cannot be read whole",
        ),
    ],
)
def test_no_single_own_merge_closes_the_row_as_unjudgeable(brain, branches, why):
    _merged_run(
        "composer-run", "o/r#2672", mode="composer", ts=NOW - 40 * DAY, recorded=NOW - 38 * DAY
    )
    res = _sweep(FakeGh(branches=branches))
    assert (res["unjudgeable"], res["judged"], res["skipped"]) == (1, 0, 0), res
    durability, failure_class, notes = _row("composer-run")
    assert (durability, failure_class) == ("unjudgeable", feedback.UNJUDGEABLE_MERGE)
    assert why in notes, notes
    # Terminal: the next sweep does not see it at all.
    assert _sweep(FakeGh(branches=branches))["checked"] == 0


def test_a_non_merged_direct_pr_is_unjudgeable_not_reopened(brain):
    _merged_run("closed-pr", "o/r#44", mode="local")
    res = _sweep(FakeGh(prs={44: _pr(44, merged_at=None, state="CLOSED")}))
    assert res["unjudgeable"] == 1 and "reads CLOSED with no merge" in _row("closed-pr")[2]


def test_unjudgeable_is_excluded_from_learning_and_never_a_failure():
    assert feedback.UNJUDGEABLE_MERGE in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert not feedback._has_outcome_evidence(
        feedback.DURABILITY_UNJUDGEABLE, "PASS", None, failure_class=feedback.UNJUDGEABLE_MERGE
    )
    assert feedback.DURABILITY_UNJUDGEABLE not in feedback.CAPABILITY_REGRESSION_DURABILITY


# --- unanswered: retried, never read as "no PR" ------------------------------------------------


def test_a_lookup_that_did_not_answer_stays_pending_and_is_retried(brain):
    _merged_run("rate-limited", "o/r#30", mode="full", recorded=NOW - 30 * DAY)
    merged = _pr(31, merged_at=NOW - 31 * DAY)
    branches = {"orchestrator/issue-30": [merged]}
    for fail in ({("view", 30)}, {("list", "orchestrator/issue-30")}, {("view", 31)}):
        res = _sweep(FakeGh(prs={31: merged}, branches=branches, fail=fail))
        assert (res["skipped"], res["drainable"], res["drains"]["retry"]) == (1, 1, 1), (fail, res)
        assert res["unjudgeable"] == 0 and _row("rate-limited")[0] == "pending", fail
    assert _sweep(FakeGh(prs={31: merged}, branches=branches))["durable"] == 1


def test_only_the_not_a_pull_request_answer_means_issue():
    assert durability_sweep.GH_NOT_A_PULL_REQUEST in NOT_A_PR.format(n=2553)


# --- role runs: drained by their acting run -------------------------------------------------


def _role_lineage(acting_target="o/r#20"):
    feedback.record_role_run("role:triage:gemini:1", "triage", "triage:1-items", "gemini")
    feedback.record_run(
        "remote:o/r#20:gemini",
        acting_target,
        "testgen",
        "gemini",
        mode="remote",
        ts=NOW - 40 * DAY,
        influenced_by_role_run_ids=["role:triage:gemini:1"],
    )
    feedback.record_outcome(
        "remote:o/r#20:gemini", adjudicated_verdict="PASS", merged=True, durability="pending"
    )


def test_a_role_run_inherits_its_acting_runs_verdict_in_the_same_sweep(brain):
    _role_lineage()
    assert _row("role:triage:gemini:1")[0] == "pending"  # propagated merged PASS, still pending
    merged = _pr(21, merged_at=NOW - 39 * DAY)
    res = _sweep(FakeGh(prs={21: merged}, branches={"gemini/issue-20": [merged]}))
    assert (res["checked"], res["durable"], res["lineage_resolved"], res["skipped"]) == (2, 1, 1, 0)
    assert _row("role:triage:gemini:1")[0] == "durable"


def test_a_role_run_inherits_unjudgeable_and_its_exclusion(brain):
    _role_lineage()
    res = _sweep(FakeGh(branches={}))
    assert res["unjudgeable"] == 1 and res["lineage_resolved"] == 1, res
    assert _row("role:triage:gemini:1")[:2] == ("unjudgeable", feedback.UNJUDGEABLE_MERGE)


def test_a_role_run_waits_while_its_acting_run_is_pending(brain):
    _role_lineage()
    res = _sweep(FakeGh(fail={("view", 20)}))
    assert res["drains"] == {"grace": 0, "retry": 1, "acting_run": 1}, res
    assert (res["skipped"], res["drainable"], res["undrainable"]) == (2, 2, 0)


def test_a_role_run_whose_verdict_never_propagated_is_reported_undrainable(brain):
    _role_lineage()
    with feedback._conn() as c:  # acting run judged, propagation lost: the defect this must show
        c.execute("UPDATE outcomes SET durability='durable' WHERE run_id='remote:o/r#20:gemini'")
    res = _sweep(FakeGh())
    assert (res["skipped"], res["drainable"], res["undrainable"]) == (1, 0, 1), res
    assert "UNDRAINABLE 1" in res["line"] and "did not propagate" in res["line"], res["line"]


# --- the report: pending beside drainable, and the drained state reachable -------------------


def test_grace_rows_are_drainable_on_a_named_date(brain):
    _merged_run("young", "o/r#9", mode="local", ts=NOW - 3 * DAY)
    res = _sweep(FakeGh(prs={9: _pr(9, merged_at=NOW - 2 * DAY)}))
    assert (res["skipped"], res["drainable"], res["drains"]["grace"]) == (1, 1, 1), res
    due = _iso(NOW + 5 * DAY)[:10]
    assert res["next_grace_drain"] == due
    assert res["line"].endswith(f"next grace drain {due}), undrainable 0"), res["line"]


def test_fully_drained_is_printed_and_reachable(brain):
    """Latched-gate question 4: the drained rendering, proven by construction, counts by ==."""
    empty = _sweep(FakeGh())
    assert (empty["checked"], empty["skipped"], empty["undrainable"]) == (0, 0, 0)
    assert empty["line"].endswith("pending 0, fully drained"), empty["line"]
    _merged_run("closes", "o/r#2672", mode="composer")
    closed = _sweep(FakeGh())
    assert (closed["checked"], closed["unjudgeable"], closed["skipped"]) == (1, 1, 0)
    assert closed["line"].endswith("pending 0, fully drained"), closed["line"]


def test_legacy_local_modes_are_one_definition_for_ingest_and_sweep(brain):
    """`outcomes._pending_runs('local')` selects exactly the runs `is_local_delegate` accepts."""
    cases = [
        (mode, target)
        for mode in ("local", "full", "composer", "cheap", "remote", "role", None)
        for target in ("o/r#5", "triage:1-items")
    ]
    for i, (mode, target) in enumerate(cases):
        feedback.record_run(
            f"run-{i}", target, "implement", "codex", mode=mode, ts=int(time.time())
        )
    selected = {row["run_id"] for row in outcomes._pending_runs("local")}
    expected = {
        f"run-{i}"
        for i, (mode, target) in enumerate(cases)
        if outcomes.is_local_delegate(mode, target)
    }
    assert selected == expected and len(expected) == 5, (selected, expected)
