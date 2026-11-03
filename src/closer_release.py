"""Durable, single-record release journal for the Orchestrator publisher.

This is a journal for one bounded in-flight release, not a queue. Callers keep
using verify_before_sync.sh and install_verified_snapshot.py; the journal only
records their receipts and compare-and-swap boundaries so a process restart can
resume or roll back deliberately.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{40}$")
_GENERATION = re.compile(r"^generation-v1:[0-9a-f]{64}$")
_ACTIVE = {
    "prepared",
    "verifying",
    "verified",
    "installing",
    "installed",
    "acceptance_pending",
    "rollback_pending",
}
_STAGES = _ACTIVE | {"accepted", "rolled_back", "failed"}


class ReleaseError(RuntimeError):
    """Base error for invalid or conflicting journal operations."""


class ReleaseConflict(ReleaseError):
    """The record is owned by another lane or has changed since it was read."""


class ReleaseHeld(ReleaseError):
    """Source installation is deferred while the canonical Closer lane is owned."""


def _serialized(operation):
    def wrapped(self, *args, **kwargs):
        with self._operation_lock():
            return operation(self, *args, **kwargs)

    return wrapped


class ReleaseRecord:
    """Atomic, fsynced state transitions for one active release candidate."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def read(self) -> dict[str, Any] | None:
        with self._lock():
            return self._read_unlocked()

    def begin(
        self,
        candidate_sha: str,
        incumbent_token: str,
        lane_owner: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
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

    def transition(
        self, stage: str, *, lane_owner: str, expected_revision: int, **fields: Any
    ) -> dict[str, Any]:
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

    def record_verification(
        self,
        *,
        lane_owner: str,
        expected_revision: int,
        verified: bool,
        source_identity: str | None,
        snapshot_path: str | Path | None,
        snapshot_digest: str | None,
        log_path: str | Path,
        exit_code: int,
    ) -> dict[str, Any]:
        """Persist verifier receipt and log reference; non-green verdict closes as failed."""
        receipt = {
            "verified": bool(verified),
            "source_identity": source_identity,
            "snapshot_path": str(Path(snapshot_path).absolute()) if snapshot_path else None,
            "snapshot_digest": snapshot_digest,
            "log_path": str(Path(log_path).absolute()),
            "exit_code": int(exit_code),
        }
        return self.transition(
            "verified" if verified else "failed",
            lane_owner=lane_owner,
            expected_revision=expected_revision,
            verification=receipt,
            failure=None if verified else {"stage": "verification", "exit_code": int(exit_code)},
        )

    @_serialized
    def verify_and_install(
        self,
        *,
        candidate_sha: str,
        lane_owner: str,
        source: str | Path,
        mirror: str | Path,
        verifier: str | Path,
        installer: str | Path,
        python: str | Path | None = None,
        bash: str | Path | None = None,
        run_id: str | None = None,
        timeout: float = 3600,
    ) -> dict[str, Any]:
        """Run the repository verifier and installer with a durable CAS journal.

        A new release captures the installer generation token before verification.
        Restarts reuse the record's original token and, after an interrupted install,
        accept readback only when the live deployment digest matches the retained
        verified digest. No verifier ceiling or tree-shape environment override is
        inherited by the gate subprocess.
        """
        source = Path(source).expanduser().resolve(strict=True)
        mirror = Path(mirror).expanduser().absolute()
        verifier = Path(verifier).expanduser().resolve(strict=True)
        installer = Path(installer).expanduser().resolve(strict=True)
        # The selected venv interpreter can be a symlink to its base Python.
        # Preserve that invocation path: it is the environment the preflight
        # admitted and the path placed first in the verifier's environment.
        python = Path(python or sys.executable).expanduser().absolute()
        bash_path = Path(bash or shutil.which("bash") or "").expanduser().resolve(strict=True)
        if (
            not python.is_file()
            or not os.access(python, os.X_OK)
            or not os.access(bash_path, os.X_OK)
        ):
            raise ValueError("pinned Python and Bash executables must be executable")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        verifier_env = _verification_env(python, mirror)
        source_head = _run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            env=verifier_env,
            timeout=timeout,
        ).strip()
        if source_head != candidate_sha:
            raise ReleaseError("release candidate does not match source HEAD")
        if _run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
            env=verifier_env,
            timeout=timeout,
        ).strip():
            raise ReleaseError("release source has uncommitted or untracked files")

        state = self.read()
        if state is not None and state["stage"] in _ACTIVE:
            # The call's candidate and owner are checked by begin; its token argument
            # is deliberately ignored for a resumed record.
            incumbent = state["incumbent_token"]
        else:
            incumbent = _run(
                [str(python), str(installer), str(mirror), "--generation-token"],
                env=_child_env(python),
                timeout=timeout,
            ).strip()
            if not _GENERATION.fullmatch(incumbent):
                raise ReleaseError(
                    "installer returned an invalid pre-verification generation token"
                )
        state = self.begin(candidate_sha, incumbent, lane_owner, run_id=run_id)
        if state["stage"] in {"accepted", "rolled_back"}:
            raise ReleaseError(f"release is already terminal: {state['stage']}")
        artifacts = self.path.parent / (self.path.name + ".artifacts") / state["release_id"]
        snapshot = artifacts / "verified-snapshot"
        digest_file = artifacts / "verified-digest.txt"
        log_file = artifacts / "verification.log"

        if state["stage"] in {"prepared", "verifying"}:
            if state["stage"] == "prepared":
                state = self.transition(
                    "verifying",
                    lane_owner=lane_owner,
                    expected_revision=state["revision"],
                    verification={
                        "status": "running",
                        "snapshot_path": str(snapshot),
                        "digest_path": str(digest_file),
                        "log_path": str(log_file),
                    },
                )
            artifacts.mkdir(parents=True, exist_ok=True)
            _remove_leaf(snapshot)
            _remove_leaf(digest_file)
            before = _run(
                [str(bash_path), str(verifier), "--identity", str(source)],
                env=_child_env(python),
                timeout=timeout,
            ).strip()
            if not _FINGERPRINT.fullmatch(before):
                return self._verification_failed(
                    state,
                    lane_owner,
                    log_file,
                    "identity",
                    "verifier returned malformed source identity",
                )
            result = subprocess.run(
                [
                    str(bash_path),
                    str(verifier),
                    "--snapshot-out",
                    str(snapshot),
                    "--digest-out",
                    str(digest_file),
                    str(source),
                ],
                env=verifier_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            _write_durable(log_file, result.stdout)
            text = result.stdout.decode("utf-8", "replace")
            identities = re.findall(r"(?m)^\s*verified source identity: ([0-9a-f]{40})\s*$", text)
            after = _run(
                [str(bash_path), str(verifier), "--identity", str(source)],
                env=_child_env(python),
                timeout=timeout,
            ).strip()
            valid_artifact = (
                snapshot.is_dir() and digest_file.is_file() and not snapshot.is_symlink()
            )
            digest = (
                digest_file.read_text(encoding="ascii").strip() if digest_file.is_file() else ""
            )
            digest_probe = ""
            if valid_artifact and _DIGEST.fullmatch(digest):
                digest_probe = _run(
                    [str(python), str(installer), str(snapshot), "--digest"],
                    env=_child_env(python),
                    timeout=timeout,
                ).strip()
            verified = (
                result.returncode == 0
                and before == after
                and identities == [before]
                and valid_artifact
                and _DIGEST.fullmatch(digest) is not None
                and digest_probe == digest
            )
            if not verified:
                reason = (
                    "verifier failed"
                    if result.returncode
                    else "source identity or verified snapshot receipt did not match"
                )
                return self._verification_failed(
                    state,
                    lane_owner,
                    log_file,
                    result.returncode or 3,
                    reason,
                    snapshot=snapshot,
                    digest=digest or None,
                    source_identity=before,
                )
            state = self.record_verification(
                lane_owner=lane_owner,
                expected_revision=state["revision"],
                verified=True,
                source_identity=before,
                snapshot_path=snapshot,
                snapshot_digest=digest,
                log_path=log_file,
                exit_code=0,
            )

        # Native Closer owns the canonical lane.  Take its parent coordination
        # flock before inspecting the lane, then retain it through publication
        # and every readback so a new Closer cannot acquire between either.
        # A held lane intentionally leaves this record VERIFIED, allowing a
        # later call to resume this exact receipt without another verification.
        with _closer_lane_installation_guard():
            if state["stage"] == "verified":
                state = self.transition(
                    "installing", lane_owner=lane_owner, expected_revision=state["revision"]
                )
            if state["stage"] != "installing":
                raise ReleaseError(f"release cannot install from stage {state['stage']}")

            receipt = state.get("verification") or {}
            snapshot = Path(receipt["snapshot_path"])
            expected_digest = receipt["snapshot_digest"]
            current = _run(
                [str(python), str(installer), str(mirror), "--generation-token"],
                env=_child_env(python),
                timeout=timeout,
            ).strip()
            if current != state["incumbent_token"]:
                if (
                    _installed_digest(
                        python, installer, mirror, env=_child_env(python), timeout=timeout
                    )
                    == expected_digest
                ):
                    return self._record_installed(
                        state, lane_owner, python, installer, mirror, timeout
                    )
                return self._install_failed(
                    state, lane_owner, "incumbent generation changed before install"
                )

            installed = subprocess.run(
                [
                    str(python),
                    str(installer),
                    str(snapshot),
                    str(mirror),
                    "--expected-digest",
                    expected_digest,
                    "--expected-generation",
                    state["incumbent_token"],
                ],
                env=_child_env(python),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            if installed.returncode != 0:
                if (
                    _installed_digest(
                        python, installer, mirror, env=_child_env(python), timeout=timeout
                    )
                    == expected_digest
                ):
                    return self._record_installed(
                        state, lane_owner, python, installer, mirror, timeout
                    )
                return self._install_failed(
                    state, lane_owner, f"installer failed with exit code {installed.returncode}"
                )
            if (
                _installed_digest(
                    python, installer, mirror, env=_child_env(python), timeout=timeout
                )
                != expected_digest
            ):
                return self._install_failed(
                    state, lane_owner, "installed deployment digest readback failed"
                )
            return self._record_installed(state, lane_owner, python, installer, mirror, timeout)

    def _verification_failed(
        self,
        state,
        lane_owner,
        log_file,
        exit_code,
        reason,
        *,
        snapshot=None,
        digest=None,
        source_identity=None,
    ):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if not log_file.exists():
            _write_durable(log_file, (reason + "\n").encode())
        return self.record_verification(
            lane_owner=lane_owner,
            expected_revision=state["revision"],
            verified=False,
            source_identity=source_identity,
            snapshot_path=snapshot,
            snapshot_digest=digest,
            log_path=log_file,
            exit_code=int(exit_code) if isinstance(exit_code, int) else 3,
        )

    def _install_failed(self, state, lane_owner, reason):
        return self.transition(
            "failed",
            lane_owner=lane_owner,
            expected_revision=state["revision"],
            failure={"stage": "install", "reason": reason},
        )

    def _record_installed(self, state, lane_owner, python, installer, mirror, timeout):
        env = _child_env(python)
        generation = _run(
            [str(python), str(installer), str(mirror), "--generation-token"],
            env=env,
            timeout=timeout,
        ).strip()
        if not _GENERATION.fullmatch(generation):
            return self._install_failed(
                state, lane_owner, "installer returned invalid installed generation"
            )
        expected_digest = state["verification"]["snapshot_digest"]
        if (
            _installed_digest(python, installer, mirror, env=env, timeout=timeout)
            != expected_digest
        ):
            return self._install_failed(
                state, lane_owner, "installed deployment digest readback failed"
            )
        confirmed = _run(
            [str(python), str(installer), str(mirror), "--generation-token"],
            env=env,
            timeout=timeout,
        ).strip()
        if confirmed != generation:
            return self._install_failed(
                state, lane_owner, "installed generation changed during readback"
            )
        return self.transition(
            "installed",
            lane_owner=lane_owner,
            expected_revision=state["revision"],
            installed_generation=generation,
        )

    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+b")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return _LockContext(stream)

    def _operation_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.with_name(self.path.name + ".publisher.lock").open("a+b")
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
        fd, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
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


@contextlib.contextmanager
def _closer_lane_installation_guard():
    """Serialize Source installation with the canonical native Closer lane.

    This deliberately uses the native lease module's canonical parent-lock
    protocol.  It neither creates nor releases the lane directory; any present
    directory (including a symlink) remains an incumbent owner's evidence.
    """
    from closer_native_lease import _coord_path, _default_lane_dir, _handoff_root

    handoff_root = _handoff_root(Path.home() / ".codex" / "handoff")
    lane_dir = _default_lane_dir(handoff_root, "closer")
    coord = _coord_path(lane_dir)
    try:
        coord.parent.mkdir(parents=True, exist_ok=True)
        handle = coord.open("a+")
    except OSError as exc:
        raise ReleaseHeld("source installation held: closer lane coordination unavailable") from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseHeld(
                "source installation held: closer lane coordination lock contended"
            ) from exc
        if lane_dir.exists() or lane_dir.is_symlink():
            raise ReleaseHeld("source installation held: current closer lane mutex present")
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _child_env(python: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The installed verifier's default ceiling is 50 MB and its expected shape is
    # EXEC MIRROR. Never inherit caller overrides that relax either gate or waive
    # a floor; only pin the interpreter used by the existing script.
    for name in (
        "VERIFY_BEFORE_SYNC_MAX_DIR_MB",
        "VERIFY_BEFORE_SYNC_TREE",
        "VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG",
    ):
        env.pop(name, None)
    env["PYTHON"] = str(python)
    return env


def _verification_env(python: Path, mirror: Path) -> dict[str, str]:
    """Share byte admission and claim the logical publication target, not a candidate.

    Existing nonparticipating writers still need explicit coordinator handoff;
    this environment is not proof that a legacy verifier stopped.
    """
    env = _child_env(python)
    for name in (
        "ORCH_STORAGE_ENTRY_PID",
        "ORCH_STORAGE_RUN_ID",
        "ORCH_STORAGE_RUN_ROOT",
        "ORCH_STORAGE_OWNER_TOKEN",
    ):
        env.pop(name, None)
    runtime = Path(env.get("ORCH_LOCAL_RUNTIME", str(Path.home() / ".codex/orchestrator")))
    env["ORCH_STORAGE_STATE_DIR"] = env.get(
        "ORCH_STORAGE_STATE_DIR", str(runtime / ".storage-lifecycle")
    )
    logical = mirror.expanduser().absolute()
    env["VERIFY_BEFORE_SYNC_OWNER_KEY"] = "publication:" + str(
        logical.parent.resolve() / logical.name
    )
    return env


def _run(command: list[str], *, env: dict[str, str], timeout: float) -> str:
    result = subprocess.run(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"command failed ({result.returncode}): {detail}")
    return result.stdout.decode("utf-8", "replace")


def _write_durable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_leaf(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _installed_digest(
    python: Path, installer: Path, mirror: Path, *, env: dict[str, str], timeout: float
) -> str:
    try:
        return _run(
            [str(python), str(installer), str(mirror), "--digest"], env=env, timeout=timeout
        ).strip()
    except (OSError, ReleaseError):
        return ""


_ALLOWED = {
    "prepared": {"verifying", "failed"},
    "verifying": {"verified", "failed"},
    "verified": {"installing", "failed"},
    "installing": {"installed", "failed"},
    "installed": {"acceptance_pending", "accepted", "rollback_pending", "failed"},
    "acceptance_pending": {"accepted", "rollback_pending", "failed"},
    "rollback_pending": {"rolled_back", "failed"},
    "accepted": set(),
    "rolled_back": set(),
    "failed": set(),
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
    if record["stage"] in {
        "verified",
        "installing",
        "installed",
        "acceptance_pending",
        "accepted",
        "rollback_pending",
        "rolled_back",
    }:
        if not isinstance(verification, dict) or not verification.get("verified"):
            raise ValueError("this stage requires a successful verification receipt")
    generation = record.get("installed_generation")
    if generation is not None and not _GENERATION.fullmatch(str(generation)):
        raise ValueError("invalid installed generation token")
    if (
        record["stage"]
        in {"installed", "acceptance_pending", "accepted", "rollback_pending", "rolled_back"}
        and generation is None
    ):
        raise ValueError("this stage requires the compare-and-swap installed generation")
    if record["stage"] == "accepted" and not (record.get("live_acceptance") or {}).get("accepted"):
        raise ValueError("accepted stage requires affirmative live acceptance evidence")
    if record["stage"] == "rolled_back" and not isinstance(record.get("rollback"), dict):
        raise ValueError("rolled_back stage requires rollback evidence")
