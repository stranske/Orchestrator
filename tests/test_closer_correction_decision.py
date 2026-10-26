"""Offline contracts: synthetic semantics are not authenticated remote emission."""

import copy

import pytest

import closer_correction_classification as contract
import closer_correction_decision as material

REQUEST = {
    "schema": "closer-correction-request/v1",
    "target": {
        "repository": "stranske/example",
        "pr": "7",
        "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "base_sha": "cccccccccccccccccccccccccccccccccccccccc",
        "merge_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    },
    "source_issue": {
        "number": "1",
        "node_id": "I_1",
        "body_sha256": "e0298894893be5f7d8a0bda03a4bbac49663d1e2ae970a39f4538e6d0621093c",
    },
    "original_verifier": {
        "verification_event": {
            "schema": "verification-event/v1",
            "repo": "stranske/example",
            "pr": "7",
            "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "evaluated_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "request_sha256": "ab8075822959ae7682a46de1d76b3059ceb6bedc6a36ef0825ce6d4ccb4c8632",
            "material_sha256": "28b03646dafc58a0b76fad36d56fbe3e002528e6df0c4a60404e33420d1a6d52",
            "event_id": "25027fccdb23d4dabf2e3c01d73764a6393c4a25a3a8c90f25a4910560fd791d",
        },
        "run_id": "100",
        "run_attempt": "2",
        "comment_id": "43",
        "comment_sha256": "9fc77762c36e676f0ee660abb8dacb8bf4c7589d98bcdcfb93a8af96d509f379",
        "decision_sha256": "026aa2789761f014288b065c39bc96b598928b241471ea3708d0cb41edad4b8a",
    },
    "finding": {
        "id": "raw-b061140462bb70ac9c4a8c5a2c9864740dbe6607713d01da45f63e7f6cc4ab7d",
        "evidence": {
            "ref": "evaluation.json#/concerns/0",
            "sha256": "e98f67f881c9cbfceee52fed3c12a8d69759bd058fa47be1142e14800d7bb7bd",
        },
    },
    "required_tests": [
        {
            "kind": "required_test_spec",
            "id": "test-1",
            "finding_id": "raw-b061140462bb70ac9c4a8c5a2c9864740dbe6607713d01da45f63e7f6cc4ab7d",
            "specification": "Assert rejected input leaves the existing issue unchanged.",
            "source_evidence": {
                "ref": "evaluation.json#/concerns/0",
                "sha256": "e98f67f881c9cbfceee52fed3c12a8d69759bd058fa47be1142e14800d7bb7bd",
            },
        }
    ],
    "incumbent": {"kind": "no_incumbent"},
    "original_operation": {
        "hold_id": "hold",
        "run_id": "original-run",
        "operation_id": "operation",
        "idempotency_key": "exact-once",
        "deadline": "2030-01-01T00:00:00Z",
        "attempt_budget": 1,
        "escalation_owner": "owner",
    },
    "scope": "substantive_implementation",
    "request_sha256": "801e0230bdbe165b0d5825a66d279aba3d1f9cc63852502bfa487e8a0182e830",
}

SEED_BUNDLE = {
    "schema": "closer-correction-seed-bundle/v1",
    "verification_event": {
        "schema": "verification-event/v1",
        "repo": "stranske/example",
        "pr": "7",
        "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "evaluated_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "request_sha256": "ab8075822959ae7682a46de1d76b3059ceb6bedc6a36ef0825ce6d4ccb4c8632",
        "material_sha256": "28b03646dafc58a0b76fad36d56fbe3e002528e6df0c4a60404e33420d1a6d52",
        "event_id": "25027fccdb23d4dabf2e3c01d73764a6393c4a25a3a8c90f25a4910560fd791d",
    },
    "assessment_identity": {
        "mode": "evaluate",
        "provider": "anthropic",
        "model": "",
        "model2": "",
        "canonical": True,
    },
    "result_sha256": "785dd41164caca06a1bb6077b920f96f00bc1e5edad193258af9106d50fd3608",
    "seeds": [
        {
            "schema": "closer-correction-seed/v1",
            "target": {
                "repository": "stranske/example",
                "pr": "7",
                "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "base_sha": "cccccccccccccccccccccccccccccccccccccccc",
                "merge_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
            "source_issue": {
                "number": "1",
                "node_id": "I_1",
                "body_sha256": "e0298894893be5f7d8a0bda03a4bbac49663d1e2ae970a39f4538e6d0621093c",
            },
            "original_verifier": {
                "verification_event": {
                    "schema": "verification-event/v1",
                    "repo": "stranske/example",
                    "pr": "7",
                    "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "evaluated_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "request_sha256": "ab8075822959ae7682a46de1d76b3059ceb6bedc6a36ef0825ce6d4ccb4c8632",
                    "material_sha256": "28b03646dafc58a0b76fad36d56fbe3e002528e6df0c4a60404e33420d1a6d52",
                    "event_id": "25027fccdb23d4dabf2e3c01d73764a6393c4a25a3a8c90f25a4910560fd791d",
                },
                "run_id": "100",
                "run_attempt": "2",
                "comment_id": "43",
                "comment_sha256": "9fc77762c36e676f0ee660abb8dacb8bf4c7589d98bcdcfb93a8af96d509f379",
                "decision_sha256": "026aa2789761f014288b065c39bc96b598928b241471ea3708d0cb41edad4b8a",
            },
            "finding": {
                "id": "raw-b061140462bb70ac9c4a8c5a2c9864740dbe6607713d01da45f63e7f6cc4ab7d",
                "evidence": {
                    "ref": "evaluation.json#/concerns/0",
                    "sha256": "e98f67f881c9cbfceee52fed3c12a8d69759bd058fa47be1142e14800d7bb7bd",
                },
            },
            "scope": "substantive_implementation",
        }
    ],
    "bundle_sha256": "8fe4cbc2ef872f6199323a9cbf14f96f41c016c0aa1778421fe2fe328f76aac7",
}

DECISION = {
    "schema": "closer-correction-decision/v1",
    "seed_effect_key": "1812e7ead551bc163d66a67233122659e8df582bc8aea060299abddf655a82e2",
    "source_scope_digest": "723b518b420c342e3b501ed69d455d4152e8736661b506974101cb1641b0ffbe",
    "request_sha256": "801e0230bdbe165b0d5825a66d279aba3d1f9cc63852502bfa487e8a0182e830",
    "findings": [
        {
            "id": "raw-b061140462bb70ac9c4a8c5a2c9864740dbe6607713d01da45f63e7f6cc4ab7d",
            "evidence": {
                "ref": "evaluation.json#/concerns/0",
                "sha256": "e98f67f881c9cbfceee52fed3c12a8d69759bd058fa47be1142e14800d7bb7bd",
            },
            "classification": "ordinary_substantive",
            "required_tests_sha256": "e77dac84bda574ec71c764f98649ae2ffe77bc3da35a47f439496352e9aecfe2",
        }
    ],
    "profile": {
        "id": "inert-qualified-profile",
        "role": "closer_interpret",
        "qualification_ref": "inert:qualified-production-source",
        "qualification_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    },
    "retained_packet_ref": "inert:prepare-packet",
    "decision_evidence_ref": "inert:bounded-source-decision",
}


def inputs():
    return dict(
        original_request=copy.deepcopy(REQUEST),
        seed_bundle=copy.deepcopy(SEED_BUNDLE),
        source_body="issue 1",
        raw_findings=[
            {
                **copy.deepcopy(REQUEST["finding"]),
                "text": "Reject the exact invalid-input case — preserve the source issue.",
            }
        ],
        retained_packet_ref={
            "path": "/synthetic/manifests/packet.json",
            "sha256": "a" * 64,
            "bytes": 42,
        },
    )


def semantic():
    args = inputs()
    request = contract.build_request(**args)
    return {
        "schema": contract.RESPONSE_SCHEMA,
        "classification_request_sha256": request["classification_request_sha256"],
        "request_sha256": REQUEST["request_sha256"],
        "bundle_sha256": contract.digest(SEED_BUNDLE),
        "packet_sha256": "a" * 64,
        "findings": copy.deepcopy(DECISION["findings"]),
    }


def project(value):
    args = inputs()
    return material.project_explicit_semantics(
        semantic=value, request=contract.build_request(**args), profile=DECISION["profile"], **args
    )


def test_synthetic_selected_contract_projection_matches_frozen_consumer():
    got = project(semantic())
    assert got == {
        k: v
        for k, v in DECISION.items()
        if k not in ("retained_packet_ref", "decision_evidence_ref")
    }
    assert "decision_evidence_ref" not in got


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unknown_enum",
        "foreign_evidence",
        "foreign_tests",
        "missing_tests",
        "request",
        "bundle",
        "duplicate",
        "prose",
    ],
)
def test_explicit_semantics_refuse_incomplete_or_mismatched_source(damage):
    value = semantic()
    row = value["findings"][0]
    if damage == "missing":
        row.pop("classification")
    elif damage == "unknown_enum":
        row["classification"] = "relevant"
    elif damage == "foreign_evidence":
        row["evidence"]["sha256"] = "0" * 64
    elif damage == "foreign_tests":
        row["required_tests_sha256"] = "0" * 64
    elif damage == "missing_tests":
        row.pop("required_tests_sha256")
    elif damage == "request":
        value["request_sha256"] = "0" * 64
    elif damage == "bundle":
        value["bundle_sha256"] = "0" * 64
    elif damage == "duplicate":
        value["findings"] *= 2
    elif damage == "prose":
        row["classification"] = "ordinary because relevant"
    with pytest.raises(material.DecisionUnavailable):
        project(value)


@pytest.mark.parametrize("classification", ["unknown", "security", "authority_conflict"])
def test_risk_never_becomes_ordinary(classification):
    value = semantic()
    value["findings"][0]["classification"] = classification
    assert project(value)["findings"][0]["classification"] == classification


def test_whole_material_bound():
    value = semantic()
    value["padding"] = "x" * 65536
    with pytest.raises(material.DecisionUnavailable, match="bound"):
        project(value)


@pytest.mark.parametrize("proof", [None, {}, {"classification": "relevant"}])
def test_ordinary_low_never_opts_into_correction(proof):
    result = material.materialize(proof=proof)
    assert result["status"] == "owned_unknown"
    assert "ordinary LOW" in result["reason"]
    assert not result["act_authorized"] and not result["source_closure_authorized"]
