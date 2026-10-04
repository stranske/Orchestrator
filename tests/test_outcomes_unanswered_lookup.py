"""A PR lookup that could not answer is not "no PR here" (outcome ingest, 2026-10-04).

THE DEFECT. `_remote_issue_pr_state` asked each candidate branch for a PR, `continue`d past every
lookup that FAILED, and then asked whether the issue was closed. A closed issue became
`closed_issue_no_remote_pr`, which `state_to_outcome` records as merged=False, FAIL, `abandoned`,
and `relearn_quality` scores as a full failure for that agent. So when a rate limit, a 5xx or a
GraphQL node limit hid every branch while the issue read CLOSED, a run whose PR may have merged was
written into the router's outcome labels as abandoned. The local path skipped `lookup_failed`, but
read `parse_failed` (gh exit 0 with output that does not parse) as "no PR" in both paths.

THE RULE. A lookup answers FOUND, NO-PR, or could-not-answer. Only NO-PR is evidence of absence, so
the closed-issue verdict needs every candidate branch to answer NO-PR. The first unanswered lookup
ends the resolution: the run is skipped with no outcome row, counted in the summary's `unanswered`,
and the next ingest asks again. No real API: `subprocess.run` is replaced by a stub that answers by
argv.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import feedback
import outcomes

RATE_LIMIT = (1, "", "API rate limit exceeded for user ID 23046322.")
UNPARSEABLE = (0, "<html>502 Bad Gateway</html>", "")
NO_PR = (0, "[]", "")


def _merged(branch: str) -> tuple:
    pr = {
        "number": 70,
        "state": "MERGED",
        "mergedAt": "2026-10-01T00:00:00Z",
        "headRefName": branch,
    }
    return (0, json.dumps([pr]), "")


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def gh(monkeypatch):
    """Install a stub gh. `branches` maps an agent prefix to an answer (default: `default`);
    `issue` is the issue-view answer. Returns the list of branches looked up, in order."""
    looked_up: list[str] = []

    def install(*, default=NO_PR, branches=None, issue=(0, json.dumps({"state": "CLOSED"}), "")):
        branches = branches or {}

        def fake_run(argv, capture_output=True, text=True, **_kw):
            verb = tuple(argv[1:3])
            if verb == ("pr", "view"):  # the target is an issue number, not a PR
                return subprocess.CompletedProcess(argv, 1, "", "Could not resolve to a PR")
            if verb == ("pr", "list"):
                branch = argv[argv.index("--head") + 1]
                looked_up.append(branch)
                answer = branches.get(branch.split("/")[0], default)
                return subprocess.CompletedProcess(
                    argv, *(answer(branch) if callable(answer) else answer)
                )
            if verb == ("issue", "view"):
                return subprocess.CompletedProcess(argv, *issue)
            raise AssertionError(f"unexpected gh call: {argv}")

        monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
        return looked_up

    return install


RESOLVERS = {"remote": outcomes._pr_state, "local": outcomes._local_pr_state}


def _outcome_row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability FROM outcomes WHERE run_id=?", (run_id,)
        ).fetchone()


def test_failed_branch_lookups_on_a_closed_issue_record_no_abandonment(brain, gh):
    """The incident end to end: every branch lookup is rate-limited while the issue reads CLOSED.
    No outcome row may be written, the skip must say why, and the next ingest must record the real
    verdict once GitHub answers -- the unresolved run drains, it does not latch."""
    feedback.record_run("remote:o/r#7:codex", "o/r#7", "implement", "codex", mode="remote")
    gh(default=RATE_LIMIT)
    first = outcomes.ingest_modes("remote")
    assert _outcome_row("remote:o/r#7:codex") is None, "unknown was written as an outcome"
    assert (first["recorded"], first["skipped"], first["unanswered"]) == (0, 1, 1), first
    skip = first["skipped_details"][0]
    assert skip["reason"] == "lookup_failed" and "rate limit" in skip["error"], skip

    gh(default=NO_PR)
    second = outcomes.ingest_modes("remote")
    assert (second["recorded"], second["unanswered"]) == (1, 0), second
    assert _outcome_row("remote:o/r#7:codex") == (0, "FAIL", "abandoned")


@pytest.mark.parametrize("path", sorted(RESOLVERS))
@pytest.mark.parametrize(
    "answer, status",
    [(RATE_LIMIT, "lookup_failed"), (UNPARSEABLE, "parse_failed")],
    ids=["gh-failed", "unparseable"],
)
def test_an_unanswered_branch_lookup_never_reaches_the_closed_issue_verdict(
    gh, path, answer, status
):
    gh(default=answer)
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] == status, state
    assert outcomes.state_to_outcome(state) is None, state
    assert outcomes._skip_reason(state) in outcomes.UNANSWERED_LOOKUPS


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_one_unanswered_branch_among_empty_ones_is_still_unresolved(gh, path):
    """Mixed answers: the agent's own branch answered NO-PR, a fallback branch failed. The failed
    branch is unknown, so the closed-issue verdict cannot follow."""
    gh(default=NO_PR, branches={"gemini": RATE_LIMIT})
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] == "lookup_failed" and state["branch"] == "gemini/issue-7"
    assert outcomes.state_to_outcome(state) is None


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_a_find_after_an_unanswered_branch_is_unresolved(gh, path):
    """The walk prefers the agent's own branch. If that lookup failed, a PR found on a fallback
    branch may not be the right one, so it is not credited either way."""
    gh(default=NO_PR, branches={"codex": RATE_LIMIT, "cursor": _merged})
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] == "lookup_failed", state
    assert outcomes.state_to_outcome(state) is None


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_every_branch_answering_no_pr_still_records_abandonment(gh, path):
    """The fix must not over-correct: a closed issue whose every candidate branch ANSWERED "no PR"
    is a real terminal state and is still recorded as abandoned."""
    looked_up = gh(default=NO_PR)
    state = RESOLVERS[path]("o/r#7", "codex")
    expected = len(outcomes._local_candidate_branches(7, "codex")) if path == "local" else 5
    assert len(looked_up) == expected, looked_up
    assert state["lookup_status"] in {"closed_issue_no_remote_pr", "closed_issue_no_branch_pr"}
    outcome = outcomes.state_to_outcome(state)
    assert outcome is not None and outcome["durability"] == "abandoned", outcome


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_a_find_with_every_earlier_branch_answered_is_credited(gh, path):
    gh(default=NO_PR, branches={"cursor": _merged})
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] == "found" and state["branch"] == "cursor/issue-7", state
    assert outcomes.state_to_outcome(state)["merged"] is True


@pytest.mark.parametrize("path", sorted(RESOLVERS))
@pytest.mark.parametrize(
    "issue",
    [(1, "", "HTTP 502: Bad Gateway"), (0, "{}", ""), (0, "not json", "")],
    ids=["gh-failed", "no-state", "unparseable"],
)
def test_an_unanswered_issue_lookup_is_counted_not_read_as_open(gh, path, issue):
    gh(default=NO_PR, issue=issue)
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] == "issue_lookup_failed", state
    assert outcomes.state_to_outcome(state) is None


@pytest.mark.parametrize("stdout", ["{}", "null", '"x"', "[1]"])
def test_only_an_empty_list_means_no_pr_on_the_branch(gh, stdout):
    """`{}` and `null` are falsy like `[]`; reading them as an answer is the same unknown-as-no."""
    gh(default=(0, stdout, ""))
    assert outcomes._pr_list_by_head("o/r", "codex/issue-7") == {
        "lookup_status": "parse_failed",
        "branch": "codex/issue-7",
    }
    gh(default=NO_PR)
    assert outcomes._pr_list_by_head("o/r", "codex/issue-7") is None


def test_the_walk_stops_at_the_first_unanswered_lookup(gh):
    """Under a rate limit every later call fails too; asking five branches spends four calls for
    nothing, and nothing after the first unanswered lookup can produce a verdict."""
    looked_up = gh(default=RATE_LIMIT)
    outcomes._pr_state("o/r#7", "codex")
    assert looked_up == ["codex/issue-7"], looked_up


def test_unanswered_reads_zero_when_nothing_is_waiting_on_github(brain, gh):
    """What the count prints when drained, proven by construction: the key is present and 0 on a
    run with work and on a run with none -- never missing, never a falsy stand-in."""
    assert outcomes.ingest_modes("both")["unanswered"] == 0
    feedback.record_run("remote:o/r#9:codex", "o/r#9", "implement", "codex", mode="remote")
    gh(default=NO_PR, issue=(0, json.dumps({"state": "OPEN"}), ""))
    summary = outcomes.ingest_modes("both")
    assert summary["skipped"] == 1 and summary["unanswered"] == 0, summary
    assert [row["unanswered"] for row in summary["results"]] == [0, 0], summary
