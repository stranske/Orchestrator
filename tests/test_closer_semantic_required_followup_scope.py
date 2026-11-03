"""Automatic work follows required host question, not optional model claims."""

import copy

from test_closer_semantic_host_glue import candidate, disputed
from test_closer_semantic_optional_accounting_scope import optional_row


def test_optional_understood_unknown_never_preempts_required_finding_dispute(tmp_path):
    case, original = disputed(tmp_path, native=True)
    _baseline, before = candidate(case, original)
    assert before["status"] == "candidate" and before["section"] == "findings"
    extra = copy.deepcopy(original)
    extra["obligations"] = [optional_row(case[4], "evidence_inconclusive")]
    canonical, after = candidate(case, extra)
    assert after == before
    assert "semantic_acceptance_proof_gap" not in canonical["substantive_needs"]
    assert canonical["semantic_assessment"] == extra
    assert canonical["decision"]["criteria"][0]["result"] == "UNKNOWN"


def test_accounted_required_findings_optional_proofgap_has_no_strong_or_collection_work(tmp_path):
    case, original = disputed(tmp_path, native=True)
    for row in original["findings"]:
        row["status"] = "nonissue"
        del row["gap"]
    _canonical, before = candidate(case, original)
    assert before["status"] == "not_needed"
    extra = copy.deepcopy(original)
    extra["obligations"] = [optional_row(case[4], "observed_execution_required")]
    canonical, after = candidate(case, extra)
    assert after == before
    assert "adjudication_request" not in canonical
    assert canonical["substantive_needs"] == _canonical["substantive_needs"]
    assert canonical["host_residual_holds"] == case[4]["host_residual_holds"]
    assert canonical["semantic_assessment"]["obligations"][0]["status"] == "unknown"
    # OTHER meaning uncertainty remains a real hold, without making that
    # optional slot an automatically authorized STRONG question.
    extra["obligations"][0].update(meaning="unknown", gap="obligation_ambiguous")
    held, followup = candidate(case, extra)
    assert held["classification"] == "uncertain"
    assert "semantic_meaning_unknown" in held["substantive_needs"]
    assert followup["status"] == "not_needed" and "adjudication_request" not in held


def test_required_ordinary_dispute_remains_eligible(tmp_path):
    case, value = disputed(tmp_path)
    canonical, followup = candidate(case, value)
    assert followup["status"] == "candidate"
    assert followup["section"] == "obligations" and followup["slot"] == 1
    assert "semantic_acceptance_proof_gap" in canonical["substantive_needs"]
