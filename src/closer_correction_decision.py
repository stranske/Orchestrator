"""Deterministic LOCAL correction decisions from an original controlled stage.

This module never authenticates remote requests, qualifies a role, allocates a
lease or invokes a model. Ordinary LOW relevance is not correction semantics.
The existing source owner must authenticate the original correction inputs and
obtain the proof/basis from its original sealed stage, not from remote JSON.
"""

from __future__ import annotations

from copy import deepcopy

import closer_correction_classification as contract

SCHEMA = "closer-correction-decision/v1"
SEMANTIC_SCHEMA = contract.RESPONSE_SCHEMA
MAX_BYTES = contract.MAX_BYTES
MAX_FINDINGS = contract.MAX_FINDINGS
DecisionUnavailable = contract.ContractError
digest = contract.digest


def project_explicit_semantics(*, semantic, request, profile, **inputs):
    """Pure semantic projection, NOT proof of emission or role qualification.

    The same reviewed selected-task semantic validator handles synthetic tests
    and real retained outputs. No alternate risk/test sidecar grammar exists.
    """
    checked = contract.validate_response(semantic, request=request, **inputs)
    original = inputs["original_request"]
    seed = next(
        row for row in inputs["seed_bundle"]["seeds"] if row["finding"] == original["finding"]
    )
    if not (
        isinstance(profile, dict)
        and set(profile) == {"id", "role", "qualification_ref", "qualification_sha256"}
        and profile["role"] == "closer_interpret"
        and all(isinstance(profile[key], str) and profile[key] for key in profile)
    ):
        raise DecisionUnavailable("classification profile differs")
    decision = {
        "schema": SCHEMA,
        "seed_effect_key": digest(
            {
                "schema": "closer-correction-effect/v1",
                "repository": seed["target"]["repository"],
                "event_id": seed["original_verifier"]["verification_event"]["event_id"],
                "finding_evidence": seed["finding"]["evidence"],
                "source_issue": seed["source_issue"],
                "scope": seed["scope"],
            }
        ),
        "source_scope_digest": digest(
            {
                "target": seed["target"],
                "source_issue": seed["source_issue"],
                "finding_evidence": seed["finding"]["evidence"],
                "scope": seed["scope"],
            }
        ),
        "request_sha256": original["request_sha256"],
        "findings": deepcopy(checked["findings"]),
        "profile": deepcopy(profile),
    }
    digest(decision)
    return decision


def _qualification(proof, profile_id, *, now=None):
    import closer_dispatcher as closer
    import closer_role_qualification as roles
    import execution_profiles

    profile = execution_profiles.get_profile(profile_id)
    expected = proof["expected"]
    record = roles.qualified_record(profile, roles.BOUNDED_CLASSIFICATION, now=now)
    if not (
        roles.profile_identity(profile) == expected["profile_identity"]
        and record is not None
        and roles.is_runtime_qualified(profile, roles.BOUNDED_CLASSIFICATION, now=now)
        and record["qualification_id"] == expected["qualification_id"]
        and closer._contract_digest(record) == expected["qualification_record_sha256"]
        and record.get("production_model_contract") == expected["production_model_contract"]
    ):
        raise DecisionUnavailable("original current correction qualification differs")
    return record


def materialize_selected(
    *,
    proof,
    run_id,
    basis,
    receipt_root,
    profile_id,
    request_ref,
    original_request,
    seed_bundle,
    source_body,
    raw_findings,
    retained_packet_ref,
    now=None,
):
    """Re-read physical original output and all exact task/prompt/source joins."""
    import closer_correction_producer as producer
    import closer_data_only_execution as execution
    import closer_dispatcher as closer

    accounting = None
    try:
        inputs, request, prompt, bound_basis, root = producer.prepare(
            original_request=original_request,
            seed_bundle=seed_bundle,
            source_body=source_body,
            raw_findings=raw_findings,
            retained_packet_ref=retained_packet_ref,
            basis=basis,
        )
        if basis != bound_basis:
            raise DecisionUnavailable("original selected request basis differs")
        result = closer._revalidate_controlled_proof(
            proof, run_id=run_id, basis=basis, receipt_root=receipt_root
        )
        expected = proof["expected"]
        if not (
            expected["task_type"] == producer.TASK
            and expected["profile_id"] == profile_id
            and expected["finite_frontier_target"] == basis["target"]
            and expected["prompt"]
            == execution.validate_payload(prompt, limits=expected["limits"], kind="prompt")
        ):
            raise DecisionUnavailable("original selected correction task/prompt differs")
        accounting = producer.compact_actual(result, profile_id=profile_id)
        record = _qualification(proof, profile_id, now=now)
        if closer._read_controlled_receipt(request_ref, root) != request:
            raise DecisionUnavailable("original retained correction request differs")
        semantic = closer._strict_response_json(result["output"])
        profile = {
            "id": profile_id,
            "role": "closer_interpret",
            "qualification_ref": "closer_role_qualification.py#" + record["qualification_id"],
            "qualification_sha256": closer._contract_digest(record),
        }
        decision = project_explicit_semantics(
            semantic=semantic, request=request, profile=profile, **inputs
        )
        decision.update(
            retained_packet_ref=retained_packet_ref["path"],
            decision_evidence_ref=proof["provider_result_ref"]["path"],
        )
        decision_ref = producer.retain(root, decision, "correction-decision.json")
        return {
            "status": "materialized",
            "evidence_scope": "local_controlled_execution_only",
            "remote_authenticated": False,
            "act_authorized": False,
            "source_closure_authorized": False,
            "decision": decision,
            "decision_ref": decision_ref,
            "request_ref": request_ref,
            "proof": proof,
            "basis": bound_basis,
            "run_id": run_id,
            "actual": accounting,
            "retry_safe": False,
        }
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
        held = producer._held(error, actual=accounting, request_ref=request_ref)
        # Preserve the original proof once for reconciliation/revalidation. An
        # invalid proof never becomes a discoverable accounting run by itself.
        held.update(proof=proof, run_id=run_id)
        return held


def materialize(**kwargs):
    """Legacy ordinary LOW can never opt into the separately selected task."""
    return {
        "status": "owned_unknown",
        "reason": "ordinary LOW lacks selected correction semantic contract",
        "evidence_scope": "local_controlled_execution_only",
        "retry_safe": False,
        "act_authorized": False,
        "source_closure_authorized": False,
    }
