"""Synthetic, offline selected-task semantics; no producer or role authentication."""

import copy
import hashlib
import json

import pytest

import closer_correction_classification as contract


def seal(value, key):
    value[key] = contract.digest({k: v for k, v in value.items() if k != key})
    return value


@pytest.fixture
def inputs():
    body = "Keep the original issue and reject invalid input."
    target = {
        "repository": "offline/example",
        "pr": "7",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "merge_sha": "c" * 40,
    }
    source = {
        "number": "1",
        "node_id": "I_1",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    event = seal(
        {
            "schema": "verification-event/v1",
            "repo": target["repository"],
            "pr": "7",
            "head_sha": target["head_sha"],
            "evaluated_sha": target["merge_sha"],
            "request_sha256": "d" * 64,
            "material_sha256": "e" * 64,
        },
        "event_id",
    )
    verifier = {
        "verification_event": event,
        "run_id": "100",
        "run_attempt": "2",
        "comment_id": "43",
        "comment_sha256": "f" * 64,
        "decision_sha256": "1" * 64,
    }
    raw, seeds = [], []
    for index, text in enumerate(
        ("Input error overwrites the issue.", "A security boundary may be missing.")
    ):
        evidence = {
            "ref": f"evaluation.json#/concerns/{index}",
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        finding = {"id": "raw-" + contract.digest(evidence), "evidence": evidence}
        raw.append({**copy.deepcopy(finding), "text": text})
        seeds.append(
            {
                "schema": "closer-correction-seed/v1",
                "target": target,
                "source_issue": source,
                "original_verifier": verifier,
                "finding": finding,
                "scope": "substantive_implementation",
            }
        )
    finding = seeds[0]["finding"]
    original = seal(
        {
            "schema": "closer-correction-request/v1",
            "target": target,
            "source_issue": source,
            "original_verifier": verifier,
            "finding": finding,
            "required_tests": [
                {
                    "kind": "required_test_spec",
                    "id": "test-1",
                    "finding_id": finding["id"],
                    "specification": "Assert invalid input leaves the issue unchanged.",
                    "source_evidence": finding["evidence"],
                }
            ],
            "incumbent": {"kind": "no_incumbent"},
            "original_operation": {
                "hold_id": "hold",
                "run_id": "original-run",
                "operation_id": "operation",
                "idempotency_key": "once",
                "deadline": "2030-01-01T00:00:00Z",
                "attempt_budget": 1,
                "escalation_owner": "existing-owner",
            },
            "scope": "substantive_implementation",
        },
        "request_sha256",
    )
    bundle = seal(
        {
            "schema": "closer-correction-seed-bundle/v1",
            "verification_event": event,
            "assessment_identity": {
                "mode": "evaluate",
                "provider": "synthetic",
                "model": "",
                "model2": "",
                "canonical": True,
            },
            "result_sha256": "2" * 64,
            "seeds": seeds,
        },
        "bundle_sha256",
    )
    return {
        "original_request": original,
        "seed_bundle": bundle,
        "source_body": body,
        "raw_findings": raw,
        "retained_packet_ref": {
            "path": "/synthetic/retained/packet.json",
            "sha256": "3" * 64,
            "bytes": 42,
        },
    }


def response(request):
    original = request["original_request"]
    return {
        "schema": contract.RESPONSE_SCHEMA,
        "classification_request_sha256": request["classification_request_sha256"],
        "request_sha256": original["request_sha256"],
        "bundle_sha256": request["raw_seed_bundle_sha256"],
        "packet_sha256": request["retained_packet_ref"]["sha256"],
        "findings": [
            {
                "id": raw["id"],
                "evidence": copy.deepcopy(raw["evidence"]),
                "classification": "unknown",
                "required_tests_sha256": (
                    request["selected_required_tests_sha256"]
                    if raw["id"] == original["finding"]["id"]
                    else None
                ),
            }
            for raw in request["raw_findings"]
        ],
    }


def test_complete_selected_request_prompt_and_response_are_bound_not_authority(inputs):
    request = contract.build_request(**inputs)
    prompt = contract.render_prompt(request)
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload == request
    assert request["original_request"] == inputs["original_request"]
    assert request["raw_seed_bundle"] == inputs["seed_bundle"]
    assert request["raw_seed_bundle_sha256"] != inputs["seed_bundle"]["bundle_sha256"]
    assert "Every unselected row must use JSON null" in prompt
    assert inputs["source_body"] in prompt
    assert len(prompt.encode()) <= contract.MAX_BYTES
    answer = response(request)
    assert contract.validate_response(answer, request=request, **inputs) == answer
    assert answer["findings"][1]["required_tests_sha256"] is None
    assert not any(
        k in answer for k in ("act_authorized", "source_closure_authorized", "qualified")
    )


def test_canonical_compare_seed_identity_with_empty_provider_is_admitted(inputs):
    inputs["seed_bundle"]["assessment_identity"] = {
        "mode": "compare",
        "provider": "",
        "model": "gpt-5.6-terra",
        "model2": "claude-opus-5-5",
        "canonical": True,
    }
    seal(inputs["seed_bundle"], "bundle_sha256")

    request = contract.build_request(**inputs)

    assert (
        request["raw_seed_bundle"]["assessment_identity"]
        == inputs["seed_bundle"]["assessment_identity"]
    )


@pytest.mark.parametrize(
    ("mode", "provider", "model2"),
    [
        ("compare", "injected-provider", "claude-opus-5-5"),
        ("evaluate", "synthetic", "injected-model"),
    ],
)
def test_noncanonical_seed_assessment_identity_is_refused(inputs, mode, provider, model2):
    inputs["seed_bundle"]["assessment_identity"].update(mode=mode, provider=provider, model2=model2)
    seal(inputs["seed_bundle"], "bundle_sha256")

    with pytest.raises(contract.ContractError, match="seed_assessment_identity_invalid"):
        contract.build_request(**inputs)


@pytest.mark.parametrize("risk", contract.RISK_CLASSES)
def test_all_risk_classes_are_explicit_semantics_not_relevance(inputs, risk):
    request = contract.build_request(**inputs)
    answer = response(request)
    for row in answer["findings"]:
        row["classification"] = risk
    assert contract.validate_response(answer, request=request, **inputs) == answer


@pytest.mark.parametrize(
    "field", ["classification_request_sha256", "request_sha256", "bundle_sha256", "packet_sha256"]
)
def test_wrong_original_or_stage_digest_refused(inputs, field):
    request = contract.build_request(**inputs)
    answer = response(request)
    answer[field] = "0" * 64
    with pytest.raises(contract.ContractError, match="correction_response_source_changed"):
        contract.validate_response(answer, request=request, **inputs)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "foreign",
        "reordered",
        "wrong_ref",
        "wrong_risk",
        "wrong_selected_tests",
        "unselected_invented_digest",
        "unselected_invented_tests",
        "ordinary_low",
    ],
)
def test_partial_ambiguous_or_old_low_outputs_are_not_correction_semantics(inputs, mutation):
    request = contract.build_request(**inputs)
    answer = response(request)
    if mutation == "missing":
        answer["findings"].pop()
    elif mutation == "duplicate":
        answer["findings"][1] = copy.deepcopy(answer["findings"][0])
    elif mutation == "foreign":
        answer["findings"][0]["id"] = "foreign"
    elif mutation == "reordered":
        answer["findings"].reverse()
    elif mutation == "wrong_ref":
        answer["findings"][0]["evidence"]["sha256"] = "0" * 64
    elif mutation == "wrong_risk":
        answer["findings"][0]["classification"] = "relevant"
    elif mutation == "wrong_selected_tests":
        answer["findings"][0]["required_tests_sha256"] = "0" * 64
    elif mutation == "unselected_invented_digest":
        answer["findings"][1]["required_tests_sha256"] = "0" * 64
    elif mutation == "unselected_invented_tests":
        answer["findings"][1]["required_tests"] = []
    elif mutation == "ordinary_low":
        answer = {"classification": "relevant", "findings": [], "substantive_needs": []}
    with pytest.raises(contract.ContractError):
        contract.validate_response(answer, request=request, **inputs)


@pytest.mark.parametrize(
    "field",
    [
        "source_body",
        "raw_findings",
        "retained_packet_ref",
        "required_tests",
        "base",
        "event",
        "operation",
    ],
)
def test_changed_source_input_cannot_rebind_original_response(inputs, field):
    request = contract.build_request(**inputs)
    answer = response(request)
    if field == "source_body":
        inputs[field] += " changed"
    elif field == "raw_findings":
        inputs[field][0]["text"] += " changed"
    elif field == "retained_packet_ref":
        inputs[field]["sha256"] = "0" * 64
    elif field == "required_tests":
        inputs["original_request"]["required_tests"][0]["specification"] += " changed"
        seal(inputs["original_request"], "request_sha256")
    elif field == "base":
        inputs["original_request"]["target"]["base_sha"] = "0" * 40
    elif field == "event":
        inputs["seed_bundle"]["verification_event"]["event_id"] = "0" * 64
    elif field == "operation":
        inputs["original_request"]["original_operation"]["operation_id"] = "foreign"
        seal(inputs["original_request"], "request_sha256")
    with pytest.raises(contract.ContractError):
        contract.validate_response(answer, request=request, **inputs)


def test_caller_cannot_rehash_wrong_selected_tests_request(inputs):
    request = contract.build_request(**inputs)
    request["selected_required_tests_sha256"] = "0" * 64
    seal(request, "classification_request_sha256")
    with pytest.raises(contract.ContractError, match="classification_request_changed"):
        contract.render_prompt(request)
    with pytest.raises(contract.ContractError, match="classification_request_changed"):
        contract.validate_response(response(request), request=request, **inputs)


def test_original_existing_selector_shape_is_supported_without_invented_source_evidence(inputs):
    original = inputs["original_request"]
    original["required_tests"] = [
        {
            "kind": "existing_selector",
            "id": "existing",
            "finding_id": original["finding"]["id"],
            "source_ref": "retained-test-source",
            "path": "tests/test_report.py",
            "selector": "test_report",
            "file_sha256": "4" * 64,
        }
    ]
    seal(original, "request_sha256")
    request = contract.build_request(**inputs)
    answer = response(request)
    assert contract.validate_response(answer, request=request, **inputs) == answer
    assert answer["findings"][0]["required_tests_sha256"] == contract.digest(
        original["required_tests"]
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "foreign", "unsupported", "unsafe_selector", "spec_wrong_ref"],
)
def test_selected_tests_fail_closed_without_new_test_kind_or_command(inputs, mutation):
    original = inputs["original_request"]
    tests = original["required_tests"]
    if mutation == "missing":
        tests.clear()
    elif mutation == "duplicate":
        tests.append(copy.deepcopy(tests[0]))
    elif mutation == "foreign":
        tests[0]["finding_id"] = "foreign"
    elif mutation == "unsupported":
        tests[0]["kind"] = "shell_command"
    elif mutation == "unsafe_selector":
        original["required_tests"] = [
            {
                "kind": "existing_selector",
                "id": "existing",
                "finding_id": original["finding"]["id"],
                "source_ref": "retained",
                "path": "../foreign.py",
                "selector": "test_report",
                "file_sha256": "4" * 64,
            }
        ]
    elif mutation == "spec_wrong_ref":
        tests[0]["source_evidence"] = {"ref": "foreign", "sha256": "0" * 64}
    seal(original, "request_sha256")
    with pytest.raises(contract.ContractError):
        contract.build_request(**inputs)


def test_duplicate_raw_seed_and_wrong_content_remain_refused_even_if_bundle_rehashed(inputs):
    inputs["seed_bundle"]["seeds"][1] = copy.deepcopy(inputs["seed_bundle"]["seeds"][0])
    inputs["raw_findings"][1] = copy.deepcopy(inputs["raw_findings"][0])
    seal(inputs["seed_bundle"], "bundle_sha256")
    with pytest.raises(contract.ContractError, match="raw_finding_identity_duplicated"):
        contract.build_request(**inputs)


def test_explicit_bounds_and_invalid_json_do_not_admit_partial_context(inputs, monkeypatch):
    request = contract.build_request(**inputs)
    monkeypatch.setattr(contract, "MAX_BYTES", len(contract._raw(request)))
    with pytest.raises(contract.ContractError, match="correction_prompt_exceeds_bound"):
        contract.render_prompt(request)
    inputs["raw_findings"][0]["text"] = "x" * 65536
    with pytest.raises(contract.ContractError, match="exceeds_bound"):
        contract.build_request(**inputs)
    with pytest.raises(contract.ContractError, match="invalid_json"):
        contract.digest({"value": float("nan")})


def test_descriptor_is_named_separate_and_does_not_register_or_authenticate():
    descriptor = contract.contract_descriptor(source_sha256="a" * 64)
    assert descriptor["role"] == "bounded_classification"
    assert descriptor["response_schema"] == contract.RESPONSE_SCHEMA
    assert descriptor["input_authentication"] == "original_producer_host_required"
    assert descriptor["execution_origin_verified"] is False
    assert descriptor["act_authority"] is descriptor["acceptance_authority"] is False
    assert descriptor["contract_sha256"] == contract.digest(
        {k: v for k, v in descriptor.items() if k != "contract_sha256"}
    )
