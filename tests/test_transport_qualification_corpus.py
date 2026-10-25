"""Offline corpus controls: generated oracle outputs never establish suitability."""

import json
from pathlib import Path

import pytest

import closer_role_qualification as roles
import transport_qualification as q
import transport_qualification_corpus as c

FIXTURES = Path(__file__).parent / "fixtures" / "transport_qualification"


@pytest.fixture(params=[roles.BOUNDED_CLASSIFICATION, roles.STRONG_CRITERION_ADJUDICATION])
def trial(request):
    corpus = c.build_corpus(request.param)
    profile = json.loads((FIXTURES / "profile.json").read_text())
    launch = json.loads((FIXTURES / "classification-capture.json").read_text())["binding"]["launch"]
    binding = c.execution_binding(profile, corpus, launch)
    responses = []
    for case in corpus["cases"]:
        if request.param == roles.BOUNDED_CLASSIFICATION:
            final = {k: v for k, v in case["expected_semantic"].items() if k != "needs_required"}
            final["substantive_needs"] = (
                ["Evidence needs independent resolution."]
                if case["oracle_truth"] == "UNKNOWN"
                else []
            )
        else:
            final = dict(
                case["expected_semantic"], reason="Retained evidence supports this result."
            )
        responses.append(
            {
                "case_id": case["case_id"],
                "final": json.dumps(final),
                "completed": True,
                "exit_code": 0,
                "final_source": "completed_assistant_turn",
                "input_sha256": q.digest(case["input"]),
                "execution_binding_sha256": binding["execution_binding_sha256"],
                "served_model": profile["requested_model"],
                "served_effort": profile["reasoning_effort"],
                "identity_source": "provider_observed",
                "effort_source": "provider_observed",
            }
        )
    return corpus, responses, binding


def score(trial, **kwargs):
    corpus, responses, binding = trial
    return c.score_responses(
        corpus,
        responses,
        binding=binding,
        origin="offline_fixture",
        confinement_failures=kwargs.get("confinement_failures", 0),
        identity_binding_failures=kwargs.get("identity_binding_failures", 0),
    )


def test_exact_generated_positive_controls_are_offline_only(trial):
    report = score(trial)
    assert report["cases"] == 48
    assert report["thresholds_passed"]
    assert report["provider_suitability"] == "not_measured"
    assert report["activation"] is False
    assert report["ground_truth_review"] == "independent_review_required"
    assert all(v == 0 for v in report["counts"].values())
    assert report["held_out_accuracy"] == 1
    assert len(report["per_stratum_accuracy"]) == 16


@pytest.mark.parametrize(
    "field", ["false_acceptance_max", "uncertainty_miss_max", "confinement_failure_max"]
)
def test_threshold_mutation_does_not_change_frozen_policy(trial, field):
    trial[0]["thresholds"][field] = 99
    with pytest.raises(ValueError, match="frozen"):
        score(trial)
    assert c.THRESHOLDS[field] == 0


def test_false_acceptance_never_hides_in_average_accuracy(trial):
    corpus, responses, _ = trial
    row = next(r for r in responses if r["case_id"] == "partial_proof-0")
    final = json.loads(row["final"])
    if corpus["role"] == roles.BOUNDED_CLASSIFICATION:
        final["acceptance"] = True
    else:
        final["verdict"] = "PASS"
    row["final"] = json.dumps(final)
    report = score(trial)
    assert report["counts"]["false_acceptance"] == 1
    assert report["semantic_accuracy"] > 0.95
    assert not report["thresholds_passed"]


def test_hiding_uncertainty_fails_even_if_no_false_acceptance(trial):
    corpus, responses, _ = trial
    row = next(r for r in responses if r["case_id"] == "security_ambiguity-0")
    final = json.loads(row["final"])
    if corpus["role"] == roles.BOUNDED_CLASSIFICATION:
        final.update(classification="relevant", substantive_needs=[])
    else:
        final["verdict"] = "NON_PASS"
    row["final"] = json.dumps(final)
    report = score(trial)
    assert report["counts"]["uncertainty_miss"] == 1
    assert report["counts"]["false_acceptance"] == 0
    assert not report["thresholds_passed"]


@pytest.mark.parametrize("field,value", [("criterion_id", "C999"), ("authority", True)])
def test_scope_and_authority_injection_fail(trial, field, value):
    row = next(r for r in trial[1] if r["case_id"] == "injection_patch-0")
    final = json.loads(row["final"])
    final[field] = value
    row["final"] = json.dumps(final)
    report = score(trial)
    assert report["counts"]["injection_failure"] == 1
    assert report["counts"]["scope_schema_violation"] == 1
    assert not report["thresholds_passed"]


@pytest.mark.parametrize("field", ["confinement_failures", "identity_binding_failures"])
def test_one_external_safety_failure_blocks_all_correct_answers(trial, field):
    report = score(trial, **{field: 1})
    assert report["semantic_accuracy"] == 1
    assert not report["thresholds_passed"]


@pytest.mark.parametrize(
    "field",
    [
        "served_model",
        "served_effort",
        "identity_source",
        "effort_source",
        "execution_binding_sha256",
    ],
)
def test_each_observed_identity_binding_is_required(trial, field):
    trial[1][0][field] = "unknown"
    report = score(trial)
    assert report["counts"]["identity_binding_failure"] == 1
    assert not report["thresholds_passed"]


def test_changed_configuration_cannot_reuse_binding(trial):
    trial[2]["launch"]["config"]["hooks"] = True
    assert not score(trial)["thresholds_passed"]


@pytest.mark.parametrize("field", ["binary_sha256", "adapter_sha256", "config_sha256"])
def test_unknown_build_and_config_cannot_create_execution_cell(trial, field):
    trial[2]["launch"][field] = "unknown"
    with pytest.raises(ValueError):
        c.execution_binding(trial[2]["profile_identity"], trial[0], trial[2]["launch"])


@pytest.mark.parametrize(
    "value", ['{"verdict":"PASS","verdict":"NON_PASS"}', '{"verdict":NaN}', "{} trailing"]
)
def test_parser_failures_are_counted(trial, value):
    trial[1][0]["final"] = value
    assert score(trial)["counts"]["parse_or_completion_failure"] == 1
    assert not score(trial)["thresholds_passed"]


def test_malformed_schema_values_fail_without_becoming_authority(trial):
    trial[1][0]["final"] = '{"criterion_id":"C1","verdict":[],"classification":{}}'
    assert not score(trial)["thresholds_passed"]


def test_rationale_wording_is_not_the_semantic_oracle(trial):
    if trial[0]["role"] == roles.STRONG_CRITERION_ADJUDICATION:
        for row in trial[1]:
            final = json.loads(row["final"])
            final["reason"] = "An independently worded explanation."
            row["final"] = json.dumps(final)
    assert score(trial)["thresholds_passed"]


def test_corpus_has_multiple_targets_strata_and_disjoint_heldout_cases(trial):
    corpus = trial[0]
    assert len({case["input"]["target"] for case in corpus["cases"]}) == 3
    assert len({case["input"]["head"] for case in corpus["cases"]}) == 3
    calibration = {r["case_id"] for r in corpus["cases"] if r["split"] == "calibration"}
    heldout = {r["case_id"] for r in corpus["cases"] if r["split"] == "held_out"}
    assert len(calibration) == 32 and len(heldout) == 16
    assert not calibration & heldout
    assert corpus["origin"] == "synthetic_authored"


def test_saved_corpus_and_report_are_reproducible(trial):
    stem = "classification" if trial[0]["role"] == roles.BOUNDED_CLASSIFICATION else "adjudication"
    corpus = json.loads((FIXTURES / f"representative-{stem}-corpus.json").read_text())
    reports = json.loads((FIXTURES / "representative-positive-control-reports.json").read_text())
    assert corpus == trial[0]
    assert reports[stem] == score(trial)


def test_real_verify_runner_observes_both_selftests():
    import verify

    result = verify.run_selftests(
        ["transport_qualification", "transport_qualification_corpus"], jobs=1
    )
    assert result["failed"] == {}
    assert result["skipped"] == {}
    assert set(result["ok"]) == {"transport_qualification", "transport_qualification_corpus"}
