"""A keepalive PR closed unmerged whose head another PR carries is not that PR's FAIL.

THE DEFECT (2026-10-11, measured on a Brain copy and read-only GitHub). A keepalive run IS its PR
(`keepalive_outcomes`), and a PR closed unmerged was recorded as that run's FAIL with no failure
class, so relearn and relearn_quality scored it. The opener lane rehomes keepalive PRs too: a
replacement opens carrying the closed PR's head commit and merges, and keepalive records it as its
OWN run with its own verdict. Of the 99 named-agent keepalive FAILs of the 90 days, 14 were such
rehomes (codex 10, claude 3, cursor 1; 8 inside the broke-later detection floor) and 2 had been
opened on work that had already merged. Every carrier with a Brain row had its own PASS.

THE RULE. Before the FAIL, the keepalive read (`outcomes.keepalive_replacement`): the closed PR's
view, then #493's REPLACEMENT_QUERY once per issue it links, judged by `judge_replacement` with the
run's start at the PR's creation. A carrier ends the run with NO verdict,
`feedback.CARRIED_BY_OTHER_PR`. The carrier is its own run, so a PASS here would count the delivery
twice. A replacement with different commits is different work and the FAIL stands; so does a PR
that links no issue. A read GitHub did not answer writes nothing and is asked again until the
horizon, and the rows written before are re-judged once (`recheck_keepalive_replacements`).

The fixtures are GitHub's answers, read 2026-10-11: Fine-Art-Archive#644 (rehomed into #648),
Counter_Risk#994 (opened on #983's branch two days after #983 merged), Workflows#3435 (#3436
closed its issue with commits of its own) and Counter_Risk#956 (links no issue). Their Brain rows
are the owner's, copied as they stood. No real API: `subprocess.run` is a stub that answers by verb.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from test_outcomes_closing_pr_timing import graphql_call_problems

import durability_sweep
import feedback
import keepalive_outcomes
import outcomes
import utc_epoch

FAA, CR, WF = "stranske/Fine-Art-Archive", "stranske/Counter_Risk", "stranske/Workflows"
OWNER = {"id": "MDQ6VXNlcjIzMDQ2MzIy", "login": "stranske"}


def _issue_ref(repo_id: str, name: str, number: int, issue_id: str) -> dict:
    return {
        "id": issue_id,
        "number": number,
        "repository": {"id": repo_id, "name": name, "owner": OWNER},
        "url": f"https://github.com/stranske/{name}/issues/{number}",
    }


# Fine-Art-Archive#644 (the closer's orphan sweep raised it), closed 13:06:35Z; #648 opened 13:07:30Z
# on a registry branch with #644's head as its SECOND commit and merged 13:37:01Z. Keepalive
# recorded #648 as codex's PASS, and #644's FAIL was written at 13:41:34Z.
HEAD_644 = "a652c4782942fd2f526eba2176aa607972466b29"
VIEW_644 = {
    "closedAt": "2026-09-02T13:06:35Z",
    "closingIssuesReferences": [
        _issue_ref("R_kgDOSrX7xw", "Fine-Art-Archive", 647, "I_kwDOSrX7x88AAAABPVY6uw")
    ],
    "createdAt": "2026-09-02T01:31:53Z",
    "headRefName": "feat/artist-approval-view",
    "headRefOid": HEAD_644,
    "mergedAt": None,
    "number": 644,
    "state": "CLOSED",
    "title": "Orphan sweep: artist approval view — approve artists one at a time",
    "url": "https://github.com/stranske/Fine-Art-Archive/pull/644",
}
LINKS_647 = (
    outcomes._replacement_link(
        648,
        "MERGED",
        "2026-09-02T13:37:01Z",
        [
            "2f94b7686558a49aed77476ca9b2ac587f41896e",
            HEAD_644,
            "eb3cb904ac6a9a0884dd0c1c7601504066fd5ec4",
        ],
        repo=FAA,
        branch="codex/issue-647-artist-approval-view",
    ),
    outcomes._replacement_link(
        651,
        "MERGED",
        "2026-09-02T14:39:11Z",
        ["3566c18a57f52b17fd2f55c03259f3554b4a522f"],
        repo=FAA,
        branch="fix/647-black-format-ci",
    ),
)
RUN_644, TS_644, WRITTEN_644 = (
    "keepalive:stranske/Fine-Art-Archive#644:codex",
    1788312713,
    1788356494,
)

# Counter_Risk#994, opened 2026-09-04 on #983's branch at #983's own last commit; #983 had merged on
# 2026-09-02. Recorded with no agent, attributed to codex later (its run id still ends `:none`).
HEAD_994 = "cddbcfbc8ab71352d66ca3cf6dcec363073ed886"
VIEW_994 = {
    "closedAt": "2026-09-04T15:35:29Z",
    "closingIssuesReferences": [
        _issue_ref("R_kgDORPCqTg", "Counter_Risk", 978, "I_kwDORPCqTs8AAAABPQl6ZA")
    ],
    "createdAt": "2026-09-04T15:32:15Z",
    "headRefName": "codex/issue-978-chat-session-provider-fallback",
    "headRefOid": HEAD_994,
    "mergedAt": None,
    "number": 994,
    "state": "CLOSED",
    "title": (
        "orphan(codex/issue-978-chat-session-provider-fallback): "
        "fix(chat): handle fully filtered exposures"
    ),
    "url": "https://github.com/stranske/Counter_Risk/pull/994",
}
LINKS_978 = (
    outcomes._replacement_link(
        983,
        "MERGED",
        "2026-09-02T10:50:25Z",
        [
            "e60b63d18cd7ee822def45d0b7e53858bbd1448a",
            "899b374f5928eb9952be8fb1f4aa6e8da18a9a7a",
            HEAD_994,
        ],
        repo=CR,
        branch="codex/issue-978-chat-session-provider-fallback",
    ),
    outcomes._replacement_link(
        999,
        "MERGED",
        "2026-09-05T13:29:06Z",
        ["061eb226deccce1946bea059db79fcfd900a05f8", "5926a486f61d9675b3bdffe99e927ec81479d600"],
        repo=CR,
        branch="codex/issue-978-reconciliation-coverage",
    ),
)
RUN_994, TS_994, WRITTEN_994 = "keepalive:stranske/Counter_Risk#994:none", 1788535935, 1788622838

# Workflows#3435: #3436 closed issue #3433 with three commits of its own; #3435's head is not one.
HEAD_3435 = "658d014f14f88befb9d0e11e6d33286fa74cd817"
VIEW_3435 = {
    "closedAt": "2026-09-14T04:26:53Z",
    "closingIssuesReferences": [
        _issue_ref("R_kgDOQprj9A", "Workflows", 3433, "I_kwDOQprj9M8AAAABRHrfPg")
    ],
    "createdAt": "2026-09-14T01:18:15Z",
    "headRefName": "codex/issue-3433-productive-dispatch",
    "headRefOid": HEAD_3435,
    "mergedAt": None,
    "number": 3435,
    "state": "CLOSED",
    "title": "fix(keepalive): retry successful runs without productive output",
    "url": "https://github.com/stranske/Workflows/pull/3435",
}
LINKS_3433 = (
    outcomes._replacement_link(
        3436,
        "MERGED",
        "2026-09-14T02:37:13Z",
        [
            "e78c3ab09d4571e9bfd76d0e908f6b0e4fdfaaed",
            "98e0ded1d1e3e8bf94e93895880bd35f6cb742b4",
            "0aaa80922fda884fe2b78110e8aa0c12d78fd278",
        ],
        repo=WF,
        branch="fix/3433-unproductive-dispatch-debounce",
    ),
)
RUN_3435, TS_3435, WRITTEN_3435 = "keepalive:stranske/Workflows#3435:codex", 1789348695, 1789418428

# Counter_Risk#956 links no issue at all.
VIEW_956 = {
    "closedAt": "2026-08-23T19:23:09Z",
    "closingIssuesReferences": [],
    "createdAt": "2026-08-23T19:19:12Z",
    "headRefName": "claude/untrack-langsmith-worker-attempt",
    "headRefOid": "ae5e78ba9bbb0928d75e6b58ffcdcb0813060494",
    "mergedAt": None,
    "number": 956,
    "state": "CLOSED",
    "title": "fix(ci): untrack the per-run worker-attempt artifact",
    "url": "https://github.com/stranske/Counter_Risk/pull/956",
}
RUN_956, TS_956, WRITTEN_956 = "keepalive:stranske/Counter_Risk#956:none", 1787512752, 1787550147

VIEWS = {(FAA, 644): VIEW_644, (CR, 994): VIEW_994, (WF, 3435): VIEW_3435, (CR, 956): VIEW_956}
ISSUES = {
    (FAA, 647): outcomes._replacement_answer(644, HEAD_644, *LINKS_647),
    (CR, 978): outcomes._replacement_answer(994, HEAD_994, *LINKS_978),
    (WF, 3433): outcomes._replacement_answer(3435, HEAD_3435, *LINKS_3433),
}
RATE_LIMIT = (1, "", "gh: API rate limit exceeded for user ID 23046322.")
NOW = 1791753405  # 2026-10-11T21:16:45Z, when the owner's Brain was copied
DAY = 86400


def answer(repo: str, issue: int) -> dict:
    return json.loads(ISSUES[(repo, issue)][1])


def judged(view: dict, repo: str) -> dict:
    """The keepalive verdict from GitHub's answers, without the stub: the pure path."""
    linked = outcomes.keepalive_linked_issues(view, repo=repo, number=view["number"])
    per_issue = {
        issue: outcomes.judge_replacement(
            answer(repo, issue),
            repo=repo,
            pr_number=view["number"],
            started_ts=linked["opened_ts"],
            opened_ts=linked["opened_ts"],
        )
        for issue in linked["issues"]
    }
    return outcomes.judge_keepalive_replacement(linked, per_issue)


def outcome(view: dict, repo: str) -> dict:
    found = outcomes.state_to_outcome({**view, "keepalive_replacement": judged(view, repo)})
    assert found is not None
    return found


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(outcomes.KEEPALIVE_RECHECK_SWITCH, raising=False)
    monkeypatch.delenv(outcomes.REPLACEMENT_RECHECK_SWITCH, raising=False)
    return tmp_path


@pytest.fixture
def gh(monkeypatch):
    """Install a stub gh: `pr view N -R repo` answers VIEWS (with `views` overriding), and the
    replacement read answers ISSUES, or `graphql` when given. Returns the calls, in order."""
    calls: list[list[str]] = []

    def install(*, views: dict | None = None, graphql: tuple | None = None):
        known = {**VIEWS, **(views or {})}

        def fake_run(argv, capture_output=True, text=True, **_kw):
            calls.append(list(argv))
            verb = tuple(argv[1:3])
            if verb == ("pr", "view"):
                assert argv[argv.index("--json") + 1] == outcomes.KEEPALIVE_VIEW_FIELDS, argv
                view = known.get((argv[argv.index("-R") + 1], int(argv[3])))
                if view is None:
                    return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
                return subprocess.CompletedProcess(argv, 0, json.dumps(view), "")
            if verb == ("api", "graphql"):
                problems = graphql_call_problems(argv)
                if problems:  # refused, as GitHub refuses it
                    return subprocess.CompletedProcess(argv, 1, "", "; ".join(problems))
                if graphql is not None:
                    return subprocess.CompletedProcess(argv, *graphql)
                sent = {
                    key: value
                    for flag, pair in zip(argv, argv[1:])
                    if flag in ("-f", "-F")
                    for key, _, value in [pair.partition("=")]
                }
                return subprocess.CompletedProcess(
                    argv, *ISSUES[(f"{sent['owner']}/{sent['name']}", int(sent["issue"]))]
                )
            raise AssertionError(f"unexpected gh call: {argv}")

        monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
        return calls

    return install


def graphql_calls(calls: list) -> list:
    return [argv for argv in calls if argv[1:3] == ["api", "graphql"]]


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
        found = c.execute("SELECT * FROM outcomes WHERE run_id=?", (run_id,)).fetchone()
        events = c.execute(
            "SELECT * FROM completion_events WHERE run_id=? AND producer=? ORDER BY event_id",
            (run_id, outcomes.RECORD_OUTCOME_PRODUCER),
        ).fetchall()
    return found, events


def listed(view: dict, label: str | None = None) -> dict:
    """The PR as keepalive's `gh pr list --json PR_LIST_FIELDS` prints it."""
    pr = {
        key: view[key]
        for key in ("number", "state", "title", "createdAt", "closedAt", "mergedAt", "url")
    }
    pr.update(
        {
            "headRefName": view["headRefName"],
            "headRefOid": view["headRefOid"],
            "labels": [{"name": label}] if label else [],
            "author": {"login": "stranske"},
            "body": "",
            "updatedAt": view["closedAt"],
            "baseRefName": "main",
            "mergeCommit": None,
        }
    )
    return pr


def ingest(prs: dict, tmp_path, **kwargs):
    """Keepalive ingest over `prs` ({repo: [listed PR]}), offline but for what `kwargs` adds."""
    return keepalive_outcomes.ingest_keepalive_outcomes(
        list(prs),
        _pr_fetch_fn=lambda repo, _days: prs[repo],
        _closure_context_fn=lambda _repo, _number: "",
        _evidence_fetch_fn=lambda _repo, _numbers: {},
        _spec_dir=tmp_path / "specs",
        _now=NOW,
        **kwargs,
    )


# --- the three cases the rule exists for ------------------------------------------------------


def test_the_rehome_ends_the_run_with_no_verdict():
    """Fine-Art-Archive#644: #648 carries its head and merged after it opened. #648 is its own
    keepalive run with its own PASS, so #644 gets none: not the FAIL, and not a second PASS."""
    verdict = judged(VIEW_644, FAA)
    assert (verdict["status"], verdict["issue"], verdict["pr"]["number"]) == ("carried", 647, 648)
    found = outcome(VIEW_644, FAA)
    assert {key: found[key] for key in ("merged", "adjudicated_verdict", "durability")} == {
        "merged": False,
        "adjudicated_verdict": None,
        "durability": "abandoned",
    }, found
    assert found["failure_class"] == feedback.CARRIED_BY_OTHER_PR
    assert feedback.CARRIED_BY_OTHER_PR in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    notes = found["notes"]
    assert notes.startswith("remote keepalive PR closed unmerged; replacement check: "), notes
    assert (
        "#648 (merged 2026-09-02T13:37:01Z) on codex/issue-647-artist-approval-view carries its "
        "head a652c4782942, linked to issue #647" in notes
    ), notes
    assert not durability_sweep.EXPLICIT_MERGED_PR_RE.search(notes), (
        "the sweep must never read a carrier's merge as this run's",
        notes,
    )


def test_a_pr_opened_on_merged_work_is_a_duplicate_with_no_verdict():
    """Counter_Risk#994 was opened on #983's branch two days after #983 merged with that very head.
    It carried nothing new, and #983's own run holds the work's verdict."""
    verdict = judged(VIEW_994, CR)
    assert verdict["status"] == "duplicate", verdict
    assert verdict["before_run"] == ["#983 (merged 2026-09-02T10:50:25Z)"], verdict
    found = outcome(VIEW_994, CR)
    assert (found["adjudicated_verdict"], found["failure_class"]) == (
        None,
        feedback.CARRIED_BY_OTHER_PR,
    )
    assert "had merged in #983 (merged 2026-09-02T10:50:25Z) before it opened" in found["notes"]


def test_a_different_implementation_leaves_the_fail():
    """Workflows#3435: #3436 closed the issue with commits of its own. #3435's work was dropped
    for different work, so its FAIL stands, saying what was read."""
    assert judged(VIEW_3435, WF)["status"] == "none"
    found = outcome(VIEW_3435, WF)
    assert {key: found[key] for key in ("merged", "adjudicated_verdict", "durability")} == {
        "merged": False,
        "adjudicated_verdict": "FAIL",
        "durability": "abandoned",
    }, found
    assert "failure_class" not in found, found
    assert found["notes"].endswith(
        "replacement check: its head 658d014f14f8: no PR linked to issue #3433 carries it "
        "(links read: #3436 (merged 2026-09-14T02:37:13Z))"
    ), found["notes"]


def test_a_pr_that_links_no_issue_keeps_its_fail_after_one_read(gh):
    """Counter_Risk#956 links no issue, so no PR can be read as carrying its head: the view is the
    only call, and the FAIL stands, marked, so the re-judge never selects it."""
    calls = gh()
    verdict = outcomes.keepalive_replacement(CR, 956)
    assert verdict == {"status": "no_issue", "elsewhere": []}, verdict
    assert len(calls) == 1 and calls[0][1:3] == ["pr", "view"], calls
    found = outcomes.state_to_outcome({**VIEW_956, "keepalive_replacement": verdict})
    assert found and found["adjudicated_verdict"] == "FAIL", found
    assert outcomes.REPLACEMENT_CHECK in found["notes"] and "links no issue" in found["notes"]


# --- the edges of the judgment -----------------------------------------------------------------


def test_an_open_carrier_is_decided_not_waited_on():
    """#648 as it stood before it merged (the real answer with its merge removed): the work moved to
    a PR whose own run will hold its verdict, whichever way it ends, so #644 has none. Waiting would
    count the work twice if #648 were abandoned: #644's FAIL after the wait, and #648's own."""
    open_648 = {**LINKS_647[0], "state": "OPEN", "mergedAt": None}
    linked = outcomes.keepalive_linked_issues(VIEW_644, repo=FAA, number=644)
    verdict = outcomes.judge_keepalive_replacement(
        linked,
        {
            647: outcomes.judge_replacement(
                json.loads(outcomes._replacement_answer(644, HEAD_644, open_648)[1]),
                repo=FAA,
                pr_number=644,
                started_ts=linked["opened_ts"],
                opened_ts=linked["opened_ts"],
            )
        },
    )
    assert verdict["status"] == "open", verdict
    found = outcomes.state_to_outcome({**VIEW_644, "keepalive_replacement": verdict})
    assert found and found["failure_class"] == feedback.CARRIED_BY_OTHER_PR, found
    assert "open #648 (open) carries its head a652c4782942, or may" in found["notes"]


def test_two_merged_carriers_are_unattributed():
    """A second merged PR also carrying #644's head: the judge cannot single one out, which is
    `unattributed_replacement`, excluded like the rest, never the FAIL."""
    second = outcomes._replacement_link(
        700, "MERGED", "2026-09-03T00:00:00Z", [HEAD_644], repo=FAA, branch="b/700"
    )
    linked = outcomes.keepalive_linked_issues(VIEW_644, repo=FAA, number=644)
    verdict = outcomes.judge_keepalive_replacement(
        linked,
        {
            647: outcomes.judge_replacement(
                json.loads(outcomes._replacement_answer(644, HEAD_644, *LINKS_647, second)[1]),
                repo=FAA,
                pr_number=644,
                started_ts=linked["opened_ts"],
                opened_ts=linked["opened_ts"],
            )
        },
    )
    assert verdict["status"] == "unattributable", verdict
    found = outcomes.state_to_outcome({**VIEW_644, "keepalive_replacement": verdict})
    assert found and found["failure_class"] == feedback.UNATTRIBUTED_REPLACEMENT, found


def test_an_issue_in_another_repository_is_named_not_read(gh):
    """REPLACEMENT_QUERY resolves an issue number in the PR's own repository, so a link elsewhere
    would read the wrong issue: it is named in the notes, never read."""
    view = {
        **VIEW_956,
        "closingIssuesReferences": [_issue_ref("R_other", "Workflows", 3433, "I_other")],
    }
    calls = gh(views={(CR, 956): view})
    verdict = outcomes.keepalive_replacement(CR, 956)
    assert verdict == {"status": "no_issue", "elsewhere": ["stranske/Workflows#3433"]}, verdict
    assert not graphql_calls(calls), calls


@pytest.mark.parametrize(
    "verb, why",
    [
        (("pr", "view"), "the PR view timed out"),
        (("api", "graphql"), "the replacement read timed out"),
    ],
)
def test_a_read_that_stalls_is_unanswered_never_raised(gh, monkeypatch, verb, why):
    """Both reads run unattended (the hourly remote ingest, the daily keepalive ingest and its
    re-judge). A stalled call is bounded at 60 s and comes back unanswered, so the run is asked
    again; it never holds the ingest or aborts it with TimeoutExpired."""
    gh()
    answered = outcomes.subprocess.run

    def stalled(argv, capture_output=True, text=True, **kw):
        if tuple(argv[1:3]) == verb:
            assert kw.get("timeout") == 60, (argv[:3], kw)
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return answered(argv, capture_output=capture_output, text=text, **kw)

    monkeypatch.setattr(outcomes.subprocess, "run", stalled)
    assert outcomes.keepalive_replacement(FAA, 644) == {"status": "unanswered", "error": why}


@pytest.mark.parametrize(
    "view, why",
    [
        ({**VIEW_644, "number": 645}, "the view of PR #644 is not in the answer"),
        ({**VIEW_644, "state": "OPEN"}, "PR #644 reads open now, not closed unmerged"),
        ({**VIEW_644, "createdAt": None}, "creation time or linked issues are not in the answer"),
        ({**VIEW_644, "closingIssuesReferences": [{"number": 647}]}, "cannot be read"),
    ],
)
def test_a_view_that_cannot_be_read_is_unanswered(view, why):
    verdict = outcomes.keepalive_linked_issues(view, repo=FAA, number=644)
    assert verdict["status"] == "unanswered" and why in verdict["error"], verdict


# --- both writers ------------------------------------------------------------------------------


def test_the_keepalive_ingest_reads_before_the_first_outcome_only(brain, gh):
    """`keepalive_outcomes` makes the read for a closed PR's first outcome: one view and one query
    per linked issue. A decided row is never read again here; the re-judge owns those."""
    calls = gh()
    prs = {FAA: [listed(VIEW_644, "agent:codex")], WF: [listed(VIEW_3435, "agent:codex")]}
    first = ingest(prs, brain, _replacement_fn=outcomes.keepalive_replacement)
    assert first["replacements"] == {
        **outcomes.keepalive_replacement_counts(),
        "carried": 1,
        "failed": 1,
    }, first["replacements"]
    assert row(RUN_644)[:4] == (0, None, "abandoned", feedback.CARRIED_BY_OTHER_PR), row(RUN_644)
    assert row(RUN_3435)[:4] == (0, "FAIL", "abandoned", None), row(RUN_3435)
    assert outcomes.REPLACEMENT_CHECK in row(RUN_3435)[4]
    assert len(calls) == 4 and len(graphql_calls(calls)) == 2, calls
    again = ingest(prs, brain, _replacement_fn=outcomes.keepalive_replacement)
    assert again["outcomes_recorded"] == 0 and len(calls) == 4, (again, calls)
    assert outcomes._keepalive_recheck_runs() == [], "every row ingest decided is marked"


def test_an_offline_ingest_reads_nothing_and_writes_the_old_fail(brain, gh):
    """Injected PRs mean an offline run, as for the verifier: no read, and the FAIL word for word."""
    calls = gh()
    result = ingest({FAA: [listed(VIEW_644, "agent:codex")]}, brain)
    assert calls == [], calls
    assert result["replacements"] == outcomes.keepalive_replacement_counts()
    assert row(RUN_644) == (0, "FAIL", "abandoned", None, "remote keepalive PR closed unmerged")


def test_remote_ingest_reads_a_row_recorded_while_its_pr_was_open(brain, gh):
    """The hourly remote ingest settles a keepalive row recorded while open. ONE view is both the
    state it maps and the start of the read."""
    feedback.record_run(
        RUN_644, f"{FAA}#644", "implement", "codex", mode="remote", source="keepalive"
    )
    calls = gh()
    result = outcomes.ingest_outcomes("remote", now=NOW)
    assert result["keepalive_replacements"] == {
        **outcomes.keepalive_replacement_counts(),
        "carried": 1,
    }, result
    assert row(RUN_644)[:4] == (0, None, "abandoned", feedback.CARRIED_BY_OTHER_PR)
    views = [argv for argv in calls if argv[1:3] == ["pr", "view"]]
    assert len(views) == 1 and len(graphql_calls(calls)) == 1, calls
    assert outcomes.ingest_modes("local")["keepalive_replacements"] is None


def test_unknown_is_not_a_verdict_until_the_horizon_and_a_drained_wait_prints_zero(brain, gh):
    """GitHub did not answer: nothing is written, the run is asked again, and the wait prints its
    blocking number and when it drains. At REPLACEMENT_RECHECK_HORIZON_DAYS after the PR closed the
    FAIL stands with the reason, and the next run prints `waiting 0` and no drain date."""
    feedback.record_run(
        RUN_644, f"{FAA}#644", "implement", "codex", mode="remote", source="keepalive"
    )
    gh(graphql=RATE_LIMIT)
    closed = utc_epoch.from_iso(VIEW_644["closedAt"])
    assert closed is not None
    horizon = closed + outcomes.REPLACEMENT_RECHECK_HORIZON_DAYS * DAY
    early = outcomes.ingest_outcomes("remote", now=horizon - 1)
    assert row(RUN_644) is None, "an unanswered read writes nothing"
    assert early["unanswered"] == 1, early
    assert early["keepalive_replacements"]["waiting"] == 1, early
    assert early["keepalive_replacements"]["drains_by"] == "2026-09-09T13:06:35Z", early
    late = outcomes.ingest_outcomes("remote", now=horizon)
    assert late["keepalive_replacements"]["closed_at_horizon"] == 1, late
    merged, verdict, _durability, failure_class, notes = row(RUN_644)
    assert (merged, verdict, failure_class) == (0, "FAIL", None), notes
    assert "GitHub did not answer within 7 days of the close" in notes and "rate limit" in notes
    drained = outcomes.ingest_outcomes("remote", now=horizon + 1)
    assert drained["keepalive_replacements"]["waiting"] == 0, drained
    assert drained["keepalive_replacements"]["drains_by"] is None, drained


@pytest.mark.parametrize("learner", ["relearn_quality", "relearn"])
def test_no_route_learner_scores_a_carried_row(brain, learner):
    """One carried row beside one real FAIL in the same cell: only the FAIL is an observation."""
    for run_id, pr in (("carried", 644), ("control", 645)):
        feedback.record_run(
            run_id, f"{FAA}#{pr}", "implement", "codex", mode="remote", source="keepalive"
        )
    feedback.record_outcome("carried", **outcome(VIEW_644, FAA))
    feedback.record_outcome(
        "control", merged=False, adjudicated_verdict="FAIL", durability="abandoned"
    )
    version = getattr(feedback, learner)({"implement": {"codex": 0.5}})
    with feedback._conn() as c:
        n_obs = c.execute(
            "SELECT n_obs FROM route_weights WHERE version=? AND task_type='implement' "
            "AND agent='codex'",
            (version,),
        ).fetchone()[0]
    assert n_obs == 1, f"{learner} counted the carried row: n_obs={n_obs}"


# --- the re-judge of the rows written before ---------------------------------------------------


def seed_fail(run_id: str, view: dict, repo: str, agent: str, ts: int, written: int) -> None:
    feedback.record_run(
        run_id,
        f"{repo}#{view['number']}",
        "implement",
        agent,
        mode="remote",
        source="keepalive",
        pr_number=view["number"],
        ts=ts,
    )
    feedback.record_outcome(
        run_id,
        merged=False,
        adjudicated_verdict="FAIL",
        durability="abandoned",
        notes="remote keepalive PR closed unmerged",
    )
    with feedback._conn() as c:
        c.execute("UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?", (written, run_id))


def seed_the_four() -> tuple:
    seed_fail(RUN_644, VIEW_644, FAA, "codex", TS_644, WRITTEN_644)
    seed_fail(RUN_994, VIEW_994, CR, "codex", TS_994, WRITTEN_994)
    seed_fail(RUN_3435, VIEW_3435, WF, "codex", TS_3435, WRITTEN_3435)
    seed_fail(RUN_956, VIEW_956, CR, "claude", TS_956, WRITTEN_956)
    return RUN_644, RUN_994, RUN_3435, RUN_956


def test_the_recheck_rejudges_each_shape_once_and_undoes_exactly(brain, gh):
    runs = seed_the_four()
    before = {run: whole(run) for run in runs}
    calls = gh()
    first = outcomes.recheck_keepalive_replacements(now=NOW)
    assert {key: first[key] for key in ("selected", "carried", "duplicate", "kept_fail")} == {
        "selected": 4,
        "carried": 1,
        "duplicate": 1,
        "kept_fail": 2,
    }, first
    assert (first["waiting"], first["snapshotted"], first["stopped"]) == (0, 4, None), first
    # A patch cannot clear the stored FAIL: the class is what every learner reads.
    assert row(RUN_644)[:4] == (0, "FAIL", "abandoned", feedback.CARRIED_BY_OTHER_PR)
    assert row(RUN_994)[3] == feedback.CARRIED_BY_OTHER_PR
    assert row(RUN_644)[4].endswith("so this one has none (re-judged 2026-10-11; was FAIL)")
    assert row(RUN_3435)[:4] == (0, "FAIL", "abandoned", None)
    assert "links no issue" in row(RUN_956)[4]
    assert len(calls) == 4 + 3, "one view per row, one query per linked issue"
    again = outcomes.recheck_keepalive_replacements(now=NOW + 100)
    assert again["selected"] == 0 and again["waiting"] == 0, again
    feedback.record_outcome(RUN_644, verifier_verdict="FAIL")  # arrives while re-judged
    undone = outcomes.undo_keepalive_replacement_recheck()
    assert (undone["restored"], undone["skipped"]) == (4, []), undone
    assert {run: whole(run) for run in runs} == before
    assert outcomes.KEEPALIVE_RECHECK_SWITCH in undone["keep_it"], undone


def test_the_keepalive_recheck_selects_named_keepalive_rows_only(brain):
    """A row with no agent trains no learner and is left as it is; local rows are #493's; and the
    local re-judge never selects a keepalive row."""
    seed_the_four()
    seed_fail("keepalive:o/r#1:none", {**VIEW_956, "number": 1}, "o/r", "none", 1, 2)
    feedback.record_run("local-run", "o/r#2", "implement", "codex", mode="local", ts=1)
    feedback.record_outcome(
        "local-run",
        merged=False,
        adjudicated_verdict="FAIL",
        durability="abandoned",
        notes="remote keepalive PR closed unmerged",
    )
    selected = {run["run_id"] for run in outcomes._keepalive_recheck_runs()}
    assert selected == {RUN_644, RUN_994, RUN_3435, RUN_956}, selected
    assert {run["run_id"] for run in outcomes._replacement_recheck_runs()} == {"local-run"}
    assert outcomes.KEEPALIVE_NO_AGENT == keepalive_outcomes.NON_AGENT


def test_the_recheck_waits_on_no_answer_then_keeps_the_fail_at_its_horizon(brain, gh):
    seed_fail(RUN_644, VIEW_644, FAA, "codex", TS_644, WRITTEN_644)
    gh(graphql=RATE_LIMIT)
    first = outcomes.recheck_keepalive_replacements(now=NOW)
    assert (first["waiting"], first["drains_by"]) == (1, outcomes._iso(NOW + 7 * DAY)), first
    assert row(RUN_644)[3] is None and outcomes.REPLACEMENT_CHECK not in row(RUN_644)[4]
    late = outcomes.recheck_keepalive_replacements(now=NOW + 7 * DAY)
    assert (late["closed_at_horizon"], late["waiting"]) == (1, 0), late
    assert "undecided for 7 days (GitHub did not answer: " in row(RUN_644)[4]
    assert outcomes.recheck_keepalive_replacements(now=NOW)["selected"] == 0


def test_the_recheck_keeps_a_pr_that_is_no_longer_the_closed_one(brain, gh):
    """A PR reopened and merged since its FAIL was written is not what the FAIL was about."""
    seed_fail(RUN_644, VIEW_644, FAA, "codex", TS_644, WRITTEN_644)
    merged = {**VIEW_644, "state": "MERGED", "mergedAt": "2026-10-01T00:00:00Z"}
    gh(views={(FAA, 644): merged})
    result = outcomes.recheck_keepalive_replacements(now=NOW)
    assert result["not_the_recorded_pr"] == 1, result
    assert "the PR now reads MERGED (merged 2026-10-01T00:00:00Z)" in row(RUN_644)[4]


def test_the_owner_question_is_its_own_and_only_its_decline_stops_it(brain, gh):
    """Recorded once, before the first snapshot, with the default "run it". A decline of the local
    re-judge's question does not stop this one, and this one's decline stops only this one."""
    seed_fail(RUN_644, VIEW_644, FAA, "codex", TS_644, WRITTEN_644)
    gh()
    local = feedback.record_owner_question(
        f"{outcomes.REPLACEMENT_RECHECK_TOKEN} Re-judge closed-PR FAIL rows?", "run it"
    )
    feedback.answer_owner_question(local["question_id"], "decline")
    ran = outcomes.recheck_keepalive_replacements(dry_run=True, now=NOW)
    assert ran["stopped"] is None and ran["carried"] == 1, ran
    snapshot = outcomes._recheck_snapshot

    def checked(*args):
        with feedback._conn() as c:
            asked = c.execute(
                "SELECT default_action FROM owner_questions WHERE instr(question, ?) > 0",
                (outcomes.KEEPALIVE_RECHECK_TOKEN,),
            ).fetchall()
        assert asked == [("run it",)], asked
        return snapshot(*args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(outcomes, "_recheck_snapshot", checked)
        outcomes.recheck_keepalive_replacements(now=NOW)
    outcomes.undo_keepalive_replacement_recheck()
    with feedback._conn() as c:
        question_id = c.execute(
            "SELECT question_id FROM owner_questions WHERE instr(question, ?) > 0",
            (outcomes.KEEPALIVE_RECHECK_TOKEN,),
        ).fetchone()[0]
    feedback.answer_owner_question(question_id, "Declined: keep the FAILs")
    stopped = outcomes.recheck_keepalive_replacements(now=NOW + 100)
    assert stopped["stopped"] == f"declined: owner question {question_id}", stopped
    assert stopped["selected"] == 1, "a stopped re-judge still says what it would select"
    assert (
        outcomes.recheck_replacements(now=NOW + 100)["stopped"] is not None
    ), "the local decline still stops the local re-judge"


def test_the_switch_and_a_dry_run_write_nothing(brain, gh, monkeypatch):
    seed_fail(RUN_644, VIEW_644, FAA, "codex", TS_644, WRITTEN_644)
    before = whole(RUN_644)
    gh()
    dry = outcomes.recheck_keepalive_replacements(dry_run=True, now=NOW)
    assert (dry["carried"], dry["snapshotted"]) == (1, 0), dry
    monkeypatch.setenv(outcomes.KEEPALIVE_RECHECK_SWITCH, "0")
    off = outcomes.recheck_keepalive_replacements(now=NOW)
    assert off["selected"] == 1 and off["stopped"] == f"{outcomes.KEEPALIVE_RECHECK_SWITCH}=0"
    assert whole(RUN_644) == before
    assert not outcomes._recheck_undo_path(outcomes.KEEPALIVE_RECHECK_UNDO).exists()
    with feedback._conn() as c:
        assert c.execute("SELECT count(*) FROM owner_questions").fetchone()[0] == 0


# --- the command line the daily cadence runs ---------------------------------------------------


@pytest.mark.parametrize("argv, dry_run", [([], False), (["--dry-run"], True)])
def test_the_daily_ingest_command_runs_the_recheck(monkeypatch, capsys, argv, dry_run):
    seen: list = []
    monkeypatch.setattr(
        keepalive_outcomes, "ingest_keepalive_outcomes", lambda *a, **k: {"prs_seen": 0}
    )
    monkeypatch.setattr(
        outcomes,
        "recheck_keepalive_replacements",
        lambda **kw: seen.append(kw["dry_run"]) or {"selected": 0},
    )
    assert keepalive_outcomes.main([*argv, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert seen == [dry_run] and printed["replacement_recheck"] == {"selected": 0}, printed


def test_the_undo_command_restores_and_runs_nothing_else(monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(
        outcomes, "undo_keepalive_replacement_recheck", lambda: calls.append(1) or {"x": 1}
    )
    monkeypatch.setattr(
        keepalive_outcomes,
        "ingest_keepalive_outcomes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ingest ran")),
    )
    assert keepalive_outcomes.main(["--undo-replacement-recheck"]) == 0
    assert calls == [1] and json.loads(capsys.readouterr().out) == {"x": 1}


@pytest.mark.parametrize("argv, dry_run", [([], False), (["--dry-run"], True)])
def test_the_recheck_runs_alone_on_its_own_flag(monkeypatch, capsys, argv, dry_run):
    """The explicit command, and its preview: the re-judge runs, the ingest never does."""
    seen: list = []
    monkeypatch.setattr(
        outcomes,
        "recheck_keepalive_replacements",
        lambda **kw: seen.append(kw["dry_run"]) or {"selected": 3},
    )
    monkeypatch.setattr(
        keepalive_outcomes,
        "ingest_keepalive_outcomes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ingest ran")),
    )
    assert keepalive_outcomes.main(["--replacement-recheck", *argv, "--json"]) == 0
    assert seen == [dry_run] and json.loads(capsys.readouterr().out) == {"selected": 3}
