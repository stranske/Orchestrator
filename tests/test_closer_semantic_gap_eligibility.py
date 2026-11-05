"""Valid refs are not permission for STRONG to guess missing execution."""

import copy

import pytest
from test_closer_semantic_host_glue import candidate, disputed


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("gap", ["evidence_absent", "observed_execution_required"])
def test_concrete_missing_evidence_uses_collection_dependency_even_with_same_valid_refs(
    tmp_path, native, gap
):
    case, value = disputed(tmp_path, native=native)
    _before, baseline = candidate(case, value)
    assert baseline["status"] == "candidate"
    section = "findings" if native else "obligations"
    original = copy.deepcopy(value)
    value[section][0]["gap"] = gap
    expected = copy.deepcopy(value)
    canonical, selected = candidate(case, value)
    assert selected["status"] == "pending"
    assert selected["reason"] == "semantic_gap_requires_existing_collection_selector"
    assert selected["slot"] == 1 and selected["gap"] == gap
    assert selected["basis"] == baseline["request"]["basis"] or selected["basis"].get(
        "criterion"
    ) == baseline["request"].get("criterion")
    assert selected["binding"]["packet_sha256"] == case[2]["sha256"]
    assert "request" not in selected and "adjudication_request" not in canonical
    assert canonical["semantic_assessment"] == expected == value
    assert value[section][0]["citations"] == original[section][0]["citations"]
    assert canonical["classification"] == ("uncertain" if native else "relevant")
    assert canonical["authority"] is False
    # Reordering output does not bypass the first original slot into a second
    # row which has a genuine remaining dispute.
    value[section].reverse()
    assert candidate(case, value)[1] == selected


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "gap", ["evidence_inconclusive", "source_conflict", "obligation_ambiguous"]
)
def test_source_admitted_actual_dispute_remains_one_candidate(tmp_path, native, gap):
    case, value = disputed(tmp_path, native=native)
    section = "findings" if native else "obligations"
    value[section][0].update(
        gap=gap, meaning="unknown" if gap == "obligation_ambiguous" else "understood"
    )
    before = copy.deepcopy(value)
    canonical, selected = candidate(case, value)
    assert selected["status"] == "candidate"
    assert selected["slot"] == 1
    assert canonical["semantic_assessment"] == before == value
    assert selected["authority"] is False
    assert canonical["classification"] == (
        "uncertain" if native or gap == "obligation_ambiguous" else "relevant"
    )
