"""Hermetic lifecycle coverage for repo-knowledge managed rules."""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

import dispatcher
import gh_capacity
import repo_knowledge


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """These helper tests may only use private state and in-process work."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("helper coverage attempted external process, provider, or network work")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(gh_capacity, "gh_run", forbidden)
    monkeypatch.setattr(dispatcher, "delegate", forbidden)
    monkeypatch.setattr(dispatcher, "offload", forbidden)
    yield
    assert calls == []


def _manifest() -> dict:
    return {
        "repo": "owner/repo",
        "section": "validation",
        "text": "Run the narrow test before merging.",
        "rule_id": "rule-1",
        "content_hash": "sha256:abc",
        "selector": {"task_types": ["implement"], "lanes": ["opener"]},
        "current_refs": [{"path": "README.md", "symbol": "check"}],
        "lifecycle": {"expires_at": "2027-01-01", "predecessor": None, "rollback": "remove"},
        "evidence_refs": ["issue#1"],
    }


def test_current_refs_reject_unsafe_and_stale_references(tmp_path) -> None:
    (tmp_path / "README.md").write_text("def check(): pass\n")
    result = repo_knowledge.validate_current_refs(
        tmp_path,
        [
            {"path": "README.md", "symbol": "check"},
            {"path": "../outside.md"},
            {"path": "README.md", "symbol": "missing"},
            {"unexpected": "shape"},
        ],
    )
    assert result["valid"] is False
    assert result["refs"] == [{"path": "README.md", "symbol": "check"}]
    assert result["errors"] == [
        "unsafe current path: ../outside.md",
        "stale current symbol: README.md:missing",
        "current_refs[3] must contain path and optional symbol",
    ]


def test_managed_rule_and_bundle_apply_preview_duplicate_and_removal(tmp_path) -> None:
    registry = tmp_path / "registry.json"
    manifest = _manifest()
    assert repo_knowledge.install_managed_rule(manifest, path=registry) == {
        "repo": "owner/repo",
        "section": "validation",
        "rule_id": "rule-1",
        "preview": True,
        "applied": False,
        "already_present": False,
    }
    applied = repo_knowledge.install_managed_rule(manifest, path=registry, apply=True)
    assert applied["applied"] is True
    duplicate = repo_knowledge.install_managed_rule(manifest, path=registry, apply=True)
    assert duplicate["already_present"] is True and duplicate["applied"] is False
    assert repo_knowledge.remove_managed_rule("rule-1", repo="owner/repo", path=registry) == {
        "repo": "owner/repo",
        "rule_id": "rule-1",
        "found": True,
        "removed": False,
        "preview": True,
    }
    assert (
        repo_knowledge.remove_managed_rule("rule-1", repo="owner/repo", path=registry, apply=True)[
            "removed"
        ]
        is True
    )
    stored_registry = json.loads(registry.read_text())
    assert all(
        item.get("rule_id") != "rule-1"
        for item in stored_registry["repos"]["owner/repo"]["validation"]
    )

    bundle = tmp_path / "bundle.json"
    assert repo_knowledge.update_capability_bundle(bundle, manifest, apply=True)["written"] is True
    stored = json.loads(bundle.read_text())
    assert stored["orchestrator_repo_playbook_rules"]["rule-1"]["repo"] == "owner/repo"
    assert repo_knowledge.update_capability_bundle(bundle, manifest)["changed"] is False
    assert (
        repo_knowledge.update_capability_bundle(bundle, manifest, remove=True, apply=True)[
            "written"
        ]
        is True
    )
