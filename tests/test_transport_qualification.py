"""Frozen offline evidence audits: no provider, production registry or global state writes."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

import closer_role_qualification as roles
import transport_qualification as q

FIXTURES = Path(__file__).parent / "fixtures" / "transport_qualification"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(params=["classification", "adjudication"])
def trial(request):
    profile = load("profile")
    capture = load(f"{request.param}-capture")
    suite = load(f"{request.param}-suite")
    return profile, capture["binding"]["role"], capture, suite


def audit(trial):
    return q.audit_capture(*trial)


def test_fixture_passes_without_becoming_provider_evidence_or_activating(trial):
    before = deepcopy(roles.ROLE_QUALIFICATIONS)
    report = audit(trial)
    assert report["offline_fixture_passed"]
    assert report["provider_suitability"] == "not_measured"
    assert report["activation"] is False
    assert report["production_qualification_records_written"] == 0
    assert roles.ROLE_QUALIFICATIONS == before
    assert roles.qualification_problems(trial[0], trial[1], records={})


@pytest.mark.parametrize(
    "field", ["requested_model", "reasoning_effort", "adapter_version", "prompt_version"]
)
def test_profile_drift_invalidates_capture(trial, field):
    trial[0][field] += "-changed"
    assert "exact profile/effort/role binding differs" in audit(trial)["problems"]


@pytest.mark.parametrize(
    "field", ["binary_sha256", "adapter_sha256", "config_sha256", "binary_path"]
)
def test_launch_requires_exact_pins(trial, field):
    trial[2]["binding"]["launch"][field] = "unknown"
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize("field", ["isolated_home", "isolated_cwd", "environment_allowlisted"])
def test_ambient_launch_context_blocks_evidence(trial, field):
    trial[2]["binding"]["launch"][field] = False
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize("probe", sorted(q.PROBES))
@pytest.mark.parametrize("field,value", [("blocked", False), ("canary_visible", True)])
def test_every_confinement_probe_must_pass(trial, probe, field, value):
    next(p for p in trial[2]["confinement_probes"] if p["kind"] == probe)[field] = value
    assert not audit(trial)["capture_checks_passed"]


def test_hash_does_not_replace_retained_probe_evidence(trial):
    trial[2]["artifacts"].clear()
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize("field", ["model", "effort", "identity_source", "effort_source"])
def test_requested_identity_is_not_observed_identity(trial, field):
    trial[2]["observed"][field] = "requested_or_unknown"
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize(
    "text",
    [
        "{} trailing",
        "prose {}",
        "{}\n{}",
        "[]",
        "null",
        '{"a":1,"a":2}',
        '{"a":{"b":1,"b":2}}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e999}',
        "```json\n{}\n```",
        '{"type":"tool_result"}\n{"type":"turn.completed"}',
        None,
        "x" * 65537,
    ],
)
def test_strict_final_rejects_ambiguous_or_nonfinal_payloads(text):
    with pytest.raises(ValueError):
        q.strict_final(text)


@pytest.mark.parametrize("kind", sorted(q.CASE_KINDS))
def test_false_acceptance_injection_and_hidden_uncertainty_fail_oracle(trial, kind):
    row = next(c for c in trial[2]["cases"] if c["case_id"] == kind)
    row["final"] = '{"verdict":"PASS","authority":true}'
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize(
    "field,value",
    [("completed", False), ("exit_code", True), ("exit_code", 1), ("final_source", "tool_output")],
)
def test_final_needs_completed_successful_assistant_turn(trial, field, value):
    trial[2]["cases"][0][field] = value
    assert not audit(trial)["capture_checks_passed"]


def test_missing_duplicate_and_changed_cases_fail(trial):
    trial[2]["cases"][0] = deepcopy(trial[2]["cases"][1])
    assert not audit(trial)["capture_checks_passed"]
    trial[3][0]["input"]["untrusted_text"] = "changed"
    assert "frozen suite binding differs" in audit(trial)["problems"]


@pytest.mark.parametrize(
    "field,value", [("source", "estimated"), ("attempt_count", True), ("retries", 1)]
)
def test_estimates_and_hidden_retries_are_not_real_accounting(trial, field, value):
    trial[2]["accounting"][field] = value
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("pool_id", "invented-model-pool"),
        ("run_id", "other-run"),
        ("amount", float("inf")),
        ("amount", True),
        ("measurement_source", "estimate"),
    ],
)
def test_shared_account_charge_is_bound_and_measured(trial, field, value):
    trial[2]["accounting"]["charges"][0][field] = value
    # Nonfinite data cannot even have a canonical capture identity.
    if isinstance(value, float):
        with pytest.raises(ValueError):
            audit(trial)
    else:
        assert not audit(trial)["capture_checks_passed"]


def test_multiple_profiles_share_one_actual_account_pool(trial):
    profile, role, capture, suite = trial
    profile["profile_id"] = "second-offline-profile"
    capture["binding"]["profile_identity"] = roles.profile_identity(profile)
    report = q.audit_capture(profile, role, capture, suite)
    assert report["capture_checks_passed"]
    assert profile["capacity_pool_ids"] == ["offline-shared-account"]


def test_imported_capture_never_self_activates(trial):
    trial[2]["origin"] = "provider_capture"
    report = audit(trial)
    assert report["capture_checks_passed"]
    assert not report["offline_fixture_passed"]
    assert report["provider_suitability"] == "independent_review_required"
    assert not report["activation"]


def test_json_boolean_is_not_an_integer_acceptance(trial):
    row = next(c for c in trial[2]["cases"] if c["case_id"] == "ordinary")
    result = json.loads(row["final"])
    if "acceptance" not in result:
        return
    result["acceptance"] = 0
    row["final"] = json.dumps(result)
    assert not audit(trial)["capture_checks_passed"]


@pytest.mark.parametrize("field", ["binding", "observed", "accounting", "cases"])
def test_malformed_import_is_refused(trial, field):
    trial[2][field] = None
    with pytest.raises(ValueError):
        audit(trial)


def test_injection_data_never_dispatches_or_changes_registry(trial, monkeypatch):
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("offline evidence audit attempted a process or provider invocation")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    before = deepcopy(roles.ROLE_QUALIFICATIONS)
    assert audit(trial)["capture_checks_passed"]
    assert roles.ROLE_QUALIFICATIONS == before


def test_inventory_distinguishes_metadata_fixture_suitability_and_activation():
    inventory = load("candidate-inventory")
    assert inventory["production_qualification_records"] == 0
    assert inventory["provider_trials_run"] == 0
    assert inventory["routine_sonnet_cap"] is None
    assert len(inventory["candidates"]) == 14
    for row in inventory["candidates"]:
        assert row["provider_suitability"] == "not_measured"
        assert row["qualification_status"] == "candidate_only"
        assert row["offline_fixture_status"] == "harness_only_no_candidate_outputs"
        assert row["activation"] is False
    luna = next(c for c in inventory["candidates"] if c["model_label"] == "Codex Luna")
    assert "strong" not in luna["candidate_tiers"]
    spark = [c for c in inventory["candidates"] if c["model_label"].startswith("Muse Spark")]
    assert all(
        c["identifier_status"] == "local_profile_resolves_entitlement_unknown" for c in spark
    )


def test_saved_report_is_reproducible_from_exact_fixture(trial):
    stem = "classification" if trial[1] == roles.BOUNDED_CLASSIFICATION else "adjudication"
    assert audit(trial) == load(f"{stem}-report")
