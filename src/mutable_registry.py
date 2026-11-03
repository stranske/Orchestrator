"""Validated JSON state outside executable generations, with serialized atomic writes.

Reads never create directories or lock files. Named registry overrides may read a
legacy generation, but writes there are refused. No migration or repair is implicit.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import paths

Document = Any
Validator = Callable[[Document], Any]
Factory = Callable[[], Document]


class RegistryError(ValueError):
    """Unsafe destination, invalid JSON or an unsupported document schema."""


def _absolute(path: Path | str) -> Path:
    selected = Path(path).expanduser()
    if ".." in selected.parts:
        raise RegistryError("registry path must not contain parent traversal")
    selected = Path(os.path.abspath(selected))
    # These are the two documented macOS root aliases, not arbitrary links.
    for alias, target in (
        (Path("/tmp"), Path("/private/tmp")),
        (Path("/var"), Path("/private/var")),
    ):
        if selected.is_relative_to(alias) and alias.is_symlink():
            if Path(os.path.abspath(alias.parent / os.readlink(alias))) != target:
                raise RegistryError(f"unexpected OS alias: {alias}")
            selected = target / selected.relative_to(alias)
            break
    if selected == Path(selected.anchor):
        raise RegistryError("registry path must name a file")
    return selected


def _immutable(path: Path) -> None:
    mirror = Path.home() / ".codex" / "orchestrator-mirror"
    roots = (Path(paths.REPO_ROOT), Path(paths.MODULE_DIR), mirror)
    for root in roots:
        # Resolve only trusted protected roots, never the candidate destination.
        for protected in (_absolute(root), _absolute(root.resolve())):
            if path.is_relative_to(protected):
                raise RegistryError(f"immutable executable tree: {path}")
    codex = _absolute(mirror.parent)
    if path.is_relative_to(codex):
        first = path.relative_to(codex).parts[0]
        if first.startswith((mirror.name + ".generation-", mirror.name + ".retired-")):
            raise RegistryError(f"immutable executable generation: {path}")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _check_regular(info: os.stat_result, label: str, *, unlinked_snapshot: bool = False) -> None:
    # Atomic replacement can unlink an inode between lstat/open and fstat.
    # Such a data snapshot is still complete and safe to read. A lock inode,
    # unlike a snapshot, must remain linked so every writer locks the same file.
    allowed_links = (0, 1) if unlinked_snapshot else (1,)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink not in allowed_links:
        raise RegistryError(f"{label} must be a regular single-link file")


def _preflight(path: Path, *, writing: bool) -> None:
    if writing:
        _immutable(path)
    # Reject every existing symlink/non-directory ancestor before *any* mkdir.
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise RegistryError(f"symlink registry component: {current}")
        if index == len(path.parts) - 2:
            _check_regular(info, "registry", unlinked_snapshot=True)
            if writing and not info.st_mode & 0o222:
                raise PermissionError(f"registry is read-only: {current}")
        elif not stat.S_ISDIR(info.st_mode):
            raise RegistryError(f"non-directory registry ancestor: {current}")


@contextmanager
def _parent(path: Path, *, create: bool) -> Iterator[int | None]:
    fd = os.open(path.anchor, _directory_flags())
    try:
        for part in path.parts[1:-1]:
            try:
                child = os.open(part, _directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # A concurrent creator still has to pass O_NOFOLLOW.
                child = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _validated(value: Document, validate: Validator) -> Document:
    value = copy.deepcopy(value)
    if validate(value) is False:
        raise RegistryError("unsupported registry schema")
    # Refuse values JSON cannot preserve, before creating any output.
    json.dumps(value, allow_nan=False)
    return value


def _load(fd: int, name: str, validate: Validator) -> tuple[bool, Document]:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return False, None
    with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
        _check_regular(os.fstat(handle.fileno()), "registry", unlinked_snapshot=True)
        try:
            value = json.load(
                handle, parse_constant=_invalid_constant, object_pairs_hook=_unique_object
            )
        except (ValueError, UnicodeError) as exc:
            raise RegistryError("malformed registry; original bytes preserved") from exc
    return True, _validated(value, validate)


def _invalid_constant(token: str) -> Any:
    raise RegistryError(f"non-JSON constant: {token}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RegistryError(f"duplicate registry key: {key}")
        value[key] = item
    return value


def read(path: Path | str, default_factory: Factory, validate: Validator) -> Document:
    """Read validated JSON, or a fresh validated default, without creating anything."""
    selected = _absolute(path)
    _preflight(selected, writing=False)
    with _parent(selected, create=False) as fd:
        exists, value = (False, None) if fd is None else _load(fd, selected.name, validate)
    return value if exists else _validated(default_factory(), validate)


def seed_default(
    path: Path | str,
    legacy_path: Path | str,
    default_factory: Factory,
    validate: Validator,
    *,
    normalize: Callable[[Document], Document] = lambda value: value,
) -> Document:
    """Return a pure seed only if it cannot silently discard legacy operator state.

    Callers apply this guard only without a named registry override. An existing
    runtime store wins: the caller's subsequent read validates that store. This
    function never copies legacy data or creates paths, locks or registry files.
    """
    selected = _absolute(path)
    legacy = _absolute(legacy_path)
    default = _validated(default_factory(), validate)
    if os.path.lexists(selected) or selected == legacy or not os.path.lexists(legacy):
        return default

    def disappeared() -> Document:
        raise RegistryError(f"legacy registry disappeared during inspection: {legacy}")

    prior = read(legacy, disappeared, validate)
    normalized_prior = json.dumps(
        normalize(copy.deepcopy(prior)), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    normalized_default = json.dumps(
        normalize(copy.deepcopy(default)), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if normalized_prior != normalized_default:
        canonical = json.dumps(prior, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        raise RegistryError(
            f"reviewed migration required for legacy registry: {legacy}; "
            f"canonical_json_sha256={digest}"
        )
    return default


@contextmanager
def _locked(fd: int, name: str) -> Iterator[Callable[[], None]]:
    lock_name = name + ".lock"
    try:
        lock_fd = os.open(
            lock_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
    except FileExistsError:
        # Separate exclusive creation from opening an incumbent. Concurrent
        # O_CREAT opens can report ENOENT on macOS even with a linked parent.
        lock_fd = os.open(lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(lock_fd)
        _check_regular(info, "registry lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        def verify() -> None:
            linked = os.stat(lock_name, dir_fd=fd, follow_symlinks=False)
            _check_regular(linked, "registry lock")
            if (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino):
                raise RegistryError("registry lock changed while holding ownership")

        verify()
        yield verify
    finally:
        os.close(lock_fd)


def _replace(fd: int, name: str, value: Document) -> None:
    data = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    temp_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
    )
    try:
        with os.fdopen(temp_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A noncooperating symlink/file swap must never be followed by the writer.
        try:
            _check_regular(
                os.stat(name, dir_fd=fd, follow_symlinks=False),
                "registry",
                unlinked_snapshot=True,
            )
        except FileNotFoundError:
            pass
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _change(
    path: Path | str,
    default_factory: Factory,
    validate: Validator,
    mutate: Callable[[Document], Document],
    *,
    only_missing: bool = False,
) -> Document:
    selected = _absolute(path)
    _preflight(selected, writing=True)
    default = _validated(default_factory(), validate)
    with _parent(selected, create=True) as fd:
        assert fd is not None
        with _locked(fd, selected.name) as verify_lock:
            exists, value = _load(fd, selected.name, validate)
            if only_missing and exists:
                return value
            value = value if exists else default
            replacement = mutate(value)
            value = _validated(value if replacement is None else replacement, validate)
            verify_lock()
            _replace(fd, selected.name, value)
            return copy.deepcopy(value)


def initialize(path: Path | str, default_factory: Factory, validate: Validator) -> Document:
    """Serialize first creation; return an existing document without rewriting it."""
    return _change(path, default_factory, validate, lambda value: value, only_missing=True)


def write(path: Path | str, value: Document, validate: Validator) -> Document:
    """Replace only a missing or schema-valid document; never repair malformed bytes."""
    checked = _validated(value, validate)
    return _change(path, lambda: checked, validate, lambda previous: checked)


def update(
    path: Path | str,
    default_factory: Factory,
    validate: Validator,
    mutate: Callable[[Document], Document],
) -> Document:
    """Run an in-place or replacement mutator under the same lock as read and replace."""
    return _change(path, default_factory, validate, mutate)
