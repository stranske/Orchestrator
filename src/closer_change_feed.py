"""Bounded local-first ingestion of trusted remote change events.

The queue's ``items[target].evidence`` remains the only item snapshot store.  This
module adds a compact event journal for idempotency, sequence watermarks, and gaps.
It never performs network I/O; callers must supply authenticated, trusted events.
"""
from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Mapping

import closer_queue

SCHEMA = "closer-change-feed/v1"
KINDS = frozenset({"commit_head", "checks", "reviews", "labels_holds", "issues", "verifier"})
MAX_EVENT_IDS = 10_000
MAX_EVENT_BYTES = 128 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")

PAYLOAD_FIELDS = {
    "commit_head": {"head", "base", "head_branch", "head_repository"},
    "checks": {"checks", "pending_check_run_ids", "pending_check_head"},
    "reviews": {"review_revision", "active_threads", "threads"},
    "labels_holds": {"labels", "label_events", "label_events_complete", "draft", "mergeable"},
    "issues": {"sources"},
    "verifier": {"verifier", "verifier_history_complete", "verifier_runs",
                 "verifier_run_identity_unknown", "verifier_comment_excerpts"},
}


def _journal_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".changes.json")


def _empty_journal() -> dict[str, Any]:
    return {"schema": SCHEMA, "event_ids": [], "targets": {}}


def _read_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_journal()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read change journal: {exc}") from exc
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA
            or not isinstance(data.get("event_ids"), list)
            or not isinstance(data.get("targets"), dict)):
        raise ValueError("malformed change journal")
    return data


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    # Queue lock is held by caller. Reuse the queue's fsync + replace semantics.
    closer_queue._atomic_write(path, value)


def _local_revision(previous: Any, changes: Mapping[str, Any], head: Any) -> str:
    current = [
        {"kind": kind, "event_id": row.get("event_id"), "sequence": row.get("sequence"),
         "head": row.get("head"), "payload": row.get("payload")}
        for kind, row in changes.items()
        if isinstance(row, Mapping) and row.get("head") == head
    ]
    current.sort(key=lambda row: row["kind"])
    canonical = json.dumps({"previous_revision": previous, "changes": current},
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_event(target: str, event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping) or event.get("trusted") is not True:
        raise ValueError("change event must come from an authenticated trusted source")
    source = event.get("source")
    event_id = event.get("event_id")
    kind = event.get("kind")
    sequence = event.get("sequence")
    head = event.get("head")
    payload = event.get("payload")
    if not isinstance(source, str) or not source.strip() or len(source) > 200:
        raise ValueError("event source is required")
    if event.get("target") != target or not isinstance(target, str) or not target.strip():
        raise ValueError("event target does not match requested target")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 500:
        raise ValueError("event_id is required")
    if kind not in KINDS:
        raise ValueError("unsupported change category")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("sequence must be a non-negative integer")
    if not isinstance(head, str) or not head.strip() or len(head) > 200:
        raise ValueError("a bound head is required")
    if not isinstance(payload, Mapping):
        raise ValueError("event payload must be an object")
    complete = event.get("complete", True)
    missed = event.get("missed_events", False)
    if not isinstance(complete, bool) or not isinstance(missed, bool):
        raise ValueError("complete and missed_events must be booleans")
    copied = closer_queue._json_copy(dict(payload), limit=MAX_EVENT_BYTES)
    unsupported = set(copied) - PAYLOAD_FIELDS[kind]
    if unsupported:
        raise ValueError(f"unsupported {kind} payload fields: {', '.join(sorted(unsupported))}")
    if "target" in copied and copied["target"] != target:
        raise ValueError("event payload target does not match requested target")
    if "head" in copied and copied["head"] != head:
        raise ValueError("event payload head does not match event binding")
    _validate_payload(kind, copied, head)
    return {"source": source, "event_id": event_id, "kind": kind,
            "sequence": sequence, "head": head, "payload": copied,
            "complete": complete, "missed_events": missed}


def _validate_payload(kind: str, payload: Mapping[str, Any], head: str) -> None:
    """Validate category values so one event cannot impersonate another signal."""
    def string_list(value: Any, name: str) -> None:
        if not isinstance(value, list) or len(value) > 500 or any(
            not isinstance(row, str) or len(row) > 500 for row in value
        ):
            raise ValueError(f"{name} must be a bounded list of strings")

    if kind == "commit_head":
        if "head" in payload and (not isinstance(payload["head"], str) or not SHA.fullmatch(payload["head"])):
            raise ValueError("commit_head head must be a full commit SHA")
        if "base" in payload and payload["base"] is not None and (
            not isinstance(payload["base"], str) or not SHA.fullmatch(payload["base"])
        ):
            raise ValueError("commit_head base must be null or a full commit SHA")
        for key in ("head_branch", "head_repository"):
            if key in payload and payload[key] is not None and not isinstance(payload[key], str):
                raise ValueError(f"{key} must be a string or null")
        if payload.get("head_repository") and not re.fullmatch(r"[^/#]+/[^/#]+", payload["head_repository"]):
            raise ValueError("head_repository must be owner/name")
    elif kind == "checks":
        if "checks" in payload and payload["checks"] not in (None, "SUCCESS", "FAILURE", "ERROR", "PENDING"):
            raise ValueError("checks must be a recognized rollup state or null")
        if "pending_check_run_ids" in payload:
            ids = payload["pending_check_run_ids"]
            if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
                raise ValueError("pending_check_run_ids must be positive integer IDs")
        if "pending_check_head" in payload and payload["pending_check_head"] not in (None, head):
            raise ValueError("pending_check_head must match the event head")
    elif kind == "reviews":
        if "review_revision" in payload and (
            not isinstance(payload["review_revision"], str)
            or not HEX_DIGEST.fullmatch(payload["review_revision"])
        ):
            raise ValueError("review_revision must be a SHA-256 digest")
        if "active_threads" in payload and (type(payload["active_threads"]) is not int or not 0 <= payload["active_threads"] <= 1000):
            raise ValueError("active_threads must be a bounded non-negative integer")
        if "threads" in payload:
            rows = payload["threads"]
            if not isinstance(rows, list) or len(rows) > 100 or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or type(row.get("isResolved")) is not bool
                or type(row.get("isOutdated")) is not bool
                for row in rows
            ):
                raise ValueError("threads must contain bounded, validated review thread rows")
    elif kind == "labels_holds":
        if "labels" in payload:
            string_list(payload["labels"], "labels")
        if "label_events_complete" in payload and type(payload["label_events_complete"]) is not bool:
            raise ValueError("label_events_complete must be boolean")
        if "draft" in payload and type(payload["draft"]) is not bool:
            raise ValueError("draft must be boolean")
        if "mergeable" in payload and payload["mergeable"] not in (None, "MERGEABLE", "CONFLICTING", "UNKNOWN"):
            raise ValueError("mergeable must be a recognized state or null")
        if "label_events" in payload:
            rows = payload["label_events"]
            if not isinstance(rows, list) or len(rows) > 100 or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("label"), str)
                for row in rows
            ):
                raise ValueError("label_events must contain bounded event rows")
    elif kind == "issues":
        rows = payload.get("sources")
        if not isinstance(rows, list) or len(rows) > 8 or any(
            not isinstance(row, dict) or not isinstance(row.get("target"), str)
            or row.get("state") not in ("open", "closed")
            or not isinstance(row.get("body", ""), str)
            or not isinstance(row.get("labels", []), list)
            or any(not isinstance(label, str) for label in row.get("labels", []))
            for row in rows
        ):
            raise ValueError("sources must be a bounded list of validated issue rows")
    elif kind == "verifier":
        if "verifier" in payload:
            marker = payload["verifier"]
            if marker is not None and (
                not isinstance(marker, dict)
                or marker.get("verdict") not in ("PASS", "CONCERNS", "FAIL", "NON_PASS")
                or not SHA.fullmatch(str(marker.get("head_sha", "")))
                or not SHA.fullmatch(str(marker.get("evaluated_sha", "")))
            ):
                raise ValueError("verifier must be null or a merge-bound validated decision")
        for key in ("verifier_history_complete", "verifier_run_identity_unknown"):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        for key in ("verifier_runs", "verifier_comment_excerpts"):
            if key in payload and (not isinstance(payload[key], list) or len(payload[key]) > 100
                                   or any(not isinstance(row, dict) for row in payload[key])):
                raise ValueError(f"{key} must be a bounded list of objects")


def apply_change(state_path: str | Path, target: str, event: Mapping[str, Any], *,
                 now=time.time) -> dict[str, Any]:
    """Apply one trusted change under the queue lock; never fetch remote state.

    Event fields: ``trusted``, ``source``, ``target``, ``event_id``, ``sequence``,
    ``kind``, ``head``, ``payload`` and optional ``complete``/``missed_events``.
    For all categories except ``commit_head``, head must match current local evidence.
    Returns ``applied``, ``duplicate``, ``out_of_order``, or ``reconciliation_needed``.
    """
    path = Path(state_path)
    row = _validate_event(target, event)
    cutoff = float(now()) + 1.0
    with closer_queue._ledger_lock(path, cutoff=cutoff, now=now) as locked:
        if not locked:
            raise TimeoutError("change feed could not acquire queue state lock")
        state = closer_queue._read_state(path)
        journal_path = _journal_path(path)
        journal = _read_journal(journal_path)
        if row["event_id"] in journal["event_ids"]:
            return {"status": "duplicate", "target": target, "kind": row["kind"]}
        item = state.get("items", {}).get(target)
        if not isinstance(item, dict):
            raise ValueError("target has no existing closer queue item")
        evidence = item.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError("target has no local evidence snapshot")
        if row["kind"] != "commit_head" and evidence.get("head") != row["head"]:
            raise ValueError("change event head does not match current local evidence")
        target_journal = journal["targets"].setdefault(target, {"watermarks": {}, "incomplete": [], "observed_at": None})
        watermarks = target_journal.setdefault("watermarks", {})
        previous = watermarks.get(row["kind"])
        if isinstance(previous, int) and row["sequence"] <= previous:
            journal["event_ids"] = (journal["event_ids"] + [row["event_id"]])[-MAX_EVENT_IDS:]
            _atomic_write(journal_path, journal)
            return {"status": "out_of_order", "target": target, "kind": row["kind"], "watermark": previous}

        remote = evidence.setdefault("remote_changes", {})
        if not isinstance(remote, dict):
            raise ValueError("remote_changes evidence is malformed")
        prior_head = evidence.get("head")
        remote[row["kind"]] = {"source": row["source"], "event_id": row["event_id"],
                               "sequence": row["sequence"], "head": row["head"],
                               "payload": row["payload"], "complete": row["complete"],
                               "observed_at": float(now())}
        if row["kind"] == "commit_head":
            evidence["head"] = row["head"]
        watermarks[row["kind"]] = row["sequence"]
        incomplete = set(target_journal.get("incomplete", []))
        if row["kind"] == "commit_head" and prior_head != row["head"]:
            # Every dependent observation is bound to the old head until refreshed.
            incomplete.update(KINDS - {"commit_head"})
            target_journal["reconciliation_needed"] = True
        if row["missed_events"] or not row["complete"]:
            incomplete.add(row["kind"])
        elif row.get("reconciled", False):
            incomplete.discard(row["kind"])
        elif row["kind"] in incomplete and row["complete"]:
            # A complete event can close a gap only when it explicitly confirms a
            # reconciliation scan, represented by reconciled=True.
            pass
        target_journal["incomplete"] = sorted(incomplete)
        target_journal["observed_at"] = float(now())
        if row["missed_events"]:
            target_journal["reconciliation_needed"] = True
        if event.get("reconciled") is True and row["complete"]:
            incomplete.discard(row["kind"])
            target_journal["incomplete"] = sorted(incomplete)
            target_journal["reconciliation_needed"] = bool(incomplete)
        previous_revision = item.get("revision", evidence.get("revision"))
        revision = _local_revision(previous_revision, remote, evidence.get("head"))
        item["revision"] = revision
        evidence["revision"] = revision
        closer_queue._atomic_write(path, state)
        journal["event_ids"] = (journal["event_ids"] + [row["event_id"]])[-MAX_EVENT_IDS:]
        _atomic_write(journal_path, journal)
        status = "reconciliation_needed" if row["missed_events"] or not row["complete"] else "applied"
        return {"status": status, "target": target, "kind": row["kind"],
                "watermark": row["sequence"], "incomplete_categories": target_journal["incomplete"]}


def read_cached(state_path: str | Path, target: str) -> dict[str, Any] | None:
    """Return the existing local snapshot with a remote overlay and explicit gaps.

    Unknown/unobserved categories remain listed as incomplete, so consumers cannot
    treat absent remote data as current. Local packet fields are preserved verbatim.
    """
    path = Path(state_path)
    with closer_queue._ledger_lock(path, cutoff=time.time() + 1.0, now=time.time) as locked:
        if not locked:
            return None
        state = closer_queue._read_state(path)
        item = state.get("items", {}).get(target)
        if not isinstance(item, dict):
            return None
        snapshot = item.get("evidence")
        if not isinstance(snapshot, dict):
            return None
        raw_snapshot = {key: value for key, value in snapshot.items()
                        if key not in {"remote_changes", "change_feed"}}
        journal = _read_journal(_journal_path(path))
    entry = journal["targets"].get(target, {})
    changes = snapshot.get("remote_changes", {})
    if not isinstance(changes, Mapping):
        changes = {}
    current_head = snapshot.get("head")
    current_changes = {kind: dict(value) for kind, value in changes.items()
                       if isinstance(value, Mapping) and value.get("head") == current_head}
    # A validated delta carries only the fields provided by its category payload.
    # Preserve all other fields from the last local snapshot.
    overlaid = dict(raw_snapshot)
    for change in current_changes.values():
        payload = change.get("payload")
        if isinstance(payload, Mapping):
            overlaid.update(payload)
    known_incomplete = set(entry.get("incomplete", []))
    incomplete = sorted((set(KINDS) - set(current_changes)) | known_incomplete)
    return {
        "snapshot": overlaid,
        "incomplete_categories": incomplete,
        "observed_at": entry.get("observed_at"),
        "watermarks": dict(entry.get("watermarks", {})),
        "reconciliation_needed": bool(entry.get("reconciliation_needed", False)),
        "remote_changes": current_changes,
        "change_feed": {
        "schema": SCHEMA,
        "observed_at": entry.get("observed_at"),
        "watermarks": dict(entry.get("watermarks", {})),
        "incomplete_categories": incomplete,
        "reconciliation_needed": bool(entry.get("reconciliation_needed", False)),
        "offline_safe": True,
        },
    }
