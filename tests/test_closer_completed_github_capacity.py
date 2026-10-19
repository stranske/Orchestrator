"""Offline scheduling recovery; an expired read throttle never authorizes an action."""

from __future__ import annotations

import copy

import pytest
from test_closer_pipeline import FakeRoundCapacityDeferred

import claims
import closer_pipeline as pipeline

TARGET = "owner/repo#1830"
RUN = "offline-completed-capacity"


@pytest.fixture(autouse=True)
def private_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    for method in ("claim", "release", "update_metadata"):
        monkeypatch.setattr(claims, method, lambda *a, **kw: True)
    monkeypatch.setattr(claims, "holder", lambda *a, **kw: None)


def prior_round(path, *, completed=True):
    resource = {
        "github_capacity": {
            "resource": "core",
            "observed_at": 1050.0,
            "reset_at_epoch": 1150.0,
            "throttle": {
                "resource": "core",
                "state": "shed",
                "action": "defer",
                "remaining": 58,
                "limit": 60,
                "reset_in_s": 100,
                "source": "call",
                "reason": "reserve exhausted; defer to reset",
            },
        }
    }
    row = {
        "status": "deferred",
        "stage": "prepare",
        "results": {},
        "metrics": {"errors": 0, "waits": 0, "stage_seconds": {}},
        "deferred_reason": "github_capacity",
        "deferred_next_trigger": "retry_after_github_reset:1150.000",
        "resource_evidence": resource,
    }
    previous = {
        "created": 1000.0,
        "deadline": 1400.0,
        "assignment": [TARGET],
        "carry_targets": [],
        "targets": {TARGET: row},
        "invalid_stop_history": [
            {
                "schema": "closer-expired-invalid-round-recovery/v1",
                "source_run_id": "earlier-offline-round",
                "source_deadline": 900.0,
                "source_assignment": [TARGET],
                "prior_stop": {"valid": False, "reasons": ["offline earlier interruption"]},
                "targets": {},
            }
        ],
        "receipt_summary": (
            {
                "schema": "closer-pipeline-receipt/v1",
                "run_id": RUN,
                "deadline": 1400.0,
                "finished_at": 1200.0,
            }
            if completed
            else None
        ),
    }
    pipeline._save(
        path,
        {"schema": "closer-pipeline/v1", "runs": {RUN: previous}, "carry": {TARGET: row}},
    )
    return previous


def recover(path, previous):
    stop = pipeline.validate_stop(path, [TARGET], now=lambda: 1500.0)
    assert not stop["valid"], "the historical throttle cannot validate today's stop"
    return pipeline._expired_invalid_round_recovery(stop, previous, path, RUN, 1500.0)


@pytest.mark.parametrize("completed", [True, False], ids=["terminal", "interrupted"])
def test_expired_no_action_prepare_is_fresh_admission_only(tmp_path, completed):
    path = tmp_path / "pipeline.json"
    previous = prior_round(path, completed=completed)
    before = path.read_bytes()
    outcome = recover(path, previous)
    assert outcome[0] and outcome[1] == {TARGET}
    assert outcome[2:5] == (set(), set(), set())
    audit = outcome[-1]
    assert audit["prior_stop"]["valid"] is False
    assert audit["targets"][TARGET]["recovery"] == "retry_fresh"
    assert audit["targets"][TARGET]["act_result"] is None
    assert (
        audit["targets"][TARGET]["resource_evidence"]
        == previous["targets"][TARGET]["resource_evidence"]
    )
    assert path.read_bytes() == before


def test_public_restart_reprepares_without_provider_or_action_and_retains_old_invalid_stop(
    tmp_path,
):
    path = tmp_path / "pipeline.json"
    previous = prior_round(path)
    original = copy.deepcopy(previous)
    clock = [1500.0]
    prepared = []

    def prepare(target, _context):
        prepared.append(target)
        throttle = copy.deepcopy(
            original["targets"][TARGET]["resource_evidence"]["github_capacity"]
        )
        raise FakeRoundCapacityDeferred(
            resource="core",
            throttle=throttle["throttle"],
            observed_at=clock[0],
            reset_at_epoch=clock[0] + 100,
        )

    def forbidden(*_args):
        pytest.fail("historical scheduling proof cannot call a provider or action")

    for cadence in range(3):
        result = pipeline.run_pipeline(
            path,
            [TARGET],
            prepare=prepare,
            interpret=forbidden,
            exception=forbidden,
            act=forbidden,
            capacity_fn=lambda: {"io": 1, "low": 0, "exception": 0},
            deadline=clock[0] + 200,
            now=lambda: clock[0],
            round_capacity_exception=FakeRoundCapacityDeferred,
        )
        assert result.get("reason") != "prior_run_has_no_valid_stop"
        assert len(prepared) == cadence + 1
        assert result["targets"][TARGET]["results"] == {}
        assert result["targets"][TARGET]["deferred_reason"] == "github_capacity"
        current = pipeline._load(path)["runs"][result["run_id"]]
        history = pipeline._resolved_audit_history(path, current)
        original_audit = next(entry for entry in history if entry.get("source_run_id") == RUN)
        assert original_audit["prior_stop"]["valid"] is False
        assert (
            original_audit["targets"][TARGET]["resource_evidence"]
            == original["targets"][TARGET]["resource_evidence"]
        )
        clock[0] += 300


@pytest.mark.parametrize(
    "mutation",
    [
        "before_round",
        "after_round",
        "after_terminal",
        "bool_observation",
        "nan_observation",
        "reset_mismatch",
        "throttle_allow",
        "foreign_resource",
        "wrong_trigger",
        "retained_act",
        "prepared_result",
        "uncertain_provider",
        "known_action",
        "unknown_barrier",
        "summary_schema",
        "summary_run",
        "summary_deadline",
        "summary_finished",
        "unknown_stop_reason",
    ],
)
def test_completed_historical_fallback_rejects_unproved_or_action_bearing_shapes(
    tmp_path, mutation
):
    path = tmp_path / "pipeline.json"
    previous = prior_round(path)
    row = previous["targets"][TARGET]
    cap = row["resource_evidence"]["github_capacity"]
    if mutation in {
        "before_round",
        "after_round",
        "after_terminal",
        "bool_observation",
        "nan_observation",
    }:
        cap["observed_at"] = {
            "before_round": 999.0,
            "after_round": 1401.0,
            "after_terminal": 1250.0,
            "bool_observation": True,
            "nan_observation": float("nan"),
        }[mutation]
        cap["reset_at_epoch"] = cap["observed_at"] + 100
        row["deferred_next_trigger"] = f"retry_after_github_reset:{cap['reset_at_epoch']:.3f}"
    elif mutation == "reset_mismatch":
        cap["reset_at_epoch"] += 10
    elif mutation == "throttle_allow":
        cap["throttle"]["action"] = "allow"
    elif mutation == "foreign_resource":
        cap["resource"] = "graphql"
    elif mutation == "wrong_trigger":
        row["deferred_next_trigger"] = "retry_anyway"
    elif mutation == "retained_act":
        row["results"]["act"] = {"status": "completed"}
    elif mutation == "prepared_result":
        row["results"]["prepare"] = {"revision": "old-head"}
    elif mutation == "uncertain_provider":
        row["provider_outcome_uncertain"] = {"run_id": "unknown-provider"}
    elif mutation == "known_action":
        row["known_completed_action"] = True
    elif mutation == "unknown_barrier":
        row["recovery_barrier"] = {"schema": "unknown"}
    elif mutation.startswith("summary_"):
        key, value = {
            "summary_schema": ("schema", "foreign/v1"),
            "summary_run": ("run_id", "foreign-run"),
            "summary_deadline": ("deadline", 1399.0),
            "summary_finished": ("finished_at", None),
        }[mutation]
        previous["receipt_summary"][key] = value
    if mutation != "nan_observation":
        pipeline._save(path, {"schema": "closer-pipeline/v1", "runs": {RUN: previous}, "carry": {}})
    stop = pipeline.validate_stop(path, [TARGET], now=lambda: 1500.0)
    if mutation == "unknown_stop_reason":
        stop["reasons"].append("unrecognized:must-remain-blocked")
    assert not pipeline._expired_invalid_round_recovery(stop, previous, path, RUN, 1500.0)[0]


@pytest.mark.parametrize("barrier", [{}, [], "", False, 0])
def test_non_null_malformed_barrier_cannot_become_fresh_admission(tmp_path, barrier):
    path = tmp_path / "pipeline.json"
    previous = prior_round(path)
    previous["targets"][TARGET]["recovery_barrier"] = barrier
    pipeline._save(path, {"schema": "closer-pipeline/v1", "runs": {RUN: previous}, "carry": {}})
    assert not recover(path, previous)[0]
