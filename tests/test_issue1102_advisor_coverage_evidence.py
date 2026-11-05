"""The recovered Gate comparison for src/capability_advisor.py, and its acceptance command."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPORT = REPO / "docs" / "verification" / "issue-1102" / "README.md"


def _load_evidence():
    path = Path(__file__).resolve().parent / "issue1102_coverage_evidence.py"
    spec = importlib.util.spec_from_file_location("issue1102_coverage_evidence", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


evidence = _load_evidence()


def _gaps():
    slices = evidence.load_slices()
    return {
        name: evidence.statement_and_branch_gap(evidence.coverage_payload(row))
        for name, row in slices["measurements"].items()
    }


def test_unmeasured_branches_are_not_counted_as_zero():
    gap = _gaps()["base"]
    assert gap["branches_measured"] is False
    assert gap["uncovered_branches"] is None
    assert gap["uncovered_branch_locations"] is None
    assert gap["uncovered_units"] is None
    assert gap["uncovered_statements"] == 68


def test_measured_branches_stay_separate_from_statements():
    payload = {
        "meta": {"version": "7.16.2", "branch_coverage": True},
        "files": {
            evidence.TARGET: {
                "missing_lines": [10, 11],
                "missing_branches": [[10, -1], [11, 12]],
                "summary": {
                    "num_statements": 20,
                    "covered_lines": 18,
                    "missing_lines": 2,
                    "missing_branches": 2,
                },
            }
        },
    }
    gap = evidence.statement_and_branch_gap(payload)
    assert gap["uncovered_statement_locations"] == [10, 11]
    assert gap["uncovered_branch_locations"] == [[10, -1], [11, 12]]
    assert gap["uncovered_statements"] == 2
    assert gap["uncovered_branches"] == 2
    assert gap["uncovered_units"] == 4
    with pytest.raises(ValueError, match="branch configuration"):
        evidence.gate_statement_reduction(_gaps()["base"], gap)


def test_pr650_gate_slices_show_a_29_statement_reduction():
    gaps = _gaps()
    assert (
        gaps["base"]["uncovered_statement_locations"]
        == gaps["merge_base"]["uncovered_statement_locations"]
    )
    assert gaps["head"]["uncovered_statements"] == 39
    assert evidence.gate_statement_reduction(gaps["base"], gaps["head"]) == 29
    assert evidence.gate_statement_reduction(gaps["merge_base"], gaps["head"]) == 29
    assert gaps["base"]["statements_total"] == 1593
    assert gaps["head"]["statements_total"] == 1602
    assert {line for line in gaps["head"]["uncovered_statement_locations"]} == set(
        gaps["head"]["uncovered_statement_locations"]
    )


def test_local_94_to_61_is_not_a_gate_reduction():
    assert evidence.LOCAL_COHORT["comparable_to_gate"] is False
    local_drop = evidence.LOCAL_COHORT["before_missing"] - evidence.LOCAL_COHORT["after_missing"]
    assert local_drop == 33
    gate_drop = evidence.gate_statement_reduction(_gaps()["base"], _gaps()["head"])
    assert gate_drop != local_drop
    assert evidence.LOCAL_COHORT["before_lines"] != "1525/1593"


def test_issue_647_ranking_selected_only_capability_advisor():
    selected = evidence.SELECTION["selected"]
    assert [row["path"] for row in selected] == [evidence.TARGET]
    assert selected[0]["reported_uncovered_units"] == 68
    assert evidence.SELECTION["order"] == ["escaped defects", "churn", "uncovered mass"]
    assert evidence.SELECTION["recount_not_substituted"]["churn"] != selected[0]["churn"]
    assert evidence.TARGET not in evidence.SELECTION["excluded_in_flight"]


def test_every_remaining_head_statement_has_its_own_limit():
    missing = _gaps()["head"]["uncovered_statement_locations"]
    assert len(missing) == 39
    reasons = [evidence.head_statement_limit(line) for line in missing]
    assert all(len(reason) > 40 for reason in reasons)
    assert len(set(reasons)) > 10
    with pytest.raises(KeyError, match="no limit recorded"):
        evidence.head_statement_limit(1)


def test_real_input_acceptance_matches_the_recorded_outcome_within_two_minutes(tmp_path):
    result = evidence.run_acceptance(tmp_path, REPO, timeout=90)
    assert result["elapsed_seconds"] < 120
    assert result["outcome"] == evidence.RECORDED_ACCEPTANCE["outcome"]
    assert evidence.RECORDED_ACCEPTANCE["attempts"] == 1
    assert "120 seconds" in evidence.RECORDED_ACCEPTANCE["stop_rule"]
    assert evidence.RECORDED_ACCEPTANCE["check_date"] == "2026-11-05"


def test_committed_report_matches_the_renderer_and_names_the_artifacts():
    rendered = evidence.render_report()
    text = REPORT.read_text(encoding="utf-8")
    assert rendered in text
    for revision in (
        "ec9c7e09aaca054dac1d38151e766827b0658c43",
        "143ce07ceacae5f4d973219f1267045133825aa7",
        "d477734a1d2ee3bb93daa599e6bb52b64c308678",
    ):
        assert revision in rendered
    assert "38970976941" in rendered
    assert "38972960240" in rendered
    assert "38977286469" in rendered
    assert "branch_coverage=False" in rendered
    assert "Uncovered branch locations: none" in rendered
