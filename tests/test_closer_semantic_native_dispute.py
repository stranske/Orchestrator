"""Current native meaning disputes stay questions, never accepted accounting."""

import copy

import pytest
from test_closer_semantic_assessment import answer, setup
from test_closer_semantic_host_glue import (
    candidate,
    disputed,
    reconcile,
    scoped_answer,
    sealed_strong,
)

import closer_disposition as disposition


def unknown_meaning(value):
    changed = copy.deepcopy(value)
    changed["findings"][0].update(meaning="unknown", gap="obligation_ambiguous")
    return changed


def test_same_actual_native_basis_and_refs_admit_meaning_question_without_accounting(tmp_path):
    case, before = disputed(tmp_path, native=True)
    baseline, baseline_candidate = candidate(case, before)
    changed = unknown_meaning(before)
    original = copy.deepcopy(changed)
    canonical, selected = candidate(case, changed)
    assert selected["status"] == baseline_candidate["status"] == "candidate"
    assert selected["request"] == baseline_candidate["request"]
    assert canonical["semantic_assessment"] == original == changed
    assert canonical["classification"] == "uncertain"
    assert "semantic_meaning_unknown" in canonical["substantive_needs"]
    placeholders = [
        row for row in canonical["resolutions"] if row["status"] == "pending_semantic_adjudication"
    ]
    assert len(placeholders) == 1
    assert placeholders[0]["finding_id"] == selected["request"]["basis"]["finding_id"]
    snap, _packet, ref, _root, *_ = case
    result = disposition.integrate_ordinary(
        snap["target"],
        snap,
        ref,
        {"revision": snap["revision"]},
        {"actual": {"exit": 0, "run_id": "synthetic-only"}, "proposal": canonical},
    )
    assert result["status"] == "exception"
    assert result.get("disposition") is None
    assert canonical["authority"] is False
    assert baseline["semantic_assessment"] == before


def test_only_authenticated_scoped_answer_can_derive_selected_meaning(tmp_path, monkeypatch):
    case, before = disputed(tmp_path / "case", native=True)
    changed = unknown_meaning(before)
    canonical, selected = candidate(case, changed)
    placeholder = next(
        row for row in canonical["resolutions"] if row["status"] == "pending_semantic_adjudication"
    )
    result = scoped_answer(selected["request"], placeholder, native=True)
    actual, _ = sealed_strong(
        tmp_path / "receipt",
        monkeypatch,
        result,
        case[0],
        case[2],
        selected["request"],
        original=canonical,
    )
    final = reconcile(case, changed, selected["request"], result, actual)
    assert final["semantic_assessment"] == changed
    derived = final["semantic_reconciliation"]["derived_assessment"]
    assert derived["findings"][0]["meaning"] == "understood"
    assert derived["findings"][0]["status"] == "nonissue"
    assert derived["findings"][1:] == changed["findings"][1:]
    assert final["authority"] is False


@pytest.mark.parametrize(
    "fault", ["owner", "foreign_slot", "empty_refs", "priorraw_only", "repeated_id"]
)
def test_question_placeholder_never_waives_existing_scope_and_owner_gates(tmp_path, fault):
    if fault in {"priorraw_only", "repeated_id"}:
        raw = {"verdict": "NON_PASS", "summary": "Original retained ordinary failure"}
        case = setup(
            tmp_path, dispositions={"raw_findings": [raw] * (2 if fault == "repeated_id" else 1)}
        )
        value = answer(case[4])
        selected = next(row for row in case[4]["catalog"] if row["purpose"] == "ordinary")
        value["findings"][0]["citations"] = [
            {
                "ref": selected["ref"],
                "excerpt": selected["content"],
                "relevance": "This exact supplied receipt is the only selected original evidence.",
            }
        ]
    else:
        case, value = disputed(tmp_path, native=True)
    changed = unknown_meaning(value)
    if fault == "owner":
        changed["global_concerns"] = [
            {
                "kind": "security",
                "explanation": "A specific original security concern requires its existing owner.",
                "citations": [],
            }
        ]
    elif fault == "foreign_slot":
        changed["findings"][0]["slot"] = 900
        with pytest.raises(ValueError):
            candidate(case, changed)
        return
    elif fault == "empty_refs":
        changed["findings"][0]["citations"] = []
    _canonical, selected = candidate(case, changed)
    assert selected["status"] == "pending"
    assert "request" not in selected
    assert selected["authority"] is False
