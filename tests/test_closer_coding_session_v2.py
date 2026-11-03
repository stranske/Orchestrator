"""Pure prospective policy tests: no session, native CLI, account, or provider."""

import copy
import dataclasses
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

import closer_coding_session_v2 as session


def budget():
    # Explicit synthetic owner numbers, never runtime defaults or native limits.
    return {
        "schema": session.BUDGET_SCHEMA,
        "max_runtime_ms": 10000,
        "max_cleanup_ms": 1000,
        "max_prompt_utf8_bytes": 20000,
        "max_captured_output_bytes": 30000,
        "max_retained_final_utf8_bytes": 12000,
        "max_retained_receipt_bytes": 40000,
        "max_outer_submissions": 1,
        "native_model_attempts": "UNKNOWN",
        "native_input_tokens": "UNKNOWN",
        "native_output_tokens": "UNKNOWN",
    }


def consumers():
    policy = {
        "schema": session.POLICY_SCHEMA,
        "consumer_id": "existing-codex-owner",
        "agent": "codex",
        "profile_id": session.PROFILE,
        "profile_sha256": "a" * 64,
        "budget": budget(),
    }
    return {
        "existing-codex-owner": {
            "runner": "existing-local-controller",
            "agent": "codex",
            "profile_id": session.PROFILE,
            "profile_sha256": "a" * 64,
            session.POLICY_FIELD: policy,
        }
    }


def test_minimum_uses_each_original_current_policy_ceiling_without_mutation():
    original, current, policy = budget(), budget(), budget()
    expected = budget()
    for index, field in enumerate(session.LIMIT_FIELDS[:-1]):
        chosen = (original, current, policy)[index % 3]
        chosen[field] = index + 1
        expected[field] = index + 1
    before = copy.deepcopy((original, current, policy))
    effective = session.minimum_budget(original, current, policy)
    assert effective == expected
    assert (original, current, policy) == before
    assert all(effective[field] == "UNKNOWN" for field in session.UNKNOWN_FIELDS)
    effective["max_runtime_ms"] = 99
    assert (original, current, policy) == before


@pytest.mark.parametrize("field", session.LIMIT_FIELDS)
@pytest.mark.parametrize("invalid", [None, True, False, 0, -1, 1.0, "1", 2**53])
def test_every_numeric_limit_must_be_explicit_positive_safe_integer(field, invalid):
    value = budget()
    value[field] = invalid
    with pytest.raises(session.SessionContractError):
        session.validate_budget(value)


@pytest.mark.parametrize("missing", ["schema", *session.LIMIT_FIELDS, *session.UNKNOWN_FIELDS])
def test_missing_fields_are_not_filled_with_defaults(missing):
    value = budget()
    value.pop(missing)
    with pytest.raises(session.SessionContractError):
        session.validate_budget(value)


@pytest.mark.parametrize(
    "extra", ["max_input_tokens", "max_output_tokens", "max_attempts", "deadline"]
)
def test_old_native_token_attempt_or_deadline_fields_cannot_be_reinterpreted(extra):
    value = {**budget(), extra: 1}
    with pytest.raises(session.SessionContractError, match="exact v2"):
        session.validate_budget(value)


@pytest.mark.parametrize("field", session.UNKNOWN_FIELDS)
@pytest.mark.parametrize("claim", [1, 0, None, True, "1", "OBSERVED", "unknown"])
def test_native_attempt_and_token_claims_cannot_be_promoted_from_outer_limits(field, claim):
    value = {**budget(), field: claim}
    with pytest.raises(session.SessionContractError, match="must remain UNKNOWN"):
        session.validate_budget(value)


@pytest.mark.parametrize("position", range(3))
def test_invalid_limit_in_any_minimum_operand_holds(position):
    values = [budget(), budget(), budget()]
    values[position]["max_outer_submissions"] = 2
    with pytest.raises(session.SessionContractError, match="one outer submission"):
        session.minimum_budget(*values)


def test_exact_integer_boundary_is_valid_but_no_old_deadline_is_a_permission():
    value = {**budget(), "max_runtime_ms": 2**53 - 1}
    assert session.validate_budget(value) == value
    with pytest.raises(session.SessionContractError):
        session.validate_budget({**value, "original_deadline": 1})


def test_snapshot_is_detached_immutable_bytes_and_binds_original_policy():
    source = consumers()
    before = copy.deepcopy(source)
    draft = session.prepare_admission_policy(source)
    assert draft.reason == "prepared_data_only_not_enrolled"
    assert type(draft.snapshot_utf8) is bytes
    snapshot = json.loads(draft.snapshot_utf8)
    assert session.validate_admission_snapshot(snapshot) == snapshot
    assert source == before
    source["existing-codex-owner"][session.POLICY_FIELD]["budget"]["max_runtime_ms"] = 1
    assert json.loads(draft.snapshot_utf8)["budget"]["max_runtime_ms"] == 10000
    with pytest.raises(dataclasses.FrozenInstanceError):
        draft.snapshot_utf8 = b"substitution"
    snapshot["budget"]["max_runtime_ms"] = 2
    with pytest.raises(session.SessionContractError, match="policy digest differs"):
        session.validate_admission_snapshot(snapshot)


@pytest.mark.parametrize("configuration", [None, [], {}, {"other": {"agent": "claude"}}])
def test_missing_configuration_holds_without_fabricating_snapshot(configuration):
    draft = session.prepare_admission_policy(configuration)
    assert draft.snapshot_utf8 is None
    assert draft.reason in {"source_consumer_config_unavailable", "v2_source_policy_absent"}


def test_ambiguous_policy_does_not_select_valid_policy_as_fallback():
    source = consumers()
    second = copy.deepcopy(source["existing-codex-owner"])
    second[session.POLICY_FIELD]["consumer_id"] = "another"
    # Even an invalid second v2 policy makes source selection ambiguous.
    second[session.POLICY_FIELD]["budget"]["max_outer_submissions"] = 2
    source["another"] = second
    before = copy.deepcopy(source)
    draft = session.prepare_admission_policy(source)
    assert draft.snapshot_utf8 is None and draft.reason == "v2_source_policy_ambiguous"
    assert source == before


@pytest.mark.parametrize(
    "fault", ["profile", "digest", "consumer", "runner", "schema", "missing", "mixed"]
)
def test_invalid_or_mixed_source_policy_holds_without_draft_or_mutation(fault):
    source = consumers()
    registered = source["existing-codex-owner"]
    policy = registered[session.POLICY_FIELD]
    if fault == "profile":
        policy["profile_id"] = "codex-6.1-sol-medium"
    elif fault == "digest":
        policy["profile_sha256"] = "b" * 64
    elif fault == "consumer":
        policy["consumer_id"] = "someone-else"
    elif fault == "runner":
        registered["runner"] = "caller-selected-runner"
    elif fault == "schema":
        policy["schema"] = "closer-coding-session-policy/v1"
    elif fault == "missing":
        policy["budget"].pop("max_retained_receipt_bytes")
    else:
        registered["policy_ceiling"] = {"max_output_tokens": 1000}
    before = copy.deepcopy(source)
    draft = session.prepare_admission_policy(source)
    assert draft.snapshot_utf8 is None and draft.reason == "v2_source_policy_invalid"
    assert source == before


def test_v1_admissions_are_never_migrated_and_mixed_budgets_hold():
    old = {
        "schema": "local-correction-admission/v1",
        "request": {"public_v1": "unchanged"},
        "original_coding_budget": {
            "max_input_tokens": 10,
            "max_output_tokens": 5,
            "max_prompt_bytes": 20,
            "max_attempts": 1,
        },
    }
    encoded = json.dumps(old, sort_keys=True).encode()
    with pytest.raises(session.SessionContractError, match="cannot be replaced or ignored"):
        session.snapshot_from_admission(old)
    draft = session.prepare_admission_policy(consumers())
    mixed = {**copy.deepcopy(old), session.ADMISSION_FIELD: json.loads(draft.snapshot_utf8)}
    with pytest.raises(session.SessionContractError, match="cannot be replaced or ignored"):
        session.snapshot_from_admission(mixed)
    assert json.dumps(old, sort_keys=True).encode() == encoded
    assert session.snapshot_from_admission(
        {"schema": old["schema"], session.ADMISSION_FIELD: json.loads(draft.snapshot_utf8)}
    ) == json.loads(draft.snapshot_utf8)
    with pytest.raises(session.SessionContractError, match="snapshot absent"):
        session.snapshot_from_admission({"schema": old["schema"]})


def test_pure_layer_does_not_read_time_files_sessions_or_call_native_ports(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("pure v2 contract attempted external/clock/source access")

    for owner, name in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (os, "open"),
        (Path, "read_bytes"),
        (Path, "read_text"),
        (socket, "socket"),
        (time, "time"),
        (time, "monotonic"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    draft = session.prepare_admission_policy(consumers())
    assert draft.snapshot_utf8 is not None
    snapshot = session.validate_admission_snapshot(json.loads(draft.snapshot_utf8))
    assert session.minimum_budget(snapshot["budget"], budget(), budget()) == budget()
    # No callable in this layer accepts a live launch, SourceInstallation,
    # account reader, old session deadline, or subprocess handle.
    assert draft.reason == "prepared_data_only_not_enrolled"


@pytest.mark.parametrize("field", ["schema", *session.UNKNOWN_FIELDS])
def test_string_subclasses_cannot_supply_exact_contract_literals(field):
    class ClaimedString(str):
        pass

    value = budget()
    value[field] = ClaimedString(value[field])
    with pytest.raises(session.SessionContractError):
        session.validate_budget(value)
