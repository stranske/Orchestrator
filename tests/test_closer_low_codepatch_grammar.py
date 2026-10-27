"""LOW's source grammar can construct the exact typed proof its validator requires."""

import copy
import hashlib
import re
from pathlib import Path

import pytest
from test_closer_disposition import check, fixture

import closer_model_contracts as contracts


def retained_case(tmp_path):
    fx = fixture(tmp_path)
    snapshot, packet, packet_ref, _prep, _interpretation = fx
    prompt = contracts.render_classification_prompt(
        {
            "target": snapshot["target"],
            "head": snapshot["head"],
            "base": snapshot["base"],
            "packet_ref": packet_ref,
        },
        source_rows=snapshot["sources"],
        diff_rows=packet["sections"]["diff_files"]["content"]["files"],
        review_rows=[],
        artifact_rows=[],
    )
    grammar = prompt.split("A codepatch proof has ", 1)[1].split(". For every supplied", 1)[0]
    return fx, prompt, grammar


def test_actual_low_grammar_constructs_current_retained_codepatch_proof(tmp_path):
    fx, _prompt, grammar = retained_case(tmp_path)
    discriminator = re.search(r"\bkind\s*:\s*['\"](codepatch)['\"]", grammar)
    assert (
        discriminator
    ), "The actual shared LOW prompt must specify its required typed discriminator"
    for field in (
        "evidence_sha256",
        "filename",
        "base",
        "head",
        "blob_sha",
        "excerpt",
        "relevance",
    ):
        assert field in grammar
    snapshot, packet, _, _, interpretation = fx
    file = packet["sections"]["diff_files"]["content"]["files"][0]
    ref = file["patch_retained"]
    proof = {
        "evidence_sha256": ref["sha256"],
        "kind": discriminator.group(1),
        "filename": file["filename"],
        "base": snapshot["base"],
        "head": snapshot["head"],
        "blob_sha": file["blob_sha"],
        "excerpt": "ship_report",
        "relevance": "The patch implements the report shipping path.",
    }
    for row in interpretation["proposal"]["decision"]["criteria"]:
        row.update(evidence=[copy.deepcopy(ref)], proofs=[copy.deepcopy(proof)])
    assert check(fx)["status"] == "accepted"
    for row in interpretation["proposal"]["decision"]["criteria"]:
        del row["proofs"][0]["kind"]
    result = check(fx)
    assert result["status"] == "wait" and result["reason"] == "criterion_proof_unbound_or_unrelated"


@pytest.mark.parametrize("kind", [None, "junit", "CodePatch"])
def test_missing_or_wrong_kind_still_fails_actual_integrate(tmp_path, kind):
    fx, _prompt, _grammar = retained_case(tmp_path)
    file = fx[1]["sections"]["diff_files"]["content"]["files"][0]
    proof = {
        "evidence_sha256": file["patch_retained"]["sha256"],
        "filename": file["filename"],
        "base": fx[0]["base"],
        "head": fx[0]["head"],
        "blob_sha": file["blob_sha"],
        "excerpt": "ship_report",
        "relevance": "The patch implements the report shipping path.",
    }
    if kind is not None:
        proof["kind"] = kind
    for row in fx[4]["proposal"]["decision"]["criteria"]:
        row.update(evidence=[file["patch_retained"]], proofs=[copy.deepcopy(proof)])
    result = check(fx)
    assert result["status"] == "wait" and result["reason"] == "criterion_proof_unbound_or_unrelated"


def test_prospective_low_descriptor_versions_new_source_grammar():
    source_sha256 = hashlib.sha256(Path(contracts.__file__).read_bytes()).hexdigest()
    descriptor = contracts.classification_contract_descriptor(source_sha256=source_sha256)
    assert descriptor["prompt_version"] == "closer-classification-prompt/v3"
    assert descriptor["source_sha256"] == source_sha256
    assert descriptor["rendering_version"] == "closer-classification-rendering/v1"
    assert descriptor["response_syntax_version"] == "closer-strict-response-json/v1"
