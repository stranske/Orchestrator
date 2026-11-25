"""Focused coverage for append-only rate-limit incident telemetry."""

import json
import subprocess
import time

import pytest

import adapters
import capacity
import dispatcher
import execution_profiles
import ledger_reconcile
import rate_incidents


@pytest.fixture
def incident_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    # The completion step reads the capacity ledger for a log's writers: never the live one.
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
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
        ("handle RESOURCE_EXHAUSTED errors", False),
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
    assert rate_incidents.stdout_carries_capacity_evidence("HTTP 429 Too Many Requests") is False
    assert rate_incidents.stdout_carries_capacity_evidence("quota exhausted") is False
    assert (
        rate_incidents.stdout_carries_capacity_evidence(
            "ActionRequiredError: quota exhausted for this provider"
        )
        is True
    )
    review = (
        "Verdict: approved. The resource_exhausted classifier and tests are correct; "
        "the provider returned resource_exhausted in the synthetic fixture. The supported JSON "
        'forms include `"status": "RESOURCE_EXHAUSTED"` and `"code": "RESOURCE_EXHAUSTED"`.'
    )
    assert rate_incidents.stdout_carries_capacity_evidence(review) is False
    assert rate_incidents.is_authoritative_error(review) is False


@pytest.mark.parametrize(
    "message",
    [
        "The model is not supported.",
        "The 'test-model' model is not supported with this account.",
        "The model `test-model` is not supported.",
        "The model test-model does not exist or you do not have access to it.",
        "Unsupported model: test-model",
        "Invalid model selection: test-model",
    ],
)
@pytest.mark.parametrize(
    "prefix",
    [
        "unexpected status 400 Bad Request: ",
        "Provider error: HTTP 400: ",
        "error: status code = 400 Bad Request:\n",
        "HTTP status: 400 ",
        "status=400:\r\n",
    ],
)
def test_model_rejection_is_authoritative_without_shedding(message, prefix):
    text = prefix + json.dumps({"error": {"message": message, "type": "invalid_request_error"}})
    assert rate_incidents.classify_provider_failure(text) == (
        "model_rejected",
        "http_400_model_rejected",
        "high",
    )
    evidence = rate_incidents.get_structured_evidence(text, "codex", "test", "rejected")
    assert evidence["category"] == "model_rejected"
    assert evidence["subcategory"] == "http_400_model_rejected"
    assert evidence["confidence"] == "high"
    assert evidence["is_authoritative"] is True
    assert evidence["should_shed"] is False


@pytest.mark.parametrize(
    "text",
    [
        "Document HTTP 400 when the model is not supported.",
        "The model test-model does not exist.",
        'Added handling for HTTP 400: {"error": {"message": "The model is not supported."}}',
        'unexpected status 404 Not Found: {"error": {"message": "The model does not exist."}}',
        'HTTP 400: {"message": "The model is not supported."}',
        'HTTP 400: {"error": "The model is not supported."}',
        'HTTP 400: {"error": {"message": "Your input exceeds the context window of this model."}}',
        'HTTP 400: {"error": {"message": "The model is supported; this option is not supported."}}',
        'HTTP 400: {"error": {"message": "The model configuration file does not exist."}}',
        'HTTP 400: {"error": {"message": null}}',
        'HTTP 400: {"error": {"message": ["model not supported"]}}',
        'HTTP 400: {"error": {"message": "model not supported"}',
        "HTTP 400: []\nThe model is not supported.",
        'HTTP 400: {"error": {"message": "Invalid request"}}\nThe model does not exist.',
        'HTTP 400: invalid request\nHTTP 404: {"error": {"message": "model not supported"}}',
        'HTTP 400 handling example: {"error": {"message": "model not supported"}}',
        'HTTP 400; HTTP 404: {"error": {"message": "model does not exist"}}',
        'HTTP 400.5: {"error": {"message": "model not supported"}}',
        'HTTP\n400: {"error": {"message": "model not supported"}}',
    ],
)
def test_model_rejection_requires_http_400_and_its_own_provider_message(text):
    assert rate_incidents.classify_provider_failure(text) == (
        "unknown",
        "non_authoritative",
        "none",
    )


def test_model_rejection_accepts_a_multiline_provider_error_body():
    text = "unexpected status 400 Bad Request:\n" + json.dumps(
        {"error": {"message": "The model does not exist."}}, indent=2
    )
    assert rate_incidents.classify_provider_failure(text)[0] == "model_rejected"


def test_model_rejection_incident_skips_the_shed_writer(incident_paths, monkeypatch):
    def unexpected_shed(*args, **kwargs):
        pytest.fail("a model rejection must not call the shed writer")

    monkeypatch.setattr(rate_incidents, "ensure_shed", unexpected_shed)
    result = rate_incidents.record_incident(
        agent="codex",
        surface="test",
        category="model_rejected",
        run_id="rejected",
        evidence="model not supported",
    )
    assert result["shed"] is False
    assert json.loads(rate_incidents.INCIDENT_FILE.read_text())["category"] == "model_rejected"
    assert not (rate_incidents.SHED_DIR / "codex").exists()
    assert rate_incidents._cooldown_seconds("model_rejected") is None


@pytest.mark.parametrize("existing_shed", [False, True])
def test_direct_model_rejection_shed_request_leaves_markers_untouched(
    incident_paths, existing_shed
):
    marker = rate_incidents.SHED_DIR / "codex"
    prior = b""
    if existing_shed:
        assert rate_incidents.ensure_shed(
            "codex", category="quota", incident_id="prior", expires_at=int(time.time()) + 3600
        )
        prior = marker.read_bytes()
    assert not rate_incidents.ensure_shed(
        "codex",
        category="model_rejected",
        incident_id="rejected",
        expires_at=int(time.time()) + 7200,
    )
    if existing_shed:
        assert marker.read_bytes() == prior
    else:
        assert not marker.exists()


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


@pytest.mark.parametrize(
    "evidence_source", ["stdout", "stderr", "agent_log", "gemini_stderr", "gemini_agent_log"]
)
def test_offload_hook_reads_output_stderr_and_agent_log(
    incident_paths, monkeypatch, evidence_source
):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", incident_paths / "logs")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 1)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(
        dispatcher,
        "_select_offload_profile",
        lambda agent, *_args: (
            execution_profiles.get_profile("gemini-3.8-flash-high") if agent == "gemini" else None
        ),
    )
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
    agent = (
        "gemini"
        if evidence_source in {"agent_log", "gemini_stderr", "gemini_agent_log"}
        else "codex"
    )
    if evidence_source == "gemini_stderr":
        stderr = GEMINI_INDIVIDUAL_QUOTA_LINE
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["agent"], 1, stdout, stderr),
    )
    monkeypatch.setattr(
        dispatcher,
        "_agent_log_tail_from_argv",
        lambda *args, **kwargs: (
            GEMINI_INDIVIDUAL_QUOTA_LINE
            if evidence_source == "gemini_agent_log"
            else "resource_exhausted" if evidence_source == "agent_log" else ""
        ),
    )
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    result = dispatcher.offload(agent, "test", cwd=str(incident_paths))
    row = json.loads(rate_incidents.INCIDENT_FILE.read_text())
    assert result["rate_incident_evidence"]["is_authoritative"] is True
    assert row["surface"] == "dispatcher.offload" and row["run_id"] == result["run_id"]
    if evidence_source.startswith("gemini_"):
        assert row["reset_at"] is not None
        assert row["reset_at"] > time.time() + 139 * 3600


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
        "=== 2026-10-05T00:00:00Z earlier run_id=other ===\nHTTP 429 Too Many Requests\n"
        "=== 2026-10-05T00:01:00Z current run_id=run-current ===\nresource_exhausted\n"
        "=== 2026-10-05T00:02:00Z later run_id=later ===\nauthentication failed\n"
    )
    ledger_reconcile.record_completion("run-current", "codex", log_file=str(log), exit_code=1)
    rows = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-current"
    assert rows[0]["surface"] == "ledger_reconcile.completion"
    assert rows[0]["category"] == "capacity"


def test_historical_reconcile_records_without_shedding(incident_paths):
    evidence = ledger_reconcile._classify_run_log_segment(
        ["HTTP 429 Too Many Requests"],
        "codex",
        "historical-run",
        shed=False,
        successful=False,
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
    mixed.write_text("=== 2026-10-05T00:00:00Z dispatch run_id=other ===\nresource_exhausted\n")
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
        "=== 2026-10-05T00:01:00Z current run_id=run-benign ===\n"
        "Reading src/capacity.py to understand the 429-shed flag design.\n"
        "HTTP 429 Too Many Requests\n"
        "Modified 429 lines in this refactor.\n"
        "All tests passed. Task complete.\n"
        "=== 2026-10-05T00:02:00Z later run_id=later ===\n"
    )

    ledger_reconcile.record_completion("run-benign", "codex", log_file=str(log), exit_code=0)

    assert not rate_incidents.INCIDENT_FILE.exists()
    assert not (rate_incidents.SHED_DIR / "codex").exists()


GEMINI_INDIVIDUAL_QUOTA_LINE = (
    "E1018 01:42:33.766065 697 errorreport.go:224] agent executor error: generating and executing: "
    "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to "
    "increase your limits. Resets in 140h5m6s."
)


def test_gemini_individual_quota_line_is_authoritative():
    category, subcategory, confidence = rate_incidents.classify_provider_failure(
        GEMINI_INDIVIDUAL_QUOTA_LINE
    )
    assert (category, subcategory, confidence) == ("quota", "quota_exhausted", "high")
    base = 1_700_000_000
    reset = rate_incidents.provider_reset_at("gemini", [GEMINI_INDIVIDUAL_QUOTA_LINE], now=base)
    assert reset == base + 140 * 3600 + 5 * 60 + 6


@pytest.mark.parametrize(
    "prose",
    ["handle RESOURCE_EXHAUSTED (code 429) errors", "handle quota reached errors"],
)
def test_gemini_quoted_capacity_prose_is_not_a_refusal(prose):
    assert not rate_incidents.stdout_carries_capacity_evidence(prose)
    assert not rate_incidents.is_authoritative_error(prose)


def test_gemini_reset_ignores_unrelated_prose_duration():
    refusal = "Error: Individual quota reached. Resets in 2h3m4s."
    lines = ["Document Resets in 999h0m0s in the help text", refusal]
    assert rate_incidents.provider_reset_at("gemini", lines, now=1000) == 1000 + 7384
    assert rate_incidents.provider_reset_at("gemini", lines[:1], now=1000) is None


def test_gemini_adapter_reads_reset_from_stderr(incident_paths, monkeypatch):
    calls = iter(
        (
            subprocess.CompletedProcess(
                ["agent"], 1, "ordinary output", GEMINI_INDIVIDUAL_QUOTA_LINE
            ),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        )
    )
    monkeypatch.setattr(adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(adapters.subprocess, "run", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(adapters, "record_ledger", lambda *args, **kwargs: None)
    before = int(time.time())
    adapters.dispatch("gemini", "test", cwd=str(incident_paths))
    row = json.loads(rate_incidents.INCIDENT_FILE.read_text())
    duration = 140 * 3600 + 5 * 60 + 6
    assert row["reset_at"] is not None
    assert before + duration <= row["reset_at"] <= int(time.time()) + duration


# The vibe CLI's block for Mistral's 2026-11-11 refusal, verbatim from the Workflows#4637 dispatch log.
VIBE_BILLING_REFUSAL = (
    "Error: API error from mistral (model: mistral-vibe-cli-latest): LLM backend error [mistral]\n"
    "  status: 402 Payment Required\n"
    "  reason: Payment Required\n"
    "  request_id: N/A\n"
    "  endpoint: https://api.mistral.ai\n"
    "  model: mistral-vibe-cli-latest\n"
    "  provider_message: Vibe budget exhausted.\n"
    '  body_excerpt: {"object":"error","message":"Vibe budget exhausted.",'
    '"type":"billing_vibe_budget_exhausted","param":null,"code":"2304","raw_status_code":402}\n'
)


def test_provider_billing_refusal_is_an_authoritative_quota():
    assert rate_incidents.classify_provider_failure(VIBE_BILLING_REFUSAL) == (
        "quota",
        "billing_exhausted",
        "high",
    )
    assert rate_incidents.stdout_carries_capacity_evidence(VIBE_BILLING_REFUSAL)
    # Either structure alone is enough: the JSON type, or the status field plus its own reason.
    body_only = VIBE_BILLING_REFUSAL.splitlines()[-1]
    status_only = "\n".join(VIBE_BILLING_REFUSAL.splitlines()[:3])
    assert rate_incidents.is_authoritative_error(body_only)
    assert rate_incidents.is_authoritative_error(status_only)


@pytest.mark.parametrize(
    "prose",
    [
        "Return HTTP 402 Payment Required when the invoice is unpaid.",
        "The handler maps status 402 to a billing banner; budget exhausted copy lives in i18n.",
        "status: 402\nAll tests passed. Task complete.",
        "Added billing_exhausted handling to the client.",
    ],
)
def test_billing_prose_is_not_a_refusal(prose):
    assert not rate_incidents.stdout_carries_capacity_evidence(prose)
    assert not rate_incidents.is_authoritative_error(prose)


def test_unknown_exit_vibe_billing_refusal_sheds_the_seat(incident_paths, monkeypatch):
    """The real 4637 case: the ledger recorded no exit code, so only the capacity envelope reads it."""
    monkeypatch.setattr(ledger_reconcile.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ledger_reconcile.feedback, "record_completion_event", lambda *args, **kwargs: None
    )
    log = incident_paths / "vibe-402.log"
    log.write_text(
        "=== 2026-11-11T17:14:52Z dispatch vibe/full -> stranske/Workflows#4637 [implement] "
        "run_id=run-vibe-402 ===\n" + VIBE_BILLING_REFUSAL
    )

    ledger_reconcile.record_completion("run-vibe-402", "vibe", log_file=str(log), exit_code=None)

    marker = json.loads((rate_incidents.SHED_DIR / "vibe").read_text())
    assert marker["category"] == "quota"
    assert "tier_scope" not in marker  # the whole seat: the budget covers every vibe model
    # Owner 2026-11-24 (#1045): a spend refusal with no reset is held until a probe run is answered, not benched
    # until the next real job re-tests it (which cost vibe ~3 failed jobs a day from 2026-11-11).
    assert marker["hold"] == rate_incidents.BILLING_HOLD and "expires_at" not in marker
    assert marker["probe_after"] > marker["created_at"]


# agy prints a Go time.Duration, which drops leading zero units (2026-11-12).
@pytest.mark.parametrize(
    ("copy", "seconds"),
    [
        ("Resets in 3h8m36s.", 3 * 3600 + 8 * 60 + 36),
        ("Resets in 45m0s.", 45 * 60),
        ("Resets in 30s.", 30),
        ("Resets in 1.5s.", 2),
        ("Resets in 140h5m6s.", 140 * 3600 + 5 * 60 + 6),
        ("Resets in 4d2h3m.", 4 * 86400 + 2 * 3600 + 3 * 60),
    ],
)
def test_gemini_reset_reads_every_go_duration_form(copy, seconds):
    line = (
        "E1111 12:32:54.598632     462 errorreport.go:273] agent executor error: generating and "
        "executing: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your "
        f"subscription to increase your limits. {copy}"
    )
    assert rate_incidents.provider_reset_at("gemini", [line], now=1000) == 1000 + seconds


def test_resets_in_without_a_duration_is_not_a_reset():
    assert rate_incidents.parse_resets_in_duration("Resets in a while", now=1000) is None


# The verbatim muse terminal failure of the 2026-11-12 07:03Z trip-planner#2029 repair.
MUSE_QUOTA_LINE = (
    "run ended with Failed: API error 429 [request_id=8c08d686-ef50-42df-9e92-7bbd2b1842f0]: "
    "Subscription quota exhausted. Your usage window resets at 2026-11-16T00:00:00Z. (rate_limit_error)"
)
MUSE_RESET = 1794787200  # 2026-11-16T00:00:00Z


def test_muse_quota_refusal_sheds_until_its_stated_reset():
    now = 1794467000  # 2026-11-12T07:03Z
    assert rate_incidents.is_authoritative_error(MUSE_QUOTA_LINE)
    assert rate_incidents.provider_reset_at("muse", [MUSE_QUOTA_LINE], now=now) == MUSE_RESET


def test_muse_reset_ignores_prose_and_past_times():
    now = 1794467000
    prose = "Docs: your usage window resets at 2027-01-01T00:00:00Z"
    assert rate_incidents.provider_reset_at("muse", [prose], now=now) is None
    assert rate_incidents.provider_reset_at("muse", [MUSE_QUOTA_LINE], now=MUSE_RESET + 1) is None
    assert rate_incidents.provider_reset_at("claude", [MUSE_QUOTA_LINE], now=now) is None


def test_failed_marker_publication_is_repaired_on_dedup_retry(incident_paths, monkeypatch):
    monkeypatch.setattr(capacity, "SHED_DIR", rate_incidents.SHED_DIR)
    monkeypatch.setattr(rate_incidents, "GEMINI_SEPARATE_QUOTA_FAMILIES", ("argon",))
    now = int(time.time())
    reset_at = now + 10 * 3600
    evidence = (
        '{"event":"init","init":{"model":"gemini-4-argon-high"}}\n' + GEMINI_INDIVIDUAL_QUOTA_LINE
    )
    real_replace = rate_incidents.os.replace
    replace_calls = 0

    def fail_first_replace(src, dst):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError("simulated marker publication failure")
        return real_replace(src, dst)

    monkeypatch.setattr(rate_incidents.os, "replace", fail_first_replace)

    first = rate_incidents.record_incident(
        agent="gemini",
        surface="dispatcher.offload",
        category="quota",
        run_id="run-dedup-repair",
        evidence=evidence,
        timestamp=now,
        reset_at=reset_at,
    )
    assert first["status"] == "shed_unfulfilled"
    assert first["deduped"] is False
    assert first["shed"] is False
    assert not (rate_incidents.SHED_DIR / "gemini").exists()

    rows = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["shed_requested"] is True
    assert rows[0]["expires_at"] == reset_at
    assert rows[0]["model"] == "gemini-4-argon-high"
    assert rows[0]["tier_scope"] == "full"
    assert rows[0]["recovered"] is False

    second = rate_incidents.record_incident(
        agent="gemini",
        surface="ledger_reconcile.completion",
        category="quota",
        run_id="run-dedup-repair",
        evidence="Individual quota reached.",
        timestamp=now + 120,
    )
    assert second == {
        "status": "ok",
        "deduped": True,
        "incident_id": first["incident_id"],
        "shed": True,
    }
    rows_after = [
        json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()
    ]
    assert len(rows_after) == 1
    marker = json.loads((rate_incidents.SHED_DIR / "gemini").read_text())
    assert marker["incident_id"] == first["incident_id"]
    assert marker["expires_at"] == reset_at
    assert marker["reset_at"] == reset_at
    assert marker["model"] == "gemini-4-argon-high"
    assert marker["tier_scope"] == "full"


def test_dedup_repair_negative_controls_expired_recovered_shed_false_and_newer_hold(
    incident_paths, monkeypatch
):
    now = int(time.time())
    real_ensure_shed = rate_incidents.ensure_shed

    # 1. expired: unfulfilled shed whose original expires_at has passed is not re-shed on dedup
    monkeypatch.setattr(rate_incidents, "ensure_shed", lambda *a, **k: False)
    expired_first = rate_incidents.record_incident(
        agent="codex",
        surface="dispatcher.offload",
        category="rate_limit",
        run_id="run-expired",
        evidence="HTTP 429 Too Many Requests",
        timestamp=now - rate_incidents.DEFAULT_COOLDOWN_S - 60,
    )
    assert expired_first["status"] == "shed_unfulfilled"

    def fail_if_called(*args, **kwargs):
        pytest.fail("ensure_shed must not be called when negative control applies")

    monkeypatch.setattr(rate_incidents, "ensure_shed", fail_if_called)
    expired_retry = rate_incidents.record_incident(
        agent="codex",
        surface="ledger_reconcile.completion",
        category="rate_limit",
        run_id="run-expired",
        evidence="HTTP 429 Too Many Requests",
        timestamp=now,
    )
    assert expired_retry == {
        "status": "ok",
        "deduped": True,
        "incident_id": expired_first["incident_id"],
        "shed": False,
    }
    assert not (rate_incidents.SHED_DIR / "codex").exists()

    # 2. recovered: explicitly cleared/recovered incident is not re-shed on dedup
    monkeypatch.setattr(rate_incidents, "ensure_shed", lambda *a, **k: False)
    rec_first = rate_incidents.record_incident(
        agent="claude",
        surface="dispatcher.offload",
        category="quota",
        run_id="run-recovered",
        evidence="out of usage",
        timestamp=now,
    )
    assert rec_first["status"] == "shed_unfulfilled"
    rate_incidents.clear_shed("claude")
    monkeypatch.setattr(rate_incidents, "ensure_shed", fail_if_called)
    rec_retry = rate_incidents.record_incident(
        agent="claude",
        surface="ledger_reconcile.completion",
        category="quota",
        run_id="run-recovered",
        evidence="out of usage",
        timestamp=now + 10,
    )
    assert rec_retry == {
        "status": "ok",
        "deduped": True,
        "incident_id": rec_first["incident_id"],
        "shed": False,
    }
    assert not (rate_incidents.SHED_DIR / "claude").exists()

    # 3. shed=False: incident recorded with shed=False never sheds on dedup
    no_shed_first = rate_incidents.record_incident(
        agent="cursor",
        surface="ledger_reconcile.reconcile",
        category="capacity",
        run_id="run-no-shed",
        evidence="resource_exhausted",
        shed=False,
        timestamp=now,
    )
    assert no_shed_first["status"] == "ok" and no_shed_first["shed"] is False
    no_shed_retry = rate_incidents.record_incident(
        agent="cursor",
        surface="dispatcher.offload",
        category="capacity",
        run_id="run-no-shed",
        evidence="resource_exhausted",
        shed=True,
        timestamp=now + 5,
    )
    assert no_shed_retry == {
        "status": "ok",
        "deduped": True,
        "incident_id": no_shed_first["incident_id"],
        "shed": False,
    }
    assert not (rate_incidents.SHED_DIR / "cursor").exists()

    # 4. newer-hold / manual-hold: existing newer or manual hold is preserved on dedup retry
    monkeypatch.setattr(rate_incidents, "ensure_shed", lambda *a, **k: False)
    old_first = rate_incidents.record_incident(
        agent="muse",
        surface="dispatcher.offload",
        category="quota",
        run_id="run-older-unfulfilled",
        evidence="quota exhausted",
        timestamp=now,
        reset_at=now + 50_000,
    )
    assert old_first["status"] == "shed_unfulfilled"
    monkeypatch.setattr(rate_incidents, "ensure_shed", real_ensure_shed)
    newer_expiry = now + 25_000
    assert rate_incidents.ensure_shed(
        "muse",
        category="rate_limit",
        incident_id="newer-incident-id",
        expires_at=newer_expiry,
    )
    prior_bytes = (rate_incidents.SHED_DIR / "muse").read_bytes()
    monkeypatch.setattr(rate_incidents, "ensure_shed", fail_if_called)
    newer_hold_retry = rate_incidents.record_incident(
        agent="muse",
        surface="ledger_reconcile.completion",
        category="quota",
        run_id="run-older-unfulfilled",
        evidence="quota exhausted",
        timestamp=now + 10,
    )
    assert newer_hold_retry == {
        "status": "ok",
        "deduped": True,
        "incident_id": old_first["incident_id"],
        "shed": False,
    }
    assert (rate_incidents.SHED_DIR / "muse").read_bytes() == prior_bytes

    manual_marker = rate_incidents.SHED_DIR / "muse"
    manual_marker.write_text(json.dumps({"created_at": 1, "expires_at": now + 10_000}) + "\n")
    manual_bytes = manual_marker.read_bytes()
    manual_hold_retry = rate_incidents.record_incident(
        agent="muse",
        surface="ledger_reconcile.completion",
        category="quota",
        run_id="run-older-unfulfilled",
        evidence="quota exhausted",
        timestamp=now + 20,
    )
    assert manual_hold_retry == {
        "status": "ok",
        "deduped": True,
        "incident_id": old_first["incident_id"],
        "shed": False,
    }
    assert manual_marker.read_bytes() == manual_bytes


def test_callers_warn_and_surface_shed_unfulfilled(incident_paths, monkeypatch, capsys):
    monkeypatch.setattr(rate_incidents, "ensure_shed", lambda *a, **k: False)
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
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")

    offload_res = dispatcher.offload("codex", "test", cwd=str(incident_paths))
    assert offload_res["shed_unfulfilled"] is True
    assert offload_res["rate_incident_evidence"]["shed_unfulfilled"] is True
    assert offload_res["rate_incident_evidence"]["incident_status"] == "shed_unfulfilled"
    assert offload_res["rate_incident_evidence"]["shed"] is False
    assert "rate-incident shed unfulfilled" in capsys.readouterr().err

    reconcile_res = ledger_reconcile._classify_run_log_segment(
        ["HTTP 429 Too Many Requests"],
        "codex",
        offload_res["run_id"],
        shed=True,
        successful=False,
    )
    assert reconcile_res is not None
    assert reconcile_res["shed_unfulfilled"] is True
    assert reconcile_res["incident_status"] == "shed_unfulfilled"
    assert reconcile_res["shed"] is False
    assert "rate-incident shed unfulfilled" in capsys.readouterr().err


def _shed_expiry(agent):
    return json.loads((rate_incidents.SHED_DIR / agent).read_text())["expires_at"]


def test_repeated_quota_incidents_escalate_the_cooldown_and_cap(incident_paths, monkeypatch):
    monkeypatch.delenv("ORCH_PROVIDER_COOLDOWN_S", raising=False)
    hour = 3600
    start = int(time.time())
    # Each incident's shed is written from its own timestamp; only the latest marker survives.
    expiries = []
    for i in range(3):
        ts = start + i * 6 * hour
        rate_incidents.record_incident(
            agent="vibe",
            surface="dispatch",
            category="quota",
            run_id=f"run-{i}",
            evidence="402 Payment Required: budget exhausted",
            timestamp=ts,
        )
        expiries.append(_shed_expiry("vibe") - ts)
    assert expiries == [6 * hour, 12 * hour, 24 * hour]

    # Ten in a row never exceed the cap.
    for i in range(3, 13):
        ts = start + i * 6 * hour
        rate_incidents.record_incident(
            agent="vibe",
            surface="dispatch",
            category="quota",
            run_id=f"run-{i}",
            evidence="402 Payment Required: budget exhausted",
            timestamp=ts,
        )
        assert _shed_expiry("vibe") - ts <= rate_incidents.MAX_COOLDOWN_S
    assert _shed_expiry("vibe") - ts == rate_incidents.MAX_COOLDOWN_S

    # In-flight runs failing minutes apart are one exhaustion, not repeated re-entries.
    burst = ts + rate_incidents.ESCALATION_WINDOW_S + 1
    for minute in range(4):
        assert (
            rate_incidents._cooldown_seconds("quota", "burst", burst + minute * 60, None)
            == rate_incidents.DEFAULT_COOLDOWN_S
        )
        rate_incidents.record_incident(
            agent="burst",
            surface="dispatch",
            category="quota",
            run_id=f"burst-{minute}",
            evidence="402 Payment Required: budget exhausted",
            timestamp=burst + minute * 60,
        )
    assert _shed_expiry("burst") - burst - 3 * 60 == rate_incidents.DEFAULT_COOLDOWN_S

    # One quiet window drains the count: the next incident benches for the default again.
    quiet = ts + rate_incidents.ESCALATION_WINDOW_S + 1
    rate_incidents.clear_shed("vibe")
    rate_incidents.record_incident(
        agent="vibe",
        surface="dispatch",
        category="quota",
        run_id="after-quiet",
        evidence="402 Payment Required: budget exhausted",
        timestamp=quiet,
    )
    assert _shed_expiry("vibe") - quiet == rate_incidents.DEFAULT_COOLDOWN_S

    # A provider's stated reset still wins when it is later than the escalated bench.
    later = quiet + 6 * hour
    reset = later + 5 * 24 * hour
    rate_incidents.record_incident(
        agent="vibe",
        surface="dispatch",
        category="quota",
        run_id="with-reset",
        evidence="402 Payment Required: budget exhausted",
        timestamp=later,
        reset_at=reset,
    )
    assert _shed_expiry("vibe") == reset

    monkeypatch.setenv("ORCH_PROVIDER_COOLDOWN_S", "259201")
    cap_ts = reset + 1
    rate_incidents.record_incident(
        agent="override-cap",
        surface="dispatch",
        category="quota",
        run_id="override-1",
        evidence="402 Payment Required: budget exhausted",
        timestamp=cap_ts,
    )
    assert _shed_expiry("override-cap") - cap_ts == rate_incidents.MAX_COOLDOWN_S


def test_dedup_retry_restores_persisted_escalated_expiry(incident_paths, monkeypatch):
    # A dedup retry restores the persisted escalated expiry without extending it.
    monkeypatch.delenv("ORCH_PROVIDER_COOLDOWN_S", raising=False)
    now = int(time.time())
    base = rate_incidents.DEFAULT_COOLDOWN_S
    rate_incidents.record_incident(
        agent="muse",
        surface="dispatch",
        category="quota",
        run_id="earlier-episode",
        timestamp=now - base,
    )
    real_ensure_shed = rate_incidents.ensure_shed
    monkeypatch.setattr(rate_incidents, "ensure_shed", lambda *args, **kwargs: False)
    first = rate_incidents.record_incident(
        agent="muse",
        surface="dispatch",
        category="quota",
        run_id="escalated-episode",
        timestamp=now,
    )
    assert first["status"] == "shed_unfulfilled"
    rows = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    assert rows[-1]["expires_at"] == now + 2 * base

    monkeypatch.setattr(rate_incidents, "ensure_shed", real_ensure_shed)
    retry = rate_incidents.record_incident(
        agent="muse",
        surface="ledger_reconcile.completion",
        category="quota",
        run_id="escalated-episode",
        timestamp=now + base,
    )
    assert retry == {
        "status": "ok",
        "deduped": True,
        "incident_id": first["incident_id"],
        "shed": True,
    }
    assert _shed_expiry("muse") == now + 2 * base
    assert len(rate_incidents.INCIDENT_FILE.read_text().splitlines()) == 2


def test_escalation_is_per_agent(incident_paths, monkeypatch):
    monkeypatch.delenv("ORCH_PROVIDER_COOLDOWN_S", raising=False)
    start = int(time.time())
    hour = 3600
    rate_incidents.record_incident(
        agent="vibe",
        surface="dispatch",
        category="quota",
        run_id="vibe-0",
        evidence="402 Payment Required",
        timestamp=start,
    )
    ts2 = start + 6 * hour
    rate_incidents.record_incident(
        agent="vibe",
        surface="dispatch",
        category="quota",
        run_id="vibe-1",
        evidence="402 Payment Required",
        timestamp=ts2,
    )
    assert _shed_expiry("vibe") - ts2 == 12 * hour
    rate_incidents.record_incident(
        agent="codex",
        surface="dispatch",
        category="quota",
        run_id="codex-1",
        evidence="quota exhausted",
        timestamp=ts2,
    )
    assert _shed_expiry("codex") - ts2 == rate_incidents.DEFAULT_COOLDOWN_S
    # A different category for the same agent does not escalate either.
    rate_incidents.record_incident(
        agent="vibe",
        surface="dispatch",
        category="capacity",
        run_id="vibe-capacity",
        evidence="503 overloaded",
        timestamp=ts2 + 6 * hour,
    )
    assert (
        rate_incidents._cooldown_seconds("capacity", "vibe", ts2 + 12 * hour, None)
        == 2 * rate_incidents.DEFAULT_COOLDOWN_S
    )
