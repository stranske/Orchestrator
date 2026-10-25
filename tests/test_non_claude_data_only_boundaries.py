"""Inspected native controls never activate a candidate transport or writing fallback."""

import json
from pathlib import Path

import pytest

import adapters

CANDIDATES = ("cursor", "vibe", "gemini", "muse", "codex")


@pytest.mark.parametrize("agent", CANDIDATES)
def test_diagnostics_separate_transport_gaps_from_capability(agent):
    report = adapters.non_claude_data_only_boundary(agent)
    assert report["status"] == "unsupported"
    assert report["command_available"] is False
    assert report["native_confinement_qualified"] is False
    assert report["model_suitability"] == "not_assessed"
    assert report["entitlement"] == "unknown"
    assert report["unresolved_controls"]
    assert adapters.data_only_problem(agent) == report["problem"]


@pytest.mark.parametrize("agent", CANDIDATES)
def test_inert_installed_binary_and_environment_cannot_enable_native_transport(
    tmp_path, monkeypatch, agent
):
    marker = tmp_path / "invoked"
    binary = tmp_path / {"cursor": "cursor-agent", "gemini": "agy"}.get(agent, agent)
    binary.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(f"ORCH_DATA_ONLY_{agent.upper()}", "1")
    monkeypatch.setenv("ORCH_DATA_ONLY_QUALIFIED", "1")
    monkeypatch.setenv("ORCH_DATA_ONLY_BUILD_SHA256", "a" * 64)
    monkeypatch.setenv("ORCH_PROFILE_ROLE_QUALIFIED", "1")

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported data-only admission attempted a process or provisioning")

    monkeypatch.setattr(adapters.subprocess, "run", forbidden)
    monkeypatch.setattr(adapters.subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapters, "agent_workspace", forbidden)
    monkeypatch.setattr(adapters, "record_ledger", forbidden)
    with pytest.raises(ValueError, match="data-only tool boundary unsupported"):
        adapters.build_command(
            agent, "", mode="assess", transport="offload", prompt_via_stdin=True, data_only=True
        )
    assert not marker.exists()


@pytest.mark.parametrize("agent", CANDIDATES)
@pytest.mark.parametrize("mode", [None, "full", "cheap", "plan"])
def test_no_mode_downgrade_to_generic_writing_transport(agent, mode):
    assert "explicit assess mode" in adapters.data_only_problem(agent, mode)
    with pytest.raises(ValueError, match="explicit assess mode"):
        adapters.build_command(agent, "hostile tool request", mode=mode, data_only=True)


@pytest.mark.parametrize("agent", CANDIDATES)
def test_diagnostic_mutation_does_not_change_admission(agent):
    report = adapters.non_claude_data_only_boundary(agent)
    report.update(status="supported", command_available=True, native_confinement_qualified=True)
    report["unresolved_controls"].clear()
    assert adapters.data_only_problem(agent)
    assert adapters.non_claude_data_only_boundary(agent)["unresolved_controls"]


def test_claude_cannot_be_routed_through_candidate_diagnostics():
    with pytest.raises(ValueError, match="independent installed-binary"):
        adapters.non_claude_data_only_boundary("claude")


def test_inspected_sources_have_explicit_native_gaps():
    expected = {"cursor": "hook", "vibe": "imports", "gemini": "MCP", "muse": "preset"}
    for agent, word in expected.items():
        assert word in adapters.data_only_problem(agent)


def test_inspected_evidence_is_metadata_and_never_entitlement_or_activation():
    fixture = Path(__file__).parent / "fixtures/non_claude_data_only/installed-controls.json"
    evidence = json.loads(fixture.read_text())
    assert evidence["model_calls"] == 0
    assert evidence["activation"] is False
    assert set(evidence["candidates"]) == {"cursor", "vibe", "gemini", "muse"}
    for agent, row in evidence["candidates"].items():
        assert row["entitlement"] == "unknown"
        assert row["status"] == "unsupported"
        assert row["hashes"] and row["source_refs"]
        assert adapters.non_claude_data_only_boundary(agent)["command_available"] is False
