"""Hermetic regression tests for advisor selftest cleanup and prerequisite gates."""

from __future__ import annotations

import socket
import subprocess

import pytest

import capability_advisor as advisor


@pytest.fixture(autouse=True)
def isolated_runtime_and_external_calls(monkeypatch, tmp_path):
    """These selftests must never consult a live ledger or start a process."""
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("advisor selftest regression attempted external I/O")

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


@pytest.mark.parametrize(
    ("surface", "selftest"),
    [
        ("t-howto", advisor._selftest_how_to_use),
        ("t-concept", advisor._selftest_how_to_use),
        ("t-contra", advisor._selftest_contraindications),
        ("t-surface", advisor._selftest_bindings),
        ("t-precond", advisor._selftest_preconditions),
    ],
)
def test_advisor_selftests_restore_existing_surface_bindings(monkeypatch, surface, selftest):
    """A selftest must restore a pre-existing binding instead of deleting it."""
    original = {"existing-capability": "must survive the isolated probe"}
    monkeypatch.setitem(advisor.SURFACE_BINDINGS, surface, original)

    selftest()

    assert advisor.SURFACE_BINDINGS[surface] == original


def test_front_door_reports_missing_ledger_rows_and_returns(monkeypatch):
    """Missing prerequisites are reported instead of exercising a live ledger."""
    reported = []
    monkeypatch.setattr(advisor.env_prereq, "runnable", lambda gaps, _check: False)
    monkeypatch.setattr(
        advisor.env_prereq,
        "report_gaps",
        lambda name, gaps: reported.append((name, gaps)),
    )

    assert advisor._selftest_front_door() is None
    assert reported == [("capability_advisor.py front-door", [])]
