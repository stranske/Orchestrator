"""Pure execution settings controls; no candidate/provider or installed runtime."""

import copy
import json
from pathlib import Path

import pytest

import closer_data_only_execution as execution


@pytest.fixture
def profile():
    return {
        "profile_id": "claude-sonnet-5-5-low",
        "agent": "claude",
        "provider": "anthropic",
        "requested_model": "claude-sonnet-5-5",
        "reasoning_effort": "low",
        "permission_mode": "workspace-write",
        "adapter_version": "claude-cli-profile-v2",
        "prompt_version": "orchestrator-prompt-v1",
        "lifecycle_status": "active",
        "transport_support": ["local", "offload"],
        "capacity_pool_ids": ["claude-subscription"],
        "requires_explicit_selection": False,
    }


@pytest.fixture
def limits():
    # Synthetic test budget, not an approved production role default.
    return {
        "schema": execution.LIMITS_SCHEMA,
        "role": "bounded_classification",
        "model_contract_sha256": "f" * 64,
        "max_prompt_bytes": 65536,
        "max_final_bytes": 8192,
        "max_native_result_bytes": 65536,
        "max_output_tokens": 2048,
        "max_turns": 1,
        "timeout_seconds": 45,
    }


@pytest.fixture
def launch(profile, limits):
    root = "/synthetic/private-cell"
    return {
        "agent": "claude",
        "profile": profile,
        "binary_path": "/synthetic/claude/2.1.296",
        "binary_sha256": execution.CLAUDE_BINARY_SHA256,
        "argv": execution.expected_argv("/synthetic/claude/2.1.296", profile, limits=limits),
        "env": execution.isolated_environment(
            root, cwd=root + "/cwd", existing_oauth_token="synthetic-EXISTING-token", limits=limits
        ),
        "cwd": root + "/cwd",
        "private_root": root,
        "response_protocol": execution.NATIVE_JSON,
        "limits": limits,
        "timeout_seconds": 45,
        "prompt": "Synthetic complete prompt; no inherited/default execution budget.",
        "source_sha256": {
            "adapters.py": "a" * 64,
            "dispatcher.py": "b" * 64,
            "closer_data_only_execution.py": "c" * 64,
        },
    }


def test_exact_controls_argv_protocol_and_nonqualifying_binding(launch):
    before = copy.deepcopy(launch)
    receipt = execution.validate_launch(**launch)
    config, binding = receipt["config"], receipt["binding"]
    assert config["control_presence"] == {
        key: True for key in execution.environment_controls(launch["limits"])
    }
    assert config["controls"] == {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "2048",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
        "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",
        "CLAUDE_CODE_DISABLE_REFUSAL_RETRY": "1",
    }
    assert config["argv"][-8:] == [
        "--model",
        "claude-sonnet-5-5",
        "--effort",
        "low",
        "--output-format",
        "json",
        "--max-turns",
        "1",
    ]
    assert config["prompt_transport"] == "stdin"
    assert config["response_protocol"] == execution.NATIVE_JSON
    assert binding["activation"] is binding["role_qualification"] is False
    assert binding["served_model"] == binding["served_effort"] == "UNKNOWN"
    assert binding["entitlement"] == "UNKNOWN"
    assert binding["profile_identity"]["capacity_pool_ids"] == ["claude-subscription"]
    assert launch == before
    assert execution.validate_launch(**launch) == receipt


@pytest.mark.parametrize("field", [*execution.CLAUDE_CONTROLS, "CLAUDE_CODE_MAX_OUTPUT_TOKENS"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_each_actual_control_is_required_not_ambient_default(launch, field, damage):
    if damage == "missing":
        del launch["env"][field]
    else:
        launch["env"][field] = "unreviewed"
    with pytest.raises(ValueError, match="environment"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "HTTP_PROXY", "NODE_OPTIONS", "BASH_ENV"])
def test_extra_ambient_environment_is_rejected(launch, key):
    launch["env"][key] = "ambient"
    with pytest.raises(ValueError, match="environment"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize("key", ["HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "PATH"])
def test_private_paths_and_fixed_path_cannot_be_widened(launch, key):
    launch["env"][key] = "/ambient"
    with pytest.raises(ValueError, match="environment"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize("append", [["--resume", "old"], ["hostile prompt"], ["--tools", "Bash"]])
def test_shell_prompt_resume_or_tool_argv_cannot_be_added(launch, append):
    launch["argv"].extend(append)
    with pytest.raises(ValueError, match="argv"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--output-format", "text"),
        ("--max-turns", "2"),
        ("--effort", "high"),
        ("--model", "claude-opus-5-5"),
    ],
)
def test_actual_argv_cannot_drift_from_explicit_profile_or_protocol(launch, flag, value):
    launch["argv"][launch["argv"].index(flag) + 1] = value
    with pytest.raises(ValueError, match="argv"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize("protocol", [execution.LEGACY_TEXT, "stream-json", "", None])
def test_native_json_is_never_guessed_from_production_text(launch, protocol):
    launch["response_protocol"] = protocol
    with pytest.raises(ValueError, match="protocol"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent", "gemini"),
        ("requested_model", "claude-opus-latest"),
        ("reasoning_effort", "default"),
        ("adapter_version", "claude-cli-profile-v1"),
        ("lifecycle_status", "retired"),
        ("capacity_pool_ids", ["invented-model-pool"]),
        ("requires_explicit_selection", 0),
        ("transport_support", ["local"]),
    ],
)
def test_profile_fields_are_explicit_and_do_not_enable_other_transports(launch, field, value):
    launch["profile"][field] = value
    with pytest.raises(ValueError):
        execution.validate_launch(**launch)


def test_no_implicit_profile_effort_or_binary_build(launch):
    del launch["profile"]["reasoning_effort"]
    with pytest.raises(ValueError):
        execution.validate_launch(**launch)
    launch["profile"]["reasoning_effort"] = "low"
    launch["binary_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="binary"):
        execution.validate_launch(**launch)


@pytest.mark.parametrize(
    "path", ["relative", "/", "/private/../home", "/private//cell", "/bad\ncell"]
)
def test_private_root_requires_normalized_absolute_lexical_paths(path, limits):
    with pytest.raises(ValueError):
        execution.isolated_environment(
            path, cwd=path + "/cwd", existing_oauth_token="synthetic", limits=limits
        )


def test_controls_and_results_are_independent_copies(launch):
    with pytest.raises(TypeError):
        execution.CLAUDE_CONTROLS["CLAUDE_CODE_MAX_RETRIES"] = "1"
    first = execution.validate_launch(**launch)
    first["config"]["controls"].clear()
    first["binding"]["profile_identity"]["capacity_pool_ids"].clear()
    assert execution.validate_launch(**launch)["config"]["controls"]


def test_credential_value_is_neither_retained_nor_hashed(launch):
    token = launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    first = execution.validate_launch(**launch)
    assert token not in json.dumps(first)
    launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"] = "different-existing-synthetic-token"
    assert execution.validate_launch(**launch) == first
    del launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    with pytest.raises(ValueError):
        execution.validate_launch(**launch)


@pytest.mark.parametrize("field", ["prompt", "profile", "path"])
def test_misplaced_credential_is_refused_before_any_binding_hash(launch, monkeypatch, field):
    token = launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    if field == "prompt":
        launch["prompt"] = token
    elif field == "profile":
        launch["profile"]["prompt_version"] = token
    else:
        launch["private_root"] = "/private/" + token
        launch["cwd"] = launch["private_root"] + "/cwd"
        launch["env"] = execution.isolated_environment(
            launch["private_root"],
            cwd=launch["cwd"],
            existing_oauth_token=token,
            limits=launch["limits"],
        )

    def forbidden(*args, **kwargs):
        pytest.fail("credential-containing data was hashed")

    monkeypatch.setattr(execution.hashlib, "sha256", forbidden)
    with pytest.raises(ValueError, match="borrowed credential"):
        execution.validate_launch(**launch)


def test_binding_changes_when_actual_nonsecret_inputs_change(launch):
    first = execution.validate_launch(**launch)
    launch["profile"]["prompt_version"] = "orchestrator-prompt-v2"
    second = execution.validate_launch(**launch)
    assert second["binding_sha256"] != first["binding_sha256"]
    launch["source_sha256"]["dispatcher.py"] = "d" * 64
    assert execution.validate_launch(**launch)["binding_sha256"] != second["binding_sha256"]


@pytest.mark.parametrize("damage", ["missing", "extra", "malformed"])
def test_code_pins_require_exact_declared_sources(launch, damage):
    if damage == "missing":
        del launch["source_sha256"]["dispatcher.py"]
    elif damage == "extra":
        launch["source_sha256"]["ambient.py"] = "d" * 64
    else:
        launch["source_sha256"]["dispatcher.py"] = float("nan")
    with pytest.raises(ValueError, match="source pins"):
        execution.validate_launch(**launch)


def test_opus_high_is_bound_without_mutating_low_defaults(launch):
    launch["profile"].update(
        profile_id="claude-opus-5-5-high",
        requested_model="claude-opus-5-5",
        reasoning_effort="high",
    )
    launch["argv"] = execution.expected_argv(
        launch["binary_path"], launch["profile"], limits=launch["limits"]
    )
    receipt = execution.validate_launch(**launch)
    assert receipt["binding"]["profile_identity"]["requested_model"] == "claude-opus-5-5"
    assert receipt["config"]["argv"][-5] == "high"


def test_pure_helpers_never_read_files_environment_or_spawn(launch, monkeypatch):
    import builtins
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("pure launch contract attempted ambient IO or execution")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-never-enter-child")
    assert execution.validate_launch(**launch)["binding"]["activation"] is False
    assert "ANTHROPIC_API_KEY" not in execution.isolated_environment(
        launch["private_root"],
        cwd=launch["cwd"],
        existing_oauth_token="synthetic",
        limits=launch["limits"],
    )


@pytest.mark.parametrize(
    "key",
    [
        "max_prompt_bytes",
        "max_final_bytes",
        "max_native_result_bytes",
        "max_output_tokens",
        "max_turns",
        "timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, float("inf"), float("nan"), None])
def test_limits_are_explicit_positive_integers(limits, key, value):
    limits[key] = value
    with pytest.raises(ValueError):
        execution.validate_limits(limits)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_prompt_bytes", 65537),
        ("max_native_result_bytes", 65537),
        ("max_output_tokens", 65537),
        ("max_turns", 2),
        ("max_final_bytes", 65537),
        ("timeout_seconds", 901),
        ("role", "constrained_repair"),
        ("model_contract_sha256", "x"),
    ],
)
def test_limits_cannot_escape_reviewed_domain(limits, key, value):
    limits[key] = value
    with pytest.raises(ValueError):
        execution.validate_limits(limits)


def test_no_output_cap_defaults_or_unknown_descriptor_fields(limits, profile):
    with pytest.raises(TypeError):
        execution.expected_argv("/synthetic/claude", profile)
    del limits["max_output_tokens"]
    with pytest.raises(ValueError):
        execution.validate_limits(limits)
    limits["max_output_tokens"] = 2048
    limits["fallback"] = "ambient"
    with pytest.raises(ValueError):
        execution.validate_limits(limits)


def test_actual_full_classification_response_uses_explicit_final_byte_budget(tmp_path, limits):
    from test_closer_disposition import fixture

    _snap, _packet, _ref, _prep, result = fixture(tmp_path)
    assert sum(len(row["proofs"]) for row in result["proposal"]["decision"]["criteria"]) == 2
    final = json.dumps(result["proposal"], separators=(",", ":"))
    assert len(final.encode()) > 512  # Full inventory/evidence response, not a C1 answer.
    proof = execution.validate_payload(final, limits=limits, kind="final")
    assert proof["utf8_bytes"] == len(final.encode())
    limits["max_final_bytes"] = 512
    with pytest.raises(ValueError, match="final"):
        execution.validate_payload(final, limits=limits, kind="final")


@pytest.mark.parametrize("role", ["bounded_classification", "strong_criterion_adjudication"])
def test_initial_source_regime_is_explicit_for_each_supported_role(launch, role):
    launch["limits"]["role"] = role
    receipt = execution.validate_launch(**launch)
    assert receipt["binding"]["role"] == role
    assert receipt["config"]["limits"] == launch["limits"]
    assert receipt["config"]["controls"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "2048"


def test_complete_prompt_utf8_budget_and_role_limits_are_bound(launch):
    first = execution.validate_launch(**launch)
    launch["limits"]["max_output_tokens"] = 1024
    with pytest.raises(ValueError, match="environment"):
        execution.validate_launch(**launch)
    launch["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "1024"
    second = execution.validate_launch(**launch)
    assert second["binding"]["limits_sha256"] != first["binding"]["limits_sha256"]
    assert second["binding_sha256"] != first["binding_sha256"]
    launch["prompt"] = "😀" * 16385
    with pytest.raises(ValueError, match="prompt"):
        execution.validate_launch(**launch)


def test_payload_kind_never_guesses_native_vs_final(limits):
    limits["max_final_bytes"] = 10
    assert execution.validate_payload("x" * 11, limits=limits, kind="native_result")
    with pytest.raises(ValueError):
        execution.validate_payload("x" * 11, limits=limits, kind="final")
    for kind, text in [("auto", "{}"), ("final", ""), ("prompt", "\ud800")]:
        with pytest.raises(ValueError):
            execution.validate_payload(text, limits=limits, kind=kind)


@pytest.mark.parametrize(
    "timeout", [0, -1, True, 45.001, float("inf"), float("nan"), None, 10**1000]
)
def test_actual_supervisor_timeout_must_fit_same_role_descriptor(launch, timeout):
    launch["timeout_seconds"] = timeout
    with pytest.raises(ValueError, match="timeout"):
        execution.validate_launch(**launch)


def test_remaining_request_time_can_narrow_without_widening_qualification_budget(launch):
    original = execution.validate_launch(**launch)
    launch["timeout_seconds"] = 1.25
    narrowed = execution.validate_launch(**launch)
    assert narrowed["config"]["timeout_seconds"] == 1.25
    assert narrowed["binding"]["limits_sha256"] == original["binding"]["limits_sha256"]
    assert narrowed["binding"]["config_sha256"] != original["binding"]["config_sha256"]
