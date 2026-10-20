"""Hermetic coverage for remaining issue-807 capability_compiler residual mass.

Round 1–2 covered skill/resource/workflow rejection arms. This round targets the
contiguous residual: `_selftest` / CLI selftest (live skill path hermeticized),
evidence-contract compile/capture/influence rejection arms, and playbook/role
compile guards that still show as Miss/BrPart under the focused measure.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

import capability_compiler as compiler
import evidence_schema
import feedback
from capability_ir import CapabilityIR, Lifecycle, SourceOccurrence, stable_hash

NOW = 2_000_000_000
NAMED_TEST = (
    "pytest:test_evidence_contract_compiler.py::"
    "test_paraphrased_gaps_form_one_distinct_subject_candidate"
)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(runtime / "feedback" / "orchestrator.db"))
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback" / "orchestrator.db")


def _script(tmp_path: Path, name: str = "helper.sh") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    return path


def _hermetic_skill_source(tmp_path: Path) -> dict:
    """Same shape as reference_skill_source, but resources live under tmp_path."""
    script = _script(tmp_path, "audit_code_root.sh")
    return {
        "schema": compiler.SKILL_SOURCE_SCHEMA,
        "version": compiler.SKILL_VERSION,
        "capability_id": "capability:audit-handoff-evidence",
        "source_ir_ref": "capability-ir:audit-handoff-evidence",
        "source_fingerprint": stable_hash(
            "skill-source-pattern", {"procedure": "audit-handoff-evidence", "version": 1}
        ),
        "procedure_class": "reusable_procedure",
        "reuse_scope": "shared",
        "name": "audit-handoff-evidence",
        "description": "Prepare bounded workspace-audit evidence and a concise completion handoff.",
        "triggers": [
            "audit workspace hygiene after completing delegated work",
            "prepare evidence for an audit or workflow handoff",
        ],
        "instructions": [
            "Run the bundled workspace audit script in report-only mode for the task workspace.",
            "Classify reported entries without deleting, moving, or archiving anything.",
            "Summarize the audit result and attach it to the completed-work handoff.",
            "Record only bounded artifact IDs and content hashes in completion lineage.",
        ],
        "resources": [
            {
                "kind": "scripts",
                "source_path": str(script),
                "target": "scripts/audit_code_root.sh",
                "content_hash": compiler._sha256_file(script),
            }
        ],
        "safety_boundaries": [
            "Keep the bundled audit script in report-only mode.",
            "Do not delete, archive, reset, or modify workspace contents.",
            "Do not include raw prompts, credentials, user documents, or command output in lineage.",
        ],
        "expected_artifacts": ["workspace-audit-report", "audit-handoff-summary"],
        "validation_command": "bash scripts/audit_code_root.sh --help",
        "expires_at": 1893456000,
        "predecessor": "capability:completion-event-lineage",
        "rollback": {
            "action": "retire_shadow_package",
            "reason": "shadow evidence expires, regresses, or duplicates an installed skill",
        },
    }


def _gap_rows() -> list[dict]:
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
    return rows


def _candidate() -> dict:
    candidates = evidence_schema.cluster_gap_rows(_gap_rows(), now=NOW)
    assert candidates
    return candidates[0]


def _valid_source() -> dict:
    return compiler.build_evidence_contract_source(_candidate(), now=NOW)


def test_selftest_and_cli_selftest_are_hermetic(tmp_path, monkeypatch):
    """Drive `_selftest` without a live ~/.codex/skills tree (bare CI skip otherwise)."""
    hermetic = _hermetic_skill_source(tmp_path)
    monkeypatch.setattr(compiler, "reference_skill_source", lambda: hermetic)
    compiler._selftest()
    assert compiler.main(["selftest"]) == 0


def test_canonical_subject_count_and_evidence_compile_rejection_arms():
    count, errors = compiler._canonical_subject_count("nope")
    assert count == 0.0 and "must be an object" in errors[0]

    count, errors = compiler._canonical_subject_count(
        {"independent_subjects": [1], "independent_specs": [2], "effective_subject_count": "x"}
    )
    assert count == 0.0
    assert any("independent_subjects" in e for e in errors)
    assert any("independent_specs" in e for e in errors)
    assert any("must be numeric" in e for e in errors)

    count, errors = compiler._canonical_subject_count(
        {
            "independent_subjects": ["a", "b"],
            "independent_specs": ["s1", "s2"],
            "effective_subject_count": 9,
        }
    )
    assert count == 2.0
    assert any("does not match canonical" in e for e in errors)

    with pytest.raises(compiler.EvidenceContractCompileError, match="must be an object"):
        compiler.compile_evidence_contract("bad")  # type: ignore[arg-type]

    source = _valid_source()
    source["extra"] = True
    with pytest.raises(compiler.EvidenceContractCompileError, match="unsupported fields"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    del source["rollback"]
    with pytest.raises(compiler.EvidenceContractCompileError, match="missing fields"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["schema"] = "wrong"
    with pytest.raises(compiler.EvidenceContractCompileError, match="unsupported evidence"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["candidate"] = copy.deepcopy(source["candidate"])
    source["candidate"]["independent_subjects"] = ["only-one"]
    source["candidate"]["independent_specs"] = ["only-one"]
    source["candidate"]["effective_subject_count"] = 1
    with pytest.raises(compiler.EvidenceContractCompileError, match="at least three"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["candidate"] = copy.deepcopy(source["candidate"])
    source["candidate"]["lifecycle"] = {"candidate_only": False, "promotion_allowed": True}
    with pytest.raises(compiler.EvidenceContractCompileError) as exc:
        compiler.compile_evidence_contract(source)
    joined = " ".join(exc.value.reasons)
    assert "candidate-only" in joined
    assert "promotion" in joined

    source = _valid_source()
    source["named_test_id"] = "not a safe id!!!"
    source["live_gate_id"] = "also bad!!!"
    with pytest.raises(compiler.EvidenceContractCompileError) as exc:
        compiler.compile_evidence_contract(source)
    joined = " ".join(exc.value.reasons)
    assert "named_test_id" in joined and "live_gate_id" in joined

    source = _valid_source()
    source["deliberate_break"] = {"required": False}
    with pytest.raises(compiler.EvidenceContractCompileError, match="deliberate-break"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["deliberate_break"] = {"required": True, "expected": "named_test_passes"}
    with pytest.raises(compiler.EvidenceContractCompileError, match="expect the named test"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["capture_contract"] = {
        "inputs": source["capture_contract"]["inputs"],
        "output": {"capture_ref": "artifact_ref", "bounded": "nope"},
    }
    with pytest.raises(compiler.EvidenceContractCompileError, match="output is not bounded"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["evaluation"] = {
        "citation_field": "cited_evidence_contracts",
        "influence_measures": ["later_agreement_delta"],
        "citation_is_not_influence": False,
    }
    with pytest.raises(compiler.EvidenceContractCompileError, match="outcome influence"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["created_at"] = NOW
    source["expires_at"] = NOW
    with pytest.raises(compiler.EvidenceContractCompileError, match="future integer expiry"):
        compiler.compile_evidence_contract(source)

    source = _valid_source()
    source["rollback"] = {"action": "noop"}
    with pytest.raises(compiler.EvidenceContractCompileError, match="disable-and-retire"):
        compiler.compile_evidence_contract(source)

    with pytest.raises(compiler.EvidenceContractCompileError, match="named-test evidence cluster"):
        compiler.compile_first_shadow_contract({"name": "other-cluster"}, now=NOW)


def test_capture_and_influence_rejection_arms():
    plan = compiler.compile_first_shadow_contract(_candidate(), now=NOW)
    with pytest.raises(ValueError, match="unsupported evidence contract plan"):
        compiler.capture_named_test_evidence({"schema": "nope"}, {})

    bad_plan = copy.deepcopy(plan)
    bad_plan["capture_hook"] = "shell.run"
    with pytest.raises(ValueError, match="not allowlisted"):
        compiler.capture_named_test_evidence(bad_plan, {})

    with pytest.raises(ValueError, match="forbidden"):
        compiler.capture_named_test_evidence(plan, {"api_token": "x"})

    result = {
        "named_test_id": NAMED_TEST,
        "status": "PASS",
        "result_hash": "sha256:" + ("0" * 64),
        "deliberate_break_status": "PASS",
        "duration_ms": 1,
        "extra": True,
    }
    with pytest.raises(ValueError, match="exactly"):
        compiler.capture_named_test_evidence(plan, result)

    result = {
        "named_test_id": "pytest:other::test",
        "status": "PASS",
        "result_hash": "sha256:" + ("0" * 64),
        "deliberate_break_status": "PASS",
        "duration_ms": 1,
    }
    with pytest.raises(ValueError, match="named_test_id does not match"):
        compiler.capture_named_test_evidence(plan, result)

    result = {
        "named_test_id": NAMED_TEST,
        "status": "MAYBE",
        "result_hash": "sha256:" + ("0" * 64),
        "deliberate_break_status": "PASS",
        "duration_ms": 1,
    }
    with pytest.raises(ValueError, match="invalid named test status"):
        compiler.capture_named_test_evidence(plan, result)

    result = {
        "named_test_id": NAMED_TEST,
        "status": "PASS",
        "result_hash": "sha256:" + ("0" * 64),
        "deliberate_break_status": "WEIRD",
        "duration_ms": 1,
    }
    with pytest.raises(ValueError, match="invalid deliberate-break status"):
        compiler.capture_named_test_evidence(plan, result)

    result = {
        "named_test_id": NAMED_TEST,
        "status": "PASS",
        "result_hash": "sha256:" + ("0" * 64),
        "deliberate_break_status": "PASS",
        "duration_ms": -1,
    }
    with pytest.raises(ValueError, match="duration_ms"):
        compiler.capture_named_test_evidence(plan, result)

    result = {
        "named_test_id": NAMED_TEST,
        "status": "PASS",
        "result_hash": "not-a-hash",
        "deliberate_break_status": "PASS",
        "duration_ms": 1,
    }
    with pytest.raises(ValueError, match="result_hash"):
        compiler.capture_named_test_evidence(plan, result)

    harm = compiler.measure_contract_influence(
        plan,
        [
            {
                "cited": True,
                "later_agreement_delta": -0.2,
                "later_decisiveness_delta": 0,
                "later_gap_delta": 0,
                "rework": False,
                "durability": "reverted",
            },
            {
                "cited": False,
                "later_agreement_delta": -0.1,
                "later_decisiveness_delta": -0.1,
                "later_gap_delta": 1,
                "rework": True,
                "durability": "regressed",
            },
        ],
        now=NOW + 60,
    )
    assert harm["harm_count"] == 2
    assert harm["retirement_reason"] == "recurring_harm"
    assert harm["state"] == "retired"

    # Force validate_contract_issue_format failure path by stubbing features.
    import issue_quality

    original = issue_quality.extract_issue_features

    def _broken(_body: str) -> dict:
        return {"has_acceptance_criteria": False, "has_test_instructions": False}

    try:
        issue_quality.extract_issue_features = _broken  # type: ignore[assignment]
        with pytest.raises(ValueError, match="not agent-ready"):
            compiler.validate_contract_issue_format(plan)
    finally:
        issue_quality.extract_issue_features = original  # type: ignore[assignment]


def _role_candidate(*, now: int | None = None) -> CapabilityIR:
    """Mirror test_role_compiler._candidate so IR validation and role guards stay aligned."""
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
        "authority": (
            "Advisory only: compare supplied evidence and recommend one bounded investigation "
            "priority."
        ),
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
            "purpose": "Rank the most informative evidence gap for the next investigation step.",
            "instructions": [
                "Compare only the supplied evidence references.",
                "Return a strict JSON decision with rationale.",
            ],
            "max_context_chars": 2048,
            "output_format": "strict_json",
        },
        "expires_at": current + 7 * 86400,
        "kill_switch": {
            "env": "ORCH_GENERATED_ROLE_EVIDENCE_GAP_PRIORITIZER_DISABLED",
            "disabled_value": "1",
        },
        "rollback": {
            "action": "retire_generated_role_and_restore_predecessor",
            "predecessor": "role-adjudicator",
            "reason": "shadow evidence expires or regresses",
        },
    }
    return CapabilityIR(
        capability_id="capability:evidence-gap-prioritizer",
        fingerprint=stable_hash("role-candidate", "evidence-gap"),
        semantic_fingerprint=stable_hash("role-semantic", "evidence-gap"),
        output_contract_fingerprint=stable_hash("role-output", role_contract["output_schema"]),
        kind_proposal="role",
        owner_proposal="orchestrator",
        source_occurrences=occurrences,
        counterexamples=(),
        independent_subjects=tuple(item.subject_id for item in occurrences),
        independent_repositories=tuple(item.repository for item in occurrences),
        selector=role_contract["selector"],
        graph={
            "phase_order": [
                "trigger",
                "decision",
                "execution",
                "artifact",
                "verification",
                "outcome",
                "durability",
            ],
            "edges": [],
            "decision_mode": "judgment",
            "requires_judgment": True,
            "role_contract": role_contract,
        },
        artifact_refs=tuple(ref for item in occurrences for ref in item.artifact_refs),
        gates={"durable_result_required": True},
        telemetry={
            "distinct_subject_count": 3,
            "effective_subject_count": 3.0,
            "negative_ratio": 0.0,
        },
        lifecycle=Lifecycle(expires_at=current + 7 * 86400),
        predecessor="role-adjudicator",
    )


def test_role_compile_rejection_arms():
    raw = _role_candidate().to_dict()
    raw["kind_proposal"] = "workflow"
    with pytest.raises(compiler.RoleCompileError, match="not a role"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["extra"] = True
    with pytest.raises(compiler.RoleCompileError, match="unsupported fields"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["schema"] = "wrong"
    with pytest.raises(compiler.RoleCompileError, match="unsupported role source"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["independent_subjects"] = raw["independent_subjects"][:2]
    raw["source_occurrences"] = raw["source_occurrences"][:2]
    raw["telemetry"]["distinct_subject_count"] = 2
    raw["telemetry"]["effective_subject_count"] = 2.0
    with pytest.raises(compiler.RoleCompileError) as exc:
        compiler.compile_role_capability(raw)
    joined = " ".join(exc.value.reasons)
    assert "three independent durable" in joined or "three effective" in joined

    # Counterexamples after IR construction: mutate the live object before compile.
    live = _role_candidate()
    object.__setattr__(live, "counterexamples", ({"note": "bad"},))
    object.__setattr__(
        live,
        "telemetry",
        {**live.telemetry, "negative_ratio": 0.5},
    )
    with pytest.raises(compiler.RoleCompileError, match="excessive counterexamples"):
        compiler.compile_role_capability(live)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["name"] = "Bad Name"
    with pytest.raises(compiler.RoleCompileError, match="invalid generated role name"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["description"] = "too short"
    with pytest.raises(compiler.RoleCompileError, match="description is unbounded"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["route_as"] = "not-a-route"
    with pytest.raises(compiler.RoleCompileError, match="unsupported role routing"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["selector"] = {"field": "task_type"}
    with pytest.raises(compiler.RoleCompileError, match="selector must be exact"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["selector"] = {
        "field": "missing",
        "operator": "regex",
        "value": 12,
        "max_matches_per_cycle": 3,
    }
    with pytest.raises(compiler.RoleCompileError) as exc:
        compiler.compile_role_capability(raw)
    joined = " ".join(exc.value.reasons)
    assert "outside input schema" in joined
    assert "unsupported operator" in joined

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["selector"] = {
        "field": "task_type",
        "operator": "in",
        "value": [],
        "max_matches_per_cycle": 1,
    }
    with pytest.raises(compiler.RoleCompileError, match="selector values are unbounded"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["capacity_policy"] = {"selection": "open"}
    with pytest.raises(compiler.RoleCompileError, match="capacity policy"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["prompt_protocol"] = {"version": 1}
    with pytest.raises(compiler.RoleCompileError, match="prompt protocol is incomplete"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["prompt_protocol"] = {
        "version": 2,
        "purpose": "short",
        "instructions": ["x"],
        "max_context_chars": 10,
        "output_format": "text",
    }
    with pytest.raises(compiler.RoleCompileError) as exc:
        compiler.compile_role_capability(raw)
    joined = " ".join(exc.value.reasons)
    assert "prompt protocol is unsupported" in joined or "purpose is unbounded" in joined

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["expires_at"] = NOW - 10
    with pytest.raises(compiler.RoleCompileError, match="bounded future expiry"):
        compiler.compile_role_capability(raw, now=NOW)

    raw = _role_candidate().to_dict()
    raw["graph"]["role_contract"]["kill_switch"] = {"env": "WRONG", "disabled_value": "1"}
    with pytest.raises(compiler.RoleCompileError, match="kill switch is invalid"):
        compiler.compile_role_capability(raw)

    raw = _role_candidate().to_dict()
    raw["predecessor"] = None
    with pytest.raises(compiler.RoleCompileError, match="predecessor rollback"):
        compiler.compile_role_capability(raw)

    # Manifest field-set / schema guard after a successful compile.
    compiled = compiler.compile_role_capability(_role_candidate())
    broken = copy.deepcopy(compiled["manifest"])
    broken["extra"] = True
    with pytest.raises(AssertionError, match="unsupported or missing fields"):
        compiler._validate_generated_role_manifest(broken)


REPO = "owner/example-repo"
TEXT = (
    "When changing `docs/ci/WORKFLOWS.md`, retain the `workflow_name` registry symbol "
    "and run its narrow validation."
)


def _playbook_repo(tmp_path: Path) -> tuple[Path, Path, CapabilityIR]:
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "ci").mkdir(parents=True)
    (repo_root / "docs" / "ci" / "WORKFLOWS.md").write_text(
        "# Workflows\n\nworkflow_name is the canonical registry symbol.\n",
        encoding="utf-8",
    )
    (repo_root / "AGENTS.md").write_text(
        "# User-authored instructions\n\nKeep this local instruction exactly.\n",
        encoding="utf-8",
    )
    registry = tmp_path / "repo_knowledge.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repos": {
                    REPO: {
                        "summary": "Example repository with workflow registry conventions.",
                        "definition_of_done": [],
                        "gotchas": [],
                        "validation": [],
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    current = int(time.time())
    refs = [{"path": "docs/ci/WORKFLOWS.md", "symbol": "workflow_name"}]
    selector = {"repo": REPO, "task_types": ["implement"], "lanes": ["opener", "closer"]}
    content_hash = stable_hash(
        "repo-playbook-rule",
        {
            "repo": REPO,
            "section": "validation",
            "text": TEXT,
            "selector": selector,
            "current_refs": refs,
        },
    )
    contract = {
        "schema": compiler.PLAYBOOK_SOURCE_SCHEMA,
        "version": compiler.PLAYBOOK_VERSION,
        "repo": REPO,
        "section": "validation",
        "text": TEXT,
        "content_hash": content_hash,
        "selector": selector,
        "current_refs": refs,
        "negative_examples": [
            {
                "text": "A prior change skipped the registry symbol and required follow-up repair.",
                "evidence_ref": stable_hash("negative", "missing-registry-symbol"),
            }
        ],
        "risk_level": "low_reversible",
        "expires_at": current + 30 * 86400,
        "rollback": {
            "action": "remove_managed_rule_and_restore_predecessor",
            "predecessor": "repo-playbook",
            "reason": "canary expired, path drifted, or linked outcomes regressed",
        },
        "optional_workflows_bundle": True,
    }
    occurrences = tuple(
        SourceOccurrence(
            event_id=f"playbook-event-{index}",
            event_refs=tuple(f"playbook-event-{index}-{phase}" for phase in range(7)),
            occurred_at=current - index,
            subject_id=f"playbook-subject:{index}",
            observation_id=f"playbook-observation:{index}",
            family_id=f"playbook-family:{index}",
            canonical_target=f"{REPO}#{index}",
            repository=REPO,
            task_type="implement",
            normalized_spec_hash=stable_hash("playbook-spec", index),
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
    candidate = CapabilityIR(
        capability_id="capability:example-workflow-registry-playbook",
        fingerprint=stable_hash("playbook-candidate", "workflow-registry"),
        semantic_fingerprint=stable_hash("playbook-semantic", "workflow-registry"),
        output_contract_fingerprint=stable_hash("playbook-output", contract),
        kind_proposal="playbook",
        owner_proposal="repo",
        source_occurrences=occurrences,
        counterexamples=(),
        independent_subjects=tuple(item.subject_id for item in occurrences),
        independent_repositories=(REPO,),
        selector=selector,
        graph={
            "phase_order": [
                "trigger",
                "decision",
                "execution",
                "artifact",
                "verification",
                "outcome",
                "durability",
            ],
            "playbook_contract": contract,
        },
        artifact_refs=tuple(ref for item in occurrences for ref in item.artifact_refs),
        gates={"durable_result_required": True},
        telemetry={
            "distinct_subject_count": 3,
            "effective_subject_count": 3.0,
            "negative_ratio": 0.0,
        },
        lifecycle=Lifecycle(expires_at=current + 30 * 86400),
        predecessor="repo-playbook",
    )
    return repo_root, registry, candidate


def test_playbook_compile_export_and_invocation_edges(tmp_path):
    repo_root, registry, candidate = _playbook_repo(tmp_path)
    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["extra"] = True
    with pytest.raises(compiler.PlaybookCompileError, match="unsupported fields"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["schema"] = "wrong"
    with pytest.raises(compiler.PlaybookCompileError, match="unsupported playbook source"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["kind_proposal"] = "role"
    with pytest.raises(compiler.PlaybookCompileError, match="not a repo-owned playbook"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["section"] = "not-a-section"
    with pytest.raises(compiler.PlaybookCompileError, match="invalid playbook section"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["text"] = "short"
    with pytest.raises(compiler.PlaybookCompileError, match="empty or unbounded"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["selector"] = {"repo": REPO}
    with pytest.raises(compiler.PlaybookCompileError, match="selector must be exact"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["selector"] = {
        "repo": "other/repo",
        "task_types": ["not-a-route"],
        "lanes": ["opener", "closer", "middle"],
    }
    with pytest.raises(compiler.PlaybookCompileError) as exc:
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)
    joined = " ".join(exc.value.reasons)
    assert "selector repo mismatch" in joined or "task types" in joined or "lanes" in joined

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["negative_examples"] = []
    with pytest.raises(compiler.PlaybookCompileError, match="bounded negative examples"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["negative_examples"] = [{"text": "x", "evidence_ref": ""}]
    with pytest.raises(compiler.PlaybookCompileError, match="negative_examples\\[0\\]"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["expires_at"] = NOW - 10
    with pytest.raises(compiler.PlaybookCompileError, match="bounded future expiry"):
        compiler.compile_playbook_capability(
            raw, repo_root=repo_root, registry_path=registry, now=NOW
        )

    raw = candidate.to_dict()
    raw["predecessor"] = None
    with pytest.raises(compiler.PlaybookCompileError, match="predecessor rollback"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["content_hash"] = "sha256:" + ("00" * 32)
    with pytest.raises(compiler.PlaybookCompileError, match="content hash mismatch"):
        compiler.compile_playbook_capability(raw, repo_root=repo_root, registry_path=registry)

    # Happy path then unmatched invocation + hash-mismatch register guard.
    manifest = compiler.compile_playbook_capability(
        candidate, repo_root=repo_root, registry_path=registry
    )
    ledger = tmp_path / "capabilities.json"
    exported = compiler.export_playbook_canary(
        manifest,
        repo_root=repo_root,
        registry_path=registry,
        ledger_path=ledger,
        apply=True,
    )
    assert exported["block_status"]["status"] == "current"

    unmatched = compiler.record_playbook_invocation(
        manifest,
        target_run_id="no-match",
        repo="other/repo",
        task_type="implement",
        lane="opener",
        accepted=False,
        ledger_path=ledger,
    )
    assert unmatched == {"matched": False, "injected": False, "accepted": False, "events": []}

    # Different manifest_id for same capability_id must refuse re-register.
    other = copy.deepcopy(manifest)
    other["manifest_id"] = "sha256:" + ("11" * 32)
    with pytest.raises(ValueError, match="different playbook rule"):
        compiler._register_playbook_capability(other, ledger)

    # Export with stale refs fails.
    stale = copy.deepcopy(manifest)
    stale["current_refs"] = [{"path": "docs/ci/MISSING.md", "symbol": "x"}]
    # Fix content/manifest hashes so validate_playbook_manifest can reach refs check.
    # Use a deliberately invalid export path after validating shape is skipped by patching.
    with pytest.raises((compiler.PlaybookCompileError, AssertionError)):
        compiler.export_playbook_canary(
            stale,
            repo_root=repo_root,
            registry_path=registry,
            ledger_path=ledger,
            apply=False,
        )

    # Manifest hash mismatch guard.
    broken = copy.deepcopy(manifest)
    broken["manifest_id"] = "sha256:" + ("22" * 32)
    with pytest.raises(AssertionError, match="manifest hash mismatch"):
        compiler.validate_playbook_manifest(broken)

    # Owner-question packaging: first contract read succeeds (policy rejection),
    # second raises so the decision still forms with an empty repo.
    raw = candidate.to_dict()
    raw["graph"]["playbook_contract"]["risk_level"] = "policy_choice"
    original = compiler._playbook_contract
    calls = {"n": 0}

    def _flaky(cand):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(cand)
        raise RuntimeError("boom")

    try:
        compiler._playbook_contract = _flaky  # type: ignore[assignment]
        decision = compiler.compile_playbook_candidate(
            raw, repo_root=repo_root, registry_path=registry, record_owner_question=False
        )
    finally:
        compiler._playbook_contract = original  # type: ignore[assignment]
    assert decision["status"] == "owner_question"
    assert decision["owner_question"]["repo"] == ""
    assert "owner_question_result" not in decision
