"""Hermetic unit tests for langsmith_direct v2 transport construction independent of installed SDKs."""

from __future__ import annotations

import importlib.abc
import socket
import subprocess
import sys
import types
from datetime import datetime, timezone
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


def test_v2_projection_window_and_cursor(monkeypatch):
    client = ld._StdlibLangSmithClient(api_key="synthetic")
    monkeypatch.setattr(client, "_project_id", lambda name: "project-id")
    requests = []

    def request(method, path, *, body):
        requests.append((method, path, dict(body)))
        return {
            "items": [{"id": str(len(requests))}],
            "next_cursor": "next" if len(requests) == 1 else None,
        }

    monkeypatch.setattr(client, "_request_json", request)
    since = datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert [r.id for r in client.list_runs(project_name="project", start_time=since)] == ["1", "2"]
    body = requests[0][2]
    assert requests[0][:2] == ("POST", "/api/v2/runs/query")
    assert body["project_ids"] == ["project-id"]
    assert body["min_start_time"] == since.isoformat()
    assert body["selects"] == [f.upper() for f in ld.RUN_SELECT_FIELDS]
    assert body["page_size"] == 100
    assert requests[1][2]["cursor"] == "next"
    assert not {"session", "start_time", "select", "limit"} & body.keys()


def test_all_time_is_not_silently_last_day(monkeypatch):
    client = ld._StdlibLangSmithClient(api_key="synthetic")
    seen = {}

    def request(*args, body):
        seen.update(body)
        return {"items": []}

    monkeypatch.setattr(client, "_request_json", request)
    monkeypatch.setattr(client, "_project_id", lambda name: "pid")
    assert list(client.list_runs(project_name="project")) == []
    assert seen["min_start_time"] == "1970-01-01T00:00:00+00:00"


def test_repeated_cursor_is_reported(monkeypatch):
    client = ld._StdlibLangSmithClient(api_key="synthetic")
    monkeypatch.setattr(
        client, "_request_json", lambda *a, **k: {"items": [], "next_cursor": "repeat"}
    )
    monkeypatch.setattr(client, "_project_id", lambda name: "pid")
    with pytest.raises(RuntimeError, match="repeated cursor"):
        list(client.list_runs(project_name="project"))


def test_uppercase_run_type_preserves_operation_and_own_usage():
    run = ld._StdlibRun(
        {
            "id": "own-run",
            "run_type": "LLM",
            "total_cost": 0.3,
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "extra": {"metadata": {"repo": "o/r", "issue_number": "1"}},
        },
        host_url="https://smith.langchain.com",
    )
    record = ld.run_to_record(run)
    assert record["operation"] == "llm"
    assert record["cost_usd"] == 0.3
    assert record["tokens_in"] == 20 and record["tokens_out"] == 5
    # A parent without own usage must not manufacture child-aggregate charges.
    parent = ld._StdlibRun(
        {
            "id": "parent",
            "run_type": "CHAIN",
            "extra": {"metadata": {"repo": "o/r", "issue_number": "1"}},
        },
        host_url="https://smith.langchain.com",
    )
    assert "cost_usd" not in ld.run_to_record(parent)


def test_query_requires_project_before_network(monkeypatch):
    client = ld._StdlibLangSmithClient(api_key="synthetic")
    with pytest.raises(ValueError, match="at least one project"):
        list(client.list_runs())


def test_versioned_base_preserves_project_endpoint(monkeypatch):
    import json
    from urllib.parse import urlparse

    calls = []

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    def request(req, timeout):
        calls.append(req.full_url)
        return Response([{"id": "pid"}] if req.method == "GET" else {"items": []})

    monkeypatch.setattr(ld.urlrequest, "urlopen", request)
    client = ld._StdlibLangSmithClient(api_key="synthetic", api_url="https://example.test/api/v1")
    assert list(client.list_runs(project_name="project")) == []
    assert [urlparse(url).path for url in calls] == ["/api/v1/sessions", "/api/v2/runs/query"]


@pytest.mark.parametrize("cursor", [False, 0, [], {}, ""])
def test_false_cursor_is_malformed(monkeypatch, cursor):
    client = ld._StdlibLangSmithClient(api_key="synthetic")
    monkeypatch.setattr(client, "_project_id", lambda name: "pid")
    monkeypatch.setattr(
        client, "_request_json", lambda *a, **kw: {"items": [], "next_cursor": cursor}
    )
    with pytest.raises(RuntimeError, match="invalid or repeated cursor"):
        list(client.list_runs(project_name="project"))


@pytest.mark.parametrize(
    "records,errors,expected", [(0, [], 0), (2, [], 0), (0, ["secret"], 1), (2, ["secret"], 1)]
)
@pytest.mark.parametrize("mode", [[], ["--json"], ["--summary-only"]])
def test_cli_result_and_allowlist(monkeypatch, capsys, records, errors, expected, mode):
    poison = "opaque-password x-api-key: secret ?token=secret TRACE_INPUT TRACE_OUTPUT"

    def fetch(**kwargs):
        print(poison)
        print(poison, file=sys.stderr)
        return {
            "records_built": records,
            "errors": errors,
            "projects": [poison],
            "ingest": {"payload": poison},
        }

    monkeypatch.setattr(ld, "fetch_and_ingest", fetch)
    monkeypatch.setenv("ORCH_CREDENTIAL_REDACTION_DISABLED", "1")
    assert ld.main(["--ingest", *mode]) == expected
    out = capsys.readouterr()
    assert not out.err and len(out.out) < 600
    for marker in ["secret", "opaque-password", "TRACE_INPUT", "TRACE_OUTPUT"]:
        assert marker not in out.out


@pytest.mark.parametrize("mode", [[], ["--json"], ["--summary-only"]])
def test_cli_unexpected_exception_is_safe(monkeypatch, capsys, mode):
    def fail(**kwargs):
        raise RuntimeError("opaque-key x-api-key: private TRACE_INPUT")

    monkeypatch.setattr(ld, "fetch_and_ingest", fail)
    assert ld.main(["--ingest", *mode]) == 1
    out = capsys.readouterr()
    assert not out.err and "opaque-key" not in out.out and "TRACE_INPUT" not in out.out


@pytest.mark.parametrize("failure", ["network", "malformed", "repeat", "false"])
def test_partial_pagination_keeps_ingest_but_fails_cli(monkeypatch, tmp_path, capsys, failure):
    client = ld._StdlibLangSmithClient(api_key="opaque-key")
    monkeypatch.setattr(client, "_project_id", lambda name: "pid")
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {
                "items": [
                    {
                        "id": "r",
                        "run_type": "llm",
                        "total_cost": 1,
                        "extra": {"metadata": {"repo": "o/r", "issue_number": "1"}},
                    }
                ],
                "next_cursor": "next",
            }
        if failure == "network":
            raise RuntimeError("opaque-key TRACE_OUTPUT")
        if failure == "malformed":
            return {"items": "opaque-key"}
        return {"items": [], "next_cursor": "next" if failure == "repeat" else False}

    monkeypatch.setattr(client, "_request_json", request)
    monkeypatch.setattr(ld, "_make_client", lambda: client)
    ingested = []
    monkeypatch.setattr(
        ld.langsmith_pull, "ingest_files", lambda files, **kw: ingested.extend(files) or {}
    )
    fetch = ld.fetch_and_ingest
    monkeypatch.setattr(ld, "fetch_and_ingest", lambda **kw: fetch(**kw, output_dir=tmp_path))
    assert ld.main(["--ingest", "--summary-only"]) == 1
    assert len(ingested) == 1
    assert '"status": "partial"' in capsys.readouterr().out


@pytest.mark.parametrize("mode", [[], ["--json"], ["--summary-only"]])
def test_http_body_secret_not_in_cli(monkeypatch, tmp_path, capsys, mode):
    import io
    from urllib.error import HTTPError

    poison = b"opaque-password x-api-key: private ?token=private TRACE_INPUT TRACE_OUTPUT"

    def request(*args, **kwargs):
        raise HTTPError("https://example.test", 403, "Forbidden", {}, io.BytesIO(poison))

    monkeypatch.setattr(ld.urlrequest, "urlopen", request)
    client = ld._StdlibLangSmithClient(api_key="opaque-password")
    monkeypatch.setattr(client, "_project_id", lambda name: "pid")
    monkeypatch.setattr(ld, "_make_client", lambda: client)
    assert ld.main(["--ingest", *mode]) == 1
    out = capsys.readouterr()
    assert not out.err
    assert all(
        x not in out.out for x in ["opaque-password", "private", "TRACE_INPUT", "TRACE_OUTPUT"]
    )


def test_cli_ingestion_reported_errors_fail(monkeypatch, capsys):
    monkeypatch.setattr(
        ld,
        "fetch_and_ingest",
        lambda **kw: {"records_built": 1, "errors": [], "ingest": {"errors": ["opaque-key"]}},
    )
    assert ld.main(["--ingest", "--json"]) == 1
    out = capsys.readouterr().out
    assert '"ingest_errors": 1' in out and "opaque-key" not in out


def test_safe_ingest_counts_available_and_unavailable():
    assert ld._safe_ingest_counts({}) == {
        "matched_records": None,
        "written_cost_records": None,
        "written_trace_records": None,
        "skipped_records": None,
    }
    assert ld._safe_ingest_counts(
        {
            "matched_records": 3,
            "written_cost_records": 2,
            "written_trace_records": 1,
            "skipped": {"secret bucket": 2, "other": 1},
            "errors": ["secret"],
        }
    ) == {
        "matched_records": 3,
        "written_cost_records": 2,
        "written_trace_records": 1,
        "skipped_records": 3,
    }
    assert ld._safe_ingest_counts(
        {
            "matched_records": "secret",
            "written_cost_records": True,
            "written_trace_records": -1,
            "skipped": {"x": "secret"},
        }
    ) == ld._safe_ingest_counts({})
    assert ld._safe_ingest_counts({"skipped": {}})["skipped_records"] == 0


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("fails", [True, False])
def test_cli_dry_run_is_strict_argument_boolean(monkeypatch, capsys, dry_run, fails):
    import json

    def fetch(**kwargs):
        if fails:
            raise RuntimeError("private")
        return {"records_built": 0, "errors": [], "dry_run": "private"}

    monkeypatch.setattr(ld, "fetch_and_ingest", fetch)
    assert ld.main(["--dry-run" if dry_run else "--ingest", "--json"]) == int(fails)
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is dry_run
