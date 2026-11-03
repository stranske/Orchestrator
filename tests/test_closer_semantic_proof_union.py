"""Structural proof compatibility only; no semantic qualification/model calls."""

import copy

import pytest
from closer_semantic_audit_support import source_unchanged
from test_closer_semantic_assessment import answer, setup
from test_closer_semantic_host_glue import (
    candidate,
    disputed,
    reconcile,
    scoped_answer,
    sealed_strong,
)

import closer_disposition as disposition
import closer_execution_contracts as contracts


@pytest.mark.parametrize("kind", ["codepatch", "junit", "native-comparison"])
def test_existing_valid_finding_proof_union_survives_actual_scoped_reconciliation(
    tmp_path, monkeypatch, kind
):
    case, value = disputed(tmp_path / "case", native=True)
    selected = next(item for item in case[4]["catalog"] if item["kind"] == kind)
    for row in value["findings"]:
        row["citations"] = [
            {
                "ref": selected["ref"],
                "excerpt": selected["content"],
                "relevance": "This exact source-bound current proof addresses the named synthetic original finding.",
            }
        ]
    original, chosen = candidate(case, value)
    assert chosen["status"] == "candidate"
    result = scoped_answer(chosen["request"], original["resolutions"][0], native=True)
    assert (
        contracts.validate_adjudication_response(
            chosen["request"],
            result,
            snapshot=case[0],
            packet=case[1],
            packet_ref=case[2],
            retained_dir=case[3],
        )
        == result
    )
    actual, _ = sealed_strong(
        tmp_path / "receipt",
        monkeypatch,
        result,
        case[0],
        case[2],
        chosen["request"],
        original=original,
    )
    final = reconcile(case, value, chosen["request"], result, actual)
    assert final["semantic_assessment"] == value
    assert (
        final["semantic_reconciliation"]["derived_assessment"]["findings"][0]["status"]
        == "nonissue"
    )
    assert final["resolutions"][0]["proofs"] == result["proofs"]
    assert final["authority"] is False


def test_native_resolution_catalog_still_cannot_prove_ordinary_acceptance(tmp_path):
    snap, packet, ref, root, view, bridge, _ = setup(tmp_path, native=True)
    native = next(item for item in view["catalog"] if item["purpose"] == "native_resolution")
    value = answer(view)
    value["obligations"] = [
        {
            "slot": row["slot"],
            "meaning": "understood",
            "status": "satisfied",
            "citations": [
                {
                    "ref": native["ref"],
                    "excerpt": native["content"],
                    "relevance": "This native input cannot establish implementation acceptance.",
                }
            ],
            "explanation": "Synthetic attempted acceptance claim, deliberately invalid.",
        }
        for row in view["obligations"]
    ]
    with pytest.raises(ValueError, match="purpose"):
        bridge(value)
    assert disposition._receipt_index(packet, root)


@pytest.mark.parametrize("damage", ["foreign_ref", "wrong_excerpt", "wrong_kind", "wrong_head"])
def test_union_never_relaxes_foreign_or_typed_proof_guards(tmp_path, damage):
    case, value = disputed(tmp_path, native=True)
    original, chosen = candidate(case, value)
    result = scoped_answer(chosen["request"], original["resolutions"][0], native=True)
    changed = copy.deepcopy(result)
    if damage == "foreign_ref":
        changed["evidence"][0]["sha256"] = "0" * 64
    elif damage == "wrong_excerpt":
        changed["proofs"][0]["excerpt"] = "not present in the retained source file"
    elif damage == "wrong_kind":
        changed["proofs"][0]["kind"] = "foreign-kind"
    else:
        changed["proofs"][0]["head_sha"] = "0" * 40
    with pytest.raises(ValueError):
        contracts.validate_adjudication_response(
            chosen["request"],
            changed,
            snapshot=case[0],
            packet=case[1],
            packet_ref=case[2],
            retained_dir=case[3],
        )


def test_existing_host_strong_and_authority_sources_unchanged():
    for filename in (
        "closer_disposition.py",
        "closer_execution_contracts.py",
        "closer_adjudication_policy.py",
    ):
        assert source_unchanged("9379f225", filename), filename
