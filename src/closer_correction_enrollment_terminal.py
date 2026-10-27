"""Future-only correction enrollment knowledge; never a continuation permit.

Only the original execute_candidate frame and its actual writer may retain a
record. Read-only boundary checks confer no ACT/provider/domain/stop authority.
No historical UNKNOWN execution can acquire this marker through a reader.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
from pathlib import Path

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA = "closer-correction-enrollment-terminal/v1"
REFERENCE = "closer-correction-enrollment-terminal-reference/v1"
BOUNDARY = "closer-correction-enrollment-boundary/v1"
_FALSE = (
    "target_complete",
    "domain_completed",
    "acceptance_authorized",
    "retry_authorized",
    "act_authorized",
    "provider_authorized",
    "source_closure_authorized",
    "global_stop_valid",
)
_SHA = re.compile(r"[a-f0-9]{64}")


class EnrollmentUnknown(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise EnrollmentUnknown(reason)


def _hash(value):
    import closer_queue as queue

    return hashlib.sha256(queue._canonical_bytes(value)).hexdigest()


def _ref(record):
    import closer_correction_controller as ctl

    return {
        "schema": REFERENCE,
        "name": "enrollment-terminal-" + record["effect_key"] + ".json",
        "sha256": ctl.digest(record),
        **{k: record[k] for k in ("effect_key", "request_sha256", "operation_sha256")},
    }


def _reference(value):
    _need(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "name",
            "sha256",
            "effect_key",
            "request_sha256",
            "operation_sha256",
        },
        "exact enrollment reference required",
    )
    _need(
        value["schema"] == REFERENCE
        and all(
            type(value[k]) is str and _SHA.fullmatch(value[k])
            for k in ("sha256", "effect_key", "request_sha256", "operation_sha256")
        )
        and value["name"] == "enrollment-terminal-" + value["effect_key"] + ".json",
        "enrollment reference identity differs",
    )
    return value


def _closure(installation):
    import closer_correction_controller as ctl
    import closer_correction_source_record as source

    for module in (source, ctl, __import__(__name__)):
        path = Path(module.__file__)
        pin = installation.source_closure.get(path.name)
        _need(
            path.is_absolute()
            and path.resolve(strict=True) == path
            and not any(p.is_symlink() for p in (path, *path.parents))
            and pin == {"path": str(path), "sha256": module._LOADED_SOURCE_SHA256}
            and hashlib.sha256(path.read_bytes()).hexdigest() == module._LOADED_SOURCE_SHA256,
            "loaded enrollment source closure unavailable or changed",
        )


def _entry(controller, custody, operation, request):
    import closer_correction_controller as ctl

    before = controller.read_bound(custody)
    rows = [e for e in before["data"]["entries"] if e["key"] == ctl.effect_key(request)]
    _need(len(rows) == 1 and rows[0]["request"] == request, "exact enrollment membership absent")
    entry = rows[0]
    selected = entry["admitted_selector"]
    controller.authenticate(selected, request, custody, operation)
    admission = custody.read("admission-" + selected["record_sha256"] + ".json")
    _need(
        ctl.digest(admission) == selected["record_sha256"] and admission["request"] == request,
        "protected original admission differs",
    )
    return before, entry, admission


def _effect(custody, config, request, *, phase, proof_sha, source_ref, seed):
    """Read exact original raw HTTP acknowledgment plus observed own delta.

    This necessary knowledge check never performs a native API read or write.
    The producer separately runs current native/source ownership validators.
    """
    import closer_correction_controller as ctl
    import closer_correction_native_effects as effects

    subject = (
        {"kind": "issue", **{k: request["source_issue"][k] for k in ("number", "node_id")}}
        if phase == "instruction_publication"
        else None
    )
    key = effects.phase_key(request, phase, subject)
    proof = custody.read("native-origin-" + proof_sha + ".json")
    intent = custody.read("native-intent-" + key + ".json")
    plan = custody.read("native-plan-" + key + ".json") if phase == "issue_creation" else intent
    _need(
        ctl.digest(proof) == proof_sha
        and proof["schema"] == "local-correction-native-origin/v1"
        and proof["phase_key"] == key
        and proof["intent_sha256"] == ctl.digest(intent)
        and intent["schema"] == "local-correction-native-intent/v1"
        and intent["phase"] == phase
        and intent["phase_key"] == key
        and intent["request"] == request
        and intent["key"] == ctl.effect_key(request)
        and intent["owner_semantics_sha256"] == ctl.digest(effects.semantic_request(request))
        and intent["payload_sha256"] == ctl.digest(intent["payload"])
        and intent["source_record_ref"] == source_ref
        and intent["seed_selector"] == seed
        and plan["phase"] == phase
        and plan["request"] == request
        and all(intent.get(k) == v for k, v in plan.items() if k != "schema"),
        "protected original native plan/intent/origin differs",
    )
    response = effects.read_response(custody, intent)
    native = effects.validate_creation(
        response,
        phase=phase,
        payload=intent["payload"],
        repository=config.repository,
        config=config,
        parent=subject,
    )
    _need(
        proof["response_sha256"] == ctl.digest(response) and proof["native"] == native,
        "original raw native acknowledgment differs",
    )
    name = (
        (
            "native-instruction-transition-"
            if phase == "instruction_publication"
            else "native-issue-transition-"
        )
        + key
        + ".json"
    )
    # Reconstruct the source-owned own-delta predicate without a live reader.
    if phase == "instruction_publication":
        transition = custody.read("native-transition-" + key + ".json")
        _need(
            transition
            == effects.observed_instruction_transition(
                intent["source_before"],
                response,
                transition["after"],
                request=request,
                payload=intent["payload"],
                config=config,
            ),
            "observed own instruction delta differs",
        )
    else:
        transition = custody.read(name)
        _need(
            transition
            == effects.observed_issue_transition(
                intent["source_before"],
                response,
                transition["after"],
                request=request,
                payload=intent["payload"],
                config=config,
            ),
            "observed own issue delta differs",
        )
    return {
        "phase": phase,
        "phase_key": key,
        "proof_sha256": proof_sha,
        "intent_sha256": ctl.digest(intent),
        "plan_sha256": ctl.digest(plan),
        "response_sha256": ctl.digest(response),
        "transition_sha256": ctl.digest(transition),
        "source_before_sha256": ctl.digest(transition["before"]),
        "source_after_sha256": ctl.digest(transition["after"]),
        "native": native,
        "owner": intent["owner"],
        "native_lifecycle": plan.get("native_lifecycle"),
    }


def _issuance_proofs(context, admission, issued_at_ms):
    """Producer-only current qualification snapshots, bound to actual proofs."""
    import closer_dispatcher as closer
    import closer_role_qualification as roles
    import execution_profiles

    proofs = [
        context[stage]["actual"]["_closer_controlled_proof"] for stage in ("interpret", "exception")
    ]
    proofs.append(admission["materializer"]["proof"])
    snapshots = []
    for proof in proofs:
        expected = proof["expected"]
        profile = execution_profiles.get_profile(expected["profile_id"])
        role = roles.TASK_ROLES[expected["task_type"]]
        qualified = roles.qualified_record(profile, role)
        _need(
            qualified is not None
            and roles.is_runtime_qualified(profile, role)
            and qualified["qualification_id"] == expected["qualification_id"]
            and closer._contract_digest(qualified) == expected["qualification_record_sha256"]
            and qualified["profile_identity"] == expected["profile_identity"]
            and qualified["production_model_contract"] == expected["production_model_contract"],
            "current original role qualification differs",
        )
        receipt = closer._read_controlled_receipt(
            proof["provider_result_ref"], admission["materializer"]["receipt_root"]
        )
        _need(
            receipt["run_id"] == proof["run_id"]
            and receipt["profile_id"] == expected["profile_id"]
            and receipt["started_at"] <= receipt["completed_at"] <= issued_at_ms / 1000,
            "original provider completion follows source issuance",
        )
        closer._revalidate_controlled_proof(
            proof,
            run_id=proof["run_id"],
            basis=expected["basis"],
            receipt_root=admission["materializer"]["receipt_root"],
        )
        snapshots.append({"proof": proof, "qualification": qualified, "role": role})
    _need(
        len({p["proof"]["run_id"] for p in snapshots}) == 3, "original role run identities overlap"
    )
    return snapshots


def retain_locked(controller, custody, operation, selection, *, published, enrolled, issue):
    """Issue only at the end of the original successful source callback."""
    import closer_correction_source_record as source

    frame = inspect.currentframe().f_back
    try:
        _need(
            frame.f_code is source.execute_candidate.__code__
            and frame.f_globals is source.__dict__
            and all(
                frame.f_locals.get(k) is v
                for k, v in {
                    "controller": controller,
                    "custody": custody,
                    "operation": operation,
                    "selection": selection,
                    "published": published,
                    "enrolled": enrolled,
                    "issue": issue,
                }.items()
            ),
            "original source execute_candidate frame required",
        )
    finally:
        del frame
    import closer_correction_controller as ctl
    import closer_native_observer as native
    import closer_queue as queue

    _need(
        type(controller) is ctl.IndexController and type(custody) is ctl._Custody,
        "actual original controller custody required",
    )
    controller._require_writer(operation, custody)
    installation = controller._source_installation()
    _closure(installation)
    facts = source.validate_for_native_effect(selection, controller, operation)
    request = facts["request"]
    _need(
        published.get("status") == "native_instruction_published"
        and enrolled.get("status") in {"checkpoint_confirmed", "checkpoint_reconciled_only"},
        "source enrollment did not confirm",
    )
    effects = controller._native_effect_source()
    instruction = effects.validate_instruction_origin(
        controller, custody, operation, selection, published
    )
    before, entry, admission = _entry(controller, custody, operation, request)
    _need(
        admission["source_record_ref"] == facts["source_record_ref"]
        and admission["owner_instruction"] == instruction
        and admission["materializer"] == facts["materializer"]
        and admission["original_source_context"] == facts["original_source_context"]
        and admission["original_incumbent_context"] == facts["original_incumbent_context"],
        "original request/context/qualification enrollment differs",
    )
    instruction_effect = _effect(
        custody,
        controller.config,
        request,
        phase="instruction_publication",
        proof_sha=admission["own_instruction"]["proof_sha256"],
        source_ref=facts["source_record_ref"],
        seed=facts["seed_selector"],
    )
    stages = entry["stages"]
    _need(
        all(s["state"] == "confirmed" and s["phase"] == "issue_creation" for s in stages),
        "unrelated or unresolved native stage blocks enrollment terminal",
    )
    if request["incumbent"].get("kind") == "no_incumbent":
        _need(
            issue.get("status") == "native_effect_confirmed" and len(stages) == 1,
            "required original issue creation incomplete",
        )
        transition = effects._read_issue_transition(
            controller, custody, operation, request, required=True
        )
        issue_effect = _effect(
            custody,
            controller.config,
            request,
            phase="issue_creation",
            proof_sha=stages[0]["proof_sha256"],
            source_ref=facts["source_record_ref"],
            seed=facts["seed_selector"],
        )
        _need(
            stages[0]["result"]
            == issue_effect["native"]["subject"]
            == transition["native"]["subject"]
            and stages[0]["result"] in entry["subjects"]
            and issue.get("native") == issue_effect["native"],
            "confirmed issue membership differs",
        )
    else:
        _need(
            issue.get("status") == "native_issue_not_required" and not stages,
            "existing incumbent issue exemption differs",
        )
        controller.fresh_issue_context(
            request, admission, {"owner_login": request["incumbent"]["owner"]}
        )
        issue_effect = {
            "status": "native_issue_not_required",
            "incumbent_sha256": ctl.digest(request["incumbent"]),
        }
    origin = source.operation_lifecycle_origin(controller, operation)
    _need(
        type(origin) is dict
        and origin["native_b_start"] == native.source_b_start(installation)
        and instruction_effect["owner"]["operation_sha256"] == origin["operation_sha256"]
        and instruction_effect["owner"]["operation_id"] == operation["id"]
        and instruction_effect["owner"]["process_id"] == os.getpid(),
        "original native B/operation lifecycle differs",
    )
    if issue_effect.get("native_lifecycle") is not None:
        _need(issue_effect["native_lifecycle"] == origin, "issue original operation differs")
    invocation = queue._callback_threads.invocation
    _need(
        queue._currently_invoking(origin["target"], operation)
        and invocation["state_path"] == origin["queue_path"],
        "original callback ended",
    )
    anchor = controller.anchor(custody)
    _need(
        anchor["state_sha256"] == ctl.digest(before["data"])
        and custody.read(anchor["result_name"])["after"] == before,
        "confirmed original checkpoint/highwater differs",
    )
    context = json.loads(selection._pipeline_context_json)
    name = "enrollment-terminal-" + ctl.effect_key(request) + ".json"
    try:
        existing = custody.read(name)
    except FileNotFoundError:
        existing = None
    issued_at_ms = int(time.time() * 1000) if existing is None else existing["issued_at_ms"]
    _need(
        type(issued_at_ms) is int
        and operation["submitted_at"] <= issued_at_ms / 1000 <= time.time() < operation["deadline"],
        "original issuance cutoff differs",
    )
    proofs = _issuance_proofs(context, admission, issued_at_ms)
    import closer_correction_producer as producer
    import closer_dispatcher as closer

    _prep, _snapshot, _packet, root = source._prepared(context)
    original_record = closer._read_controlled_receipt(facts["source_record_ref"], root)
    # This already-current materialized decision is retained by the source
    # publisher. Derive its immutable reference, never create another decision.
    decision_ref = producer.retained_ref(root, facts["decision"], "correction-decision.json")
    _need(
        closer._read_controlled_receipt(decision_ref, root) == facts["decision"],
        "original retained materialized decision differs",
    )
    record = {
        "schema": SCHEMA,
        "issuer_source_sha256": _LOADED_SOURCE_SHA256,
        "controller_config": controller.config.source_binding(),
        "manifest_path": installation.manifest_path,
        "manifest_sha256": installation.manifest_sha256,
        "source_closure_sha256": ctl.digest(installation.source_closure),
        "effect_key": ctl.effect_key(request),
        "request_sha256": request["request_sha256"],
        "admitted_selector": entry["admitted_selector"],
        "admission_sha256": ctl.digest(admission),
        "entry_sha256": ctl.digest(entry),
        "checkpoint": anchor,
        "operation_json": origin["operation_json"],
        "operation_sha256": origin["operation_sha256"],
        "queue_path": origin["queue_path"],
        "target": origin["target"],
        "finite_frontier": operation["finite_frontier"],
        "native_b_start": origin["native_b_start"],
        "source_record_ref": facts["source_record_ref"],
        "issued_at_ms": issued_at_ms,
        "original_source_record_sha256": _hash(original_record),
        "original_proofs_json": queue._canonical_bytes(proofs).decode(),
        "decision_ref": decision_ref,
        "context_identity_json": queue._canonical_bytes(
            {k: v for k, v in context.items() if k not in {"prepare", "interpret", "exception"}}
        ).decode(),
        "context_results_sha256": _hash(
            {k: context[k] for k in ("prepare", "interpret", "exception")}
        ),
        "original_facts_sha256": _hash(
            {
                k: facts[k]
                for k in (
                    "request",
                    "raw_findings",
                    "seed_selector",
                    "materializer",
                    "original_source_context",
                    "original_source_native",
                    "original_incumbent_context",
                    "source_record_ref",
                    "decision",
                )
            }
        ),
        "instruction_effect": instruction_effect,
        "issue_effect": issue_effect,
        **dict.fromkeys(_FALSE, False),
    }
    controller._require_writer(operation, custody)
    _need(controller.read_bound(custody) == before, "checkpoint changed before terminal retention")
    reference = _ref(record)
    custody.create(reference["name"], record)
    _need(custody.read(reference["name"]) == record, "conflicting terminal custody")
    return reference


def validate_queue_boundary(
    *,
    manifest_path,
    state_path,
    target,
    finite_frontier,
    proof_ref,
    expected_stage,
    expected_operation_id=None,
):
    """Immutable knowledge reader; missing proof primitives hold by default."""
    try:
        _reference(proof_ref)
        return _read_boundary(
            manifest_path=manifest_path,
            state_path=state_path,
            target=target,
            finite_frontier=finite_frontier,
            proof_ref=proof_ref,
            expected_stage=expected_stage,
            expected_operation_id=expected_operation_id,
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, ImportError):
        return None


def _native_start(manifest, campaign, config, reference, operation):
    """Read signed original B and handoff; never infer C/domain terminality."""
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    root = native.contract(manifest)[3]
    path = Path(reference["path"])
    _need(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and path.parent.parent == root
        and re.fullmatch(r"[a-f0-9]{32}", path.parent.name)
        and re.fullmatch(r"worker-b-start-[a-f0-9]{12}\.json", path.name),
        "original B-start custody differs",
    )
    key = native._existing_lifecycle_key(root)
    start, exact = native._read_lifecycle_record(path.parent, path.name, key)
    _need(exact == reference, "original signed B-start changed")
    workers = [w for w in campaign["workers"] if w.get("worker_id") == start["worker_id"]]
    _need(
        len(workers) == 1 and workers[0]["target"] == start["worker"]["target"],
        "original native worker membership differs",
    )
    cfg = native.result_admission_config(
        campaign["worker_config"], start["admission"], Path(config.state_path), campaign, workers[0]
    )
    directory, _key, context, fields, identifier, handoff_sha = native._lifecycle_authority(
        manifest, cfg
    )
    _need(
        directory == path.parent
        and start["schema"] == "closer-native-correction-b-start/v1"
        and start["binding"] == native.binding(manifest)
        and start["admission"] == context
        and start["worker"] == fields
        and start["worker_id"] == identifier
        and start["handoff_sha256"] == handoff_sha
        and start["config_sha256"] == mechanisms.digest(config.source_binding())
        and start["worker_config_sha256"] == mechanisms.digest(cfg)
        and start["producer_sha256"] == manifest["modules"]["closer_runtime_binding.py"]
        and start["process"]["pid"] == start["process"]["process_group"]
        and type(start["process"]["os_started"]) is str
        and context["finite_frontier"] == operation["finite_frontier"]
        and all(
            start.get(k) is False
            for k in (
                "domain_completed",
                "acceptance_authorized",
                "execution_authorized",
                "source_closure_authorized",
                "remote_execution_attested",
            )
        ),
        "signed original B/worker/source identities differ",
    )
    return start


def _no_uncertainty(value):
    _need(type(value) is dict, "source knowledge row is not an object")
    status = value.get("status")
    _need(
        status not in {"unknown", "uncertain", "failed"}
        and not (type(status) is str and status.startswith("unresolved_"))
        and all(
            value.get(k) is None or value.get(k) is False
            for k in ("outcome_uncertain", "provider_outcome_uncertain")
        )
        and value.get("recovery_barrier") is None,
        "explicit unresolved source or queue barrier remains owned",
    )


def _stage_identity(row, context):
    """Necessary original ACT-stage identity/barrier check; no issuance."""
    _no_uncertainty(row)
    _need(
        row.get("stage") == "act"
        and (row.get("status"), row.get("stage_status"))
        in {("running", "running"), ("held", "completed")}
        and row.get("work_key") == context["work_key"],
        "original ACT work/stage identity differs",
    )
    stage = row.get("stage_results", {}).get("act")
    _need(
        stage is not None or row.get("stage_status") == "running",
        "completed original ACT receipt missing",
    )
    if stage is not None:
        _no_uncertainty(stage)
        _need(
            stage.get("work_key") == context["work_key"]
            and stage.get("status") == row["stage_status"],
            "original ACT receipt work key differs",
        )


def _queue_operation(record, proof_ref, target, finite_frontier, expected_operation_id):
    import closer_correction_controller as ctl
    import closer_queue as queue

    original = json.loads(record["operation_json"])
    _need(
        queue._canonical_bytes(original).decode() == record["operation_json"]
        and _hash(original) == record["operation_sha256"] == proof_ref["operation_sha256"]
        and original["status"] == "submitted"
        and original["action"] == "disposition"
        and original["finite_frontier"] == record["finite_frontier"] == finite_frontier
        and record["target"] == target
        and target in finite_frontier["targets"]
        and (expected_operation_id is None or expected_operation_id == original["id"]),
        "original operation/frontier selector differs",
    )
    state = ctl.IndexController._physical_state(record["queue_path"], "enrollment queue redirected")
    bucket = state["frontiers"][finite_frontier["frontier_id"]]
    _need(
        bucket["policy"] == finite_frontier
        and bucket["round"]["finite_frontier"] == finite_frontier,
        "original durable queue frontier differs",
    )
    row = bucket["items"][target]
    _no_uncertainty(row)
    retained = row["operation"]
    _no_uncertainty(retained)
    _no_uncertainty(retained["result"])
    additions = {
        "result",
        "elapsed_seconds",
        "next_trigger",
        "completed_at",
        "reconcile_next_trigger",
        "invoked_at",
    }
    _need(
        retained["status"] == "completed"
        and all(retained.get(k) == v for k, v in original.items() if k != "status")
        and not (set(retained) - set(original) - additions)
        and original["submitted_at"] <= retained["invoked_at"] < original["deadline"]
        and retained["result"]["correction_enrollment_terminal"] == proof_ref
        and retained["result"].get("target_complete") is False
        and retained["result"].get("act_authorized") is False
        and retained["result"].get("source_closure_authorized") is False,
        "completed source operation/result marker differs",
    )
    # This narrow knowledge reader never blesses another unresolved operation.
    for other_target, other in bucket["items"].items():
        if other_target == target:
            continue
        op = other.get("operation")
        _need(
            op is None
            or (
                op.get("status") == "completed"
                and op.get("result", {}).get("outcome_uncertain") is not True
            ),
            "unrelated unresolved queue operation remains owned",
        )
    for entry_bucket in [state, *state["frontiers"].values()]:
        for other in entry_bucket.get("items", {}).values():
            _no_uncertainty(other)
            for prior in [other.get("operation"), *other.get("operation_history", [])]:
                accepted = (
                    {"completed"}
                    if prior is other.get("operation")
                    else {"completed", "confirmed_not_started", "abandoned"}
                )
                _need(
                    prior is None
                    or (
                        prior.get("status") in accepted
                        and prior.get("result", {}).get("outcome_uncertain") is not True
                        and prior.get("result", {}).get("status") not in {"uncertain", "unknown"}
                    ),
                    "unrelated unresolved queue history remains owned",
                )
                if prior is not None:
                    _no_uncertainty(prior)
                    _no_uncertainty(prior.get("result", {}))
    return original, retained


def _proof_knowledge(record, admission, context, row):
    """Authenticate source-issued immutable proof/qualification knowledge only.

    The cutoff comes exclusively from original source writer issuance. It is
    neither a caller clock nor a way to renew an old materializer/ACT lease.
    No current runtime qualification or installed reader is invoked here.
    """
    import closer_correction_source_record as source
    import closer_dispatcher as closer
    import closer_role_qualification as roles
    import execution_profiles

    snapshots = json.loads(record["original_proofs_json"])
    proofs = [
        context[stage]["actual"]["_closer_controlled_proof"] for stage in ("interpret", "exception")
    ]
    proofs.append(admission["materializer"]["proof"])
    _need(
        type(snapshots) is list
        and len(snapshots) == len(proofs) == 3
        and len({p["run_id"] for p in proofs}) == 3,
        "original proof/qualification inventory differs",
    )
    cutoff = record["issued_at_ms"] / 1000
    controlled_results = []
    for saved, proof in zip(snapshots, proofs):
        _need(
            set(saved) == {"proof", "qualification", "role"} and saved["proof"] == proof,
            "source-issued original proof bytes changed",
        )
        expected, qualified = proof["expected"], saved["qualification"]
        profile = execution_profiles.get_profile(expected["profile_id"])
        role = roles.TASK_ROLES[expected["task_type"]]
        _need(
            saved["role"] == role
            and qualified["qualification_id"] == expected["qualification_id"]
            and qualified["profile_identity"] == expected["profile_identity"]
            and closer._contract_digest(qualified) == expected["qualification_record_sha256"]
            and qualified["production_model_contract"] == expected["production_model_contract"]
            and not roles.qualification_problems(
                profile, role, now=cutoff, records={qualified["qualification_id"]: qualified}
            ),
            "source-issued qualification identity/range differs",
        )
        result = closer._revalidate_controlled_proof(
            proof,
            run_id=proof["run_id"],
            basis=expected["basis"],
            receipt_root=admission["materializer"]["receipt_root"],
        )
        controlled_results.append(result)
        receipt = closer._read_controlled_receipt(
            proof["provider_result_ref"], admission["materializer"]["receipt_root"]
        )
        _need(
            receipt["run_id"] == result["run_id"] == proof["run_id"]
            and receipt["profile_id"] == expected["profile_id"]
            and receipt["started_at"] <= receipt["completed_at"] <= cutoff
            and proof["observed"]["finished"]["cleanup_terminal"] is True
            and expected["finite_frontier_id"] == record["finite_frontier"]["frontier_id"]
            and expected["finite_frontier_target"] == record["target"],
            "original completed provider proof differs",
        )
    prep, snapshot, packet, root = source._prepared(context)
    original = closer._read_controlled_receipt(record["source_record_ref"], root)
    _need(
        _hash(original) == record["original_source_record_sha256"]
        and original["schema"] == source.SCHEMA
        and original["issuer_source_sha256"] == source._LOADED_SOURCE_SHA256
        and original["original_frame"]["state_path"] == context["state_path"]
        and original["original_frame"]["pipeline_run_id"] == context["pipeline_run_id"]
        and original["original_frame"]["work_key"] == row["stage_results"]["exception"]["work_key"]
        and original["snapshot_ref"] == prep["snapshot_ref"]
        and original["packet_ref"] == prep["packet_ref"],
        "protected original source record/frame differs",
    )
    intent = closer._read_controlled_receipt(original["intent_ref"], root)
    link = closer._read_controlled_receipt(original["result_link_ref"], root)
    result = closer._read_controlled_receipt(original["native_result_ref"], root)
    decision = closer._read_controlled_receipt(record["decision_ref"], root)
    _need(
        link["intent_sha256"] == original["intent_ref"]["sha256"]
        and link["result_ref"] == original["native_result_ref"]
        and intent["original_frame"] == original["original_frame"]
        and intent["identity"] == original["identity"]
        and intent["original_request"] == admission["request"]
        and source.materializer_arguments(intent, result) == admission["materializer"]
        and decision == result["decision"]
        and closer._contract_digest(decision) == original["classifier_decision_sha256"]
        and original["classifier_run_id"] == proofs[-1]["run_id"],
        "protected original decision/qualification/request bytes differ",
    )
    # Replay only the pure semantic projection from the exact emitted classifier
    # result. This does not call either materializer or a historical role scope.
    import closer_correction_bridge as bridge
    import closer_correction_decision as decisions
    import closer_correction_producer as producer
    import closer_data_only_execution as execution

    args = admission["materializer"]
    inputs, classification, prompt, basis, decision_root = producer.prepare(
        **{
            k: args[k]
            for k in (
                "original_request",
                "seed_bundle",
                "source_body",
                "raw_findings",
                "retained_packet_ref",
                "basis",
            )
        }
    )
    expected = proofs[-1]["expected"]
    _need(
        args["basis"] == basis
        and expected["basis"] == basis
        and expected["task_type"] == producer.TASK
        and expected["profile_id"] == args["profile_id"]
        and expected["prompt"]
        == execution.validate_payload(prompt, limits=expected["limits"], kind="prompt")
        and closer._read_controlled_receipt(args["request_ref"], decision_root) == classification
        and bridge._owner_semantic_digest(admission["request"], intent["inputs"])
        == original["owner_semantics_sha256"],
        "original selected classifier prompt/request semantics differ",
    )
    qualified = snapshots[-1]["qualification"]
    projected = decisions.project_explicit_semantics(
        semantic=closer._strict_response_json(controlled_results[-1]["output"]),
        request=classification,
        profile={
            "id": args["profile_id"],
            "role": "closer_interpret",
            "qualification_ref": "github:stranske/Orchestrator:src/closer_role_qualification.py#ROLE_QUALIFICATIONS/"
            + qualified["qualification_id"],
            "qualification_sha256": closer._contract_digest(qualified),
        },
        **inputs,
    )
    projected.update(
        retained_packet_ref=args["retained_packet_ref"]["path"],
        decision_evidence_ref=proofs[-1]["provider_result_ref"]["path"],
    )
    _need(projected == decision, "original classifier emitted decision differs")
    source._ordinary_selected({"decision": decision}, admission["request"])
    facts = {
        "request": admission["request"],
        "raw_findings": intent["inputs"]["raw_findings"],
        "seed_selector": admission["seed_selector"],
        "materializer": admission["materializer"],
        "original_source_context": original["original_source_context"],
        "original_source_native": original["original_source_native"],
        "original_incumbent_context": original["original_incumbent_context"],
        "source_record_ref": record["source_record_ref"],
        "decision": decision,
    }
    _need(
        _hash(facts) == record["original_facts_sha256"],
        "original source/context/tests/finding identities differ",
    )
    return facts


def _pipeline_view(document, frontier_id, target):
    bucket = document["frontiers"][frontier_id]
    return {
        "finite_frontier": bucket["finite_frontier"],
        "run_id": bucket["run_id"],
        "row": bucket["items"][target],
    }


def _campaign_view(document, admission_sha256, worker_id):
    registration = document["correction_controller"]
    return {
        "identity": {k: document[k] for k in ("campaign_id", "owner", "generation", "release_pin")},
        "config": registration["config"],
        "admission": registration["admission_records"][admission_sha256],
        "workers": [w for w in document["workers"] if w.get("worker_id") == worker_id],
    }


def _reread_pipeline(path, frontier_id, target, expected_sha256):
    import closer_correction_controller as ctl

    current = ctl.IndexController._physical_state(path, "source pipeline redirected during read")
    _need(
        _hash(_pipeline_view(current, frontier_id, target)) == expected_sha256,
        "original pipeline row/run/frontier changed during read",
    )


def _read_boundary(
    *,
    manifest_path,
    state_path,
    target,
    finite_frontier,
    proof_ref,
    expected_operation_id,
    expected_stage,
):
    import closer_campaign as campaign_source
    import closer_correction_controller as ctl
    import closer_native_observer as native
    import closer_runtime_binding as runtime

    path = Path(manifest_path)
    _need(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "manifest redirected",
    )
    manifest = campaign_source.load_manifest(path)
    _need(
        not campaign_source.validate_manifest(manifest)
        and not campaign_source.verify_modules(manifest)
        and not native.qualification_receipt_problems(manifest),
        "original pinned source qualification unavailable",
    )
    runtime.CoreBundle(manifest["portable"])
    config = ctl.ControllerConfig(
        **manifest["policy"]["correction_controller_installation"]["config"]
    )
    campaign = ctl.IndexController._physical_state(config.state_path, "source campaign redirected")
    _need(
        campaign["correction_controller"]["config"] == config.source_binding(),
        "source controller registration differs",
    )
    custody = ctl._Custody(config)
    custody._inspection_read_budget = [min(config.max_bytes * 16, 32 * 1024 * 1024)]
    try:
        record = custody.read(proof_ref["name"])
        _need(
            record["schema"] == SCHEMA
            and _ref(record) == proof_ref
            and record["issuer_source_sha256"] == _LOADED_SOURCE_SHA256
            and record["controller_config"] == config.source_binding()
            and str(state_path) == record["queue_path"]
            and record["manifest_path"] == str(path)
            and record["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            and all(record.get(k) is False for k in _FALSE),
            "protected source-issued terminal differs",
        )
        base = Path(manifest["module_dir"])
        closure = {
            name: {"path": str(base / name), "sha256": sha}
            for name, sha in manifest["modules"].items()
        }
        _need(
            ctl.digest(closure) == record["source_closure_sha256"]
            and closure[Path(__file__).name]
            == {"path": str(Path(__file__)), "sha256": _LOADED_SOURCE_SHA256}
            and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _LOADED_SOURCE_SHA256,
            "original enrollment source closure differs",
        )
        inspection = ctl.inspect_provisioned_custody(config.source_binding())
        _need(
            not inspection["highwater"]["pending_transactions"]
            and inspection["highwater"]["anchor"] == record["checkpoint"]
            and inspection["identity"]["custody_path"] == str(custody.path),
            "unresolved or changed controller highwater",
        )
        anchor = record["checkpoint"]
        checkpoint = custody.read(anchor["result_name"])
        _need(ctl.digest(checkpoint) == anchor["result_sha256"], "exact checkpoint result changed")
        entries = checkpoint["after"]["data"]["entries"]
        selected = [e for e in entries if e["key"] == record["effect_key"]]
        _need(
            len(selected) == 1 and ctl.digest(selected[0]) == record["entry_sha256"],
            "confirmed original entry differs",
        )
        entry = selected[0]
        admission = custody.read(
            "admission-" + entry["admitted_selector"]["record_sha256"] + ".json"
        )
        _need(
            ctl.digest(admission)
            == record["admission_sha256"]
            == entry["admitted_selector"]["record_sha256"]
            and entry["admitted_selector"] == record["admitted_selector"]
            and admission["request"] == entry["request"]
            and ctl.effect_key(admission["request"]) == record["effect_key"]
            and admission["request"]["request_sha256"] == record["request_sha256"],
            "original admission registration differs",
        )
        # The registry binds the original classification decision, checked below.
        original, retained = _queue_operation(
            record, proof_ref, target, finite_frontier, expected_operation_id
        )
        _need(
            type(record["issued_at_ms"]) is int
            and original["submitted_at"] <= record["issued_at_ms"] / 1000 < original["deadline"],
            "original source issuance cutoff outside original callback",
        )
        start = _native_start(manifest, campaign, config, record["native_b_start"], original)
        campaign_snapshot_sha = _hash(
            _campaign_view(campaign, record["admission_sha256"], start["worker_id"])
        )
        context = json.loads(record["context_identity_json"])
        stage = {
            "pipeline_state_path": context["state_path"],
            **{
                k: context[k]
                for k in (
                    "pipeline_run_id",
                    "work_key",
                    "identity",
                    "revision",
                    "action_state_path",
                )
            },
        }
        _need(
            type(expected_stage) is dict and expected_stage == stage,
            "exact original ACT stage selector differs",
        )
        pipeline = ctl.IndexController._physical_state(
            context["state_path"], "source pipeline redirected"
        )
        bucket = pipeline["frontiers"][finite_frontier["frontier_id"]]
        row = bucket["items"][target]
        pipeline_snapshot_sha = _hash(
            _pipeline_view(pipeline, finite_frontier["frontier_id"], target)
        )
        _stage_identity(row, context)
        _need(
            bucket["finite_frontier"] == finite_frontier
            and bucket["run_id"] == context["pipeline_run_id"]
            and row["identity"] == context["identity"]
            and row["revision"] == context["revision"]
            and row["stage_results"]["exception"]["status"] == "completed"
            and row["results"]["exception"]["correction"]["source_decision_ref"]
            == record["source_record_ref"]
            and context["action_state_path"] == record["queue_path"],
            "original pipeline stage/source identity differs",
        )
        context.update({k: row["results"][k] for k in ("prepare", "interpret", "exception")})
        _need(
            _hash({k: context[k] for k in ("prepare", "interpret", "exception")})
            == record["context_results_sha256"],
            "original source stage results changed",
        )
        facts = _proof_knowledge(record, admission, context, row)
        _need(
            campaign["correction_controller"]["admission_records"][record["admission_sha256"]]
            == ctl.digest(facts["decision"]),
            "source decision registration differs",
        )
        instruction = _effect(
            custody,
            config,
            admission["request"],
            phase="instruction_publication",
            proof_sha=admission["own_instruction"]["proof_sha256"],
            source_ref=record["source_record_ref"],
            seed=admission["seed_selector"],
        )
        _need(
            instruction == record["instruction_effect"]
            and instruction["native"]["owner_instruction"] == admission["owner_instruction"]
            and instruction["owner"]["operation_id"] == original["id"]
            and instruction["owner"]["operation_sha256"] == record["operation_sha256"]
            and instruction["owner"]["process_id"] == start["process"]["pid"],
            "original publication source acknowledgment differs",
        )
        if admission["request"]["incumbent"].get("kind") == "no_incumbent":
            stages = entry["stages"]
            _need(
                len(stages) == 1
                and stages[0]["phase"] == "issue_creation"
                and stages[0]["state"] == "confirmed",
                "required issue membership incomplete",
            )
            issue = _effect(
                custody,
                config,
                admission["request"],
                phase="issue_creation",
                proof_sha=stages[0]["proof_sha256"],
                source_ref=record["source_record_ref"],
                seed=admission["seed_selector"],
            )
            _need(
                issue == record["issue_effect"]
                and issue["source_before_sha256"] == instruction["source_after_sha256"]
                and issue["owner"]["operation_id"] == original["id"]
                and issue["owner"]["operation_sha256"] == record["operation_sha256"]
                and issue["owner"]["process_id"] == start["process"]["pid"]
                and stages[0]["result"] == issue["native"]["subject"]
                and stages[0]["result"] in entry["subjects"]
                and issue["native_lifecycle"]["native_b_start"] == record["native_b_start"]
                and issue["native_lifecycle"]["operation_sha256"] == record["operation_sha256"],
                "original required issue proof differs",
            )
        else:
            _need(
                not entry["stages"]
                and record["issue_effect"]
                == {
                    "status": "native_issue_not_required",
                    "incumbent_sha256": ctl.digest(admission["request"]["incumbent"]),
                },
                "original incumbent exemption differs",
            )
        custody.check()
        _reread_pipeline(
            context["state_path"], finite_frontier["frontier_id"], target, pipeline_snapshot_sha
        )
        current_campaign = ctl.IndexController._physical_state(
            config.state_path, "source campaign redirected during read"
        )
        _need(
            _hash(_campaign_view(current_campaign, record["admission_sha256"], start["worker_id"]))
            == campaign_snapshot_sha,
            "original campaign registration/worker changed during read",
        )
        _need(
            custody.read(proof_ref["name"]) == record
            and ctl.inspect_provisioned_custody(config.source_binding()) == inspection
            and hashlib.sha256(path.read_bytes()).hexdigest() == record["manifest_sha256"]
            and not campaign_source.verify_modules(manifest)
            and _queue_operation(record, proof_ref, target, finite_frontier, expected_operation_id)
            == (original, retained),
            "original terminal/namespace/source/queue changed during read",
        )
        return {
            "schema": BOUNDARY,
            "reference": dict(proof_ref),
            "operation_id": original["id"],
            "operation_sha256": record["operation_sha256"],
            "stage": stage,
            **{
                k: record[k]
                for k in (
                    "target",
                    "finite_frontier",
                    "effect_key",
                    "request_sha256",
                    "admitted_selector",
                    "native_b_start",
                    "checkpoint",
                )
            },
            **dict.fromkeys(_FALSE, False),
        }
    finally:
        custody.close()
