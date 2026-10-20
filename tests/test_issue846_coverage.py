"""Hermetic regression coverage for the ranked #846 telemetry/shadow targets."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest
from test_consumer_sync_shadow import valid_handoff, valid_plan

import consumer_sync_shadow
import langsmith_direct
import redirect_sweep
import watch


class _Response:
    def __init__(self, payload: object):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class _Run:
    id = "run-1"
    trace_id = "trace-1"
    run_type = "llm"
    total_cost = 0
    prompt_tokens = None
    completion_tokens = None
    total_tokens = 12
    start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    url = None
    extra = {"metadata": {"repo": "owner/repo", "issue_number": "7", "operation": "worker"}}


def test_langsmith_direct_http_fallback_and_record_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_open(request, timeout):
        calls.append((request.full_url, request.method, request.data, timeout))
        if request.full_url.endswith("/sessions?limit=1&name=project&include_stats=false"):
            return _Response([{"id": "project-id"}])
        return _Response(
            {
                "runs": [
                    {
                        "id": "r1",
                        "trace_id": "t1",
                        "app_path": "/runs/r1",
                        "start_time": "2026-01-01T00:00:00Z",
                        "end_time": "2026-01-01T00:00:01Z",
                    }
                ],
                "cursors": {},
            }
        )

    monkeypatch.setattr(langsmith_direct.urlrequest, "urlopen", fake_open)
    client = langsmith_direct._StdlibLangSmithClient(
        api_key="test", api_url="https://api.example.test"
    )
    runs = list(
        client.list_runs(
            project_name="project", start_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
    )
    assert runs[0].url == "https://example.test/runs/r1"
    assert len(calls) == 2 and calls[1][1] == "POST"
    assert langsmith_direct._parse_dt("bad") is None
    assert (
        langsmith_direct._host_url("https://api.smith.langchain.com")
        == "https://smith.langchain.com"
    )
    assert langsmith_direct._ref_num(" 12 ") == "12"
    assert langsmith_direct._ref_num("12x") is None
    record = langsmith_direct.run_to_record(_Run())
    assert record and record["github_issue"] == "owner/repo#7" and record["total_tokens"] == 12

    def bad_open(request, timeout):
        raise HTTPError(request.full_url, 403, "no", {}, None)

    monkeypatch.setattr(langsmith_direct.urlrequest, "urlopen", bad_open)
    with pytest.raises(RuntimeError, match="LangSmith HTTP 403"):
        client._request_json("GET", "/nope")


def test_langsmith_direct_fetch_errors_and_cli_modes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class BadClient:
        def list_runs(self, **kwargs):
            raise RuntimeError("offline")

    records, errors = langsmith_direct.fetch_records(
        client=BadClient(), projects=["a"], since_hours=1
    )
    assert records == [] and errors == ["a: offline"]
    monkeypatch.setattr(
        langsmith_direct,
        "fetch_and_ingest",
        lambda **kw: {
            "dry_run": True,
            "records_built": 0,
            "with_cost": 0,
            "errors": [],
            "ingest": {},
        },
    )
    assert langsmith_direct.main(["--dry-run", "--json", "--project", "p", "--all-time"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_watch_and_redirect_sweep_hermetic_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(watch, "_capability_heartbeat", lambda *args: None)
    report = watch.classify_lane(log=tmp_path / "missing.log", now=1, credit=True)
    assert report["state"] == "missing" and report["a2a_state"] == "unknown"
    assert watch.a2a_state("stalled", "waiting for approval") == "input-required"
    assert (
        watch._action_for_signals("progress", [], {}, {"severity": "high", "findings": []})
        == "inspect"
    )
    monkeypatch.setattr(redirect_sweep, "_capability_heartbeat", lambda: None)
    monkeypatch.setattr(
        redirect_sweep.claims,
        "active_claims",
        lambda **kw: {"a#1": {}, "b#2": {"pid": 5, "agent": "x"}},
    )
    monkeypatch.setattr(
        redirect_sweep.watch,
        "classify_lane",
        lambda **kw: {"state": "exited", "recommended_action": "collect"},
    )
    swept = redirect_sweep.sweep(max_reports=1, now=1)
    assert swept["unwatchable_count"] == 1 and swept["truncated_count"] == 1
    assert redirect_sweep._parse_action_set("redirect,invalid") == {"redirect"}


def test_consumer_shadow_cli_validation_and_dashboard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = valid_plan()
    plan_path = tmp_path / "plan.json"
    observed_path = tmp_path / "observed.json"
    handoff_path = tmp_path / "handoff.json"
    plan_path.write_text(json.dumps(plan))
    observed_path.write_text("{}")
    handoff_path.write_text(json.dumps(valid_handoff(plan)))
    assert (
        consumer_sync_shadow.main(
            [
                "classify",
                "--plan",
                str(plan_path),
                "--observed",
                str(observed_path),
                "--repository",
                "OWNER/REPO",
                "--handoff",
                str(handoff_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["repository"] == "owner/repo"
    assert consumer_sync_shadow.validate_shadow_result(result) == result
    result["side_effects_performed"] = ["write"]
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="side_effects"):
        consumer_sync_shadow.validate_shadow_result(result)
    assert (
        consumer_sync_shadow.promotion_dashboard(ledger_path=tmp_path / "none.json")["status"]
        == "no-data"
    )
