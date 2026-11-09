"""Actual transport metadata must not change a command's known disposition."""

import json
import subprocess

import github_api_observer as observer
import switch_review


def test_shared_reader_observes_requests_without_replaying_or_leaking(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("LANE_API_EVENTS", str(events))
    monkeypatch.delenv("ORCH_API_LANE", raising=False)
    secret = "ghp_" + "privatecredential" * 3
    trace = (
        "> GET /rate_limit?private=query HTTP/1.1\n"
        f"> Authorization: token {secret}\nprivate body\n"
        "< HTTP/2.0 503 Service Unavailable\n"
        "< X-Ratelimit-Resource: core\n< X-Ratelimit-Remaining: 4010\n"
        "> GET /rate_limit HTTP/1.1\n< HTTP/2.0 200 OK\n"
        "< X-Ratelimit-Resource: core\n< X-Ratelimit-Remaining: 4009\n"
    )
    calls = []

    def completed(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"resources":{}}', trace)

    with monkeypatch.context() as patch:
        patch.setattr(observer.subprocess, "run", completed)
        assert switch_review._gh_call(["api", "rate_limit"]) == (True, '{"resources":{}}', "")
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(calls) == 1 and calls[0][1]["env"]["GH_DEBUG"] == "api"
    assert [(r["http_status"], r["remaining"]) for r in rows] == [(503, 4010), (200, 4009)]
    assert all(r["lane"] == "other" and r["component"] == "shared-gh-reader" for r in rows)
    assert all(r["callsite"]["file"] == "test_github_api_observer.py" for r in rows)
    assert secret not in events.read_text() and "private" not in events.read_text()

    with monkeypatch.context() as patch:
        patch.setenv("LANE_API_EVENTS", str(tmp_path / "query.jsonl"))
        patch.setattr(
            observer.subprocess,
            "run",
            lambda args, **kwargs: subprocess.CompletedProcess(
                args,
                0,
                '{"data":{"rateLimit":{"cost":7}}}',
                "> POST /graphql HTTP/1.1\n< HTTP/2.0 200 OK\n< X-Ratelimit-Resource: graphql\n",
            ),
        )
        assert switch_review._gh_call(["api", "graphql"])[0]
    query = json.loads((tmp_path / "query.jsonl").read_text())
    assert query["graphql_cost"] == 7 and query["resource"] == "graphql"

    with monkeypatch.context() as patch:
        patch.setenv("LANE_API_EVENTS", str(tmp_path / "private-query.jsonl"))
        patch.setattr(observer.subprocess, "run", completed)
        assert switch_review._gh_call(["api", "/rate_limit?credential=" + secret])[0]
    private_query = (tmp_path / "private-query.jsonl").read_text()
    assert secret not in private_query and "credential=" not in private_query
    assert (
        observer.operation_name(
            ["api", "https://user:password@api.github.com/rate_limit?credential=" + secret]
        )
        == "api https://api.github.com/rate_limit"
    )

    # A recording failure cannot turn a confirmed operation into a retry.
    with monkeypatch.context() as patch:
        patch.setenv("LANE_API_EVENTS", str(tmp_path))
        patch.setattr(observer.subprocess, "run", completed)
        before = len(calls)
        assert switch_review._gh_call(["api", "rate_limit"])[0]
        assert len(calls) == before + 1

    timeout = subprocess.TimeoutExpired("gh", 1, output=b"partial", stderr=trace.encode())

    def timed_out(*args, **kwargs):
        raise timeout

    with monkeypatch.context() as patch:
        patch.setenv("LANE_API_EVENTS", str(tmp_path / "timeout.jsonl"))
        patch.setattr(observer.subprocess, "run", timed_out)
        ok, output, reason = switch_review._gh_call(["api", "graphql", "-f", "query=" + secret])
        assert not ok and output == "" and "timed out" in reason
        assert secret not in reason
        assert secret not in timeout.stderr
        timeout = subprocess.TimeoutExpired("gh", 1, output=b"partial", stderr=trace.encode())
        ok, output, reason = switch_review._gh_call(["api", "/rate_limit?credential=" + secret])
        assert not ok and output == "" and "timed out" in reason
        assert secret not in reason and "credential=" not in reason
    assert secret not in timeout.stderr
    timed_rows = [
        json.loads(line) for line in (tmp_path / "timeout.jsonl").read_text().splitlines()
    ]
    assert all(r["coverage"] == "partial" for r in timed_rows)
