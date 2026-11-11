"""Offline failure and CLI contracts for issue #1235's ranked targets."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import dispatcher
import feedback
import frontend_verify as frontend
import keepalive_supervisor as supervisor
import langsmith_pull as pull
import relearn_report
import runtime_ac
import runtime_ac_panel as panel


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for key in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "feedback.db"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    connections = []
    connect = feedback._conn

    def temporary_connection(**kwargs):
        assert feedback.DB_PATH.is_relative_to(tmp_path)
        connection = connect(**kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(feedback, "_conn", temporary_connection)
    denied = Mock(side_effect=AssertionError("unexpected process or network call"))
    for module, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (socket, "socket"),
        (socket, "create_connection"),
        (os, "system"),
        (dispatcher, "offload"),
        (frontend, "urlopen"),
    ):
        monkeypatch.setattr(module, name, denied)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    yield
    for connection in connections:
        connection.close()
    denied.assert_not_called()


def panel_files(tmp_path):
    spec = runtime_ac._valid_spec()
    gate = runtime_ac.evaluate_results(spec, {"check_results": panel._all_check_results(spec)})
    gate["verification_id"] = "fixture-gate"
    reviews = {"reviews": [{"reviewer": name, "verdict": "PASS"} for name in ("a", "b")]}
    paths = []
    for name, value in (("spec", spec), ("gate", gate), ("reviews", reviews)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(str(path))
    return spec, gate, paths


def test_panel_selects_review_and_rejects_invalid_documents():
    doc = {"reviews": [None, {"reviewer": "other"}, {"reviewer": " a ", "verdict": "PASS"}]}
    assert panel._single_review_from_doc(doc, "a")["verdict"] == "PASS"
    assert panel._single_review_from_doc(doc, "missing") == {"reviewer": "other"}
    assert panel.parse_review_output('[null, {"verdict": "FAIL"}]', "a") == {
        "reviewer": "a",
        "verdict": "FAIL",
    }
    for doc, message in (([], "no review objects"), (42, "must be an object")):
        with pytest.raises(ValueError, match=message):
            panel._single_review_from_doc(doc, "a")
    for doc in (None, {}, {"reviews": "bad"}):
        with pytest.raises(ValueError, match="review JSON must be a list"):
            panel.coerce_reviews(doc)
    with pytest.raises(ValueError, match="first 100000 chars"):
        panel.parse_review_output("x" * panel.JSON_SCAN_LIMIT + '{"verdict":"PASS"}', "a")


def test_panel_json_scan_recovers_after_malformed_brackets():
    output = 'Reviewer notes: [not JSON] {broken} finally {"verdict":"PASS","rationale":"checked"}'
    assert panel.parse_review_output(output, "a") == {
        "reviewer": "a",
        "verdict": "PASS",
        "rationale": "checked",
    }


def test_panel_json_scan_respects_limit_when_review_straddles_boundary(monkeypatch):
    monkeypatch.setattr(panel, "JSON_SCAN_LIMIT", 128)
    review = '{"verdict":"PASS"}'
    prefix = "Reviewer notes: [invalid] "
    padding = "x" * (panel.JSON_SCAN_LIMIT - len(prefix) - len(review))
    output = prefix + padding + review
    assert len(output) == panel.JSON_SCAN_LIMIT
    assert panel.parse_review_output(output, "a") == {"reviewer": "a", "verdict": "PASS"}

    # Moving the closing brace outside the scan must not accept a truncated review.
    with pytest.raises(ValueError, match="no parseable JSON.*in first 128 chars"):
        panel.parse_review_output(prefix + padding + "x" + review, "a")


@pytest.mark.parametrize(
    "gate_verdict,review_verdicts,reason",
    [
        (
            "NEEDS_REVIEW",
            ["PASS", "PASS"],
            "automated runtime AC gate still needs review",
        ),
        (
            "PASS",
            ["FAIL", "NEEDS_REVIEW"],
            "fail review present, but pass threshold was not met",
        ),
        (
            "PASS",
            ["NEEDS_REVIEW", "NEEDS_REVIEW"],
            "panel did not reach a pass threshold",
        ),
    ],
)
def test_panel_keeps_inconclusive_evidence_pending(gate_verdict, review_verdicts, reason):
    reviews = [
        {"reviewer": f"judge-{index}", "verdict": verdict}
        for index, verdict in enumerate(review_verdicts)
    ]
    result = panel.adjudicate_panel(runtime_ac._valid_spec(), {"verdict": gate_verdict}, reviews)
    assert result["verdict"] == "NEEDS_REVIEW"
    assert result["verifier_verdict"] == "NEEDS_REVIEW_RUNTIME_AC"
    assert result["reason"] == reason
    assert result["gate_verdict"] == gate_verdict
    assert result["n_reviews"] == 2
    assert result["n_vetoes"] == 0
    assert [review["verdict"] for review in result["reviews"]] == review_verdicts
    assert result["counts"] == {
        verdict: review_verdicts.count(verdict) for verdict in panel.VALID_PANEL_VERDICTS
    }


def test_panel_dispatch_fails_closed_on_invalid_adapter_results(tmp_path):
    spec, gate, _ = panel_files(tmp_path)
    offload = Mock(side_effect=[None, {"exit": 3, "stderr_tail": "offline"}, {"exit": 4}])
    result = panel.dispatch_reviewers(spec, gate, ["a", "b", "c"], offload_fn=offload)
    assert [r["rationale"] for r in result["review_doc"]["reviews"]] == [
        "reviewer offload returned a non-dict result",
        "offline",
        "reviewer exited 4",
    ]
    assert all(r["verdict"] == "NEEDS_REVIEW" for r in result["review_doc"]["reviews"])
    assert all(not meta["parsed"] for meta in result["dispatches"])
    offload.assert_called()
    with pytest.raises(ValueError, match="invalid runtime AC spec"):
        panel.dispatch_reviewers({}, gate, ["a"], offload_fn=offload)
    assert offload.call_count == 3
    with pytest.raises(ValueError, match="invalid runtime AC spec"):
        panel.adjudicate_panel({}, gate, [])


@pytest.mark.parametrize("mode", ["prompt", "dispatch", "adjudicate"])
def test_panel_cli_reads_evidence_and_records_verdict(mode, tmp_path, monkeypatch, capsys):
    _, _, paths = panel_files(tmp_path)
    spec_path, gate_path, review_path = paths
    args = [f"--{mode}", spec_path, "--gate", gate_path]
    record = Mock(return_value={"recorded": True})
    monkeypatch.setattr(panel, "record_panel_verdict", record)
    adapter = Mock(return_value={"exit": 0, "output": '{"verdict":"PASS"}'})
    monkeypatch.setattr(dispatcher, "offload", adapter)
    if mode == "dispatch":
        args += [
            "--reviewers",
            "a,b",
            "--cwd",
            str(tmp_path),
            "--mode",
            "local",
            "--timeout",
            "7",
            "--isolate",
        ]
    if mode == "adjudicate":
        args += ["--reviews", review_path]
    if mode != "prompt":
        args += ["--record-run-id", "fixture-run"]
    assert panel.main(args) == 0
    output = capsys.readouterr().out
    if mode == "prompt":
        assert '"verification_id": "fixture-gate"' in output
        record.assert_not_called()
        adapter.assert_not_called()
    else:
        result = json.loads(output)
        verdict = result["panel"] if mode == "dispatch" else result
        assert verdict["verdict"] == "PASS"
        assert verdict["feedback"] == {"recorded": True}
        assert record.call_args.args[0] == "fixture-run"
        assert record.call_args.args[1]["verification_id"] == "fixture-gate"
        if mode == "dispatch":
            assert adapter.call_count == 2
            assert adapter.call_args.kwargs == {
                "cwd": str(tmp_path),
                "mode": "local",
                "timeout": 7,
                "isolate": True,
            }


@pytest.mark.parametrize("mode", ["prompt", "dispatch", "adjudicate"])
def test_panel_cli_requires_gate_before_reading_files(mode, capsys):
    with pytest.raises(SystemExit) as error:
        panel.main([f"--{mode}", "missing.json"])
    assert error.value.code == 2
    assert f"--{mode} requires --gate" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "unreadable", "incompatible"])
def test_relearn_falls_back_when_quality_learner_is_incompatible(kind, monkeypatch):
    if kind == "missing":
        monkeypatch.delattr(feedback, "relearn_quality")
    elif kind == "unreadable":
        monkeypatch.setattr(
            relearn_report.inspect, "getsource", Mock(side_effect=OSError("no source"))
        )
    else:
        monkeypatch.setattr(relearn_report.inspect, "getsource", lambda _fn: "legacy_weights")
    name, learner, reason = relearn_report._learner()
    assert name == "feedback.relearn"
    assert learner is feedback.relearn
    assert reason == "binary outcome learner; no compatible relearn_quality was found"


def test_relearn_role_import_failure_and_invalid_window(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "roles", None)
    assert relearn_report._role_priors_from_registry({"implement": {"a": 0.7}}) == {}
    with pytest.raises(SystemExit) as error:
        relearn_report.main(["--window-days", "bad"])
    assert error.value.code == 2
    assert "not an integer: bad" in capsys.readouterr().err
    assert (
        relearn_report.blend_bt_priors(
            {"task": {"a": 0.7}}, bt_fn=Mock(side_effect=RuntimeError("offline"))
        )
        == {}
    )
    assert (
        relearn_report.blend_bt_priors(
            {"task": {"a": 0.7}}, bt_fn=lambda **_kw: {"ready": True, "strengths": {"other": 1.0}}
        )
        == {}
    )


def test_relearn_dry_run_reports_priors_without_writing_weights(monkeypatch, capsys):
    monkeypatch.setattr(relearn_report, "_role_priors_from_registry", lambda _priors: {})
    report = relearn_report.build_report(
        dry_run=True, route_table={"implement": {"agents": [{"agent": "a"}]}}
    )
    assert report["dry_run"] is True
    assert report["would_write_version"] == 1 and report["current_version"] is None
    assert report["tasks"] == [
        {
            "task_type": "implement",
            "prior_order": ["a"],
            "rows": [{"agent": "a", "prior": 0.7, "prior_rank": 1}],
        }
    ]
    assert relearn_report._max_version() == 0
    assert relearn_report._version_exists(None) is False
    monkeypatch.setattr(relearn_report, "build_report", lambda **_kwargs: report)
    assert relearn_report.main(["--dry-run"]) == 0
    assert "No route_weights version was written." in capsys.readouterr().out


def test_supervisor_search_deduplicates_and_limits_without_launching():
    runner = Mock(
        return_value=SimpleNamespace(returncode=0, stdout="o/r#1\no/r#1\no/r#2\n", stderr="")
    )
    assert supervisor.live_targets(owner="o", limit=2, runner=runner) == ["o/r#1", "o/r#2"]
    assert runner.call_count == 1
    command = runner.call_args.args[0]
    assert command[:3] == ["gh", "search", "prs"]
    assert command[command.index("--owner") + 1] == "o"
    runner.return_value = SimpleNamespace(returncode=1, stdout="", stderr="offline")
    with pytest.raises(RuntimeError, match="offline"):
        supervisor.live_targets(runner=runner)


@pytest.mark.parametrize("mode", ["target", "list-live"])
def test_supervisor_cli_prints_plans_with_operator_commands(mode, tmp_path, monkeypatch, capsys):
    planned = {
        "target": "o/r#1",
        "eligible": True,
        "next_step": "operator_inspect",
        "reason": "needs evidence",
        "proposal_command": ["redirect", "--inspect"],
        "stage2_record_command": ["record", "--proposal"],
        "outcome_link_command_template": ["link", "--run"],
    }
    plan = Mock(return_value=planned)
    monkeypatch.setattr(supervisor, "plan_target", plan)
    live = Mock(return_value=["o/r#1"])
    monkeypatch.setattr(supervisor, "live_targets", live)
    args = ["--target", "o/r#1"] if mode == "target" else ["--list-live"]
    args += [
        "--owner",
        "o",
        "--limit",
        "2",
        "--write-report-dir",
        str(tmp_path),
        "--stage2-backend",
        "vibe",
    ]
    assert supervisor.main(args) == 0
    text = capsys.readouterr().out
    assert "stage2_record_command=record --proposal" in text
    assert plan.call_args.args == ("o/r#1",)
    assert plan.call_args.kwargs["report_dir"] == tmp_path
    assert plan.call_args.kwargs["proposal_backend"] == "vibe"
    if mode == "target":
        assert "reason=needs evidence" in text
        assert "proposal_command=redirect --inspect" in text
        assert "outcome_link_command_template=link --run" in text
        live.assert_not_called()
    else:
        assert "post-escalation keepalive targets: 1" in text
        assert "o/r#1: eligible=True next=operator_inspect" in text
        live.assert_called_once_with(owner="o", limit=2)


@pytest.mark.parametrize(
    "args,message",
    [
        ([], "one of --target"),
        (["--limit", "0"], "--limit must be positive"),
        (["--historical-limit", "0"], "--historical-limit must be positive"),
    ],
)
def test_supervisor_cli_rejects_invalid_requests(args, message, capsys):
    with pytest.raises(SystemExit) as error:
        supervisor.main(args)
    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_supervisor_stage2_cli_displays_acquisition_and_refusal(monkeypatch, capsys):
    result = {
        "stage": "stage2_proposal_corpus",
        "status": "needs_proposals",
        "recommendation": "collect",
        "unrecorded_live_candidate_count": 2,
        "lane_refused_live_candidate_count": 1,
        "eligible_live_candidate_count": 3,
        "live_candidate_count": 4,
        "historical_preview": {"would_collect": 5},
        "plans": [],
        "commands": [{"kind": "historical", "command": ["collect", "--preview"]}],
        "lane_refused_live_candidates": [{"target": "o/r#2", "blocks": ["held", "busy"]}],
    }
    acquire = Mock(return_value=result)
    close = Mock(return_value={"closed": 0, "errors": 1, "skips": {"offline": 1}})
    monkeypatch.setattr(supervisor, "stage2_acquisition_plan", acquire)
    monkeypatch.setattr(supervisor, "close_verified_issues", close)
    assert (
        supervisor.main(
            [
                "--stage2-plan",
                "--owner",
                "o",
                "--limit",
                "3",
                "--historical-backend",
                "vibe",
                "--include-calibration",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    for expected in (
        "supervisor closures: 0; errors=1",
        "stage2_status=needs_proposals",
        "recommendation=collect",
        "2 unrecorded / 1 lane-refused / 3 eligible / 4 total historical_would_collect=5",
        "historical_command=collect --preview",
        "lane_refused=o/r#2: held; busy",
    ):
        assert expected in text
    assert acquire.call_args.kwargs["historical_backend"] == "vibe"
    assert acquire.call_args.kwargs["include_calibration"] is True
    close.assert_called_once_with(owner="o", limit=3)


def test_langsmith_parses_bridge_refs_and_rejects_invalid_measurements():
    assert pull._parse_github_ref("#12", default_repo="O/R") == ("o/r", 12)
    for value in (None, "", "garbage", "#1"):
        assert pull._parse_github_ref(value) is None
    assert pull._normalize_repo(None) is None
    for value in (None, "", -1, "bad", {}):
        assert pull._coerce_nonnegative_number(value) is None
    assert pull._first_number({"usage": {"tokens_in": "bad"}}, ("tokens_in",)) == (0.0, False)
    measurements, present = pull._measurements({"total_tokens": 12})
    assert present and measurements["tokens_in"] == 12 and measurements["tokens_out"] == 0
    assert pull._attempt_ordinal({"attempt": "bad"}) is None
    assert pull._record_agent({"agent": " ViBe "}) == "vibe"
    assert pull._durability_label({"operation": "durability", "domain": {"result": "unknown"}}) == (
        None,
        "invalid durability label: unknown",
    )
    assert pull._record_ref({}) == (None, None)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"],
    ids=[
        "numeric-nan",
        "numeric-inf",
        "numeric-negative-inf",
        "string-nan",
        "string-inf",
        "string-negative-inf",
    ],
)
def test_langsmith_rejects_nonfinite_measurements_without_crashing(value):
    for field in ("tokens_in", "tokens_out", "total_tokens", "cost_usd", "latency_ms", "latency_s"):
        measurements, present = pull._measurements({field: value})
        assert measurements == {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_s": 0.0}
        assert present is False
    assert pull._coerce_nonnegative_number(value) is None


def test_langsmith_run_indexes_preserve_ambiguity_and_fallback_refs():
    feedback.record_run("one", "o/r#1", "implement", "vibe")
    feedback.record_run("two", "o/r#1", "implement", "vibe")
    feedback.record_run("fallback", "o/r", "implement", "codex", pr_number=2)
    feedback.record_run("unjoinable", "unjoinable", "implement", "codex")
    assert pull.known_run_ids() == {"one", "two", "fallback", "unjoinable"}
    indexes = pull._run_indexes()
    assert indexes["by_ref"][("o/r", 2)] == ["fallback"]
    assert "unjoinable" not in [run for runs in indexes["by_ref"].values() for run in runs]
    assert pull._resolve_run_id({"run_id": "one"}, indexes) == ("one", "run_id", "")
    for agent, reason in (
        ("vibe", "ambiguous_github_ref_agent"),
        ("other", "ambiguous_github_ref"),
    ):
        run, method, _ = pull._resolve_run_id({"github_pr": "o/r#1", "agent": agent}, indexes)
        assert run is None and method == reason
    assert pull._resolve_run_id({"github_pr": "o/r#9"}, indexes) == (
        None,
        "unmatched_github_ref",
        "o/r#9",
    )


def test_langsmith_strict_ingestion_reports_bad_input_without_writes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bad.ndjson"
    rows = [
        "",
        "{",
        "[]",
        json.dumps({"schema_version": "wrong"}),
        json.dumps({"schema_version": pull.SCHEMA_VERSION}),
        json.dumps(
            {
                "schema_version": pull.SCHEMA_VERSION,
                "run_id": "one",
                "trace_id": "trace",
                "operation_role": "invalid",
            }
        ),
    ]
    path.write_text("\n".join(rows), encoding="utf-8")
    feedback.record_run("one", "o/r#1", "implement", "vibe")
    summary = pull.ingest_files([path, tmp_path / "missing.ndjson"], strict=True)
    assert summary["strict_failed"] is True
    assert summary["skipped"] == {
        "invalid_operation_role": 1,
        "missing_run_id": 1,
        "wrong_schema": 1,
    }
    assert len(summary["errors"]) == 3
    assert ":2: invalid JSON" in summary["errors"][0]
    assert ":3: record is not an object" in summary["errors"][1]
    assert "cannot read" in summary["errors"][2]
    assert summary["written_trace_records"] == summary["written_cost_records"] == 0
    pull._print_summary(summary, as_json=False)
    text = capsys.readouterr().out
    assert "wrote 0 trace record(s), 0 cost row(s), and 0 durability row(s)" in text
    assert "skipped:" in text and "errors:" in text and "record is not an object" in text
    monkeypatch.setattr(pull, "ingest_files", Mock(return_value=summary))
    assert pull.main(["--ndjson", str(path), "--strict", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["strict_failed"] is True


def test_langsmith_cli_requires_input_and_dry_run_prints_counts(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as error:
        pull.main([])
    assert error.value.code == 2
    assert "provide at least one --ndjson PATH" in capsys.readouterr().err
    artifact = tmp_path / "trace.ndjson"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": pull.SCHEMA_VERSION,
                "run_id": "external",
                "trace_id": "trace",
                "total_tokens": 7,
            }
        ),
        encoding="utf-8",
    )
    assert pull.main([str(artifact), "--allow-unmatched", "--dry-run"]) == 0
    assert (
        "would write 1 trace record(s), 1 cost row(s), and 0 durability row(s)"
        in capsys.readouterr().out
    )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0] == 0


def test_frontend_endpoint_validation_and_candidate_order(tmp_path, monkeypatch):
    for name in frontend.BROWSER_ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    assert frontend.default_browser_endpoint() is None
    for endpoint, message in (
        ("ws://localhost:9222", "http://host:port"),
        ("http://remote:9222", "non-local"),
        ("http://localhost", "include a port"),
    ):
        port, error = frontend._endpoint_port(endpoint)
        assert port is None and message in error
    monkeypatch.setenv(frontend.CHROME_PATH_ENV, "/fixture/chrome")
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: "/fixture/chrome")
    candidates = frontend._candidate_browser_paths(str(tmp_path / "explicit"))
    assert candidates[:2] == [str(tmp_path / "explicit"), "/fixture/chrome"]
    assert frontend._shell_join(["chrome path", "--profile=a b"]) == "'chrome path' '--profile=a b'"


@pytest.mark.parametrize("outcome", ["missing", "denied", "delayed", "timeout"])
def test_frontend_browser_start_is_bounded_and_reports_failures(outcome, tmp_path, monkeypatch):
    browser = tmp_path / "chrome"
    browser.touch()
    monkeypatch.setattr(
        frontend,
        "_candidate_browser_paths",
        lambda _path: [] if outcome == "missing" else [str(browser)],
    )
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    if outcome == "denied":
        spawn.side_effect = OSError("launch refused")
    fetch = Mock(side_effect=[OSError("not ready"), '{"Browser":"fixture"}'])
    if outcome == "timeout":
        fetch.side_effect = OSError("not ready")
    clock = iter([0, 0, 2])
    monkeypatch.setattr(frontend.time, "monotonic", lambda: next(clock, 2))
    sleep = Mock()
    result = frontend.start_browser_endpoint(
        "http://localhost:9222",
        timeout=1,
        profile_dir=str(tmp_path / "profile with spaces"),
        fetch_url=fetch,
        sleep=sleep,
        popen=spawn,
    )
    assert (
        result["diagnostic"]
        == {
            "missing": "browser_executable_missing",
            "denied": "browser_start_failed",
            "delayed": "browser_started",
            "timeout": "browser_start_not_ready",
        }[outcome]
    )
    if outcome == "missing":
        spawn.assert_not_called()
        fetch.assert_not_called()
        assert result["attempted"] is False
    else:
        spawn.assert_called_once()
        assert spawn.call_args.args[0] == [
            str(browser),
            "--remote-debugging-port=9222",
            f"--user-data-dir={tmp_path / 'profile with spaces'}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        assert spawn.call_args.kwargs["start_new_session"] is True
        assert result["attempted"] is True
        if outcome == "denied":
            assert result["error"] == "launch refused"
            fetch.assert_not_called()
        else:
            assert result["pid"] == 123
            assert result["ok"] is (outcome == "delayed")
            if outcome == "delayed":
                sleep.assert_called_once_with(0.25)
                assert fetch.call_count == 2


@pytest.mark.parametrize("outcome", ["missing", "node", "timeout", "json", "classified", "invalid"])
def test_frontend_verifier_reports_helper_results_without_processes(outcome, tmp_path, monkeypatch):
    helper = tmp_path / "verify.mjs"
    if outcome != "missing":
        helper.touch()
    monkeypatch.setattr(frontend, "VERIFY_JS", helper)
    monkeypatch.setattr(frontend, "VERIFY_DIR", tmp_path)
    monkeypatch.setattr(
        frontend.shutil, "which", lambda _name: None if outcome == "node" else "node"
    )
    run = Mock(
        return_value=SimpleNamespace(stdout='{"ok":true,"findings":[]}', stderr="", returncode=0)
    )
    if outcome == "timeout":
        run.side_effect = subprocess.TimeoutExpired("node", 45)
    if outcome == "classified":
        run.return_value = SimpleNamespace(
            stdout="", stderr="Please run playwright install", returncode=1
        )
    if outcome == "invalid":
        run.return_value = SimpleNamespace(stdout="x" * 600, stderr="y" * 400, returncode=1)
    monkeypatch.setattr(frontend.subprocess, "run", run)
    result = frontend.verify(
        "http://fixture",
        asserts=["text:Ready"],
        timeout=2000,
        browser_endpoint="http://localhost:9222",
    )
    if outcome == "json":
        assert result == {"ok": True, "findings": []}
    else:
        assert result["ok"] is False
        assert (
            result["diagnostic"]
            == {
                "missing": "helper_missing",
                "node": "node_missing",
                "timeout": "verifier_timeout",
                "classified": "browser_not_installed",
                "invalid": "invalid_helper_output",
            }[outcome]
        )
    if outcome in {"missing", "node"}:
        run.assert_not_called()
    else:
        run.assert_called_once()
        assert run.call_args.args[0] == [
            "node",
            str(helper),
            "--url",
            "http://fixture",
            "--timeout",
            "2000",
            "--assert",
            "text:Ready",
            "--browser-endpoint",
            "http://localhost:9222",
        ]
        assert run.call_args.kwargs == {
            "capture_output": True,
            "text": True,
            "cwd": str(tmp_path),
            "timeout": 47.0,
        }
    if outcome == "invalid":
        assert result["raw"] == "x" * 500 and result["stderr"] == "y" * 300


@pytest.mark.parametrize("ok", [True, False])
def test_frontend_cli_doctor_and_url_return_verdict_status(ok, monkeypatch, capsys):
    doctor = Mock(return_value={"ok": ok, "status": "fixture"})
    verify = Mock(return_value={"ok": ok, "findings": []})
    monkeypatch.setattr(frontend, "doctor", doctor)
    monkeypatch.setattr(frontend, "verify", verify)
    assert frontend.main(
        [
            "--doctor",
            "--require-browser-endpoint",
            "--start-browser",
            "--start-browser-timeout",
            "3",
        ]
    ) == (0 if ok else 1)
    assert json.loads(capsys.readouterr().out) == {"ok": ok, "status": "fixture"}
    doctor.assert_called_once_with(
        browser_endpoint=None,
        require_browser_endpoint=True,
        start_browser=True,
        start_browser_timeout=3.0,
    )
    assert frontend.main(["--url", "http://fixture"]) == (0 if ok else 1)
    assert json.loads(capsys.readouterr().out) == {"ok": ok, "findings": []}
    assert verify.call_args.args[0] == "http://fixture"
    with pytest.raises(SystemExit) as error:
        frontend.main([])
    assert error.value.code == 2
    assert "--url is required" in capsys.readouterr().err
