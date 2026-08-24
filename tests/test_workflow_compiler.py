from __future__ import annotations

import copy
from pathlib import Path

import pytest

import capabilities
import capability_compiler as compiler


@pytest.fixture
def valid_dag() -> dict:
    return compiler.reference_workflow_source()


def test_deterministic_sequence_compiles_to_idempotent_dag(valid_dag: dict, tmp_path: Path) -> None:
    source_keys = [step["idempotency_key"] for step in valid_dag["steps"]]
    assert len(set(source_keys)) == len(source_keys), "duplicate idempotency key"
    plan_one = compiler.compile_workflow_rail(valid_dag)
    plan_two = compiler.compile_workflow_rail(copy.deepcopy(valid_dag))
    assert plan_one == plan_two
    assert [step["id"] for step in plan_one["steps"]] == [
        "sync",
        "consumer-drift",
        "hygiene",
        "test-gate",
    ]
    assert len({step["idempotency_key"] for step in plan_one["steps"]}) == len(
        plan_one["steps"]
    ), "duplicate idempotency key"
    assert plan_one["execution_policy"] == {
        "executable": False,
        "mode": "shadow_dry_run",
        "allow_arbitrary_shell": False,
        "side_effects_permitted": False,
    }

    ledger = tmp_path / "capabilities.json"
    first = compiler.run_reference_workflow(ledger_path=ledger)
    second = compiler.run_reference_workflow(ledger_path=ledger)
    assert first == second
    assert first["result"]["side_effects"] == []
    assert all(not row["side_effect_recorded"] for row in first["result"]["steps"])

    cap = capabilities.load(ledger, create=False)[plan_one["capability_id"]]
    event_types = [event["type"] for event in cap["event_history"]]
    for event_type in ("match", "invocation", "output", "consumer", "success", "outcome"):
        assert event_types.count(event_type) == 1
    assert cap["status"] == "shadow"
    assert cap["expiry"] == valid_dag["expires_at"]
    assert cap["kill_switch"] == valid_dag["kill_switch"]
    assert cap["rollback"]["steps"] == plan_one["rollback_order"]
    assert cap["outcome_links"] == [f"shadow:{first['consumer_receipt']['receipt_id']}"]
    assert all(
        (cap["activation_evidence"].get(probe) or {}).get("passed")
        for probe in capabilities.ACTIVE_PROBES
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda dag: dag["steps"][0].update(depends_on=["test-gate"]), "cycle detected"),
        (lambda dag: dag["steps"][2].pop("rollback"), "missing rollback: hygiene"),
        (
            lambda dag: dag["steps"][2].update(entrypoint="shell.run"),
            "unallowlisted command: shell.run",
        ),
        (
            lambda dag: dag["steps"][2].update(requires_judgment=True),
            "ambiguous judgment: workflow step",
        ),
    ],
)
def test_unsafe_workflow_fixtures_are_rejected(valid_dag: dict, mutation, reason: str) -> None:
    mutation(valid_dag)
    with pytest.raises(compiler.WorkflowCompileError) as caught:
        compiler.compile_workflow_rail(valid_dag)
    assert reason in caught.value.reasons


def test_unallowlisted_action_becomes_rejection_not_executable_proposal(valid_dag: dict) -> None:
    valid_dag["steps"][0]["entrypoint"] = "git arbitrary-command"
    with pytest.raises(compiler.WorkflowCompileError) as caught:
        compiler.compile_workflow_rail(valid_dag)
    assert "unallowlisted command: git arbitrary-command" in caught.value.reasons
    assert "git arbitrary-command" not in compiler.ENTRYPOINTS
    proposal = compiler.compile_workflow_candidate(valid_dag)
    assert proposal["status"] == "proposal"
    assert proposal["executable"] is False and "plan" not in proposal
    assert "unallowlisted command: git arbitrary-command" in proposal["rejection_reasons"]


def test_reference_caller_records_bounded_failure_heartbeat(tmp_path: Path) -> None:
    ledger = tmp_path / "capabilities.json"

    def fail_consumer(_result: dict) -> dict:
        raise RuntimeError("raw proprietary failure detail")

    with pytest.raises(RuntimeError, match="raw proprietary"):
        compiler.run_reference_workflow(ledger_path=ledger, consumer=fail_consumer)
    cap = capabilities.load(ledger, create=False)["capability:reference-sync-hygiene-test-gate"]
    failures = [event for event in cap["event_history"] if event["type"] == "failure"]
    assert len(failures) == 1
    assert failures[0]["ref"].startswith("sha256:")
    assert "proprietary" not in str(failures[0])


def test_barrier_must_be_encoded_in_dependency_dag(valid_dag: dict) -> None:
    valid_dag["steps"][3]["depends_on"] = []
    with pytest.raises(compiler.WorkflowCompileError) as caught:
        compiler.compile_workflow_rail(valid_dag)
    assert "barrier is not represented in DAG: hygiene-before-tests" in caught.value.reasons


def test_existing_shadow_capability_adopts_extended_plan(tmp_path: Path) -> None:
    ledger = tmp_path / "capabilities.json"
    first = compiler.reference_workflow_source()
    first["steps"] = [step for step in first["steps"] if step["id"] != "consumer-drift"]
    first["steps"][1]["depends_on"] = ["sync"]
    first["barriers"] = [
        barrier for barrier in first["barriers"] if "consumer-drift" not in barrier["id"]
    ]
    old_plan = compiler.compile_workflow_rail(first)
    compiler._register_shadow(old_plan, ledger)

    new_plan = compiler.compile_workflow_rail(compiler.reference_workflow_source())
    compiler._register_shadow(new_plan, ledger)

    cap = capabilities.load(ledger, create=False)[new_plan["capability_id"]]
    assert cap["activation_evidence"]["workflow_plan_id"] == new_plan["plan_id"]
    assert cap["activation_evidence"]["workflow_plan_previous_ids"] == [old_plan["plan_id"]]
    assert cap["matcher"]["value"] == ["maintenance", "consumer_sync_drift"]
