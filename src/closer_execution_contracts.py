"""Bounded advisory Closer requests and mechanical plan admission.

This module reads retained evidence and validates identities. It does not launch
tools, choose a model, write a workspace, register an owner, or grant acceptance.
The existing disposition validator and guarded action queue remain authoritative.
No mechanical executor or required-test identity is enabled by this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import closer_disposition as disposition
import closer_evidence as evidence
from closer_role_qualification import MECHANICAL_OPERATIONS

REQUEST_SCHEMA = "closer-criterion-request/v1"
RESPONSE_SCHEMA = "closer-criterion-response/v1"
PLAN_SCHEMA = "closer-mechanical-plan/v1"
TEST_SCHEMA = "closer-required-test/v1"
REGISTERED_REQUIRED_TESTS: dict[str, dict[str, Any]] = {}
MAX_BYTES = 8 * 1024 * 1024
MAX_ADJUDICATION_PROMPT_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_TARGET = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[A-Za-z0-9_. -]+\])?")
_IDENTITY = ("target", "head", "base", "revision", "packet_sha256")


class ContractError(ValueError):
    """A deterministic collection/owner refusal, never permission to improvise."""

    def __init__(self, reason: str, *, owner_required: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.owner_required = owner_required
        self.bound_request: dict[str, Any] | None = None


def _text(value: Any, limit: int = 2000) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractError("contract_contains_invalid_json") from exc
    if len(raw) > MAX_BYTES:
        raise ContractError("contract_exceeds_bound")
    return hashlib.sha256(raw).hexdigest()


def _owners(snapshot: Mapping[str, Any]) -> list[str]:
    sources = snapshot.get("sources")
    if not isinstance(sources, list):
        return []
    return sorted(
        {
            row["target"]
            for row in sources
            if isinstance(row, Mapping)
            and isinstance(row.get("target"), str)
            and _TARGET.fullmatch(row["target"])
        }
    )


def deterministic_refusal(error: ContractError, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Retain the existing source/coordinator route; never invent a security actor."""
    result: dict[str, Any] = {
        "status": "pending",
        "pending_kind": "external_event",
        "classification": "execution_contract_unavailable",
        "target": snapshot.get("target"),
        "revision": snapshot.get("revision"),
        "reason": error.reason,
        "next_trigger": (
            "existing source/coordinator owner supplies qualified review or corrective coding; no generic worker authority"
            if error.owner_required
            else "collect the exact missing criterion, retained evidence or registered required-test identity"
        ),
        "pending_evidence": {
            "event_id": "closer-execution-contract:"
            + _digest([snapshot.get("target"), snapshot.get("revision"), error.reason]),
            "target": snapshot.get("target"),
            "revision": snapshot.get("revision"),
            "contract_reason": error.reason,
            "source_owners": _owners(snapshot),
        },
        "substantive_needs": [],
        "next_guarded_action": "wait" if error.owner_required else "collect_evidence",
        "requires_coordinator": error.owner_required,
        "source_owners": _owners(snapshot),
        "authority": False,
    }
    if error.bound_request is not None:
        result["pending_evidence"]["adjudication_request"] = {
            key: copy.deepcopy(error.bound_request[key])
            for key in ("schema", "request_id", *_IDENTITY, "basis")
        }
    return result


def owner_review_required(values):
    """Detect explicit risk/authority metadata without inventing a qualified owner."""
    pending = [values]
    for _ in range(100000):
        if not pending:
            return False
        value = pending.pop()
        if isinstance(value, Mapping):
            if value.get("authority_conflict") is True or any(
                str(value.get(key, "")).casefold()
                in {
                    "security",
                    "critical",
                    "high",
                    "vulnerability",
                    "conflicting_authority",
                    "authority_conflict",
                    "ownership_conflict",
                }
                for key in ("severity", "category", "type")
            ):
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return True


def load_retained_packet(packet_ref):
    """Bound reads before decoding an untrusted direct worker packet reference."""
    descriptor = None
    try:
        if not isinstance(packet_ref, Mapping) or not isinstance(packet_ref.get("path"), str):
            raise ValueError("missing path")
        path = Path(packet_ref["path"])
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_BYTES:
            raise ValueError("not bounded regular evidence")
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != packet_ref.get("sha256"):
            raise ValueError("packet changed")
        packet = json.loads(raw)
        if not isinstance(packet, dict):
            raise ValueError("not packet object")
        return packet, path.parent.parent
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ContractError("retained_packet_reference_changed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _context(snapshot, packet, packet_ref, retained_dir):
    if not all(isinstance(value, Mapping) for value in (snapshot, packet, packet_ref)):
        raise ContractError("current_packet_identity_missing")
    if not evidence.validate_packet(packet):
        raise ContractError("retained_packet_invalid")
    try:
        root = Path(retained_dir).resolve(strict=True)
        path = Path(packet_ref.get("path", ""))
        physical = path.resolve(strict=True)
        physical.relative_to(root)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("unsafe packet reference")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if (
            len(raw) > MAX_BYTES
            or hashlib.sha256(raw).hexdigest() != packet_ref.get("sha256")
            or json.loads(raw) != packet
        ):
            raise ValueError("packet changed")
    except (OSError, ValueError, TypeError) as exc:
        raise ContractError("retained_packet_reference_changed") from exc
    target, head, base, revision = (
        snapshot.get(key) for key in ("target", "head", "base", "revision")
    )
    if not (
        isinstance(target, str)
        and _TARGET.fullmatch(target)
        and isinstance(head, str)
        and _SHA1.fullmatch(head)
        and isinstance(base, str)
        and _SHA1.fullmatch(base)
        and _text(revision, 512)
        and packet.get("target") == target
        and packet.get("head") == head
        and packet.get("base") == base
        and packet.get("producer_sha256")
        == hashlib.sha256(Path(evidence.__file__).read_bytes()).hexdigest()
        and packet.get("bindings", {}).get("source") == evidence._hash(snapshot.get("sources"))
        and packet.get("bindings", {}).get("reviews")
        == evidence._hash(snapshot.get("review_revision"))
    ):
        raise ContractError("current_packet_identity_changed")
    return {
        "target": target,
        "head": head,
        "base": base,
        "revision": revision,
        "packet_sha256": packet_ref["sha256"],
    }, root


def _basis(value, snapshot, packet):
    if not isinstance(value, Mapping):
        raise ContractError("named_disputed_criterion_missing")
    if set(value) in ({"source", "criterion"}, {"source", "criterion_sha256"}):
        expected = disposition._expected(snapshot)
        if expected is None:
            raise ContractError("original_source_criteria_unavailable")
        selected = [
            (source, criterion)
            for source, criterion in expected
            if source == value.get("source")
            and (
                criterion == value.get("criterion")
                if "criterion" in value
                else hashlib.sha256(criterion.encode()).hexdigest() == value.get("criterion_sha256")
            )
        ]
        if len(selected) != 1:
            raise ContractError("named_source_criterion_missing_or_ambiguous")
        source, criterion = selected[0]
        return {
            "source": source,
            "criterion_sha256": hashlib.sha256(criterion.encode()).hexdigest(),
        }, criterion
    if set(value) == {"finding_id"}:
        matched = [
            row
            for row in disposition.pending_nonpass_findings(snapshot, packet)
            if row["finding_id"] == value.get("finding_id")
        ]
        if len(matched) != 1:
            raise ContractError("current_finding_missing_or_ambiguous")
        if owner_review_required(matched[0]["raw"]):
            raise ContractError(
                "high_risk_or_authority_finding_requires_existing_owner", owner_required=True
            )
        return {"finding_id": matched[0]["finding_id"]}, None
    raise ContractError("one_exact_criterion_or_finding_basis_required")


def _refs(values, index, root):
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ContractError("bounded_retained_evidence_required")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            raise ContractError("retained_evidence_reference_invalid")
        if (
            not isinstance(value.get("sha256"), str)
            or not _SHA256.fullmatch(value["sha256"])
            or type(value.get("bytes")) is not int
            or value["bytes"] < 1
        ):
            raise ContractError("retained_evidence_reference_invalid")
        key = (value["path"], value["sha256"])
        if key in seen or key not in index or disposition._safe_raw(value, root) is None:
            raise ContractError("retained_evidence_reference_changed_or_unbound")
        seen.add(key)
        result.append({key: value[key] for key in ("path", "sha256", "bytes")})
    return result


def build_adjudication_request(
    snapshot, packet, packet_ref, *, basis, question, evidence_refs, retained_dir
):
    identity, root = _context(snapshot, packet, packet_ref, retained_dir)
    owner_error = None
    try:
        selected, criterion = _basis(basis, snapshot, packet)
    except ContractError as error:
        if (
            not error.owner_required
            or not isinstance(basis, Mapping)
            or set(basis) != {"finding_id"}
        ):
            raise
        # _basis already proved the exact current finding before refusing its
        # owner class. Complete its identity for a hold, never worker admission.
        selected, criterion = {"finding_id": basis["finding_id"]}, None
        owner_error = error
    if not _text(question) or "\n" in question or "\r" in question or question.count("?") > 1:
        raise ContractError("one_bounded_disputed_question_required")
    refs = _refs(evidence_refs, disposition._receipt_index(packet, root), root)
    result = {
        "schema": REQUEST_SCHEMA,
        **identity,
        "basis": selected,
        "question": question,
        "evidence": refs,
    }
    if criterion is not None:
        result["criterion"] = criterion
    result["request_id"] = _digest(result)
    if owner_error is not None:
        owner_error.bound_request = result
        raise owner_error
    return result


def validate_adjudication_response(
    request, response, *, snapshot, packet, packet_ref, retained_dir
):
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise ContractError("scoped_adjudication_response_invalid")
    expected_request = build_adjudication_request(
        snapshot,
        packet,
        packet_ref,
        basis=request.get("basis"),
        question=request.get("question"),
        evidence_refs=request.get("evidence"),
        retained_dir=retained_dir,
    )
    if dict(request) != expected_request:
        raise ContractError("adjudication_request_changed")
    expected_keys = {
        "schema",
        "request_id",
        *_IDENTITY,
        "basis",
        "outcome",
        "reason",
        "evidence",
        "proofs",
    }
    if "finding_id" in request["basis"]:
        expected_keys.add("next_guarded_action")
    if (
        set(response) != expected_keys
        or response.get("schema") != RESPONSE_SCHEMA
        or any(response.get(key) != request[key] for key in ("request_id", *_IDENTITY, "basis"))
    ):
        raise ContractError("adjudication_response_scope_changed")
    if not _text(response.get("reason")):
        raise ContractError("adjudication_reason_missing")
    outcomes = {"satisfied", "unsatisfied", "insufficient_evidence"}
    if "finding_id" in request["basis"]:
        outcomes = {
            "resolved_duplicate",
            "resolved_nonissue",
            "unresolved_missing_evidence",
            "unresolved_mechanical",
        }
        if not isinstance(response.get("next_guarded_action"), str) or response[
            "next_guarded_action"
        ] not in {"wait", "collect_evidence", "repair"}:
            raise ContractError("finding_route_outside_existing_guard")
        if response.get("next_guarded_action") == "repair":
            raise ContractError("mechanical_plan_required_before_repair_route", owner_required=True)
    if not isinstance(response.get("outcome"), str) or response["outcome"] not in outcomes:
        raise ContractError("scoped_adjudication_outcome_invalid")
    _, root = _context(snapshot, packet, packet_ref, retained_dir)
    refs = response.get("evidence")
    needs_proof = response["outcome"] not in {
        "insufficient_evidence",
        "unresolved_missing_evidence",
        "unresolved_mechanical",
    }
    if needs_proof:
        normalized = _refs(refs, disposition._receipt_index(packet, root), root)
        if any(
            ref not in request["evidence"] for ref in normalized
        ) or not disposition._proofs_valid(
            refs, response.get("proofs"), disposition._receipt_index(packet, root), root
        ):
            raise ContractError("scoped_adjudication_proof_unbound")
    elif refs != [] or response.get("proofs") != []:
        raise ContractError("missing_evidence_answer_must_not_invent_proof")
    return copy.deepcopy(dict(response))


def validate_selected_inventory(original, request):
    """Refuse a missing/ambiguous selected row before spending strong capacity."""
    if not isinstance(original, Mapping) or not isinstance(request, Mapping):
        raise ContractError("original_selected_inventory_invalid")
    basis = request.get("basis") or {}
    if "finding_id" in basis:
        rows = original.get("resolutions")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ContractError("original_resolution_inventory_invalid")
        selected = [row for row in rows if row.get("finding_id") == basis["finding_id"]]
    else:
        decision = original.get("decision")
        rows = decision.get("criteria") if isinstance(decision, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ContractError("original_criterion_inventory_invalid")
        selected = [
            row
            for row in rows
            if row.get("source") == basis.get("source")
            and row.get("criterion") == request.get("criterion")
        ]
    if len(selected) != 1:
        raise ContractError("selected_row_missing_or_ambiguous")
    row = selected[0]
    keys = (
        ("finding_id", "status", "next_guarded_action", "reason", "evidence", "proofs")
        if "finding_id" in basis
        else ("source", "criterion", "result", "evidence", "proofs")
    )
    projected = {
        key: copy.deepcopy(row[key])
        for key in keys
        if key in row and key not in {"evidence", "proofs"}
    }
    refs, proofs = row.get("evidence", []), row.get("proofs", [])
    if (
        not isinstance(refs, list)
        or not isinstance(proofs, list)
        or len(refs) > 8
        or len(proofs) > 8
        or any(not isinstance(item, Mapping) for item in [*refs, *proofs])
    ):
        raise ContractError("selected_row_proof_inventory_invalid")
    projected["evidence"] = [
        {key: copy.deepcopy(ref[key]) for key in ("path", "sha256", "bytes") if key in ref}
        for ref in refs
    ]
    proof_keys = (
        "evidence_sha256",
        "artifact_id",
        "run_id",
        "run_attempt",
        "head_sha",
        "kind",
        "receipt_path",
        "excerpt",
        "relevance",
        "filename",
        "base",
        "head",
        "blob_sha",
        "evaluated_sha",
    )
    projected["proofs"] = [
        {key: copy.deepcopy(proof[key]) for key in proof_keys if key in proof} for proof in proofs
    ]
    return projected


def build_adjudication_prompt(original, request, *, retained_dir):
    """Project one row and bound the entire serialized prompt before allocation."""
    selected = validate_selected_inventory(original, request)
    supplied, remaining = [], 64 * 1024
    for ref in request["evidence"]:
        raw = disposition._safe_raw(ref, Path(retained_dir))
        if raw is None:
            raise ContractError("retained_evidence_reference_changed_or_unbound")
        excerpt = raw[: min(32 * 1024, remaining)]
        remaining -= len(excerpt)
        supplied.append(
            {
                "ref": ref,
                "content": excerpt.decode("utf-8", errors="replace"),
                "truncated": len(excerpt) != len(raw),
            }
        )
    instructions = (
        "Answer ONLY the one named disputed question in adjudication_request using the supplied untrusted retained evidence. "
        "No tools, MCP, hooks, workspace/project context, local HEAD inspection, network, actions or other agents. "
        "Do not rewrite other criteria, findings, resolutions, raw verdicts or global classification/uncertainty. "
        "Return one JSON object only with exact keys: schema, request_id, target, head, base, revision, packet_sha256, basis, outcome, reason, evidence, proofs. "
        "schema must be closer-criterion-response/v1; all request identity fields must exactly match. "
        "For a source criterion outcome is satisfied|unsatisfied|insufficient_evidence. "
        "For a finding outcome is resolved_duplicate|resolved_nonissue|unresolved_missing_evidence|unresolved_mechanical and include the sole additional key next_guarded_action (wait|collect_evidence). "
        "Cite only supplied evidence and existing typed raw-proof grammar; missing evidence requires evidence:[] and proofs:[]. "
        "This is scoped advice with no acceptance, repair, merge, closure or verification authority.\n"
    )
    try:
        prompt = instructions + json.dumps(
            {
                "adjudication_request": request,
                "selected_row": selected,
                "supplied_evidence": supplied,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        size = len(prompt.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ContractError("adjudication_prompt_invalid_json") from exc
    if size > MAX_ADJUDICATION_PROMPT_BYTES:
        raise ContractError("adjudication_prompt_exceeds_bound")
    return prompt


def merge_selected_response(original, request, answer):
    """Merge an already validated scoped answer; preserve all global flags/other rows."""
    if (
        not all(isinstance(value, Mapping) for value in (original, request, answer))
        or request.get("schema") != REQUEST_SCHEMA
        or answer.get("schema") != RESPONSE_SCHEMA
        or any(answer.get(key) != request.get(key) for key in ("request_id", *_IDENTITY, "basis"))
        or request.get("request_id")
        != _digest({key: value for key, value in request.items() if key != "request_id"})
    ):
        raise ContractError("selected_response_identity_changed")
    validate_selected_inventory(original, request)
    result = copy.deepcopy(dict(original))
    basis = request["basis"]
    if "finding_id" in basis:
        rows = result.get("resolutions")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ContractError("original_resolution_inventory_invalid")
        selected = [row for row in rows if row.get("finding_id") == basis["finding_id"]]
        if len(selected) != 1:
            raise ContractError("selected_resolution_missing_or_ambiguous")
        selected[0].update(
            status=answer["outcome"],
            next_guarded_action=answer["next_guarded_action"],
            reason=answer["reason"],
            evidence=copy.deepcopy(answer["evidence"]),
            proofs=copy.deepcopy(answer["proofs"]),
        )
    else:
        rows = (result.get("decision") or {}).get("criteria")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ContractError("original_criterion_inventory_invalid")
        selected = [
            row
            for row in rows
            if row.get("source") == basis["source"]
            and row.get("criterion") == request.get("criterion")
        ]
        if len(selected) != 1:
            raise ContractError("selected_criterion_missing_or_ambiguous")
        selected[0].update(
            result={"satisfied": "PASS", "unsatisfied": "FAIL", "insufficient_evidence": "UNKNOWN"}[
                answer["outcome"]
            ],
            evidence=copy.deepcopy(answer["evidence"]),
            proofs=copy.deepcopy(answer["proofs"]),
        )
    return result


def _tracked_file(workspace, path, tracked_paths):
    if not isinstance(path, str) or not path or "\\" in path:
        raise ContractError("mechanical_path_invalid", owner_required=True)
    relative = PurePosixPath(path)
    if (
        relative.is_absolute()
        or str(relative) != path
        or any(part in {"..", ".git", ".codex", ".aws", ".agents"} for part in relative.parts)
        or path not in tracked_paths
    ):
        raise ContractError("mechanical_path_not_exact_tracked_file", owner_required=True)
    candidate = workspace / path
    opened = []
    try:
        directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.append(directory)
        for part in relative.parts[:-1]:
            directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            opened.append(directory)
        descriptor = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        opened.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise ValueError("not an exclusive bounded regular file")
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named = os.stat(relative.parts[-1], dir_fd=directory, follow_symlinks=False)
        if (
            len(raw) > MAX_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_nlink,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
            )
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            or hashlib.sha256(raw).hexdigest() != tracked_paths[path]
        ):
            raise ValueError("tracked contents changed")
    except (OSError, ValueError) as exc:
        raise ContractError("mechanical_path_not_physically_confined", owner_required=True) from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    return candidate


def validate_mechanical_plan_identity(
    plan,
    *,
    snapshot,
    packet,
    packet_ref,
    retained_dir,
    registered_tests=None,
):
    """Validate retained plan/test identities without pretending filesystem admission.

    The test registry is coordinator-owned; never populate it from model output.
    This phase is suitable for the existing read-only disposition integration.
    """
    identity, root = _context(snapshot, packet, packet_ref, retained_dir)
    allowed = {
        "schema",
        "target",
        "head",
        "source_owner",
        "finding_id",
        "operation",
        "paths",
        "required_test",
        "expected_failure",
        "evidence",
        "proofs",
        "plan_id",
    }
    if not isinstance(plan, Mapping) or not isinstance(plan.get("required_test"), Mapping):
        raise ContractError("registered_required_test_missing")
    if set(plan) != allowed or plan.get("schema") != PLAN_SCHEMA:
        raise ContractError("mechanical_plan_schema_invalid", owner_required=True)
    _basis({"finding_id": plan.get("finding_id")}, snapshot, packet)
    if (
        plan.get("target") != identity["target"]
        or plan.get("head") != identity["head"]
        or plan.get("source_owner") not in _owners(snapshot)
    ):
        raise ContractError("mechanical_plan_source_or_head_changed", owner_required=True)
    if (
        not isinstance(plan.get("operation"), str)
        or plan["operation"] not in MECHANICAL_OPERATIONS
        or not _text(plan.get("expected_failure"), 500)
    ):
        raise ContractError("mechanical_operation_or_failure_missing", owner_required=True)
    paths = plan.get("paths")
    index = disposition._receipt_index(packet, root)
    changed = {
        member.get("filename")
        for entry in index.values()
        for member in (entry if isinstance(entry, list) else [entry])
        if isinstance(member, dict) and member.get("kind") == "codepatch"
    }
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 5
        or any(not isinstance(path, str) for path in paths)
        or len(set(paths)) != len(paths)
        or any(path not in changed for path in paths)
    ):
        raise ContractError("mechanical_paths_not_bound_to_current_diff", owner_required=True)
    tests = REGISTERED_REQUIRED_TESTS if registered_tests is None else registered_tests
    selected = plan.get("required_test")
    if (
        not isinstance(tests, Mapping)
        or not isinstance(selected, Mapping)
        or set(selected) != {"test_id", "identity_sha256"}
        or not _text(selected.get("test_id"), 128)
        or not isinstance(selected.get("identity_sha256"), str)
        or not _SHA256.fullmatch(selected["identity_sha256"])
    ):
        raise ContractError("registered_required_test_missing")
    test = tests.get(selected.get("test_id"))
    if test is None:
        raise ContractError("registered_required_test_missing")
    if (
        not isinstance(test, Mapping)
        or set(test)
        != {
            "schema",
            "test_id",
            "target",
            "head",
            "finding_id",
            "kind",
            "path",
            "selector",
            "file_sha256",
        }
        or test.get("schema") != TEST_SCHEMA
        or test.get("kind") != "pytest"
        or test.get("test_id") != selected.get("test_id")
        or selected.get("identity_sha256") != _digest(test)
        or any(test.get(key) != plan.get(key) for key in ("target", "head", "finding_id"))
    ):
        raise ContractError("required_test_identity_changed_or_unrelated", owner_required=True)
    selector = test.get("selector")
    if (
        not isinstance(selector, str)
        or not selector
        or not all(_SYMBOL.fullmatch(part) for part in selector.split("::"))
    ):
        raise ContractError("required_test_selector_invalid", owner_required=True)
    test_path = test.get("path")
    if (
        not isinstance(test_path, str)
        or not test_path.endswith(".py")
        or not isinstance(test.get("file_sha256"), str)
        or not _SHA256.fullmatch(test["file_sha256"])
    ):
        raise ContractError("required_test_file_not_current_tracked_identity", owner_required=True)
    if not disposition._proofs_valid(plan.get("evidence"), plan.get("proofs"), index, root):
        raise ContractError("mechanical_failure_proof_unbound", owner_required=True)
    content = {key: value for key, value in plan.items() if key != "plan_id"}
    if plan.get("plan_id") != _digest(content):
        raise ContractError("mechanical_plan_digest_changed", owner_required=True)
    return {
        **copy.deepcopy(dict(plan)),
        "required_test_identity": copy.deepcopy(dict(test)),
        "runtime_executor_qualified": False,
        "authority": False,
    }


def validate_mechanical_plan(
    plan,
    *,
    snapshot,
    packet,
    packet_ref,
    retained_dir,
    workspace,
    tracked_paths,
    registered_tests=None,
):
    """Recheck trusted exact-head tracked bytes and physical paths before a writer.

    Tracking and test registrations are coordinator inputs, never model output.
    Success still does not qualify a generic model writer or an executor.
    """
    admitted = validate_mechanical_plan_identity(
        plan,
        snapshot=snapshot,
        packet=packet,
        packet_ref=packet_ref,
        retained_dir=retained_dir,
        registered_tests=registered_tests,
    )
    if not isinstance(tracked_paths, Mapping) or any(
        not isinstance(value, str) or not _SHA256.fullmatch(value)
        for value in tracked_paths.values()
    ):
        raise ContractError("trusted_current_head_tracking_required", owner_required=True)
    try:
        original = Path(workspace).absolute()
        physical = original.resolve(strict=True)
        if original.is_symlink() or not physical.is_dir():
            raise ValueError("workspace aliased")
    except (OSError, ValueError, TypeError) as exc:
        raise ContractError("mechanical_workspace_unavailable", owner_required=True) from exc
    for path in plan["paths"]:
        _tracked_file(physical, path, tracked_paths)
    test = admitted["required_test_identity"]
    if test["file_sha256"] != tracked_paths.get(test["path"]):
        raise ContractError("required_test_file_not_current_tracked_identity", owner_required=True)
    _tracked_file(physical, test["path"], tracked_paths)
    return {**admitted, "physical_confinement_verified": True}


def mechanical_execution_refusal(plan, snapshot):
    """Return the current deterministic no-executor/test-catalog boundary."""
    if not isinstance(plan, Mapping) or not isinstance(plan.get("required_test"), Mapping):
        return ContractError("registered_required_test_missing")
    operation = plan.get("operation")
    if not isinstance(operation, str) or operation not in MECHANICAL_OPERATIONS:
        return ContractError("mechanical_operation_not_allowlisted", owner_required=True)
    selected = plan["required_test"]
    test_id = selected.get("test_id")
    if not isinstance(test_id, str) or test_id not in REGISTERED_REQUIRED_TESTS:
        return ContractError("registered_required_test_missing")
    return ContractError("bounded_mechanical_executor_unqualified", owner_required=True)


def mechanical_execution_pending(plan, snapshot):
    """Keep a known exact finding/test on the refusal; this is no owner dispatch."""
    pending = deterministic_refusal(mechanical_execution_refusal(plan, snapshot), snapshot)
    if not isinstance(plan, Mapping):
        return pending
    current_ids = {
        row["finding_id"]
        for row in disposition.pending_nonpass_findings(snapshot, {"sections": {}})
    }
    finding_id = plan.get("finding_id")
    if not isinstance(finding_id, str) or finding_id not in current_ids:
        return pending
    pending["pending_evidence"]["finding_id"] = finding_id
    selected = plan.get("required_test")
    if not isinstance(selected, Mapping) or not isinstance(selected.get("test_id"), str):
        return pending
    test = REGISTERED_REQUIRED_TESTS.get(selected["test_id"])
    if (
        isinstance(test, Mapping)
        and test.get("schema") == TEST_SCHEMA
        and selected.get("identity_sha256") == _digest(test)
        and test.get("target") == snapshot.get("target")
        and test.get("head") == snapshot.get("head")
        and test.get("finding_id") == finding_id
    ):
        pending["pending_evidence"]["required_test_identity"] = copy.deepcopy(dict(test))
    return pending
