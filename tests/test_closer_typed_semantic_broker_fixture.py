"""Pure delivered-input -> new synthetic JSON -> real semantic host controls."""

import copy
import json

import pytest
from closer_typed_semantic_broker_fixture import delivered_inventory, emit_assessment
from test_closer_disposition import fixture
from test_closer_semantic_assessment import prompt

import closer_disposition as disposition
import closer_model_contracts as contracts
import closer_semantic_assessment as semantic


def case(tmp_path):
    snap, packet, ref, prep, legacy_fixture = fixture(tmp_path)
    root = tmp_path / "evidence"
    view = semantic.catalog_input(snap, packet, ref, root)
    rendered = prompt(snap, view)
    return (
        snap,
        packet,
        ref,
        prep,
        root,
        view,
        rendered,
        legacy_fixture["proposal"]["decision"]["criteria"],
    )


def test_actual_rendered_inventory_new_synthetic_response_roundtrips_real_host(tmp_path):
    snap, packet, ref, prep, root, view, rendered, gold = case(tmp_path)
    before = copy.deepcopy(gold)
    assert delivered_inventory(rendered) == view
    emitted = emit_assessment(rendered, gold)
    parsed = contracts.parse_low_proposal(json.dumps(emitted))
    canonical = semantic.materialize(
        parsed,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert gold == before and parsed == emitted
    assert set(parsed) == {"schema", "obligations", "findings", "global_concerns"}
    assert all(
        set(row) == {"slot", "meaning", "status", "citations", "explanation"}
        for row in parsed["obligations"]
    )
    result = disposition.integrate_ordinary(
        snap["target"],
        snap,
        ref,
        prep,
        {"actual": {"exit": 0, "run_id": "synthetic-new-v2-only"}, "proposal": canonical},
    )
    assert result["status"] == "accepted"
    assert canonical["authority"] is False


def test_new_dispute_is_declared_before_sealing_not_changed_after_low(tmp_path):
    snap, packet, ref, _, root, view, rendered, gold = case(tmp_path)
    emitted = emit_assessment(
        rendered,
        gold,
        unknown_slots={2},
        global_explanations=["A separately declared synthetic ambiguity must remain."],
    )
    original, chosen = semantic.adjudication_candidate(
        contracts.parse_low_proposal(json.dumps(emitted)),
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert chosen["status"] == "candidate" and chosen["slot"] == 2
    assert "semantic_global_concern" in original["substantive_needs"]
    assert original["semantic_assessment"] == emitted


@pytest.mark.parametrize(
    "damage", ["foreign_excerpt", "missing_relevance", "missing_gold", "serialized_answer"]
)
def test_fixture_cannot_repair_missing_semantics_or_actual_answers(tmp_path, damage):
    snap, packet, ref, _, root, view, rendered, gold = case(tmp_path)
    if damage == "missing_gold":
        with pytest.raises(ValueError, match="cover"):
            emit_assessment(rendered, gold[:-1])
    elif damage == "serialized_answer":
        with pytest.raises(ValueError, match="never provider output"):
            emit_assessment(rendered, json.dumps({"decision": {"criteria": gold}}))
    else:
        if damage == "foreign_excerpt":
            gold[0]["proofs"][0]["excerpt"] = "not in the actual delivered receipt"
        else:
            gold[0]["proofs"][0].pop("relevance")
        emitted = emit_assessment(rendered, gold)
        with pytest.raises(ValueError):
            semantic.materialize(
                contracts.parse_low_proposal(json.dumps(emitted)),
                snapshot=snap,
                packet=packet,
                packet_ref=ref,
                retained_dir=root,
                expected_catalog_sha256=semantic.catalog_digest(view),
            )


@pytest.mark.parametrize("invalid", [False, True])
def test_actual_new_synthetic_offload_method_uses_delivered_inventory_before_sealing(
    tmp_path, invalid
):
    from test_closer_delivery_lifecycle import OfflineDelivery

    snap, packet, ref, _, root, view, rendered, gold = case(tmp_path)
    delivery = OfflineDelivery.__new__(OfflineDelivery)
    delivery.native = False
    delivery.provider_calls = []
    delivery.transport_uncertain = False
    delivery.invalid_proof = invalid
    delivery.proposal = {"decision": {"criteria": gold}, "substantive_needs": []}
    result = delivery.offload("gemini", rendered)
    parsed = contracts.parse_low_proposal(result["output"])
    assert parsed["schema"] == semantic.SCHEMA
    assert len(delivery.provider_calls) == 1  # Synthetic method counter, no process.
    args = dict(
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    if invalid:
        with pytest.raises(ValueError):
            semantic.materialize(parsed, **args)
    else:
        assert semantic.materialize(parsed, **args)["authority"] is False


def test_explicit_native_finding_gold_preserves_inventory_and_host_other_holds(tmp_path):
    snap, packet, ref, _, _ = fixture(tmp_path, native=True)
    root = tmp_path / "evidence"
    view = semantic.catalog_input(snap, packet, ref, root)
    rendered = prompt(snap, view)
    findings = disposition.pending_nonpass_findings(snap, packet)
    gold = [
        {
            "finding_id": row["finding_id"],
            "status": "unresolved_missing_evidence",
            "reason": "The declared synthetic finding has no complete resolution proof.",
        }
        for row in findings
    ]
    emitted = emit_assessment(rendered, [], finding_gold=gold)
    result = semantic.materialize(
        contracts.parse_low_proposal(json.dumps(emitted)),
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert len(emitted["findings"]) == len(view["findings"]) == len(findings)
    assert result["decision"]["criteria"] == []
    assert result["host_residual_holds"] == view["host_residual_holds"]
    assert result["classification"] == "uncertain"
    with pytest.raises(ValueError, match="explicit synthetic finding"):
        emit_assessment(rendered, [], finding_gold=gold[:-1])


def test_large_new_typed_fixture_uses_more_original_obligations_not_oversize_rows(tmp_path):
    from test_closer_semantic_assessment import setup

    body = "## Tasks\n" + "\n".join(
        "- Trim surrounding whitespace, requirement " + str(i) for i in range(9)
    )
    snap, packet, ref, root, view, *_ = setup(tmp_path, body=body)
    rendered = prompt(snap, view)
    entry = next(row for row in view["catalog"] if row["kind"] == "codepatch")
    gold = [
        {
            "source": row["source"],
            "criterion": row["text"],
            "result": "PASS",
            "proofs": [
                {
                    "kind": "codepatch",
                    "filename": entry["context"]["filename"],
                    "excerpt": "+    return value.strip()",
                    "relevance": ("Explicit synthetic static trimming evidence. " * 80)[:1800],
                }
            ],
        }
        for row in view["obligations"]
    ]
    emitted = emit_assessment(
        rendered,
        gold,
        unknown_slots={2},
        explanation=("Explicit synthetic static trimming assessment. " * 80)[:1800],
    )
    raw = json.dumps(emitted)
    assert 32768 < len(raw.encode()) < 48 * 1024
    assert all(len(row["explanation"]) <= 2000 for row in emitted["obligations"])
    parsed = contracts.parse_low_proposal(raw)
    original, selected = semantic.adjudication_candidate(
        parsed,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert selected["status"] == "candidate" and selected["slot"] == 2
    assert original["authority"] is False


def test_inert_fixture_will_not_reemit_an_already_materialized_actual_answer():
    from closer_inert_dispatcher_support import InertCaller

    caller = InertCaller.__new__(InertCaller)
    with pytest.raises(ValueError, match="never re-emit an actual answer"):
        caller.interpretation({}, {}, {}, {"semantic_assessment": {"schema": semantic.SCHEMA}})


@pytest.mark.parametrize("scenario", ["independent_ambiguity", "understood_proof_gap"])
def test_prospective_refusal_scenarios_preserve_inventory_and_distinct_needs(tmp_path, scenario):
    snap, packet, ref, prep, root, view, rendered, gold = case(tmp_path)
    if scenario == "understood_proof_gap":
        for row in gold:
            row["result"] = "UNKNOWN"
        concerns = []
        expected_need = "semantic_acceptance_proof_gap"
    else:
        concerns = ["An explicitly declared original criterion meaning remains ambiguous."]
        expected_need = "semantic_global_concern"
    emitted = emit_assessment(rendered, gold, global_explanations=concerns)
    assert {row["slot"] for row in emitted["obligations"]} == set(view["required_obligation_slots"])
    proposal = semantic.materialize(
        contracts.parse_low_proposal(json.dumps(emitted)),
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert expected_need in proposal["substantive_needs"]
    outcome = disposition.integrate_ordinary(
        snap["target"],
        snap,
        ref,
        prep,
        {"actual": {"exit": 0, "run_id": "synthetic-v2-refusal-only"}, "proposal": proposal},
    )
    assert outcome["status"] == "exception"
    assert outcome["reason"] == "substantive_or_ambiguous_finding"
    assert not outcome.get("disposition")
