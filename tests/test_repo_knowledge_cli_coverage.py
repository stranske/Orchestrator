"""Hermetic public-CLI routing coverage for repo knowledge."""

import socket
import subprocess

import pytest

import repo_knowledge


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("CLI coverage test attempted an external process or network connection")

    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    monkeypatch.setattr(repo_knowledge, "_capability_heartbeat", lambda: None)
    yield
    assert attempted == []


def test_main_exports_agent_rules_and_requires_complete_apply_arguments(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(repo_knowledge, "export_agents_md", lambda repo, **kwargs: f"export:{repo}")
    assert repo_knowledge.main(["--export-agents-md", "owner/repo", "--max-lines", "4"]) == 0
    assert capsys.readouterr().out == "export:owner/repo"

    assert repo_knowledge.main(["--export-agents-md"]) == 2
    assert "requires owner/repo" in capsys.readouterr().err
    assert repo_knowledge.main(["--export-agents-md", "owner/repo", "--apply"]) == 2
    assert "requires --repo-path" in capsys.readouterr().err

    seen = {}

    def update(path, **kwargs):
        seen.update(path=path, **kwargs)
        return {"updated": True}

    monkeypatch.setattr(repo_knowledge, "update_agents_md", update)
    assert (
        repo_knowledge.main(
            ["--export-agents-md", "owner/repo", "--repo-path", str(tmp_path), "--apply", "--json"]
        )
        == 0
    )
    assert seen == {"path": tmp_path, "repo": "owner/repo", "apply": True, "max_lines": 30}
    assert '"updated": true' in capsys.readouterr().out


def test_main_exports_all_registered_repos(monkeypatch, capsys):
    monkeypatch.setattr(repo_knowledge, "load", lambda: {"repos": {"z/repo": {}, "a/repo": {}}})
    monkeypatch.setattr(
        repo_knowledge, "export_agents_md", lambda repo, **kwargs: "" if repo == "z/repo" else "A"
    )
    assert repo_knowledge.main(["--export-all-agents-md", "--json", "--max-lines", "2"]) == 0
    assert capsys.readouterr().out.strip() == '{\n  "a/repo": "A"\n}'


def test_main_validates_agent_exports_and_preserves_status(monkeypatch, capsys, tmp_path):
    seen = {}

    def validate(path, **kwargs):
        seen.update(path=path, **kwargs)
        return {"ok": False}

    monkeypatch.setattr(repo_knowledge, "validate_agents_md_export", validate)
    assert (
        repo_knowledge.main(
            ["--validate-agents-md", str(tmp_path), "--repo", "owner/repo", "--json"]
        )
        == 1
    )
    assert seen == {"path": tmp_path, "repo": "owner/repo", "max_lines": 30}
    assert '"ok": false' in capsys.readouterr().out


def test_main_suggests_from_snapshot(monkeypatch, capsys, tmp_path):
    snapshot = tmp_path / "snapshot.json"
    monkeypatch.setattr(repo_knowledge, "suggest_from_snapshot", lambda path: [{"path": str(path)}])
    assert repo_knowledge.main(["--suggest-from-snapshot", str(snapshot)]) == 0
    assert str(snapshot) in capsys.readouterr().out


def test_main_searches_with_all_optional_filters(monkeypatch, capsys, tmp_path):
    seen = {}

    def search(target, **kwargs):
        seen.update(target=target, **kwargs)
        return [{"ok": True}]

    monkeypatch.setattr(repo_knowledge, "search_repo_memory", search)
    assert repo_knowledge.main(["--search"]) == 2
    assert "requires owner/repo" in capsys.readouterr().err
    assert (
        repo_knowledge.main(
            [
                "--search",
                "owner/repo#7",
                "--query",
                "lint",
                "--task-type",
                "repair",
                "--lane",
                "opener",
                "--max",
                "3",
                "--snapshot-json",
                str(tmp_path / "snapshot.json"),
                "--no-runs",
            ]
        )
        == 0
    )
    assert seen == {
        "target": "owner/repo#7",
        "query": "lint",
        "task_type": "repair",
        "lane": "opener",
        "max_results": 3,
        "snapshot_path": tmp_path / "snapshot.json",
        "include_runs": False,
    }
    assert '"ok": true' in capsys.readouterr().out


def test_main_audits_scopes_in_machine_and_human_formats(monkeypatch, capsys):
    monkeypatch.setattr(
        repo_knowledge,
        "scope_audit",
        lambda: {
            "clean": False,
            "scoped_items": 2,
            "invariant_scoped_count": 1,
            "invariant_scoped": [
                {
                    "repo": "owner/repo",
                    "section": "gotchas",
                    "invariant_signal": "black",
                    "scope": {},
                    "text": "x",
                }
            ],
        },
    )
    assert repo_knowledge.main(["--audit-scopes", "--json"]) == 1
    assert '"clean": false' in capsys.readouterr().out
    assert repo_knowledge.main(["--audit-scopes"]) == 1
    assert "scoped items: 2" in capsys.readouterr().out


def test_main_suggests_from_docs_with_requested_sections(monkeypatch, capsys, tmp_path):
    seen = {}

    def suggest(path, **kwargs):
        seen.update(path=path, **kwargs)
        return [{"section": "Validation"}]

    monkeypatch.setattr(repo_knowledge, "suggest_from_docs", suggest)
    assert (
        repo_knowledge.main(
            [
                "--suggest-from-docs",
                str(tmp_path),
                "--repo",
                "owner/repo",
                "--max",
                "2",
                "--include-root-docs",
                "--section",
                "Validation",
                "--section",
                "Gotchas",
            ]
        )
        == 0
    )
    assert seen == {
        "path": tmp_path,
        "repo": "owner/repo",
        "max_per_repo": 2,
        "include_root_docs": True,
        "sections": ["Validation", "Gotchas"],
    }
    assert '"section": "Validation"' in capsys.readouterr().out
