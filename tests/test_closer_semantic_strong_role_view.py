"""Authenticated STRONG view is distinct from original protected LOW view."""

import copy
import hashlib

import pytest
from test_closer_semantic_assessment import answer
from test_closer_semantic_catalog_visibility import collected
from test_closer_semantic_host_glue import candidate, reconcile, scoped_answer, sealed_strong

import closer_disposition as disposition
import closer_execution_contracts as contracts
import closer_semantic_assessment as semantic


def large_case(tmp_path, filler):
    raw, built, _check, packet, root = collected(tmp_path, filler_bytes=filler)
    identity = built[0]
    snapshot = {
        "target": identity["target"],
        "kind": "pr",
        "head": identity["head"],
        "base": identity["base"],
        "revision": "r1",
        "review_revision": "reviews1",
        "checks": "SUCCESS",
        "threads": [],
        "sources": [
            {
                "target": "catalog/test#2",
                "body": "## Tasks\n- Inspect the supplied report receipt.",
                "state": "open",
                "labels": [],
            }
        ],
    }
    path = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    ref = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    view = semantic.catalog_input(snapshot, packet, ref, root)
    assert view == built[-1]
    value = answer(view)
    for row in value["obligations"]:
        row.update(status="unknown", gap="evidence_inconclusive")
        row["citations"][0]["excerpt"] = "<testsuite>"
    return (snapshot, packet, ref, root, view), value, raw


@pytest.mark.parametrize("filler", [18000, 40000])
def test_strong_cites_only_its_actual_per_ref_view_not_low_view_or_whole_prompt(
    tmp_path, monkeypatch, filler
):
    case, value, raw = large_case(tmp_path / "case", filler)
    before = copy.deepcopy(value)
    # Claim text outside a supplied raw ref is not raw proof, even when the
    # same authentic tail marker appears elsewhere in the actual prompt.
    value["obligations"][0]["citations"][0][
        "relevance"
    ] += " Unproven diagnostic: LATE_SYNTHETIC_ONLY."
    original, chosen = candidate(case, value)
    assert chosen["status"] == "candidate"
    prompt = contracts.build_adjudication_prompt(original, chosen["request"], retained_dir=case[3])
    assert "LATE_SYNTHETIC_ONLY" in prompt  # relevance is deliberately not proof
    result = scoped_answer(chosen["request"], original["decision"]["criteria"][0])
    result["proofs"][0]["excerpt"] = "LATE_SYNTHETIC_ONLY"
    assert disposition._proofs_valid(
        result["evidence"], result["proofs"], disposition._receipt_index(case[1], case[3]), case[3]
    )
    assert "LATE_SYNTHETIC_ONLY" not in case[4]["catalog"][0]["content"]
    actual, _ = sealed_strong(
        tmp_path / "receipt",
        monkeypatch,
        result,
        case[0],
        case[2],
        chosen["request"],
        original=original,
    )
    if filler == 40000:
        with pytest.raises(ValueError, match="outside_actual_supplied"):
            reconcile(case, value, chosen["request"], result, actual)
    else:
        final = reconcile(case, value, chosen["request"], result, actual)
        assert final["semantic_assessment"] == value
        assert final["semantic_reconciliation"]["derived_role"] == "strong_criterion_adjudication"
        assert (
            final["semantic_reconciliation"]["derived_assessment"]["obligations"][0]["status"]
            == "satisfied"
        )
        assert final["decision"]["criteria"][0]["proofs"][0]["excerpt"] == "LATE_SYNTHETIC_ONLY"
        assert final["decision"]["criteria"][1] == original["decision"]["criteria"][1]
        assert "semantic_acceptance_proof_gap" in final["substantive_needs"]
    assert raw.endswith(b"</testsuite>")
    assert before["obligations"][0]["status"] == value["obligations"][0]["status"] == "unknown"


def test_genuine_receipt_with_wrong_prompt_is_not_a_source_question(tmp_path, monkeypatch):
    case, value, _raw = large_case(tmp_path / "case", 18000)
    original, chosen = candidate(case, value)
    result = scoped_answer(chosen["request"], original["decision"]["criteria"][0])
    actual, _ = sealed_strong(
        tmp_path / "receipt",
        monkeypatch,
        result,
        case[0],
        case[2],
        chosen["request"],
        original=original,
        prompt_override="A different bounded synthetic request, not the source question.",
    )
    with pytest.raises(ValueError, match="prompt_differs"):
        reconcile(case, value, chosen["request"], result, actual)
