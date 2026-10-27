"""Borrow only the existing canonical Claude OAuth export; never mint or persist.

Production callers must supply dispatcher.REAL_HOME, which must equal this UID's
OS-account home (not mutable HOME). No environment credential fallback exists.
Only the qualified caller decides whether to invoke this context manager. The
reader does not launch a provider, probe login/status, or change any file/mode.

The yielded Python string is transient: internal references are released on exit,
not securely erased from immutable Python memory. Consumers must not retain,
serialize, hash or log it. Tests replace the private account-home constant with a
synthetic directory; production has no path/UID override or environment switch.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
_FILENAME = ".claude-oauth-token"
MAX_CREDENTIAL_BYTES = 16 * 1024
_ASSIGNMENT = re.compile(
    r"(?:export )?CLAUDE_CODE_OAUTH_TOKEN=(?:([A-Za-z0-9_./+=:-]+)|'([A-Za-z0-9_./+=:-]+)'|\"([A-Za-z0-9_./+=:-]+)\")(?:\n)?",
    re.ASCII,
)


class ClaudeAuthUnavailable(RuntimeError):
    """A sanitized refusal. Never includes credential bytes or a shell command."""


def _literal_assignment(raw: bytearray) -> str:
    # Parsing is deliberately not shell evaluation or shlex expansion. Exactly
    # one documented literal assignment is accepted; every other form is held.
    document: str | None = None
    match: re.Match[str] | None = None
    try:
        try:
            document = raw.decode("ascii")
        except UnicodeError:
            document = ""
        match = _ASSIGNMENT.fullmatch(document)
        if match is None:
            raise ClaudeAuthUnavailable("claude_oauth_literal_assignment_required")
        return next(value for value in match.groups() if value is not None)
    finally:
        document = None
        match = None


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid


def _check_file(value: os.stat_result, uid: int) -> None:
    if not (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == uid
        and stat.S_IMODE(value.st_mode) == 0o600
        and 0 < value.st_size <= MAX_CREDENTIAL_BYTES
    ):
        raise ClaudeAuthUnavailable("claude_oauth_file_identity_or_bounds_invalid")


def _check_directory(value: os.stat_result, uid: int) -> None:
    if not (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == uid
        and not value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ClaudeAuthUnavailable("claude_oauth_directory_identity_or_permissions_invalid")


def _read_existing(real_home: Path) -> bytearray:
    """Read through no-follow directory descriptors, then recheck every join."""
    descriptors: list[int] = []
    links: list[tuple[int, str, os.stat_result]] = []
    raw = bytearray()
    chunk = b""
    try:
        uid = os.getuid()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        home = os.open(real_home, directory_flags)
        descriptors.append(home)
        anchor = os.fstat(home)
        _check_directory(anchor, uid)
        parent = home
        for name in (".codex", "handoff"):
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _check_directory(before, uid)
            child = os.open(name, directory_flags, dir_fd=parent)
            descriptors.append(child)
            if _directory_identity(os.fstat(child)) != _directory_identity(before):
                raise ClaudeAuthUnavailable("claude_oauth_directory_changed")
            links.append((parent, name, before))
            parent = child
        before = os.stat(_FILENAME, dir_fd=parent, follow_symlinks=False)
        _check_file(before, uid)
        descriptor = os.open(_FILENAME, file_flags, dir_fd=parent)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        _check_file(opened, uid)
        if _identity(opened) != _identity(before):
            raise ClaudeAuthUnavailable("claude_oauth_file_changed")
        while len(raw) <= MAX_CREDENTIAL_BYTES:
            chunk = os.read(descriptor, min(4096, MAX_CREDENTIAL_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        current = os.stat(_FILENAME, dir_fd=parent, follow_symlinks=False)
        absolute = (real_home / ".codex" / "handoff" / _FILENAME).lstat()
        if (
            len(raw) > MAX_CREDENTIAL_BYTES
            or len(raw) != before.st_size
            or any(_identity(value) != _identity(before) for value in (after, current, absolute))
            or real_home.resolve() != real_home
            or _directory_identity(real_home.lstat()) != _directory_identity(anchor)
            or any(
                _directory_identity(os.stat(name, dir_fd=fd, follow_symlinks=False))
                != _directory_identity(original)
                for fd, name, original in links
            )
        ):
            raise ClaudeAuthUnavailable("claude_oauth_file_or_directory_changed")
        return raw
    except (OSError, ValueError, TypeError, AttributeError):
        raw[:] = b"\x00" * len(raw)
        raise ClaudeAuthUnavailable("claude_oauth_existing_file_unavailable") from None
    except BaseException:
        raw[:] = b"\x00" * len(raw)
        raise
    finally:
        chunk = b""
        close_error = False
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                close_error = True
        if close_error:
            raw[:] = b"\x00" * len(raw)
            raise ClaudeAuthUnavailable("claude_oauth_descriptor_close_failed")


@contextmanager
def borrow_claude_oauth_token(*, real_home: str | Path) -> Iterator[str]:
    """Yield the literal token only from the canonical existing protected file.

    No path override is accepted. Failure requires the existing owner to resolve
    the exact configured file; it never triggers credential creation or fallback.
    """
    supplied = Path(real_home)
    if supplied != _ACCOUNT_HOME or not supplied.is_absolute() or supplied.resolve() != supplied:
        raise ClaudeAuthUnavailable("claude_oauth_real_home_differs_from_os_account")
    raw: bytearray | None = None
    token: str | None = None
    try:
        raw = _read_existing(supplied)
        token = _literal_assignment(raw)
        raw[:] = b"\x00" * len(raw)
        raw = None
        yield token
    finally:
        token = None
        if raw is not None:
            raw[:] = b"\x00" * len(raw)
        raw = None
