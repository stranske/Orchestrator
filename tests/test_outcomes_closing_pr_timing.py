"""A closing PR counts only if it MERGED by the time the issue closed (2026-10-04).

THE DEFECT. #411 made a closed issue's closing PRs decide its verdict: none, an abandoned FAIL; one or
more, someone delivered, so the run is `unattributed_closing_pr`. It read them from `gh issue view
--json closedByPullRequestsReferences`, and GitHub lists there EVERY PR whose body links the issue
with a closing keyword, including PRs created long after the issue closed, which cannot have closed
it. Workflows#2819 was closed by hand at 2026-08-15T13:18:25Z with no commit; its references #3402,
#3442 and #3688 were created on 2026-09-07, 09-14 and 10-02 by automated maintenance PRs. A fresh
ingest would have recorded it "closed by a PR no candidate branch produced (#3402, #3442, #3688)".
At ingest time, shortly after a close, late references usually do not exist yet, so the rule misread
re-evaluations and late ingests.

THE RULE (owner decision on stranske/Orchestrator#439). A reference counts only if it merged at or
before the issue's `closedAt`, within `outcomes.CLOSING_PR_MERGE_SLACK_SECONDS`. One that merged
later, or has not merged, is named in the notes and decides nothing. One whose merge time cannot be
read is UNKNOWN: the run is unanswered (skipped, retried, counted), never "no closing PR" (#404).

MEASURED (read-only GraphQL, 2026-10-04): over 1,600 closed fleet issues, the PR that closed one
merged 0-2 s before its closedAt (once 460 s before) and never after; 8 of 1,135 references merged
after the close, the nearest 113 s after. Of the 25 recorded acting rows this rule decides, none
changes verdict. No real API: `subprocess.run` is replaced by a stub that answers by verb.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

import durability_sweep
import feedback
import outcomes

NO_PR = (0, "[]", "")
RATE_LIMIT = (1, "", "API rate limit exceeded for user ID 23046322.")
SLACK = outcomes.CLOSING_PR_MERGE_SLACK_SECONDS
# Workflows#2819: closed by hand; every reference was created and merged weeks later.
CLOSED_2819 = "2026-08-15T13:18:25Z"
LATE_2819 = (
    (3402, "2026-09-07T14:58:30Z"),
    (3442, "2026-09-15T01:33:20Z"),
    (3688, "2026-10-02T07:23:59Z"),
)
RESOLVERS = {"remote": outcomes._pr_state, "local": outcomes._local_pr_state}


def issue_view(*numbers: int, state: str = "CLOSED") -> tuple:
    """What `gh issue view --json ...,closedByPullRequestsReferences` prints: no merge times."""
    refs = [
        {"number": n, "repository": {"name": "r", "owner": {"login": "o"}}, "url": f"u/{n}"}
        for n in numbers
    ]
    body = {"state": state, "closedAt": CLOSED_2819, "closedByPullRequestsReferences": refs}
    return (0, json.dumps(body), "")


def read(closed: str | None, *refs: tuple) -> tuple:
    """The merge-time read's answer: one page, the close time and each (number, mergedAt)."""
    return outcomes._closing_read(closed, *refs)


def pages(*bodies: dict) -> tuple:
    """A merge-time read of several pages, each `{"closedAt", "nodes", "more"}`."""
    out = []
    for body in bodies:
        conn = {"pageInfo": {"hasNextPage": body["more"]}, "nodes": body["nodes"]}
        issue = {"closedAt": body["closedAt"], "closedByPullRequestsReferences": conn}
        out.append({"data": {"repository": {"issueOrPullRequest": issue}}})
    return (0, json.dumps(out), "")


def graphql_call_problems(argv: list) -> list[str]:
    """What GitHub and gh would refuse in a `gh api graphql` call, by their own rules. Every declared
    variable must arrive with its declared type: `-f` always sends a string, and `-F` sends digits
    as an Int. A `--paginate` read must declare `$endCursor`, pass it as `after:` and ask for
    `pageInfo { hasNextPage endCursor }`, or gh refetches the first page. Empty means accepted.
    The stub answers a refused call as GitHub does, so a read built wrongly cannot pass a test that
    expects a verdict: in production it would be unanswered on every ingest, forever."""
    sent: dict = {}
    for flag, pair in zip(argv, argv[1:]):
        if flag in ("-f", "-F"):
            key, _, value = pair.partition("=")
            sent[key] = int(value) if flag == "-F" and value.lstrip("-").isdigit() else value
    query = str(sent.pop("query", ""))
    problems = []
    for name, kind, required in re.findall(r"\$(\w+):\s*(\w+)(!?)", query.split("{", 1)[0]):
        if name not in sent:
            problems += [f"${name} is required and was not sent"] if required else []
        elif (kind == "Int") != isinstance(sent[name], int):
            problems.append(f"Variable ${name} of type {kind} was provided invalid value")
    if "--paginate" in argv:
        for needle in (
            "$endCursor: String",
            "after: $endCursor",
            "pageInfo { hasNextPage endCursor }",
        ):
            if query.count(needle) != 1:
                problems.append(f"--paginate needs {needle!r} exactly once in the query")
    return problems


def node(number: int, merged: str | None, state: str = "MERGED", repo: str = "o/r") -> dict:
    return {
        "number": number,
        "state": state,
        "mergedAt": merged,
        "repository": {"nameWithOwner": repo},
    }


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def gh(monkeypatch):
    """Install a stub gh. Every candidate branch has no PR and the target is not a PR number; the
    issue view answers `issue`, and the merge-time read answers `closing`. Returns the verbs asked.
    """
    asked: list[str] = []

    def install(*, issue=None, closing=None):
        def fake_run(argv, capture_output=True, text=True, **_kw):
            verb = " ".join(argv[1:3])
            asked.append(verb)
            if verb == "pr view":
                return subprocess.CompletedProcess(argv, 1, "", "Could not resolve to a PR")
            if verb == "pr list":
                return subprocess.CompletedProcess(argv, *NO_PR)
            if verb == "issue view" and issue is not None:
                return subprocess.CompletedProcess(argv, *issue)
            if verb == "api graphql" and closing is not None:
                assert "--paginate" in argv and "--slurp" in argv, argv
                problems = graphql_call_problems(argv)
                if problems:  # refused, as GitHub refuses it: exit 1 and the reason
                    return subprocess.CompletedProcess(argv, 1, "", "gh: " + "; ".join(problems))
                return subprocess.CompletedProcess(argv, *closing)
            raise AssertionError(f"unexpected gh call: {argv}")

        monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
        return asked

    return install


def closed_issue(gh, *numbers: int, closing: tuple) -> dict | None:
    gh(issue=issue_view(*numbers), closing=closing)
    return outcomes._closed_issue_without_branch_pr("o/r", 2819, "orchestrator/issue-2819")


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability, failure_class, notes "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


# --- the rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_references_created_after_the_close_do_not_count(gh, path):
    """Workflows#2819: closed by hand, its three references created and merged 23 to 48 days later.
    None closed it, so the closed issue keeps #411's abandoned FAIL on the local path and the
    keepalive-row path, and the notes name every reference it did not count."""
    gh(issue=issue_view(3402, 3442, 3688), closing=read(CLOSED_2819, *LATE_2819))
    state = RESOLVERS[path]("o/r#2819", "gemini")
    assert state["lookup_status"] in outcomes.CLOSED_ISSUE_LOOKUPS, state
    assert (state["closing_pr_count"], state["closing_prs"]) == (0, []), state
    outcome = outcomes.state_to_outcome(state)
    assert outcome is not None, state
    assert (outcome["merged"], outcome["adjudicated_verdict"], outcome["durability"]) == (
        False,
        "FAIL",
        "abandoned",
    ), "a reference that merged after the issue closed was counted as the PR that closed it"
    assert outcome.get("failure_class") is None, outcome
    notes = outcome["notes"]
    assert "no closing PR merged by the close" in notes, notes
    assert "no closing PR references" not in notes, "the issue HAS references; none counted"
    for number, merged in LATE_2819:
        assert f"#{number} (merged {merged})" in notes, notes
    assert CLOSED_2819 in notes, notes


def test_a_delegation_never_reads_late_references_as_a_closing_pr(gh):
    """The delegation resolver takes the same closed-issue answer. Whatever a delegation with no
    PR of its own and no counting reference records, it is never #411's someone-delivered, and
    its notes name the references it did not count, as every closed-issue verdict's do."""
    calls = gh(issue=issue_view(3402, 3442, 3688), closing=read(CLOSED_2819, *LATE_2819))
    state = outcomes._delegated_pr_state("o/r#2819", "gemini", 1_786_000_000)
    assert calls == ["pr view", "pr list", "issue view", "api graphql"], calls
    assert state["lookup_status"] == "closed_issue_no_remote_pr", state
    assert state["closing_pr_count"] == 0 and len(state["late_closing_prs"]) == 3, state
    outcome = outcomes.state_to_outcome(state)
    assert outcome is not None and outcome.get("failure_class") != feedback.UNATTRIBUTED_CLOSING_PR
    assert "#3402 (merged 2026-09-07T14:58:30Z)" in outcome["notes"], (
        "a closed-issue verdict's notes must name the references it did not count "
        "(`late_closing_prs`), on this path too",
        outcome,
    )
    assert "no closing PR references" not in outcome["notes"], (
        "the issue HAS references, none of which counted: say so, never that it has none",
        outcome,
    )


def test_a_pr_merged_the_second_before_the_close_counts(gh):
    """The normal case, Workflows#3050: #3067 merged at 06:49:47 and its merge commit closed the
    issue at 06:49:48. It is the closing PR: someone delivered, not this run."""
    state = closed_issue(
        gh, 3067, closing=read("2026-08-13T06:49:48Z", (3067, "2026-08-13T06:49:47Z"))
    )
    assert (state["closing_pr_count"], state["closing_prs"], state["late_closing_prs"]) == (
        1,
        ["#3067"],
        [],
    ), state
    outcome = outcomes.state_to_outcome(state)
    assert outcome["failure_class"] == feedback.UNATTRIBUTED_CLOSING_PR, outcome
    assert "#3067" in outcome["notes"] and "not counted" not in outcome["notes"], outcome


def test_a_pr_merged_long_before_a_later_close_still_counts(gh):
    """Fine-Art-Archive#166: #168 and #173 merged, the issue was reopened and closed by hand five
    hours later. Both merged before the close, so both count, as recorded."""
    closing = read(
        "2026-06-28T23:24:48Z", (168, "2026-06-28T17:56:22Z"), (173, "2026-06-28T18:06:36Z")
    )
    state = closed_issue(gh, 168, 173, closing=closing)
    assert (state["closing_pr_count"], state["closing_prs"]) == (2, ["#168", "#173"]), state


def test_counted_and_late_references_are_both_named(gh):
    """Trend#5816, closed by hand at 00:39:26: #5834 merged 15 minutes before the close, #5836 113 s
    after it. The verdict comes from the first; the notes still name the second as not counted."""
    closing = read(
        "2026-08-13T00:39:26Z", (5834, "2026-08-13T00:24:50Z"), (5836, "2026-08-13T00:41:19Z")
    )
    outcome = outcomes.state_to_outcome(closed_issue(gh, 5834, 5836, closing=closing))
    assert outcome["failure_class"] == feedback.UNATTRIBUTED_CLOSING_PR, outcome
    assert "closed by a PR no candidate branch produced (#5834)" in outcome["notes"], outcome
    assert "not counted: #5836 (merged 2026-08-13T00:41:19Z)" in outcome["notes"], outcome
    assert "closing_pr_count=1" in outcome["notes"], outcome


@pytest.mark.parametrize(
    "offset, counts", [(-86400, True), (0, True), (SLACK, True), (SLACK + 1, False)]
)
def test_the_slack_is_one_constant_at_its_boundary(offset, counts):
    """GitHub writes a merge and the close it causes in the same second or the next. A reference
    merged up to the slack AFTER the close still counts; one second more does not."""
    closed = outcomes.utc_epoch.from_iso(CLOSED_2819)
    merged = outcomes._iso(closed + offset)
    assert outcomes._merged_by_close(node(1, merged), closed) is counts
    assert 0 < SLACK < 113, "the nearest late reference measured merged 113 s after its close"


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_a_reference_that_never_merged_is_answered_not_unknown(gh, state):
    """An OPEN or closed-unmerged PR that links the issue did not close it: GitHub answered, so it
    is named as not merged, never an unknown that holds the run."""
    closing = pages({"closedAt": CLOSED_2819, "more": False, "nodes": [node(9, None, state)]})
    issue_state = closed_issue(gh, 9, closing=closing)
    assert issue_state["late_closing_prs"] == ["#9 (not merged)"], issue_state
    outcome = outcomes.state_to_outcome(issue_state)
    assert outcome["adjudicated_verdict"] == "FAIL" and "#9 (not merged)" in outcome["notes"]


def test_a_reference_in_another_repository_is_named_with_it(gh):
    closing = pages(
        {
            "closedAt": CLOSED_2819,
            "more": False,
            "nodes": [node(77, "2026-08-15T13:18:24Z", repo="o/other")],
        }
    )
    state = closed_issue(gh, 77, closing=closing)
    assert state["closing_prs"] == ["o/other#77"], state


def test_every_page_of_references_is_read(gh):
    """The counting reference is on the second page: the read follows the cursor, and the close
    time it compares against is the one every page agrees on."""
    closing = pages(
        {"closedAt": CLOSED_2819, "more": True, "nodes": [node(1, "2026-09-01T00:00:00Z")]},
        {"closedAt": CLOSED_2819, "more": False, "nodes": [node(2, "2026-08-15T13:18:24Z")]},
    )
    state = closed_issue(gh, 1, 2, closing=closing)
    assert (state["closing_prs"], state["late_closing_prs"]) == (
        ["#2"],
        ["#1 (merged 2026-09-01T00:00:00Z)"],
    ), state


def test_the_merge_time_read_is_a_call_github_accepts(monkeypatch):
    """The read GitHub would refuse is the one no stub-only test notices: with `-f number=` (a
    string for `Int!`) or no `after: $endCursor`, every closed issue with a reference would read
    unanswered, or page forever, on every ingest. Assert the real argv, then that the check fires.
    """
    captured: list = []

    def fake_run(argv, capture_output=True, text=True, **_kw):
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, *read(CLOSED_2819))

    monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
    assert outcomes._closing_pr_merges("o/r", 2819) == {"closedAt": CLOSED_2819, "refs": []}
    (argv,) = captured
    assert graphql_call_problems(argv) == [], graphql_call_problems(argv)
    as_string = [("-f" if arg == "-F" else arg) for arg in argv]
    assert graphql_call_problems(as_string) == [
        "Variable $number of type Int was provided invalid value"
    ]
    unpaged = [arg.replace("after: $endCursor", "") for arg in argv]
    assert graphql_call_problems(unpaged) == [
        "--paginate needs 'after: $endCursor' exactly once in the query"
    ]


def test_no_reference_needs_no_merge_time_read(gh):
    """A closed issue nothing links: #411's FAIL from one read. The merge-time read costs a call
    only when there is a reference to time."""
    asked = gh(issue=issue_view())
    state = outcomes._closed_issue_without_branch_pr("o/r", 711, "orchestrator/issue-711")
    assert asked == ["issue view"], asked
    assert (state["closing_pr_count"], state["late_closing_prs"]) == (0, []), state
    assert "no closing PR references" in outcomes.state_to_outcome(state)["notes"]


# --- unknown is not "no closing PR" -----------------------------------------------------------

UNREADABLE = {
    "unparseable-merge-time": read(CLOSED_2819, (3402, "not a time")),
    "merged-without-a-time": pages(
        {"closedAt": CLOSED_2819, "more": False, "nodes": [node(3402, None, "MERGED")]}
    ),
    "no-merge-time-field": pages(
        {"closedAt": CLOSED_2819, "more": False, "nodes": [{"number": 3402, "state": "MERGED"}]}
    ),
    "no-state-no-time": pages(
        {"closedAt": CLOSED_2819, "more": False, "nodes": [{"number": 3402, "mergedAt": None}]}
    ),
    "no-number": pages(
        {"closedAt": CLOSED_2819, "more": False, "nodes": [{"mergedAt": None, "state": "OPEN"}]}
    ),
    "rate-limited": RATE_LIMIT,
    "not-json": (0, "<html>502</html>", ""),
    "no-pages": (0, "[]", ""),
    "page-without-a-list": (
        0,
        json.dumps([{"data": {"repository": {"issueOrPullRequest": None}}}]),
        "",
    ),
    "a-pull-request-number": (
        0,
        json.dumps([{"data": {"repository": {"issueOrPullRequest": {}}}}]),
        "",
    ),
    "says-more-pages-exist": pages(
        {"closedAt": CLOSED_2819, "more": True, "nodes": [node(3402, "2026-09-07T14:58:30Z")]}
    ),
    "pages-disagree-on-the-close": pages(
        {"closedAt": CLOSED_2819, "more": True, "nodes": []},
        {"closedAt": "2026-08-16T00:00:00Z", "more": False, "nodes": []},
    ),
    "reopened-between-reads": read(None, (3402, "2026-08-15T13:18:24Z")),
}


@pytest.mark.parametrize("answer", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_a_merge_time_that_cannot_be_read_leaves_the_run_unanswered(gh, answer):
    """An unread reference may be the one that closed the issue, so it decides nothing: the answer
    carries no state, the outcome is None, and the run is skipped and asked again."""
    state = closed_issue(gh, 3402, closing=answer)
    assert state is not None and state["lookup_status"] == "closing_pr_lookup_failed", state
    assert state.get("error"), "an unanswered read names why"
    assert "state" not in state and outcomes.state_to_outcome(state) is None, state
    assert "closing_pr_lookup_failed" in outcomes.UNANSWERED_LOOKUPS


def test_an_unread_merge_time_skips_counts_and_then_drains(brain, gh):
    """End to end, and the latched-gate questions 1 and 2: the drain is the next ingest, and it
    runs while the run is held. First pass: no row, counted `unanswered`, the skip names the read.
    Second pass, answered: the decided verdict. Third: nothing pending, `unanswered` reads 0."""
    feedback.record_run("o__r_2819-codex-1", "o/r#2819", "implement", "codex", mode="local")
    gh(issue=issue_view(3402, 3442, 3688), closing=RATE_LIMIT)
    first = outcomes.ingest_modes("local")
    assert (first["recorded"], first["unanswered"]) == (0, 1), first
    assert first["skipped_details"][0]["reason"] == "closing_pr_lookup_failed", first
    assert _row("o__r_2819-codex-1") is None, "an unknown wrote an outcome row"

    gh(issue=issue_view(3402, 3442, 3688), closing=read(CLOSED_2819, *LATE_2819))
    second = outcomes.ingest_modes("local")
    assert (second["recorded"], second["unanswered"], second["unattributed"]) == (1, 0, 0), second
    merged, verdict, durability, failure_class, notes = _row("o__r_2819-codex-1")
    assert (merged, verdict, durability, failure_class) == (0, "FAIL", "abandoned", None)
    assert "#3688 (merged 2026-10-02T07:23:59Z)" in notes, notes

    third = outcomes.ingest_modes("local")
    assert (third["pending"], third["recorded"], third["unanswered"]) == (0, 0, 0), third


def test_the_notes_never_name_a_late_reference_as_a_merged_pr():
    """The durability sweep resolves a run's PR from `PR #N merged` in its notes. A reference that
    did not close the issue must never be named in that form, or a sweep could judge it as the
    run's merge."""
    for count, closing in ((0, []), (1, ["#5815"])):
        outcome = outcomes.state_to_outcome(
            {
                "lookup_status": "closed_issue_no_branch_pr",
                "state": "CLOSED",
                "closedAt": CLOSED_2819,
                "closing_pr_count": count,
                "closing_prs": closing,
                "late_closing_prs": ["#3402 (merged 2026-09-07T14:58:30Z)", "#9 (not merged)"],
            }
        )
        assert outcome is not None and "#3402" in outcome["notes"], outcome
        assert not durability_sweep.EXPLICIT_MERGED_PR_RE.search(outcome["notes"]), outcome
