"""Exact admitted proof identities are visible; semantic conclusions are not supplied."""

import copy
import hashlib
import json

import pytest
from test_closer_execution_contracts import answer, inputs
from test_closer_execution_contracts import context as _context_fixture
from test_closer_native_adjudication_receipts import native_context

import closer_execution_contracts as contracts
import closer_model_contracts as model

context = _context_fixture


def code_request(ctx):
    file = ctx["packet"]["sections"]["diff_files"]["content"]["files"][0]
    req = contracts.build_adjudication_request(
        **inputs(ctx),
        basis={"source": ctx["criterion"]["source"], "criterion": ctx["criterion"]["criterion"]},
        question="Does this code implement the named report requirement?",
        evidence_refs=[file["patch_retained"]],
    )
    return req


def render(ctx, req):
    return contracts.build_adjudication_prompt(
        ctx["original"], req, retained_dir=ctx["retained_dir"]
    )


def payload(prompt):
    return json.loads(prompt.split("\n", 1)[1])


def code_answer(ctx, req):
    response = answer(ctx, req)
    response["evidence"] = copy.deepcopy(req["evidence"])
    response["proofs"] = [
        {
            **req["proof_identities"][0]["alternatives"][0],
            "excerpt": "ship_report",
            "relevance": "The implementation defines the report shipping function.",
        }
    ]
    return response


def test_exact_codepatch_identity_and_proof_grammar_are_visible(context):
    req = code_request(context)
    prompt = render(context, req)
    data = payload(prompt)
    identity = data["adjudication_request"]["proof_identities"][0]["alternatives"][0]
    file = context["packet"]["sections"]["diff_files"]["content"]["files"][0]
    assert identity == {
        "kind": "codepatch",
        "filename": file["filename"],
        "blob_sha": file["blob_sha"],
        "base": req["base"],
        "head": req["head"],
        "evidence_sha256": req["evidence"][0]["sha256"],
    }
    assert data["prompt_contract"] == contracts.ADJUDICATION_PROMPT_VERSION
    assert (
        req["schema"]
        == model.STRONG_CONTRACT_ENTRYPOINTS["request_schema"]
        == "closer-criterion-request/v2"
    )
    assert contracts.RESPONSE_SCHEMA == "closer-criterion-response/v1"
    assert "copy EVERY field" in prompt and "one proof per cited ref in the same order" in prompt
    assert "exact substring" in prompt and "You author excerpt and relevance" in prompt
    assert "excerpt" not in identity and "relevance" not in identity and "outcome" not in identity
    assert render(context, req) == prompt
    assert len(prompt.encode("utf-8")) <= contracts.MAX_ADJUDICATION_PROMPT_BYTES
    response = code_answer(context, req)
    assert contracts.validate_adjudication_response(req, response, **inputs(context)) == response


@pytest.mark.parametrize(
    "field,value",
    [
        ("filename", "foreign.py"),
        ("blob_sha", "e" * 40),
        ("head", "f" * 40),
        ("evidence_sha256", "e" * 64),
        ("kind", "junit"),
        ("excerpt", "This text does not appear in the patch"),
        ("relevance", "short"),
    ],
)
def test_visible_identity_does_not_weaken_exact_proof_validation(context, field, value):
    req = code_request(context)
    response = code_answer(context, req)
    response["proofs"][0][field] = value
    with pytest.raises(contracts.ContractError, match="scoped_adjudication_proof_unbound"):
        contracts.validate_adjudication_response(req, response, **inputs(context))


@pytest.mark.parametrize("redigest", [False, True])
def test_caller_cannot_override_admitted_identity_even_with_new_request_digest(context, redigest):
    req = code_request(context)
    req["proof_identities"][0]["alternatives"][0]["filename"] = "foreign.py"
    if redigest:
        req["request_id"] = contracts._digest({k: v for k, v in req.items() if k != "request_id"})
    with pytest.raises(contracts.ContractError, match="adjudication_request_changed"):
        contracts.validate_adjudication_response(req, code_answer(context, req), **inputs(context))


def test_v1_request_cannot_silently_acquire_proof_identity(context):
    req = code_request(context)
    req["schema"] = "closer-criterion-request/v1"
    del req["proof_identities"]
    req["request_id"] = contracts._digest({k: v for k, v in req.items() if k != "request_id"})
    with pytest.raises(contracts.ContractError, match="adjudication_request_changed"):
        render(context, req)
    with pytest.raises(contracts.ContractError, match="adjudication_request_changed"):
        contracts.validate_adjudication_response(req, answer(context, req), **inputs(context))


def test_missing_proof_and_wrong_raw_reference_still_refused(context):
    req = code_request(context)
    response = code_answer(context, req)
    response["proofs"] = []
    with pytest.raises(contracts.ContractError, match="scoped_adjudication_proof_unbound"):
        contracts.validate_adjudication_response(req, response, **inputs(context))
    req["evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(contracts.ContractError):
        contracts.validate_adjudication_response(req, response, **inputs(context))


def test_identity_and_complete_prompt_are_explicitly_bounded(context, monkeypatch):
    req = code_request(context)
    prompt = render(context, req)
    monkeypatch.setattr(contracts, "MAX_ADJUDICATION_PROMPT_BYTES", len(prompt.encode("utf-8")) - 1)
    with pytest.raises(contracts.ContractError, match="adjudication_prompt_exceeds_bound"):
        render(context, req)
    monkeypatch.setattr(contracts, "MAX_PROOF_IDENTITY_BYTES", 1)
    with pytest.raises(contracts.ContractError, match="adjudication_proof_identity_exceeds_bound"):
        code_request(context)


def test_native_identity_reuses_same_narrow_admitted_index(tmp_path):
    from test_closer_native_adjudication_receipts import answer as native_answer
    from test_closer_native_adjudication_receipts import request, validate

    ctx = native_context(tmp_path)
    req = request(ctx)
    prompt = contracts.build_adjudication_prompt(
        ctx["interpreted"]["proposal"], req, retained_dir=ctx["root"]
    )
    identity = payload(prompt)["adjudication_request"]["proof_identities"][0]["alternatives"][0]
    expected = {k: v for k, v in ctx["proof"].items() if k not in {"excerpt", "relevance"}}
    assert identity == expected
    response = native_answer(ctx, req)
    assert validate(ctx, req, response) == response
    response["proofs"][0]["run_attempt"] += 1
    with pytest.raises(contracts.ContractError, match="scoped_adjudication_proof_unbound"):
        validate(ctx, req, response)


def test_prompt_hash_changes_when_admitted_identity_changes(context):
    req = code_request(context)
    # Request digest covers typed identities as well as refs. No historical
    # answer is edited or credited by a newly rendered prompt.
    old_id = req["request_id"]
    changed = copy.deepcopy(req)
    changed["proof_identities"][0]["alternatives"][0]["filename"] += ".other"
    assert contracts._digest({k: v for k, v in changed.items() if k != "request_id"}) != old_id
    assert hashlib.sha256(render(context, req).encode()).hexdigest()
