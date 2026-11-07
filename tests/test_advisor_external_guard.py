"""Self-checks for the capability-advisor process and network guard.

Expected attempts are the deliberate calls in this module. An attempt recorded outside
``expected()`` is unexpected, and closing the guard fails on it even when the code under
test caught the guard's exception. ``external_guard_direct`` skips the suite fixture so
the restoration check can see the unpatched entry points.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

import advisor_external_guard
from advisor_external_guard import (
    AUDIT_EVENTS,
    PATCH_TARGETS,
    ExternalOperation,
    ExternalOperationGuard,
    audit_is_armed,
    dispatch_audit,
    in_advisor_suite,
)

_REAL_POPEN = subprocess.Popen
_REAL_RUN = subprocess.run
_REAL_CONNECT = socket.socket.connect

_CALLS = {
    "subprocess.Popen": lambda: subprocess.Popen(["/nonexistent-orch-guard"]),
    "subprocess.run": lambda: subprocess.run(["/nonexistent-orch-guard"]),
    "subprocess.call": lambda: subprocess.call(["/nonexistent-orch-guard"]),
    "subprocess.check_call": lambda: subprocess.check_call(["/nonexistent-orch-guard"]),
    "subprocess.check_output": lambda: subprocess.check_output(["/nonexistent-orch-guard"]),
    "os.system": lambda: os.system("/nonexistent-orch-guard"),
    "socket.create_connection": lambda: socket.create_connection(("127.0.0.1", 9)),
    "socket.getaddrinfo": lambda: socket.getaddrinfo("example.invalid", 9),
    "socket.gethostbyname": lambda: socket.gethostbyname("example.invalid"),
    "socket.connect": lambda: socket.socket().connect(("127.0.0.1", 9)),
    "socket.connect_ex": lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
    "urllib.request.urlopen": lambda: urllib.request.urlopen("http://127.0.0.1:9"),
}


def test_guard_scope_is_the_capability_advisor_suite():
    """The suite is the advisor tests, except the one probe that must start a process."""
    assert in_advisor_suite("test_capability_advisor_coverage.py")
    assert in_advisor_suite("test_advisor_ranking_failure.py")
    assert in_advisor_suite("test_advisor_external_guard.py")
    assert not in_advisor_suite("test_advisor_hermetic.py")
    assert not in_advisor_suite("test_tick.py")
    labels = [label for _owner, _name, label in PATCH_TARGETS]
    assert "subprocess.Popen" in labels
    assert "socket.connect" in labels
    assert "urllib.request.urlopen" in labels
    assert "os.exec" in AUDIT_EVENTS
    assert "http.client.connect" in AUDIT_EVENTS
    assert "urllib.Request" not in AUDIT_EVENTS
    assert set(_CALLS) == set(labels)


def test_guard_module_is_in_exactly_one_ci_shard():
    """CI rejects a collected module that the shard plan never assigns.

    ``test_shard_lists_partition_every_collected_test_exactly_once`` is that check.
    This module was collected and absent from ``config/test-shards.json``, so the
    guard self-checks never reached a shard.
    """
    repo = Path(__file__).resolve().parents[1]
    plan = json.loads((repo / "config" / "test-shards.json").read_text(encoding="utf-8"))
    name = "tests/test_advisor_external_guard.py"
    hits = [shard for shard in plan["shards"] if name in shard]
    assert len(hits) == 1
    assert hits[0].count(name) == 1
    assert plan["untimed_modules"].count(name) == 1


def test_suite_guard_patches_the_import_time_entry_points(advisor_external_guard):
    """While a suite test runs, the guard is holding the real entry points."""
    assert advisor_external_guard.saved("subprocess.Popen") is _REAL_POPEN
    assert advisor_external_guard.saved("subprocess.run") is _REAL_RUN
    assert advisor_external_guard.saved("socket.connect") is _REAL_CONNECT
    assert audit_is_armed()


def test_process_and_network_operations_are_blocked_and_recorded(advisor_external_guard):
    """Each patched entry point records one attempt and raises before the real call."""
    with advisor_external_guard.expected():
        for _owner, _name, label in PATCH_TARGETS:
            before = len(advisor_external_guard.attempts)
            with pytest.raises(ExternalOperation, match=rf"^{re.escape(label)}$"):
                _CALLS[label]()
            recorded = advisor_external_guard.attempts[before:]
            assert [attempt["operation"] for attempt in recorded] == [label]
            assert recorded[0]["via"] == "patch"
            assert recorded[0]["expected"] is True
    assert advisor_external_guard.unexpected() == []


def test_expected_self_checks_are_not_unexpected_attempts(advisor_external_guard):
    """A deliberate self-check is marked expected. Anything else stays unexpected."""
    with advisor_external_guard.expected():
        with pytest.raises(ExternalOperation):
            subprocess.Popen(["/nonexistent-orch-guard"])
    assert [attempt["operation"] for attempt in advisor_external_guard.attempts] == [
        "subprocess.Popen"
    ]
    assert advisor_external_guard.attempts[0]["expected"] is True
    assert advisor_external_guard.unexpected() == []


def test_swallowed_attempt_fails_through_the_record_while_expected_attempts_do_not():
    """Catching the guard exception does not clear the attempt that was recorded first."""
    before = subprocess.Popen
    with pytest.raises(
        AssertionError,
        match=r"^unexpected external-operation attempts: subprocess\.run$",
    ):
        with ExternalOperationGuard() as guard:
            with guard.expected():
                try:
                    subprocess.Popen(["/nonexistent-orch-guard"])
                except ExternalOperation:
                    pass
            try:
                subprocess.run(["/nonexistent-orch-guard"])
            except ExternalOperation:
                pass
            assert [attempt["expected"] for attempt in guard.attempts] == [True, False]
            assert len(guard.attempts) == 2
    assert subprocess.Popen is before


def test_prebound_popen_records_via_the_audit_hook_before_starting(advisor_external_guard):
    """A Popen alias captured before the patch still records, and does not start."""
    assert subprocess.Popen is not _REAL_POPEN
    with advisor_external_guard.expected():
        before = len(advisor_external_guard.attempts)
        with pytest.raises(ExternalOperation, match=r"^subprocess\.Popen$"):
            _REAL_POPEN(["/nonexistent-orch-guard"])
        recorded = advisor_external_guard.attempts[before:]
    assert recorded == [{"operation": "subprocess.Popen", "via": "audit", "expected": True}]
    assert advisor_external_guard.unexpected() == []


def test_test_monkeypatch_undo_does_not_remove_the_guard(advisor_external_guard, monkeypatch):
    """The guard's MonkeyPatch is not the test's, so undo() cannot disarm it."""
    blocker = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: None)
    assert subprocess.Popen is not blocker
    monkeypatch.undo()
    assert subprocess.Popen is blocker
    with advisor_external_guard.expected():
        with pytest.raises(ExternalOperation, match=r"^subprocess\.Popen$"):
            subprocess.Popen(["/nonexistent-orch-guard"])


@pytest.mark.external_guard_direct
def test_close_restores_patched_callables_and_disarms_the_audit_hook():
    """Closing restores the callables this guard replaced and stops the audit hook."""
    assert subprocess.Popen is _REAL_POPEN
    assert socket.socket.connect is _REAL_CONNECT
    with ExternalOperationGuard() as guard:
        assert subprocess.Popen is not _REAL_POPEN
        assert socket.socket.connect is not _REAL_CONNECT
        assert audit_is_armed()
        with guard.expected():
            with pytest.raises(ExternalOperation, match=r"^subprocess\.Popen$"):
                subprocess.Popen(["/nonexistent-orch-guard"])
            with pytest.raises(ExternalOperation, match=r"^socket\.connect$"):
                socket.socket().connect(("127.0.0.1", 9))
        count = len(guard.attempts)
    assert subprocess.Popen is _REAL_POPEN
    assert socket.socket.connect is _REAL_CONNECT
    assert not audit_is_armed()
    dispatch_audit("subprocess.Popen", ())
    assert len(guard.attempts) == count
