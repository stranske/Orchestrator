"""Offline request-contract and accounting regressions for SmithDB migration."""

from datetime import datetime, timezone
import pytest
import langsmith_direct as direct


def test_v2_projection_window_and_cursor(monkeypatch):
    client = direct._StdlibLangSmithClient(api_key="synthetic")
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
    assert [
        r.id for r in client.list_runs(project_name="project", start_time=since)
    ] == ["1", "2"]
    body = requests[0][2]
    assert requests[0][:2] == ("POST", "/api/v2/runs/query")
    assert body["project_ids"] == ["project-id"]
    assert body["min_start_time"] == since.isoformat()
    assert body["selects"] == [f.upper() for f in direct.RUN_SELECT_FIELDS]
    assert body["page_size"] == 100
    assert requests[1][2]["cursor"] == "next"
    assert not {"session", "start_time", "select", "limit"} & body.keys()


def test_all_time_is_not_silently_last_day(monkeypatch):
    client = direct._StdlibLangSmithClient(api_key="synthetic")
    seen = {}

    def request(*args, body):
        seen.update(body)
        return {"items": []}

    monkeypatch.setattr(client, "_request_json", request)
    assert list(client.list_runs()) == []
    assert seen["min_start_time"] == "1970-01-01T00:00:00+00:00"


def test_repeated_cursor_is_reported(monkeypatch):
    client = direct._StdlibLangSmithClient(api_key="synthetic")
    monkeypatch.setattr(
        client, "_request_json", lambda *a, **k: {"items": [], "next_cursor": "repeat"}
    )
    with pytest.raises(RuntimeError, match="repeated cursor"):
        list(client.list_runs())


def test_uppercase_run_type_preserves_operation_and_own_usage():
    run = direct._StdlibRun(
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
    record = direct.run_to_record(run)
    assert record["operation"] == "llm"
    assert record["cost_usd"] == 0.3
    assert record["tokens_in"] == 20 and record["tokens_out"] == 5
    # A parent without own usage must not manufacture child-aggregate charges.
    parent = direct._StdlibRun(
        {
            "id": "parent",
            "run_type": "CHAIN",
            "extra": {"metadata": {"repo": "o/r", "issue_number": "1"}},
        },
        host_url="https://smith.langchain.com",
    )
    assert "cost_usd" not in direct.run_to_record(parent)
