"""Focused coverage for append-only rate-limit incident telemetry."""

import json
import subprocess
import time

import pytest

import adapters
import capacity
import dispatcher
import ledger_reconcile
import rate_incidents


@pytest.fixture
def incident_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    return tmp_path


def test_append_preserves_history_and_dedupes(incident_paths):
    rate_incidents.INCIDENT_FILE.write_text('{"existing":true}\n')
    first = rate_incidents.record_incident(
        agent="codex",
        surface="dispatch",
        category="quota",
        run_id="run-1",
        evidence="quota exhausted",
        credential_pool="codex-subscription",
        resource="5h-window",
        reroute="claude",
    )
    duplicate = rate_incidents.record_incident(
        agent="codex",
        surface="dispatch",
        category="quota",
        run_id="run-1",
        evidence="quota exhausted",
    )
    lines = rate_incidents.INCIDENT_FILE.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0]) == {"existing": True}
    row = json.loads(lines[1])
    assert row["schema"] == "rate-limit-incident/v1"
    assert row["incident_id"] == first["incident_id"]
    assert row["idempotency_key"] == "run-1|codex|quota"
    assert row["credential_pool"] == "codex-subscription"
    assert row["resource"] == "5h-window"
    assert row["reroute"] == "claude"
    assert duplicate["deduped"] is True


def test_bounded_dedup_scan_finds_recent_key_after_large_history(incident_paths):
    key = "recent-run|codex|quota"
    recent = json.dumps({"idempotency_key": key, "incident_id": "recent-incident"})
    rate_incidents.INCIDENT_FILE.write_bytes(
        b"x" * (rate_incidents.MAX_DEDUP_SCAN_BYTES + 200) + b"\n" + recent.encode() + b"\n"
    )
    assert rate_incidents._existing_idempotency_key(key) == "recent-incident"


def test_evidence_is_bounded_and_redacted(incident_paths):
    rate_incidents.record_incident(
        agent="codex",
        surface="dispatch",
        category="rate_limit",
        run_id="run-2",
        evidence="429 token=super-secret-value sk-abcdefghijklmnopqrstuvwxyz " + "x" * 600,
        shed=False,
    )
    row = json.loads(rate_incidents.INCIDENT_FILE.read_text())
    assert "super-secret-value" not in row["evidence_excerpt"]
    assert "sk-" not in row["evidence_excerpt"]
    assert len(row["evidence_excerpt"]) <= rate_incidents.MAX_EVIDENCE_EXCERPT + len(
        "...[TRUNCATED]"
    )


@pytest.mark.parametrize(
    ("text", "authoritative"),
    [
        ("ActionRequiredError", False),
        ("implemented rate limit handling and tests", False),
        ("rate-limit resilience work completed", False),
        ("ActionRequiredError: out of usage", True),
        ("Error: provider returned resource_exhausted", True),
        ("[resource_exhausted]", True),
        ('{"error":{"status":"RESOURCE_EXHAUSTED"}}', True),
        ("review confirms resource_exhausted handling is covered", False),
        ("the provider returned resource_exhausted in the test fixture", False),
        ("HTTP 429 Too Many Requests", True),
        ("status code: 429", True),
        ("Error 429 from provider", True),
        ("429 Too Many Requests", True),
        ("Modified 429 lines in this refactor", False),
        ("collected 429 items", False),
        ("issue #429 documents rate limit handling", False),
        ("network connection reset", False),
        ("authentication failed", False),
        ("permission denied", False),
        ("server overloaded", False),
        ("server unavailable", False),
        ("workflow action_required", False),
    ],
)
def test_only_explicit_provider_capacity_evidence_sheds(text, authoritative):
    assert rate_incidents.is_authoritative_error(text) is authoritative


def test_success_stdout_capacity_gate_rejects_review_prose():
    assert rate_incidents.stdout_carries_capacity_evidence("[resource_exhausted]") is True
    review = (
        "Verdict: approved. The resource_exhausted classifier and tests are correct; "
        "the provider returned resource_exhausted in the synthetic fixture."
    )
    assert rate_incidents.stdout_carries_capacity_evidence(review) is False
    assert rate_incidents.is_authoritative_error(review) is False


def test_json_shed_marker_expiry_and_legacy_marker(incident_paths, monkeypatch):
    monkeypatch.setattr(capacity, "SHED_DIR", rate_incidents.SHED_DIR)
    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="expired", expires_at=int(time.time()) - 1
    )
    assert capacity._shed("codex") is False
    assert not (rate_incidents.SHED_DIR / "codex").exists()
    rate_incidents.SHED_DIR.mkdir(exist_ok=True)
    (rate_incidents.SHED_DIR / "codex").touch()
    assert capacity._shed("codex") is True


@pytest.mark.parametrize("expires_at", [True, float("nan")], ids=["boolean", "nan"])
def test_invalid_json_shed_marker_remains_active(incident_paths, monkeypatch, expires_at):
    monkeypatch.setattr(capacity, "SHED_DIR", rate_incidents.SHED_DIR)
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "codex"
    marker.write_text(json.dumps({"expires_at": expires_at}))
    assert capacity._shed("codex") is True
    assert marker.exists()


def test_expired_marker_is_refreshed_by_new_incident(incident_paths):
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "cursor"
    marker.write_text(json.dumps({"expires_at": time.time() - 1}))
    rate_incidents.record_incident(
        agent="cursor",
        surface="direct",
        category="capacity",
        run_id="run-new",
        evidence="resource_exhausted",
    )
    assert json.loads(marker.read_text())["expires_at"] > time.time()


def test_synchronous_adapter_hook_records_structured_evidence(incident_paths, monkeypatch):
    calls = iter(
        (
            subprocess.CompletedProcess(["agent"], 1, "", "HTTP 429 Too Many Requests"),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        )
    )
    monkeypatch.setattr(adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(adapters.subprocess, "run", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(adapters, "record_ledger", lambda *args, **kwargs: None)
    result = adapters.dispatch("codex", "test", cwd=str(incident_paths))
    row = json.loads(rate_incidents.INCIDENT_FILE.read_text())
    assert result["rate_incident_evidence"]["is_authoritative"] is True
    assert row["surface"] == "adapters.dispatch" and row["target"] == str(incident_paths.resolve())


def test_synchronous_adapter_repeated_failures_get_distinct_run_ids(incident_paths, monkeypatch):
    def fake_run(command, *args, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "HTTP 429 Too Many Requests")

    monkeypatch.setattr(adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    monkeypatch.setattr(adapters, "record_ledger", lambda *args, **kwargs: None)
    adapters.dispatch("codex", "test", cwd=str(incident_paths))
    adapters.dispatch("codex", "test", cwd=str(incident_paths))
    rows = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["run_id"] != rows[1]["run_id"]


def test_synchronous_adapter_preserves_classification_when_recording_fails(
    incident_paths, monkeypatch, capsys
):
    calls = iter(
        (
            subprocess.CompletedProcess(["agent"], 1, "", "HTTP 429 Too Many Requests"),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        )
    )
    monkeypatch.setattr(adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(adapters.subprocess, "run", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rate_incidents, "record_incident", lambda **kwargs: (_ for _ in ()).throw(OSError("disk"))
    )
    result = adapters.dispatch("codex", "test", cwd=str(incident_paths))
    assert result["rate_incident_evidence"]["is_authoritative"] is True
    assert "rate-incident recording failed" in capsys.readouterr().err


@pytest.mark.parametrize("evidence_source", ["stdout", "stderr", "agent_log"])
def test_offload_hook_reads_output_stderr_and_agent_log(
    incident_paths, monkeypatch, evidence_source
):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", incident_paths / "logs")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 1)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *args: (False, "test")
    )
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args, **kwargs: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *args, **kwargs: None)
    stdout = "resource_exhausted" if evidence_source == "stdout" else "ordinary output"
    stderr = "resource_exhausted" if evidence_source == "stderr" else "ordinary stderr"
    agent = "gemini" if evidence_source == "agent_log" else "codex"
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["agent"], 1, stdout, stderr),
    )
    monkeypatch.setattr(
        dispatcher,
        "_agent_log_tail_from_argv",
        lambda *args, **kwargs: "resource_exhausted" if evidence_source == "agent_log" else "",
    )
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    result = dispatcher.offload(agent, "test", cwd=str(incident_paths))
    row = json.loads(rate_incidents.INCIDENT_FILE.read_text())
    assert result["rate_incident_evidence"]["is_authoritative"] is True
    assert row["surface"] == "dispatcher.offload" and row["run_id"] == result["run_id"]


def test_offload_preserves_classification_when_recording_fails(incident_paths, monkeypatch, capsys):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", incident_paths / "logs")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 1)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *args: (False, "test")
    )
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args, **kwargs: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["agent"], 1, "", "HTTP 429 Too Many Requests"
        ),
    )
    monkeypatch.setattr(
        rate_incidents, "record_incident", lambda **kwargs: (_ for _ in ()).throw(OSError("disk"))
    )
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    result = dispatcher.offload("codex", "test", cwd=str(incident_paths))
    assert result["rate_incident_evidence"]["is_authoritative"] is True
    assert "rate-incident recording failed" in capsys.readouterr().err


def test_ledger_completion_hook_uses_only_run_segment(incident_paths, monkeypatch):
    monkeypatch.setattr(ledger_reconcile.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ledger_reconcile.feedback, "record_completion_event", lambda *args, **kwargs: None
    )
    log = incident_paths / "run.log"
    log.write_text(
        "=== earlier run_id=other ===\nHTTP 429 Too Many Requests\n"
        "=== current run_id=run-current ===\nresource_exhausted\n"
        "=== later run_id=later ===\nauthentication failed\n"
    )
    ledger_reconcile.record_completion("run-current", "codex", log_file=str(log))
    rows = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-current"
    assert rows[0]["surface"] == "ledger_reconcile.completion"
    assert rows[0]["category"] == "capacity"


def test_historical_reconcile_records_without_shedding(incident_paths):
    evidence = ledger_reconcile._classify_run_log_segment(
        ["HTTP 429 Too Many Requests"], "codex", "historical-run", shed=False
    )
    assert evidence and evidence["is_authoritative"] is True
    assert rate_incidents.INCIDENT_FILE.exists()
    assert not (rate_incidents.SHED_DIR / "codex").exists()


def test_unmarked_dedicated_log_is_available_but_mixed_log_remains_bounded(incident_paths):
    dedicated = incident_paths / "dedicated.log"
    dedicated.write_text("usage event\nresource_exhausted\n")
    assert ledger_reconcile._log_segment(dedicated, "run") == [
        "usage event",
        "resource_exhausted",
    ]
    mixed = incident_paths / "mixed.log"
    mixed.write_text("=== run_id=other ===\nresource_exhausted\n")
    assert ledger_reconcile._log_segment(mixed, "run") == []


def test_record_incident_rejects_unknown_keywords(incident_paths):
    with pytest.raises(TypeError):
        getattr(rate_incidents, "record_incident")(
            agent="codex", surface="test", category="quota", bogus=True
        )


def test_successful_completion_log_with_bare_429_does_not_shed(incident_paths, monkeypatch):
    monkeypatch.setattr(ledger_reconcile.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ledger_reconcile.feedback, "record_completion_event", lambda *args, **kwargs: None
    )
    log = incident_paths / "successful-run.log"
    log.write_text(
        "=== current run_id=run-benign ===\n"
        "Reading src/capacity.py to understand the 429-shed flag design.\n"
        "Modified 429 lines in this refactor.\n"
        "All tests passed. Task complete.\n"
        "=== later run_id=later ===\n"
    )

    ledger_reconcile.record_completion("run-benign", "codex", log_file=str(log))

    assert not rate_incidents.INCIDENT_FILE.exists()
    assert not (rate_incidents.SHED_DIR / "codex").exists()
