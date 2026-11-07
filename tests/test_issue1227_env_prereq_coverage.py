"""Hermetic pytest coverage for the first-ranked #1227 target."""

from __future__ import annotations

from types import SimpleNamespace

import agent_auth_check
import capabilities
import capability_activation_audit
import env_prereq


def test_env_prereq_selftest_runs_with_an_isolated_runtime(monkeypatch, tmp_path):
    """Exercise the module's real detector contract without consulting local state or spawning git."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(env_prereq.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_auth_check, "HOME", tmp_path)
    monkeypatch.setattr(
        agent_auth_check,
        "CREDENTIAL_FILES",
        {
            agent: (tmp_path / agent / path.name, key)
            for agent, (path, key) in agent_auth_check.CREDENTIAL_FILES.items()
        },
    )
    monkeypatch.setattr(capability_activation_audit, "STATE_DIR", tmp_path)
    monkeypatch.setattr(env_prereq.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")
    monkeypatch.setattr(
        env_prereq.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    env_prereq._selftest()
    assert env_prereq.ledger_legacy_rows_absent().startswith(
        "capability ledger holds no pre-admission-gate (legacy) capability"
    )


def test_ledger_detectors_name_an_unreadable_ledger(monkeypatch):
    """A local-ledger read failure is a named prerequisite, never an uncaught error."""
    monkeypatch.setattr(
        env_prereq,
        "_ledger",
        lambda: (_ for _ in ()).throw(OSError("fixture ledger unavailable")),
    )

    for detector in (
        env_prereq.ledger_rows_absent,
        env_prereq.ledger_version_lineage_absent,
        env_prereq.ledger_invocation_history_absent,
        env_prereq.ledger_rows_not_live,
    ):
        reason = detector("fixture-capability")
        assert reason == "capability ledger unreadable (OSError: fixture ledger unavailable)"
