from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

import capabilities
import capability_compiler as compiler
from capability_ir import CapabilityIR, Lifecycle, SourceOccurrence, stable_hash
import feedback
import roles


def _candidate(*, now: int | None = None) -> CapabilityIR:
    current = int(time.time()) if now is None else int(now)
    occurrences = tuple(
        SourceOccurrence(
            event_id=f"event-{index}",
            event_refs=tuple(f"event-{index}-{phase}" for phase in range(7)),
            occurred_at=current - index,
            subject_id=f"subject:{index}",
            observation_id=f"observation:{index}",
            family_id=f"family:{index}",
            canonical_target=f"owner/repo#{index}",
            repository=f"owner/repo-{index}",
            task_type="review",
            normalized_spec_hash=stable_hash("spec", index),
            base_sha=f"base-{index}",
            profile_id=f"profile-{index}",
            arm_id=f"arm-{index}",
            attempt_id=f"attempt-{index}",
            artifact_refs=(stable_hash("artifact", index),),
            verification_ref=stable_hash("verification", index),
            outcome_ref=stable_hash("outcome", index),
            durability_ref=stable_hash("durability", index),
        )
        for index in range(1, 4)
    )
    role_contract = {
        "schema": compiler.ROLE_SOURCE_SCHEMA,
        "version": compiler.ROLE_VERSION,
        "name": "evidence-gap-prioritizer",
        "description": "Compare bounded evidence gaps and recommend the next investigation priority.",
        "authority": "Advisory only: compare supplied evidence and recommend one bounded investigation priority.",
        "route_as": "review",
        "input_schema": {
            "task_type": {
                "type": "string",
                "required": True,
                "enum": ["review"],
                "max_length": 32,
            },
            "evidence_refs": {
                "type": "string_list",
                "required": True,
                "max_items": 8,
            },
            "question": {"type": "string", "required": True, "max_length": 500},
        },
        "output_schema": {
            "decision": {
                "type": "string",
                "required": True,
                "enum": ["inspect", "defer"],
                "max_length": 32,
            },
            "evidence_refs": {
                "type": "string_list",
                "required": True,
                "max_items": 8,
            },
            "rationale": {"type": "string", "required": True, "max_length": 800},
        },
        "selector": {
            "field": "task_type",
            "operator": "equals",
            "value": "review",
            "max_matches_per_cycle": 1,
        },
        "capacity_policy": {
            "selection": "router_capacity_and_learned_weights",
            "reserve_policy": "preserve",
            "max_invocations_per_cycle": 1,
        },
        "prompt_protocol": {
            "version": 1,
            "purpose": "Prioritize a supplied evidence gap using judgment bounded by cited evidence.",
            "instructions": [
                "Use only the supplied evidence references.",
                "Return one advisory decision with a concise rationale.",
            ],
            "max_context_chars": 4096,
            "output_format": "strict_json",
        },
        "expires_at": current + 30 * 86400,
        "kill_switch": {
            "env": "ORCH_GENERATED_ROLE_EVIDENCE_GAP_PRIORITIZER_DISABLED",
            "disabled_value": "1",
        },
        "rollback": {
            "action": "retire_generated_role_and_restore_predecessor",
            "predecessor": "role-adjudicator",
            "reason": "shadow evidence expired, regressed, or remained inconclusive",
        },
    }
    return CapabilityIR(
        capability_id="capability:evidence-gap-prioritizer",
        fingerprint=stable_hash("candidate", "evidence-gap-prioritizer"),
        semantic_fingerprint=stable_hash("semantic", "evidence-gap-prioritizer"),
        output_contract_fingerprint=stable_hash("output", role_contract["output_schema"]),
        kind_proposal="role",
        owner_proposal="orchestrator",
        source_occurrences=occurrences,
        counterexamples=(),
        independent_subjects=tuple(item.subject_id for item in occurrences),
        independent_repositories=tuple(item.repository for item in occurrences),
        selector=role_contract["selector"],
        graph={
            "phase_order": [
                "trigger", "decision", "execution", "artifact",
                "verification", "outcome", "durability",
            ],
            "edges": [],
            "decision_mode": "judgment",
            "requires_judgment": True,
            "role_contract": role_contract,
        },
        artifact_refs=tuple(
            ref for item in occurrences for ref in item.artifact_refs
        ),
        gates={"durable_result_required": True},
        telemetry={
            "distinct_subject_count": 3,
            "effective_subject_count": 3.0,
            "negative_ratio": 0.0,
        },
        lifecycle=Lifecycle(expires_at=current + 30 * 86400),
        predecessor="role-adjudicator",
    )


@pytest.fixture
def generated_role_manifest() -> dict:
    compiled = compiler.compile_role_capability(_candidate())
    manifest = compiled["manifest"]
    # Deliberate-break contract: deleting only output_schema must fail here with
    # exactly "AssertionError: generated role lacks output schema".
    assert "output_schema" in manifest, "generated role lacks output schema"
    return manifest


@pytest.fixture(autouse=True)
def _clean_generated_registry() -> None:
    roles.unregister_generated_role("evidence-gap-prioritizer")
    roles.reset_role_invocation_counts()
    yield
    roles.unregister_generated_role("evidence-gap-prioritizer")
    roles.reset_role_invocation_counts()


def test_judgment_pattern_compiles_to_valid_shadow_role(
    generated_role_manifest: dict,
) -> None:
    manifest = compiler._validate_generated_role_manifest(generated_role_manifest)
    role = roles.role_from_generated_manifest(manifest)
    proposal = {
        "decision": "inspect",
        "evidence_refs": ["artifact:verification-1"],
        "rationale": "The named verification result is the smallest decisive evidence gap.",
    }
    assert role.validate(proposal) == []
    assert role.generated and role.mode is None
    assert role.output_keys == ("decision", "evidence_refs", "rationale")
    assert role.prompt_hash == manifest["prompt_hash"]
    assert manifest["shadow_only"] is True
    assert manifest["profile_agnostic"] is True
    assert not any(
        key in json.dumps(manifest, sort_keys=True).lower()
        for key in ('"provider"', '"model"', '"profile"', '"agent"', '"backend"')
    )


def test_deterministic_pattern_is_not_role() -> None:
    candidate = _candidate().to_dict()
    candidate["graph"]["decision_mode"] = "deterministic"
    candidate["graph"]["requires_judgment"] = False
    decision = compiler.compile_role_candidate(candidate)
    assert decision["status"] == "routed"
    assert decision["target"] == "workflow"
    assert decision["executable"] is False
    assert "deterministic pattern: route to workflow" in decision["rejection_reasons"]


def test_generated_role_shadow_runner_records_accepted_lineage_and_lifecycle(
    generated_role_manifest: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback" / "orchestrator.db")
    target_run = "generated-role-target"
    feedback.record_run(target_run, "owner/repo#18", "review", "codex")
    feedback.record_outcome(
        target_run,
        verifier_verdict="PASS",
        adjudicated_verdict="PASS",
        merged=True,
        durability="durable",
    )
    ledger = tmp_path / "capabilities.json"
    result = roles.run_generated_shadow_role(
        generated_role_manifest,
        context={
            "task_type": "review",
            "evidence_refs": ["artifact:verification-1"],
            "question": "Which missing observation is most likely to resolve the dispute?",
        },
        proposal={
            "decision": "inspect",
            "evidence_refs": ["artifact:verification-1"],
            "rationale": "Inspect the named verifier artifact before changing the baseline.",
        },
        target="owner/repo#18",
        backend_agent="codex",
        influenced_run_ids=[target_run],
        ledger_path=ledger,
        env={},
    )
    assert result["accepted"] and result["shadow"]
    assert result["baseline_changed"] is False
    assert result["outcome_ref"]
    with feedback._conn() as conn:
        edge = conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,merged,durability "
            "FROM influence_edges WHERE target_run_id=? AND influence_type='role'",
            (target_run,),
        ).fetchone()
    assert edge == (1, 0, "PASS", 1, "durable")
    cap = capabilities.load(ledger, create=False)[generated_role_manifest["capability_id"]]
    assert cap["status"] == "shadow"
    assert cap["expiry"] == generated_role_manifest["lifecycle"]["expires_at"]
    assert cap["kill_switch"] == generated_role_manifest["lifecycle"]["kill_switch"]
    assert cap["rollback"] == generated_role_manifest["lifecycle"]["rollback"]
    assert cap["predecessor"] == "role-adjudicator"
    assert result["outcome_ref"] in cap["outcome_links"]


def test_rejected_generated_role_is_counterfactual(
    generated_role_manifest: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback" / "orchestrator.db")
    target_run = "generated-role-rejected-target"
    feedback.record_run(target_run, "owner/repo#19", "review", "codex")
    result = roles.run_generated_shadow_role(
        generated_role_manifest,
        context={
            "task_type": "review",
            "evidence_refs": ["artifact:verification-2"],
            "question": "Which evidence gap should be inspected?",
        },
        proposal={"decision": "inspect"},
        target="owner/repo#19",
        backend_agent="codex",
        influenced_run_ids=[target_run],
        ledger_path=tmp_path / "capabilities.json",
        env={},
    )
    assert not result["accepted"]
    assert "output is missing required key: evidence_refs" in result["errors"]
    with feedback._conn() as conn:
        edge = conn.execute(
            "SELECT accepted,counterfactual FROM influence_edges WHERE target_run_id=?",
            (target_run,),
        ).fetchone()
    assert edge == (0, 1)


def test_expiry_kill_switch_and_capacity_prevent_invocation(
    generated_role_manifest: dict, tmp_path: Path
) -> None:
    for env, current, available, reason in (
        ({generated_role_manifest["lifecycle"]["kill_switch"]["env"]: "1"}, None, True, "shadow_gate_disabled"),
        ({}, generated_role_manifest["lifecycle"]["expires_at"], True, "shadow_gate_disabled"),
        ({}, None, False, "no_role_capacity"),
    ):
        roles.unregister_generated_role("evidence-gap-prioritizer")
        roles.reset_role_invocation_counts()
        result = roles.run_generated_shadow_role(
            generated_role_manifest,
            context={
                "task_type": "review",
                "evidence_refs": ["artifact:one"],
                "question": "What should be inspected?",
            },
            proposal={
                "decision": "inspect",
                "evidence_refs": ["artifact:one"],
                "rationale": "Inspect the only named artifact.",
            },
            target="owner/repo#20",
            backend_agent="codex",
            influenced_run_ids=[],
            ledger_path=tmp_path / f"{reason}-{current or 0}.json",
            env=env,
            capacity_available=available,
            now=current,
        )
        assert result["role_run_id"] is None
        assert result["selector"]["reason"] == reason


def test_missing_output_schema_has_exact_deliberate_break_message(
    generated_role_manifest: dict,
) -> None:
    broken = copy.deepcopy(generated_role_manifest)
    broken.pop("output_schema")
    with pytest.raises(AssertionError, match="^generated role lacks output schema$"):
        compiler._validate_generated_role_manifest(broken)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda raw: raw["graph"]["role_contract"].update(
                authority="May merge and push the recommended change."
            ),
            "destructive or unbounded role authority",
        ),
        (
            lambda raw: raw["graph"]["role_contract"]["capacity_policy"].update(
                max_invocations_per_cycle=10
            ),
            "generated role capacity policy is not bounded",
        ),
        (
            lambda raw: raw["graph"]["role_contract"]["output_schema"].update(
                merge={"type": "boolean", "required": True}
            ),
            "destructive generated role contract",
        ),
        (
            lambda raw: raw["graph"]["role_contract"]["prompt_protocol"].update(
                purpose="Use token=secretvalue123456 to inspect evidence."
            ),
            "secret-bearing role candidate",
        ),
    ],
)
def test_unsafe_role_candidates_are_rejected(mutation, reason: str) -> None:
    candidate = _candidate().to_dict()
    mutation(candidate)
    decision = compiler.compile_role_candidate(candidate)
    assert decision["status"] == "rejected"
    assert decision["executable"] is False
    assert reason in decision["rejection_reasons"]
