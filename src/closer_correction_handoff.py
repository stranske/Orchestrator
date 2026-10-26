"""Pure correction wire contracts; validity never authenticates or authorizes work.

Mirrors Workflows' verifier_correction_consumer.js field names. Original operation
bounds and incumbent declarations are caller claims. A trusted collector must
independently authenticate them, ownership and actual auto-pilot execution before
any effect. This module performs no IO and grants no retry, ACT or closure rights.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

import verification_events

REQUEST_SCHEMA = "closer-correction-request/v1"
RECEIPT_SCHEMA = "closer-correction-receipt/v1"
SELECTOR_SCHEMA = "closer-correction-selector/v1"
ACCEPTED = "accepted_with_exact_issue_and_actual_autopilot_run_attempt"
MAX_BYTES = 131072
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_LIST = 64
MAX_TEXT = 4096
MAX_SAFE_INTEGER = 2**53 - 1
_ID = re.compile(r"[1-9][0-9]{0,15}")
_SHA = re.compile(r"[a-f0-9]{64}")
_COMMIT = re.compile(r"[a-f0-9]{40}")


def _require(ok: Any, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def _exact(value: Any, fields: str) -> bool:
    return type(value) is dict and set(value) == set(fields.split())


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value.encode("utf-16-be")) // 2 <= MAX_TEXT


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _id(value: Any) -> bool:
    return _matches(_ID, value)


def _bounded(value: Any) -> None:
    pending, count = [(value, 0)], 0
    while pending:
        row, depth = pending.pop()
        count += 1
        _require(depth <= MAX_DEPTH and count <= MAX_NODES, "JSON structure exceeds bound")
        if type(row) is dict:
            _require(len(row) <= MAX_LIST, "JSON object exceeds bound")
            for key, child in row.items():
                _require(type(key) is str, "JSON object key must be text")
                pending.extend(((key, depth + 1), (child, depth + 1)))
        elif type(row) is list:
            _require(len(row) <= MAX_LIST, "JSON list exceeds bound")
            pending.extend((child, depth + 1) for child in row)
        elif type(row) is str:
            try:
                _require(len(row.encode("utf-16-be")) // 2 <= MAX_TEXT, "JSON text exceeds bound")
            except UnicodeError as exc:
                raise ValueError("JSON contains unpaired surrogate") from exc
        elif type(row) is int:
            _require(abs(row) <= MAX_SAFE_INTEGER, "JSON integer exceeds JS safe range")
        else:
            _require(row is None or type(row) is bool, "Only finite integer JSON numbers allowed")


def canonical_json(value: Any) -> str:
    """Workflows canonical JSON on this bounded domain (IDs remain strings)."""
    _bounded(value)

    def render(row: Any) -> str:
        if type(row) is dict:
            keys = sorted(row, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(render(key) + ":" + render(row[key]) for key in keys) + "}"
        if type(row) is list:
            return "[" + ",".join(render(child) for child in row) + "]"
        return json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    result = render(value)
    _require(len(result.encode("utf-8")) <= MAX_BYTES, "Canonical JSON exceeds byte bound")
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse(data: bytes | str) -> dict[str, Any]:
    _require(type(data) in (bytes, str), "Pass JSON bytes or text only")
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        _require(len(raw) <= MAX_BYTES, "JSON exceeds byte bound")

        def pairs(rows):
            result = {}
            for key, value in rows:
                _require(key not in result, "Duplicate JSON field")
                result[key] = value
            return result

        def reject_number(_value):
            raise ValueError("Only finite integer JSON numbers allowed")

        result = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
        _bounded(result)
        _require(type(result) is dict, "JSON root must be an object")
        return result
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Malformed or overdeep JSON") from exc


def _issue(row: Any) -> bool:
    return (
        _exact(row, "number node_id body_sha256")
        and _id(row["number"])
        and _text(row["node_id"])
        and _matches(_SHA, row["body_sha256"])
    )


def _evidence(row: Any) -> bool:
    return _exact(row, "ref sha256") and _text(row["ref"]) and _matches(_SHA, row["sha256"])


def _instant(value: Any) -> bool:
    if not _text(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _incumbent(row: Any) -> bool:
    return (
        _exact(row, "issue owner assignment_ref lineage_ref")
        and _issue(row["issue"])
        and all(_text(row[key]) for key in ("owner", "assignment_ref", "lineage_ref"))
    )


def _event(event: Any, target: dict[str, Any]) -> None:
    _require(
        _exact(
            event, "schema repo pr head_sha evaluated_sha request_sha256 material_sha256 event_id"
        )
        and _id(event["pr"])
        and verification_events.valid_event(
            event, target["repository"], int(target["pr"]), target["head_sha"], target["merge_sha"]
        ),
        "Original verifier event differs from exact target/head/merge",
    )


def validate_request(
    request: dict[str, Any], *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Validate declared shape/integrity only; does not prove origin or authority."""
    _bounded(request)
    _require(
        _exact(
            request,
            "schema request_sha256 target source_issue original_verifier finding "
            "required_tests incumbent original_operation scope",
        ),
        "Invalid correction request fields",
    )
    _require(request["schema"] == REQUEST_SCHEMA, "Unsupported correction request")
    claimed = request["request_sha256"]
    _require(
        _matches(_SHA, claimed)
        and digest({key: value for key, value in request.items() if key != "request_sha256"})
        == claimed
        and (expected_sha256 is None or claimed == expected_sha256),
        "Correction request digest or expected identity mismatch",
    )
    t = request["target"]
    _require(
        _exact(t, "repository pr head_sha base_sha merge_sha")
        and _text(t["repository"])
        and re.fullmatch(r"[^/\s]+/[^/\s]+", t["repository"]) is not None
        and t["repository"] == t["repository"].lower()
        and _id(t["pr"])
        and all(_matches(_COMMIT, t[key]) for key in ("head_sha", "base_sha", "merge_sha")),
        "Invalid correction target",
    )
    _require(_issue(request["source_issue"]), "Invalid source issue binding")
    v = request["original_verifier"]
    _require(
        _exact(v, "verification_event run_id run_attempt comment_id comment_sha256 decision_sha256")
        and all(_id(v[key]) for key in ("run_id", "run_attempt", "comment_id"))
        and all(_matches(_SHA, v[key]) for key in ("comment_sha256", "decision_sha256")),
        "Invalid original verifier binding",
    )
    _event(v["verification_event"], t)
    f = request["finding"]
    _require(
        _exact(f, "id evidence") and _text(f["id"]) and _evidence(f["evidence"]),
        "Invalid exact finding",
    )
    tests = request["required_tests"]
    _require(type(tests) is list and 1 <= len(tests) <= 20, "Explicit required tests missing")
    for test in tests:
        _require(
            type(test) is dict and _text(test.get("id")) and test.get("finding_id") == f["id"],
            "Required-test finding differs",
        )
        if test.get("kind") == "existing_selector":
            _require(
                _exact(test, "kind id finding_id source_ref path selector file_sha256")
                and all(_text(test[key]) for key in ("source_ref", "path", "selector"))
                and not test["path"].startswith("/")
                and ".." not in test["path"].split("/")
                and _matches(_SHA, test["file_sha256"]),
                "Invalid existing required-test selector",
            )
        else:
            _require(
                _exact(test, "kind id finding_id specification source_evidence")
                and test["kind"] == "required_test_spec"
                and _text(test["specification"])
                and _evidence(test["source_evidence"])
                and test["source_evidence"] == f["evidence"],
                "Invalid source-bound required-test specification",
            )
    _require(len({test["id"] for test in tests}) == len(tests), "Duplicate required-test identity")
    incumbent = request["incumbent"]
    _require(
        _incumbent(incumbent)
        or (_exact(incumbent, "kind") and incumbent["kind"] == "no_incumbent"),
        "Invalid declared incumbent",
    )
    op = request["original_operation"]
    _require(
        _exact(
            op,
            "hold_id run_id operation_id idempotency_key deadline attempt_budget escalation_owner",
        )
        and all(
            _text(op[key])
            for key in ("hold_id", "run_id", "operation_id", "idempotency_key", "escalation_owner")
        )
        and _instant(op["deadline"])
        and type(op["attempt_budget"]) is int
        and 1 <= op["attempt_budget"] <= 10,
        "Invalid declared original operation",
    )
    _require(request["scope"] == "substantive_implementation", "Unsupported correction scope")
    return request


def parse_request(data: bytes | str, *, expected_sha256: str | None = None) -> dict[str, Any]:
    return validate_request(_parse(data), expected_sha256=expected_sha256)


def validate_selector(selector: dict[str, Any], *, request: dict[str, Any]) -> dict[str, Any]:
    validate_request(request)
    _bounded(selector)
    _require(
        _exact(
            selector,
            "schema run_id run_attempt artifact_id request_sha256 verification_event assessment_identity",
        )
        and selector["schema"] == SELECTOR_SCHEMA
        and all(
            type(selector[key]) is int and 1 <= selector[key] <= MAX_SAFE_INTEGER
            for key in ("run_id", "run_attempt", "artifact_id")
        )
        and selector["request_sha256"] == request["request_sha256"],
        "Invalid correction selector",
    )
    v = request["original_verifier"]
    _event(selector["verification_event"], request["target"])
    _require(
        selector["verification_event"] == v["verification_event"]
        and str(selector["run_id"]) == v["run_id"]
        and str(selector["run_attempt"]) == v["run_attempt"],
        "Stale correction selector",
    )
    assessment = selector["assessment_identity"]
    _require(
        _exact(assessment, "mode provider model model2 canonical")
        and assessment["canonical"] is True
        and assessment["mode"] in ("evaluate", "compare")
        and all(type(assessment[key]) is str for key in ("provider", "model", "model2"))
        and (assessment["mode"] != "compare" or assessment["provider"] == "")
        and (assessment["mode"] != "evaluate" or assessment["model2"] == ""),
        "Invalid canonical assessment identity",
    )
    return selector


def parse_selector(data: bytes | str, *, request: dict[str, Any]) -> dict[str, Any]:
    return validate_selector(_parse(data), request=request)


def _ownership(row: Any, request: dict[str, Any]) -> None:
    _require(type(row) is dict, "Ownership must be an object")
    kind = row.get("kind")
    _require(
        kind in ("existing", "no_incumbent", "unknown")
        and _exact(
            row,
            "schema kind seed_effect_key source_scope_digest observed_at evidence_refs"
            + (" incumbent" if kind == "existing" else ""),
        ),
        "Invalid ownership fields",
    )
    scope = {
        "target": request["target"],
        "source_issue": request["source_issue"],
        "finding_evidence": request["finding"]["evidence"],
        "scope": request["scope"],
    }
    effect = {
        "schema": "closer-correction-effect/v1",
        "repository": request["target"]["repository"],
        "event_id": request["original_verifier"]["verification_event"]["event_id"],
        "finding_evidence": request["finding"]["evidence"],
        "source_issue": request["source_issue"],
        "scope": request["scope"],
    }
    _require(
        row["schema"] == "closer-correction-ownership/v1"
        and row["seed_effect_key"] == digest(effect)
        and row["source_scope_digest"] == digest(scope)
        and _instant(row["observed_at"])
        and type(row["evidence_refs"]) is list
        and 1 <= len(row["evidence_refs"]) <= MAX_LIST
        and all(map(_text, row["evidence_refs"]))
        and (
            kind != "existing"
            or (_incumbent(row["incumbent"]) and row["incumbent"] == request["incumbent"])
        ),
        "Ownership declared scope/identity differs",
    )


def validate_receipt(receipt: dict[str, Any], *, request: dict[str, Any]) -> dict[str, Any]:
    validate_request(request)
    _bounded(receipt)
    _require(
        type(receipt) is dict
        and _exact(
            receipt,
            "schema request_sha256 request consumer effect_state reason issue continuation "
            "retry_authorized act_authorized source_closure_authorized receipt_sha256"
            + (" ownership" if "ownership" in receipt else ""),
        ),
        "Invalid receipt fields",
    )
    _require(
        receipt["schema"] == RECEIPT_SCHEMA
        and _matches(_SHA, receipt["receipt_sha256"])
        and digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
        == receipt["receipt_sha256"]
        and receipt["request_sha256"] == request["request_sha256"]
        and receipt["request"] == request,
        "Receipt digest or exact request differs",
    )
    validate_request(receipt["request"])
    _require(
        all(
            receipt[key] is False
            for key in ("retry_authorized", "act_authorized", "source_closure_authorized")
        ),
        "Receipt cannot grant authority",
    )
    _require(
        _text(receipt["reason"])
        and _exact(receipt["consumer"], "run_id run_attempt")
        and all(map(_id, receipt["consumer"].values())),
        "Invalid consumer execution",
    )
    ownership = receipt.get("ownership")
    if "ownership" in receipt:
        _ownership(ownership, request)
    issue = receipt["issue"]
    if issue is not None:
        _require(
            _issue(issue)
            and (
                (ownership is not None and ownership["kind"] == "no_incumbent")
                or issue == request["incumbent"].get("issue")
            ),
            "Receipt issue differs from incumbent/observed ownership",
        )
    state, continuation = receipt["effect_state"], receipt["continuation"]
    _require(
        state in (ACCEPTED, "confirmed_not_started", "outcome_unknown"), "Invalid effect state"
    )
    if state == ACCEPTED:
        _require(
            issue is not None
            and _exact(continuation, "workflow run_id run_attempt request_sha256 issue_node_id")
            and continuation["workflow"] == "agents-auto-pilot.yml"
            and _id(continuation["run_id"])
            and _id(continuation["run_attempt"])
            and continuation["request_sha256"] == request["request_sha256"]
            and continuation["issue_node_id"] == issue["node_id"],
            "Accepted receipt requires exact actual auto-pilot continuation",
        )
    else:
        _require(continuation is None, "Nonaccepted receipt cannot claim continuation")
    return receipt


def parse_receipt(data: bytes | str, *, request: dict[str, Any]) -> dict[str, Any]:
    return validate_receipt(_parse(data), request=request)
