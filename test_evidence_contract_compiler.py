from __future__ import annotations

import copy

import pytest

import capability_compiler
import evidence_schema
import local_verify
import runtime_ac
import runtime_ac_panel
import exp_abcd

NOW = 2_000_000_000
NAMED_TEST = (
    "pytest:test_evidence_contract_compiler.py::"
    "test_paraphrased_gaps_form_one_distinct_subject_candidate"
)


@pytest.fixture
def gap_rows() -> list[dict]:
    rows = [
        {
            "ts": NOW - 30,
            "ref": "run-a",
            "evaluator": "sol",
            "gap": "Need pytest execution output for the focused suite.",
            "subject_id": "subject-1",
            "spec_hash": "sha256:spec-1",
            "polarity": "positive",
        },
        {
            "ts": NOW - 20,
            "ref": "run-b",
            "evaluator": "terra",
            "gap": "CLI smoke results were not provided.",
            "subject_id": "subject-2",
            "spec_hash": "sha256:spec-2",
            "polarity": "positive",
        },
        {
            "ts": NOW - 10,
            "ref": "run-c",
            "evaluator": "luna",
            "gap": "No deliberate-break or negative control proof that named tests fail.",
            "subject_id": "subject-3",
            "spec_hash": "sha256:spec-3",
            "polarity": "positive",
        },
    ]
    # Raw frequency from one subject is intentionally much larger than the
    # independent signal. It must still contribute exactly one effective row.
    for index in range(5):
        rows.append(
            {
                **rows[0],
                "ts": NOW - 100 - index,
                "ref": f"repeat-{index}",
                "evaluator": "sol",
            }
        )
    return rows


def _candidate(gap_rows: list[dict]) -> dict:
    candidates = evidence_schema.cluster_gap_rows(gap_rows, now=NOW)
    assert candidates, "one subject met promotion threshold"
    return candidates[0]


def _plan(gap_rows: list[dict]) -> dict:
    return capability_compiler.compile_first_shadow_contract(_candidate(gap_rows), now=NOW)


def _capture_result(named_test_id: str = NAMED_TEST) -> dict:
    return {
        "named_test_id": named_test_id,
        "status": "PASS",
        "result_hash": "sha256:" + "0" * 64,
        "deliberate_break_status": "PASS",
        "duration_ms": 17,
    }


def test_paraphrased_gaps_form_one_distinct_subject_candidate(gap_rows: list[dict]) -> None:
    candidate = _candidate(gap_rows)
    assert candidate["name"] == "named_test_smoke_deliberate_break"
    assert candidate["effective_subject_count"] == 3.0
    assert candidate["raw_row_count"] == 8
    assert candidate["independent_subjects"] == ["subject-1", "subject-2", "subject-3"]
    assert candidate["lifecycle"]["candidate_only"] is True
    assert candidate["lifecycle"]["promotion_allowed"] is False


def test_repeated_one_subject_rows_are_one_effective_observation(gap_rows: list[dict]) -> None:
    for row in gap_rows:
        row["subject_id"] = "subject-1"
    assert evidence_schema.cluster_gap_rows(gap_rows, now=NOW) == []


def test_first_shadow_is_inert_and_integrates_named_capture_hooks(gap_rows: list[dict]) -> None:
    plan = _plan(gap_rows)
    assert plan["lifecycle"] == {
        "state": "compiled_shadow_candidate",
        "candidate_only": True,
        "executable": False,
        "promotion_allowed": False,
    }
    local_capture = local_verify.capture_evidence_contract(plan, _capture_result())
    assert local_capture["bounded"] is True
    assert local_capture["producer"] == "local_verify"
    assert "raw_output" not in local_capture["evidence"]

    source = capability_compiler.build_evidence_contract_source(
        _candidate(gap_rows),
        capture_hook="runtime_ac.named_test_capture",
        named_test_id="runtime-ac:focused-gate",
        live_gate_id="runtime-ac:runtime-gate",
        now=NOW,
    )
    runtime_plan = capability_compiler.compile_evidence_contract(source)
    runtime_capture = runtime_ac.capture_evidence_contract(
        runtime_plan, _capture_result("runtime-ac:focused-gate")
    )
    assert runtime_capture["producer"] == "runtime_ac"


@pytest.mark.parametrize("bad_key", ["command", "raw_output", "api_token", "secret"])
def test_contract_rejects_commands_raw_output_and_secrets(
    gap_rows: list[dict], bad_key: str
) -> None:
    source = capability_compiler.build_evidence_contract_source(_candidate(gap_rows), now=NOW)
    source["candidate"] = copy.deepcopy(source["candidate"])
    source["candidate"][bad_key] = "must never be retained"
    with pytest.raises(capability_compiler.EvidenceContractCompileError, match="forbidden"):
        capability_compiler.compile_evidence_contract(source)


def test_contract_rejects_unallowlisted_capture_hook(gap_rows: list[dict]) -> None:
    source = capability_compiler.build_evidence_contract_source(
        _candidate(gap_rows), capture_hook="shell.run", now=NOW
    )
    with pytest.raises(capability_compiler.EvidenceContractCompileError, match="not allowlisted"):
        capability_compiler.compile_evidence_contract(source)


def test_capture_rejects_raw_output(gap_rows: list[dict]) -> None:
    result = _capture_result()
    result["raw_output"] = "sensitive log"
    with pytest.raises(ValueError, match="forbidden"):
        local_verify.capture_evidence_contract(_plan(gap_rows), result)


def test_influence_is_later_outcomes_not_citation_count(gap_rows: list[dict]) -> None:
    plan = _plan(gap_rows)
    measured = capability_compiler.measure_contract_influence(
        plan,
        [
            {
                "cited": True,
                "later_agreement_delta": 0,
                "later_decisiveness_delta": 0,
                "later_gap_delta": 0,
                "rework": False,
                "durability": "unknown",
            },
            {
                "cited": True,
                "later_agreement_delta": 0.2,
                "later_decisiveness_delta": 0.1,
                "later_gap_delta": -1,
                "rework": False,
                "durability": "durable",
            },
        ],
        now=NOW + 60,
    )
    assert measured["citation_count"] == 2
    assert measured["influential_use_count"] == 1


def test_expired_no_influence_retires_and_rolls_back(gap_rows: list[dict]) -> None:
    plan = _plan(gap_rows)
    measured = capability_compiler.measure_contract_influence(
        plan,
        [{"cited": True, "durability": "unknown"}],
        now=plan["expires_at"],
    )
    assert measured["state"] == "retired"
    assert measured["retirement_reason"] == "expired_without_influence"
    assert measured["rollback"] == {
        "performed": True,
        "capture_hook_enabled": False,
        "action": "disable_capture_hook_and_retire_candidate",
    }


def test_evaluator_prompt_and_generated_issue_are_contract_aware(gap_rows: list[dict]) -> None:
    plan = _plan(gap_rows)
    prompt = capability_compiler.evaluator_prompt_fragment(plan)
    assert "cited_evidence_contracts" in prompt
    assert "citation alone is not influence" in prompt.lower()
    panel_prompt = runtime_ac_panel.build_review_prompt(
        copy.deepcopy(runtime_ac.RUNTIME_AC_SCHEMA_EXAMPLE),
        {"verification_id": "example", "target": "owner/repo#1", "verdict": "PASS"},
        evidence_contract_plan=plan,
    )
    experiment_prompt = exp_abcd.evaluate_prompt(
        "frozen spec", {"A": "diff A", "B": "diff B"}, evidence_contract_plan=plan
    )
    assert plan["plan_id"] in panel_prompt
    assert plan["plan_id"] in experiment_prompt
    assert "cited_evidence_contracts" in panel_prompt
    assert "cited_evidence_contracts" in experiment_prompt
    features = capability_compiler.validate_contract_issue_format(plan)
    assert features["has_acceptance_criteria"] is True
    assert features["has_test_instructions"] is True
    assert features["has_non_goals"] is True
