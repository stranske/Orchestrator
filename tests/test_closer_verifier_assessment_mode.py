"""A correlated result must satisfy the actual compare request, not only its event."""

import copy

import pytest
from test_closer_verifier_request_correlation import terminal_world

import closer_verifier_followup as followup
import verifier_evidence


@pytest.mark.parametrize("change", ["evaluate", "missing-mode", "noncanonical", "missing-identity"])
def test_wrong_assessment_does_not_release_compare_request(tmp_path, change):
    path, target, accepted, observed = terminal_world(tmp_path)
    original = path.read_bytes()
    marker = observed["verifier"]
    if change == "evaluate":
        marker["assessment_identity"]["mode"] = "evaluate"
    elif change == "missing-mode":
        marker["assessment_identity"].pop("mode")
    elif change == "noncanonical":
        marker["assessment_identity"]["canonical"] = False
    else:
        marker.pop("assessment_identity")
    observed["verifier_lifecycle"] = copy.deepcopy(marker)
    repo, number = target.split("#")
    assert verifier_evidence.decision_matches_identity(
        marker, repo, int(number), accepted["head"], accepted["merge"]
    )
    assert followup.terminal_proof(path, target, accepted, observed, now=1000) is None
    assert path.read_bytes() == original


def test_canonical_compare_retains_original_operation_without_request(tmp_path):
    path, target, accepted, observed = terminal_world(tmp_path)
    original = path.read_bytes()
    proof = followup.terminal_proof(path, target, accepted, observed, now=1000)
    assert proof["operation_id"] == accepted["operation_id"]
    assert path.read_bytes() == original
