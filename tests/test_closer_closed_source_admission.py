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
import execution_profiles

controlled_world = controlled_fixture.world
ORIGINAL = copy.deepcopy(next(iter(roles.ROLE_QUALIFICATIONS.values())))
ORIGINAL_SHA = "837713af72c5a701bd1b50757458632597aaf8f482ef2f2109811b101d402482"
INSIDE = ORIGINAL["qualified_at"] + 60
assert ORIGINAL["qualified_at"] < INSIDE < ORIGINAL["expires_at"]
HISTORICAL_DESCRIPTOR = copy.deepcopy(
    roles.ROLE_QUALIFICATIONS[
        "sonnet-current-role-qualification:c256f1adb15405e8aa82e2baab729c38f1a0edb9994c529079e99ec3d56358f2"
    ]["production_model_contract"]
)
CURRENT = copy.deepcopy(
    roles.ROLE_QUALIFICATIONS[
        "sonnet-current-role-qualification:7be42fa46b23bbf5ae9a5021e03fa7fc1aec9375ad2bd2705990f815f20962ae"
    ]
)
CURRENT_INSIDE = CURRENT["qualified_at"] + 60


def support(
    record=None, descriptor=None, *, checked_at=None, identity=None, task="closer_exception"
):
    record = copy.deepcopy(ORIGINAL) if record is None else record
    return roles.execution_source_support(
        record,
        record["profile_identity"] if identity is None else identity,
        HISTORICAL_DESCRIPTOR if descriptor is None else descriptor,
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
    # Start with the reviewed pair: the unrenewed live contract would reject
    # every case before exercising its specific record/source mutation.
    descriptor = copy.deepcopy(HISTORICAL_DESCRIPTOR)
    identity = copy.deepcopy(record["profile_identity"])
    checked_at, task = INSIDE, "closer_exception"
    baseline = support(record, descriptor, identity=identity, checked_at=checked_at, task=task)
    assert baseline["admission"]["mode"] == "reviewed_current_source_admission"
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
        # Each changed execution source must independently invalidate admission,
        # including the router/dispatcher edits that require the #1039 renewal.
        for source in ("adapters.py", "router.py", "dispatcher.py"):
            changed = copy.deepcopy(descriptor)
            changed["sources"][source] = "0" * 64
            changed["contract_sha256"] = roles._source_digest(
                {k: v for k, v in changed.items() if k != "contract_sha256"}
            )
            with pytest.raises(ValueError, match="does not support this exact production source"):
                support(record, changed, identity=identity, checked_at=checked_at, task=task)
        assert descriptor == HISTORICAL_DESCRIPTOR
        assert record == ORIGINAL
        return
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
    descriptor = HISTORICAL_DESCRIPTOR
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


def test_historical_current_record_cannot_authorize_unreviewed_live_contract():
    """The #1039 predecessor remains historical until its successor is reviewed."""
    live = closer._controlled_model_contract("closer_exception")
    profile = execution_profiles.get_profile(CURRENT["profile_identity"]["profile_id"])
    assert (
        roles.qualified_record(
            profile,
            roles.STRONG_CRITERION_ADJUDICATION,
            now=CURRENT_INSIDE,
            records={CURRENT["qualification_id"]: CURRENT},
        )
        == CURRENT
    )
    assert CURRENT["production_model_contract"] != live
    with pytest.raises(ValueError, match="does not support this exact production source"):
        roles.execution_source_support(
            CURRENT,
            CURRENT["profile_identity"],
            live,
            task_type="closer_exception",
            checked_at=CURRENT_INSIDE,
        )
    expected = {
        "profile_identity": CURRENT["profile_identity"],
        "task_type": "closer_exception",
        "production_model_contract": live,
        "limits": {
            **CURRENT["execution_limits"],
            "model_contract_sha256": live["contract_sha256"],
        },
    }
    assert not roles.execution_record_matches(CURRENT, expected, checked_at=CURRENT_INSIDE)
    assert closer._contract_digest(ORIGINAL) == ORIGINAL_SHA


@pytest.mark.parametrize("qualification_id", sorted(roles.ROLE_QUALIFICATIONS))
def test_historical_qualification_refuses_live_launch_before_offload(qualification_id, monkeypatch):
    """A source mismatch must block every historical row while it is otherwise eligible."""
    record = copy.deepcopy(roles.ROLE_QUALIFICATIONS[qualification_id])
    descriptor = closer._controlled_model_contract("closer_exception")
    if record["production_model_contract"] == descriptor:
        # A reviewed successor is exercised by the native proof test below.
        return
    profile = execution_profiles.get_profile(record["profile_identity"]["profile_id"])
    checked_at = record["qualified_at"] + 1
    original_record = roles.qualified_record
    original_support = roles.execution_source_support
    source_checks = []

    def historical_record(profile, role):
        return original_record(profile, role, now=checked_at, records={qualification_id: record})

    def historical_support(*args, **kwargs):
        source_checks.append(args[2])
        return original_support(*args, **{**kwargs, "checked_at": checked_at})

    assert historical_record(profile, roles.STRONG_CRITERION_ADJUDICATION) == record
    monkeypatch.setattr(roles, "qualified_record", historical_record)
    monkeypatch.setattr(roles, "execution_source_support", historical_support)
    # Installed runtime eligibility is a fixture; source admission stays real.
    monkeypatch.setattr(roles, "is_runtime_qualified", lambda *_args: True)

    def forbidden_offload(*_args, **_kwargs):
        pytest.fail("historical source qualification reached the broker")

    launch = {
        "profile_id": profile["profile_id"],
        "task_type": "closer_exception",
        "operation_id": "historical-source-refusal",
        "frontier": {"frontier_id": "historical-source-refusal"},
        "target": "offline/repo#1",
        "prompt": "bounded inert request",
    }
    with pytest.raises(
        closer.ControlledOriginUnavailable,
        match="reviewed production execution contract unavailable",
    ):
        closer._controlled_launch_options(**launch)
    result = closer._controlled_offload(
        agent="claude",
        **launch,
        basis={},
        deadline=time.time() + 10,
        offload=forbidden_offload,
    )
    assert result == closer._controlled_prestart_refusal()
    assert source_checks == [descriptor, descriptor]
    assert roles.ROLE_QUALIFICATIONS[qualification_id] == record


def test_current_production_requires_reviewed_source_before_native_proof_and_enrollment(
    world,
    controlled_world,
    monkeypatch,
    tmp_path,
):
    """Native acquisition/outer Root/installed-adapter facts are inert fixtures.

    Pending renewal must refuse before dispatch. With a reviewed successor,
    actual launch, callbacks, native proof validation and cold enrollment run
    against inert transport. Qualification data stays unchanged in both cases.
    There is no live provider, enrollment or native issue effect.
    """
    # Exercise refusal while renewal is pending, then the inert success path
    # once an actual reviewed registry successor exists. Neither path renews it.
    descriptor = closer._controlled_model_contract("closer_exception")
    profile = execution_profiles.get_profile(CURRENT["profile_identity"]["profile_id"])
    observations = [
        record["qualified_at"]
        for record in roles.ROLE_QUALIFICATIONS.values()
        if record.get("production_model_contract") == descriptor
        and record.get("profile_identity") == roles.profile_identity(profile)
    ]
    if not observations:
        monkeypatch.setattr(
            roles, "ROLE_QUALIFICATIONS", {CURRENT["qualification_id"]: copy.deepcopy(CURRENT)}
        )
    checked_at = max(observations) + 1 if observations else CURRENT_INSIDE
    current = roles.qualified_record(profile, roles.STRONG_CRITERION_ADJUDICATION, now=checked_at)
    assert current is not None
    assert (current["production_model_contract"] == descriptor) is bool(observations)

    # Only qualification clock inputs are fixed. The actual policy validators
    # still execute; native invocation, invitation, deadline and timeout clocks
    # retain their real module clock and the original record is never renewed.
    live_time = time.time
    original_problems = roles.qualification_problems
    original_support = roles.execution_source_support
    qualification_clock_calls = []

    def problems_at_observation(profile, role, *, now=None, records=None):
        qualification_clock_calls.append("qualification")
        return original_problems(profile, role, now=checked_at, records=records)

    def support_at_observation(*args, **kwargs):
        qualification_clock_calls.append("source_admission")
        return original_support(*args, **{**kwargs, "checked_at": checked_at})

    monkeypatch.setattr(roles, "qualification_problems", problems_at_observation)
    monkeypatch.setattr(roles, "execution_source_support", support_at_observation)
    original_record = roles.qualified_record

    def record_at_observation(profile, role, **kwargs):
        kwargs.pop("now", None)
        return original_record(profile, role, now=checked_at, **kwargs)

    monkeypatch.setattr(roles, "qualified_record", record_at_observation)
    monkeypatch.setattr(
        roles, "ROLE_QUALIFICATIONS", {current["qualification_id"]: copy.deepcopy(current)}
    )
    assert invitation.time.time is closer.time.time is dispatcher.time.time is live_time
    if not observations:
        # Supply only the runtime fixture: the real source-admission validator
        # must reject the otherwise eligible, still-in-lifetime predecessor.
        monkeypatch.setattr(
            roles,
            "is_runtime_qualified",
            lambda p, r: roles.qualified_record(p, r) == current,
        )
        launch = {
            "profile_id": profile["profile_id"],
            "task_type": "closer_exception",
            "operation_id": "pending-reviewed-source-renewal",
            "frontier": {"frontier_id": "pending-renewal"},
            "target": "offline/repo#1",
            "prompt": "bounded inert request",
        }
        with pytest.raises(
            closer.ControlledOriginUnavailable,
            match="reviewed production execution contract unavailable",
        ):
            closer._controlled_launch_options(**launch)

        def forbidden_offload(*_args, **_kwargs):
            pytest.fail("unreviewed production source reached the broker")

        result = closer._controlled_offload(
            agent="claude",
            **launch,
            basis={},
            deadline=live_time() + 10,
            offload=forbidden_offload,
        )
        assert result == closer._controlled_prestart_refusal()
        assert {"qualification", "source_admission"} <= set(qualification_clock_calls)
        assert roles.ROLE_QUALIFICATIONS == {current["qualification_id"]: CURRENT}
        assert invitation.time.time is closer.time.time is dispatcher.time.time is live_time
        assert not controlled_world.capture.exists()
        assert controlled_world.events["auth"] == 0
        assert controlled_world.events["started"] == controlled_world.events["finished"] == []
        assert closer._contract_digest(ORIGINAL) == ORIGINAL_SHA
        return

    original_factory = invitation.InertCaller
    captured, callers = [], []

    def caller_factory(*args, **kwargs):
        caller = original_factory(*args, **kwargs)
        callers.append(caller)
        monkeypatch.setattr(
            roles, "ROLE_QUALIFICATIONS", {current["qualification_id"]: copy.deepcopy(current)}
        )
        # Explicit hardware/bootstrap fixture; proof/result validators untouched.
        monkeypatch.setattr(
            roles,
            "is_runtime_qualified",
            lambda p, r, **kw: roles.qualified_record(p, r, **kw) is not None
            and p["profile_id"] == current["profile_identity"]["profile_id"],
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
    assert expected["qualification_record_sha256"] == closer._contract_digest(current)
    assert expected["production_model_contract"] == closer._controlled_model_contract(
        "closer_exception"
    )
    assert "qualification_source_admission" not in expected
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
    assert closer._contract_digest(ORIGINAL) == ORIGINAL_SHA
    assert closer._contract_digest(roles.ROLE_QUALIFICATIONS[current["qualification_id"]]) == (
        closer._contract_digest(current)
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
