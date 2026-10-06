"""A local run's own PR closed unmerged is not its FAIL when a replacement carrying its head merged.

THE DEFECT (2026-10-05, verifier K7 in the review of stranske/Orchestrator#461). The opener lane
rehomes a PR keepalive cannot drive: one on `orchestrator/issue-N` gets a replacement opened from a
registry branch AT THE SAME HEAD SHA (`codex/issue-N-<slug>`, "Closes #N", "Supersedes #old"), and
the old PR is closed. Outcome ingest read the closed PR and recorded the run's FAIL, with no failure
class, so every route learner scored it. Measured on a Brain copy and read-only GitHub (2026-10-05):
of the 9 local closed-unmerged FAIL rows of the 90 days, 5 (all codex: Ready#548, Orchestrator#213,
Counter_Risk#986, Inv-Man-Intake#934, Pension-Data#865) had closed their run's PR 1.2 to 3.5 h
after it opened, and a replacement opened within seconds merged the same day CARRYING THE RUN'S
OWN HEAD COMMIT. Their FAILs were written after those merges.

THE RULE. Before the FAIL, one read asks which PRs linked to the run's issue carry the closed PR's
head commit (`outcomes.judge_replacement`). Exactly one that merged after the run started, for a PR
the run opened (created at or after it started, #441's window rule), is the run's PASS, written
through the same path as its own merge and named `PR #M merged` for the durability sweep. A
replacement with different commits is different work and never credited: Orchestrator#199's PR #200
was superseded by #208, a separate implementation, and stays FAIL. Three outcomes stay apart: a read
GitHub did not answer is unknown (skipped, retried, never a PASS); no carrier is the FAIL; an answer
that cannot single one out is `unattributed_replacement`, which no learner scores. An OPEN carrier
waits, as the run's own open PR would.

The two real fixtures are GitHub's answers for Orchestrator#213/#214 and #199/#200, read
2026-10-05. No real API: `subprocess.run` is a stub that answers by verb.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from test_outcomes_closing_pr_timing import graphql_call_problems

import durability_sweep
import feedback
import outcomes

REPO = "stranske/Orchestrator"
NOT_A_PR = (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 213.")
RATE_LIMIT = (1, "", "gh: API rate limit exceeded for user ID 23046322.")

# Orchestrator#213: run started 2026-09-04T00:33:01Z, its PR #214 (orchestrator/issue-213) opened at
# 00:49:13Z and closed at 04:05:20Z, replacement #219 merged at 06:26:24Z with the head 2829492 as its
# FIRST commit; the FAIL was written at 17:40:37Z.
RUN_213 = "stranske__Orchestrator_213-codex-1788481981941284000"
START_213, FAIL_WRITTEN_213, OPENED_214 = 1788481981, 1788543637, 1788482953
HEAD_214 = "282949241f3c33569c1854e2f9f91f2be387207f"
CLOSED_214 = {
    "number": 214,
    "title": "fix: follow Trend default branch for provisioning",
    "url": "https://github.com/stranske/Orchestrator/pull/214",
    "headRefName": "orchestrator/issue-213",
    "state": "CLOSED",
    "mergedAt": None,
    "closedAt": "2026-09-04T04:05:20Z",
    "createdAt": "2026-09-04T00:49:13Z",
}
REPLACEMENT_219 = outcomes._replacement_link(
    219,
    "MERGED",
    "2026-09-04T06:26:24Z",
    [HEAD_214, "9dcafe02fb60006b9f4e3214e29b73807a273430"],
    repo=REPO,
    branch="codex/issue-213-trend-default-branch",
)
# Orchestrator#199: run started 2026-09-03T10:17:06Z; its PR #200 closed at 13:29:46Z, after #208, a
# different implementation with commits of its own, merged at 12:38:57Z.
RUN_199 = "stranske__Orchestrator_199-codex-1788430626365158000"
START_199, FAIL_WRITTEN_199 = 1788430626, 1788453831
HEAD_200 = "3f8fbfd21488d17fad0884ed1d85dfdd800b6ba0"
CLOSED_200 = {
    **CLOSED_214,
    "number": 200,
    "headRefName": "orchestrator/issue-199",
    "closedAt": "2026-09-03T13:29:46Z",
    "createdAt": "2026-09-03T10:23:46Z",
}
DIFFERENT_208 = outcomes._replacement_link(
    208,
    "MERGED",
    "2026-09-03T12:38:57Z",
    ["fc84de6a3ce04562ec960b6cf835e97616bd2b19", "cdaeb4c809cca01432e68b7a1762b5a6177d3e09"],
    repo=REPO,
    branch="codex/issue-199-coverage-source-workflows",
)
# What gh prints for an issue number that does not resolve: exit 1, and the data beside the error.
ISSUE_GONE = (
    1,
    json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {"number": 214, "headRefOid": HEAD_214},
                    "issueOrPullRequest": None,
                }
            },
            "errors": [
                {
                    "type": "NOT_FOUND",
                    "path": ["repository", "issueOrPullRequest"],
                    "message": "Could not resolve to an issue or pull request with the number "
                    "of 213.",
                }
            ],
        }
    ),
    "gh: Could not resolve to an issue or pull request with the number of 213.",
)


def answer_213(*links: dict, more: bool = False) -> dict:
    return json.loads(outcomes._replacement_answer(214, HEAD_214, *links, more=more)[1])


def judge_213(
    *links: dict,
    started: int | None = START_213,
    opened: int | None = OPENED_214,
    more: bool = False,
) -> dict:
    return outcomes.judge_replacement(
        answer_213(*links, more=more),
        repo=REPO,
        pr_number=214,
        started_ts=started,
        opened_ts=opened,
    )


def link(number: int, state: str, merged: str | None, oids: list[str], **kw) -> dict:
    return outcomes._replacement_link(number, state, merged, oids, repo=REPO, **kw)


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(outcomes.REPLACEMENT_RECHECK_SWITCH, raising=False)
    return tmp_path


@pytest.fixture
def gh(monkeypatch):
    """Install a stub gh: the target is not a PR, `pr list --head B` answers `prs[B]` (no PR
    otherwise), and the replacement read answers `read`. Returns the calls made, in order."""
    calls: list[list[str]] = []

    def install(*, prs: dict | None = None, read: tuple | None = None):
        def fake_run(argv, capture_output=True, text=True, **_kw):
            calls.append(list(argv))
            verb = tuple(argv[1:3])
            if verb == ("pr", "view"):
                return subprocess.CompletedProcess(argv, *NOT_A_PR)
            if verb == ("pr", "list"):
                head = argv[argv.index("--head") + 1]
                found = (prs or {}).get(head)
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps([found] if found else []), ""
                )
            if verb == ("api", "graphql") and read is not None:
                problems = graphql_call_problems(argv)
                if problems:  # refused, as GitHub refuses it
                    return subprocess.CompletedProcess(argv, 1, "", "; ".join(problems))
                return subprocess.CompletedProcess(argv, *read)
            raise AssertionError(f"unexpected gh call: {argv}")

        monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
        return calls

    return install


def record_run(run_id: str, issue: int, ts: int) -> None:
    feedback.record_run(run_id, f"{REPO}#{issue}", "implement", "codex", mode="local", ts=ts)


def row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability, failure_class, notes "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


def whole(run_id: str) -> tuple:
    """Everything the re-judge may touch: the whole outcome row and record_outcome's events."""
    with feedback._conn() as c:
        outcome = c.execute("SELECT * FROM outcomes WHERE run_id=?", (run_id,)).fetchone()
        events = c.execute(
            "SELECT * FROM completion_events WHERE run_id=? AND producer=? ORDER BY event_id",
            (run_id, outcomes.RECORD_OUTCOME_PRODUCER),
        ).fetchall()
    return outcome, events


# --- the three cases the rule exists for ------------------------------------------------------


def test_the_same_commit_replacement_is_the_runs_merge(brain, gh):
    """Orchestrator#213: #219 carries #214's head. Credited as the run's merge, pending durability,
    through the ordinary ingest write, and named so the durability sweep judges #219 itself."""
    record_run(RUN_213, 213, START_213)
    calls = gh(
        prs={"orchestrator/issue-213": CLOSED_214},
        read=outcomes._replacement_answer(214, HEAD_214, REPLACEMENT_219),
    )
    result = outcomes.ingest_modes("local")
    assert result["replacements"] == {
        "credited": 1,
        "waiting": 0,
        "unattributable": 0,
        "failed": 0,
        "not_read": 0,
    }, result
    merged, verdict, durability, failure_class, notes = row(RUN_213)
    assert (merged, verdict, durability, failure_class) == (1, "PASS", "pending", None), notes
    assert durability_sweep._explicit_merged_pr_target(f"{REPO}#213", notes) == f"{REPO}#219", (
        "the sweep must resolve the replacement's merge from the notes, never the closed PR",
        notes,
    )
    assert notes.startswith(outcomes.REPLACEMENT_CREDIT), notes
    assert HEAD_214[:12] in notes and "closed PR #214 (orchestrator/issue-213)" in notes, notes
    read = [argv for argv in calls if argv[1:3] == ["api", "graphql"]]
    assert len(read) == 1 and graphql_call_problems(read[0]) == [], read


def test_a_different_implementation_is_never_credited(brain, gh):
    """Orchestrator#199: #208 closed the issue with commits of its own; #200's head is not among
    them. The run's PR was dropped for different work, so its FAIL stands, and the notes say what
    was read, in a form the sweep can never mistake for a merge of this run."""
    record_run(RUN_199, 199, START_199)
    gh(
        prs={"orchestrator/issue-199": CLOSED_200},
        read=outcomes._replacement_answer(200, HEAD_200, DIFFERENT_208),
    )
    result = outcomes.ingest_modes("local")
    assert result["replacements"]["failed"] == 1 and result["replacements"]["credited"] == 0
    merged, verdict, durability, failure_class, notes = row(RUN_199)
    assert (merged, verdict, durability, failure_class) == (0, "FAIL", "abandoned", None), notes
    assert "no PR linked to its issue carries the head 3f8fbfd21488" in notes, notes
    assert "#208 (merged 2026-09-03T12:38:57Z)" in notes, notes
    assert not durability_sweep.EXPLICIT_MERGED_PR_RE.search(notes), notes


def test_no_replacement_is_the_fail(brain, gh):
    """No PR links the issue at all: the closed PR was the run's end, as before."""
    record_run(RUN_213, 213, START_213)
    gh(prs={"orchestrator/issue-213": CLOSED_214}, read=outcomes._replacement_answer(214, HEAD_214))
    outcomes.ingest_modes("local")
    merged, verdict, durability, failure_class, notes = row(RUN_213)
    assert (merged, verdict, durability, failure_class) == (0, "FAIL", "abandoned", None)
    assert notes.endswith("of its closed PR #214 (orchestrator/issue-213); no other PR links it")


def test_an_issue_that_does_not_resolve_is_answered_not_retried(brain, gh):
    """gh exits 1 for an issue number GitHub cannot resolve, and prints the data beside the error.
    That is an answer, no PR can link a missing issue, and the FAIL stands as it did before the
    read existed. Reading it as unanswered would retry the run on every ingest, for ever."""
    record_run(RUN_213, 213, START_213)
    gh(prs={"orchestrator/issue-213": CLOSED_214}, read=ISSUE_GONE)
    result = outcomes.ingest_modes("local")
    assert (result["recorded"], result["unanswered"]) == (1, 0), result
    merged, verdict, _durability, _failure_class, notes = row(RUN_213)
    assert (merged, verdict) == (0, "FAIL") and "does not resolve on GitHub" in notes, notes


def test_a_target_that_is_itself_a_pr_is_marked_not_read(brain, gh):
    """When the run's target number turns out to be a pull request, no issue links to it: the
    FAIL stands, marked, so the re-judge never selects it."""
    record_run(RUN_213, 213, START_213)
    pr_answer = json.loads(outcomes._replacement_answer(214, HEAD_214)[1])
    pr_answer["data"]["repository"]["issueOrPullRequest"] = {"__typename": "PullRequest"}
    gh(prs={"orchestrator/issue-213": CLOSED_214}, read=(0, json.dumps(pr_answer), ""))
    result = outcomes.ingest_modes("local")
    assert result["replacements"]["not_read"] == 1, result
    assert row(RUN_213)[1] == "FAIL" and outcomes.REPLACEMENT_CHECK in row(RUN_213)[4]
    assert outcomes._replacement_recheck_runs() == []


# --- unknown is not a verdict -----------------------------------------------------------------

UNREADABLE = {
    "rate-limited": RATE_LIMIT,
    "not-json": (0, "<html>502</html>", ""),
    "no-repository": (0, json.dumps({"data": {"repository": None}}), ""),
    "head-missing": (0, outcomes._replacement_answer(214, "")[1], ""),
    "head-of-another-pr": outcomes._replacement_answer(999, HEAD_214, REPLACEMENT_219),
    "issue-without-a-type": (
        0,
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {"number": 214, "headRefOid": HEAD_214},
                        "issueOrPullRequest": {},
                    }
                }
            }
        ),
        "",
    ),
    "merged-link-without-a-time": outcomes._replacement_answer(
        214, HEAD_214, {**REPLACEMENT_219, "mergedAt": None}
    ),
    "commit-without-an-oid": outcomes._replacement_answer(
        214,
        HEAD_214,
        {**REPLACEMENT_219, "commits": {"totalCount": 1, "nodes": [{"commit": {}}]}},
    ),
    "link-in-an-unknown-state": outcomes._replacement_answer(
        214, HEAD_214, {**REPLACEMENT_219, "state": "LOCKED"}
    ),
    "link-without-its-repository-name": outcomes._replacement_answer(
        214, HEAD_214, {**REPLACEMENT_219, "repository": {}}
    ),
}


@pytest.mark.parametrize("read", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_a_read_that_does_not_answer_writes_nothing_and_is_asked_again(brain, gh, read):
    record_run(RUN_213, 213, START_213)
    gh(prs={"orchestrator/issue-213": CLOSED_214}, read=read)
    first = outcomes.ingest_modes("local")
    assert (first["recorded"], first["unanswered"]) == (0, 1), first
    assert first["skipped_details"][0]["reason"] == "replacement_lookup_failed", first
    assert row(RUN_213) is None, "an unknown was written as a verdict"
    gh(
        prs={"orchestrator/issue-213": CLOSED_214},
        read=outcomes._replacement_answer(214, HEAD_214, REPLACEMENT_219),
    )
    second = outcomes.ingest_modes("local")
    assert (second["recorded"], second["unanswered"]) == (1, 0), second
    assert row(RUN_213)[1] == "PASS"


def test_a_closed_pr_record_without_a_number_is_unanswered_not_failed(gh):
    calls = gh(read=outcomes._replacement_answer(214, HEAD_214, REPLACEMENT_219))
    numberless = {**CLOSED_214, "number": None, "lookup_status": "found"}
    state = outcomes._attach_replacement(numberless, REPO, 213, START_213)
    assert state["lookup_status"] == "replacement_lookup_failed", state
    assert outcomes.state_to_outcome(state) is None and calls == []


# --- what a complete answer cannot decide -----------------------------------------------------


@pytest.mark.parametrize(
    "links, more, started, opened, why",
    [
        (
            (
                REPLACEMENT_219,
                link(230, "MERGED", "2026-09-05T00:00:00Z", [HEAD_214], branch="b/230"),
            ),
            False,
            START_213,
            OPENED_214,
            "2 merged PR(s) carry its head or may",
        ),
        (
            (link(231, "MERGED", "2026-09-04T06:00:00Z", ["f" * 40], total=101),),
            False,
            START_213,
            OPENED_214,
            "commits cut at 100",
        ),
        ((), True, START_213, OPENED_214, "over 100 PRs link the issue"),
        ((REPLACEMENT_219,), False, None, OPENED_214, "no start to place the merge against"),
        ((REPLACEMENT_219,), False, START_213, None, "records no creation time"),
        (
            (REPLACEMENT_219,),
            False,
            START_213,
            START_213 - 60,
            "was opened before the run started",
        ),
    ],
    ids=[
        "two-merged-carriers",
        "a-list-cut-short",
        "links-past-the-limit",
        "no-run-start",
        "no-pr-creation-time",
        "pr-opened-before-the-run",
    ],
)
def test_an_answer_that_cannot_single_one_out_is_unattributed(links, more, started, opened, why):
    judged = judge_213(*links, started=started, opened=opened, more=more)
    assert judged["status"] == "unattributable" and why in judged["reason"], judged
    outcome = outcomes.state_to_outcome({**CLOSED_214, "replacement": judged})
    assert outcome is not None
    assert (outcome["merged"], outcome["adjudicated_verdict"], outcome["durability"]) == (
        None,
        None,
        "abandoned",
    ), outcome
    assert outcome["failure_class"] == feedback.UNATTRIBUTED_REPLACEMENT
    assert feedback.UNATTRIBUTED_REPLACEMENT in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert feedback.UNATTRIBUTED_REPLACEMENT in outcomes.UNATTRIBUTED_CLASSES
    assert not durability_sweep.EXPLICIT_MERGED_PR_RE.search(outcome["notes"]), outcome


def test_another_runs_closed_pr_on_the_branch_earns_this_run_nothing(brain, gh):
    """Run A's PR #214 was rehomed into #219. Run B starts later on the same issue, pushes nothing,
    and the branch walk hands it #214, A's PR. #219 merges after B started, but #214 was opened
    before B started, so it is not shown to be B's: no PASS for A's work, and no FAIL either."""
    record_run("run-b", 213, OPENED_214 + 3600)
    gh(
        prs={"orchestrator/issue-213": CLOSED_214},
        read=outcomes._replacement_answer(214, HEAD_214, REPLACEMENT_219),
    )
    outcomes.ingest_modes("local")
    merged, verdict, _durability, failure_class, notes = row("run-b")
    assert (merged, verdict, failure_class) == (None, None, feedback.UNATTRIBUTED_REPLACEMENT)
    assert "was opened before the run started" in notes, notes


def test_a_carrier_merged_before_the_run_started_is_not_its_work():
    """A head that merged before the run began predates it: the PR the run is charged with did not
    carry its work. Named, never credited, and the FAIL stands (#441's window rule)."""
    early = link(150, "MERGED", "2026-09-01T00:00:00Z", [HEAD_214], branch="codex/issue-213-x")
    judged = judge_213(early)
    assert judged["status"] == "none" and judged["before_run"], judged
    outcome = outcomes.state_to_outcome({**CLOSED_214, "replacement": judged})
    assert outcome["adjudicated_verdict"] == "FAIL", outcome
    assert "after the run started (only #150 (merged 2026-09-01T00:00:00Z))" in outcome["notes"]


def test_links_elsewhere_and_the_closed_pr_itself_carry_nothing():
    """Another repository's PR cannot carry this repository's commit, and #214 is the closed PR."""
    elsewhere = link(219, "MERGED", "2026-09-04T06:26:24Z", [HEAD_214])
    elsewhere["repository"]["nameWithOwner"] = "stranske/Workflows"
    itself = link(214, "MERGED", "2026-09-04T06:26:24Z", [HEAD_214])
    assert judge_213(elsewhere, itself)["status"] == "none"


def test_the_repository_name_is_matched_as_github_matches_it():
    """GitHub names are case-insensitive: a target recorded in other case is the same repository."""
    shouty = link(219, "MERGED", "2026-09-04T06:26:24Z", [HEAD_214])
    shouty["repository"]["nameWithOwner"] = REPO.upper()
    assert judge_213(shouty)["status"] == "credited"


def test_a_cut_list_that_still_shows_the_head_is_a_carrier():
    """The rehome puts the run's head FIRST in the replacement, so a long replacement still shows
    it: found within the read, it carries, whatever the list's length."""
    long_one = link(219, "MERGED", "2026-09-04T06:26:24Z", [HEAD_214, "9" * 40], total=400)
    assert judge_213(long_one)["status"] == "credited"


# --- an open carrier waits, and the wait drains -----------------------------------------------


def test_an_open_carrier_waits_and_its_merge_drains_the_wait(brain, gh):
    """Latched-gate questions 1, 2 and 4. The wait is decremented by the carrier settling, read by
    the next ingest, which runs while the run waits; when nothing waits every count prints 0."""
    record_run(RUN_213, 213, START_213)
    still_open = {**REPLACEMENT_219, "state": "OPEN", "mergedAt": None}
    gh(
        prs={"orchestrator/issue-213": CLOSED_214},
        read=outcomes._replacement_answer(214, HEAD_214, still_open),
    )
    waiting = outcomes.ingest_modes("local")
    assert waiting["replacements"]["waiting"] == 1 and waiting["recorded"] == 0, waiting
    assert waiting["skipped_details"][0]["reason"] == "open_replacement_pr", waiting
    assert waiting["unanswered"] == 0, "a wait on an open PR is not an unanswered read"
    assert row(RUN_213) is None
    gh(
        prs={"orchestrator/issue-213": CLOSED_214},
        read=outcomes._replacement_answer(214, HEAD_214, REPLACEMENT_219),
    )
    merged = outcomes.ingest_modes("local")
    assert merged["replacements"]["credited"] == 1 and row(RUN_213)[1] == "PASS", merged
    drained = outcomes.ingest_modes("local")
    assert drained["replacements"] == dict.fromkeys(
        ("credited", "waiting", "unattributable", "failed", "not_read"), 0
    ), "a drained ingest prints every count as zero, never a missing key"
    assert outcomes.ingest_modes("remote")["replacements"] is None


# --- the paths that must not read it ----------------------------------------------------------


def test_a_closed_pr_without_the_read_is_the_fail_it_always_was():
    """keepalive_outcomes and every injected state map through `state_to_outcome` with no
    `replacement` key: their closed PRs keep the old verdict, word for word."""
    outcome = outcomes.state_to_outcome({"state": "CLOSED"})
    assert outcome == {
        "merged": False,
        "adjudicated_verdict": "FAIL",
        "durability": "abandoned",
        "notes": "remote keepalive PR closed unmerged",
    }


def test_a_remote_run_and_a_pr_target_are_never_read(brain, gh, monkeypatch):
    """A remote run IS its PR (keepalive records the replacement as its own run), and a local run
    whose target is a PR has no issue to read: neither makes the replacement read."""
    calls = gh(prs={"codex/issue-213": CLOSED_214})
    remote = outcomes._pr_state(f"{REPO}#213", "codex")
    assert remote["state"] == "CLOSED" and "replacement" not in remote
    assert not any(argv[1:3] == ["api", "graphql"] for argv in calls), calls

    def pr_target(argv, capture_output=True, text=True, **_kw):
        assert argv[1:3] == ["pr", "view"], argv
        return subprocess.CompletedProcess(argv, 0, json.dumps(CLOSED_214), "")

    monkeypatch.setattr(outcomes.subprocess, "run", pr_target)
    direct = outcomes._local_pr_state(f"{REPO}#214", "codex", started_ts=START_213)
    assert direct["direct_target_pr"] and direct["replacement"] == {"status": "not_read"}
    outcome = outcomes.state_to_outcome(direct)
    assert outcome["adjudicated_verdict"] == "FAIL" and "not read" in outcome["notes"], outcome


# --- the verifier evidence of the replacement stays with its own run --------------------------


def test_a_credited_replacement_leaves_the_verifier_evidence_to_the_prs_own_run(brain):
    """The keepalive run of #219 IS #219. A local run credited with #219's merge inherited it, so
    it never competes for #219's verifier verdict: two claims would credit nobody."""
    credited = outcomes._replacement_outcome(
        CLOSED_214, judge_213(REPLACEMENT_219), "remote keepalive PR closed unmerged"
    )
    record_run(RUN_213, 213, START_213)
    feedback.record_outcome(RUN_213, **credited)
    key = ("stranske/orchestrator", 219)
    assert durability_sweep._merged_verifier_candidates()[key] == {
        RUN_213
    }, "with no other claimant the inheriting run is the PR's only candidate"
    assert durability_sweep.verifier_candidate_run_ids(REPO, 219, prospective_run_id="ka") == {
        "ka"
    }, "keepalive recording #219's own run must not be blocked by the inherited claim"
    feedback.record_run(
        "ka", f"{REPO}#219", "implement", "codex", mode="remote", source="keepalive", pr_number=219
    )
    feedback.record_outcome("ka", merged=True, adjudicated_verdict="PASS", durability="pending")
    assert durability_sweep._merged_verifier_candidates()[key] == {"ka"}
    assert ("ka", "stranske/orchestrator", 219) in durability_sweep._missing_verifier_runs()


# --- re-judging the rows written before the rule ---------------------------------------------


def seed_fail(run_id: str, issue: int, ts: int, written: int, notes: str) -> None:
    record_run(run_id, issue, ts)
    feedback.record_outcome(
        run_id, merged=False, adjudicated_verdict="FAIL", durability="abandoned", notes=notes
    )
    with feedback._conn() as c:
        c.execute("UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?", (written, run_id))


def resolver(states: dict):
    return lambda run: states.get(run["target"])


def seed_the_two(brain) -> dict:
    seed_fail(RUN_213, 213, START_213, FAIL_WRITTEN_213, "remote keepalive PR closed unmerged")
    seed_fail(RUN_199, 199, START_199, FAIL_WRITTEN_199, "remote keepalive PR closed unmerged")
    credited = judge_213(REPLACEMENT_219)
    kept = outcomes.judge_replacement(
        json.loads(outcomes._replacement_answer(200, HEAD_200, DIFFERENT_208)[1]),
        repo=REPO,
        pr_number=200,
        started_ts=START_199,
        opened_ts=START_199 + 400,
    )
    return {
        f"{REPO}#213": {**CLOSED_214, "lookup_status": "found", "replacement": credited},
        f"{REPO}#199": {**CLOSED_200, "lookup_status": "found", "replacement": kept},
    }


def test_the_recheck_credits_the_replacement_once_and_keeps_the_other_fail(brain):
    states = seed_the_two(brain)
    before = {run: whole(run) for run in (RUN_213, RUN_199)}
    first = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert (first["selected"], first["credited"], first["kept_fail"]) == (2, 1, 1), first
    assert first["snapshotted"] == 2 and first["stopped"] is None, first
    merged, verdict, durability, failure_class, notes = row(RUN_213)
    assert (merged, verdict, durability, failure_class) == (1, "PASS", "pending", None)
    assert durability_sweep._explicit_merged_pr_target(f"{REPO}#213", notes) == f"{REPO}#219"
    assert notes.endswith("remote keepalive PR closed unmerged (re-judged 2026-10-06; was FAIL)")
    kept = row(RUN_199)
    assert kept[:4] == (0, "FAIL", "abandoned", None) and "replacement check" in kept[4], kept
    # Idempotent: both rows now carry the check, so the next run selects nothing and says so.
    again = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_100)
    assert again["selected"] == 0 and again["waiting"] == 0, again
    # A verifier verdict that arrives while the row is credited is part of what the undo reverts.
    feedback.record_outcome(RUN_213, verifier_verdict="PASS")
    # The undo restores the snapshot taken before the first write: the whole outcome row, and the
    # completion events record_outcome rewrote, field for field.
    undone = outcomes.undo_replacement_recheck()
    assert (undone["restored"], undone["skipped"]) == (2, []), undone
    assert {run: whole(run) for run in (RUN_213, RUN_199)} == before


def test_an_unattributable_rejudge_is_undone_exactly(brain):
    states = seed_the_two(brain)
    states[f"{REPO}#213"]["replacement"] = judge_213(
        REPLACEMENT_219, link(230, "MERGED", "2026-09-05T00:00:00Z", [HEAD_214], branch="b/230")
    )
    before = whole(RUN_213)
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert result["unattributable"] == 1, result
    assert row(RUN_213)[3] == feedback.UNATTRIBUTED_REPLACEMENT
    with feedback._conn() as c:
        origin = c.execute(
            "SELECT failure_class_origin FROM outcomes WHERE run_id=?", (RUN_213,)
        ).fetchone()[0]
    assert origin == "own", "the class is the run's own, as every learner reads it"
    outcomes.undo_replacement_recheck()
    assert whole(RUN_213) == before, "the undo clears the class and its origin with the rest"


def test_an_old_pr_target_fail_is_settled_on_the_first_pass(brain, monkeypatch):
    """A FAIL written before the read existed, on a run whose target is itself a PR: the re-judge
    resolves it with ingest's own resolver, finds no issue to read, and keeps the FAIL with the
    marker on its first pass, never waiting out the horizon on a read that is never made."""
    seed_fail("pr-target", 214, START_213, FAIL_WRITTEN_213, "remote keepalive PR closed unmerged")

    def pr_view(argv, capture_output=True, text=True, **_kw):
        assert argv[1:3] == ["pr", "view"], argv
        return subprocess.CompletedProcess(argv, 0, json.dumps(CLOSED_214), "")

    monkeypatch.setattr(outcomes.subprocess, "run", pr_view)
    result = outcomes.recheck_replacements(now=1_791_300_000)
    assert (result["selected"], result["kept_fail"], result["waiting"]) == (1, 1, 0), result
    notes = row("pr-target")[4]
    assert row("pr-target")[1] == "FAIL" and "not read" in notes, notes
    assert outcomes.recheck_replacements(now=1_791_300_100)["selected"] == 0


def test_a_credited_rejudge_leads_its_notes_so_the_sweep_judges_the_replacement(brain):
    """The durability sweep takes the FIRST `PR #N merged` in a row's notes as the merge to judge.
    A re-judged row keeps its old notes, and old notes are free text: the credited clause must
    stand in front of them, or a stray mention would hand the sweep someone else's merge."""
    states = seed_the_two(brain)
    stray = "remote keepalive PR closed unmerged; reviewed: PR #77 merged the docs half"
    with feedback._conn() as c:
        c.execute("UPDATE outcomes SET notes=? WHERE run_id=?", (stray, RUN_213))
    outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    notes = row(RUN_213)[4]
    assert durability_sweep._explicit_merged_pr_target(f"{REPO}#213", notes) == f"{REPO}#219"
    assert stray in notes, "the old notes are kept, behind the verdict"


def test_the_recheck_judges_only_the_pr_its_fail_was_written_about(brain):
    """The resolver asks a branch for its NEWEST PR. A later PR on the same branch (a later run's)
    is not the one this FAIL was about, whatever it carries: the row keeps its FAIL."""
    states = seed_the_two(brain)
    later = {**CLOSED_214, "number": 260, "closedAt": "2026-09-20T00:00:00Z"}
    states[f"{REPO}#213"] = {**states[f"{REPO}#213"], **later}
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert result["not_the_recorded_pr"] == 1 and result["credited"] == 0, result
    assert row(RUN_213)[1] == "FAIL" and "not the PR this FAIL was written about" in row(RUN_213)[4]


def test_the_recheck_waits_on_no_answer_then_keeps_the_fail_at_its_horizon(brain):
    states = seed_the_two(brain)
    states[f"{REPO}#213"] = {"lookup_status": "replacement_lookup_failed", "error": "rate limit"}
    start = 1_791_300_000
    first = outcomes.recheck_replacements(_resolve=resolver(states), now=start)
    assert (first["waiting"], first["selected"]) == (1, 2), first
    assert first["drains_by"] == outcomes._iso(start + 7 * 86400), first
    assert row(RUN_213)[1] == "FAIL" and "replacement check" not in row(RUN_213)[4]
    late = outcomes.recheck_replacements(_resolve=resolver(states), now=start + 7 * 86400)
    assert (late["closed_at_horizon"], late["waiting"]) == (1, 0), late
    assert "undecided for 7 days (GitHub did not answer: rate limit); FAIL kept" in row(RUN_213)[4]
    assert outcomes.recheck_replacements(_resolve=resolver(states), now=start)["selected"] == 0


def test_the_recheck_selects_only_local_runs(brain):
    """The keepalive writer records about 790 closed-unmerged FAILs in 90 days with the same note.
    They are their PRs' own runs, not local runs, and the re-judge must never select one."""
    seed_the_two(brain)
    for run_id, mode, source in (
        ("ka-closed", "remote", "keepalive"),
        ("remote-closed", "remote", "orchestrator_remote"),
    ):
        feedback.record_run(run_id, f"{REPO}#214", "implement", "codex", mode=mode, source=source)
        feedback.record_outcome(
            run_id,
            merged=False,
            adjudicated_verdict="FAIL",
            durability="abandoned",
            notes="remote keepalive PR closed unmerged",
        )
    selected = {run["run_id"] for run in outcomes._replacement_recheck_runs()}
    assert selected == {RUN_213, RUN_199}, selected


@pytest.mark.parametrize(
    "answer, stops",
    [
        ("decline", True),
        ("Declined.", True),
        ("decline: keep the FAILs", True),
        ("No objection, run it", False),
        ("no problem", False),
        ("do not run", False),
        ("run it", False),
    ],
)
def test_only_a_decline_stops_the_recheck(brain, answer, stops):
    """The question says: answer "decline" to stop it. That one word decides, so "no objection"
    can never read as "no"."""
    states = seed_the_two(brain)
    question = feedback.record_owner_question(
        f"{outcomes.REPLACEMENT_RECHECK_TOKEN} Re-judge the closed-PR FAIL rows?", "run it"
    )
    feedback.answer_owner_question(question["question_id"], answer)
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert bool(result["stopped"]) is stops, result
    assert result["selected"] == 2, "a stopped re-judge still says what it would select"


def test_the_newest_answer_decides(brain):
    states = seed_the_two(brain)
    old = feedback.record_owner_question(
        f"{outcomes.REPLACEMENT_RECHECK_TOKEN} first ask", "run it"
    )
    feedback.answer_owner_question(old["question_id"], "decline")
    with feedback._conn() as c:
        c.execute(
            "UPDATE owner_questions SET answered_ts=100 WHERE question_id=?", (old["question_id"],)
        )
    new = feedback.record_owner_question(
        f"{outcomes.REPLACEMENT_RECHECK_TOKEN} second ask", "run it"
    )
    feedback.answer_owner_question(new["question_id"], "run it after all")
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert result["stopped"] is None and result["credited"] == 1, result


def test_the_switch_stops_the_recheck_and_still_counts_its_selection(brain, monkeypatch):
    states = seed_the_two(brain)
    monkeypatch.setenv(outcomes.REPLACEMENT_RECHECK_SWITCH, "0")
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert result["selected"] == 2 and result["stopped"], (
        "a stopped re-judge must still say what it would select, never print a drained zero",
        result,
    )
    assert row(RUN_213)[1] == "FAIL" and not outcomes._recheck_undo_path().exists()


def test_a_dry_run_recheck_writes_nothing(brain):
    states = seed_the_two(brain)
    before = {run: whole(run) for run in (RUN_213, RUN_199)}
    result = outcomes.recheck_replacements(
        dry_run=True, _resolve=resolver(states), now=1_791_300_000
    )
    assert (result["credited"], result["snapshotted"]) == (1, 0), result
    assert {run: whole(run) for run in (RUN_213, RUN_199)} == before
    assert not outcomes._recheck_undo_path().exists()


# --- the command line the daily cadence runs --------------------------------------------------


@pytest.mark.parametrize(
    "argv, recheck",
    [
        (["--mode", "local"], {"dry_run": False}),
        (["--mode", "both", "--dry-run"], {"dry_run": True}),
        (["--mode", "remote"], None),
    ],
)
def test_the_local_ingest_command_runs_the_recheck(monkeypatch, capsys, argv, recheck):
    """orchestrate.sh runs `outcomes.py --mode local` daily: that run re-judges, a dry run stays
    dry, and a remote-only ingest never touches local rows."""
    seen: list = []
    monkeypatch.setattr(outcomes, "ingest_modes", lambda mode, dry_run=False: {"mode": mode})
    monkeypatch.setattr(
        outcomes, "recheck_replacements", lambda **kw: seen.append(kw) or {"selected": 0}
    )
    assert outcomes.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert seen == ([recheck] if recheck else []), seen
    assert (printed["replacement_recheck"] is None) is (recheck is None), printed


def test_the_undo_command_restores_and_runs_nothing_else(monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(outcomes, "undo_replacement_recheck", lambda: calls.append(1) or {"x": 1})
    monkeypatch.setattr(
        outcomes, "ingest_modes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran"))
    )
    assert outcomes.main(["--undo-replacement-recheck"]) == 0
    assert calls == [1] and json.loads(capsys.readouterr().out) == {"x": 1}
