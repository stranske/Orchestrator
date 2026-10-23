"""Bound shared attribution evidence without disposing of referenced progress.

This is a cache client of the existing storage admission ledger, not a cleaner.
Unknown legacy references and interrupted transactions remain protected. There
is no retirement/deletion entry point; recovery material still spends this ceiling.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

import storage_lifecycle as storage
from paths import runtime_state_path

MAX_BYTES = 256 * 1024**2
SCHEMA = "closer-attribution-store/v1"


class AttributionStorageWait(storage.StorageBlocked):
    """This acquisition needs storage; existing evidence remains usable."""

    def __init__(self, reason, *, accounting=None):
        super().__init__(reason)
        self.accounting = accounting or {}


class Store:
    def __init__(self, root: Path, *, maximum: int = MAX_BYTES, ledger=None):
        self.root = Path(root)
        self.maximum = maximum
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("invalid attribution store ceiling")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise AttributionStorageWait("attribution store alias requires reconciliation")
        self.control = self.root / ".retention"
        self.control.mkdir(mode=0o700, exist_ok=True)
        info = self.control.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AttributionStorageWait("attribution control must remain owner-private")
        directory = Path(
            os.environ.get("ORCH_STORAGE_STATE_DIR", runtime_state_path(".storage-lifecycle"))
        )
        self.ledger = ledger or storage.Reservations(directory, self.root, floor=0)

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.control / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.control / "index.json"
            if path.is_symlink():
                raise AttributionStorageWait("attribution index alias; preserved")
            try:
                index = (
                    json.loads(path.read_text())
                    if path.exists()
                    else {"schema": SCHEMA, "objects": {}}
                )
            except (OSError, ValueError) as exc:
                raise AttributionStorageWait("unreadable attribution index; preserved") from exc
            if index.get("schema") != SCHEMA or not isinstance(index.get("objects"), dict):
                raise AttributionStorageWait("unknown attribution index; preserved")
            yield index
        finally:
            os.close(fd)

    def resident(self):
        try:
            # Metadata-only accounting does not follow links. The exact object
            # path is checked separately, preserving its existing identity refusal.
            measured = storage.footprint(self.root)
            return max(measured["allocated"], measured["logical"])
        except (OSError, storage.StorageBlocked) as exc:
            raise AttributionStorageWait("attribution footprint unavailable; preserved") from exc

    def path(self, key):
        if not re.fullmatch(r"[0-9a-f]{64}\.(?:json|zip|txt|md|patch)", key):
            raise AttributionStorageWait("invalid attribution object identity")
        return self.root / "raw" / key[:2] / key

    def safe_path(self, path):
        if not path.resolve().is_relative_to(self.root.resolve()) or any(
            parent.is_symlink()
            for parent in (path, *path.parents)
            if parent == self.root or self.root in parent.parents
        ):
            raise AttributionStorageWait("aliased attribution object; preserved")

    def verify(self, path, sha, size):
        self.safe_path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise AttributionStorageWait("attribution object changed; preserved")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise AttributionStorageWait("attribution digest mismatch; preserved")
        return data

    @contextlib.contextmanager
    def admitted(self, growth):
        resident = self.resident()
        if resident + growth > self.maximum:
            raise AttributionStorageWait(
                "attribution retained-byte ceiling; required acquisition deferred",
                accounting={
                    "resident_bytes": resident,
                    "prospective_bytes": growth,
                    "maximum_bytes": self.maximum,
                },
            )
        job, token = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            self.ledger.admit(job, "closer-attribution/v1", growth, 0, owner_token=token)
        except storage.StorageBlocked as exc:
            try:
                accounting = json.loads(str(exc))
            except ValueError:
                accounting = {}
            raise AttributionStorageWait(str(exc), accounting=accounting) from exc
        try:
            yield
        except Exception:
            # A synchronous Python failure is terminal for these in-process file
            # writes: every handle has unwound, resident partial bytes remain
            # measured. It is not an uncertain remote consequential operation.
            self.ledger.finish(job, {"status": "file_write_failed"}, terminal=True)
            raise
        except BaseException:
            self.ledger.finish(job, {"status": "interrupted"}, terminal=False)
            raise
        else:
            self.ledger.finish(job, {"status": "retained"}, terminal=True)

    def retain(self, data, suffix, *, reference="legacy:unresolved", expired=False):
        if not isinstance(data, bytes) or suffix not in {"json", "zip", "txt", "md", "patch"}:
            raise ValueError("invalid attribution object")
        if not isinstance(reference, str) or not reference or len(reference) > 512:
            raise ValueError("invalid attribution reference")
        sha = hashlib.sha256(data).hexdigest()
        key = f"{sha}.{suffix}"
        path = self.path(key)
        self.safe_path(path)
        with self.locked() as index:
            old = index["objects"].get(key)
            if old is not None and (old.get("sha256") != sha or old.get("bytes") != len(data)):
                raise AttributionStorageWait("attribution index identity changed")
            row = dict(old or {"sha256": sha, "bytes": len(data), "references": []})
            # An unindexed pre-existing object may have historical users we cannot
            # infer from this invocation. Import it protected, never as disposable.
            if old is None and path.exists():
                row["references"].append("legacy:unresolved")
            row["references"] = sorted(set(row["references"]) | {reference})
            row["expired_required"] = bool(row.get("expired_required") or expired)
            if path.exists():
                self.verify(path, sha, len(data))
                if old == row:
                    return {"sha256": sha, "path": str(path), "bytes": len(data)}
            row["phase"] = "active"
            index["objects"][key] = row
            encoded = json.dumps(index, sort_keys=True).encode()
            # Include atomic temporary object/index copies plus block/receipt slack.
            growth = (0 if path.exists() else 2 * len(data)) + 2 * len(encoded) + 65536
            with self.admitted(growth):
                # Register the reference before publishing bytes or a dependent receipt.
                storage.write_json(self.control / "index.json", index)
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_name(path.name + ".pending")
                    if temp.exists() or temp.is_symlink():
                        # Every store writer holds this same lock through close.
                        # Resume only an exact verified staging object, never an
                        # unknown partial file or an uncertain external effect.
                        self.verify(temp, sha, len(data))
                    else:
                        with temp.open("xb") as output:
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                    os.replace(temp, path)
                self.verify(path, sha, len(data))
        return {"sha256": sha, "path": str(path), "bytes": len(data)}
