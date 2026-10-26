"""Pure, nonactivating import of one saved Claude native JSON result.

No CLI is launched and no credentials, settings, registry or quota store is read.
Imported bytes are untrusted claims, even when their shape matches native output.

Schema evidence: installed Claude 2.1.296 embedded SDK result/modelUsage schemas,
SHA256 c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937.
The success result has result/is_error/num_turns/stop_reason/terminal_reason,
usage, total_cost_usd and modelUsage. Its modelUsage keys are raw model ids;
canonicalModel is explicitly a pricing lookup id, not a served identity. Totals
include query-pipeline helpers and can span earlier resumed turns. They are
estimates, not billing statements. Native usage can be per-turn or cumulative,
so it must not be equated with cumulative modelUsage. Native success subtype can
also accompany turn_setup_failed; success alone is not completion evidence.
Existing adapters._claude_models_in_jsonl likewise reads CLI model fields rather
than requested defaults; ledger_reconcile documents native total_cost_usd.

This narrower importer accepts only explicit completed/end_turn single-model
results. It does not authenticate those claims or prove no hidden fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any

from transport_qualification import strict_final

SCHEMA = "closer-saved-claude-result/v1"
TOKEN_FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
}
# Refuse routing aliases, preserving the native spelling without canonicalization.
MODEL_ID = re.compile(r"claude-[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")
GAPS = (
    "saved capture provenance and original run/pool binding are unauthenticated",
    "tools/MCP/hooks/ambient project context confinement unestablished",
    "served reasoning effort UNKNOWN: native result has no supported observation field",
    "quota balance and subscription-pool charge unmeasured; dollars are CLI estimates",
    "single modelUsage entry cannot prove absence of unreported fallback or helper calls",
    "cumulative modelUsage may include resumed history; original-run-only attribution unestablished",
)


def _label(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 256
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"{name} must be a bounded nonempty caller-supplied label")
    return value


def _tokens(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _cost(value: Any, name: str) -> int | float:
    try:
        valid = type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite nonnegative dollar amount")
    return value


def _counters(value: Any, fields: dict[str, str], name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {out: _tokens(value.get(key), f"{name}.{key}") for out, key in fields.items()}


def normalize_claude_result(
    text: str,
    *,
    run_id: str,
    pool_id: str,
    requested_model: str,
    requested_effort: str,
) -> dict[str, Any]:
    """Validate one saved native result and return unverified imported evidence.

    run_id/pool_id bind the caller's original run and real pool labels. Their
    truth and attribution need independent review; no source attestation is
    inferred. No retry, accounting mutation, qualification or activation occurs.
    """
    run_id = _label(run_id, "run_id")
    pool_id = _label(pool_id, "pool_id")
    requested_effort = _label(requested_effort, "requested_effort")
    requested_model = _label(requested_model, "requested_model")
    row = strict_final(text)
    if "local_command" in row and row["local_command"] is not False:
        raise ValueError("local command marker cannot establish a model evaluation")
    if (
        row.get("type") != "result"
        or row.get("subtype") != "success"
        or row.get("is_error") is not False
        or row.get("terminal_reason") != "completed"
        or row.get("stop_reason") != "end_turn"
    ):
        raise ValueError("saved result must explicitly report completed successful end_turn")
    turns = _tokens(row.get("num_turns"), "num_turns")
    if turns == 0:
        raise ValueError("local command/status with num_turns=0 is not a model evaluation")
    final = row.get("result")
    if not isinstance(final, str) or not final.strip():
        raise ValueError("completed final assistant result text is required")
    session_id = _label(row.get("session_id"), "native session_id")
    if row.get("errors", []) != [] or row.get("deferred_tool_use") is not None:
        raise ValueError("failed or deferred result cannot establish a completed final response")
    # Refuse extra event envelopes, explicit fallback claims and uncertain runner exits.
    for key in (
        "events",
        "messages",
        "tool_calls",
        "runner_exit",
        "fallback_model",
        "fallbackModel",
        "outcome_uncertain",
        "retry_safe",
        "exit",
        "exit_code",
    ):
        if key in row:
            raise ValueError(f"unsupported event/fallback/runner field: {key}")
    models = row.get("modelUsage")
    if not isinstance(models, dict) or len(models) != 1:
        raise ValueError("exactly one served modelUsage entry is required; fallback is unsupported")
    served_model = next(iter(models))
    if (
        not MODEL_ID.fullmatch(served_model)
        or not any(c.isdigit() for c in served_model)
        or any(part in {"latest", "default", "auto"} for part in re.split(r"[._-]", served_model))
    ):
        raise ValueError("modelUsage key must be an explicit versioned Claude model id")
    if served_model != requested_model:
        raise ValueError("served modelUsage key differs from exact requested model")
    # A top-level model is not part of the reviewed result schema. Never use it
    # to fill missing modelUsage; reject conflicting ancillary identity claims.
    if "model" in row and row["model"] != served_model:
        raise ValueError("ancillary model field conflicts with modelUsage")
    entry = models[served_model]
    tokens = _counters(entry, TOKEN_FIELDS, "modelUsage")
    if (
        tokens["output_tokens"] == 0
        or sum(v for k, v in tokens.items() if k != "output_tokens") == 0
    ):
        raise ValueError("model evaluation requires reported input and output token activity")
    native_usage_tokens = _counters(row.get("usage"), {k: k for k in TOKEN_FIELDS}, "usage")
    for key in ("thinkingTokens", "webSearchRequests", "contextWindow", "maxOutputTokens"):
        if key in entry:
            _tokens(entry[key], f"modelUsage.{key}")
    if entry.get("thinkingTokens", 0) > tokens["output_tokens"]:
        raise ValueError("thinkingTokens exceeds its containing outputTokens")
    total_cost = _cost(row.get("total_cost_usd"), "total_cost_usd")
    model_cost = _cost(entry.get("costUSD"), "modelUsage.costUSD")
    if not math.isclose(total_cost, model_cost, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("single-model reported costs disagree")
    return {
        "schema": SCHEMA,
        "status": "unverified_imported_capture",
        "independent_review_required": True,
        "activation": False,
        "production_qualification_records_written": 0,
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "binding": {"run_id": run_id, "pool_id": pool_id, "source": "caller_supplied_labels"},
        "native_session_id": session_id,
        "requested": {"model": requested_model, "effort": requested_effort},
        "observed": {
            "model": served_model,
            "identity_source": "unverified_saved_cli_modelUsage_key",
            "effort": "UNKNOWN",
            "effort_source": "missing_native_result_evidence",
        },
        "response": {
            "completed": True,
            "completion_source": "unverified_saved_cli_result",
            "final": final,
            "num_turns": turns,
        },
        "usage": {
            **tokens,
            "reported_cost_usd": total_cost,
            "reported_model_cost_usd": model_cost,
            "source": "unverified_saved_cli_modelUsage_estimate",
            "scope": "native_cumulative_query_pipeline_totals",
            "native_result_usage_tokens": native_usage_tokens,
            "native_result_usage_scope": "unverified_per_turn_or_cumulative",
            "quota_balance": "UNKNOWN",
            "pool_charge": "UNKNOWN",
            "provider_attested": False,
        },
        "missing_evidence": list(GAPS),
    }


def selftest() -> dict[str, Any]:
    """Inert synthetic pipeline controls; never provider evidence."""
    model = "claude-opus-4-7"
    row: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "stop_reason": "end_turn",
        "num_turns": 1,
        "session_id": "synthetic-selftest-session",
        "result": '{"verdict":"UNKNOWN"}',
        "usage": {k: 1 for k in TOKEN_FIELDS},
        "total_cost_usd": 0.01,
        "modelUsage": {model: {**{v: 1 for v in TOKEN_FIELDS.values()}, "costUSD": 0.01}},
    }
    kwargs = dict(
        run_id="synthetic-run",
        pool_id="synthetic-pool",
        requested_model=model,
        requested_effort="high",
    )
    report = normalize_claude_result(json.dumps(row), **kwargs)
    checks = {
        "single_native_model_preserved": report["observed"]["model"] == model,
        "requested_effort_not_observed": report["observed"]["effort"] == "UNKNOWN",
        "never_activates": report["activation"] is False,
        "never_attests_provider": report["usage"]["provider_attested"] is False,
        "gaps_retained": len(report["missing_evidence"]) == len(GAPS),
    }
    for name, key, value in (
        ("local_command_rejected", "num_turns", 0),
        ("uncertain_completion_rejected", "terminal_reason", "aborted_tools"),
        ("malformed_usage_rejected", "modelUsage", {}),
    ):
        broken = deepcopy(row)
        broken[key] = value
        try:
            normalize_claude_result(json.dumps(broken), **kwargs)
        except ValueError:
            checks[name] = True
        else:
            checks[name] = False
    if not all(checks.values()):
        raise AssertionError(checks)
    return {
        "synthetic_pipeline_controls_only": True,
        "checked_assertions": checks,
        "activation": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(selftest(), sort_keys=True))


if __name__ == "__main__":
    main()
