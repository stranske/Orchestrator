"""Durable, single-record release journal for the Orchestrator publisher.

This is a journal for one bounded in-flight release, not a queue. Callers keep
using verify_before_sync.sh and install_verified_snapshot.py; the journal only
records their receipts and compare-and-swap boundaries so a process restart can
resume or roll back deliberately.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^generation-v1:[0-9a-f]{64}$")
_ACTIVE = {"prepared", "verifying", "verified", "installing", "installed", "acceptance_pending", "rollback_pending"}
_STAGES = _ACTIVE | {"accepted", "rolled_back", "failed"}


class ReleaseError(RuntimeError):
    """Base error for invalid or conflicting journal operations."""


class ReleaseConflict(ReleaseError):
    """The record is owned by another lane or has changed since it was read."""


class ReleaseRecord:
    """Atomic, fsynced state transitions for one active release candidate."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def read(self) -> dict[str, Any] | None:
        with self._lock():
            return self._read_unlocked()

    def begin(self, candidate_sha: str, incumbent_token: str, lane_owner: str, *, run_id: str | None = None) -> dict[str, Any]:
        """Create the record, or resume this exact candidate under its owner."""
        if not _SHA.fullmatch(candidate_sha):
            raise ValueError("candidate_sha must be a lowercase git SHA")
        if not incumbent_token or not lane_owner:
            raise ValueError("incumbent_token and lane_owner are required")
        with self._lock():
            current = self._read_unlocked()
            if current and current["stage"] in _ACTIVE:
                if current["candidate_sha"] != candidate_sha or current["lane_owner"] != lane_owner:
                    raise ReleaseConflict("an active release is owned by another candidate or lane")
                return current
            now = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "release_id": uuid.uuid4().hex,
                "revision": 1,
                "candidate_sha": candidate_sha,
                "incumbent_token": incumbent_token,
                "lane_owner": lane_owner,
                "run_id": run_id,
                "stage": "prepared",
                "created_at": now,
                "updated_at": now,
                "verification": None,
                "installed_generation": None,
                "live_acceptance": None,
                "rollback": None,
                "failure": None,
            }
            self._write_unlocked(record)
            return record

    def transition(self, stage: str, *, lane_owner: str, expected_revision: int, **fields: Any) -> dict[str, Any]:
        """Record one legal transition; revision and lane ownership are CAS guards."""
        if stage not in _STAGES:
            raise ValueError(f"unknown release stage: {stage}")
        with self._lock():
            current = self._read_unlocked()
            if current is None:
                raise ReleaseError("no release record exists")
            if current["lane_owner"] != lane_owner:
                raise ReleaseConflict("release lane ownership changed")
            if current["revision"] != expected_revision:
                raise ReleaseConflict("release record revision changed")
            if stage not in _ALLOWED.get(current["stage"], set()):
                raise ReleaseError(f"illegal release transition: {current['stage']} -> {stage}")
            unknown = fields.keys() - _MUTABLE_FIELDS
            if unknown:
                raise ValueError(f"unknown release fields: {', '.join(sorted(unknown))}")
            updated = dict(current)
            updated.update(fields)
            updated["stage"] = stage
            updated["revision"] += 1
            updated["updated_at"] = _now()
            _validate_record(updated)
            self._write_unlocked(updated)
            return updated

    def record_verification(self, *, lane_owner: str, expected_revision: int, verified: bool,
                            source_identity: str | None, snapshot_path: str | Path | None,
                            snapshot_digest: str | None, log_path: str | Path, exit_code: int) -> dict[str, Any]:
        """Persist verifier receipt and log reference; non-green verdict closes as failed."""
        receipt = {
            "verified": bool(verified), "source_identity": source_identity,
            "snapshot_path": str(Path(snapshot_path).absolute()) if snapshot_path else None,
            "snapshot_digest": snapshot_digest, "log_path": str(Path(log_path).absolute()),
            "exit_code": int(exit_code),
        }
        return self.transition("verified" if verified else "failed", lane_owner=lane_owner,
                               expected_revision=expected_revision, verification=receipt,
                               failure=None if verified else {"stage": "verification", "exit_code": int(exit_code)})

    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+b")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return _LockContext(stream)

    def _read_unlocked(self) -> dict[str, Any] | None:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return None
        if len(data) > MAX_RECORD_BYTES:
            raise ReleaseError("release record exceeds its size bound")
        try:
            record = json.loads(data)
            _validate_record(record)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ReleaseError(f"release record is invalid: {exc}") from exc
        return record

    def _write_unlocked(self, record: dict[str, Any]) -> None:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_RECORD_BYTES:
            raise ReleaseError("release record exceeds its size bound")
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temporary.unlink(missing_ok=True)


class _LockContext:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_ALLOWED = {
    "prepared": {"verifying", "failed"}, "verifying": {"verified", "failed"},
    "verified": {"installing", "failed"}, "installing": {"installed", "failed"},
    "installed": {"acceptance_pending", "accepted", "rollback_pending", "failed"},
    "acceptance_pending": {"accepted", "rollback_pending", "failed"},
    "rollback_pending": {"rolled_back", "failed"}, "accepted": set(),
    "rolled_back": set(), "failed": set(),
}
_MUTABLE_FIELDS = {"verification", "installed_generation", "live_acceptance", "rollback", "failure"}


def _validate_record(record: dict[str, Any]) -> None:
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")
    if record.get("stage") not in _STAGES:
        raise ValueError("invalid stage")
    if not _SHA.fullmatch(str(record.get("candidate_sha", ""))):
        raise ValueError("invalid candidate SHA")
    if not record.get("incumbent_token") or not record.get("lane_owner"):
        raise ValueError("missing incumbent token or lane owner")
    if not isinstance(record.get("revision"), int) or record["revision"] < 1:
        raise ValueError("invalid revision")
    verification = record.get("verification")
    if verification is not None:
        if not isinstance(verification, dict):
            raise ValueError("invalid verification receipt")
        if verification.get("verified"):
            if not _DIGEST.fullmatch(str(verification.get("snapshot_digest", ""))):
                raise ValueError("verified receipt requires a SHA-256 snapshot digest")
            if not verification.get("source_identity"):
                raise ValueError("verified receipt requires the verifier's source identity")
            if not verification.get("snapshot_path") or not verification.get("log_path"):
                raise ValueError("verified receipt requires snapshot and log paths")
    if record["stage"] in {"verified", "installing", "installed", "acceptance_pending", "accepted", "rollback_pending", "rolled_back"}:
        if not isinstance(verification, dict) or not verification.get("verified"):
            raise ValueError("this stage requires a successful verification receipt")
    generation = record.get("installed_generation")
    if generation is not None and not _GENERATION.fullmatch(str(generation)):
        raise ValueError("invalid installed generation token")
    if record["stage"] in {"installed", "acceptance_pending", "accepted", "rollback_pending", "rolled_back"} and generation is None:
        raise ValueError("this stage requires the compare-and-swap installed generation")
    if record["stage"] == "accepted" and not (record.get("live_acceptance") or {}).get("accepted"):
        raise ValueError("accepted stage requires affirmative live acceptance evidence")
    if record["stage"] == "rolled_back" and not isinstance(record.get("rollback"), dict):
        raise ValueError("rolled_back stage requires rollback evidence")
