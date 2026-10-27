"""LOW's source grammar can construct the exact typed proof its validator requires."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_disposition import check, fixture

import closer_model_contracts as contracts
import closer_semantic_assessment as semantic


def retained_case(tmp_path):
    fx = fixture(tmp_path)
    snapshot, packet, packet_ref, _prep, _interpretation = fx
    root = Path(packet_ref["path"]).parent.parent
    view = semantic.catalog_input(snapshot, packet, packet_ref, root)
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
        semantic_inventory=view,
    )
    return fx, prompt, view


def test_actual_low_grammar_constructs_current_retained_codepatch_proof(tmp_path):
    fx, prompt, view = retained_case(tmp_path)
    snapshot, packet, packet_ref, _, interpretation = fx
    code = next(row for row in view["catalog"] if row["kind"] == "codepatch")
    assert "literal raw UTF-8 substring" in prompt
    assert "Host owns identities" in prompt
    advice = {
        "schema": semantic.SCHEMA,
        "findings": [],
        "global_concerns": [],
        "obligations": [
            {
                "slot": slot,
                "meaning": "understood",
                "status": "satisfied",
                "explanation": "Synthetic structural codepatch proof claim, not model qualification.",
                "citations": [
                    {
                        "ref": code["ref"],
                        "excerpt": "ship_report",
                        "relevance": "The declared synthetic patch claim addresses the original report path.",
                    }
                ],
            }
            for slot in view["required_obligation_slots"]
        ],
    }
    parsed = contracts.parse_low_proposal(json.dumps(advice))
    interpretation["proposal"] = semantic.materialize(
        parsed,
        snapshot=snapshot,
        packet=packet,
        packet_ref=packet_ref,
        retained_dir=Path(packet_ref["path"]).parent.parent,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert all(
        row["proofs"][0]["kind"] == "codepatch"
        for row in interpretation["proposal"]["decision"]["criteria"]
    )
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
    assert descriptor["prompt_version"] == "closer-semantic-assessment-prompt/v2"
    assert descriptor["source_sha256"] == source_sha256
    assert descriptor["rendering_version"] == "closer-classification-rendering/v1"
    assert descriptor["response_syntax_version"] == "closer-strict-response-json/v1"
