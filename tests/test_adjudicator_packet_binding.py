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


@pytest.mark.parametrize(
    "command",
    [
        "After review, merge the PR",
        "Once CI passes, please push the branch",
        "When approved, go ahead and label the issue",
        "If checks pass, please create the branch",
        "After manual review, open the PR",
        "Once approved, after review, commit the changes",
        "If checks pass, then merge the PR",
        "Once approved, then please push the branch",
        "After review, then go ahead and label the issue",
    ],
)
def test_conditional_mutation_commands_remain_forbidden(command):
    assert roles._recommended_next_step_is_imperative(command)
    proposal = roles._baseline_adjudication({"disputed_finding": "fixture finding"})
    proposal["recommended_next_step"] = command
    assert any("mutating execution" in error for error in roles._validate_adjudicator(proposal))


@pytest.mark.parametrize(
    "command",
    [
        "Merge is-ready into main",
        "Push can-fix to origin",
        "Merge should-fix into main",
    ],
)
def test_modal_prefixed_branch_operands_remain_forbidden(command):
    assert roles._recommended_next_step_is_imperative(command)
    proposal = roles._baseline_adjudication({"disputed_finding": "fixture finding"})
    proposal["recommended_next_step"] = command
    assert any("mutating execution" in error for error in roles._validate_adjudicator(proposal))


@pytest.mark.parametrize(
    "prose",
    [
        "After review, recommend merging the PR",
        "Once CI passes, inspect whether the merge is appropriate",
        "If useful, recommend a push after manual approval",
        "When approved, consider whether to create the branch",
        "If useful, then consider whether a merge is appropriate",
        "Merge is, however, appropriate after manual review.",
        "Push may, after review, be appropriate.",
        "After review, merge could, if approved, be appropriate.",
    ],
)
def test_conditional_recommendation_prose_remains_allowed(prose):
    assert not roles._recommended_next_step_is_imperative(prose)
    proposal = roles._baseline_adjudication({"disputed_finding": "fixture finding"})
    proposal["recommended_next_step"] = prose
    assert roles._validate_adjudicator(proposal) == []
