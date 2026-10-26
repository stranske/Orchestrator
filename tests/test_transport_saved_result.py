"""Synthetic saved-native-result pipeline controls, never provider evidence."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

import transport_saved_result as saved

FIXTURES = Path(__file__).parent / "fixtures" / "transport_saved_result"
MODEL = "claude-opus-4-7"
BINDING = {
    "run_id": "original-run-123",
    "pool_id": "caller-real-pool-id",
    "requested_model": MODEL,
    "requested_effort": "high",
}


@pytest.fixture
def native():
    return json.loads((FIXTURES / "completed-synthetic.json").read_text())


def normalize(row, **overrides):
    return saved.normalize_claude_result(json.dumps(row), **{**BINDING, **overrides})


def test_completed_capture_preserves_native_fields_but_cannot_activate(native):
    text = json.dumps(native)
    report = saved.normalize_claude_result(text, **BINDING)
    assert report["status"] == "unverified_imported_capture"
    assert report["independent_review_required"] is True
    assert report["activation"] is False
    assert report["production_qualification_records_written"] == 0
    assert report["binding"] == {
        "run_id": BINDING["run_id"],
        "pool_id": BINDING["pool_id"],
        "source": "caller_supplied_labels",
    }
    assert report["native_session_id"] == native["session_id"]
    assert report["response"]["final"] == native["result"]
    assert report["observed"]["model"] == MODEL
    assert report["requested"]["effort"] == "high"
    assert report["observed"]["effort"] == "UNKNOWN"
    assert "provider_observed" not in json.dumps(report)
    assert report["usage"]["input_tokens"] == 10
    assert report["usage"]["output_tokens"] == 20
    assert report["usage"]["cache_read_input_tokens"] == 30
    assert report["usage"]["cache_creation_input_tokens"] == 40
    assert report["usage"]["reported_cost_usd"] == 0.0123
    assert report["usage"]["provider_attested"] is False
    assert report["usage"]["pool_charge"] == "UNKNOWN"
    assert report["usage"]["quota_balance"] == "UNKNOWN"
    assert any("confinement" in gap for gap in report["missing_evidence"])
    assert any("served reasoning effort" in gap for gap in report["missing_evidence"])
    assert any("quota balance" in gap for gap in report["missing_evidence"])
    assert any("resumed history" in gap for gap in report["missing_evidence"])
    assert len(report["source_sha256"]) == 64
    assert normalize(native) == report


def test_native_status_local_command_is_not_an_evaluation():
    text = (FIXTURES / "local-status-synthetic.json").read_text()
    with pytest.raises(ValueError):
        saved.normalize_claude_result(text, **BINDING)


@pytest.mark.parametrize(
    "key,value",
    [
        ("type", "assistant"),
        ("type", "tool_result"),
        ("type", "stream_event"),
        ("subtype", "error_max_turns"),
        ("is_error", True),
        ("is_error", 0),
        ("num_turns", 0),
        ("num_turns", True),
        ("num_turns", 1.0),
        ("terminal_reason", "aborted_streaming"),
        ("terminal_reason", "turn_setup_failed"),
        ("terminal_reason", "hook_stopped"),
        ("terminal_reason", None),
        ("stop_reason", "max_tokens"),
        ("stop_reason", "tool_use"),
        ("stop_reason", None),
        ("result", ""),
        ("result", []),
        ("session_id", ""),
        ("errors", ["failure"]),
        ("deferred_tool_use", {"name": "shell"}),
        ("events", []),
        ("messages", []),
        ("tool_calls", []),
        ("runner_exit", {}),
        ("fallback_model", MODEL),
        ("fallbackModel", MODEL),
        ("model", "claude-sonnet-4-6"),
    ],
)
def test_uncertain_nonfinal_failed_and_event_envelopes_refuse(native, key, value):
    native[key] = value
    with pytest.raises(ValueError):
        normalize(native)


@pytest.mark.parametrize("key", ["terminal_reason", "stop_reason", "is_error", "num_turns"])
def test_missing_completion_evidence_refuses(native, key):
    del native[key]
    with pytest.raises(ValueError):
        normalize(native)


def test_model_key_is_not_inferred_from_request_or_pricing_canonical_model(native):
    native["modelUsage"] = {}
    native["model"] = MODEL
    with pytest.raises(ValueError):
        normalize(native)
    entry = {"canonicalModel": MODEL}
    native["modelUsage"] = {"default": entry}
    with pytest.raises(ValueError):
        normalize(native)


@pytest.mark.parametrize(
    "key", ["opus", "claude-opus-latest", "claude-opus", "auto", "<synthetic>"]
)
def test_alias_model_usage_key_refuses_even_when_requested_matches(native, key):
    native["modelUsage"] = {key: native["modelUsage"][MODEL]}
    with pytest.raises(ValueError):
        normalize(native, requested_model=key)


def test_exact_model_mismatch_and_multiple_models_refuse(native):
    with pytest.raises(ValueError, match="differs"):
        normalize(native, requested_model="claude-sonnet-4-6")
    native["modelUsage"]["claude-sonnet-4-6"] = deepcopy(native["modelUsage"][MODEL])
    with pytest.raises(ValueError, match="exactly one"):
        normalize(native)


def test_pricing_identity_and_arbitrary_attestation_fields_cannot_override_observations(native):
    native["modelUsage"][MODEL]["canonicalModel"] = "claude-sonnet-4-6"
    native["observed_effort"] = "high"
    native["provider_observed"] = True
    report = normalize(native)
    assert report["observed"]["model"] == MODEL
    assert report["observed"]["effort"] == "UNKNOWN"
    assert report["usage"]["provider_attested"] is False


def test_cumulative_model_usage_is_not_replaced_by_native_result_usage(native):
    native["usage"]["input_tokens"] = 1
    report = normalize(native)
    assert report["usage"]["input_tokens"] == 10
    assert report["usage"]["native_result_usage_tokens"]["input_tokens"] == 1
    assert report["usage"]["scope"] == "native_cumulative_query_pipeline_totals"
    assert report["usage"]["native_result_usage_scope"] == "unverified_per_turn_or_cumulative"


@pytest.mark.parametrize("field", list(saved.TOKEN_FIELDS.values()))
@pytest.mark.parametrize("bad", [-1, True, 1.0, "1", None, [], {}])
def test_all_model_token_counters_require_nonnegative_integers(native, field, bad):
    native["modelUsage"][MODEL][field] = bad
    with pytest.raises(ValueError):
        normalize(native)


@pytest.mark.parametrize("field", list(saved.TOKEN_FIELDS))
@pytest.mark.parametrize("bad", [-1, False, 1.5, "1", None])
def test_terminal_turn_usage_is_also_validated(native, field, bad):
    native["usage"][field] = bad
    with pytest.raises(ValueError):
        normalize(native)


@pytest.mark.parametrize("where,key", [("root", "total_cost_usd"), ("model", "costUSD")])
@pytest.mark.parametrize("bad", [-0.1, True, "0.1", None, float("nan"), float("inf")])
def test_dollar_estimates_are_finite_nonnegative_numbers(native, where, key, bad):
    target = native if where == "root" else native["modelUsage"][MODEL]
    target[key] = bad
    with pytest.raises(ValueError):
        normalize(native)


def test_missing_or_disagreeing_cost_and_usage_refuse(native):
    for target, key in [(native, "total_cost_usd"), (native["modelUsage"][MODEL], "inputTokens")]:
        old = target.pop(key)
        with pytest.raises(ValueError):
            normalize(native)
        target[key] = old
    native["total_cost_usd"] = 2
    with pytest.raises(ValueError, match="costs disagree"):
        normalize(native)


def test_zero_cost_is_retained_without_implying_zero_quota_charge(native):
    native["total_cost_usd"] = native["modelUsage"][MODEL]["costUSD"] = 0
    report = normalize(native)
    assert report["usage"]["reported_cost_usd"] == 0
    assert report["usage"]["pool_charge"] == "UNKNOWN"


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "null",
        "{}\n{}",
        '{"type":"result","type":"result"}',
        '{"usage":{"input_tokens":1,"input_tokens":2}}',
        '{"value":NaN}',
        '{"value":1e999}',
        "```json\n{}\n```",
        "x" * 65537,
    ],
)
def test_strict_saved_json_rejects_streams_duplicates_nonfinite_and_oversize(text):
    with pytest.raises(ValueError):
        saved.normalize_claude_result(text, **BINDING)


@pytest.mark.parametrize("field", ["run_id", "pool_id", "requested_model", "requested_effort"])
@pytest.mark.parametrize("bad", ["", " padded ", "line\nbreak", None])
def test_caller_binding_labels_must_be_explicit(native, field, bad):
    with pytest.raises(ValueError):
        normalize(native, **{field: bad})


def test_parser_does_not_mutate_input_or_read_settings(native, monkeypatch):
    original = deepcopy(native)
    text = json.dumps(native)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure parser attempted file access")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    report = saved.normalize_claude_result(text, **BINDING)
    assert native == original
    assert report["activation"] is False


def test_selftest_reports_checked_controls_without_provider_claims(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["transport_saved_result.py", "--selftest"])
    saved.main()
    report = json.loads(capsys.readouterr().out)
    assert report["synthetic_pipeline_controls_only"] is True
    assert len(report["checked_assertions"]) >= 8
    assert all(report["checked_assertions"].values())
    assert report["activation"] is False
