"""Optional understood proof gaps do not expand source-owned accounting work."""

import copy

import pytest
from test_closer_semantic_assessment import answer, setup


def optional_row(view, gap):
    proof = next(row for row in view["catalog"] if row["purpose"] == "ordinary")
    return {
        "slot": 1,
        "meaning": "understood",
        "status": "unknown",
        "gap": gap,
        "explanation": "The original body is understood; this optional acceptance proof remains unresolved.",
        "citations": [
            {
                "ref": proof["ref"],
                "excerpt": proof["content"],
                "relevance": "This exact visible retained source is context for the optional acceptance proof question.",
            }
        ],
    }


@pytest.mark.parametrize(
    "gap",
    ["evidence_absent", "evidence_inconclusive", "observed_execution_required", "source_conflict"],
)
def test_omitted_and_understood_optional_unknown_have_same_nonaccepting_disposition(tmp_path, gap):
    *_, view, bridge, integrate = setup(tmp_path, native=True)
    assert view["required_obligation_slots"] == []
    omitted = answer(view)
    returned = copy.deepcopy(omitted)
    returned["obligations"] = [optional_row(view, gap)]
    canonical = bridge(returned)
    assert canonical["semantic_assessment"] == returned
    assert canonical["classification"] == "uncertain"
    assert canonical["substantive_needs"] == [
        "host_OTHER:" + hold for hold in view["host_residual_holds"]
    ]
    assert canonical["decision"]["criteria"][0]["result"] == "UNKNOWN"
    assert canonical["semantic_summary"]["known_unmet"] is False
    assert integrate(returned) == integrate(omitted)
    assert integrate(returned)["status"] == "exception"
    assert integrate(returned)["disposition"] is None
    assert canonical["host_residual_holds"] == view["host_residual_holds"]


@pytest.mark.parametrize("fault", ["meaning", "security", "required_finding", "proof"])
def test_optional_unknown_cannot_clear_other_meaning_risk_finding_or_proof_guards(tmp_path, fault):
    *_, view, bridge, integrate = setup(tmp_path, native=True)
    value = answer(view)
    value["obligations"] = [optional_row(view, "evidence_inconclusive")]
    if fault == "meaning":
        value["obligations"][0].update(meaning="unknown", gap="obligation_ambiguous")
        assert bridge(value)["classification"] == "uncertain"
        assert "semantic_meaning_unknown" in bridge(value)["substantive_needs"]
        assert integrate(value)["status"] == "exception"
    elif fault == "security":
        value["global_concerns"] = [
            {
                "kind": "security",
                "explanation": "Actual original security concern remains unresolved.",
                "citations": [],
            }
        ]
        assert "semantic_global_concern" in bridge(value)["substantive_needs"]
        assert integrate(value)["status"] == "exception"
    elif fault == "required_finding":
        value["findings"].pop()
        with pytest.raises(ValueError, match="coverage"):
            bridge(value)
    else:
        value["obligations"][0]["citations"][0]["excerpt"] = "not in the visible original proof"
        with pytest.raises(ValueError):
            bridge(value)


def test_ordinary_required_proof_gap_is_still_mandatory(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path)
    value = answer(view)
    value["obligations"][0].update(status="unknown", gap="evidence_inconclusive")
    assert 1 in view["required_obligation_slots"]
    assert "semantic_acceptance_proof_gap" in bridge(value)["substantive_needs"]
    assert integrate(value)["status"] == "exception"
    value["obligations"].pop()
    with pytest.raises(ValueError, match="coverage"):
        bridge(value)
