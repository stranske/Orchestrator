"""Agent-neutral local-tool boundary; no formatter, repair or ACT authority.

Deny-default Seatbelt is deliberately distinct from adapters' allow-default
agent write boundary. Unsupported/nested hosts refuse without an unconfined
fallback. The caller owns operation intent, reservation and reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import pwd
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "closer-local-tool-boundary/v1"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MAX_SOURCE_BYTES = 8_000_000


class BoundaryRefused(ValueError):
    """Known local boundary admission refusal; no tool has been submitted."""


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _canonical(value: str | Path, *, directory: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise BoundaryRefused("path must be absolute, physical and unaliased")
    for part in (path, *path.parents):
        if stat.S_ISLNK(part.lstat().st_mode):
            raise BoundaryRefused("symlink ancestry is unsupported")
    info = path.stat()
    if directory != stat.S_ISDIR(info.st_mode):
        raise BoundaryRefused("path kind differs")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise BoundaryRefused("regular single-link files required")
    return path


def _identity(path: Path) -> tuple[int, ...]:
    return _stat_identity(path.stat(follow_symlinks=False))


def _stat_identity(item: os.stat_result) -> tuple[int, ...]:
    return tuple(
        getattr(item, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
        )
    )


def _hash(path: Path) -> str:
    before = _identity(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if _stat_identity(os.fstat(stream.fileno())) != before:
            raise BoundaryRefused("source/binary open identity changed")
        raw = stream.read(MAX_SOURCE_BYTES + 1)
        after_open = _stat_identity(os.fstat(stream.fileno()))
    if len(raw) > MAX_SOURCE_BYTES or before != after_open or before != _identity(path):
        raise BoundaryRefused("source/binary changed or exceeds bound")
    return hashlib.sha256(raw).hexdigest()


def unsupported_reason() -> str | None:
    if platform.system() != "Darwin" or not Path(SANDBOX_EXEC).is_file():
        return "deny-default Seatbelt unavailable on this host"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("APP_SANDBOX_CONTAINER_ID"):
        return "nested sandbox is unsupported"
    return None


@dataclass(frozen=True)
class Boundary:
    workspace: str
    write_paths: tuple[str, ...]
    scratch: str
    runtime_read_roots: tuple[str, ...]
    argv: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int
    operation_id: str
    read_only: bool
    identities: tuple[tuple[str, tuple[int, ...]], ...]
    source_hashes: tuple[tuple[str, str], ...]
    profile: str
    descriptor_sha256: str


def _render_profile(root, private, runtime, writes):
    """Pure deny-default rendering, shared by exact postimage check validation."""
    rules = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
    ]
    rules += [f"(allow file-read* (subpath {_quote(str(p))}))" for p in (root, private, *runtime)]
    rules += [f"(allow file-read* (subpath {_quote(p)}))" for p in ("/usr/lib", "/System/Library")]
    rules += [
        '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
        '(allow file-write-data (literal "/dev/null"))',
    ]
    rules += [f"(allow file-write-data (literal {_quote(str(p))}))" for p in writes]
    rules += [
        f"(allow file-write* (subpath {_quote(str(private))}))",
        f"(deny file-read* file-write* (subpath {_quote(str(root / '.git'))}))",
        "(deny network*)",
    ]
    return "\n".join(rules)


def prepare_boundary(
    *,
    workspace: str | Path,
    write_paths: list[str | Path],
    scratch: str | Path,
    runtime_read_roots: list[str | Path],
    argv: list[str],
    timeout_seconds: int,
    max_output_bytes: int,
    operation_id: str,
    read_only: bool = False,
) -> Boundary:
    """Freeze trusted exact local inputs; does not execute or qualify a tool."""
    root = _canonical(workspace, directory=True)
    private = _canonical(scratch, directory=True)
    if (
        root == Path(root.anchor)
        or private == root
        or private.is_relative_to(root)
        or root.is_relative_to(private)
    ):
        raise BoundaryRefused("workspace and private scratch must be disjoint bounded directories")
    if (
        root.stat().st_uid != os.getuid()
        or private.stat().st_uid != os.getuid()
        or private.stat().st_mode & 0o077
    ):
        raise BoundaryRefused("owned workspace and private 0700 scratch required")
    if root.stat().st_mode & 0o022:
        raise BoundaryRefused("workspace is writable by other accounts")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
        raise BoundaryRefused("explicit bounded timeout required")
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 65536:
        raise BoundaryRefused("explicit bounded output required")
    if (
        type(operation_id) is not str
        or not operation_id
        or len(operation_id) > 256
        or any(ord(c) < 32 for c in operation_id)
    ):
        raise BoundaryRefused("original operation identity required")
    if type(read_only) is not bool:
        raise BoundaryRefused("explicit boolean read-only mode required")
    if read_only:
        if write_paths:
            raise BoundaryRefused("read-only mode forbids workspace write paths")
    elif not 1 <= len(write_paths) <= 5 or len(set(map(str, write_paths))) != len(write_paths):
        raise BoundaryRefused("one to five unique existing write files required")
    writes = tuple(_canonical(p, directory=False) for p in write_paths)
    if any(
        not p.is_relative_to(root)
        or ".git" in p.relative_to(root).parts
        or p.stat().st_uid != os.getuid()
        or p.stat().st_mode & 0o022
        for p in writes
    ):
        raise BoundaryRefused("write files must belong to exact workspace owner")
    if any(private.iterdir()):
        raise BoundaryRefused("private scratch must be empty before original submission")
    if not 1 <= len(runtime_read_roots) <= 8:
        raise BoundaryRefused("explicit bounded runtime roots required")
    runtime = tuple(_canonical(p, directory=True) for p in runtime_read_roots)
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if any(p.is_relative_to(account_home) or account_home.is_relative_to(p) for p in runtime):
        raise BoundaryRefused("runtime cannot expose account home or user project context")
    if any(
        p == Path(p.anchor)
        or root.is_relative_to(p)
        or private.is_relative_to(p)
        or p.is_relative_to(root)
        or p.is_relative_to(private)
        for p in runtime
    ):
        raise BoundaryRefused("runtime roots overlap workspace/scratch or grant global context")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 64
        or any(
            type(a) is not str or not a or len(a.encode()) > 4096 or any(ord(c) < 32 for c in a)
            for a in argv
        )
    ):
        raise BoundaryRefused("bounded explicit argv required")
    executable = _canonical(argv[0], directory=False)
    if (
        not executable.stat().st_mode & 0o111
        or executable in writes
        or executable.is_relative_to(private)
    ):
        raise BoundaryRefused("absolute real non-writable-target executable required")
    if not any(executable.is_relative_to(p) for p in runtime):
        raise BoundaryRefused("executable must be inside explicit readonly runtime")
    paths = (root, private, *writes, *runtime, executable)
    identities = tuple((str(p), _identity(p)) for p in paths)
    import dispatcher

    sources: tuple[Path, ...] = (
        executable,
        Path(__file__).resolve(),
        Path(dispatcher.__file__).resolve(),
    )
    if Path(SANDBOX_EXEC).is_file():
        sources += (_canonical(SANDBOX_EXEC, directory=False),)
    hashes = tuple((str(p), _hash(p)) for p in sources)
    profile = _render_profile(root, private, runtime, writes)
    fields = (
        str(root),
        tuple(map(str, writes)),
        str(private),
        tuple(map(str, runtime)),
        tuple(argv),
        timeout_seconds,
        max_output_bytes,
        operation_id,
        read_only,
        identities,
        hashes,
        profile,
    )
    digest = hashlib.sha256(
        json.dumps([SCHEMA, fields], separators=(",", ":")).encode()
    ).hexdigest()
    return Boundary(*fields, digest)


def run_confined_tool(
    boundary: Boundary, *, starting_callback, started_callback, finished_callback
) -> dict[str, Any]:
    """Use existing observed broker; report transport facts, never effect authority."""

    def refused(reason):
        return {
            "schema": SCHEMA,
            "mode": (
                ("read_only" if boundary.read_only else "write")
                if type(boundary) is Boundary
                else "unknown"
            ),
            "status": "refused",
            "reason": reason,
            "process_started": False,
            "effect_outcome": "not_started",
            "qualified": False,
            "authority": False,
        }

    if reason := unsupported_reason():
        return refused(reason)
    if type(boundary) is not Boundary or not all(
        callable(cb) for cb in (starting_callback, started_callback, finished_callback)
    ):
        return refused("exact boundary and original-operation lifecycle callbacks required")
    try:
        current = prepare_boundary(
            workspace=boundary.workspace,
            write_paths=list(boundary.write_paths),
            scratch=boundary.scratch,
            runtime_read_roots=list(boundary.runtime_read_roots),
            argv=list(boundary.argv),
            timeout_seconds=boundary.timeout_seconds,
            max_output_bytes=boundary.max_output_bytes,
            operation_id=boundary.operation_id,
            read_only=boundary.read_only,
        )
        if current != boundary:
            raise BoundaryRefused("source/paths/binary/boundary changed before submission")
    except (OSError, ValueError, TypeError):
        return refused("fresh boundary revalidation failed")
    import dispatcher

    observation = {
        "operation_id": boundary.operation_id,
        "tool_boundary_sha256": boundary.descriptor_sha256,
        "source": "local-tool-confinement-request/v1",
    }
    actual = {}

    def started(row):
        actual.update(row)
        started_callback(row)

    env = {
        "HOME": boundary.scratch,
        "TMPDIR": boundary.scratch,
        "XDG_CACHE_HOME": boundary.scratch,
        "XDG_CONFIG_HOME": boundary.scratch,
        "BLACK_CACHE_DIR": str(Path(boundary.scratch) / "black-cache"),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = dispatcher.run_observed_offload(
            [SANDBOX_EXEC, "-p", boundary.profile, *boundary.argv],
            callback=started,
            observation=observation,
            cwd=boundary.workspace,
            timeout=boundary.timeout_seconds,
            stdin=subprocess.DEVNULL,
            env=env,
            output_limit=boundary.max_output_bytes,
            starting_callback=starting_callback,
            finished_callback=finished_callback,
        )
    except Exception as error:
        observed = getattr(error, "observation", actual)
        return {
            "schema": SCHEMA,
            "status": "held",
            "mode": "read_only" if boundary.read_only else "write",
            "process_started": bool(observed.get("pid")),
            "effect_outcome": "unknown",
            "error_type": type(error).__name__,
            "process_observation": dispatcher._public_process_observation(observed),
            "qualified": False,
            "authority": False,
        }
    observed = getattr(result, "observation", actual)
    return {
        "schema": SCHEMA,
        "status": "observed" if observed.get("cleanup_terminal") is True else "held",
        "mode": "read_only" if boundary.read_only else "write",
        "process_started": True,
        "tool_started": "unverified_sandbox_wrapper_transport_only",
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "process_observation": dispatcher._public_process_observation(observed),
        "effect_outcome": "unknown_requires_original_operation_reconciliation",
        "boundary_sha256": boundary.descriptor_sha256,
        "qualified": False,
        "authority": False,
    }
