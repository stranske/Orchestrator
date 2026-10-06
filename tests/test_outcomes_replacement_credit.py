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
head commit (`outcomes.judge_replacement`). Exactly one that merged after the run started is the
run's PASS, written through the same path as its own merge and named `PR #M merged` for the
durability sweep. A replacement with different commits is different work and never credited:
Orchestrator#199's PR #200 was superseded by #208, a separate implementation, and stays FAIL. Three
outcomes stay apart: a read GitHub did not answer is unknown (skipped, retried, never a PASS); no
carrier is the FAIL; an answer that cannot single one out is `unattributed_replacement`, which no
learner scores. An OPEN carrier waits, as the run's own open PR would.

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

# Orchestrator#213: run started 2026-09-04T00:33:01Z, its PR #214 (orchestrator/issue-213) closed at
# 04:05:20Z, replacement #219 merged at 06:26:24Z with the head 2829492 as its FIRST commit; the
# FAIL was written at 17:40:37Z.
RUN_213 = "stranske__Orchestrator_213-codex-1788481981941284000"
START_213, FAIL_WRITTEN_213 = 1788481981, 1788543637
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


def answer_213(*links: dict, more: bool = False) -> dict:
    return json.loads(outcomes._replacement_answer(214, HEAD_214, *links, more=more)[1])


def judge_213(*links: dict, started: int | None = START_213, more: bool = False) -> dict:
    return outcomes.judge_replacement(
        answer_213(*links, more=more), repo=REPO, pr_number=214, started_ts=started
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
    }, result
    merged, verdict, durability, failure_class, notes = row(RUN_213)
    assert (merged, verdict, durability, failure_class) == (1, "PASS", "pending", None), notes
    assert durability_sweep._explicit_merged_pr_target(f"{REPO}#213", notes) == f"{REPO}#219", (
        "the sweep must resolve the replacement's merge from the notes, never the closed PR",
        notes,
    )
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


# --- unknown is not a verdict -----------------------------------------------------------------

UNREADABLE = {
    "rate-limited": RATE_LIMIT,
    "not-json": (0, "<html>502</html>", ""),
    "no-repository": (0, json.dumps({"data": {"repository": None}}), ""),
    "head-missing": (0, outcomes._replacement_answer(214, "")[1], ""),
    "head-of-another-pr": outcomes._replacement_answer(999, HEAD_214, REPLACEMENT_219),
    "target-is-not-an-issue": (
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


# --- what a complete answer cannot decide -----------------------------------------------------


@pytest.mark.parametrize(
    "links, more, started, why",
    [
        (
            (
                REPLACEMENT_219,
                link(230, "MERGED", "2026-09-05T00:00:00Z", [HEAD_214], branch="b/230"),
            ),
            False,
            START_213,
            "2 merged PR(s) carry its head or may",
        ),
        (
            (link(231, "MERGED", "2026-09-04T06:00:00Z", ["f" * 40], total=101),),
            False,
            START_213,
            "commits cut at 100",
        ),
        ((), True, START_213, "over 100 PRs link the issue"),
        ((REPLACEMENT_219,), False, None, "no start to place the merge against"),
    ],
    ids=["two-merged-carriers", "a-list-cut-short", "links-past-the-limit", "no-run-start"],
)
def test_an_answer_that_cannot_single_one_out_is_unattributed(links, more, started, why):
    judged = judge_213(*links, started=started, more=more)
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
        ("credited", "waiting", "unattributable", "failed"), 0
    ), "a drained ingest prints four zeros, never a missing key"
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
    assert direct["direct_target_pr"] and "replacement" not in direct
    assert outcomes.state_to_outcome(direct)["adjudicated_verdict"] == "FAIL"


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
    credited = outcomes.judge_replacement(
        answer_213(REPLACEMENT_219), repo=REPO, pr_number=214, started_ts=START_213
    )
    kept = outcomes.judge_replacement(
        json.loads(outcomes._replacement_answer(200, HEAD_200, DIFFERENT_208)[1]),
        repo=REPO,
        pr_number=200,
        started_ts=START_199,
    )
    return {
        f"{REPO}#213": {**CLOSED_214, "lookup_status": "found", "replacement": credited},
        f"{REPO}#199": {**CLOSED_200, "lookup_status": "found", "replacement": kept},
    }


def test_the_recheck_credits_the_replacement_once_and_keeps_the_other_fail(brain):
    states = seed_the_two(brain)
    before = {run: row(run) for run in (RUN_213, RUN_199)}
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
    # The undo restores the snapshot taken before the first write, field for field.
    assert outcomes.undo_replacement_recheck()["restored"] == 2
    assert {run: row(run) for run in (RUN_213, RUN_199)} == before


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


@pytest.mark.parametrize("how", ["switch", "owner-declined"])
def test_the_recheck_stops_when_declined_and_still_counts_its_selection(brain, monkeypatch, how):
    states = seed_the_two(brain)
    if how == "switch":
        monkeypatch.setenv(outcomes.REPLACEMENT_RECHECK_SWITCH, "0")
    else:
        question = feedback.record_owner_question(
            f"{outcomes.REPLACEMENT_RECHECK_TOKEN} Re-judge the closed-PR FAIL rows?", "run it"
        )
        feedback.answer_owner_question(question["question_id"], "decline: keep them")
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert result["selected"] == 2 and result["stopped"], (
        "a stopped re-judge must still say what it would select, never print a drained zero",
        result,
    )
    assert row(RUN_213)[1] == "FAIL" and not outcomes._recheck_undo_path().exists()


def test_an_open_or_default_owner_question_lets_the_recheck_run(brain):
    """The owner-question protocol: work proceeds on the default until the owner declines."""
    states = seed_the_two(brain)
    question = feedback.record_owner_question(
        f"{outcomes.REPLACEMENT_RECHECK_TOKEN} Re-judge the closed-PR FAIL rows?", "run it"
    )
    feedback.answer_owner_question(question["question_id"], "now, please")
    result = outcomes.recheck_replacements(_resolve=resolver(states), now=1_791_300_000)
    assert result["stopped"] is None and result["credited"] == 1, result


def test_a_dry_run_recheck_writes_nothing(brain):
    states = seed_the_two(brain)
    before = {run: row(run) for run in (RUN_213, RUN_199)}
    result = outcomes.recheck_replacements(
        dry_run=True, _resolve=resolver(states), now=1_791_300_000
    )
    assert (result["credited"], result["snapshotted"]) == (1, 0), result
    assert {run: row(run) for run in (RUN_213, RUN_199)} == before
    assert not outcomes._recheck_undo_path().exists()
