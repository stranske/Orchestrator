"""Bounded mechanical submission; original queue/owner retain all authority.

OwnerCapability callbacks are trusted source interfaces, never JSON loaders.
They must resolve the real catalog, independently qualify the installed boundary,
and authenticate the independent test collector. This module supplies no catalog,
qualification, collector, retry, registry, scheduler or acceptance authority.
The existing wrapper-only confinement observation ALWAYS remains unknown.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Any, Callable

import closer_evidence as evidence
import closer_execution_contracts as contracts
import closer_local_tool_confinement as confinement
import closer_queue as queue

SCHEMA = "closer-mechanical-execution/v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class OriginalEffectUnknown(ValueError):
    """An original attempt exists; never reinterpret this as known not started."""


@dataclass(frozen=True)
class ToolRegistration:
    """One source-resolved exact finding/tool binding, not a model wire schema."""

    tool_id: str
    target: str
    head: str
    source_owner: str
    finding_id: str
    operation: str
    paths: tuple[str, ...]
    required_test_sha256: str
    expected_failure: str
    argv_prefix: tuple[str, ...]
    executable_sha256: str
    runtime_read_roots: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int


@dataclass(frozen=True)
class OwnerAdmission:
    tool: ToolRegistration
    registered_tests: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class BoundaryPermit:
    """Mint only after the owner's independent installed-tool validation."""

    context_sha256: str
    boundary_sha256: str
    evidence_refs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RegisteredTestObservation:
    """Independent collector result; a tool exit code cannot construct this."""

    context_sha256: str
    boundary_sha256: str
    required_test_sha256: str
    postimages: tuple[tuple[str, str], ...]
    nodeid: str
    raw_junit: bytes
    collector_id: str
    evidence_refs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RegisteredToolCheckObservation:
    """Trusted independent collector protocol; no production producer is registered."""

    context_sha256: str
    boundary_sha256: str
    required_test_sha256: str
    # Local check observation namespace, never a provider/model/GitHub run.
    run_id: str
    process_observation: dict[str, Any]
    raw_receipt: bytes
    collector_id: str
    evidence_refs: tuple[tuple[str, str], ...]
    check_boundary: confinement.Boundary | None = None
    check_permit: BoundaryPermit | None = None


@dataclass(frozen=True)
class OwnerCapability:
    """Source-owned callbacks, invoked only inside the exact live queue callback.

    resolve: return OwnerAdmission from existing source registrations, or None.
    qualify: independently validate actual installed confinement, then mint a
      BoundaryPermit; None/JSON/bool means UNMET, never qualified.
    collect_test: authenticate a separately executed registered-test observation
      against current postimages; never return a pre-fix CI/command-exit summary.
    No callback implementation or production registration is supplied here.
    """

    resolve: Callable[[dict], OwnerAdmission | None]
    qualify: Callable[[dict, confinement.Boundary], BoundaryPermit | None]
    collect_test: Callable[
        [dict, confinement.Boundary, tuple],
        RegisteredTestObservation | RegisteredToolCheckObservation | None,
    ]


def _result(status, reason, operation, **facts):
    return {
        "schema": SCHEMA,
        "status": status,
        "reason": reason,
        "operation_id": operation.get("id") if isinstance(operation, dict) else None,
        "retry_safe": False,
        "authority": False,
        "acceptance_authority": False,
        "role_qualification": False,
        **facts,
    }


def _live(snapshot, operation):
    return (
        isinstance(snapshot, dict)
        and isinstance(operation, dict)
        and operation.get("action") == "repair"
        and operation.get("revision") == snapshot.get("revision")
        and isinstance(operation.get("binding"), dict)
        and operation["binding"].get("head") == snapshot.get("head")
        and queue._currently_invoking(snapshot.get("target"), operation)
    )


def _remaining(operation):
    current = getattr(queue._callback_threads, "invocation", None)
    if (
        not isinstance(current, dict)
        or not callable(current.get("now"))
        or type(operation.get("deadline")) not in {int, float}
        or not math.isfinite(operation["deadline"])
    ):
        raise ValueError("original_operation_clock_unavailable")
    now = current["now"]()
    if type(now) not in {int, float} or not math.isfinite(now):
        raise ValueError("original_operation_clock_unavailable")
    return operation["deadline"] - now


def _references(refs):
    return (
        isinstance(refs, tuple)
        and 1 <= len(refs) <= 8
        and all(
            isinstance(row, tuple)
            and len(row) == 2
            and isinstance(row[0], str)
            and 0 < len(row[0]) <= 4096
            and isinstance(row[1], str)
            and _SHA.fullmatch(row[1])
            for row in refs
        )
    )


def _prepare(plan, inputs, workspace, tracked_paths, scratch, operation, owner):
    snapshot = inputs["snapshot"]
    if not _live(snapshot, operation):
        raise ValueError("original_live_repair_queue_callback_required")
    if type(owner) is not OwnerCapability or not all(
        callable(fn) for fn in (owner.resolve, owner.qualify, owner.collect_test)
    ):
        raise ValueError("source_catalog_confinement_test_collector_UNMET")
    request = {
        "operation_id": operation["id"],
        "operation_sha256": contracts._digest(operation),
        "target": snapshot["target"],
        "head": snapshot["head"],
        "revision": snapshot["revision"],
        "plan_id": plan.get("plan_id") if isinstance(plan, dict) else None,
        # Candidate hints only: the owner must resolve independent source facts,
        # never register commands/tests from this projection.
        "finding_id": plan.get("finding_id") if isinstance(plan, dict) else None,
        "operation": plan.get("operation") if isinstance(plan, dict) else None,
        "source_owner": plan.get("source_owner") if isinstance(plan, dict) else None,
        "paths": copy.deepcopy(plan.get("paths")) if isinstance(plan, dict) else None,
        "required_test": (
            copy.deepcopy(plan.get("required_test")) if isinstance(plan, dict) else None
        ),
    }
    request["context_sha256"] = contracts._digest(request)
    try:
        admission = owner.resolve(copy.deepcopy(request))
    except NotImplementedError as exc:
        raise ValueError("source_catalog_loader_UNMET") from exc
    if not _live(snapshot, operation) or type(admission) is not OwnerAdmission:
        raise ValueError("source_registered_tool_and_test_UNMET")
    admitted = contracts.validate_mechanical_plan(
        plan,
        **inputs,
        workspace=workspace,
        tracked_paths=tracked_paths,
        registered_tests=admission.registered_tests,
    )
    tool = admission.tool
    if type(tool) is not ToolRegistration or not isinstance(tool.tool_id, str) or not tool.tool_id:
        raise ValueError("source_registered_tool_UNMET")
    if (
        any(
            getattr(tool, k) != admitted[k]
            for k in (
                "target",
                "head",
                "source_owner",
                "finding_id",
                "operation",
                "expected_failure",
            )
        )
        or tool.paths != tuple(admitted["paths"])
        or tool.required_test_sha256 != contracts._digest(admitted["required_test_identity"])
        or admitted["required_test_identity"].get("path") in tool.paths
        or not isinstance(tool.argv_prefix, tuple)
        or not tool.argv_prefix
        or not isinstance(tool.executable_sha256, str)
        or not _SHA.fullmatch(tool.executable_sha256)
    ):
        raise ValueError("source_tool_finding_paths_or_test_binding_changed")
    if admitted["required_test_identity"].get("kind") == "tool_check":
        import closer_mechanical_catalog as catalog

        test = admitted["required_test_identity"]
        policy = catalog.validate_tool_check_identity(test, plan)
        if (
            tool.argv_prefix
            != policy.argv(test["executable"], Path(workspace).resolve(strict=True))
            or tool.executable_sha256 != test["executable_sha256"]
            or type(tool.timeout_seconds) is not int
            or not 0 < tool.timeout_seconds <= 30
            or type(tool.max_output_bytes) is not int
            or not 0 < tool.max_output_bytes <= 65536
        ):
            raise ValueError("source_tool_class_argv_binary_or_bounds_changed")
    if _remaining(operation) < tool.timeout_seconds:
        raise ValueError("original_tool_operation_budget_insufficient")
    root = Path(workspace).resolve(strict=True)
    boundary = confinement.prepare_boundary(
        workspace=root,
        write_paths=[root / path for path in tool.paths],
        scratch=scratch,
        runtime_read_roots=list(tool.runtime_read_roots),
        argv=[*tool.argv_prefix, *(str(root / path) for path in tool.paths)],
        timeout_seconds=tool.timeout_seconds,
        max_output_bytes=tool.max_output_bytes,
        operation_id=operation["id"],
    )
    if dict(boundary.source_hashes).get(str(Path(tool.argv_prefix[0]))) != tool.executable_sha256:
        raise ValueError("registered_executable_bytes_changed")
    try:
        permit = owner.qualify(copy.deepcopy(request), boundary)
    except NotImplementedError as exc:
        raise ValueError("installed_confinement_validator_UNMET") from exc
    if (
        not _live(snapshot, operation)
        or type(permit) is not BoundaryPermit
        or permit.context_sha256 != request["context_sha256"]
        or permit.boundary_sha256 != boundary.descriptor_sha256
        or not _references(permit.evidence_refs)
    ):
        raise ValueError("independently_qualified_installed_tool_confinement_UNMET")
    return request, admitted, boundary, tool


@dataclass(frozen=True)
class PreparedMechanical:
    """Ephemeral owner/callback handle, never a JSON permission or queue receipt."""

    request: dict
    admitted: dict
    boundary: confinement.Boundary
    plan: dict
    inputs: dict
    workspace: str
    tracked_paths: dict
    scratch: str
    owner: OwnerCapability
    tool: ToolRegistration


def prepare_mechanical_execution(
    plan,
    *,
    snapshot,
    packet,
    packet_ref,
    retained_dir,
    workspace,
    tracked_paths,
    scratch,
    operation,
    owner=None,
) -> PreparedMechanical:
    current = getattr(queue._callback_threads, "invocation", None)
    if isinstance(current, dict) and current.get("_closer_mechanical_submitted") is not None:
        raise OriginalEffectUnknown("original_callback_already_submitted_tool")
    inputs = dict(
        snapshot=copy.deepcopy(snapshot),
        packet=copy.deepcopy(packet),
        packet_ref=copy.deepcopy(packet_ref),
        retained_dir=str(retained_dir),
    )
    request, admitted, boundary, tool = _prepare(
        plan, inputs, workspace, tracked_paths, scratch, operation, owner
    )
    return PreparedMechanical(
        request,
        admitted,
        boundary,
        copy.deepcopy(plan),
        inputs,
        str(workspace),
        copy.deepcopy(tracked_paths),
        str(scratch),
        owner,
        tool,
    )


def _original(prepared, operation):
    if (
        type(prepared) is not PreparedMechanical
        or not _live(prepared.inputs["snapshot"], operation)
        or prepared.request["operation_sha256"] != contracts._digest(operation)
    ):
        raise ValueError("original_live_prepared_repair_required")


def submit_mechanical(
    prepared, *, operation, starting_callback, started_callback, finished_callback
):
    """Submit at most once; wrapper exit zero never proves a mechanical effect.

    The existing queue owns durable intent, claims and retry/reconciliation. The
    invocation fences repeated submissions; this module supplies no new journal.
    Missing source facts refuse before submission. Every actual wrapper return
    remains held/unknown; no production completion is synthesized here.
    """
    try:
        _original(prepared, operation)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return _result("held_unknown", str(exc), operation, effect_outcome="unknown")
    invocation = queue._callback_threads.invocation
    if invocation.get("_closer_mechanical_submitted") is not None:
        return _result(
            "held_unknown",
            "original_callback_already_submitted_tool",
            operation,
            effect_outcome="unknown",
        )
    try:
        _original(prepared, operation)
        request, admitted, boundary, tool = _prepare(
            prepared.plan,
            prepared.inputs,
            prepared.workspace,
            prepared.tracked_paths,
            prepared.scratch,
            operation,
            prepared.owner,
        )
        if (
            request != prepared.request
            or admitted != prepared.admitted
            or boundary != prepared.boundary
            or tool != prepared.tool
            or not all(
                callable(c) for c in (starting_callback, started_callback, finished_callback)
            )
        ):
            raise ValueError("prepared_source_boundary_or_callbacks_changed")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return _result("unmet", str(exc), operation, process_started=False)
    # Reuse the original invocation lifetime; no new persistent journal/registry.
    # The existing queue already durably saved this submitted original operation.
    invocation["_closer_mechanical_submitted"] = {
        "operation_id": operation["id"],
        "plan_id": prepared.plan["plan_id"],
        "boundary_sha256": boundary.descriptor_sha256,
        "outcome": "unknown",
    }

    def before_spawn(row):
        expected = {
            "source": "local-tool-confinement-request/v1",
            "operation_id": operation["id"],
            "tool_boundary_sha256": boundary.descriptor_sha256,
        }
        if row != expected or not _live(prepared.inputs["snapshot"], operation):
            raise ValueError("original_tool_pre_spawn_identity_or_deadline_changed")
        if _remaining(operation) < boundary.timeout_seconds:
            raise ValueError("original_tool_pre_spawn_budget_insufficient")
        starting_callback(row)
        if (
            not _live(prepared.inputs["snapshot"], operation)
            or _remaining(operation) < boundary.timeout_seconds
        ):
            raise ValueError("original_tool_pre_spawn_budget_insufficient")

    try:
        observed = confinement.run_confined_tool(
            boundary,
            starting_callback=before_spawn,
            started_callback=started_callback,
            finished_callback=finished_callback,
        )
    except Exception as exc:
        return _result("held_unknown", type(exc).__name__, operation, effect_outcome="unknown")
    if (
        isinstance(observed, dict)
        and observed.get("schema") == confinement.SCHEMA
        and observed.get("status") == "refused"
        and observed.get("process_started") is False
        and observed.get("effect_outcome") == "not_started"
    ):
        invocation["_closer_mechanical_submitted"]["outcome"] = "not_started"
        return _result(
            "not_started",
            "confinement_refused_before_submission",
            operation,
            observation=observed,
            effect_outcome="not_started",
        )
    return _result(
        "held_unknown",
        "original_tool_effect_requires_independent_reconciliation",
        operation,
        effect_outcome="unknown",
        observation=observed,
        context_sha256=request["context_sha256"],
        boundary_sha256=boundary.descriptor_sha256,
        required_test_sha256=contracts._digest(admitted["required_test_identity"]),
    )


def _tool_check_proof(receipt, prepared, test, policy, operation, postimages):
    """Join independently collected structured facts; exit zero alone is insufficient."""
    request, boundary = prepared.request, prepared.boundary
    if (
        type(receipt) is not RegisteredToolCheckObservation
        or receipt.context_sha256 != request["context_sha256"]
        or receipt.boundary_sha256 != boundary.descriptor_sha256
        or receipt.required_test_sha256 != contracts._digest(test)
        or not isinstance(receipt.run_id, str)
        or not receipt.run_id.startswith("local_tool_check:")
        or not isinstance(receipt.collector_id, str)
        or not receipt.collector_id
        or not _references(receipt.evidence_refs)
        or not isinstance(receipt.raw_receipt, bytes)
        or not 0 < len(receipt.raw_receipt) <= 65536
    ):
        raise ValueError("independent_registered_tool_check_proof_UNMET")
    process = receipt.process_observation
    root = Path(prepared.workspace).resolve(strict=True)
    argv = (
        *policy.argv(test["executable"], root, check=True),
        *(str(root / path) for path in prepared.plan["paths"]),
    )
    check = receipt.check_boundary
    permit = receipt.check_permit
    if (
        type(check) is not confinement.Boundary
        or check.read_only is not True
        or check.write_paths
        or check.workspace != str(root)
        or check.operation_id != operation["id"]
        or check.argv != argv
        or check.runtime_read_roots != boundary.runtime_read_roots
        or check.profile
        != confinement._render_profile(
            root, Path(check.scratch), tuple(map(Path, check.runtime_read_roots)), ()
        )
        or check.descriptor_sha256
        != hashlib.sha256(
            json.dumps([confinement.SCHEMA, astuple(check)[:-1]], separators=(",", ":")).encode()
        ).hexdigest()
        or type(permit) is not BoundaryPermit
        or permit.context_sha256 != request["context_sha256"]
        or permit.boundary_sha256 != check.descriptor_sha256
        or not _references(permit.evidence_refs)
        or any(confinement._hash(Path(path)) != sha for path, sha in check.source_hashes)
    ):
        raise ValueError("independently_qualified_readonly_check_boundary_UNMET")
    wrapper = (confinement.SANDBOX_EXEC, "-p", check.profile, *check.argv)
    argv_hash = hashlib.sha256(b"".join(arg.encode() + b"\0" for arg in wrapper)).hexdigest()
    if (
        not isinstance(process, dict)
        or type(process.get("pid")) is not int
        or process["pid"] < 1
        or not isinstance(process.get("os_started"), str)
        or not process["os_started"]
        or process.get("run_id") != receipt.run_id
        or process.get("operation_id") != operation["id"]
        or process.get("context_sha256") != request["context_sha256"]
        or process.get("transport_argv_sha256") != argv_hash
        or process.get("cleanup_terminal") is not True
        or type(process.get("transport_returncode")) is not int
        or process["transport_returncode"] != 0
    ):
        raise ValueError("independent_tool_check_process_join_changed")
    if receipt.run_id != "local_tool_check:" + contracts._digest(
        [operation["id"], check.descriptor_sha256, process["pid"], process["os_started"]]
    ):
        raise ValueError("local_tool_check_observation_identity_changed")
    expected = {
        "schema": "closer-independent-tool-check/v1",
        "context_sha256": request["context_sha256"],
        "boundary_sha256": boundary.descriptor_sha256,
        "check_boundary_sha256": check.descriptor_sha256,
        "operation_id": operation["id"],
        "required_test_sha256": contracts._digest(test),
        "target": test["target"],
        "head": test["head"],
        "finding_id": test["finding_id"],
        "ci_finding_sha256": contracts._digest(test["ci_finding"]),
        "source_policy_sha256": test["policy_sha256"],
        "collector_source_sha256": test["ci_finding"]["collector_source_sha256"],
        "collector_id": receipt.collector_id,
        "run_id": receipt.run_id,
        "process_observation": process,
        "tool": policy.tool,
        "tool_version": policy.version,
        "executable_sha256": test["executable_sha256"],
        "argv": list(argv),
        "source_refs": [list(row) for row in policy.source_refs],
        "preimages": [[path, prepared.tracked_paths[path]] for path, _digest in postimages],
        "postimages": [list(row) for row in postimages],
        "outcome": "passed",
        "diagnostics": [],
    }
    # Strict existing syntax parser rejects duplicate keys/nonfinite numbers.
    import closer_model_contracts

    if closer_model_contracts.strict_response_json(receipt.raw_receipt.decode("utf-8")) != expected:
        raise ValueError("independent_tool_check_structured_receipt_changed")
    return hashlib.sha256(receipt.raw_receipt).hexdigest()


def observe_required_test(prepared, *, operation):
    """Validate independent postimage-bound test evidence; never settle unknown effects.

    This never executes pytest or qualifies its collector. The original owner
    authenticates the separately executed test via collect_test, while current
    bytes/nodeid/JUnit/operation joins are checked here. Pre-fix CI or exit zero
    cannot substitute for that result. Even valid evidence grants no authority.
    """
    try:
        _original(prepared, operation)
        request, boundary, owner = prepared.request, prepared.boundary, prepared.owner
        if queue._callback_threads.invocation.get("_closer_mechanical_submitted") != {
            "operation_id": operation["id"],
            "plan_id": prepared.plan["plan_id"],
            "boundary_sha256": boundary.descriptor_sha256,
            "outcome": "unknown",
        }:
            raise ValueError("original_tool_submission_not_observed")
        admission = owner.resolve(copy.deepcopy(request))
        if type(admission) is not OwnerAdmission:
            raise ValueError("source_registered_tool_and_test_UNMET")
        admitted = contracts.validate_mechanical_plan_identity(
            prepared.plan,
            **prepared.inputs,
            registered_tests=admission.registered_tests,
        )
        if admitted != {
            k: v for k, v in prepared.admitted.items() if k != "physical_confinement_verified"
        }:
            raise ValueError("original_plan_or_registered_test_identity_changed")
        if admission.tool != prepared.tool:
            raise ValueError("source_tool_registration_changed")
        permit = owner.qualify(copy.deepcopy(request), boundary)
        if (
            type(permit) is not BoundaryPermit
            or permit.context_sha256 != request["context_sha256"]
            or permit.boundary_sha256 != boundary.descriptor_sha256
            or not _references(permit.evidence_refs)
        ):
            raise ValueError("independently_qualified_installed_tool_confinement_UNMET")
        if any(confinement._hash(Path(path)) != digest for path, digest in boundary.source_hashes):
            raise ValueError("original_tool_or_boundary_source_changed")
        root = Path(prepared.workspace).resolve(strict=True)
        test = admitted["required_test_identity"]
        tool_policy = None
        if test.get("kind") == "tool_check":
            import closer_mechanical_catalog as catalog

            tool_policy = catalog.validate_tool_check_identity(test, prepared.plan)
            compiled, _ = catalog.compile_catalog(root)
            if tool_policy not in compiled:
                raise ValueError("source_tool_class_policy_changed")
            paths = sorted(
                {*admitted["paths"], *(path for path, _digest in tool_policy.source_refs)}
            )
        else:
            paths = sorted({*admitted["paths"], test["path"]})
        postimages = tuple(
            (path, confinement._hash(confinement._canonical(root / path, directory=False)))
            for path in paths
        )
        if tool_policy is None and dict(postimages)[test["path"]] != test["file_sha256"]:
            raise ValueError("registered_test_bytes_changed")
        receipt = owner.collect_test(copy.deepcopy(request), boundary, postimages)
        if tool_policy is not None:
            if not _live(prepared.inputs["snapshot"], operation):
                raise ValueError("original_tool_check_callback_expired")
            tool_check_sha256 = _tool_check_proof(
                receipt, prepared, test, tool_policy, operation, postimages
            )
        else:
            if (
                not _live(prepared.inputs["snapshot"], operation)
                or type(receipt) is not RegisteredTestObservation
                or receipt.context_sha256 != request["context_sha256"]
                or receipt.boundary_sha256 != boundary.descriptor_sha256
                or receipt.required_test_sha256 != contracts._digest(test)
                or receipt.postimages != postimages
                or receipt.nodeid != test["path"] + "::" + test["selector"]
                or not isinstance(receipt.raw_junit, bytes)
                or not 0 < len(receipt.raw_junit) <= 65536
                or not isinstance(receipt.collector_id, str)
                or not receipt.collector_id
                or not _references(receipt.evidence_refs)
            ):
                raise ValueError("independent_registered_postimage_test_proof_UNMET")
            parsed = evidence._junit_receipt(receipt.raw_junit)
            if not parsed or not (
                parsed["tests"] > parsed["skipped"] and parsed["failures"] == parsed["errors"] == 0
            ):
                raise ValueError("required_registered_test_did_not_pass")
            cases = list(ET.fromstring(receipt.raw_junit).iter("testcase"))
            if len(cases) != 1 or cases[0].get("name") != test["selector"].split("::")[-1]:
                raise ValueError("required_registered_test_case_not_in_structured_report")
        fresh = tuple(
            (path, confinement._hash(confinement._canonical(root / path, directory=False)))
            for path in paths
        )
        if fresh != postimages:
            raise ValueError("postimages_changed_during_independent_collection")
        if tool_policy is not None and not _live(prepared.inputs["snapshot"], operation):
            raise ValueError("original_tool_check_callback_expired")
        return _result(
            "test_proof_observed",
            "independent_structured_test_only",
            operation,
            effect_outcome="unknown",
            postimages=postimages,
            **(
                {"test_tool_check_sha256": tool_check_sha256}
                if tool_policy is not None
                else {"test_junit_sha256": hashlib.sha256(receipt.raw_junit).hexdigest()}
            ),
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ET.ParseError,
        NotImplementedError,
    ) as exc:
        return _result("unmet", str(exc), operation, effect_outcome="unknown")
