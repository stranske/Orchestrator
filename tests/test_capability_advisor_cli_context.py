"""Hermetic CLI-context coverage for the capability advisor."""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

import capability_advisor as advisor


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append(args)
        raise AssertionError("CLI tests must not start processes or network calls")

    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


def test_cli_preserves_explicit_context_and_overrides_its_pr_with_the_cli_value(
    monkeypatch, capsys
):
    """A CLI PR is the authoritative PR fact, while the rest of context remains intact."""
    seen = {}

    def fake_advise(task, **kwargs):
        """Capture the CLI contract without consulting a capability ledger."""
        seen["task"] = task
        seen.update(kwargs)
        return {"useful": False, "capabilities": []}

    monkeypatch.setattr(advisor, "advise", fake_advise)

    assert (
        advisor.main(
            [
                "--repository",
                "stranske/Orchestrator",
                "--lane",
                "closer",
                "--surface",
                "closer-lane",
                "--repo-path",
                "/tmp/consumer",
                "--context",
                json.dumps({"labels": ["agent:retry"], "pr": 41}),
                "--pr",
                "42",
                "--json",
                "review",
                "this",
                "PR",
            ]
        )
        == 0
    )

    assert seen == {
        "task": "review this PR",
        "repository": "stranske/Orchestrator",
        "lane": "closer",
        "surface": "closer-lane",
        "repo_path": "/tmp/consumer",
        "context": {"labels": ["agent:retry"], "pr": 42},
    }
    assert json.loads(capsys.readouterr().out) == {"useful": False, "capabilities": []}
