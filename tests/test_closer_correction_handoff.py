"""Pure wire parsing and causal identity checks; no corrective effects exist here."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

import closer_correction_handoff as contract

FIXTURE = Path(__file__).parent / "fixtures/closer_correction/request.json"
REQUEST_SHA = "68998ab61d0d0540fca0ac37205e2d68a0be4812c7098d40346181238f236490"


@pytest.fixture
def req():
    return contract.parse_request(FIXTURE.read_bytes())


def seal(row, field="request_sha256"):
    row[field] = contract.digest({k: v for k, v in row.items() if k != field})
    return json.dumps(row)


def selector(req):
    v = req["original_verifier"]
    return {
        "schema": contract.SELECTOR_SCHEMA,
        "run_id": int(v["run_id"]),
        "run_attempt": int(v["run_attempt"]),
        "artifact_id": 910,
        "request_sha256": req["request_sha256"],
        "verification_event": copy.deepcopy(v["verification_event"]),
        "assessment_identity": {
            "mode": "compare",
            "provider": "",
            "model": "model",
            "model2": "model",
            "canonical": True,
        },
    }


def receipt(req, state=contract.ACCEPTED):
    issue = copy.deepcopy(req["incumbent"].get("issue"))
    return {
        "schema": contract.RECEIPT_SCHEMA,
        "request_sha256": req["request_sha256"],
        "request": copy.deepcopy(req),
        "consumer": {"run_id": "201", "run_attempt": "1"},
        "effect_state": state,
        "reason": "observed exact execution",
        "issue": issue,
        "continuation": (
            {
                "workflow": "agents-auto-pilot.yml",
                "run_id": "202",
                "run_attempt": "3",
                "request_sha256": req["request_sha256"],
                "issue_node_id": issue["node_id"],
            }
            if state == contract.ACCEPTED
            else None
        ),
        "retry_authorized": False,
        "act_authorized": False,
        "source_closure_authorized": False,
    }


def ownership(req, kind="existing"):
    evidence = req["finding"]["evidence"]
    scope = {
        "target": req["target"],
        "source_issue": req["source_issue"],
        "finding_evidence": evidence,
        "scope": req["scope"],
    }
    effect = {
        "schema": "closer-correction-effect/v1",
        "repository": req["target"]["repository"],
        "event_id": req["original_verifier"]["verification_event"]["event_id"],
        "finding_evidence": evidence,
        "source_issue": req["source_issue"],
        "scope": req["scope"],
    }
    return {
        "schema": "closer-correction-ownership/v1",
        "kind": kind,
        "seed_effect_key": contract.digest(effect),
        "source_scope_digest": contract.digest(scope),
        "observed_at": "2030-01-01T00:00:00Z",
        "evidence_refs": ["inventory.json#/ownership"],
        **({"incumbent": copy.deepcopy(req["incumbent"])} if kind == "existing" else {}),
    }


def set_path(row, path, value):
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value


def test_committed_workflows_fixture_bytes_and_canonical_digest(req):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "e1d6a9383f2649505d9e964a37d096a300bab61ef37939cd126886f6b3d690c1"
    )
    assert req["request_sha256"] == REQUEST_SHA
    assert contract.parse_request(json.dumps(req), expected_sha256=REQUEST_SHA) == req
    assert contract.validate_request(req) is req
    with pytest.raises(ValueError, match="identity mismatch"):
        contract.parse_request(json.dumps(req), expected_sha256="f" * 64)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"x","schema":"y"}',
        b'{"x":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e309}',
        b'{"x":1.0}',
        b'{"x":9007199254740992}',
        b'{"x":1',
        b"{} {}",
        b"[]",
        b"null",
        b"\xff",
        b'{"x":"\\ud800"}',
        b" " * (contract.MAX_BYTES + 1),
        b'{"x":' + b"[" * 20 + b"0" + b"]" * 20 + b"}",
        b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    ],
)
def test_malformed_duplicate_partial_nonfinite_overflow_is_rejected(raw):
    with pytest.raises(ValueError):
        contract.parse_request(raw)


@pytest.mark.parametrize(
    "path,value",
    [
        (("extra",), "ignored"),
        (("schema",), "closer-correction-request/v2"),
        (("scope",), "mechanical_repair"),
        (("target", "pr"), True),
        (("target", "pr"), 7),
        (("target", "pr"), "07"),
        (("target", "repository"), "Stranske/example"),
        (("target", "head_sha"), "f" * 40),
        (("target", "merge_sha"), "f" * 40),
        (("source_issue", "number"), False),
        (("original_verifier", "run_id"), 100),
        (("original_verifier", "run_attempt"), "0"),
        (("original_verifier", "comment_id"), "1" * 17),
        (("finding", "id"), ""),
        (("finding", "evidence", "ref"), "x" * 4097),
        (("required_tests",), []),
        (("required_tests", 0, "finding_id"), "different-finding"),
        (("required_tests", 0, "source_evidence", "sha256"), "f" * 64),
        (("required_tests", 0, "source_evidence", "ref"), "other.json#/finding"),
        (("required_tests", 0, "command"), "pytest -q"),
        (("incumbent",), {"kind": "unknown"}),
        (("incumbent",), {"kind": "no_incumbent", "owner": "owner"}),
        (("original_operation", "attempt_budget"), True),
        (("original_operation", "attempt_budget"), 0),
        (("original_operation", "attempt_budget"), 11),
        (("original_operation", "deadline"), "tomorrow"),
        (("original_operation", "deadline"), "2030-01-01T00:00:00"),
        (("original_operation", "authenticated"), True),
    ],
)
def test_rehashed_request_cannot_hide_invalid_fields_or_bindings(req, path, value):
    set_path(req, path, value)
    with pytest.raises(ValueError):
        contract.parse_request(seal(req))


def test_digest_and_required_test_list_bounds(req):
    req["request_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="digest"):
        contract.parse_request(json.dumps(req))
    req["required_tests"] *= 2
    with pytest.raises(ValueError, match="Duplicate required-test"):
        contract.parse_request(seal(req))
    req["required_tests"] *= 11
    with pytest.raises(ValueError, match="required tests"):
        contract.parse_request(seal(req))


def test_declared_no_incumbent_and_test_selector_are_only_data(req):
    req["incumbent"] = {"kind": "no_incumbent"}
    req["required_tests"] = [
        {
            "kind": "existing_selector",
            "id": "test-1",
            "finding_id": req["finding"]["id"],
            "source_ref": "main",
            "path": "tests/test_contract.py",
            "selector": "test_no_effect",
            "file_sha256": "e" * 64,
        }
    ]
    parsed = contract.parse_request(seal(req))
    assert parsed["incumbent"] == {"kind": "no_incumbent"}
    assert not any("authoriz" in key for key in parsed)
    for path in ("/absolute.py", "tests/../outside.py"):
        parsed["required_tests"][0]["path"] = path
        with pytest.raises(ValueError):
            contract.parse_request(seal(parsed))


@pytest.mark.parametrize(
    "path,value",
    [
        (("run_id",), 101),
        (("run_attempt",), 3),
        (("run_id",), "100"),
        (("artifact_id",), True),
        (("artifact_id",), 0),
        (("artifact_id",), 2**53),
        (("request_sha256",), "f" * 64),
        (("verification_event", "event_id"), "f" * 64),
        (("assessment_identity", "canonical"), False),
        (("assessment_identity", "provider"), "injected-provider"),
        (("assessment_identity", "tools"), []),
        (("extra",), "unknown"),
    ],
)
def test_selector_requires_exact_current_request_event_and_numeric_transport_ids(req, path, value):
    row = selector(req)
    set_path(row, path, value)
    with pytest.raises(ValueError):
        contract.parse_selector(json.dumps(row), request=req)


def test_selector_validity_supplies_no_authentication(req):
    row = selector(req)
    assert contract.parse_selector(json.dumps(row), request=req) == row
    row["verification_event"]["material_sha256"] = "f" * 64
    row["verification_event"]["event_id"] = contract.verification_events.event_id(
        row["verification_event"]
    )
    with pytest.raises(ValueError, match="Stale"):
        contract.parse_selector(json.dumps(row), request=req)


@pytest.mark.parametrize("state", [contract.ACCEPTED, "confirmed_not_started", "outcome_unknown"])
def test_each_receipt_outcome_is_parseable_and_never_grants_authority(req, state):
    row = receipt(req, state)
    row["ownership"] = ownership(req)
    parsed = contract.parse_receipt(seal(row, "receipt_sha256"), request=req)
    assert parsed["effect_state"] == state
    assert all(
        parsed[key] is False
        for key in ("retry_authorized", "act_authorized", "source_closure_authorized")
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema",), "closer-correction-receipt/v2"),
        (("request_sha256",), "f" * 64),
        (("effect_state",), "accepted"),
        (("retry_authorized",), True),
        (("act_authorized",), 0),
        (("source_closure_authorized",), None),
        (("issue", "node_id"), "I_another"),
        (("issue", "body_sha256"), "f" * 64),
        (("issue", "number"), "9"),
        (("issue",), None),
        (("consumer", "run_id"), 201),
        (("consumer", "run_attempt"), True),
        (("continuation",), None),
        (("continuation", "workflow"), "agents-verifier.yml"),
        (("continuation", "request_sha256"), "f" * 64),
        (("continuation", "issue_node_id"), "I_other"),
        (("continuation", "run_attempt"), 3),
        (("continuation", "authenticated"), True),
        (("reason",), ""),
        (("extra",), "unknown"),
    ],
)
def test_rehashed_receipt_rejects_wrong_identity_partial_acceptance_and_grants(req, path, value):
    row = receipt(req)
    set_path(row, path, value)
    with pytest.raises(ValueError):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


@pytest.mark.parametrize("state", ["confirmed_not_started", "outcome_unknown"])
def test_nonaccepted_cannot_claim_a_continuation(req, state):
    row = receipt(req)
    row["effect_state"] = state
    with pytest.raises(ValueError, match="Nonaccepted"):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


@pytest.mark.parametrize(
    "path,value",
    [
        (("kind",), "absent"),
        (("seed_effect_key",), "f" * 64),
        (("source_scope_digest",), "f" * 64),
        (("observed_at",), "invalid"),
        (("evidence_refs",), []),
        (("evidence_refs",), [True]),
        (("incumbent", "owner"), "different-owner"),
        (("authenticated",), True),
    ],
)
def test_rehashed_ownership_cannot_rebind_scope_or_owner(req, path, value):
    row = receipt(req)
    row["ownership"] = ownership(req)
    set_path(row["ownership"], path, value)
    with pytest.raises(ValueError):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


def test_no_incumbent_receipt_requires_observed_shape_for_the_created_issue(req):
    req["incumbent"] = {"kind": "no_incumbent"}
    req = contract.parse_request(seal(req))
    row = receipt(req, "outcome_unknown")
    row["effect_state"] = contract.ACCEPTED
    row["issue"] = {"number": "9", "node_id": "I_9", "body_sha256": "f" * 64}
    row["continuation"] = {
        "workflow": "agents-auto-pilot.yml",
        "run_id": "202",
        "run_attempt": "1",
        "request_sha256": req["request_sha256"],
        "issue_node_id": "I_9",
    }
    with pytest.raises(ValueError, match="ownership"):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)
    row["ownership"] = ownership(req, "no_incumbent")
    assert contract.parse_receipt(seal(row, "receipt_sha256"), request=req)["issue"] == row["issue"]
    row["ownership"]["incumbent"] = {"owner": "injected"}
    with pytest.raises(ValueError, match="ownership fields"):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


def test_stale_nested_request_is_rejected_even_with_both_hashes_recomputed(req):
    row = receipt(req)
    row["request"]["original_operation"]["operation_id"] = "different-original-request"
    seal(row["request"])
    row["request_sha256"] = row["request"]["request_sha256"]
    with pytest.raises(ValueError, match="exact request"):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


def test_accepted_receipt_matches_actual_workflows_js_golden_digest(req):
    # Produced offline with the actual Workflows receipt() implementation,
    # not by a second Python implementation of this parser's hash formula.
    row = receipt(req)
    row["reason"] = "cross-language-golden"
    parsed = contract.parse_receipt(seal(row, "receipt_sha256"), request=req)
    assert parsed["receipt_sha256"] == (
        "912f31a942cdfec3ad25938d8865ea9efdf81b9933336d466e89df67cae6954b"
    )


@pytest.mark.parametrize(
    "part", ["target", "original_verifier", "finding", "incumbent", "original_operation"]
)
@pytest.mark.parametrize("malformed", [None, [], True, "partial"])
def test_rehashed_request_nested_nonobjects_fail_as_value_errors(req, part, malformed):
    req[part] = malformed
    with pytest.raises(ValueError):
        contract.parse_request(seal(req))


@pytest.mark.parametrize("part", ["consumer", "continuation", "ownership"])
@pytest.mark.parametrize("malformed", [None, [], True, "partial"])
def test_rehashed_receipt_nested_nonobjects_fail_as_value_errors(req, part, malformed):
    row = receipt(req)
    row[part] = malformed
    with pytest.raises(ValueError):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


def test_unknown_ownership_is_observation_data_and_cannot_claim_an_incumbent(req):
    row = receipt(req, "outcome_unknown")
    row["issue"] = None
    row["ownership"] = ownership(req, "unknown")
    assert contract.parse_receipt(seal(row, "receipt_sha256"), request=req)["continuation"] is None
    row["ownership"]["incumbent"] = req["incumbent"]
    with pytest.raises(ValueError, match="ownership fields"):
        contract.parse_receipt(seal(row, "receipt_sha256"), request=req)


def test_pure_parsers_do_not_read_files_or_launch_work(req, monkeypatch):
    import builtins
    import subprocess

    request_data = json.dumps(req)
    selector_data = json.dumps(selector(req))
    receipt_data = seal(receipt(req), "receipt_sha256")
    before = copy.deepcopy(req)

    def forbidden(*args, **kwargs):
        pytest.fail("Pure contract attempted IO/execution")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    contract.parse_request(request_data)
    contract.parse_selector(selector_data, request=req)
    contract.parse_receipt(receipt_data, request=req)
    assert req == before


def test_canonical_js_domain_preserves_unicode_string_ids_and_utf16_key_sort():
    assert contract.canonical_json({"z": "100", "a": "é😀\n", "budget": 1}) == (
        '{"a":"é😀\\n","budget":1,"z":"100"}'
    )
    assert contract.canonical_json({"\ue000": 1, "😀": 2}) == '{"😀":2,"\ue000":1}'
    for value in (float("inf"), float("nan"), 1.0, 2**53, {"x": [0] * 65}):
        with pytest.raises(ValueError):
            contract.canonical_json(value)
