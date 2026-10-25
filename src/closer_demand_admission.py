"""Generation-fenced demand admission inside the EXISTING campaign transaction.

The caller supplies a verified immutable package identity and fresh post-discovery
assignment bytes. This module does not qualify packages, create campaigns, grant
launch/native authority, or own a lock. ``admit_in_transaction`` must run inside
Campaign.update (or its existing equivalent); only admission changes doc in place.
The optional wrapper preserves outer revisions and bytes for replay/refusal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

MAX_ASSIGNMENT_BYTES = 2 * 1024 * 1024
MAX_ASSIGNMENT_AGE_SECONDS = 600
REQUEST_SCHEMA = "closer-demand-request/v1"
RECORD_SCHEMA = "closer-demand-admission/v1"
PINS = ("package_digest", "manifest_sha256", "qualification_digest")
REQUEST_FIELDS = frozenset(
    (
        "schema",
        "campaign_id",
        "owner",
        "campaign_generation",
        "expected_revision",
        "assignment_generation",
        "assignment_sha256",
        *PINS,
        "not_before",
        "deadline",
        "targets",
    )
)


class _Refused(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Refused(reason)


def _number(value: Any, name: str) -> float:
    _require(
        type(value) in (int, float) and math.isfinite(value), f"{name}: finite number required"
    )
    return float(value)


def _integer(value: Any, name: str) -> int:
    _require(type(value) is int and value > 0, f"{name}: positive integer required")
    return value


def _text(value: Any, name: str) -> str:
    _require(
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and value == value.strip()
        and not any(ord(c) < 32 for c in value),
        f"{name}: bounded nonempty text required",
    )
    return value


def _digest(value: Any, name: str) -> str:
    _require(
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
        f"{name}: SHA256 required",
    )
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _pairs(items):
    doc = {}
    for key, value in items:
        _require(key not in doc, "assignment: duplicate JSON key")
        doc[key] = value
    return doc


def _constant(value):
    raise _Refused(f"assignment: non-finite JSON constant {value}")


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(
            parsed.tzinfo is not None and parsed.utcoffset() is not None,
            f"{name}: timezone required",
        )
        return _number(parsed.timestamp(), name)
    return _number(value, name)


def _targets(value: Any, *, rows: bool = False, now: float = 0) -> list[str]:
    _require(isinstance(value, list) and bool(value), "targets: nonempty list required")
    result = []
    for row in value:
        if isinstance(row, dict) and rows:
            target = row.get("target")
            for gate in ("due", "not_before"):
                if gate in row:
                    _require(_timestamp(row[gate], gate) <= now, f"target {gate} is future")
        else:
            target = row
        result.append(_text(target, "target"))
    _require(
        len({target.casefold() for target in result}) == len(result), "targets: duplicate scope"
    )
    return result


def _result(status: str, record=None, reason=None) -> dict[str, Any]:
    return {
        "status": status,
        "changed": status == "admitted",
        "reason": reason,
        "record": copy.deepcopy(record),
    }


def admit_in_transaction(
    doc: dict[str, Any],
    *,
    assignment_bytes: bytes,
    request: Mapping[str, Any],
    qualified_package: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Mutate only doc['demand_admission'] on admitted; replay/refused never mutate.

    qualified_package is a CALLER-VERIFIED identity, not an attestation accepted
    from assignment JSON. Return values never grant launch or observed entry.
    Same-request replay may report an expired admission but cannot renew it.
    """
    try:
        now = _number(now, "now")
        _require(isinstance(doc, dict), "campaign: object required")
        _require(
            isinstance(request, Mapping) and set(request) == REQUEST_FIELDS,
            "request: exact schema fields required",
        )
        request = copy.deepcopy(dict(request))
        _require(request["schema"] == REQUEST_SCHEMA, "request: unsupported schema")
        _require(
            len(json.dumps(request, allow_nan=False).encode()) <= MAX_ASSIGNMENT_BYTES,
            "request too large",
        )
        for name in ("campaign_id", "owner"):
            _text(request[name], name)
        for name in ("campaign_generation", "expected_revision", "assignment_generation"):
            _integer(request[name], name)
        _require(isinstance(qualified_package, Mapping), "verified package identity required")
        for name in (*PINS, "assignment_sha256"):
            _digest(request[name], name)
        for name in PINS:
            _require(
                request[name] == qualified_package.get(name), f"{name}: verified package mismatch"
            )
        not_before = _number(request["not_before"], "not_before")
        deadline = _number(request["deadline"], "deadline")
        _require(not_before < deadline, "request: invalid time window")
        targets = _targets(request["targets"])
        _require(
            type(assignment_bytes) is bytes and 0 < len(assignment_bytes) <= MAX_ASSIGNMENT_BYTES,
            "assignment bytes absent or too large",
        )
        _require(
            hashlib.sha256(assignment_bytes).hexdigest() == request["assignment_sha256"],
            "assignment: raw digest mismatch",
        )
        assignment = json.loads(
            assignment_bytes, object_pairs_hook=_pairs, parse_constant=_constant
        )
        _require(isinstance(assignment, dict), "assignment: object required")
        _require(
            type(assignment.get("generation")) is int
            and assignment["generation"] == request["assignment_generation"],
            "assignment: foreign generation",
        )
        _require(assignment.get("errors") == [], "assignment: complete discovery required")
        # Scope and raw digest remain immutable on replay too. Future-due gating
        # below applies to NEW acceptance, never extends an existing receipt.
        scope = _targets(assignment.get("targets"), rows=True, now=math.inf)
        _require(scope == targets, "assignment: exact ordered target scope mismatch")
        _require(
            doc.get("campaign_id") == request["campaign_id"]
            and doc.get("owner") == request["owner"],
            "campaign: foreign identity/owner",
        )
        _require(
            type(doc.get("generation")) is int
            and doc["generation"] == request["campaign_generation"],
            "campaign: foreign generation",
        )
        _require(
            doc.get("release_pin") == request["package_digest"], "campaign: release pin mismatch"
        )
        request_id = _hash(request)
        previous = doc.get("demand_admission")
        if previous is not None:
            _require(
                isinstance(previous, dict) and previous.get("schema") == RECORD_SCHEMA,
                "previous admission: invalid schema",
            )
            protected = {key: value for key, value in previous.items() if key != "record_digest"}
            _require(
                previous.get("record_digest") == _hash(protected),
                "previous admission: record digest mismatch",
            )
            _integer(previous.get("assignment_generation"), "previous generation")
            _integer(previous.get("admission_count"), "previous admission count")
            if previous.get("request_id") == request_id:
                return _result("replay", previous)
            _require(
                request["assignment_generation"] > previous["assignment_generation"],
                "assignment: stale or reused generation",
            )
        _require(
            type(doc.get("revision")) is int and doc["revision"] == request["expected_revision"],
            "campaign: stale revision",
        )
        _require(not_before <= now < deadline, "request: not due or expired")
        generated = _timestamp(assignment.get("generated_at"), "generated_at")
        _require(
            0 <= now - generated <= MAX_ASSIGNMENT_AGE_SECONDS,
            "assignment: future or stale discovery",
        )
        _targets(assignment["targets"], rows=True, now=now)
        _require(
            doc.get("phase") == "work" and not doc.get("hold"),
            "campaign: phase or hold blocks admission",
        )
        workers = doc.get("workers", [])
        _require(isinstance(workers, list), "campaign: malformed workers")
        _require(
            all(
                isinstance(worker, dict)
                and worker.get("state") == "terminal"
                and worker.get("outcome") in ("ok", "unavailable", "scoped_continuation")
                for worker in workers
            ),
            "campaign: live, denied or unresolved worker",
        )
        record = {
            **request,
            "schema": RECORD_SCHEMA,
            "request_id": request_id,
            "accepted_at": now,
            "admission_count": previous["admission_count"] + 1 if previous else 1,
        }
        record["record_digest"] = _hash(record)
        doc["demand_admission"] = record
        return _result("admitted", record)
    except (ValueError, TypeError, OverflowError, RecursionError, KeyError) as exc:
        return _result("refused", reason=str(exc))


class _NoWrite(Exception):
    def __init__(self, result):
        self.result = result


def admit_assignment(campaign, **kwargs) -> dict[str, Any]:
    """Use the passed EXISTING Campaign.update lock; no second lock or state root.

    Its mutator exceptions prevent Campaign.update's unconditional revision/write
    on replay/refusal. Storage failures propagate: uncertain writes must reconcile.
    """
    outcome = None

    def mutate(doc):
        nonlocal outcome
        outcome = admit_in_transaction(doc, **kwargs)
        if outcome["status"] != "admitted":
            raise _NoWrite(outcome)

    try:
        campaign.update(mutate)
    except _NoWrite as exc:
        return exc.result
    assert outcome is not None
    return outcome
