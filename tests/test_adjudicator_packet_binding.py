import copy

import pytest

import adjudicator_retro as retro
import roles


def full_evidence():
    return {
        "diff_patch": {"text": "diff --git a/a b/a", "revision": "head"},
        "acceptance_criteria": [{"text": "named control passes", "revision": "issue-revision"}],
    }


@pytest.mark.parametrize("missing", ["patch", "criteria", "blank_patch", "blank_criteria"])
def test_missing_revision_cannot_bypass_metadata_floor(missing):
    evidence = copy.deepcopy(full_evidence())
    if missing == "patch":
        evidence["diff_patch"].pop("revision")
    elif missing == "criteria":
        evidence["acceptance_criteria"][0].pop("revision")
    elif missing == "blank_patch":
        evidence["diff_patch"]["revision"] = " "
    else:
        evidence["acceptance_criteria"][0]["revision"] = " "
    assert retro._packet_metadata_only(evidence)


def test_truncated_patch_cannot_bypass_metadata_floor():
    evidence = full_evidence()
    evidence["diff_patch"]["truncated"] = True
    assert retro._packet_metadata_only(evidence)


def test_complete_versioned_evidence_remains_gradeable():
    assert retro._packet_metadata_only(full_evidence()) is False


@pytest.mark.parametrize(
    "command",
    [
        "open the PR",
        "Please open the PR",
        "create the branch",
        "Please create the branch",
        "Go ahead and merge the PR",
    ],
)
def test_definite_article_mutation_commands_remain_forbidden(command):
    assert roles._recommended_next_step_is_imperative(command)
    for prefix in ("Please go ahead and ", "Go ahead and please "):
        assert roles._recommended_next_step_is_imperative(
            prefix + command.removeprefix("Please ").removeprefix("Go ahead and ")
        )
