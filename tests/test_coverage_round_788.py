"""Regression coverage for the two next-ranked coverage targets (#788).

Both modules publish an offline ``--selftest`` contract, but pytest did not execute it.  Running
that contract here makes its executable safety and attribution/readiness invariants part of the
ordinary suite, without reaching the live ledger, GitHub, or an agent runtime.
"""

from __future__ import annotations

import subprocess

import capability_outcome_bridge as bridge
import issue_readiness


def _unexpected_process(*args, **kwargs):
    raise AssertionError(f"selftest attempted to start a real process: {args!r} {kwargs!r}")


def test_capability_outcome_bridge_offline_selftest(monkeypatch, tmp_path):
    """The bridge's advertised offline selftest stays runnable without external processes."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(subprocess, "run", _unexpected_process)
    monkeypatch.setattr(subprocess, "Popen", _unexpected_process)

    bridge._selftest()


def test_issue_readiness_offline_selftest(monkeypatch, tmp_path):
    """Readiness's advertised offline selftest stays runnable without external processes."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(subprocess, "run", _unexpected_process)
    monkeypatch.setattr(subprocess, "Popen", _unexpected_process)

    issue_readiness._selftest()
