"""Portable coverage of the generated historical STRONG predicate's role-time terms.

The retained fault audit covers signed-file integrity. These tests evaluate the
actual predicate literal against in-memory qualification records only.
"""

import ast
import copy
import hashlib
import inspect

import closer_dispatcher as dispatcher
import closer_native_source_upgrade as upgrade
import closer_role_qualification as roles

ROLE = roles.STRONG_CRITERION_ADJUDICATION
TASK_TYPE = "closer_exception"
ADMISSION_AT = 1_200.0
COMPLETION_AT = 1_800.0
EXPIRES_AT = 2_000.0


def _actual_strong_expression():
    """Extract the exact bool expression compiled by `_old_diagnostic`."""
    tree = ast.parse(inspect.getsource(upgrade._old_diagnostic))
    fragments = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "strong_as_of=bool(" in node.value
    ]
    assert len(fragments) == 1
    line = next(line for line in fragments[0].splitlines() if "strong_as_of=bool(" in line)
    statement = ast.parse(line.strip()).body[0]
    assert isinstance(statement, ast.Assign)
    return compile(ast.Expression(statement.value), "<old-diagnostic-strong-as-of>", "eval")


def _offline_qualification():
    """Build an in-memory reviewed-record fixture; never read a live clock."""
    qualification_id, source = next(
        (key, value) for key, value in roles.ROLE_QUALIFICATIONS.items() if value["role"] == ROLE
    )
    record = copy.deepcopy(source)
    record["qualified_at"] = 1_000.0
    record["expires_at"] = EXPIRES_AT
    profile = copy.deepcopy(record["profile_identity"])
    expected = {
        "task_type": TASK_TYPE,
        "profile_id": profile["profile_id"],
        "profile_identity": copy.deepcopy(record["profile_identity"]),
        "qualification_id": qualification_id,
        "qualification_record_sha256": dispatcher._contract_digest(record),
        "production_model_contract": copy.deepcopy(record["production_model_contract"]),
        "limits": copy.deepcopy(record["execution_limits"]),
        "qualification_source_admission": None,
        "operation_id": "historical-operation",
    }
    return profile, {qualification_id: record}, expected


def _records_at(profile, records):
    return (
        roles.qualified_record(profile, ROLE, now=COMPLETION_AT, records=records),
        roles.qualified_record(profile, ROLE, now=ADMISSION_AT, records=records),
    )


def _namespace(profile, expected, completion, admission):
    basis = {
        "target": "owner/repository#17",
        "revision": "r1",
        "head": "h1",
        "base": "b1",
        "packet_sha256": "p1",
    }
    return {
        "actual": {"run_id": "run-1", "exit": 0},
        "proof": {"run_id": "run-1"},
        "proved": {"run_id": "run-1", "exit": 0},
        "receipt": {"run_id": "run-1"},
        "expected": expected,
        "basis": basis,
        "prepared_basis": dict(basis),
        "source_target": basis["target"],
        "stage": {
            "requested_agent": "claude",
            "requested_profile": expected["profile_identity"]["profile_id"],
        },
        "execution": {"started_at": 1_100.0, "finished_at": 1_900.0},
        "at": COMPLETION_AT,
        "profile": profile,
        "role": ROLE,
        "record": completion,
        "admission_record": admission,
        "identity": roles.profile_identity(profile),
        "context": {"started_at": ADMISSION_AT},
        "cd": dispatcher,
        "roles": roles,
    }


def _evaluate_actual_predicate(namespace):
    globals_ = {
        "__builtins__": {"all": all, "bool": bool, "dict": dict, "isinstance": isinstance},
        **namespace,
    }
    return bool(
        eval(
            _actual_strong_expression(),
            globals_,
        )
    )


def test_generated_strong_predicate_uses_signed_admission_time_not_later_observation():
    profile, records, expected = _offline_qualification()
    completion, admission = _records_at(profile, records)
    assert _evaluate_actual_predicate(_namespace(profile, expected, completion, admission))

    # Later expiry cannot alter the signed historical admission observation.
    assert roles.qualified_record(profile, ROLE, now=9_999.0, records=records) is None
    assert roles.qualified_record(profile, ROLE, now=ADMISSION_AT, records=records) is not None


def test_generated_strong_predicate_refuses_missing_or_expired_admission_record():
    profile, records, expected = _offline_qualification()
    completion, _admission = _records_at(profile, records)
    missing_admission = roles.qualified_record(profile, ROLE, now=ADMISSION_AT, records={})
    assert missing_admission is None
    assert not _evaluate_actual_predicate(
        _namespace(profile, expected, completion, missing_admission)
    )

    qualification_id, record = next(iter(records.items()))
    record["expires_at"] = ADMISSION_AT
    completion, expired_admission = _records_at(profile, {qualification_id: record})
    assert expired_admission is None
    assert not _evaluate_actual_predicate(
        _namespace(profile, expected, completion, expired_admission)
    )


def test_generated_strong_predicate_refuses_identity_id_and_digest_divergence():
    profile, records, expected = _offline_qualification()
    completion, admission = _records_at(profile, records)
    assert completion is not None and admission is not None

    wrong_identity = copy.deepcopy(admission)
    wrong_identity["profile_identity"]["profile_id"] = "offline-other-profile"
    assert not _evaluate_actual_predicate(_namespace(profile, expected, completion, wrong_identity))

    wrong_id = copy.deepcopy(admission)
    wrong_id["qualification_id"] = "offline-other-qualification"
    assert not _evaluate_actual_predicate(_namespace(profile, expected, completion, wrong_id))

    wrong_digest = dict(expected)
    wrong_digest["qualification_record_sha256"] = hashlib.sha256(b"other-record").hexdigest()
    assert not _evaluate_actual_predicate(_namespace(profile, wrong_digest, completion, admission))
