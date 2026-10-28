"""Source-owned correction intent from a current qualified criterion UNSAT.

The new required test is an owner-declared normative specification, never a
claim that a pytest selector exists. Operation bounds are declared correlation,
not remote hold attestation. Nothing returned here authorizes ACT or an effect.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import closer_correction_bridge as bridge
import closer_correction_classification as contract
import closer_correction_ownership as ownership

ATTEMPT_BUDGET = 1


class OwnerDecisionUnavailable(ValueError):
    pass


def _need(ok, reason):
    if not ok:
        raise OwnerDecisionUnavailable(reason)


def _declared_deadline(actual, *, now):
    """A stricter whole-second declared ceiling; original float stays private."""
    _need(
        type(actual) in (int, float) and math.isfinite(actual),
        "owner original deadline is not finite",
    )
    seconds = math.floor(actual)
    _need(
        abs(seconds) <= 9007199254740991 and seconds > now,
        "owner declared deadline already expired or outside safe integer range",
    )
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _physical_json(path, *, limit=8388608):
    name = Path(path)
    _need(
        name.is_absolute()
        and name.resolve() == name
        and not any(p.is_symlink() for p in (name, *name.parents)),
        "owner physical path required",
    )
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _need(
            stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit,
            "owner original file outside bounds",
        )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        _need(len(raw) == info.st_size, "owner original file changed")
        import closer_dispatcher as closer

        return closer._strict_response_json(raw.decode()), raw
    finally:
        os.close(fd)


def _current(context, preparation, *, now):
    import closer_pipeline as pipeline
    import closer_resources as resources

    path = context["state_path"]
    fence = resources.fence_for(path)
    identity = resources.fence_identity(path)
    _need(fence is not None and identity is not None, "owner current resource fence missing")
    fence()
    state, _raw = _physical_json(path)
    frontier = context["finite_frontier"]
    entry = state.get("frontiers", {}).get(frontier["frontier_id"], {})
    target = preparation["snapshot"]["target"]
    row = entry.get("items", {}).get(target, {})
    binding = frontier["bindings"][target]
    _need(
        state.get("schema") == pipeline.SCHEMA
        and entry.get("schema") == "closer-pipeline-frontier/v2"
        and entry.get("run_id") == context["pipeline_run_id"]
        and entry.get("finite_frontier") == frontier
        and row.get("stage") == "exception"
        and row.get("status") == "running"
        and row.get("stage_status") == "running"
        and row.get("scope_lease") == identity
        and identity.get("target") == target.lower()
        and row.get("revision") == context["revision"] == preparation["revision"]
        and row.get("identity") == context["identity"]
        and row.get("work_key")
        == context["work_key"]
        == pipeline._digest([target, "exception", context["revision"], binding])
        and context["frontier_binding"] == binding
        and row.get("stage_deadline") == context["deadline"]
        and row.get("stage_started_at") == context["request_started_at"]
        and math.isfinite(context["deadline"])
        and now < context["deadline"],
        "owner current exception queue identity differs",
    )
    original = row.get("results", {})
    _need(
        original.get("interpret") == context.get("interpret")
        and original.get("prepare") == context.get("prepare"),
        "owner original stage context changed",
    )
    effective = (original.get("interpret") or {}).get("recovered_prepare") or original.get(
        "prepare"
    )
    _need(effective == preparation, "owner preparation is not original current preparation")
    return row, identity, fence


def _stage(stage, *, task, basis, operation_id, now, host_context=None):
    import closer_dispatcher as closer
    import closer_role_qualification as roles
    import dispatcher
    import execution_profiles

    actual = stage.get("actual") or {}
    if actual.get("kind") == "source_native_conservative_inventory":
        import closer_conservative_intake as intake

        _need(
            task == "closer_interpret" and host_context is not None,
            "host inventory is not a model or another role",
        )
        return intake.revalidate(
            stage, context=host_context, basis=basis, operation_id=operation_id
        )
    proof = actual.get("_closer_controlled_proof")
    _need(isinstance(proof, dict), "owner original native stage proof missing")
    expected = proof.get("expected", {})
    _need(
        stage.get("requested_agent") == "claude"
        and expected.get("task_type") == task
        and expected.get("operation_id") == operation_id
        and expected.get("profile_id") == stage.get("requested_profile"),
        "owner original native stage task/operation differs",
    )
    result = closer._revalidate_controlled_proof(
        proof, run_id=actual.get("run_id"), basis=basis, receipt_root=dispatcher.DISPATCH_LOG_DIR
    )
    profile = execution_profiles.get_profile(stage["requested_profile"])
    role = roles.TASK_ROLES[task]
    record = roles.qualified_record(profile, role, now=now)
    _need(
        record is not None
        and roles.is_runtime_qualified(profile, role, now=now)
        and roles.profile_identity(profile) == expected.get("profile_identity")
        and record["qualification_id"] == expected.get("qualification_id")
        and closer._contract_digest(record) == expected.get("qualification_record_sha256")
        and roles.execution_record_matches(record, expected, checked_at=now),
        "owner original current qualification differs",
    )
    _need(result.get("exit") == 0, "owner stage did not complete successfully")
    response = closer._worker_proposal(result, agent=stage.get("requested_agent"))
    _need(bool(response), "owner original response invalid")
    return response, proof


def _selected_defect(
    *,
    snapshot,
    packet,
    packet_ref,
    root,
    low,
    strong,
    inputs,
    selected_finding_id=None,
):
    """A pure source policy after native receipt validation by the caller above."""
    import closer_dispatcher as closer
    import closer_disposition as disposition
    import closer_execution_contracts as execution

    explicit = low.get("adjudication_request")
    _need(
        isinstance(explicit, dict) and set(explicit) == {"basis", "question", "evidence_refs"},
        "owner one named criterion request required",
    )
    request = execution.build_adjudication_request(
        snapshot,
        packet,
        packet_ref,
        basis=explicit["basis"],
        question=explicit["question"],
        evidence_refs=explicit["evidence_refs"],
        retained_dir=root,
    )
    answer = execution.validate_adjudication_response(
        request, strong, snapshot=snapshot, packet=packet, packet_ref=packet_ref, retained_dir=root
    )
    _need(
        answer["outcome"] == "unsatisfied" and "finding_id" not in request["basis"],
        "owner established source criterion UNSAT required",
    )
    criterion = request["criterion"]
    source_target = request["basis"]["source"]
    sources = [s for s in snapshot["sources"] if s["target"] == source_target]
    _need(
        len(sources) == 1
        and sources[0]["kind"] == "issue"
        and criterion in closer.requirements(inputs["source_body"]),
        "owner exact normative source criterion required",
    )
    # Uncertainty and all other findings remain unchanged. This source policy
    # establishes only the selected defect, never ordinary acceptance.
    merged = execution.merge_selected_response(low, request, answer)
    expected = disposition._expected(snapshot)
    rows = (low.get("decision") or {}).get("criteria")
    _need(
        isinstance(rows, list)
        and len(rows) == len(expected)
        and sorted((r.get("source"), r.get("criterion")) for r in rows) == sorted(expected),
        "owner original criterion inventory incomplete",
    )
    native_ids = {r["finding_id"] for r in disposition.pending_nonpass_findings(snapshot, packet)}
    resolutions = low.get("resolutions")
    _need(
        low.get("findings") == []
        and isinstance(resolutions, list)
        and len(resolutions) == len(native_ids)
        and {r.get("finding_id") for r in resolutions} == native_ids
        and all(
            r.get("status") == "unresolved_missing_evidence"
            and r.get("next_guarded_action") in {"wait", "collect_evidence"}
            and r.get("evidence") == []
            and r.get("proofs") == []
            for r in resolutions
        )
        and merged.get("classification") == low.get("classification")
        and merged.get("substantive_needs") == low.get("substantive_needs")
        and merged.get("resolutions") == resolutions,
        "owner original unresolved native inventory differs",
    )
    pairing = low.get("source_intake_selection")
    if pairing is not None:
        # This is reviewed source question intent plus a real named criterion
        # proof, NOT an assertion that a native claim quoted/proved that clause.
        import closer_conservative_intake as intake

        _low, selected = intake._inventory(snapshot, packet, packet_ref, root, inputs)
        _need(
            low == _low
            and pairing.get("semantic_relationship_asserted") is False
            and pairing.get("criterion_sha256")
            == selected["criterion_sha256"]
            == hashlib.sha256(criterion.encode()).hexdigest(),
            "owner source intake selection changed",
        )
        matches = [r for r in inputs["raw_findings"] if r["evidence"] == pairing["native_claim"]]
    else:
        matches = [r for r in inputs["raw_findings"] if criterion in r["text"]]
    _need(len(matches) == 1, "owner native criterion/finding quote ambiguous or missing")
    raw = matches[0]
    mapping = [m for m in inputs["native_seed_mapping"] if m["seed_finding_id"] == raw["id"]]
    _need(
        len(mapping) == 1
        and (selected_finding_id is None or selected_finding_id == mapping[0]["native_finding_id"]),
        "owner selected native finding differs",
    )
    seeds = [s for s in inputs["seed_bundle"]["seeds"] if s["finding"]["id"] == raw["id"]]
    _need(len(seeds) == 1, "owner selected seed ambiguous")
    seed = seeds[0]
    _need(
        source_target == seed["target"]["repository"] + "#" + seed["source_issue"]["number"],
        "owner source criterion differs from seed",
    )
    test_id = "source-test-" + contract.digest({"seed": seed, "criterion": criterion})
    spec = (
        "Add a regression test for the original source acceptance criterion quoted exactly:\n"
        + criterion
        + "\nOriginal native verifier finding quoted exactly:\n"
        + raw["text"]
        + "\nThe test must demonstrate the missing behavior and pass after its correction."
    )
    required = [
        {
            "kind": "required_test_spec",
            "id": test_id,
            "finding_id": raw["id"],
            "specification": spec,
            "source_evidence": raw["evidence"],
        }
    ]
    return seed, required, request


def build_owner_request(
    *,
    context,
    preparation,
    stage_result=None,
    github,
    selected_finding_id=None,
    declared_attempt_budget=1,
    now=None,
):
    """Derive a nonauthorizing request only from current native positive facts."""
    import closer_correction_handoff as handoff
    import closer_execution_contracts as execution

    clock = time.time() if now is None else float(now)
    try:
        _need(math.isfinite(clock) and abs(clock - time.time()) <= 2, "owner clock is not current")
        _need(
            type(declared_attempt_budget) is int and declared_attempt_budget == ATTEMPT_BUDGET,
            "owner source attempt policy is exactly one",
        )
        row, identity, fence = _current(context, preparation, now=clock)
        packet, root = execution.load_retained_packet(preparation["packet_ref"])
        ref = preparation["snapshot_ref"]
        snapshot, raw = _physical_json(ref["path"])
        _need(
            hashlib.sha256(raw).hexdigest() == ref["sha256"]
            and snapshot["revision"] == preparation["revision"]
            and snapshot["target"] == preparation["snapshot"]["target"],
            "owner original snapshot changed",
        )
        # Native pipeline refs omit byte count. Derive the exact public contract
        # shape from the original physical file, never from a caller size claim.
        packet_value, packet_raw = _physical_json(preparation["packet_ref"]["path"])
        _need(
            packet_value == packet
            and hashlib.sha256(packet_raw).hexdigest() == preparation["packet_ref"]["sha256"],
            "owner packet readback changed",
        )
        packet_ref = {
            "path": preparation["packet_ref"]["path"],
            "sha256": preparation["packet_ref"]["sha256"],
            "bytes": len(packet_raw),
        }
        inputs = bridge.collect_original_inputs(snapshot, packet, packet_ref)
        basis = {
            "target": snapshot["target"],
            "revision": snapshot["revision"],
            "head": snapshot["head"],
            "base": snapshot["base"],
            "packet_sha256": preparation["packet_ref"]["sha256"],
        }
        low_stage = context["interpret"]
        host_intake = (low_stage.get("actual") or {}).get("kind") == (
            "source_native_conservative_inventory"
        )
        low_basis = (
            basis
            if host_intake
            else (
                (low_stage.get("actual") or {})
                .get("_closer_controlled_proof", {})
                .get("expected", {})
                .get("basis")
            )
        )
        _need(low_basis == basis, "owner original LOW basis differs")
        low, low_proof = _stage(
            low_stage,
            task="closer_interpret",
            basis=basis,
            operation_id=row["stage_results"]["interpret"]["work_key"],
            now=clock,
            host_context=context,
        )
        proof = ((stage_result or {}).get("actual") or {}).get("_closer_controlled_proof", {})
        strong_basis = proof.get("expected", {}).get("basis", {})
        _need(
            {k: strong_basis.get(k) for k in basis} == basis, "owner original STRONG basis differs"
        )
        strong, strong_proof = _stage(
            stage_result or {},
            task="closer_exception",
            basis=strong_basis,
            operation_id=context["work_key"],
            now=clock,
        )
        seed, required, named = _selected_defect(
            snapshot=snapshot,
            packet=packet,
            packet_ref=preparation["packet_ref"],
            root=root,
            low=low,
            strong=strong,
            inputs=inputs,
            selected_finding_id=selected_finding_id,
        )
        _need(
            strong_basis.get("request_id") == named["request_id"],
            "owner original STRONG named request differs",
        )
        observed = ownership.collect_ownership(
            github=github, seed=seed, deadline=context["deadline"], now=clock
        ).document()
        fence()
        # Re-check queue custody and clock after bounded external observations.
        _current(context, preparation, now=time.time())
        operation = {
            "hold_id": context["finite_frontier"]["frontier_id"],
            "run_id": context["pipeline_run_id"],
            "operation_id": context["work_key"],
            "idempotency_key": "correction-"
            + observed["ownership"]["seed_effect_key"]
            + "-"
            + observed["ownership"]["source_scope_digest"],
            "deadline": _declared_deadline(context["deadline"], now=time.time()),
            "attempt_budget": ATTEMPT_BUDGET,
            "escalation_owner": identity["owner"],
        }
        original = {
            k: seed[k] for k in ("target", "source_issue", "original_verifier", "finding", "scope")
        }
        original.update(
            schema="closer-correction-request/v1",
            required_tests=required,
            incumbent=observed["incumbent"],
            original_operation=operation,
        )
        original["request_sha256"] = contract.digest(original)
        handoff.validate_request(original)
        contract.build_request(
            original_request=original,
            **{
                k: inputs[k]
                for k in ("seed_bundle", "source_body", "raw_findings", "retained_packet_ref")
            },
        )
        handoff._ownership(observed["ownership"], original)
        return {
            "status": "ready",
            "original_request": original,
            "required_tests": required,
            "ownership": observed["ownership"],
            "original_inputs": inputs,
            "private_builder_proof": {
                "schema": "closer-correction-owner-proof/v1",
                "declared_operation_limits": True,
                "external_hold_attested": False,
                "source_attempt_budget": ATTEMPT_BUDGET,
                "original_queue_operation": {
                    "pipeline_run_id": context["pipeline_run_id"],
                    "work_key": context["work_key"],
                    "deadline": context["deadline"],
                    "scope_lease": identity,
                    "finite_frontier_id": context["finite_frontier"]["frontier_id"],
                },
                "low_proof_sha256": contract.digest(low_proof),
                "low_proof_kind": (
                    "source_native_conservative_inventory"
                    if host_intake
                    else "controlled_provider_execution"
                ),
                "strong_proof_sha256": contract.digest(strong_proof),
                "named_request_sha256": contract.digest(named),
                "native_ownership_sha256": contract.digest(observed),
            },
            "remote_authenticated": False,
            "retry_safe": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        RecursionError,
        AttributeError,
        OverflowError,
    ) as error:
        return {
            "status": "owned_unknown",
            "reason": str(error)[:1000],
            "remote_authenticated": False,
            "retry_safe": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        }
