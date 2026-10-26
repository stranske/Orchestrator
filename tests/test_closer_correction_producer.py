"""Actual inert selected task; synthetic source/role/auth, no remote authority."""

import contextlib
import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from closer_inert_dispatcher_support import LOW, InertCaller
from test_closer_correction_classification import inputs as _semantic_inputs
from test_closer_correction_classification import response
from test_closer_role_qualification import record
from test_dispatcher_controlled_data_only import world as _controlled_world

import closer_correction_classification as contract
import closer_correction_decision as decisions
import closer_correction_producer as producer
import closer_dispatcher as closer
import closer_resources as resources
import closer_role_qualification as roles
import dispatcher
import execution_profiles

controlled_world = _controlled_world
semantic_inputs = _semantic_inputs


@pytest.fixture
def local(controlled_world, semantic_inputs, monkeypatch):
    world = controlled_world
    monkeypatch.setenv("HANDOFF_DIR", str(world.root / "handoff"))
    monkeypatch.setenv("HOME", str(world.root / "private-home"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(world.root / "private-state"))
    monkeypatch.setenv("ORCH_BRAIN_ROOT", str(world.root / "private-brain"))
    caller = InertCaller(world, monkeypatch)
    inputs = copy.deepcopy(semantic_inputs)
    target_row = inputs["original_request"]["target"]
    target = target_row["repository"] + "#" + target_row["pr"]
    packet = {
        "target": target,
        "head": target_row["head_sha"],
        "base": target_row["base_sha"],
        "capture_revision": "original-revision",
        "manifest": {"packet_sha256": "f" * 64},
    }
    packet_path = world.root / "evidence" / "manifests" / ("f" * 64 + ".json")
    packet_path.parent.mkdir(parents=True)
    raw = json.dumps(packet, sort_keys=True).encode()
    packet_path.write_bytes(raw)
    inputs["retained_packet_ref"] = {
        "path": str(packet_path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    basis = {
        "target": target,
        "head": target_row["head_sha"],
        "base": target_row["base_sha"],
        "revision": "original-revision",
        "packet_sha256": inputs["retained_packet_ref"]["sha256"],
    }
    frontier = {
        "frontier_id": "original-frontier",
        "bindings": {target: {"revision": basis["revision"]}},
    }
    request = contract.build_request(**inputs)
    answer = response(request)
    for row in answer["findings"]:
        row["classification"] = "ordinary_substantive"
    profile = execution_profiles.get_profile(LOW)
    descriptor = closer._controlled_model_contract(producer.TASK)
    qualified = record(profile, roles.BOUNDED_CLASSIFICATION)
    qualified.update(
        qualification_id="synthetic-correction-record",
        qualified_at=time.time() - 10,
        expires_at=time.time() + 600,
        production_model_contract=descriptor,
        execution_limits={
            **world.args["data_only_limits"],
            "role": roles.BOUNDED_CLASSIFICATION,
            "model_contract_sha256": descriptor["contract_sha256"],
        },
    )
    qualified["confinement"]["adapter_build_sha256"] = caller.binary
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {qualified["qualification_id"]: qualified})

    def broker(agent, prompt, **options):
        return caller.response({"exit": 0, "output": json.dumps(answer)}, agent, prompt, **options)

    monkeypatch.setattr(dispatcher, "offload", broker)

    @contextlib.contextmanager
    def lease():
        with resources.pool_slot(
            world.root / "pools",
            hashlib.sha256(b"claude-subscription").hexdigest(),
            1,
            deadline=time.time() + 15,
            finite_frontier=True,
            reservation={
                "reservation_id": "original-reservation",
                "frontier_id": frontier["frontier_id"],
                "target": target,
                "agent": "claude",
                "profile_id": LOW,
            },
        ):
            yield

    def run():
        return producer.produce(
            **inputs,
            profile_id=LOW,
            frontier=frontier,
            basis=basis,
            operation_id="original-correction-operation",
            deadline=time.time() + 30
        )

    return SimpleNamespace(
        world=world,
        caller=caller,
        inputs=inputs,
        basis=basis,
        frontier=frontier,
        request=request,
        answer=answer,
        qualified=qualified,
        lease=lease,
        run=run,
    )


def replay(local, result):
    return decisions.materialize_selected(
        proof=result["proof"],
        run_id=result["run_id"],
        basis=result["basis"],
        receipt_root=dispatcher.DISPATCH_LOG_DIR,
        profile_id=LOW,
        request_ref=result["request_ref"],
        **local.inputs
    )


def test_selected_task_actual_native_proof_projects_and_restarts_without_broker(local, monkeypatch):
    with local.lease():
        result = local.run()
    assert result["status"] == "materialized", json.dumps(result, indent=2)
    assert local.world.events["auth"] == local.world.events["auth_closed"] == 1
    assert len(local.caller.results) == 1
    assert result["proof"]["expected"]["task_type"] == producer.TASK
    assert (
        result["proof"]["expected"]["production_model_contract"]["model_contract"][
            "response_schema"
        ]
        == contract.RESPONSE_SCHEMA
    )
    assert (
        result["proof"]["expected"]["basis"]["request_id"]
        == local.request["classification_request_sha256"]
    )
    assert not result["remote_authenticated"] and not result["act_authorized"]
    assert not result["source_closure_authorized"]
    decision = result["decision"]
    assert decision["request_sha256"] == local.inputs["original_request"]["request_sha256"]
    assert decision["findings"] == local.answer["findings"]
    assert decision["profile"]["qualification_sha256"] == closer._contract_digest(local.qualified)
    assert Path(decision["decision_evidence_ref"]).is_file()
    assert json.loads(Path(result["decision_ref"]["path"]).read_bytes()) == decision
    original = local.world.capture.read_bytes()
    monkeypatch.setattr(dispatcher, "offload", lambda *a, **k: pytest.fail("replay called broker"))
    assert replay(local, result) == result
    assert local.world.capture.read_bytes() == original
    # Preserve the actual successful local proof for independent source review.
    # These are disposable synthetic source/role inputs, never production auth.
    (local.world.root / "local-selected-result.json").write_text(json.dumps(result, sort_keys=True))
    (local.world.root / "local-selected-inputs.json").write_text(
        json.dumps(local.inputs, sort_keys=True)
    )
    (local.world.root / "synthetic-role-record.json").write_text(
        json.dumps(local.qualified, sort_keys=True)
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_record",
        "old_low_record",
        "wrong_limits",
        "missing_pool",
        "wrong_basis",
        "wrong_request",
    ],
)
def test_unqualified_or_wrong_original_context_never_calls_broker_or_auth(
    local, monkeypatch, fault
):
    if fault == "missing_record":
        monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    elif fault == "old_low_record":
        descriptor = closer._controlled_model_contract("closer_interpret")
        local.qualified["production_model_contract"] = descriptor
        local.qualified["execution_limits"]["model_contract_sha256"] = descriptor["contract_sha256"]
    elif fault == "wrong_limits":
        local.qualified["execution_limits"]["role"] = "strong_criterion_adjudication"
    elif fault == "wrong_basis":
        local.basis["revision"] = "foreign"
    elif fault == "wrong_request":
        local.basis["request_id"] = "0" * 64
    monkeypatch.setattr(
        dispatcher, "offload", lambda *a, **k: pytest.fail("unqualified call reached broker")
    )
    if fault == "missing_pool":
        result = local.run()
    else:
        with local.lease():
            result = local.run()
    assert result["status"] == "owned_unknown", result
    assert not result["act_authorized"] and result["retry_safe"] is False
    assert local.world.events["auth"] == 0


@pytest.mark.parametrize(
    "fault",
    [
        "risk",
        "missing",
        "tests",
        "request",
        "packet",
        "ordinary_low",
        "duplicate_json",
        "native_unknown",
    ],
)
def test_genuine_transport_never_promotes_invalid_or_uncertain_semantics(local, monkeypatch, fault):
    if fault == "risk":
        local.answer["findings"][0]["classification"] = "relevant"
    elif fault == "missing":
        local.answer["findings"].pop()
    elif fault == "tests":
        local.answer["findings"][1]["required_tests_sha256"] = "0" * 64
    elif fault == "request":
        local.answer["classification_request_sha256"] = "0" * 64
    elif fault == "packet":
        local.answer["packet_sha256"] = "0" * 64

    def broker(agent, prompt, **options):
        value = json.dumps(local.answer)
        if fault == "ordinary_low":
            value = json.dumps({"classification": "relevant", "findings": []})
        elif fault == "duplicate_json":
            value = value[:-1] + ',"schema":"duplicate"}'
        return local.caller.response(
            {
                "exit": 0,
                "output": value,
                **({"outcome_uncertain": True} if fault == "native_unknown" else {}),
            },
            agent,
            prompt,
            **options
        )

    monkeypatch.setattr(dispatcher, "offload", broker)
    with local.lease():
        result = local.run()
    assert result["status"] == "owned_unknown", result
    assert result["retry_safe"] is False and not result["act_authorized"]
    assert len(local.caller.results) == 1 and local.world.events["auth"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "source_body",
        "request_file",
        "provider_file",
        "record",
        "request_id",
        "profile",
        "native_cleanup",
    ],
)
def test_original_proof_replay_tamper_stays_held_without_retry(local, monkeypatch, fault):
    with local.lease():
        result = local.run()
    assert result["status"] == "materialized", json.dumps(result, indent=2)
    if fault == "source_body":
        local.inputs["source_body"] += " changed"
    elif fault == "request_file":
        Path(result["request_ref"]["path"]).write_bytes(b"{}")
    elif fault == "provider_file":
        Path(result["proof"]["provider_result_ref"]["path"]).write_bytes(b"{}")
    elif fault == "record":
        local.qualified["expires_at"] += 1
    elif fault == "request_id":
        result["basis"]["request_id"] = "0" * 64
    elif fault == "profile":
        result["proof"]["expected"]["profile_identity"]["requested_model"] = "foreign"
    else:
        result["proof"]["observed"]["finished"]["cleanup_terminal"] = False
    monkeypatch.setattr(dispatcher, "offload", lambda *a, **k: pytest.fail("replay resubmitted"))
    held = replay(local, result)
    assert held["status"] == "owned_unknown", held
    assert held["retry_safe"] is False and not held["act_authorized"]
    assert local.world.events["auth"] == 1


@pytest.mark.parametrize("suffix", ["correction-request.json", "correction-decision.json"])
def test_retention_failure_does_not_authorize_or_retry(local, monkeypatch, suffix):
    import closer_evidence

    real = closer_evidence._retain_bytes

    def retain(root, raw, name):
        if name == suffix:
            raise OSError("injected correction retention failure")
        return real(root, raw, name)

    monkeypatch.setattr(closer_evidence, "_retain_bytes", retain)
    with local.lease():
        result = local.run()
    assert result["status"] == "owned_unknown"
    assert len(local.caller.results) == (0 if suffix == "correction-request.json" else 1)
    assert result["retry_safe"] is False


def test_redirected_custody_refuses_before_any_write_or_auth(local, monkeypatch):
    root = Path(local.inputs["retained_packet_ref"]["path"]).parent.parent
    outside = local.world.root / "unowned-output"
    outside.mkdir()
    (root / "raw").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        dispatcher, "offload", lambda *a, **k: pytest.fail("redirected custody called broker")
    )
    with local.lease():
        result = local.run()
    assert result["status"] == "owned_unknown"
    assert "redirected" in result["reason"]
    assert list(outside.iterdir()) == []
    assert local.world.events["auth"] == 0


def test_actual_generic_preflight_rejects_correction_role_mismatch_without_auth(local):
    limits = copy.deepcopy(local.qualified["execution_limits"])
    limits["role"] = roles.STRONG_CRITERION_ADJUDICATION
    args = {**local.world.args, "task_type": producer.TASK, "data_only_limits": limits}
    with local.lease():
        result = dispatcher.offload(**args)
    assert result["process_started"] is False, result
    assert result["error"] == "controlled data-only contract/auth/lifecycle admission unavailable"
    assert local.world.events["auth"] == 0
