"""Hermetic regression coverage for the ranked #846 telemetry/shadow targets."""

from __future__ import annotations

import io
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from test_consumer_sync_shadow import entry, stable_hash, valid_handoff, valid_plan

import capabilities
import consumer_sync_shadow
import langsmith_direct
import redirect_sweep
import watch


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep #846 tests off the live ledger and away from process/network side effects."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(redirect_sweep, "HANDOFF", tmp_path / "handoff")
    attempted: list[object] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("issue #846 coverage test started an external process or network call")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    yield
    assert attempted == []


class _Response:
    def __init__(self, payload: object):
        self.payload = (
            b""
            if payload is None
            else json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        )

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
        raise HTTPError(request.full_url, 403, "no", {}, io.BytesIO(b"forbidden-detail"))

    monkeypatch.setattr(langsmith_direct.urlrequest, "urlopen", bad_open)
    with pytest.raises(RuntimeError, match=r"LangSmith HTTP 403 for GET /nope: forbidden-detail"):
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


def test_langsmith_key_client_host_parse_and_print(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    cred = tmp_path / "langsmith_api_key"
    monkeypatch.setattr(langsmith_direct, "CRED_PATH", cred)
    assert langsmith_direct.load_api_key() is None
    cred.write_text("  from-file  \n")
    assert langsmith_direct.load_api_key() == "from-file"
    monkeypatch.setenv("LANGSMITH_API_KEY", " from-env ")
    assert langsmith_direct.load_api_key() == "from-env"

    monkeypatch.setenv("LANGSMITH_ENDPOINT", ' "https://api.custom.example/" ')
    assert langsmith_direct._api_url() == "https://api.custom.example"
    assert (
        langsmith_direct._host_url("https://beta.api.smith.langchain.com")
        == "https://beta.smith.langchain.com"
    )
    assert langsmith_direct._host_url("https://api.other.test") == "https://other.test"
    assert langsmith_direct._host_url("https://plain.example") == "https://plain.example"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert langsmith_direct._parse_dt(now) is now
    assert langsmith_direct._parse_dt("") is None
    assert langsmith_direct._parse_dt(None) is None

    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    cred.write_text("")
    with pytest.raises(RuntimeError, match="no LANGSMITH_API_KEY"):
        langsmith_direct._make_client()

    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "langsmith" or name.startswith("langsmith."):
            raise ModuleNotFoundError("No module named 'langsmith'", name="langsmith")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    client = langsmith_direct._make_client()
    assert isinstance(client, langsmith_direct._StdlibLangSmithClient)

    def other_missing(name, *args, **kwargs):
        if name == "langsmith" or name.startswith("langsmith."):
            raise ModuleNotFoundError(
                "No module named 'langsmith._internal'", name="langsmith._internal"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", other_missing)
    with pytest.raises(ModuleNotFoundError, match="langsmith._internal"):
        langsmith_direct._make_client()

    run = langsmith_direct._StdlibRun({"id": "only-id", "app_path": ""}, host_url="https://h")
    assert run.trace_id == "only-id" and run.url is None

    langsmith_direct._print(
        {
            "dry_run": True,
            "records_built": 1,
            "with_cost": 1,
            "errors": ["proj: boom"],
            "ingest": {"matched_records": 0, "written_cost_records": 0, "written_trace_records": 0},
        },
        as_json=False,
    )
    out = capsys.readouterr().out
    assert "would write" in out and "error: proj: boom" in out

    with pytest.raises(SystemExit):
        langsmith_direct.main([])


def test_langsmith_stdlib_pagination_empty_and_project_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_open(request, timeout):
        if request.full_url.endswith("/sessions?limit=1&name=missing&include_stats=false"):
            return _Response([])
        if request.method == "GET" and "/sessions" in request.full_url:
            return _Response([{"id": "pid"}])
        if b'"cursor"' in (request.data or b""):
            return _Response({"runs": [{"id": "r2"}], "cursors": {}})
        if request.full_url.endswith("/runs/query"):
            return _Response(
                {
                    "runs": [{"id": "r1", "app_path": "/o/r1"}],
                    "cursors": {"next": "page-2"},
                }
            )
        return _Response(b"")

    client = langsmith_direct._StdlibLangSmithClient(
        api_key="k", api_url="https://api.example.test"
    )
    monkeypatch.setattr(langsmith_direct.urlrequest, "urlopen", fake_open)
    with pytest.raises(RuntimeError, match="Project missing not found"):
        client._project_id("missing")
    assert client._request_json("GET", "/empty") is None
    pages = list(client.list_runs(project_name="ok"))
    assert [run.id for run in pages] == ["r1", "r2"]

    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *a, **k: ["not-a-dict"],
    )
    assert list(client.list_runs(project_name="ok")) == []


def test_langsmith_run_to_record_and_fetch_and_ingest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert langsmith_direct.run_to_record(SimpleNamespace(extra={}, id="x")) is None
    assert (
        langsmith_direct.run_to_record(
            SimpleNamespace(
                extra={"metadata": {"repo": "o/r", "operation": "worker"}},
                id="x",
                total_cost=None,
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                start_time=None,
                end_time=None,
                url=None,
                run_type="llm",
                trace_id=None,
            )
        )
        is None
    )
    # Contradicting operation_role is dropped (caught ValueError → None).
    assert (
        langsmith_direct.run_to_record(
            SimpleNamespace(
                extra={
                    "metadata": {
                        "repo": "o/r",
                        "operation": "implement",
                        "operation_role": "verifier",
                        "pr_number": "9",
                    }
                },
                id="bad",
                run_type="llm",
            )
        )
        is None
    )

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    rich = langsmith_direct.run_to_record(
        SimpleNamespace(
            id="rid",
            run_type="llm",
            total_cost=0.25,
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
            start_time=t0,
            end_time=t1,
            url="https://smith/r",
            trace_id="tid",
            extra={
                "metadata": {
                    "repo": "owner/repo",
                    "pr_number": "3",
                    "operation": "implement",
                    "agent": "Codex",
                    "model": "gpt-x",
                    "resolved_model": "gpt-x-resolved",
                    "ls_provider": "openai",
                    "attempt": 2,
                    "profile_id": "p1",
                    "fallback_reason": "none",
                }
            },
        )
    )
    assert rich is not None
    assert rich["github_pr"] == "owner/repo#3"
    assert rich["agent"] == "codex"
    assert rich["cost_usd"] == 0.25
    assert rich["tokens_in"] == 10 and rich["tokens_out"] == 4
    assert rich["latency_ms"] == 2000.0
    assert rich["provider"] == "openai"
    assert rich["attempt_ordinal"] == 2
    assert rich["trace_url"] == "https://smith/r"
    assert rich["resolved_model"] == "gpt-x-resolved"

    class OkClient:
        def list_runs(self, **kwargs):
            return iter(
                [
                    SimpleNamespace(
                        id="a",
                        run_type="llm",
                        total_cost=1.0,
                        prompt_tokens=1,
                        completion_tokens=1,
                        total_tokens=2,
                        start_time=t0,
                        end_time=t1,
                        url=None,
                        trace_id="t",
                        extra={
                            "metadata": {
                                "repo": "owner/repo",
                                "issue_number": "1",
                                "operation": "worker",
                            }
                        },
                    )
                ]
            )

    ingested: list[object] = []

    def fake_ingest(files, **kwargs):
        ingested.append((files, kwargs))
        return {
            "records_read": 1,
            "matched_records": 0,
            "written_cost_records": 0,
            "written_trace_records": 0,
        }

    monkeypatch.setattr(langsmith_direct.langsmith_pull, "ingest_files", fake_ingest)
    out = tmp_path / "out"
    summary = langsmith_direct.fetch_and_ingest(
        client=OkClient(), projects=["p"], since_hours=None, dry_run=True, output_dir=out
    )
    assert summary["records_built"] == 1 and summary["with_cost"] == 1
    assert Path(summary["combined"]).is_file()
    assert ingested and ingested[0][1]["source"] == "langsmith"

    class EmptyClient:
        def list_runs(self, **kwargs):
            return iter([])

    empty = langsmith_direct.fetch_and_ingest(
        client=EmptyClient(), projects=["p"], since_hours=None, dry_run=True, output_dir=out
    )
    assert empty["records_built"] == 0
    assert empty["ingest"]["records_read"] == 0


def test_watch_and_redirect_sweep_hermetic_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_watch_heartbeat = watch._capability_heartbeat
    monkeypatch.setattr(watch, "_capability_heartbeat", lambda *args: None)
    report = watch.classify_lane(log=tmp_path / "missing.log", now=1, credit=True)
    assert report["state"] == "missing" and report["a2a_state"] == "unknown"
    assert watch.a2a_state("stalled", "waiting for approval") == "input-required"
    assert (
        watch._action_for_signals("progress", [], {}, {"severity": "high", "findings": []})
        == "inspect"
    )
    assert watch._is_git_worktree(None) is False
    assert watch._is_git_worktree("") is False
    monkeypatch.setattr(watch, "_is_git_worktree", lambda wt: False)
    assert watch.git_signals(tmp_path, "HEAD")["git_available"] is False
    assert watch.changed_paths(tmp_path, "HEAD") == []

    monkeypatch.setattr(
        watch,
        "changed_paths",
        lambda *a, **k: [
            "Orchestrator/testgen_gate.py",
            ".github/workflows/ci.yml",
            "a.py",
            "b.py",
            "c.py",
            "d.py",
            "e.py",
            "f.py",
            "g.py",
            "h.py",
            "i.py",
            "j.py",
            "k.py",
        ],
    )
    drift = watch.drift_signals(
        tmp_path,
        None,
        expected_paths=["docs/"],
        task_type="testgen",
        max_files=5,
        max_top_level_dirs=2,
    )
    kinds = {f["kind"] for f in drift["findings"]}
    assert "forbidden_task_paths" in kinds
    assert "high_risk_paths" in kinds
    assert "broad_churn" in kinds
    assert (
        watch._action_for_signals("progress", [], {}, drift) == "inspect"
        or watch.recommended_action("progress", []) == "wait"
    )
    assert watch.recommended_action("running", []) == "wait"

    non_git = tmp_path / "plain"
    non_git.mkdir()
    (non_git / "nested").mkdir()
    target = non_git / "nested" / "file.txt"
    target.write_text("x")
    monkeypatch.setattr(watch, "_is_git_worktree", lambda wt: False)
    age = watch.worktree_activity_age(non_git, now=target.stat().st_mtime + 10)
    assert age == pytest.approx(10.0, abs=1.0)

    # Git-status path: skip missing status paths and swallow per-path OSError.
    monkeypatch.setattr(watch, "_is_git_worktree", lambda wt: True)

    def fake_git(wt, args):
        return SimpleNamespace(returncode=0, stdout=" M gone.txt\n?? nested/file.txt\n", stderr="")

    monkeypatch.setattr(watch, "_git", fake_git)
    # gone.txt missing → continue; nested/file.txt ages via the git-status branch.
    git_age = watch.worktree_activity_age(non_git, now=target.stat().st_mtime + 5)
    assert git_age == pytest.approx(5.0, abs=1.0)
    present = non_git / "gone.txt"
    present.write_text("y")
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "gone.txt":
            raise OSError("race")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert watch.worktree_activity_age(non_git, now=target.stat().st_mtime + 5) == pytest.approx(
        5.0, abs=1.0
    )
    monkeypatch.setattr(Path, "stat", real_stat)
    monkeypatch.setattr(watch, "_is_git_worktree", lambda wt: False)

    real_stat = Path.stat

    def boom_stat(self, *args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(Path, "stat", boom_stat)
    assert watch.worktree_activity_age(non_git, now=1.0) is None
    monkeypatch.setattr(Path, "stat", real_stat)

    real_rglob = Path.rglob

    def boom_rglob(self, pattern):
        raise OSError("walk failed")

    monkeypatch.setattr(Path, "rglob", boom_rglob)
    assert watch.worktree_activity_age(non_git, now=1.0) is None
    monkeypatch.setattr(Path, "rglob", real_rglob)

    missing_wt = tmp_path / "no-such-worktree"
    monkeypatch.setattr(watch, "pid_alive", lambda pid: True)
    monkeypatch.setattr(watch, "read_log_tail", lambda *a, **k: "")
    monkeypatch.setattr(watch, "hints_from_log", lambda *a, **k: [])
    monkeypatch.setattr(
        watch,
        "git_signals",
        lambda *a, **k: {
            "status_short": "",
            "uncommitted_diff_stat": "(none)",
            "committed_diff_stat": "(none)",
            "has_worktree_changes": False,
            "git_available": False,
        },
    )
    monkeypatch.setattr(
        watch, "drift_signals", lambda *a, **k: {"severity": "none", "findings": []}
    )
    monkeypatch.setattr(watch, "worktree_activity_age", lambda *a, **k: None)
    classified = watch.classify_lane(log=None, worktree=str(missing_wt), pid=1, now=1, credit=False)
    assert any("worktree not found" in e for e in classified["errors"])

    monkeypatch.setattr(watch, "_capability_heartbeat", real_watch_heartbeat)
    monkeypatch.setattr(
        capabilities,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    watch._capability_heartbeat()
    redirect_sweep._capability_heartbeat()

    real_sweep = redirect_sweep.sweep
    monkeypatch.setattr(
        redirect_sweep,
        "sweep",
        lambda **kw: {
            "active_claim_count": 1,
            "watched_count": 0,
            "actionable_count": 1,
            "unwatchable_count": 0,
            "actionable": [
                {
                    "target": "o/r#1",
                    "agent": "codex",
                    "state": "stalled",
                    "recommended_action": "redirect",
                    "policy_decision": {"action": "redirect"},
                }
            ],
            "shadow_recording": {
                "recorded_count": 1,
                "attempted_count": 2,
                "corpus": str(tmp_path / "c.jsonl"),
            },
        },
    )
    assert redirect_sweep.main([]) == 0
    printed = capsys.readouterr().out
    assert "shadow_recording recorded=1" in printed
    monkeypatch.setattr(redirect_sweep, "sweep", real_sweep)

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
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
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

    # content_matches / content_differs / invalid inputs
    content = stable_hash("content", ".github/workflows/new.yml")
    matched = consumer_sync_shadow.classify_shadow_drift(
        plan,
        repository="owner/repo",
        observed_targets={".github/workflows/new.yml": content},
    )
    by_target = {row["target"]: row for row in matched["proposals"]}
    assert by_target[".github/workflows/new.yml"]["reason"] == "content_matches"
    differing = consumer_sync_shadow.classify_shadow_drift(
        plan,
        repository="owner/repo",
        observed_targets={".github/workflows/new.yml": stable_hash("other", "x")},
    )
    assert {row["target"]: row["reason"] for row in differing["proposals"]}[
        ".github/workflows/new.yml"
    ] == "content_differs"
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="invalid_repository"):
        consumer_sync_shadow.classify_shadow_drift(plan, repository="bad", observed_targets={})
    with pytest.raises(
        consumer_sync_shadow.ConsumerSyncShadowError, match="invalid_observed_targets"
    ):
        consumer_sync_shadow.classify_shadow_drift(
            plan, repository="owner/repo", observed_targets={"../x": "sha256:" + "a" * 64}
        )

    # handoff / plan / result rejection matrices
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_shadow_handoff(["nope"], plan=plan)
    assert "invalid_shadow_handoff_fields" in caught.value.reasons
    bad_handoff = valid_handoff(plan)
    bad_handoff["schema"] = "other"
    bad_handoff["capability_id"] = "x"
    bad_handoff["manifest_sha256"] = "sha256:" + "0" * 64
    bad_handoff["entry_count"] = 0
    bad_handoff["plan_filename"] = "other.json"
    bad_handoff["run_ref"] = "token:secret"
    bad_handoff["supervision_mode"] = "live"
    bad_handoff["effect_allowlist"] = ["create"]
    bad_handoff["kill_switch"] = "OTHER=1"
    bad_handoff["consumer"] = "other"
    bad_handoff["handoff_id"] = "sha256:" + "1" * 64
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_shadow_handoff(bad_handoff, plan=plan)
    reasons = set(caught.value.reasons)
    assert "unsupported_shadow_handoff_schema" in reasons
    assert "secret_like_shadow_handoff_run_ref" in reasons
    assert "shadow_handoff_consumer_mismatch" in reasons

    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="plan_not_object"):
        consumer_sync_shadow.validate_consumer_sync_plan("nope")
    broken = valid_plan()
    broken["schema"] = "nope"
    broken["manifest_sha256"] = "bad"
    broken["entries"] = "nope"
    broken["removals"] = "nope"
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_consumer_sync_plan(broken)
    assert "unsupported_plan_schema" in caught.value.reasons
    assert "entries_not_array" in caught.value.reasons

    bad_entry_plan = valid_plan()
    bad_entry_plan["entries"][0]["section"] = "  "
    bad_entry_plan["entries"][0]["sync_mode"] = "overwrite"
    bad_entry_plan["entries"][0]["is_directory"] = "yes"
    bad_entry_plan["entries"][0]["skip_repos"] = [1]
    bad_entry_plan["entries"][0]["skip_reasons"] = {}
    bad_entry_plan["entries"][0]["template_sync"] = "fuzzy"
    bad_entry_plan["entries"][0]["delivery"] = "symlink"
    bad_entry_plan["entries"][0]["content_sha256"] = "bad"
    bad_entry_plan["entries"][0]["source"] = "../escape"
    # recompute will fail many checks; also add bad removal
    bad_entry_plan["removals"] = [
        {"target": "../x", "description": "", "effect_fingerprint": "bad"}
    ]
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_consumer_sync_plan(bad_entry_plan)
    assert any("unsafe_source" in r or "invalid_section" in r for r in caught.value.reasons)

    good = consumer_sync_shadow.classify_shadow_drift(
        plan, repository="owner/repo", observed_targets={}
    )
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="result_not_object"):
        consumer_sync_shadow.validate_shadow_result([])
    spoof = dict(good)
    spoof["extra"] = 1
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_shadow_result(spoof)
    assert "invalid_result_fields" in caught.value.reasons
    spoof = dict(good)
    spoof["schema"] = "x"
    spoof["capability_id"] = "x"
    spoof["plan_id"] = "bad"
    spoof["repository"] = "Owner/Repo"
    spoof["mode"] = "write"
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_shadow_result(spoof)
    assert "result_repository_not_lowercase" in caught.value.reasons
    spoof = dict(good)
    spoof["proposals"] = "nope"
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_shadow_result(spoof)
    assert "proposals_not_array" in caught.value.reasons
    spoof = dict(good)
    bad_prop = dict(spoof["proposals"][0])
    bad_prop["action"] = "explode"
    bad_prop["reason"] = "nope"
    bad_prop["target"] = "../x"
    bad_prop["source_effect_fingerprint"] = "bad"
    bad_prop["effect_fingerprint"] = "bad"
    bad_prop["observed_sha256"] = "bad"
    bad_prop["desired_sha256"] = "bad"
    spoof["proposals"] = [bad_prop]
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError) as caught:
        consumer_sync_shadow.validate_shadow_result(spoof)
    assert any("unallowlisted_shadow_action" in r for r in caught.value.reasons)

    # record phase / supervision gates + dashboard blockers + CLI dashboard
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="invalid_supervision"):
        consumer_sync_shadow.record_shadow_result(
            good, ledger_path=tmp_path / "l.json", supervision_mode="live"
        )
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="invalid_phase_id"):
        consumer_sync_shadow.record_shadow_result(
            good, ledger_path=tmp_path / "l.json", phase_id="bad phase"
        )
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="importer"):
        consumer_sync_shadow.record_shadow_result(
            good,
            ledger_path=tmp_path / "l.json",
            supervision_mode="human-on-exception",
            phase_id="not-importer",
        )
    ledger = tmp_path / "caps.json"
    receipt = consumer_sync_shadow.record_shadow_result(
        good,
        ledger_path=ledger,
        timestamp=100,
        supervision_mode="human-on-exception",
        phase_id="importer-pilot",
    )
    assert receipt["mutated"] is True
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="evidence_artifact_ref"):
        consumer_sync_shadow.record_shadow_result(
            good,
            ledger_path=ledger,
            timestamp=101,
            phase_id="importer-pilot",
            evidence_artifact_ref="missing-phase-ref",
            event_ref=f"{good['result_id']}:importer-pilot",
        )
    with pytest.raises(consumer_sync_shadow.ConsumerSyncShadowError, match="event_ref_missing"):
        consumer_sync_shadow.record_shadow_result(
            good,
            ledger_path=ledger,
            timestamp=102,
            phase_id="importer-pilot",
            evidence_artifact_ref=f"consumer-sync-shadow-importer-pilot:{good['result_id'].split(':',1)[1]}",
            event_ref="github-actions:owner/repo:1",
        )

    # Seed enough distinct effects + a counterexample harm and strip probes/kill/status.
    # Write raw JSON so save() validation does not reject the deliberately incomplete row.
    raw = json.loads(ledger.read_text())
    caps = raw["capabilities"] if "capabilities" in raw else raw
    cap = caps[consumer_sync_shadow.CAPABILITY_ID]
    for i in range(6):
        cap.setdefault("event_history", []).append(
            {
                "type": "outcome",
                "ref": f"effect:seed-{i}",
                "metadata": {"supervision_mode": "unattended"},
            }
        )
    cap["event_history"].append({"type": "failure", "ref": "counterexample:harm-1"})
    cap["activation_evidence"] = {}
    cap["kill_switch"] = ""
    cap["status"] = "active"
    cap["expiry"] = 10**12
    ledger.write_text(json.dumps(raw))
    dash = consumer_sync_shadow.promotion_dashboard(ledger_path=ledger, now=200)
    blockers = set(dash["promotion_blockers"])
    assert "harm_ratio_exceeds_limit" in blockers
    assert "rollback_probe_missing" in blockers
    assert "kill_switch_missing" in blockers
    assert "not_in_shadow_state" in blockers

    assert consumer_sync_shadow.main(["dashboard", "--ledger", str(ledger)]) == 0
    assert "promotion_blockers" in capsys.readouterr().out

    # classify --record path (already-registered capability)
    observed_path.write_text("{}")
    assert (
        consumer_sync_shadow.main(
            [
                "classify",
                "--plan",
                str(plan_path),
                "--observed",
                str(observed_path),
                "--repository",
                "owner/repo",
                "--record",
                "--ledger",
                str(ledger),
            ]
        )
        == 0
    )
    assert "receipt" in json.loads(capsys.readouterr().out)

    # unused import guard for entry helper (identity recompute uses it in other modules)
    assert entry(".github/workflows/x.yml")["target"].endswith("x.yml")
