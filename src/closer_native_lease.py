"""Cooperative per-lane round lease for handoff prerun/postrun (preview protocol).

Protocol marker HANDOFF_EXACT_TOKEN_V1: acquire refuses any pre-existing lane directory;
release removes the directory only when the expected credential matches stored meta under a
short parent-file flock keyed by the canonical lane directory. No rm -rf and no stale takeover.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROTOCOL = "HANDOFF_EXACT_TOKEN_V1"
META_FIELDS = 5
_COORD_SUFFIX = ".coord.lock"
_LANE_PREFIX = ".lane-"
_LANE_SUFFIX = ".lock"
_LANE_AGENT_RE = re.compile(r"^[\w][\w.-]*$")


class LeaseError(RuntimeError):
    """Base lease failure."""


class LeaseRefused(LeaseError):
    """Acquire refused (contention or existing lane directory)."""


class LeaseShapeError(LeaseError):
    """Lane directory or meta does not match the expected protocol shape."""


def _handoff_root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _validate_name(label: str, value: str) -> None:
    if not value or value != value.strip():
        raise LeaseShapeError(f"invalid {label}")
    if "\n" in value or "\r" in value or "/" in value or "\\" in value or ".." in value:
        raise LeaseShapeError(f"invalid {label}")
    if not _LANE_AGENT_RE.match(value):
        raise LeaseShapeError(f"invalid {label}")


def _validate_lane_agent(lane: str, agent: str) -> None:
    _validate_name("lane", lane)
    _validate_name("agent", agent)


def _default_lane_dir(root: Path, lane: str) -> Path:
    return (root / f"{_LANE_PREFIX}{lane}{_LANE_SUFFIX}").resolve()


def _lane_dir(root: Path, lane: str, override: str | None = None) -> Path:
    _validate_name("lane", lane)
    if override:
        raw = Path(override).expanduser()
        if raw.is_symlink():
            raise LeaseShapeError("lane dir override must not be a symlink")
        resolved = raw.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            raise LeaseShapeError("lane dir override outside handoff root") from None
        return resolved
    return _default_lane_dir(root, lane)


def _coord_path(lane_dir: Path) -> Path:
    """Parent flock file for a canonical lane directory (aliases share one lock)."""
    resolved = lane_dir.resolve()
    return resolved.parent / f"{resolved.name}{_COORD_SUFFIX}"


def _parent_lock(coord: Path):
    coord.parent.mkdir(parents=True, exist_ok=True)
    handle = coord.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _unlock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mint_token(lane_dir: Path) -> str:
    inode = lane_dir.stat().st_ino if lane_dir.exists() else 0
    return f"{PROTOCOL}:{uuid.uuid4().hex}:{secrets.token_hex(16)}:{inode}"


def normalize_meta_bytes(data: bytes) -> bytes:
    """Single-line meta with exactly one trailing newline (storage + compare contract)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LeaseShapeError("meta is not utf-8") from exc
    if not text.strip():
        raise LeaseShapeError("empty meta")
    lines = text.splitlines()
    if len(lines) != 1:
        raise LeaseShapeError(f"meta line count {len(lines)} != 1")
    return (lines[0] + "\n").encode("utf-8")


def _parse_meta_line(line: str) -> tuple[str, str, str, str, str]:
    parts = line.split()
    if len(parts) != META_FIELDS:
        raise LeaseShapeError(f"meta field count {len(parts)} != {META_FIELDS}")
    pid, epoch, lane, agent, token = parts
    if not token.startswith(f"{PROTOCOL}:"):
        raise LeaseShapeError("meta token missing protocol marker")
    _validate_lane_agent(lane, agent)
    return pid, epoch, lane, agent, token


def _parse_meta(text: str) -> tuple[str, str, str, str, str]:
    norm = normalize_meta_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return _parse_meta_line(norm.decode("utf-8").rstrip("\n"))


def read_meta_bytes(lane_dir: Path) -> bytes:
    meta_path = lane_dir / "meta"
    if not meta_path.is_file():
        raise LeaseShapeError("missing meta file")
    return normalize_meta_bytes(meta_path.read_bytes())


def read_meta(lane_dir: Path) -> dict[str, str]:
    raw = read_meta_bytes(lane_dir)
    pid, epoch, lane, agent, token = _parse_meta_line(raw.decode("utf-8").rstrip("\n"))
    return {
        "owner_pid": pid,
        "acquired_epoch": epoch,
        "lane": lane,
        "agent": agent,
        "token": token,
    }


def _credential_matches(meta_bytes: bytes, expected: str) -> bool:
    if not expected:
        return False
    try:
        stored = _parse_meta_line(meta_bytes.decode("utf-8").rstrip("\n"))
    except LeaseShapeError:
        return False
    if expected.startswith(f"{PROTOCOL}:"):
        return stored[4] == expected
    try:
        expected_norm = normalize_meta_bytes(expected.encode("utf-8"))
    except LeaseShapeError:
        return False
    return meta_bytes == expected_norm


def _lane_dir_releaseable(lane_dir: Path) -> bool:
    if not lane_dir.is_dir():
        return True
    names = {p.name for p in lane_dir.iterdir()}
    return names <= {"meta"}


def acquire(
    root: Path,
    lane: str,
    agent: str,
    owner_pid: int,
    *,
    lane_dir_override: str | None = None,
    refuse_existing_lane: bool = False,
) -> str:
    _validate_lane_agent(lane, agent)
    lane_dir = _lane_dir(root, lane, lane_dir_override)
    coord = _coord_path(lane_dir)
    handle = _parent_lock(coord)
    try:
        if lane_dir.exists():
            raise LeaseRefused("lane directory already exists")
        lane_dir.mkdir(parents=False, exist_ok=False)
        token = mint_token(lane_dir)
        epoch = str(int(time.time()))
        meta_line = f"{owner_pid} {epoch} {lane} {agent} {token}\n"
        meta_path = lane_dir / "meta"
        fence = lane_dir / ".lease-writing"
        fence.write_text("1", encoding="utf-8")
        tmp = lane_dir / ".meta.tmp"
        with tmp.open("w", encoding="utf-8") as handle_meta:
            handle_meta.write(meta_line)
            handle_meta.flush()
            os.fsync(handle_meta.fileno())
        os.replace(tmp, meta_path)
        _fsync_dir(lane_dir)
        fence.unlink(missing_ok=True)
        return token
    except FileExistsError:
        raise LeaseRefused("lane directory already exists") from None
    finally:
        _unlock(handle)


def release_with_receipt(
    root: Path,
    lane: str,
    expected_token: str,
    *,
    lane_dir_override: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Release the matching lease and attest that removal while holding its lock.

    Absence remains an idempotent success, but supplies no exact-release receipt.
    A caller must never use absence to invent ownership of an earlier lease.
    """
    lane_dir = _lane_dir(root, lane, lane_dir_override)
    coord = _coord_path(lane_dir)
    if not lane_dir.is_dir():
        return True, None
    handle = _parent_lock(coord)
    try:
        if not lane_dir.is_dir():
            return True, None
        if not _lane_dir_releaseable(lane_dir):
            return False, None
        try:
            meta_bytes = read_meta_bytes(lane_dir)
        except LeaseShapeError:
            return False, None
        if not _credential_matches(meta_bytes, expected_token):
            return False, None
        try:
            identity = read_meta(lane_dir)
            if (
                identity["lane"] != lane
                or int(identity["token"].rsplit(":", 1)[1]) != lane_dir.stat().st_ino
            ):
                return False, None
        except (LeaseShapeError, ValueError, OSError):
            return False, None
        lease_inode = lane_dir.stat().st_ino
        meta_path = lane_dir / "meta"
        meta_path.unlink(missing_ok=True)
        try:
            lane_dir.rmdir()
        except OSError:
            # A non-cooperating file appeared after the shape check. Retain the
            # original evidence without replacing any concurrent meta write.
            with meta_path.open("xb") as restored:
                restored.write(meta_bytes)
                restored.flush()
                os.fsync(restored.fileno())
            _fsync_dir(lane_dir)
            return False, None
        _fsync_dir(lane_dir.parent)
        return True, {
            "schema": "closer-native-lease-cleanup/v1",
            "lane": lane,
            "lane_dir": str(lane_dir),
            "expected_token_sha256": hashlib.sha256(expected_token.encode()).hexdigest(),
            "meta_sha256": hashlib.sha256(meta_bytes).hexdigest(),
            "released_lease_inode": lease_inode,
            "released": True,
            "exact_token_matched": True,
            "released_at": time.time(),
        }
    finally:
        _unlock(handle)


def release(
    root: Path,
    lane: str,
    expected_token: str,
    *,
    lane_dir_override: str | None = None,
) -> bool:
    """Compatibility API: an absent lane is still an idempotent success."""
    ok, _receipt = release_with_receipt(
        root, lane, expected_token, lane_dir_override=lane_dir_override
    )
    return ok


def protocol_record(lease_module: Path) -> dict[str, Any]:
    data = lease_module.read_bytes()
    import hashlib

    return {
        "protocol": PROTOCOL,
        "meta_fields": META_FIELDS,
        "coord_lock": _COORD_SUFFIX,
        "coord_lock_key": "canonical_lane_dir",
        "lease_module_sha256": hashlib.sha256(data).hexdigest(),
        "cleanup_receipt_schema": "closer-native-lease-cleanup/v1",
        "refuse_existing_lane_env": "HANDOFF_REFUSE_EXISTING_LANE",
        "expected_token_env": "HANDOFF_EXPECTED_LANE_TOKEN",
        "lane_token_env": "HANDOFF_LANE_TOKEN",
        "expected_credential": "full meta line bytes (single line + newline) or bare HANDOFF_EXACT_TOKEN_V1 token",
    }


def _resolve_root(handoff_root: str) -> Path:
    if handoff_root:
        return _handoff_root(handoff_root)
    env = os.environ.get("HANDOFF_HANDOFF_ROOT", "")
    if env:
        return _handoff_root(env)
    return _handoff_root(Path.home() / ".codex" / "handoff")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Handoff lane lease (HANDOFF_EXACT_TOKEN_V1)")
    parser.add_argument(
        "--handoff-root",
        default=os.environ.get("HANDOFF_HANDOFF_ROOT", ""),
        help="Handoff state root (before subcommand; default: $HANDOFF_HANDOFF_ROOT)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    acquire_p = sub.add_parser("acquire")
    acquire_p.add_argument("--lane", required=True)
    acquire_p.add_argument("--agent", required=True)
    acquire_p.add_argument("--owner-pid", type=int, default=0)
    acquire_p.add_argument("--lane-dir", default="")
    acquire_p.add_argument(
        "--refuse-existing-lane",
        action="store_true",
        default=os.environ.get("HANDOFF_REFUSE_EXISTING_LANE", "") == "1",
    )

    release_p = sub.add_parser("release")
    release_p.add_argument("--lane", required=True)
    release_p.add_argument("--expected-token", required=True)
    release_p.add_argument("--lane-dir", default="")
    release_p.add_argument(
        "--receipt-json", action="store_true", help="attest actual exact-token removal"
    )

    sub.add_parser("protocol-json")

    inspect_p = sub.add_parser("inspect")
    inspect_p.add_argument("--lane", required=True)
    inspect_p.add_argument("--lane-dir", default="")

    args = parser.parse_args(argv)
    root = _resolve_root(args.handoff_root)

    if args.cmd == "protocol-json":
        mod = Path(__file__).resolve()
        print(json.dumps(protocol_record(mod), sort_keys=True))
        return 0

    if args.cmd == "acquire":
        owner = args.owner_pid or os.getppid()
        try:
            token = acquire(
                root,
                args.lane,
                args.agent,
                owner,
                lane_dir_override=args.lane_dir or None,
                refuse_existing_lane=args.refuse_existing_lane,
            )
        except LeaseRefused as exc:
            print(f"RELAY-HALTED: {exc}", file=sys.stderr)
            return 1
        except LeaseShapeError as exc:
            print(f"handoff-lease: shape error: {exc}", file=sys.stderr)
            return 3
        sys.stdout.write(token + "\n")
        return 0

    if args.cmd == "release":
        try:
            ok, receipt = release_with_receipt(
                root,
                args.lane,
                args.expected_token,
                lane_dir_override=args.lane_dir or None,
            )
        except LeaseShapeError:
            return 3
        if args.receipt_json:
            if not ok:
                return 2
            if receipt is None:
                return 4  # Already absent is not evidence of this caller's exact cleanup.
            print(json.dumps(receipt, sort_keys=True))
        return 0 if ok else 2

    if args.cmd == "inspect":
        try:
            lane_dir = _lane_dir(root, args.lane, args.lane_dir or None)
        except LeaseShapeError as exc:
            print(json.dumps({"present": False, "shape_error": str(exc)}))
            return 3
        if not lane_dir.is_dir():
            print(json.dumps({"present": False}))
            return 0
        try:
            meta = read_meta(lane_dir)
            meta_raw = read_meta_bytes(lane_dir).decode("utf-8")
        except LeaseShapeError as exc:
            print(json.dumps({"present": True, "shape_error": str(exc)}))
            return 3
        print(
            json.dumps(
                {"present": True, "meta": meta, "meta_line": meta_raw},
                sort_keys=True,
            )
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
