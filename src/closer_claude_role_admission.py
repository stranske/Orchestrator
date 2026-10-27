"""One current-source public Sonnet capability canary; no enrollment authority.

The existing Claude broker owns OAuth custody, physical binary/argv/environment,
private runtime, shared pool and child supervision. This module only bridges the
empty role registry for ONE explicitly selected public named-question probe.
Receivers propose inactive rows; source review alone may enroll a reviewed row.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import stat
import sys
import threading
import time
from pathlib import Path

PROFILE_ID = "claude-sonnet-5-5-high-v2"
PROFILE_IDENTITY_SHA256 = "c6ea6091c54feec59a7405a17795236d7362aa19290d8b11cd77333271076bcd"
QUALITY_SHA256 = "7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0"
TASK = "closer_exception"
ROLE = "strong_criterion_adjudication"
PREFIX = "sonnet-current-role-qualification:"
SCHEMA = "closer-claude-current-role-canary/v1"
PROPOSAL_SCHEMA = "closer-public-csv-role-canary-input/v1"
PUBLIC_TARGET = "stranske/orchestrator#984"
PUBLIC_SOURCE = "stranske/orchestrator#983"
PUBLIC_SOURCE_SHA256 = "64a22ce25c538cfeb98c058fc66bca2165ac829df1c037e5b95cefa22959646c"
PUBLIC_HEADS = frozenset(
    {"0507d28acf68d6bf21c68138d18d56cd01968827", "d1947b5b4775c2e6a07bb46b3f85eb2e28c1b001"}
)
PUBLIC_FILE = "tests/fixtures/closer_real_csv_canary/v1/csv_export.py"
PUBLIC_CRITERION = (
    "CSV must emit exactly the header name,score, CRLF record endings and one record per input. "
    "Values containing commas, quotes, LF or CR must round-trip through Python csv.reader. "
    "Empty input must return the header alone."
)
QUESTION = "Does the retained public CSV canary implementation satisfy the original CSV round-trip criterion?"
_SEAL = object()
_CURRENT = contextvars.ContextVar("source_sonnet_role_qualification", default=None)
MAX_REPORT_BYTES = 512 * 1024
MAX_PROPOSAL_BYTES = 128 * 1024


def _need(condition, reason):
    if not condition:
        import closer_dispatcher

        raise closer_dispatcher.ControlledOriginUnavailable(reason)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def is_qualification_operation(operation_id):
    return isinstance(operation_id, str) and operation_id.startswith(PREFIX)


def is_qualification_result(result):
    origin = result.get("data_only_origin") if isinstance(result, dict) else None
    return isinstance(origin, dict) and is_qualification_operation(origin.get("operation_id"))


class _Permit:
    def __init__(self, seal, mode, operation_id, record, deadline):
        _need(seal is _SEAL, "source qualification seal unavailable")
        self.seal, self.mode = seal, mode
        self.operation_id, self.record_digest = operation_id, _digest(record)
        self.deadline, self.thread = deadline, threading.get_ident()
        self.active, self.launch_used = True, False
        self.token = None


def _issue_permit(mode, operation_id, record, deadline):
    caller = sys._getframe(1)
    expected = (
        run_current_source_public_qualification if mode == "launch" else reviewed_record_proposal
    )
    _need(
        mode in {"launch", "readback"}
        and caller.f_code is expected.__code__
        and caller.f_globals is globals()
        and _CURRENT.get() is None
        and is_qualification_operation(operation_id),
        "actual source qualification issuer required",
    )
    permit = _Permit(_SEAL, mode, operation_id, record, deadline)
    permit.token = _CURRENT.set(permit)
    return permit


def _live_permit(permit, operation_id):
    _need(
        type(permit) is _Permit
        and permit.seal is _SEAL
        and permit is _CURRENT.get()
        and permit.active
        and permit.thread == threading.get_ident()
        and time.time() < permit.deadline
        and permit.operation_id == operation_id,
        "live source qualification permit required",
    )


def require_launch_permit(permit, operation_id, profile, record):
    import closer_dispatcher
    import closer_role_qualification as roles

    caller = sys._getframe(1)
    issuer = caller.f_back
    _live_permit(permit, operation_id)
    _need(
        caller.f_code is closer_dispatcher._controlled_launch_inputs.__code__
        and caller.f_globals is closer_dispatcher.__dict__
        and issuer is not None
        and issuer.f_code is run_current_source_public_qualification.__code__
        and issuer.f_globals is globals()
        and permit.mode == "launch"
        and not permit.launch_used
        and _digest(record) == permit.record_digest
        and roles.identity_digest(profile) == PROFILE_IDENTITY_SHA256,
        "original source qualification launch caller differs",
    )
    permit.launch_used = True


def require_validation_permit(permit, expected):
    import closer_dispatcher

    caller = sys._getframe(1)
    issuer = caller.f_back
    operation_id = expected.get("operation_id")
    _live_permit(permit, operation_id)
    entry = (
        run_current_source_public_qualification
        if permit.mode == "launch"
        else reviewed_record_proposal
    )
    _need(
        caller.f_code is closer_dispatcher._validate_controlled_result.__code__
        and caller.f_globals is closer_dispatcher.__dict__
        and issuer is not None
        and issuer.f_code is entry.__code__
        and issuer.f_globals is globals()
        and expected.get("qualification_only") is True
        and expected.get("profile_id") == PROFILE_ID
        and expected.get("task_type") == TASK
        and expected.get("qualification_id") == operation_id
        and expected.get("qualification_record_sha256") == permit.record_digest
        and is_qualification_operation(operation_id),
        "source qualification validation caller differs",
    )


def _close(permit):
    permit.active = False
    _CURRENT.reset(permit.token)


def _read(ref, *, root=None):
    _need(
        isinstance(ref, dict) and set(ref) == {"path", "bytes", "sha256"}, "physical ref required"
    )
    path = Path(ref["path"])
    _need(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and not any(parent.is_symlink() for parent in (path, *path.parents))
        and (root is None or path.is_relative_to(root))
        and type(ref["bytes"]) is int
        and 0 < ref["bytes"] <= MAX_REPORT_BYTES,
        "physical bounded reference required",
    )
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        _need(
            stat.S_ISREG(info.st_mode)
            and info.st_size == ref["bytes"]
            and info.st_uid == os.getuid(),
            "owned regular reference required",
        )
        raw = stream.read(MAX_REPORT_BYTES + 1)
    _need(
        len(raw) == ref["bytes"] and hashlib.sha256(raw).hexdigest() == ref["sha256"],
        "reference bytes changed",
    )
    import closer_model_contracts

    return closer_model_contracts.strict_response_json(raw.decode())


def _quality(ref):
    import closer_role_qualification as roles
    import closer_role_selection as selection
    import execution_profiles

    _need(isinstance(ref, dict), "bound quality evidence reference required")
    profile = execution_profiles.get_profile(PROFILE_ID)
    _need(
        roles.identity_digest(profile) == PROFILE_IDENTITY_SHA256, "exact Sonnet identity changed"
    )
    scope = selection.quality_scope(profile, ROLE, "named_proof")
    _need(
        isinstance(scope, dict)
        and scope.get("subject") == "sonnet_high_v2"
        and scope.get("quality_evidence", {}).get("sha256") == QUALITY_SHA256
        and scope.get("runtime_enrollment_granted") is False
        and scope.get("action_authority") is False
        and ref.get("sha256") == QUALITY_SHA256,
        "reviewed exact named Sonnet quality scope required",
    )
    record = _read(ref)
    _need(
        record.get("schema") == "closer-scoped-role-evidence-record/v1"
        and record.get("status") == "scoped_evidence_eligible_not_runtime_enrolled"
        and record.get("scope", {}).get("profile_identity") == roles.profile_identity(profile)
        and record["scope"].get("role") == ROLE
        and record["scope"].get("named_question_only") is True,
        "original quality evidence differs",
    )
    return profile, scope


def _limits(descriptor):
    return {
        "schema": "closer-data-only-limits/v1",
        "role": ROLE,
        "model_contract_sha256": descriptor["contract_sha256"],
        "max_prompt_bytes": 65536,
        "max_final_bytes": 16384,
        "max_native_result_bytes": 65536,
        "max_output_tokens": 4096,
        "max_turns": 1,
        "timeout_seconds": 60,
    }


def _prepare(proposal):
    import closer_dispatcher
    import closer_execution_contracts as contracts

    _need(
        type(proposal) is dict
        and set(proposal)
        == {"schema", "snapshot", "packet_ref", "original", "basis", "evidence_refs"}
        and proposal["schema"] == PROPOSAL_SCHEMA,
        "closed public qualification input required",
    )
    _need(
        len(json.dumps(proposal, ensure_ascii=False, allow_nan=False).encode())
        <= MAX_PROPOSAL_BYTES,
        "complete public qualification input exceeds retention bound",
    )
    snapshot = proposal["snapshot"]
    _need(
        isinstance(snapshot, dict)
        and str(snapshot.get("target", "")).casefold() == PUBLIC_TARGET
        and snapshot.get("head") in PUBLIC_HEADS,
        "exact public canary target/head required",
    )
    sources = [
        s
        for s in snapshot.get("sources", [])
        if isinstance(s, dict) and str(s.get("target", "")).casefold() == PUBLIC_SOURCE
    ]
    _need(
        len(sources) == 1
        and isinstance(sources[0].get("body"), str)
        and hashlib.sha256(sources[0]["body"].encode()).hexdigest() == PUBLIC_SOURCE_SHA256,
        "unchanged original public task required",
    )
    packet, root = contracts.load_retained_packet(proposal["packet_ref"])
    request = contracts.build_adjudication_request(
        snapshot,
        packet,
        proposal["packet_ref"],
        basis=proposal["basis"],
        question=QUESTION,
        evidence_refs=proposal["evidence_refs"],
        retained_dir=root,
    )
    _need(
        request.get("criterion") == PUBLIC_CRITERION
        and str(request["basis"].get("source", "")).casefold() == PUBLIC_SOURCE
        and bool(request["proof_identities"])
        and all(
            alt.get("kind") == "codepatch" and alt.get("filename") == PUBLIC_FILE
            for item in request["proof_identities"]
            for alt in item["alternatives"]
        ),
        "one exact public CSV criterion and its retained codepatch required",
    )
    prompt = contracts.build_adjudication_prompt(proposal["original"], request, retained_dir=root)
    descriptor = closer_dispatcher._controlled_model_contract(TASK)
    return packet, root, request, prompt, descriptor


def _directory():
    import dispatcher

    parent = Path(dispatcher.DISPATCH_LOG_DIR).resolve(strict=True)
    _need(
        not Path(dispatcher.DISPATCH_LOG_DIR).is_symlink(),
        "actual dispatcher receipt root required",
    )
    root = parent / "sonnet-role-qualification"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = root.lstat()
    _need(
        stat.S_ISDIR(info.st_mode)
        and not root.is_symlink()
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "private qualification receipt directory required",
    )
    return root


def _write_once(path, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    _need(0 < len(raw) <= MAX_REPORT_BYTES, "bounded qualification receipt required")
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
    ) as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def run_current_source_public_qualification(proposal, *, quality_ref, deadline):
    """ONE fresh canary through the actual broker; never register, repair or ACT."""
    import adapters
    import closer_dispatcher as closer
    import closer_execution_contracts as contracts
    import closer_resources as resources
    import dispatcher

    now = time.time()
    _need(
        type(deadline) in (int, float) and math.isfinite(deadline) and 0 < deadline - now <= 90,
        "qualification deadline must fit90seconds",
    )
    profile, scope = _quality(quality_ref)
    packet, retained, request, prompt, descriptor = _prepare(proposal)
    limits = _limits(descriptor)
    _need(
        closer._profile_has_capacity(closer._fresh_capacity_report(), profile)
        and adapters.automatic_profile_within_cap(profile),
        "current capacity and existing operator cap required",
    )
    _need(
        not resources.finite_pool_has_active_reservations(),
        "qualification requires its own fresh finite reservation",
    )
    root = _directory()
    identity = _digest(
        {
            "request": request["request_id"],
            "descriptor": descriptor["contract_sha256"],
            "profile": PROFILE_ID,
        }
    )
    operation_id = PREFIX + identity
    pending = {
        "qualification_id": operation_id,
        "production_model_contract": descriptor,
        "execution_limits": limits,
    }
    intent = {
        "schema": "closer-claude-role-canary-intent/v1",
        "operation_id": operation_id,
        "created_at": now,
        "request_id": request["request_id"],
        "descriptor_sha256": descriptor["contract_sha256"],
        "quality_ref": quality_ref,
        "qualification_only": True,
        "authority": False,
    }
    intent_ref = _write_once(root / (identity + ".intent.json"), intent)
    frontier = {
        "frontier_id": operation_id,
        "bindings": {request["target"]: {"revision": request["revision"]}},
    }
    pool_root = (
        Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex/handoff"))
        / "closer-profile-execution"
    )
    reservation = {
        "reservation_id": _digest({"root": str(pool_root.resolve()), "operation": operation_id}),
        "frontier_id": frontier["frontier_id"],
        "target": request["target"],
        "agent": "claude",
        "profile_id": PROFILE_ID,
    }
    report = {
        "schema": SCHEMA,
        "operation_id": operation_id,
        "proposal": proposal,
        "quality_ref": quality_ref,
        "quality_scope": scope,
        "intent_ref": intent_ref,
        "started_at": now,
        "qualification_only": True,
        "authority": False,
        "activation": False,
        "retry_authorized": False,
        "capability_pass": False,
    }
    permit = None
    try:
        with resources.pool_slot(
            pool_root,
            hashlib.sha256(b"claude-subscription").hexdigest(),
            1,
            deadline=min(deadline, time.time()),
            finite_frontier=True,
            reservation=reservation,
        ):
            reservation_identity = {
                "agent": "claude",
                "profile_id": PROFILE_ID,
                "frontier_id": frontier["frontier_id"],
                "target": request["target"],
            }
            try:
                permit = _issue_permit("launch", operation_id, pending, deadline)
                options, expected, observed = closer._controlled_launch_inputs(
                    profile=profile,
                    record=pending,
                    task_type=TASK,
                    operation_id=operation_id,
                    frontier=frontier,
                    target=request["target"],
                    prompt=prompt,
                    qualification=permit,
                )
                expected["basis"] = {
                    key: request[key]
                    for key in ("target", "head", "base", "revision", "packet_sha256", "request_id")
                }
                remaining = deadline - time.time()
                _need(remaining > 0, "qualification deadline exhausted before native submission")
                result = dispatcher.offload(
                    "claude",
                    prompt,
                    mode="assess",
                    profile_id=PROFILE_ID,
                    task_type=TASK,
                    data_only=True,
                    max_network_retries=0,
                    subject_id=request["target"],
                    timeout=min(60, remaining),
                    request_deadline_mono=time.monotonic() + remaining,
                    **options,
                )
                report["result"] = result
                if isinstance(result, dict) and result.get("exit") == 0:
                    proof = closer._validate_controlled_result(
                        result,
                        expected,
                        observed,
                        receipt_root=dispatcher.DISPATCH_LOG_DIR,
                        _qualification=permit,
                    )
                    answer = contracts.validate_adjudication_response(
                        request,
                        closer._worker_proposal(result, agent="claude"),
                        snapshot=proposal["snapshot"],
                        packet=packet,
                        packet_ref=proposal["packet_ref"],
                        retained_dir=retained,
                    )
                    report.update(
                        callback_proof=proof,
                        request=request,
                        answer=answer,
                        capability_pass=answer["outcome"] == "unsatisfied",
                    )
                else:
                    resources.finite_pool_no_start(expected=reservation_identity)
            except BaseException:
                resources.finite_pool_no_start(expected=reservation_identity)
                raise
    except Exception as error:
        report["failure_type"] = type(error).__name__
    finally:
        if permit is not None:
            _close(permit)
    report["completed_at"] = time.time()
    ref = _write_once(root / (identity + ".result.json"), report)
    return {
        "report_ref": ref,
        "qualification_only": True,
        "capability_pass": report["capability_pass"],
        "authority": False,
        "activation": False,
    }


def reviewed_record_proposal(report_ref):
    """Revalidate physical original callback proof and propose an INACTIVE row.

    No caller expiry or registry mutation is accepted. The existing registry has
    no maximum-lifetime policy: source enrollment review must set an explicit
    durable expiry; a fixture TTL must not silently become production policy.
    """
    import closer_data_only_execution as execution
    import closer_dispatcher as closer
    import closer_execution_contracts as contracts
    import closer_role_qualification as roles
    import dispatcher

    report = _read(report_ref, root=_directory())
    _need(
        report.get("schema") == SCHEMA
        and report.get("qualification_only") is True
        and report.get("capability_pass") is True
        and report.get("authority") is False
        and report.get("activation") is False
        and report.get("retry_authorized") is False,
        "completed qualification-only native report required",
    )
    _need(
        all(
            type(report.get(key)) in (int, float) and math.isfinite(report[key])
            for key in ("started_at", "completed_at")
        )
        and 0 <= report["started_at"] <= report["completed_at"] <= time.time(),
        "original native qualification times invalid",
    )
    profile, scope = _quality(report["quality_ref"])
    _need(scope == report["quality_scope"], "reviewed quality scope changed")
    packet, retained, request, _prompt, descriptor = _prepare(report["proposal"])
    proof = report["callback_proof"]
    expected, observed = proof["expected"], proof["observed"]
    _need(
        expected["production_model_contract"] == descriptor
        and expected["profile_identity"] == roles.profile_identity(profile)
        and report["request"] == request
        and expected["operation_id"] == report["operation_id"],
        "current canary source/request identity changed",
    )
    intent = _read(report["intent_ref"], root=_directory())
    _need(
        intent["operation_id"] == report["operation_id"]
        and intent["request_id"] == request["request_id"]
        and intent["descriptor_sha256"] == descriptor["contract_sha256"]
        and intent["created_at"] == report["started_at"],
        "original immutable qualification intent differs",
    )
    pending = {
        "qualification_id": report["operation_id"],
        "production_model_contract": descriptor,
        "execution_limits": _limits(descriptor),
    }
    permit = _issue_permit("readback", report["operation_id"], pending, time.time() + 30)
    try:
        checked = closer._validate_controlled_result(
            report["result"],
            expected,
            observed,
            receipt_root=dispatcher.DISPATCH_LOG_DIR,
            _qualification=permit,
        )
        _need(checked == proof, "original canary callback proof changed")
        answer = contracts.validate_adjudication_response(
            request,
            closer._worker_proposal(report["result"], agent="claude"),
            snapshot=report["proposal"]["snapshot"],
            packet=packet,
            packet_ref=report["proposal"]["packet_ref"],
            retained_dir=retained,
        )
        _need(
            answer == report["answer"] and answer["outcome"] == "unsatisfied",
            "original named canary answer changed",
        )
    finally:
        _close(permit)
    record = {
        "schema": roles.SCHEMA,
        "status": "pending_review",
        "qualification_id": report["operation_id"],
        "role": ROLE,
        "profile_identity": roles.profile_identity(profile),
        "profile_identity_sha256": roles.identity_digest(profile),
        "qualified_at": report["completed_at"],
        "expires_at": None,
        "transport": "offload",
        "named_criterion_only": True,
        "confinement": {
            "data_only": True,
            "tools": False,
            "mcp": False,
            "hooks": False,
            "project_context": False,
            "adapter_build_sha256": execution.CLAUDE_BINARY_SHA256,
        },
        "execution_limits": _limits(descriptor),
        "production_model_contract": descriptor,
        "evidence_refs": [
            {"reference": report_ref["path"], "sha256": report_ref["sha256"]},
            {"reference": report["quality_ref"]["path"], "sha256": QUALITY_SHA256},
        ],
    }
    return {
        "record": record,
        "review_required": [
            "independent native result review",
            "explicit source-owned durable expiry",
            "reviewed registry insertion",
        ],
        "enrollment_granted": False,
        "authority": False,
        "activation": False,
    }
