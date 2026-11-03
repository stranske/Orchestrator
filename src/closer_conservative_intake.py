"""Source-owned nonaccepting native intake, not a model or risk clearance.

Only an actual registered finite interpret callback may issue its receipt. The
owner recomputes its complete inventory from the same native packet and sealed
queue row. A self-consistent supplied JSON object is never execution authority.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import os
import stat
import time
from pathlib import Path

import closer_conservative_intake_policy as policy

SCHEMA = "closer-source-native-intake/v1"
KIND = "source_native_conservative_inventory"
INVITATION_REQUEST = "closer-source-corrective-invitation-request/v1"
INVITATION_DECISION = "closer-source-corrective-invitation/v1"
_SOURCE_PATH = Path(__file__).resolve()
_SOURCE_SHA = hashlib.sha256(_SOURCE_PATH.read_bytes()).hexdigest()
_POLICY_PATH = Path(policy.__file__).resolve()
_POLICY_SHA = hashlib.sha256(_POLICY_PATH.read_bytes()).hexdigest()
_DATA_SHA = hashlib.sha256(json.dumps(policy.SELECTIONS, sort_keys=True).encode()).hexdigest()


class IntakeUnavailable(ValueError):
    pass


def _need(ok, reason):
    if not ok:
        raise IntakeUnavailable(reason)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _live_sources():
    for path, expected in ((_SOURCE_PATH, _SOURCE_SHA), (_POLICY_PATH, _POLICY_SHA)):
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            meta = os.fstat(stream.fileno())
            _need(stat.S_ISREG(meta.st_mode) and not meta.st_mode & 0o022, "intake_source_type")
            _need(
                hashlib.sha256(stream.read(1024 * 1024)).hexdigest() == expected,
                "intake_source_changed",
            )
    _need(
        hashlib.sha256(json.dumps(policy.SELECTIONS, sort_keys=True).encode()).hexdigest()
        == _DATA_SHA,
        "intake_selection_data_changed",
    )


def _preparation(context):
    import closer_correction_owner as owner
    import closer_execution_contracts as contracts

    prep = (context.get("interpret") or {}).get("recovered_prepare") or context["prepare"]
    snapshot, raw = owner._physical_json(prep["snapshot_ref"]["path"])
    _need(
        hashlib.sha256(raw).hexdigest() == prep["snapshot_ref"]["sha256"], "intake_snapshot_changed"
    )
    packet, root = contracts.load_retained_packet(prep["packet_ref"])
    size = os.stat(prep["packet_ref"]["path"], follow_symlinks=False).st_size
    _need(
        "bytes" not in prep["packet_ref"] or prep["packet_ref"]["bytes"] == size,
        "intake_packet_byte_count_changed",
    )
    # Native preparation may omit length; derive it from the already validated
    # physical member, without modifying the original queue preparation.
    prep = copy.deepcopy(prep)
    prep["packet_ref"] = {**prep["packet_ref"], "bytes": size}
    _need(
        prep["snapshot"]["target"] == snapshot["target"]
        and prep["revision"] == snapshot["revision"],
        "intake_snapshot_basis",
    )
    return prep, snapshot, packet, root


def _queue(context, *, stages):
    import closer_correction_owner as owner
    import closer_pipeline as pipeline
    import closer_resources as resources

    path = context["state_path"]
    fence, identity = resources.fence_for(path), resources.fence_identity(path)
    _need(fence is not None and identity is not None, "intake_actual_fence_required")
    fence()
    state, _ = owner._physical_json(path)
    frontier = context["finite_frontier"]
    target = context["prepare"]["snapshot"]["target"]
    entry = state["frontiers"][frontier["frontier_id"]]
    row = entry["items"][target]
    stage = row["stage"]
    _need(
        state["schema"] == pipeline.SCHEMA
        and entry["schema"] == "closer-pipeline-frontier/v2"
        and entry["run_id"] == context["pipeline_run_id"]
        and entry["finite_frontier"] == frontier
        and stage in stages
        and row["status"] == row["stage_status"] == "running"
        and row["scope_lease"] == identity
        and identity["target"] == target.lower()
        and row["revision"] == context["revision"] == frontier["bindings"][target]["revision"]
        and row["identity"] == context["identity"]
        and row["work_key"]
        == context["work_key"]
        == pipeline._digest([target, stage, context["revision"], frontier["bindings"][target]])
        and row["stage_deadline"] == context["deadline"]
        and (
            row["stage_started_at"] == context.get("request_started_at")
            or (stage == "act" and "request_started_at" not in context)
        )
        and math.isfinite(context["deadline"])
        and time.time() < context["deadline"],
        "intake_original_queue_identity",
    )
    _need(row["results"]["prepare"] == context["prepare"], "intake_original_preparation")
    return row, identity, fence


def _select(snapshot, inputs, selections):
    """Closed source question intent; native claim remains an unresolved claim."""
    import closer_dispatcher as closer

    keys = {
        "schema",
        "selection_id",
        "target",
        "head",
        "merge",
        "event_id",
        "source",
        "source_body_sha256",
        "criterion_sha256",
        "native_claim",
    }
    matches = []
    for value in selections:
        _need(
            type(value) is dict
            and set(value) == keys
            and value["schema"] == "closer-source-intake-selection/v1",
            "intake_selection_schema",
        )
        event = snapshot.get("verifier", {}).get("verification_event", {})
        if not (
            snapshot["target"].casefold() == value["target"]
            and snapshot["head"] == value["head"]
            and snapshot["merge"] == value["merge"]
            and event.get("event_id") == value["event_id"]
        ):
            continue
        source = [s for s in snapshot["sources"] if s["target"].casefold() == value["source"]]
        _need(
            len(source) == 1
            and source[0]["kind"] == "issue"
            and source[0]["body"] == inputs["source_body"]
            and hashlib.sha256(source[0]["body"].encode()).hexdigest()
            == value["source_body_sha256"],
            "intake_selected_source_changed",
        )
        criteria = [
            c
            for c in closer.requirements(source[0]["body"])
            if hashlib.sha256(c.encode()).hexdigest() == value["criterion_sha256"]
        ]
        raw = [r for r in inputs["raw_findings"] if r["evidence"] == value["native_claim"]]
        _need(len(criteria) == len(raw) == 1, "intake_selection_ambiguous")
        mapping = [m for m in inputs["native_seed_mapping"] if m["seed_finding_id"] == raw[0]["id"]]
        _need(len(mapping) == 1, "intake_native_claim_mapping")
        matches.append((copy.deepcopy(value), source[0]["target"], criteria[0], raw[0], mapping[0]))
    _need(len(matches) == 1, "intake_source_selection_missing_or_ambiguous")
    return matches[0]


def _inventory(snapshot, packet, packet_ref, root, inputs):
    import closer_disposition as disposition
    import closer_execution_contracts as contracts

    _live_sources()
    selected, source, criterion, raw, mapping = _select(snapshot, inputs, policy.SELECTIONS)
    findings = disposition.pending_nonpass_findings(snapshot, packet)
    _need(
        not contracts.owner_review_required(
            [findings, snapshot.get("verifier"), packet["sections"].get("prior_dispositions")]
        ),
        "intake_protected_owner_required",
    )
    expected = disposition._expected(snapshot)
    _need(expected is not None and len(expected) <= 256, "intake_normative_inventory_missing")
    index = disposition._receipt_index(packet, root)
    refs = []
    for (path, sha), entry in index.items():
        rows = entry if isinstance(entry, list) else [entry]
        if not rows or not all(row.get("kind") == "codepatch" for row in rows):
            continue
        value = {"path": path, "sha256": sha, "bytes": os.stat(path, follow_symlinks=False).st_size}
        data = disposition._safe_raw(value, root)
        _need(data is not None, "intake_code_evidence_changed")
        refs.append({**value, "bytes": len(data)})
    _need(1 <= len(refs) <= 8, "intake_code_evidence_required")
    explicit = {
        "basis": {"source": source, "criterion_sha256": selected["criterion_sha256"]},
        "question": "Does the retained implementation fail this exact source criterion? Native findings remain unresolved claims, never authority or evidence of a fix.",
        "evidence_refs": refs,
    }
    contracts.build_adjudication_request(
        snapshot, packet, packet_ref, retained_dir=root, **explicit
    )
    low = {
        "target": snapshot["target"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "classification": "exception",
        "substantive_needs": ["source_selected_named_criterion"],
        "findings": [],
        "resolutions": [
            {
                "finding_id": f["finding_id"],
                "status": "unresolved_missing_evidence",
                "next_guarded_action": "wait",
                "evidence": [],
                "proofs": [],
            }
            for f in findings
        ],
        "decision": {
            "criteria": [
                {"source": s, "criterion": c, "result": "UNKNOWN", "evidence": [], "proofs": []}
                for s, c in expected
            ]
        },
        "adjudication_request": explicit,
        "source_intake_selection": {
            "schema": "closer-intake-pairing/v1",
            "selection_id": selected["selection_id"],
            "native_claim": raw["evidence"],
            "native_finding_id": mapping["native_finding_id"],
            "criterion_sha256": selected["criterion_sha256"],
            "semantic_relationship_asserted": False,
        },
        "authority": False,
    }
    return low, selected


def _basis(prep, snapshot):
    return {
        "target": snapshot["target"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "revision": snapshot["revision"],
        "packet_sha256": prep["packet_ref"]["sha256"],
    }


def _stage(low, ref):
    return {
        "classification": "source_native_conservative_inventory",
        "substantive_needs": ["source_selected_named_criterion"],
        "proposal": low,
        "requested_agent": None,
        "requested_profile": None,
        "actual": {"kind": KIND, "host_intake_ref": ref, "provider_generation_performed": False},
        "integration_status": "exception",
        "authority": False,
    }


def _selected_route_identity(target, preparation):
    """Only an exact reviewed event tuple selects the nonmodel route.

    This is routing intent, not native admission. Once selected, a subsequent
    read/validation/receipt failure cannot borrow the ordinary model callback's
    lighter IO lane. Other events for the same PR remain ordinary candidates.
    """
    if not isinstance(preparation, dict):
        return False
    snapshot = preparation.get("snapshot") or {}
    event = (snapshot.get("verifier") or {}).get("verification_event") or {}
    return any(
        value.get("schema") == "closer-source-intake-selection/v1"
        and str(target).casefold() == value.get("target")
        and snapshot.get("target", "").casefold() == value.get("target")
        and snapshot.get("head") == value.get("head")
        # Production compact preparation omits merge; its event carries the
        # evaluated revision. This is intent only; _preparation/_inventory still
        # authenticate the complete retained snapshot and native packet.
        and snapshot.get("merge", event.get("evaluated_sha")) == value.get("merge")
        and event.get("evaluated_sha") == value.get("merge")
        and event.get("head_sha") == snapshot.get("head")
        and event.get("event_id") == value.get("event_id")
        for value in policy.SELECTIONS
    )


def ready(target, preparation):
    """Non-authorizing retained-read lane hint; issuance repeats its real checks."""
    import closer_correction_bridge as bridge

    try:
        if not _selected_route_identity(target, preparation):
            return False
        prep, snapshot, packet, root = _preparation({"prepare": preparation})
        inputs = bridge.collect_original_inputs(snapshot, packet, prep["packet_ref"])
        _inventory(snapshot, packet, prep["packet_ref"], root, inputs)
        return True
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def try_from_pipeline(target, context, callback):
    """Only the original registered callback's real finite invocation can issue."""
    import closer_correction_bridge as bridge
    import closer_correction_producer as producer
    import closer_pipeline as pipeline

    caller = inspect.currentframe().f_back
    _need(
        caller is not None
        and caller.f_code is pipeline._source_native_intake.__code__
        and caller.f_globals is pipeline.__dict__,
        "intake_original_pipeline_caller",
    )
    if callback not in pipeline._FINITE_DISPATCHER_CALLBACKS:
        return None
    # Route identity is source-owned intent, not an admission or model proof.
    if not _selected_route_identity(target, context.get("prepare")):
        return None
    try:
        _live_sources()
        row, lease, fence = _queue(context, stages={"interpret"})
        prep, snapshot, packet, root = _preparation(context)
        inputs = bridge.collect_original_inputs(snapshot, packet, prep["packet_ref"])
        low, selected = _inventory(snapshot, packet, prep["packet_ref"], root, inputs)
        document = {
            "schema": SCHEMA,
            "kind": KIND,
            "producer_sha256": _SOURCE_SHA,
            "policy_sha256": _POLICY_SHA,
            "selection_sha256": _digest(selected),
            "expected": {
                "operation_id": context["work_key"],
                "basis": _basis(prep, snapshot),
                "finite_frontier_id": context["finite_frontier"]["frontier_id"],
                "finite_frontier_target": target,
            },
            "original_frame": {
                k: copy.deepcopy(context[k])
                for k in (
                    "state_path",
                    "pipeline_run_id",
                    "finite_frontier",
                    "frontier_binding",
                    "identity",
                    "revision",
                    "work_key",
                    "deadline",
                    "request_started_at",
                )
            },
            "scope_lease": lease,
            "inventory": low,
            "authority": False,
            "provider_generation_performed": False,
        }
        ref = producer.retained_ref(root, document, "host-native-intake.json")
        result = _stage(low, ref)
        _need(
            len(json.dumps(result).encode()) <= pipeline.CALLBACK_RESULT_BYTES,
            "intake_callback_bound",
        )
        fence()
        _queue(context, stages={"interpret"})
        _need(
            producer.retain(root, document, "host-native-intake.json") == ref,
            "intake_original_receipt_changed",
        )
        return result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        # No provider was started. Preserve the selected source route as a
        # bounded wait; never invoke a model callback under its IO-only guards.
        return {
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "reconcile_source_native_intake_before_readmission",
            "pending_evidence": {
                "target": target,
                "revision": context.get("revision"),
                "event_id": "source_native_intake_unavailable",
            },
            "reason": "source_native_intake_unavailable",
            # Exception messages may contain private native inputs or credentials.
            "failure_type": type(error).__name__[:64],
            "provider_generation_performed": False,
            "authority": False,
        }


def revalidate(stage, *, context, basis, operation_id):
    """Recompute host facts in the original current EXCEPTION/ACT custody."""

    row, _lease, fence = _queue(context, stages={"exception", "act"})
    _need(row["results"]["interpret"] == stage, "intake_original_stage_changed")
    result = _read_original(stage, context=context, row=row, basis=basis, operation_id=operation_id)
    fence()
    return result


def _read_original(stage, *, context, row, basis, operation_id):
    """Deterministic original-row readback; no effect/renewal or model claim."""
    import closer_correction_bridge as bridge
    import closer_dispatcher as closer

    prep, snapshot, packet, root = _preparation(context)
    ref = stage.get("actual", {}).get("host_intake_ref")
    document = closer._read_controlled_receipt(ref, root)
    inputs = bridge.collect_original_inputs(snapshot, packet, prep["packet_ref"])
    low, selected = _inventory(snapshot, packet, prep["packet_ref"], root, inputs)
    old = row["stage_results"]["interpret"]
    expected = {
        "operation_id": operation_id,
        "basis": basis,
        "finite_frontier_id": context["finite_frontier"]["frontier_id"],
        "finite_frontier_target": snapshot["target"],
    }
    _need(
        set(document)
        == {
            "schema",
            "kind",
            "producer_sha256",
            "policy_sha256",
            "selection_sha256",
            "expected",
            "original_frame",
            "scope_lease",
            "inventory",
            "authority",
            "provider_generation_performed",
        },
        "intake_receipt_schema",
    )
    _need(
        set(document["scope_lease"]) == {"target", "owner", "epoch"}
        and document["scope_lease"]["target"] == snapshot["target"].lower()
        and type(document["scope_lease"]["epoch"]) is int
        and document["scope_lease"]["epoch"] > 0
        and type(document["scope_lease"]["owner"]) is str
        and document["scope_lease"]["owner"],
        "intake_original_scope_identity",
    )
    _need(
        document.get("schema") == SCHEMA
        and document.get("kind") == KIND
        and document.get("producer_sha256") == _SOURCE_SHA
        and document.get("policy_sha256") == _POLICY_SHA
        and document.get("selection_sha256") == _digest(selected)
        and document.get("inventory") == low
        and document.get("expected") == expected
        and basis == _basis(prep, snapshot)
        and old["status"] == "completed"
        and old["work_key"] == operation_id
        and old["started_at"] == document["original_frame"]["request_started_at"]
        and old["deadline"] == document["original_frame"]["deadline"]
        and old["started_at"] <= old["finished_at"] < old["deadline"]
        and document.get("authority") is False
        and document.get("provider_generation_performed") is False
        and stage == _stage(low, ref),
        "intake_original_receipt_or_inventory_changed",
    )
    frame = document["original_frame"]
    _need(
        set(frame)
        == {
            "state_path",
            "pipeline_run_id",
            "finite_frontier",
            "frontier_binding",
            "identity",
            "revision",
            "work_key",
            "deadline",
            "request_started_at",
        }
        and frame["work_key"] == operation_id,
        "intake_original_frame_schema",
    )
    _need(
        frame["state_path"] == context["state_path"]
        and frame["pipeline_run_id"] == context["pipeline_run_id"]
        and frame["finite_frontier"] == context["finite_frontier"]
        and frame["frontier_binding"] == context["finite_frontier"]["bindings"][snapshot["target"]]
        and frame["revision"] == row["revision"] == snapshot["revision"]
        and frame["identity"] == row["identity"]
        and ("revision" not in context or frame["revision"] == context["revision"])
        and ("identity" not in context or frame["identity"] == context["identity"]),
        "intake_original_frame_changed",
    )
    return low, document


def original_row(context):
    """Read the exact original sealed stage values, never a caller substitute."""
    import closer_correction_owner as owner

    prep, snapshot, _packet, _root = _preparation(context)
    state, _raw = owner._physical_json(context["state_path"])
    entry = state["frontiers"][context["finite_frontier"]["frontier_id"]]
    row = entry["items"][snapshot["target"]]
    _need(
        entry["finite_frontier"] == context["finite_frontier"]
        and entry["run_id"] == context["pipeline_run_id"]
        and row["results"]["prepare"] == context["prepare"]
        and row["results"]["interpret"] == context["interpret"]
        and row["revision"] == snapshot["revision"] == prep["revision"],
        "invitation_original_pipeline_changed",
    )
    return row


def original_low(stage, *, context, row, snapshot, packet_ref):
    """Historical deterministic inventory is facts, not an expired model role."""
    basis = {
        "target": snapshot["target"],
        "revision": snapshot["revision"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "packet_sha256": packet_ref["sha256"],
    }
    return _read_original(
        stage,
        context=context,
        row=row,
        basis=basis,
        operation_id=row["stage_results"]["interpret"]["work_key"],
    )


def produce_invitation(*, context, preparation, stage_result, decision):
    """Issue only in the original bridge invoke; no new model/risk judgment."""
    import types

    import closer_correction_bridge as bridge
    import closer_correction_decision as decisions
    import closer_correction_owner as owner
    import closer_correction_producer as producer
    import dispatcher

    caller = inspect.currentframe().f_back
    code = next(
        c
        for c in bridge.run_controlled_correction.__code__.co_consts
        if isinstance(c, types.CodeType) and c.co_name == "invoke"
    )
    _need(
        caller is not None and caller.f_code is code and caller.f_globals is bridge.__dict__,
        "invitation_original_bridge_caller",
    )
    owner._current(context, preparation, now=time.time())
    _need(
        decision["private_builder_proof"]["low_proof_kind"] == KIND,
        "invitation_host_intake_required",
    )
    inputs = decision["original_inputs"]
    _packet, root = contracts_packet(inputs["retained_packet_ref"])
    strong = stage_result["actual"]["_closer_controlled_proof"]
    # The actual named result is the sole provider basis. It is not renamed to
    # LOW/classifier, and no third provider call or risk clearance is claimed.
    source_context = {
        k: copy.deepcopy(context[k])
        for k in (
            "state_path",
            "pipeline_run_id",
            "finite_frontier",
            "frontier_binding",
            "identity",
            "revision",
            "work_key",
            "deadline",
            "request_started_at",
            "prepare",
            "interpret",
        )
    }
    document = {
        "schema": INVITATION_REQUEST,
        "producer_sha256": _SOURCE_SHA,
        "source_context": source_context,
        "strong_stage": copy.deepcopy(stage_result),
        "original_request": decision["original_request"],
        "provider_generation_performed": False,
        "authority": False,
    }
    ref = producer.retain(root, document, "source-invitation-request.json")
    return decisions.materialize_selected(
        proof=strong,
        run_id=strong["run_id"],
        basis=strong["expected"]["basis"],
        receipt_root=str(dispatcher.DISPATCH_LOG_DIR),
        profile_id=stage_result["requested_profile"],
        request_ref=ref,
        original_request=decision["original_request"],
        **{
            k: inputs[k]
            for k in ("seed_bundle", "source_body", "raw_findings", "retained_packet_ref")
        },
    )


def contracts_packet(ref):
    import closer_execution_contracts as contracts

    return contracts.load_retained_packet(ref)


def is_invitation_request(value):
    return isinstance(value, dict) and value.get("schema") == INVITATION_REQUEST


def materialize_invitation(
    *,
    document,
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
    persist=True,
):
    """Recompute the source invitation from native inputs and the real named proof.

    A successful data projection is NOT an issue/PR/push/merge permit. Native
    SourceSelection and every original owner/action/publication fence remain.
    """
    import closer_correction_bridge as bridge
    import closer_correction_owner as owner
    import closer_correction_producer as producer
    import closer_dispatcher as closer
    import dispatcher

    _live_sources()
    _check_invitation_document(document, original_request)
    _need(
        str(receipt_root) == str(dispatcher.DISPATCH_LOG_DIR),
        "invitation_actual_receipt_root_required",
    )
    context = document["source_context"]
    prep, snapshot, packet, root = _preparation(context)
    _need(prep["packet_ref"] == retained_packet_ref, "invitation_original_packet_changed")
    inputs = bridge.collect_original_inputs(snapshot, packet, retained_packet_ref)
    _need(
        all(
            inputs[k] == value
            for k, value in (
                ("seed_bundle", seed_bundle),
                ("source_body", source_body),
                ("raw_findings", raw_findings),
            )
        ),
        "invitation_native_original_inventory_changed",
    )
    row = original_row(context)
    low, _host_proof = original_low(
        context["interpret"],
        context=context,
        row=row,
        snapshot=snapshot,
        packet_ref=retained_packet_ref,
    )
    stage = document["strong_stage"]
    _need(
        stage["requested_profile"] == profile_id
        and stage["actual"]["_closer_controlled_proof"] == proof
        and stage["actual"]["run_id"] == run_id == proof["run_id"]
        and proof["expected"]["basis"] == basis
        and proof["expected"]["finite_frontier_id"] == context["finite_frontier"]["frontier_id"]
        and proof["expected"]["finite_frontier_target"] == snapshot["target"]
        and {k: basis[k] for k in _basis(prep, snapshot)} == _basis(prep, snapshot),
        "invitation_named_native_identity_changed",
    )
    _check_named_stage(document, row, prep, allow_running=True)
    strong, checked = owner._stage(
        stage,
        task="closer_exception",
        basis=basis,
        operation_id=context["work_key"],
        now=time.time() if now is None else now,
    )
    _need(checked == proof, "invitation_original_named_proof_changed")
    decision = _project_invitation(
        document=document,
        snapshot=snapshot,
        packet=packet,
        root=root,
        inputs=inputs,
        low=low,
        strong=strong,
        run_id=run_id,
        basis=basis,
        retained_packet_ref=retained_packet_ref,
    )
    if persist:
        decision_ref = producer.retain(root, decision, "source-corrective-invitation.json")
    else:
        decision_ref = producer.retained_ref(root, decision, "source-corrective-invitation.json")
        _need(
            closer._read_controlled_receipt(decision_ref, root) == decision,
            "invitation_original_projection_unavailable",
        )
    return {
        "status": "materialized",
        "kind": INVITATION_DECISION,
        "evidence_scope": "source_owned_corrective_invitation_only",
        "decision": decision,
        "decision_ref": decision_ref,
        "request_ref": request_ref,
        "proof": proof,
        "proof_role": "named_criterion_basis",
        "basis": basis,
        "run_id": run_id,
        "basis_provider_run_id": run_id,
        "provider_generation_performed": False,
        "remote_authenticated": False,
        "act_authorized": False,
        "source_closure_authorized": False,
        "retry_safe": False,
    }


def _check_invitation_document(document, original_request):
    _live_sources()
    _need(
        set(document)
        == {
            "schema",
            "producer_sha256",
            "source_context",
            "strong_stage",
            "original_request",
            "provider_generation_performed",
            "authority",
        }
        and document["schema"] == INVITATION_REQUEST
        and document["producer_sha256"] == _SOURCE_SHA
        and document["provider_generation_performed"] is False
        and document["authority"] is False
        and document["original_request"] == original_request,
        "invitation_original_request_changed",
    )


def _check_named_stage(document, row, prep, *, allow_running):
    import closer_correction_owner as owner

    context, stage = document["source_context"], document["strong_stage"]
    stage_meta = row.get("stage_results", {}).get("exception")
    if stage_meta is None:
        _need(allow_running, "invitation_original_named_stage_incomplete")
        original_preparation = (context.get("interpret") or {}).get("recovered_prepare") or context[
            "prepare"
        ]
        owner._current(context, original_preparation, now=time.time())
    else:
        _need(
            stage_meta["status"] == "completed"
            and stage_meta["work_key"] == context["work_key"]
            and stage_meta["started_at"] == context["request_started_at"]
            and stage_meta["deadline"] == context["deadline"]
            and stage_meta["started_at"] <= stage_meta["finished_at"] < stage_meta["deadline"]
            and row["results"]["exception"]["actual"] == stage["actual"],
            "invitation_original_named_stage_changed",
        )


def _project_invitation(
    *, document, snapshot, packet, root, inputs, low, strong, run_id, basis, retained_packet_ref
):
    import closer_correction_bridge as bridge
    import closer_correction_classification as contract
    import closer_correction_owner as owner

    original_request, context = document["original_request"], document["source_context"]
    seed_bundle, source_body, raw_findings = (
        inputs[k] for k in ("seed_bundle", "source_body", "raw_findings")
    )
    seed, tests, named = owner._selected_defect(
        snapshot=snapshot,
        packet=packet,
        packet_ref=retained_packet_ref,
        root=root,
        low=low,
        strong=strong,
        inputs=inputs,
    )
    _need(
        all(
            original_request[k] == seed[k]
            for k in ("target", "source_issue", "original_verifier", "finding", "scope")
        )
        and original_request["required_tests"] == tests
        and basis["request_id"] == named["request_id"],
        "invitation_selected_source_meaning_changed",
    )
    contract.build_request(
        original_request=original_request,
        seed_bundle=seed_bundle,
        source_body=source_body,
        raw_findings=raw_findings,
        retained_packet_ref=retained_packet_ref,
    )
    operation = original_request["original_operation"]
    _need(
        operation["hold_id"] == context["finite_frontier"]["frontier_id"]
        and operation["run_id"] == context["pipeline_run_id"]
        and operation["operation_id"] == context["work_key"]
        and operation["attempt_budget"] == 1
        and operation["deadline"]
        == owner._declared_deadline(context["deadline"], now=context["request_started_at"]),
        "invitation_original_operation_limits_changed",
    )
    decision = {
        "schema": INVITATION_DECISION,
        **bridge.effect_identity(seed),
        "request_sha256": original_request["request_sha256"],
        "source_criterion_sha256": hashlib.sha256(named["criterion"].encode()).hexdigest(),
        "native_trigger": seed["finding"],
        "criteria": copy.deepcopy(low["decision"]["criteria"]),
        "findings": [
            {"id": r["id"], "evidence": r["evidence"], "status": "unresolved_missing_evidence"}
            for r in raw_findings
        ],
        "basis_provider_run_id": run_id,
        "proof_role": "named_criterion_basis",
        "provider_generation_performed": False,
        "acceptance_authority": False,
        "mechanical_repair_authority": False,
        "security_clearance": False,
        "authority": False,
    }
    return decision


def retained_invitation_projection(*, context, row, materializer, strong_result):
    """Pure replay after the caller validated original source-issued qualifications.

    No current role snapshot, installation or evaluator is called here. Native
    receipt/qualification cutoff verification remains at the original consumer.
    """
    import closer_dispatcher as closer

    prep, snapshot, packet, root = _preparation(context)
    document = closer._read_controlled_receipt(materializer["request_ref"], root)
    _check_invitation_document(document, materializer["original_request"])
    original = document["source_context"]
    original_physical = original_row(original)
    _need(
        original_physical == row
        and original["state_path"] == context["state_path"]
        and original["finite_frontier"] == context["finite_frontier"]
        and original["pipeline_run_id"] == context["pipeline_run_id"]
        and original["prepare"] == context["prepare"]
        and original["interpret"] == context["interpret"]
        and document["strong_stage"]["actual"] == context["exception"]["actual"],
        "invitation_retained_original_context_changed",
    )
    _check_named_stage(document, row, prep, allow_running=False)
    proof = materializer["proof"]
    _need(
        proof == document["strong_stage"]["actual"]["_closer_controlled_proof"]
        and strong_result["run_id"] == proof["run_id"] == materializer["run_id"]
        and proof["expected"]["task_type"] == "closer_exception"
        and materializer["basis"] == proof["expected"]["basis"],
        "invitation_retained_actual_named_proof_changed",
    )
    import closer_correction_bridge as bridge

    inputs = bridge.collect_original_inputs(snapshot, packet, prep["packet_ref"])
    _need(
        all(
            materializer[k] == inputs[k]
            for k in ("seed_bundle", "source_body", "raw_findings", "retained_packet_ref")
        ),
        "invitation_retained_native_inputs_changed",
    )
    low, _ = original_low(
        original["interpret"],
        context=original,
        row=row,
        snapshot=snapshot,
        packet_ref=prep["packet_ref"],
    )
    strong = closer._worker_proposal(
        strong_result, agent=document["strong_stage"]["requested_agent"]
    )
    return _project_invitation(
        document=document,
        snapshot=snapshot,
        packet=packet,
        root=root,
        inputs=inputs,
        low=low,
        strong=strong,
        run_id=materializer["run_id"],
        basis=materializer["basis"],
        retained_packet_ref=prep["packet_ref"],
    )
