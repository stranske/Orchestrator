"""Hermetic unit tests for langsmith_direct v2 transport construction independent of installed SDKs."""

from __future__ import annotations

import importlib.abc
import socket
import subprocess
import sys
import types
from pathlib import Path
from urllib import request as urlrequest

import pytest

import capabilities
import feedback
import langsmith_direct as ld


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()

    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

    monkeypatch.setattr(ld, "CRED_PATH", runtime / "langsmith_api_key")
    monkeypatch.setattr(ld, "DEFAULT_OUTPUT_DIR", runtime / "langsmith-artifacts")
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)

    attempted: list[tuple] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("langsmith_direct tests must not spawn processes or network calls")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urlrequest, "urlopen", forbidden)
    yield runtime
    assert attempted == [], f"unexpected external calls: {attempted}"


def test_make_client_uses_v2_transport_even_when_sdk_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sdk = types.ModuleType("langsmith")

    class FakeSDKClient:
        def __init__(self, *, api_key: str) -> None:
            raise AssertionError("installed legacy SDK must not be instantiated")

    fake_sdk.Client = FakeSDKClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langsmith", fake_sdk)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_sdk_key")

    client = ld._make_client()
    assert isinstance(client, ld._StdlibLangSmithClient)
    assert client.api_key == "lsv2_test_sdk_key"


def test_make_client_uses_credentials_without_loading_broken_sdk(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match="no LANGSMITH_API_KEY"):
        ld._make_client()

    cred = isolated_runtime / "langsmith_api_key"
    cred.write_text("lsv2_file_key\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "langsmith", None)
    fallback = ld._make_client()
    assert isinstance(fallback, ld._StdlibLangSmithClient)
    assert fallback.api_key == "lsv2_file_key"

    class BrokenSDKFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "langsmith":
                raise ModuleNotFoundError("broken nested dependency", name="broken_sdk_dep")
            return None

    monkeypatch.delitem(sys.modules, "langsmith", raising=False)
    monkeypatch.setattr(sys, "meta_path", [BrokenSDKFinder(), *sys.meta_path])
    client = ld._make_client()
    assert isinstance(client, ld._StdlibLangSmithClient)
    assert client.api_key == "lsv2_file_key"
