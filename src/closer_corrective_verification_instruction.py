"""Prospective bounded native instruction DATA; no authorization or effects.

The workflow collector separately authenticates sender, writer, native parent,
incumbent and published PR. Parsing a footer or matching hashes grants nothing.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import closer_correction_handoff as wire

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA = "closer-corrective-verification-instruction/v1"
PREFIX = "<!-- " + SCHEMA + " "
SUFFIX = " -->"


class InstructionHeld(ValueError):
    pass


def _need(ok, reason):
    if not ok:
        raise InstructionHeld(reason)


def payload(original_request, finding_text, raw_findings):
    try:
        request = wire.validate_request(original_request)
    except (ValueError, TypeError, KeyError) as error:
        raise InstructionHeld("original correction request invalid") from error
    _need(
        request["incumbent"].get("kind") != "no_incumbent",
        "existing incumbent corrective verification required",
    )
    _need(type(finding_text) is str, "original selected finding text missing")
    _need(
        hashlib.sha256(finding_text.encode()).hexdigest()
        == request["finding"]["evidence"]["sha256"],
        "original selected finding digest differs",
    )
    _need(
        type(raw_findings) is list
        and bool(raw_findings)
        and all(type(row) is dict for row in raw_findings),
        "complete original raw findings required",
    )
    for row in raw_findings:
        _need(
            set(row) == {"id", "evidence", "text"}
            and type(row["id"]) is str
            and bool(row["id"])
            and type(row["evidence"]) is dict
            and set(row["evidence"]) == {"ref", "sha256"}
            and type(row["evidence"]["ref"]) is str
            and bool(row["evidence"]["ref"])
            and type(row["evidence"]["sha256"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", row["evidence"]["sha256"])
            and type(row["text"]) is str
            and hashlib.sha256(row["text"].encode()).hexdigest() == row["evidence"]["sha256"],
            "original raw finding fields/digest differ",
        )
    _need(
        len({row["id"] for row in raw_findings}) == len(raw_findings),
        "original raw finding identity duplicated",
    )
    selected = [row for row in raw_findings if row.get("id") == request["finding"]["id"]]
    _need(
        len(selected) == 1
        and selected[0].get("evidence") == request["finding"]["evidence"]
        and selected[0].get("text") == finding_text,
        "original selected raw finding differs",
    )
    value = {
        "schema": SCHEMA,
        "original_request": request,
        "finding_text": finding_text,
        "raw_findings": copy.deepcopy(raw_findings),
        "required_tests_sha256": wire.digest(request["required_tests"]),
    }
    wire.canonical_json(value)  # Existing bounded pure wire validator, no reference reads.
    return value


def append(human_text, original_request, finding_text, raw_findings, *, max_bytes):
    _need(
        type(human_text) is str and type(max_bytes) is int and max_bytes > 0,
        "native instruction bound missing",
    )
    raw = wire.canonical_json(payload(original_request, finding_text, raw_findings)).encode()
    body = human_text + "\n\n" + PREFIX + base64.b64encode(raw).decode("ascii") + SUFFIX
    _need(len(body.encode()) <= max_bytes, "complete native instruction exceeds existing bound")
    return body


def parse(body, *, max_bytes):
    """Strict final suffix only. Human prefix stays unchanged untrusted evidence."""
    _need(
        type(body) is str and type(max_bytes) is int and max_bytes > 0,
        "native instruction bound missing",
    )
    _need(len(body.encode()) <= max_bytes, "complete native instruction exceeds existing bound")
    at = body.rfind(PREFIX)
    _need(
        at >= 2
        and body[at - 2 : at] == "\n\n"
        and body.endswith(SUFFIX)
        and PREFIX not in body[:at],
        "structured native instruction suffix missing",
    )
    encoded = body[at + len(PREFIX) : -len(SUFFIX)]
    try:
        raw = base64.b64decode(encoded, validate=True)
        _need(
            base64.b64encode(raw).decode("ascii") == encoded,
            "native instruction base64 is not canonical",
        )

        def pairs(rows):
            value = {}
            for key, item in rows:
                _need(key not in value, "duplicate native instruction field")
                value[key] = item
            return value

        value = json.loads(raw, object_pairs_hook=pairs)
        _need(
            wire.canonical_json(value).encode() == raw, "native instruction JSON is not canonical"
        )
    except (ValueError, TypeError, UnicodeError) as error:
        raise InstructionHeld("native instruction encoding invalid") from error
    _need(
        type(value) is dict
        and set(value)
        == {"schema", "original_request", "finding_text", "raw_findings", "required_tests_sha256"}
        and value["schema"] == SCHEMA,
        "native instruction fields differ",
    )
    expected = payload(value["original_request"], value["finding_text"], value["raw_findings"])
    _need(value == expected, "native instruction original request/tests differ")
    return {"human_text": body[: at - 2], "payload": expected}


def validate_selector(value):
    """Closed wire shape only. The native workflow independently authorizes it."""

    def exact(row, keys):
        _need(
            type(row) is dict and set(row) == set(keys.split()), "corrective selector fields differ"
        )

    def text(item):
        return (
            type(item) is str
            and bool(item)
            and len(item) <= 4096
            and not any(ord(c) < 32 for c in item)
        )

    def positive(item):
        return type(item) is int and 0 < item <= 9007199254740991

    def sha(item, size):
        return type(item) is str and re.fullmatch(r"[a-f0-9]{" + str(size) + "}", item) is not None

    def number(item):
        return type(item) is str and re.fullmatch(r"[1-9][0-9]{0,15}", item) is not None

    exact(
        value,
        "kind owner_instruction request_sha256 required_tests_sha256 corrective_pr incumbent_assignee",
    )
    _need(value["kind"] == "corrective_instruction", "corrective selector kind differs")
    owner, pr, assignee = (
        value["owner_instruction"],
        value["corrective_pr"],
        value["incumbent_assignee"],
    )
    exact(
        owner,
        "repository issue_number issue_node_id comment_id comment_node_id updated_at body_sha256 writer_id",
    )
    exact(pr, "repository_id number node_id head_sha")
    exact(assignee, "id login")
    _need(
        type(owner["repository"]) is str
        and re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", owner["repository"])
        and number(owner["issue_number"])
        and text(owner["issue_node_id"])
        and positive(owner["comment_id"])
        and text(owner["comment_node_id"])
        and positive(owner["writer_id"])
        and sha(owner["body_sha256"], 64)
        and all(sha(value[k], 64) for k in ("request_sha256", "required_tests_sha256"))
        and positive(pr["repository_id"])
        and number(pr["number"])
        and text(pr["node_id"])
        and sha(pr["head_sha"], 40)
        and positive(assignee["id"])
        and text(assignee["login"]),
        "corrective selector native identity/digests invalid",
    )
    try:
        _need(
            type(owner["updated_at"]) is str and owner["updated_at"].endswith("Z"),
            "corrective instruction timestamp invalid",
        )
        datetime.fromisoformat(owner["updated_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise InstructionHeld("corrective instruction timestamp invalid") from error
    return copy.deepcopy(value)
