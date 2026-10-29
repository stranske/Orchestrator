"""One closed source relation; actual process tests use an inert emitter only."""

import copy
import hashlib
import inspect
import json
import time
from pathlib import Path

import pytest
import test_closer_conservative_invitation as invitation
import test_dispatcher_controlled_data_only as controlled_fixture
from test_closer_conservative_intake import world as world

import closer_dispatcher as closer
import closer_role_qualification as roles
import closer_source_installation as installation
import dispatcher

controlled_world = controlled_fixture.world
ORIGINAL = copy.deepcopy(next(iter(roles.ROLE_QUALIFICATIONS.values())))
ORIGINAL_SHA = "837713af72c5a701bd1b50757458632597aaf8f482ef2f2109811b101d402482"
INSIDE = ORIGINAL["qualified_at"] + 60
assert ORIGINAL["qualified_at"] < INSIDE < ORIGINAL["expires_at"]


def support(
    record=None, descriptor=None, *, checked_at=None, identity=None, task="closer_exception"
):
    record = copy.deepcopy(ORIGINAL) if record is None else record
    return roles.execution_source_support(
        record,
        record["profile_identity"] if identity is None else identity,
        closer._controlled_model_contract(task) if descriptor is None else descriptor,
        task_type=task,
        checked_at=INSIDE if checked_at is None else checked_at,
    )


def test_original_physical_record_remains_distinct_and_byte_exact():
    original = json.dumps(ORIGINAL, sort_keys=True)
    result = support()
    assert closer._contract_digest(ORIGINAL) == ORIGINAL_SHA
    assert result["admission"]["mode"] == "reviewed_current_source_admission"
    assert result["admission"]["observation_mode"] == "historical_physical_qualification"
    assert result["admission"]["physical_qualification_performed"] is False
    assert result["admission"]["freshness_renewed"] is False
    assert result["admission"]["authority"] is False
    assert result["admission"]["original_qualified_at"] == ORIGINAL["qualified_at"]
    assert result["admission"]["original_expires_at"] == ORIGINAL["expires_at"]
    assert {k: v for k, v in result["limits"].items() if k != "model_contract_sha256"} == {
        k: v for k, v in ORIGINAL["execution_limits"].items() if k != "model_contract_sha256"
    }
    assert (
        result["limits"]["model_contract_sha256"]
        != ORIGINAL["execution_limits"]["model_contract_sha256"]
    )
    assert json.dumps(ORIGINAL, sort_keys=True) == original


@pytest.mark.parametrize(
    "change",
    [
        "record",
        "model",
        "effort",
        "profile",
        "numeric_limit",
        "role",
        "evidence",
        "renewal",
        "source",
        "hash_claim",
        "old_as_current",
        "expired",
        "before_observed",
        "wrong_task",
    ],
)
def test_unreviewed_source_profile_or_lifetime_never_gets_admission(change):
    record = copy.deepcopy(ORIGINAL)
    descriptor = closer._controlled_model_contract("closer_exception")
    identity = copy.deepcopy(record["profile_identity"])
    checked_at, task = INSIDE, "closer_exception"
    if change == "record":
        record["qualification_id"] = "unknown"
    elif change == "model":
        identity["requested_model"] = "another"
    elif change == "effort":
        identity["reasoning_effort"] = "low"
    elif change == "profile":
        identity["profile_id"] = "another"
    elif change == "numeric_limit":
        record["execution_limits"]["timeout_seconds"] += 1
    elif change == "role":
        record["role"] = "bounded_classification"
    elif change == "evidence":
        record["evidence_refs"][0]["sha256"] = "0" * 64
    elif change == "renewal":
        record["expires_at"] += 1
    elif change == "source":
        descriptor["sources"]["adapters.py"] = "0" * 64
        descriptor["contract_sha256"] = roles._source_digest(
            {k: v for k, v in descriptor.items() if k != "contract_sha256"}
        )
    elif change == "hash_claim":
        descriptor["sources"]["adapters.py"] = "0" * 64
    elif change == "old_as_current":
        # An original physical observation is never a current production proof.
        descriptor = copy.deepcopy(record["production_model_contract"])
        expected = {
            "profile_identity": identity,
            "task_type": task,
            "production_model_contract": closer._controlled_model_contract(task),
            "limits": record["execution_limits"],
        }
        assert not roles.execution_record_matches(record, expected, checked_at=checked_at)
        return
    elif change == "expired":
        checked_at = record["expires_at"]
    elif change == "before_observed":
        checked_at = record["qualified_at"] - 1
    elif change == "wrong_task":
        task = "closer_interpret"
    with pytest.raises(ValueError):
        support(record, descriptor, identity=identity, checked_at=checked_at, task=task)


def test_mode_or_source_witness_forgery_is_rejected():
    descriptor = closer._controlled_model_contract("closer_exception")
    result = support(descriptor=descriptor)
    expected = {
        "profile_identity": ORIGINAL["profile_identity"],
        "task_type": "closer_exception",
        "production_model_contract": descriptor,
        "limits": result["limits"],
        "qualification_source_admission": result["admission"],
    }
    assert roles.execution_record_matches(ORIGINAL, expected, checked_at=INSIDE)
    for change in ("missing", "new_observation", "renewed", "contract", "limits"):
        forged = copy.deepcopy(expected)
        if change == "missing":
            forged.pop("qualification_source_admission")
        elif change == "new_observation":
            forged["qualification_source_admission"]["physical_qualification_performed"] = True
        elif change == "renewed":
            forged["qualification_source_admission"]["original_expires_at"] += 1
        elif change == "contract":
            forged["qualification_source_admission"]["execution_contract_sha256"] = "0" * 64
        else:
            forged["limits"]["max_output_tokens"] += 1
        assert not roles.execution_record_matches(ORIGINAL, forged, checked_at=INSIDE)


def test_existing_root_closure_requires_and_pins_role_policy_bytes(tmp_path):
    assert "closer_role_qualification.py" in installation._REQUIRED
    policy = inspect.getsource(installation._qualified_policy)
    writer = inspect.getsource(installation.original_writer_validation_scope)
    assert '_REQUIRED <= set(manifest["modules"])' in policy
    assert "hashlib.sha256(data).hexdigest() == expected" in policy
    assert "for name, row in installation.source_closure.items()" in writer
    assert 'scope.pins.pin(Path(row["path"]), row["sha256"])' in writer
    source = tmp_path / "closer_role_qualification.py"
    source.write_bytes(Path(roles.__file__).read_bytes())
    pins = installation._PhysicalPackagePins()
    try:
        pins.pin(source, hashlib.sha256(source.read_bytes()).hexdigest())
        pins.check()
        source.write_bytes(source.read_bytes() + b"\n# unreviewed policy mutation\n")
        with pytest.raises(ValueError):
            pins.check()
    finally:
        pins.close()


def test_current_production_native_inert_proof_and_cold_enrollment(
    world, controlled_world, monkeypatch, tmp_path
):
    """Native acquisition/outer Root/installed-adapter facts are inert fixtures.

    The original real qualification DATA is unchanged. All actual launch,
    callbacks, native receipt/proof validation, owner and cold enrollment code
    executes. There is no live provider, enrollment or native issue effect.
    """
    # Only qualification clock inputs are fixed. The actual policy validators
    # still execute; native invocation, invitation, deadline and timeout clocks
    # retain their real module clock and the original record is never renewed.
    live_time = time.time
    original_problems = roles.qualification_problems
    original_support = roles.execution_source_support
    qualification_clock_calls = []

    def problems_at_observation(profile, role, *, now=None, records=None):
        qualification_clock_calls.append("qualification")
        return original_problems(profile, role, now=INSIDE, records=records)

    def support_at_observation(*args, **kwargs):
        qualification_clock_calls.append("source_admission")
        return original_support(*args, **{**kwargs, "checked_at": INSIDE})

    monkeypatch.setattr(roles, "qualification_problems", problems_at_observation)
    monkeypatch.setattr(roles, "execution_source_support", support_at_observation)
    assert invitation.time.time is closer.time.time is dispatcher.time.time is live_time
    original_factory = invitation.InertCaller
    captured, callers = [], []

    def caller_factory(*args, **kwargs):
        caller = original_factory(*args, **kwargs)
        callers.append(caller)
        monkeypatch.setattr(
            roles, "ROLE_QUALIFICATIONS", {ORIGINAL["qualification_id"]: copy.deepcopy(ORIGINAL)}
        )
        # Explicit hardware/bootstrap fixture; proof/result validators untouched.
        monkeypatch.setattr(
            roles,
            "is_runtime_qualified",
            lambda p, r, **kw: roles.qualified_record(p, r, **kw) is not None
            and p["profile_id"] == ORIGINAL["profile_identity"]["profile_id"],
        )
        return caller

    original_offload = closer._controlled_offload

    def observed(*args, **kwargs):
        answer = original_offload(*args, **kwargs)
        captured.append(answer)
        return answer

    monkeypatch.setattr(invitation, "InertCaller", caller_factory)
    monkeypatch.setattr(closer, "_controlled_offload", observed)
    invitation.test_one_actual_inert_strong_and_cold_invitation_readback(
        world, controlled_world, monkeypatch, tmp_path, include_packet_length=False
    )
    assert len(captured) == len(callers) == len(callers[0].results) == 1
    assert {"qualification", "source_admission"} <= set(qualification_clock_calls)
    assert invitation.time.time is closer.time.time is dispatcher.time.time is live_time
    proof = captured[0]["_closer_controlled_proof"]
    expected = proof["expected"]
    assert expected["qualification_record_sha256"] == ORIGINAL_SHA
    assert expected["production_model_contract"] == closer._controlled_model_contract(
        "closer_exception"
    )
    assert (
        expected["qualification_source_admission"]["historical_contract_sha256"]
        == ORIGINAL["production_model_contract"]["contract_sha256"]
    )
    assert expected.get("qualification_only") is not True
    retained = closer._revalidate_controlled_proof(
        proof,
        run_id=proof["run_id"],
        basis=expected["basis"],
        receipt_root=dispatcher.DISPATCH_LOG_DIR,
    )
    assert retained["run_id"] == proof["run_id"]
    old = copy.deepcopy(proof)
    old["expected"]["production_model_contract"] = ORIGINAL["production_model_contract"]
    old["expected"]["limits"] = ORIGINAL["execution_limits"]
    old["expected"]["qualification_only"] = True
    with pytest.raises(closer.ControlledOriginUnavailable, match="source context differs"):
        closer._revalidate_controlled_proof(
            old,
            run_id=old["run_id"],
            basis=old["expected"]["basis"],
            receipt_root=dispatcher.DISPATCH_LOG_DIR,
        )
    assert (
        closer._contract_digest(roles.ROLE_QUALIFICATIONS[ORIGINAL["qualification_id"]])
        == ORIGINAL_SHA
    )


def test_prospective_source_audit_rejects_modified_policy_or_legacy_code():
    import closer_semantic_native_integration_support as audit

    raw = Path(roles.__file__).read_bytes()
    audit.assert_reviewed_role_registry_successor(raw)
    for before, after in (
        (b'"freshness_renewed": False', b'"freshness_renewed": True'),
        (b"def qualified_record(", b"def another_record("),
        (b'"execution_contract_sha256": "239d', b'"execution_contract_sha256": "0000'),
    ):
        assert before in raw
        with pytest.raises(AssertionError):
            audit.assert_reviewed_role_registry_successor(raw.replace(before, after, 1))
