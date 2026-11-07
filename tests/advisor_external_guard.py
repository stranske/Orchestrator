"""Restorable process and network guard for the capability-advisor suite.

PR #1082 (merged as the closer repair for #647) blocked external operations with a
process-wide ``sys.addaudithook``. The hook records an attempt and then raises, so a
caller that catches the exception still leaves a record the teardown can fail on. Python
does not allow that hook to be removed, so the proposal cannot show that the process's
callables were restored.

This guard keeps the record-before-raise behavior and the alias coverage of that hook.
The hook stays installed for the process and does nothing while no guard is on the stack,
which is the disarmed state. The entry points a test can hold are patched on a private
``MonkeyPatch``, so a test's own ``monkeypatch.undo()`` cannot drop them, and closing the
guard puts those callables back before it fails on an unexpected attempt.

``urllib.Request`` is in the #1082 event list and is not adopted. CPython emits it when a
``Request`` object is constructed, before any connection. That is outside process and
network operations.

``test_advisor_hermetic.py`` is outside the suite scope. It starts a real interpreter to
prove import-time path restoration, and this guard would block that probe.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

# (owner, attribute, record name). The record name is what teardown reports.
PATCH_TARGETS: tuple[tuple[Any, str, str], ...] = (
    (subprocess, "Popen", "subprocess.Popen"),
    (subprocess, "run", "subprocess.run"),
    (subprocess, "call", "subprocess.call"),
    (subprocess, "check_call", "subprocess.check_call"),
    (subprocess, "check_output", "subprocess.check_output"),
    (os, "system", "os.system"),
    (socket, "create_connection", "socket.create_connection"),
    (socket, "getaddrinfo", "socket.getaddrinfo"),
    (socket, "gethostbyname", "socket.gethostbyname"),
    (socket.socket, "connect", "socket.connect"),
    (socket.socket, "connect_ex", "socket.connect_ex"),
    (urllib.request, "urlopen", "urllib.request.urlopen"),
)
# #1082's audit events, minus urllib.Request (see the module docstring).
AUDIT_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "http.client.connect",
    }
)
_EXCLUDED_SUITE_FILES = frozenset({"test_advisor_hermetic.py"})
_STACK: list[ExternalOperationGuard] = []
_HOOK_INSTALLED = False
_SUITE_UNEXPECTED: list[int] | None = None


class ExternalOperation(Exception):
    """Raised after the attempt is recorded. Application code can catch it."""


class ExternalOperationGuard:
    """Patch process and network entry points, record every attempt, then restore."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.active = False
        self._expecting = 0
        self._patch: pytest.MonkeyPatch | None = None
        self._saved: list[tuple[Any, str, Any, str]] = []

    def saved(self, label: str) -> Any:
        """The callable ``label`` had when this guard was opened."""
        for _owner, _name, original, saved_label in self._saved:
            if saved_label == label:
                return original
        raise KeyError(label)

    def unexpected(self) -> list[dict[str, Any]]:
        """Attempts not marked as expected self-checks."""
        return [attempt for attempt in self.attempts if not attempt["expected"]]

    def record(self, operation: str, via: str) -> None:
        """Append the attempt. Call this before raising so a caught exception still counts."""
        self.attempts.append(
            {
                "operation": operation,
                "via": via,
                "expected": self._expecting > 0,
            }
        )

    @contextmanager
    def expected(self) -> Iterator[None]:
        """Mark attempts made in this block as self-checks, not suite failures."""
        self._expecting += 1
        try:
            yield
        finally:
            self._expecting -= 1

    def open(self) -> None:
        if self.active:
            raise RuntimeError("external-operation guard is already open")
        patch = pytest.MonkeyPatch()
        saved: list[tuple[Any, str, Any, str]] = []
        try:
            for owner, name, label in PATCH_TARGETS:
                original = getattr(owner, name)
                saved.append((owner, name, original, label))
                patch.setattr(owner, name, self._blocker(label))
            _push(self)
        except Exception:
            patch.undo()
            raise
        self._patch = patch
        self._saved = saved
        self.active = True

    def close(self, *, tally: bool = False) -> None:
        """Restore patched callables, then fail if an unexpected attempt remains."""
        if not self.active:
            return
        self.active = False
        patch = self._patch
        self._patch = None
        saved = self._saved
        if patch is not None:
            patch.undo()
        _pop(self)
        unrestored = [
            label for owner, name, original, label in saved if getattr(owner, name) is not original
        ]
        pending = self.unexpected()
        if tally:
            _note_suite_close(len(pending))
        if unrestored:
            raise AssertionError("guard left patched state in place: " + ", ".join(unrestored))
        if pending:
            names = ", ".join(attempt["operation"] for attempt in pending)
            raise AssertionError(f"unexpected external-operation attempts: {names}")

    def __enter__(self) -> ExternalOperationGuard:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def _blocker(self, label: str):
        def blocked(*_args: object, **_kwargs: object) -> None:
            self.record(label, "patch")
            raise ExternalOperation(label)

        blocked.__name__ = label.replace(".", "_")
        return blocked


def in_advisor_suite(filename: str) -> bool:
    """True for capability-advisor tests whose process and network calls must be blocked."""
    return (
        filename.startswith("test_capability_advisor_") or filename.startswith("test_advisor_")
    ) and filename not in _EXCLUDED_SUITE_FILES


def audit_is_armed() -> bool:
    """True while a guard is on the stack. The hook itself stays installed."""
    return bool(_STACK)


def dispatch_audit(event: str, args: tuple) -> None:
    """Audit hook. Records on the innermost open guard, then raises.

    A hook Python cannot remove is disarmed by leaving the stack empty: the function
    returns without recording. ``args`` is part of the audit-hook signature.
    """
    del args
    if not _STACK or event not in AUDIT_EVENTS:
        return
    guard = _STACK[-1]
    guard.record(event, "audit")
    raise ExternalOperation(event)


def suite_unexpected_total() -> int | None:
    """How many unexpected attempts suite fixtures observed, or None if none ran."""
    if _SUITE_UNEXPECTED is None:
        return None
    return sum(_SUITE_UNEXPECTED)


def _push(guard: ExternalOperationGuard) -> None:
    global _HOOK_INSTALLED
    _STACK.append(guard)
    if not _HOOK_INSTALLED:
        sys.addaudithook(dispatch_audit)
        _HOOK_INSTALLED = True


def _pop(guard: ExternalOperationGuard) -> None:
    if not _STACK or _STACK[-1] is not guard:
        raise AssertionError("external-operation guard stack was unbalanced")
    _STACK.pop()


def _note_suite_close(count: int) -> None:
    global _SUITE_UNEXPECTED
    if _SUITE_UNEXPECTED is None:
        _SUITE_UNEXPECTED = []
    _SUITE_UNEXPECTED.append(count)
