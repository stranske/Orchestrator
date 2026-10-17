from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

import adapters


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("coverage test attempted an external process or network connection")

    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    monkeypatch.setattr(adapters, "AGENT_RUNTIME", tmp_path / "agent-runtime")
    monkeypatch.setattr(adapters, "_ADVERTISED_MEMO", {})
    yield
    assert attempted == []


def test_env_int_and_codex_sandbox_override(monkeypatch):
    monkeypatch.setenv("NUMBER", "3")
    assert adapters._env_int("NUMBER", 9) == 3
    monkeypatch.setenv("NUMBER", "not-a-number")
    assert adapters._env_int("NUMBER", 9) == 9
    monkeypatch.setenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", "off")
    assert adapters.codex_bypass_inner_sandbox() is False
    monkeypatch.setenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", "yes")
    assert adapters.codex_bypass_inner_sandbox() is True


def test_profile_codex_binary_uses_existing_explicit_file_and_refuses_missing(
    monkeypatch, tmp_path
):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN", binary)
    assert adapters.profile_codex_binary() == str(binary)
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN", tmp_path / "missing")
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN_EXPLICIT", True)
    with pytest.raises(RuntimeError, match="refusing"):
        adapters.profile_codex_binary()


def test_worktree_git_roots_accepts_only_a_real_linked_worktree(monkeypatch, tmp_path):
    workspace, common = tmp_path / "work", tmp_path / "common"
    private = common / "worktrees" / "name"
    (common / "objects").mkdir(parents=True)
    (common / "refs").mkdir()
    (common / "logs").mkdir()
    private.mkdir(parents=True)
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {private}\n")
    (private / "commondir").write_text("../..\n")
    assert adapters.codex_worktree_git_roots(workspace) == [
        str(private),
        str(common / "objects"),
        str(common / "refs"),
        str(common / "logs"),
    ]
    monkeypatch.setenv(adapters.CODEX_GIT_GRANT_DISABLED_ENV, "1")
    assert adapters.codex_worktree_git_roots(workspace) == []
    monkeypatch.delenv(adapters.CODEX_GIT_GRANT_DISABLED_ENV)
    regular = tmp_path / "regular"
    (regular / ".git").mkdir(parents=True)
    assert adapters.codex_worktree_git_roots(regular) == []
    outside = common / "outside" / "name"
    outside.mkdir(parents=True)
    (outside / "commondir").write_text("../..\n")
    (workspace / ".git").write_text(f"gitdir: {outside}\n")
    assert adapters.codex_worktree_git_roots(workspace) == []


def test_catalog_parsers_drop_prose_and_preserve_labels():
    catalog = (
        "Available models\na\tA model\nb - Bee (current)\n-not-an-id - bad\nwith space - bad\n"
    )
    assert adapters.parse_model_catalog(catalog) == ["a", "b"]
    assert adapters.parse_model_catalog_pairs(catalog) == {
        "a model": "a",
        "bee (current)": "b",
        "bee": "b",
    }
    assert adapters._catalog_model_id("auto") is None
    assert adapters._catalog_model_id("gemini-3.8-flash") == "gemini-3.8-flash"


def test_model_id_for_label_prefers_catalog_and_refuses_unknown_when_catalog_read(monkeypatch):
    monkeypatch.setattr(
        adapters,
        "advertised_catalog",
        lambda _agent: {"models": ["real-id"], "pairs": {"human label": "real-id"}},
    )
    assert adapters.model_id_for_label("cursor", "Human Label") == "real-id"
    assert adapters.model_id_for_label("cursor", "unlisted but vendor-shaped 3.8") is None
    monkeypatch.setattr(adapters, "advertised_catalog", lambda _agent: {"models": [], "pairs": {}})
    assert (
        adapters.model_id_for_label("gemini", "Gemini 3.8 Flash (High)") == "gemini-3.8-flash-high"
    )


def test_agy_path_label_and_stream_parsers_are_defensive():
    assert adapters.agy_log_for(None) is None
    assert adapters.agy_log_for("/tmp/run.log") == Path("/tmp/run.agy.log")
    assert adapters.agy_log_for("/tmp/run.agy.log") == Path("/tmp/run.agy.log")
    assert adapters.model_label_from_agy_log('label="first"\nlabel="last"') is None
    log = 'Propagating selected model override to backend: label="first"\nPropagating selected model override to backend: label="last"'
    assert adapters.model_label_from_agy_log(log) == "last"
    stream = "\n".join(
        [
            "junk",
            '{"type":"system","subtype":"init","model":"M","session_id":"s"}',
            '{"type":"result","result":"done"}',
        ]
    )
    assert adapters.observed_model_from_stream(stream) == {
        "model_label": "M",
        "session_id": "s",
        "result_text": "done",
    }


def test_probe_env_and_cache_are_local_and_expiry_aware(monkeypatch, tmp_path):
    env_file = tmp_path / "probe.env"
    env_file.write_text("# comment\nexport TOKEN='secret'\nINVALID\nA=B=C\n")
    monkeypatch.setitem(adapters.MODEL_PROBE_ENV_FILES, "test", env_file)
    assert adapters._probe_env("test")["TOKEN"] == "secret"
    assert adapters._probe_env("test")["A"] == "B=C"
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"ts": 100.0, "models": ["id"], "pairs": {"label": "id"}}))
    assert adapters._cached_catalog("test", 101.0, cache, need_pairs=True) == {
        "models": ["id"],
        "pairs": {"label": "id"},
    }
    assert (
        adapters._cached_catalog(
            "test", 100 + adapters.GEMINI_MODEL_CACHE_TTL_S + 1, cache, need_pairs=False
        )
        is None
    )


def test_probe_switch_workspace_path_and_marker_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_MODEL_PROBE", "false")
    assert adapters._model_probe_enabled() is False
    monkeypatch.setenv("ORCH_MODEL_PROBE", "1")
    assert adapters._model_probe_enabled() is True
    assert adapters.workspace_path(None) == Path.cwd().resolve()
    marker = adapters.done_marker_cmd(
        "run id", tmp_path / "log.txt", "rc", release_rc_var="release"
    )
    assert '"rc_of":"agent"' in marker and "release_rc" in marker and "run id" in marker


def test_read_head_and_iso_epoch_are_safe_for_absent_or_bad_values(tmp_path):
    path = tmp_path / "events"
    path.write_text("abcdef")
    assert adapters._read_head(path, 3) == "abc"
    assert adapters._read_head(tmp_path / "missing") == ""
    assert adapters._iso_to_epoch("2026-10-17T12:00:00Z") is not None
    assert adapters._iso_to_epoch("not-a-date") is None
    assert adapters._iso_to_epoch(3) is None


def test_agent_subprocess_env_for_non_muse_never_mutates_callers():
    env = {"META_API_KEY": "keep-for-non-muse"}
    assert adapters.agent_subprocess_env("codex", env) is env
    assert env == {"META_API_KEY": "keep-for-non-muse"}
