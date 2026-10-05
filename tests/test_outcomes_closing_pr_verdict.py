"""A closed issue whose closing PR no candidate branch produced is not a failure (2026-10-04).

THE DEFECT. Outcome ingest resolves a delegate run to the PR on its candidate branches
(`{agent}/issue-N`, `orchestrator/issue-N`, ...). When none had a PR and the issue was closed, the
run was recorded merged=False / FAIL / abandoned, which `relearn_quality` scores as a full failure,
even though the issue's own `closedByPullRequestsReferences` named the PR that closed it. Measured on
the live Brain: 23 such rows, and for 18 a MERGED closing PR existed before the verdict was written
(15 of them not already excluded as infra). Five were remote delegations whose labelled agent never
ran (the closing PRs were codex keepalive or manual work); at least four local ones were the runs'
OWN merged PRs on branches the agent chose. Seven role:triage runs inherited the label.

THE RULE. No candidate-branch PR plus a closing PR means someone delivered and nothing says it was
this run. The run is terminal (it leaves the pending set: a closed issue never changes, so skipping
it would latch) and records no verdict and no merge state, with `feedback.UNATTRIBUTED_CLOSING_PR`,
a class every learner excludes through `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES`. A closed issue
with NO closing PR keeps the abandoned FAIL it was built for. No real API: `subprocess.run` is
replaced by a stub that answers by argv.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

import capabilities
import exploration_review
import feedback
import outcomes

NO_PR = (0, "[]", "")
RATE_LIMIT = (1, "", "API rate limit exceeded for user ID 23046322.")
CLOSING_REF = {
    "id": "PR_kwDOfixture",
    "number": 3067,
    "repository": {"id": "R_fixture", "name": "r", "owner": {"id": "U_fixture", "login": "o"}},
    "url": "https://github.com/o/r/pull/3067",
}


def _issue(state: str = "CLOSED", **fields) -> tuple:
    return (0, json.dumps({"state": state, **fields}), "")


CLOSED_BY_PR = _issue(closedByPullRequestsReferences=[CLOSING_REF])
CLOSED_NO_PR = _issue(closedByPullRequestsReferences=[])
RESOLVERS = {"remote": outcomes._pr_state, "local": outcomes._local_pr_state}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def gh(monkeypatch):
    """Install a stub gh: every candidate branch answers `branch`, the issue view answers `issue`."""

    def install(*, issue=CLOSED_BY_PR, branch=NO_PR):
        def fake_run(argv, capture_output=True, text=True, **_kw):
            verb = tuple(argv[1:3])
            if verb == ("pr", "view"):  # the target is an issue number, not a PR
                return subprocess.CompletedProcess(argv, 1, "", "Could not resolve to a PR")
            if verb == ("pr", "list"):
                return subprocess.CompletedProcess(argv, *branch)
            if verb == ("issue", "view"):
                return subprocess.CompletedProcess(argv, *issue)
            raise AssertionError(f"unexpected gh call: {argv}")

        monkeypatch.setattr(outcomes.subprocess, "run", fake_run)

    return install


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability, failure_class, notes "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


def _weight(version: int, task_type: str, agent: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT prior, posterior, n_obs FROM route_weights "
            "WHERE version=? AND task_type=? AND agent=?",
            (version, task_type, agent),
        ).fetchone()


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_a_closing_pr_no_candidate_branch_produced_is_unattributed_not_failed(gh, path):
    gh(issue=CLOSED_BY_PR)
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] in outcomes.CLOSED_ISSUE_LOOKUPS, state
    assert state["closing_prs"] == ["#3067"], state
    outcome = outcomes.state_to_outcome(state)
    assert outcome["failure_class"] == feedback.UNATTRIBUTED_CLOSING_PR, outcome
    assert outcome["adjudicated_verdict"] is None, "a closing PR is not a FAIL verdict"
    assert outcome["merged"] is None, "nothing says whether this run's PR merged"
    assert outcome["durability"] not in (None, "pending"), "the run must leave the pending set"
    assert "#3067" in outcome["notes"], outcome


@pytest.mark.parametrize("path", sorted(RESOLVERS))
def test_a_closed_issue_no_pr_closed_is_still_an_abandoned_failure(gh, path):
    """The case the verdict was built for (2026-06-23, Counter_Risk#711) keeps its FAIL."""
    gh(issue=CLOSED_NO_PR)
    outcome = outcomes.state_to_outcome(RESOLVERS[path]("o/r#7", "codex"))
    assert (outcome["merged"], outcome["adjudicated_verdict"], outcome["durability"]) == (
        False,
        "FAIL",
        "abandoned",
    ), outcome
    assert outcome.get("failure_class") is None, outcome


@pytest.mark.parametrize("path", sorted(RESOLVERS))
@pytest.mark.parametrize(
    "issue",
    [
        _issue(),
        _issue(closedByPullRequestsReferences=None),
        _issue(closedByPullRequestsReferences={}),
    ],
    ids=["missing", "null", "not-a-list"],
)
def test_a_closed_issue_without_its_closing_list_is_unanswered(gh, path, issue):
    """The closing list now decides the verdict, so a CLOSED answer without one cannot say "nobody
    delivered": it is the third answer, skipped and retried, never an abandoned FAIL."""
    gh(issue=issue)
    state = RESOLVERS[path]("o/r#7", "codex")
    assert state["lookup_status"] == "issue_lookup_failed", state
    assert outcomes.state_to_outcome(state) is None


def test_ingest_records_it_once_and_the_run_drains(brain, gh):
    """End to end, and the latched-gate question 1: the verdict is the drain. It is written on the
    first pass that gets answers, and the run is gone from the pending set on the next."""
    feedback.record_run("remote:o/r#7:cursor", "o/r#7", "mechanical", "cursor", mode="remote")
    gh(issue=CLOSED_BY_PR)
    first = outcomes.ingest_modes("remote")
    assert (first["recorded"], first["unattributed"], first["unanswered"]) == (1, 1, 0), first
    merged, verdict, durability, failure_class, notes = _row("remote:o/r#7:cursor")
    assert (merged, verdict, durability) == (None, None, "abandoned")
    assert failure_class == feedback.UNATTRIBUTED_CLOSING_PR and "#3067" in notes

    second = outcomes.ingest_modes("remote")
    assert (second["pending"], second["recorded"], second["unattributed"]) == (0, 0, 0), second
    assert "remote:o/r#7:cursor" not in {r["run_id"] for r in feedback.runs_needing_outcome()}


def test_a_failed_lookup_then_an_answer_drains_to_the_right_verdict(brain, gh):
    """#404's rule still holds on this path: unknown first, retried, then the answered verdict."""
    feedback.record_run("o__r_7-codex-1", "o/r#7", "implement", "codex", mode="local")
    gh(issue=CLOSED_BY_PR, branch=RATE_LIMIT)
    first = outcomes.ingest_modes("local")
    assert (first["recorded"], first["unanswered"], first["unattributed"]) == (0, 1, 0), first
    assert _row("o__r_7-codex-1") is None
    gh(issue=CLOSED_BY_PR)
    second = outcomes.ingest_modes("local")
    assert (second["recorded"], second["unanswered"], second["unattributed"]) == (1, 0, 1), second


def test_unattributed_reads_zero_when_drained(brain, gh):
    """What the count prints when nothing is unattributed, proven by construction: present and 0 on
    an empty store and on a store with only open work -- never missing, never a falsy stand-in."""
    empty = outcomes.ingest_modes("both")
    assert empty["unattributed"] == 0 and [r["unattributed"] for r in empty["results"]] == [0, 0]
    feedback.record_run("remote:o/r#9:codex", "o/r#9", "implement", "codex", mode="remote")
    gh(issue=_issue("OPEN", closedByPullRequestsReferences=[CLOSING_REF]))
    waiting = outcomes.ingest_modes("both")
    assert waiting["skipped"] == 1 and waiting["unattributed"] == 0, waiting


def test_the_excluded_set_is_one_set_and_unclassified_failures_still_train():
    assert feedback.UNATTRIBUTED_CLOSING_PR in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert "transient_infra" in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert "" not in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES, (
        "an unclassified FAIL is an attributed verdict to the route learners; excluding '' would "
        "silence every outcome"
    )
    assert feedback.LEARNING_EXCLUDED_FAILURE_CLASSES <= feedback.NONATTRIBUTABLE_FAILURE_CLASSES
    assert (
        feedback.NONATTRIBUTABLE_FAILURE_CLASSES - feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
        == {""}
    )


def test_the_evidence_predicate_cannot_be_called_without_the_class():
    """Required with no default: a new reader of outcomes cannot silently skip the exclusion."""
    with pytest.raises(TypeError):
        feedback._has_outcome_evidence("abandoned", "FAIL", None)  # type: ignore[call-arg]
    for excluded in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
        assert not feedback._has_outcome_evidence(
            "abandoned", "FAIL", None, failure_class=excluded
        ), excluded
    assert feedback._has_outcome_evidence("abandoned", "FAIL", None, failure_class=None)


def _two_runs(task_type: str = "implement", agent: str = "codex") -> None:
    """One unattributed run and one plain abandoned FAIL control, in one learner cell."""
    feedback.record_run("closed-by-other", "o/r#1", task_type, agent, mode="local")
    feedback.record_outcome(
        "closed-by-other",
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
        notes="local delegate issue closed by a PR no candidate branch produced (#3067)",
    )
    feedback.record_run("real-failure", "o/r#2", task_type, agent, mode="local")
    feedback.record_outcome(
        "real-failure", adjudicated_verdict="FAIL", merged=False, durability="abandoned"
    )


@pytest.mark.parametrize("learner", ["relearn_quality", "relearn"])
def test_no_route_learner_scores_an_unattributed_row(brain, learner):
    _two_runs()
    version = getattr(feedback, learner)({"implement": {"codex": 0.5}})
    prior, posterior, n_obs = _weight(version, "implement", "codex")
    assert n_obs == 1, f"{learner} counted the unattributed row: n_obs={n_obs}"
    assert posterior < prior, "the control FAIL must still lower the posterior"


def test_the_capability_tally_reads_an_unattributed_fail_as_churn(brain, monkeypatch):
    """A legacy row relabelled by class alone keeps its FAIL; the tally must still not count it,
    because NONATTRIBUTABLE_FAILURE_CLASSES is derived from the excluded set."""
    ledger = brain / "capabilities.json"
    now = int(time.time())
    cap = capabilities.register_compiled_version(
        "capability:closing-pr-fixture",
        target_kind="workflow",
        artifact={"entrypoint": "fixture.py:run", "content": "v1"},
        lifecycle_policy={"min_independent_durable_reuse": 2},
        record={
            "status": "canary",
            "owner": "orchestrator",
            "matcher": {"repository": "o/r", "task_types": ["implement"]},
            "entrypoint": "fixture.py:run",
            "trigger_cadence": "per matching task",
            "output_artifact": "fixture-result.json",
            "downstream_consumer": "dispatcher.py",
            "learning_sink": "feedback capability joins",
            "expiry": now + 86400,
            "activation_deadline": now + 86400,
            "next_transition": "active",
            "kill_switch": "ORCH_FIXTURE=0",
            "rollback": {"transition": "retired", "predecessor": "baseline"},
            "predecessor": "baseline",
        },
        path=ledger,
    )
    feedback.record_run("work:1", "o/r#1", "implement", "codex")
    feedback.record_capability_consumption(
        capability_id=cap["capability_id"],
        capability_version_id=cap["capability_version_id"],
        source_run_id="capability-output:1",
        target_run_id="work:1",
        accepted=True,
        producer="selftest",
    )
    feedback.record_outcome(
        "work:1",
        adjudicated_verdict="FAIL",
        merged=False,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
    )
    (row,) = feedback.capability_causal_evidence(cap["capability_id"], cap["capability_version_id"])
    assert row["tally_class"] == "churn_unattributed", row


def test_a_role_run_inherits_the_exclusion_with_the_verdict(brain):
    """The 7-row case: a role run copies the acting run's outcome over an accepted edge. It must
    copy the class too, or the role trains on a label its acting run was never judged by."""
    feedback.record_role_run("role:triage:gemini:1", "triage", "triage:3-items", "gemini")
    feedback.record_run(
        "remote:o/r#7:cursor",
        "o/r#7",
        "mechanical",
        "cursor",
        mode="remote",
        influenced_by_role_run_ids=["role:triage:gemini:1"],
    )
    feedback.record_outcome(
        "remote:o/r#7:cursor",
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
        notes="remote delegate issue closed by a PR no candidate branch produced (#3067)",
    )
    role = _row("role:triage:gemini:1")
    assert role is not None and role[4].startswith("automatically influenced"), role
    assert role[3] == feedback.UNATTRIBUTED_CLOSING_PR, f"the role row lost the exclusion: {role}"
    version = feedback.relearn_quality({"role:triage": {"gemini": 0.5}})
    assert _weight(version, "role:triage", "gemini")[2] == 0


def test_a_role_run_keeps_its_own_class_when_the_acting_run_has_none(brain):
    """Only an excluded class travels; a plain verdict never clears what the role row carries."""
    feedback.record_role_run("role:triage:gemini:2", "triage", "triage:1-items", "gemini")
    feedback.record_outcome(
        "role:triage:gemini:2", durability="abandoned", failure_class="transient_infra"
    )
    feedback.record_run(
        "work:2",
        "o/r#8",
        "implement",
        "codex",
        mode="local",
        influenced_by_role_run_ids=["role:triage:gemini:2"],
    )
    feedback.record_outcome("work:2", adjudicated_verdict="PASS", merged=True, durability="pending")
    assert _row("role:triage:gemini:2")[3] == "transient_infra"
    feedback.record_outcome("work:2", durability="broke_later")
    assert _row("role:triage:gemini:2")[3] == "transient_infra"
    feedback.record_outcome("work:2", notes="late downstream observation")
    role = _row("role:triage:gemini:2")
    assert role[3] == "transient_infra", role
    assert not feedback._has_outcome_evidence(role[2], role[1], None, failure_class=role[3]), role


def test_the_exploration_gate_does_not_count_it(brain):
    """exploration_review gates the routing mode on the same evidence the learner scores."""
    metadata = {
        "source": "router_assignment",
        "exploration": True,
        "exploration_mode": "thompson-hybrid",
    }
    feedback.record_run(
        "explore:1", "o/r#1", "testgen", "vibe", mode="local", routing_metadata=metadata
    )
    feedback.record_outcome(
        "explore:1", durability="abandoned", failure_class=feedback.UNATTRIBUTED_CLOSING_PR
    )
    evidence = exploration_review._recorded_exploration_evidence()
    assert evidence["outcome_exploration_runs"] == 0, evidence
    feedback.record_run(
        "explore:2", "o/r#2", "testgen", "vibe", mode="local", routing_metadata=metadata
    )
    feedback.record_outcome("explore:2", adjudicated_verdict="FAIL", durability="abandoned")
    assert exploration_review._recorded_exploration_evidence()["outcome_exploration_runs"] == 1


def test_closing_pr_names_carry_the_repo_only_when_it_differs():
    assert outcomes._closing_pr_name(CLOSING_REF, "o/r") == "#3067"
    assert outcomes._closing_pr_name(CLOSING_REF, "o/other") == "o/r#3067"
    assert outcomes._closing_pr_name({"url": "https://x/pull/1"}, "o/r") == "https://x/pull/1"
