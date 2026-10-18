"""The lifecycle tally scores no outcome a learner may not, whatever made it a failure.

THE DEFECT (measured 2026-10-12 on a copy of the live Brain). `capability_causal_evidence` refused a
class in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` only on its adjudicated-FAIL branch. A
VERIFIER fail took a second branch that never read the class, so 11 `transient_infra` runs of
agy-runtime-isolation (verifier FAIL, durability pending) tallied as failures. The live ledger's
routing prior for it read beta 20, posterior 0.2, where 0.357 was due. CLAUDE.md §2: infra and
killed failures are excluded from learning, in every learner, through that ONE set.

THE RULE THIS PINS. A consumed terminal row whose class is in the set tallies `learning_excluded`:
never `success`, never `failure`, never a regression that holds the capability back, never a durable
subject. Readiness counts it in `learning_excluded`, so the set-aside is a number. The set is read
at call time. A durable subject is a `success` tally, the classification the prior counts, so an
advisory role run holding a copy of its acting run's outcome is not one either.

Every row is written by the production writers, on a capability registered with
`register_compiled_version`, as in `test_capability_causal_core.py`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import capabilities
import feedback

NOW = int(time.time())


@pytest.fixture
def ledger(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path / "capabilities.json"


def _register(ledger: Path) -> dict:
    return capabilities.register_compiled_version(
        "capability:excluded-fixture",
        target_kind="workflow",
        artifact={"entrypoint": "fixture.py:run", "content": "v1"},
        lifecycle_policy={"min_independent_durable_reuse": 1},
        record={
            "status": "canary",
            "owner": "orchestrator",
            "matcher": {"repository": "owner/repo", "task_types": ["implement"]},
            "entrypoint": "fixture.py:run",
            "trigger_cadence": "per matching task",
            "output_artifact": "fixture-result.json",
            "downstream_consumer": "dispatcher.py",
            "learning_sink": "feedback capability joins",
            "expiry": NOW + 86400,
            "activation_deadline": NOW + 86400,
            "next_transition": "active",
            "kill_switch": "ORCH_FIXTURE=0",
            "rollback": {"transition": "retired", "predecessor": "baseline"},
            "predecessor": "baseline",
        },
        path=ledger,
    )


def _consumed(cap: dict, n: int, **outcome) -> str:
    run_id = f"work:{n}"
    feedback.record_run(run_id, f"owner/repo#{n}", "implement", "codex")
    feedback.record_capability_consumption(
        capability_id=cap["capability_id"],
        capability_version_id=cap["capability_version_id"],
        source_run_id=f"capability-output:{n}",
        target_run_id=run_id,
        accepted=True,
        producer="selftest",
        receipt_ref="fixture:consumed-output",
    )
    feedback.record_outcome(run_id, **outcome)
    return run_id


def _reconcile(cap: dict, ledger: Path) -> dict:
    return capabilities.reconcile_causal_lifecycle(
        cap["capability_id"], path=ledger, timestamp=NOW + 30
    )


def _tally(cap: dict) -> list[str]:
    rows = feedback.capability_causal_evidence(cap["capability_id"], cap["capability_version_id"])
    return [row["tally_class"] for row in rows]


@pytest.mark.parametrize("failure_class", sorted(feedback.LEARNING_EXCLUDED_FAILURE_CLASSES))
def test_a_verifier_fail_in_an_excluded_class_never_trains_the_prior(ledger, failure_class):
    # The live shape: verifier FAIL, adjudicated FAIL, durability pending, no merge.
    cap = _register(ledger)
    _consumed(
        cap,
        1,
        verifier_verdict="FAIL",
        adjudicated_verdict="FAIL",
        durability="pending",
        failure_class=failure_class,
    )
    assert _tally(cap) == ["learning_excluded"]
    result = _reconcile(cap, ledger)
    assert result["routing_prior"]["observations"] == 0, result["routing_prior"]
    assert result["routing_prior"]["beta"] == 1.0, result["routing_prior"]
    assert (result["readiness"]["failures"], result["readiness"]["learning_excluded"]) == (0, 1)


def test_the_same_verifier_fail_outside_the_set_still_counts(ledger):
    cap = _register(ledger)
    _consumed(cap, 1, verifier_verdict="FAIL", adjudicated_verdict="FAIL", durability="pending")
    assert _tally(cap) == ["failure"]
    result = _reconcile(cap, ledger)
    assert (result["routing_prior"]["beta"], result["readiness"]["failures"]) == (2.0, 1)
    assert result["readiness"]["learning_excluded"] == 0


def test_an_excluded_regression_holds_nothing_back(ledger):
    # A merged-and-reverted outcome whose class says it is not the run's own work.
    cap = _register(ledger)
    _consumed(
        cap,
        1,
        adjudicated_verdict="PASS",
        merged=True,
        durability="reverted",
        failure_class=feedback.UNATTRIBUTED_REPLACEMENT,
    )
    assert _tally(cap) == ["learning_excluded"]
    result = _reconcile(cap, ledger)
    assert result["rollback_pending"] is None, result["rollback_pending"]
    assert result["routing_prior"]["observations"] == 0


def test_an_excluded_durable_merge_is_not_a_durable_subject(ledger):
    cap = _register(ledger)
    _consumed(
        cap,
        1,
        adjudicated_verdict="PASS",
        merged=True,
        durability="durable",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
    )
    assert _tally(cap) == ["learning_excluded"]
    result = _reconcile(cap, ledger)
    assert result["readiness"]["durable_subjects"] == [], result["readiness"]
    assert result["routing_prior"]["alpha"] == 1.0


def test_the_set_is_read_when_the_tally_is_made(ledger, monkeypatch):
    monkeypatch.setattr(
        feedback,
        "LEARNING_EXCLUDED_FAILURE_CLASSES",
        feedback.LEARNING_EXCLUDED_FAILURE_CLASSES | {"a_class_added_later"},
    )
    cap = _register(ledger)
    _consumed(
        cap,
        1,
        verifier_verdict="FAIL",
        adjudicated_verdict="FAIL",
        durability="pending",
        failure_class="a_class_added_later",
    )
    assert _tally(cap) == ["learning_excluded"]


def test_a_durable_subject_is_a_success_tally(ledger):
    """`_causal_readiness` read `durable_success`, which an advisory role run holding a copy of its
    acting run's outcome also carries, under a subject of its own (`triage:N-items`)."""
    cap = _register(ledger)
    rows = [
        {
            "accepted_consumption": True,
            "terminal_outcome": True,
            "durable_success": True,
            "tally_class": tally,
            "subject_id": subject,
            "rework": False,
            "observed_ts": NOW,
        }
        for tally, subject in (("success", "owner/repo#1"), ("advisory_self", "triage:1-items"))
    ]
    ready = capabilities._causal_readiness(cap, rows, NOW + 30)
    assert ready["durable_subjects"] == ["owner/repo#1"], ready


def test_a_capability_on_a_role_run_is_an_association_not_a_second_delivery(ledger):
    """Any capability other than the role's own, on a role run, is `role_association` (the
    2026-08-21 decision for `offload`). The role run's outcome is a copy of its acting run's, and
    the acting run carries the capability's own edge: tallied, one delivery counts twice."""
    cap = _register(ledger)
    role_run = "role:triage:gemini:1"
    feedback.record_run(role_run, "triage:1-items", "role:triage", "gemini", role_name="triage")
    feedback.record_capability_consumption(
        capability_id=cap["capability_id"],
        capability_version_id=cap["capability_version_id"],
        source_run_id="capability-output:role",
        target_run_id=role_run,
        accepted=True,
        producer="selftest",
        receipt_ref="fixture:consumed-output",
    )
    # What propagation writes onto a role run when its acting run merges durably.
    feedback.record_outcome(role_run, adjudicated_verdict="PASS", merged=True, durability="durable")
    _consumed(cap, 2, adjudicated_verdict="PASS", merged=True, durability="durable")
    assert sorted(_tally(cap)) == ["role_association", "success"]
    result = _reconcile(cap, ledger)
    assert result["routing_prior"]["alpha"] == 2.0, result["routing_prior"]
    assert result["readiness"]["durable_subjects"] == ["owner/repo#2"], result["readiness"]
    assert result["readiness"]["role_association_edges"] == 1, result["readiness"]


def _row(tally: str, *, rework: bool = False, ts: int = NOW, subject: str = "owner/repo#1") -> dict:
    return {
        "accepted_consumption": True,
        "terminal_outcome": True,
        "durable_success": tally == "success",
        "tally_class": tally,
        "subject_id": subject,
        "rework": rework,
        "observed_ts": ts,
    }


@pytest.mark.parametrize("tally", ["learning_excluded", "advisory_self", "role_association"])
def test_an_unscored_reworked_row_never_blocks_promotion(ledger, tally):
    cap = _register(ledger)
    ready = capabilities._causal_readiness(cap, [_row(tally, rework=True)], NOW + 30)
    assert (ready["rework"], ready["criteria"]["rework"]) == (0, True), ready


@pytest.mark.parametrize("tally", ["learning_excluded", "advisory_self", "role_association"])
def test_an_unscored_recent_row_never_refreshes_stale_evidence(ledger, tally):
    cap = _register(ledger)
    stale = NOW - 90 * 86400
    rows = [_row("success", ts=stale), _row(tally, ts=NOW, subject="owner/repo#2")]
    ready = capabilities._causal_readiness(cap, rows, NOW + 30)
    assert ready["latest_evidence_ts"] == stale, ready
    assert ready["criteria"]["evidence_age"] is False, ready
    assert (ready["terminal_outcomes"], ready["scored_outcomes"]) == (2, 1), ready


def test_a_role_run_regression_holds_nothing_back(ledger):
    cap = _register(ledger)
    role_run = "role:triage:gemini:1"
    feedback.record_run(role_run, "triage:1-items", "role:triage", "gemini", role_name="triage")
    feedback.record_capability_consumption(
        capability_id=cap["capability_id"],
        capability_version_id=cap["capability_version_id"],
        source_run_id="capability-output:role",
        target_run_id=role_run,
        accepted=True,
        producer="selftest",
        receipt_ref="fixture:consumed-output",
    )
    # A copy of an acting run's merged-then-reverted outcome.
    feedback.record_outcome(
        role_run, adjudicated_verdict="PASS", merged=True, durability="reverted"
    )
    assert _tally(cap) == ["role_association"]
    result = _reconcile(cap, ledger)
    assert result["rollback_pending"] is None, result["rollback_pending"]


def test_a_gate_counts_only_scored_outcomes_as_linked(ledger):
    cap = _register(ledger)
    cap["gate_reason"] = "fixture gate"
    cap["gate_criteria"] = {"min_linked_outcomes": 2}
    rows = [_row("success")] + [_row("learning_excluded", subject=f"o/r#{n}") for n in range(5)]
    cap["causal_evidence"] = {"readiness": capabilities._causal_readiness(cap, rows, NOW + 30)}
    gate = capabilities.gate_readiness(cap, now=NOW + 30)
    assert gate["criteria"]["linked_outcomes"] is False, gate
