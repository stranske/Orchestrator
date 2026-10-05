"""A remote delegation is credited only with a PR it can be shown to have produced (2026-10-04).

THE DEFECT. Outcome ingest resolved a remote delegation (the tick applied `agent:<X>` to an issue or
a PR) to the first PR it could find: the delegated agent's branch, then every other agent's
`<agent>/issue-N` and the local lane's `orchestrator/issue-N`, and for a labelled PR the PR itself,
whatever had run on it. Measured on a Brain copy and read-only GitHub: none of the 9 merged
`orchestrator_remote` PASS rows was the delegated agent's work. Two were other lanes' PRs on
fallback branches (one merged 39 days before the label), two were codex PRs the labelled
gemini/cursor never ran on, two were bootstrap PRs the agent never ran on (one merged empty, one
finished by the closer), and three June codex PRs were done by local vibe, cursor and codex runs. The FAIL side had the same
shape: five gemini bootstrap PRs were closed without a gemini round ever completing.

THE RULE. The run's PR is the labelled PR, or the PR on the delegated agent's own branch
`{agent}/issue-N`; no other branch is asked. A PR settled before the label is not its work. And a
PASS or a FAIL needs at least one completed, not-unproductive round of the delegated agent's
keepalive runner on that PR since the label, read from the runner's trusted markers. A settled PR
without that is terminal with no verdict, `feedback.UNATTRIBUTED_DELEGATION`, which no learner
scores. A read that fails is unanswered: skipped and asked again. No real API: `subprocess.run` is
replaced by a stub that answers by verb.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

import durability_sweep
import feedback
import outcomes

LABEL = 1_790_000_000  # when the delegation's label was applied (runs.ts)
NOT_A_PR = (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 7.")
RATE_LIMIT = (1, "", "API rate limit exceeded for user ID 23046322.")
NO_PR = (0, "[]", "")


def iso(offset: int) -> str:
    return outcomes._iso(LABEL + offset)


def comment(kind: str, provider: str, pr: int, payload: dict, **author) -> dict:
    """One PR comment carrying one runner marker, encoded as runner_lib's `_build_marker` does."""
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return {
        "user": {"login": author.get("login", "github-actions[bot]")},
        "author_association": author.get("association", "CONTRIBUTOR"),
        "body": f"Runner dispatch state for {provider} on PR #{pr}. Do not edit.\n\n"
        f"<!-- {kind}:{provider}:{pr}:v1 base64:{encoded} -->",
    }


def reservation(provider: str, pr: int, rid: str, started: str, **author) -> dict:
    record = {"provider": provider, "pr_number": pr, "reservation_id": rid, "status": "pending"}
    return comment("runner-reservation", provider, pr, {**record, "started_at": started}, **author)


def receipt(provider: str, pr: int, rid: str, started: str, **final) -> dict:
    record = {"provider": provider, "pr_number": pr, "reservation_id": rid, "started_at": started}
    payload = {
        "schema": outcomes.RUNNER_RECEIPT_SCHEMA,
        "provider": provider,
        "reservation_id": rid,
        "record": {**record, **final},
    }
    return comment("runner-completion", provider, pr, payload)


def legacy(provider: str, pr: int, started: str, status: str) -> dict:
    record = {"provider": provider, "pr_number": pr, "started_at": started, "status": status}
    return comment("runner-dispatch", provider, pr, record)


def credited_round(provider: str, pr: int, rid: str = "r1") -> list[dict]:
    return [
        reservation(provider, pr, rid, iso(600)),
        receipt(provider, pr, rid, iso(600), status="completed", productive=True),
    ]


def pages(*comments: dict) -> tuple:
    """What `gh api --paginate --slurp` prints: every page wrapped in one array."""
    return (0, json.dumps([list(comments)]), "")


def pr_json(number: int, head: str, state: str, settled: int | None = 3000) -> dict:
    when = iso(settled) if settled is not None and state != "OPEN" else None
    return {
        "number": number,
        "headRefName": head,
        "state": state,
        "mergedAt": when if state == "MERGED" else None,
        "closedAt": when,
    }


def issue(state: str = "CLOSED", refs: list | None = None, url: str = "") -> tuple:
    body = {"state": state, "closedByPullRequestsReferences": refs if refs is not None else []}
    if url:
        body["url"] = url
    return (0, json.dumps(body), "")


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def gh(monkeypatch):
    """Install a stub gh answering `pr view`, `issue view`, `comments`, `closing` (the closing
    references' merge-time read) and `list:<head branch>`. Returns the list of keys asked, in
    order; an unexpected call fails the test."""
    calls: list[str] = []

    def install(answers: dict) -> list[str]:
        def fake_run(argv, capture_output=True, text=True, **_kw):
            if argv[1:3] == ["pr", "list"]:
                key = f"list:{argv[argv.index('--head') + 1]}"
            elif argv[1] == "api":
                key = "closing" if argv[2] == "graphql" else "comments"
            else:
                key = " ".join(argv[1:3])
            calls.append(key)
            if key not in answers:
                raise AssertionError(f"unexpected gh call: {argv}")
            return subprocess.CompletedProcess(argv, *answers[key])

        monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
        return calls

    return install


def resolve(target: str, agent: str, started: int | None = LABEL) -> tuple[dict, dict | None]:
    state = outcomes._delegated_pr_state(target, agent, started)
    return state, outcomes.state_to_outcome(state)


def assert_unattributed(outcome: dict | None, failure_class: str) -> None:
    assert outcome is not None, "a settled PR must end the run, not leave it pending"
    assert outcome.get("failure_class") == failure_class, outcome
    assert (outcome["merged"], outcome["adjudicated_verdict"]) == (None, None), outcome
    assert outcome["durability"] == "abandoned", outcome


# --- the runner's own record of who ran ---------------------------------------------------------


def test_only_the_delegated_agents_rounds_since_the_label_are_counted():
    comments = [
        *credited_round("gemini", 5, "r1"),
        reservation("gemini", 5, "r2", iso(900)),
        receipt("gemini", 5, "r2", iso(900), status="completed", productive=False),
        reservation("gemini", 5, "r3", iso(1200)),
        receipt("gemini", 5, "r3", iso(1200), status="error"),
        reservation("gemini", 5, "r4", iso(1500)),  # dispatched, never completed
        reservation("gemini", 5, "r0", iso(-600)),  # before the label: an earlier delegation's
        *credited_round("codex", 5, "c1"),  # another agent on the same PR
        reservation("gemini", 5, "u1", "not a time"),
    ]
    counts = outcomes.runner_rounds(comments, provider="gemini", pr_number=5, since_ts=LABEL)
    assert counts == {
        "credited": 1,
        "unproductive": 1,
        "errored": 1,
        "pending": 1,
        "before_label": 1,
        "undated": 1,
    }, counts


def test_a_marker_from_an_untrusted_author_is_no_evidence():
    rid = "r1"
    forged = [
        reservation("gemini", 5, rid, iso(600), login="mallory", association="NONE"),
        comment(
            "runner-completion",
            "gemini",
            5,
            {
                "schema": outcomes.RUNNER_RECEIPT_SCHEMA,
                "reservation_id": rid,
                "record": {
                    "provider": "gemini",
                    "pr_number": 5,
                    "reservation_id": rid,
                    "started_at": iso(600),
                    "status": "completed",
                },
            },
            login="mallory",
            association="NONE",
        ),
    ]
    assert outcomes.runner_rounds(forged, provider="gemini", pr_number=5, since_ts=LABEL) == (
        dict.fromkeys(
            ("credited", "unproductive", "errored", "pending", "before_label", "undated"), 0
        )
    )
    # runner_lib also trusts a repository collaborator by association, whatever the login.
    collaborator = reservation("gemini", 5, "r2", iso(600), login="someone", association="MEMBER")
    counts = outcomes.runner_rounds([collaborator], provider="gemini", pr_number=5, since_ts=LABEL)
    assert counts["pending"] == 1, counts


def test_markers_written_for_another_pr_are_not_this_prs_evidence():
    """The round on #5 says nothing about #70: the PR number inside the marker must match."""
    counts = outcomes.runner_rounds(
        credited_round("gemini", 5), provider="gemini", pr_number=70, since_ts=LABEL
    )
    assert counts["credited"] == 0, counts


def test_a_legacy_runner_dispatch_record_is_one_round():
    """Before reservations, runner_lib rewrote one `runner-dispatch` record per provider in place.
    Trend#5913's codex record was an `error` (a usage limit) after the gemini label."""
    errored = [legacy("codex", 5913, iso(1500), "error")]
    counts = outcomes.runner_rounds(errored, provider="codex", pr_number=5913, since_ts=LABEL)
    assert (counts["credited"], counts["errored"]) == (0, 1), counts
    completed = [legacy("codex", 5913, iso(1500), "completed")]
    counts = outcomes.runner_rounds(completed, provider="codex", pr_number=5913, since_ts=LABEL)
    assert counts["credited"] == 1, "a completed legacy round is unmeasured, not unproductive"


# --- which PR is the delegation's, and whether it can be shown to be its work ---------------------


def test_another_lanes_pr_on_a_fallback_branch_is_never_asked_about_or_credited(gh):
    """Workflows#2620 (gemini): the only PR was the local lane's `orchestrator/issue-2620`, merged
    39 days before the label. The old walk credited it as gemini's PASS."""
    calls = gh(
        {
            "pr view": NOT_A_PR,
            "list:gemini/issue-2620": NO_PR,
            "issue view": issue(refs=[{"number": 2627}]),
            # #2627 merged 39 days before the label, long before the issue closed: it counts.
            "closing": outcomes._closing_read(iso(6000), (2627, iso(-39 * 86400))),
        }
    )
    state, outcome = resolve("o/r#2620", "gemini")
    assert calls == ["pr view", "list:gemini/issue-2620", "issue view", "closing"], calls
    assert state["candidateBranches"] == ["gemini/issue-2620"], state
    assert_unattributed(outcome, feedback.UNATTRIBUTED_CLOSING_PR)


@pytest.mark.parametrize("agent", ["gemini", "cursor"])
def test_a_labelled_pr_the_delegated_agent_never_ran_on_is_not_its_pass(gh, agent):
    """Trend#5913 (gemini) and #5944 (cursor): codex PRs, merged after the label, with codex and
    autofix rounds on them and none of the labelled agent's."""
    other = [
        *credited_round("codex", 5913, "c1"),
        legacy("autofix", 5913, iso(900), "completed"),
    ]
    calls = gh(
        {
            "pr view": (0, json.dumps(pr_json(5913, "codex/issue-5858", "MERGED")), ""),
            "comments": pages(*other),
        }
    )
    state, outcome = resolve("o/r#5913", agent)
    assert calls == ["pr view", "comments"], calls
    assert state["attribution"]["attributable"] is False, state
    assert_unattributed(outcome, feedback.UNATTRIBUTED_DELEGATION)
    assert "#5913" in outcome["notes"] and agent in outcome["notes"], outcome


def test_an_own_branch_bootstrap_merged_without_the_agents_rounds_is_not_its_pass(gh):
    """trip-planner#1694: the gemini bootstrap PR merged with only its bootstrap file (+1/-0) and no
    gemini round; Trend#5813: the closer finished the gemini bootstrap with keepalive off."""
    gh(
        {
            "pr view": NOT_A_PR,
            "list:gemini/issue-1694": (
                0,
                json.dumps([pr_json(1702, "gemini/issue-1694", "MERGED")]),
                "",
            ),
            "comments": pages(),
        }
    )
    _state, outcome = resolve("o/r#1694", "gemini")
    assert_unattributed(outcome, feedback.UNATTRIBUTED_DELEGATION)


def test_an_own_branch_pr_closed_without_a_completed_round_is_not_its_fail(gh):
    """Fine-Art-Archive#560/#561: gemini was dispatched, the round never completed (runner_lib
    wrote a pending record, keepalive logged agent-run-skipped), and the closer closed it."""
    gh(
        {
            "pr view": NOT_A_PR,
            "list:gemini/issue-560": (
                0,
                json.dumps([pr_json(570, "gemini/issue-560", "CLOSED")]),
                "",
            ),
            "comments": pages(legacy("gemini", 570, iso(600), "pending")),
        }
    )
    _state, outcome = resolve("o/r#560", "gemini")
    assert_unattributed(outcome, feedback.UNATTRIBUTED_DELEGATION)


def test_its_own_pr_with_a_credited_round_is_its_pass_and_names_the_pr(gh):
    gh(
        {
            "pr view": NOT_A_PR,
            "list:gemini/issue-7": (0, json.dumps([pr_json(70, "gemini/issue-7", "MERGED")]), ""),
            "comments": pages(*credited_round("gemini", 70)),
        }
    )
    state, outcome = resolve("o/r#7", "gemini")
    assert outcome is not None, state
    assert (outcome["merged"], outcome["adjudicated_verdict"], outcome["durability"]) == (
        True,
        "PASS",
        "pending",
    ), outcome
    assert outcome.get("failure_class") is None, outcome
    # The durability sweep resolves an issue-target run's PR from exactly this note.
    assert durability_sweep._explicit_merged_pr_target("o/r#7", outcome["notes"]) == "o/r#70"


def test_its_own_pr_closed_after_a_completed_round_is_its_fail(gh):
    unmeasured = [
        reservation("gemini", 70, "g1", iso(600)),
        receipt("gemini", 70, "g1", iso(600), status="completed"),
    ]
    gh(
        {
            "pr view": NOT_A_PR,
            "list:gemini/issue-7": (0, json.dumps([pr_json(70, "gemini/issue-7", "CLOSED")]), ""),
            "comments": pages(*unmeasured),
        }
    )
    _state, outcome = resolve("o/r#7", "gemini")
    assert outcome is not None
    assert (outcome["merged"], outcome["adjudicated_verdict"], outcome["durability"]) == (
        False,
        "FAIL",
        "abandoned",
    ), outcome


@pytest.mark.parametrize(
    "issue_answer, expected",
    [
        (issue("OPEN"), "pending"),
        (issue(refs=[{"number": 9}]), feedback.UNATTRIBUTED_CLOSING_PR),
        (issue(refs=[]), feedback.UNATTRIBUTED_DELEGATION),
    ],
    ids=["issue-open", "issue-closed-by-a-pr", "issue-closed-by-no-pr"],
)
def test_an_own_branch_pr_settled_before_the_label_is_passed_over(gh, issue_answer, expected):
    """An earlier delegation's PR on the same branch is not this run's: the run is judged as one
    with no PR of its own, and never credited with the old PR."""
    old = pr_json(60, "gemini/issue-7", "MERGED", settled=-86400)
    calls = gh(
        {
            "pr view": NOT_A_PR,
            "list:gemini/issue-7": (0, json.dumps([old]), ""),
            "issue view": issue_answer,
            "closing": outcomes._closing_read(iso(3000), (9, iso(2999))),
        }
    )
    state, outcome = resolve("o/r#7", "gemini")
    assert "comments" not in calls, "a passed-over PR needs no evidence read"
    assert state["passed_over_pr"] == "#60", state
    if expected == "pending":
        assert outcome is None and state["lookup_status"] == "no_pr_for_remote_issue_branch"
    else:
        assert_unattributed(outcome, expected)


def test_a_delegation_whose_issue_closed_with_no_pr_at_all_is_not_its_fail(gh):
    """Workflows#2729 (cursor), #2521 and #2819 (gemini): the label never got a bootstrap PR, so the
    labelled agent never ran, and the issues were closed by hand. A remote delegation's agent runs
    only on the PR its label bootstraps, so this is not its failure (owner decision 2026-10-04,
    amending #411 for remote delegations only)."""
    calls = gh({"pr view": NOT_A_PR, "list:cursor/issue-2729": NO_PR, "issue view": issue(refs=[])})
    state, outcome = resolve("o/r#2729", "cursor")
    assert calls == ["pr view", "list:cursor/issue-2729", "issue view"], calls
    assert state["delegation_without_own_pr"] is True, state
    assert_unattributed(outcome, feedback.UNATTRIBUTED_DELEGATION)
    assert "cursor/issue-2729" in outcome["notes"] and "never had a PR" in outcome["notes"]


@pytest.mark.parametrize("path", ["remote", "local"])
def test_only_a_delegation_reads_a_closed_issue_with_no_pr_as_not_its_failure(gh, path):
    """The keepalive-row resolver and a LOCAL run keep #411's abandoned FAIL: a local run did run,
    and produced nothing that landed."""
    gh({"pr view": NOT_A_PR, "issue view": issue(refs=[]), **_no_pr_on_every_branch(7)})
    resolver = outcomes._pr_state if path == "remote" else outcomes._local_pr_state
    outcome = outcomes.state_to_outcome(resolver("o/r#7", "codex"))
    assert outcome is not None and outcome["adjudicated_verdict"] == "FAIL", outcome


def _no_pr_on_every_branch(num: int) -> dict:
    names = ["codex", "cursor", "claude", "gemini", "vibe", "orchestrator"]
    return {f"list:{name}/issue-{num}": NO_PR for name in names}


def test_a_labelled_pr_settled_before_the_label_is_not_its_work(gh):
    calls = gh({"pr view": (0, json.dumps(pr_json(5, "codex/issue-4", "MERGED", settled=-60)), "")})
    state, outcome = resolve("o/r#5", "gemini")
    assert calls == ["pr view"], "a PR settled before the label needs no evidence read"
    assert "before the label" in state["attribution"]["reason"], state
    assert_unattributed(outcome, feedback.UNATTRIBUTED_DELEGATION)


def test_an_open_pr_waits_and_reads_no_evidence(gh):
    calls = gh({"pr view": (0, json.dumps(pr_json(5, "codex/issue-4", "OPEN")), "")})
    state, outcome = resolve("o/r#5", "gemini")
    assert outcome is None and "attribution" not in state, state
    assert calls == ["pr view"], calls


@pytest.mark.parametrize(
    "answer",
    [RATE_LIMIT, (0, "<html>502</html>", ""), (0, json.dumps([{"body": "x"}]), "")],
    ids=["failed", "unparseable", "not-slurped-pages"],
)
def test_an_evidence_read_that_cannot_answer_is_unanswered_not_unattributed(gh, answer):
    gh(
        {
            "pr view": (0, json.dumps(pr_json(5913, "codex/issue-5858", "MERGED")), ""),
            "comments": answer,
        }
    )
    state, outcome = resolve("o/r#5913", "gemini")
    assert state["lookup_status"] == "runner_rounds_lookup_failed", state
    assert state["lookup_status"] in outcomes.UNANSWERED_LOOKUPS
    assert outcome is None, "unknown is not 'the agent never ran'"


def test_a_pr_whose_view_failed_is_not_read_as_a_closed_issue(gh):
    """`gh issue view` answers for a PR number too; its URL says which one it is."""
    gh(
        {
            "pr view": RATE_LIMIT,
            "list:gemini/issue-5913": NO_PR,
            "issue view": issue(url="https://github.com/o/r/pull/5913"),
        }
    )
    state, outcome = resolve("o/r#5913", "gemini")
    assert state["lookup_status"] == "lookup_failed" and outcome is None, state


def test_an_own_branch_lookup_that_cannot_answer_is_unanswered(gh):
    gh({"pr view": NOT_A_PR, "list:gemini/issue-7": RATE_LIMIT})
    state, outcome = resolve("o/r#7", "gemini")
    assert state["lookup_status"] == "lookup_failed" and outcome is None, state


def test_a_run_without_a_delegation_time_cannot_be_credited(gh):
    gh({"pr view": (0, json.dumps(pr_json(5, "gemini/issue-4", "MERGED")), "")})
    _state, outcome = resolve("o/r#5", "gemini", started=None)
    assert_unattributed(outcome, feedback.UNATTRIBUTED_DELEGATION)


# --- ingest: who goes through the guard, and what it prints ----------------------------------


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability, failure_class, notes "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


def _record_at(run_id: str, agent: str, ts: int) -> None:
    feedback.record_run(run_id, "o/r#5913", "implement", agent, mode="remote")
    with feedback._conn() as c:
        c.execute("UPDATE runs SET ts=? WHERE run_id=?", (ts, run_id))


def test_ingest_guards_delegations_and_leaves_keepalive_rows_to_their_own_pr(brain, gh):
    """Both rows point at Trend#5913. The keepalive row IS that PR (codex ran it); the delegation
    labelled gemini onto it and gemini never ran, so only the keepalive row is a PASS."""
    _record_at("keepalive:o/r#5913:codex", "codex", LABEL - 3600)
    _record_at("remote:o/r#5913:gemini", "gemini", LABEL)
    gh(
        {
            "pr view": (0, json.dumps(pr_json(5913, "codex/issue-5858", "MERGED")), ""),
            "comments": pages(*credited_round("codex", 5913)),
        }
    )
    first = outcomes.ingest_modes("remote")
    assert (first["recorded"], first["unattributed"], first["unanswered"]) == (2, 1, 0), first
    assert _row("keepalive:o/r#5913:codex")[:2] == (1, "PASS")
    merged, verdict, durability, failure_class, notes = _row("remote:o/r#5913:gemini")
    assert (merged, verdict, durability) == (None, None, "abandoned")
    assert failure_class == feedback.UNATTRIBUTED_DELEGATION and "gemini" in notes
    # Drained: the delegation is terminal, so the next ingest does not ask about it again.
    second = outcomes.ingest_modes("remote")
    assert second["unattributed"] == 0 and second["recorded"] == 0, second
    assert "remote:o/r#5913:gemini" not in {r["run_id"] for r in feedback.runs_needing_outcome()}


def test_an_unanswered_evidence_read_is_retried_and_then_drains(brain, gh):
    _record_at("remote:o/r#5913:gemini", "gemini", LABEL)
    merged_pr = (0, json.dumps(pr_json(5913, "codex/issue-5858", "MERGED")), "")
    gh({"pr view": merged_pr, "comments": RATE_LIMIT})
    first = outcomes.ingest_modes("remote")
    assert (first["recorded"], first["unanswered"]) == (0, 1), first
    assert _row("remote:o/r#5913:gemini") is None
    gh({"pr view": merged_pr, "comments": pages()})
    second = outcomes.ingest_modes("remote")
    assert (second["recorded"], second["unanswered"], second["unattributed"]) == (1, 0, 1), second


# --- no learner scores it ------------------------------------------------------------------


def test_the_delegation_class_is_in_the_one_excluded_set():
    assert feedback.UNATTRIBUTED_DELEGATION in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert feedback.UNATTRIBUTED_DELEGATION in feedback.NONATTRIBUTABLE_FAILURE_CLASSES
    assert outcomes.UNATTRIBUTED_CLASSES <= feedback.LEARNING_EXCLUDED_FAILURE_CLASSES


@pytest.mark.parametrize("learner", ["relearn_quality", "relearn"])
def test_no_route_learner_scores_an_unattributed_delegation(brain, learner):
    feedback.record_run("remote:o/r#1:gemini", "o/r#1", "implement", "gemini", mode="remote")
    feedback.record_outcome(
        "remote:o/r#1:gemini",
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
        notes="remote delegation's PR #5 merged, but gemini's runner completed 0 rounds",
    )
    feedback.record_run("remote:o/r#2:gemini", "o/r#2", "implement", "gemini", mode="remote")
    feedback.record_outcome(
        "remote:o/r#2:gemini", adjudicated_verdict="FAIL", merged=False, durability="abandoned"
    )
    version = getattr(feedback, learner)({"implement": {"gemini": 0.5}})
    with feedback._conn() as c:
        prior, posterior, n_obs = c.execute(
            "SELECT prior, posterior, n_obs FROM route_weights "
            "WHERE version=? AND task_type='implement' AND agent='gemini'",
            (version,),
        ).fetchone()
    assert n_obs == 1, f"{learner} counted the unattributed delegation: n_obs={n_obs}"
    assert posterior < prior, "the control FAIL must still lower the posterior"


def test_a_role_run_inherits_the_delegation_class(brain):
    feedback.record_role_run("role:triage:gemini:9", "triage", "triage:1-items", "gemini")
    feedback.record_run(
        "remote:o/r#9:gemini",
        "o/r#9",
        "implement",
        "gemini",
        mode="remote",
        influenced_by_role_run_ids=["role:triage:gemini:9"],
    )
    feedback.record_outcome(
        "remote:o/r#9:gemini",
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
        notes="remote delegation's PR #5 merged, but gemini's runner completed 0 rounds",
    )
    assert _row("role:triage:gemini:9")[3] == feedback.UNATTRIBUTED_DELEGATION
