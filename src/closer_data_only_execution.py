"""Pure Claude data-only launch contract; no execution or qualification authority.

Controls/argv match reviewed launcher 64a2b140, not an assertion about an installed
deployment. Callers create and verify private directories, rehash the real binary,
qualify the exact role/profile and retain their existing pool/process fences.
No filesystem, environment, credential store, registry or provider is accessed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

SCHEMA = "closer-data-only-execution/v1"
NATIVE_JSON = "claude-native-json-result/v1"
LEGACY_TEXT = "production-deliverable-text/v1"
CLAUDE_BINARY_SHA256 = "c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937"
CLAUDE_CONTROLS: Mapping[str, str] = MappingProxyType(
    {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
        "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",
        "CLAUDE_CODE_DISABLE_REFUSAL_RETRY": "1",
    }
)
_DIRECTORIES = {
    "HOME": "home",
    "XDG_CACHE_HOME": "cache",
    "XDG_CONFIG_HOME": "config",
    "TMPDIR": "tmp",
    "CLAUDE_CONFIG_DIR": "claude-config",
}
_PROFILE_TEXT = (
    "profile_id",
    "agent",
    "provider",
    "requested_model",
    "reasoning_effort",
    "permission_mode",
    "adapter_version",
    "prompt_version",
    "lifecycle_status",
)
_SOURCE_FILES = frozenset({"adapters.py", "dispatcher.py", "closer_data_only_execution.py"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
LIMITS_SCHEMA = "closer-data-only-limits/v1"
_LIMIT_FIELDS = frozenset(
    {
        "schema",
        "role",
        "model_contract_sha256",
        "max_prompt_bytes",
        "max_final_bytes",
        "max_native_result_bytes",
        "max_output_tokens",
        "max_turns",
        "timeout_seconds",
    }
)


def _text(value: Any, name: str, *, maximum: int = 4096) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"{name} must be bounded explicit text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} contains invalid Unicode") from exc
    return value


def _path(value: Any, name: str) -> str:
    value = _text(value, name)
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or value == "/"
        or ".." in path.parts
        or str(path) != value
    ):
        raise ValueError(f"{name} must be an absolute normalized non-root path")
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _contains_credential(value: Any, credential: str) -> bool:
    if isinstance(value, str):
        return credential in value
    if isinstance(value, dict):
        return any(
            _contains_credential(key, credential) or _contains_credential(child, credential)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_credential(child, credential) for child in value)
    return False


def validate_limits(limits: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit caller-approved limits, never select production defaults.

    These ceilings bound this contract's input domain, not model capability. The
    role-qualification consumer must bind the SAME descriptor/hash and independently
    match model_contract_sha256 to its reviewed whole prompt/response contract.
    """
    if not isinstance(limits, Mapping) or set(limits) != _LIMIT_FIELDS:
        raise ValueError("an exact explicit role/contract limits descriptor is required")
    if (
        limits["schema"] != LIMITS_SCHEMA
        or type(limits["role"]) is not str
        or limits["role"] not in {"bounded_classification", "strong_criterion_adjudication"}
        or type(limits["model_contract_sha256"]) is not str
        or not _SHA.fullmatch(limits["model_contract_sha256"])
    ):
        raise ValueError("limits require an explicit data-only role and model contract digest")
    for key in (
        "max_prompt_bytes",
        "max_final_bytes",
        "max_native_result_bytes",
        "max_output_tokens",
        "max_turns",
        "timeout_seconds",
    ):
        if type(limits[key]) is not int or limits[key] <= 0:
            raise ValueError(f"{key} must be an explicit positive integer")
    if (
        limits["max_prompt_bytes"] > 65536
        or limits["max_native_result_bytes"] > 65536
        or limits["max_final_bytes"] > limits["max_native_result_bytes"]
        or limits["max_output_tokens"] > 65536
        or limits["max_turns"] != 1
        or limits["timeout_seconds"] > 900
    ):
        raise ValueError("limits exceed the bounded native JSON execution contract")
    return dict(limits)


def environment_controls(limits: Mapping[str, Any]) -> dict[str, str]:
    """Fixed verified controls plus the explicit role's output-token bound."""
    checked = validate_limits(limits)
    return {**CLAUDE_CONTROLS, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(checked["max_output_tokens"])}


def validate_payload(text: str, *, limits: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Bind exact complete prompt/final/native bytes; never infer their protocol.

    Size validation is not JSON syntax or semantic validation. Only the caller's
    explicit native collector may extract a final result before kind='final'.
    """
    checked = validate_limits(limits)
    fields = {
        "prompt": "max_prompt_bytes",
        "final": "max_final_bytes",
        "native_result": "max_native_result_bytes",
    }
    if type(kind) is not str or kind not in fields or type(text) is not str:
        raise ValueError("explicit text and prompt/final/native_result kind required")
    try:
        raw = text.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("payload contains invalid Unicode") from exc
    if not 0 < len(raw) <= checked[fields[kind]]:
        raise ValueError(f"complete {kind} payload exceeds explicit byte limits or is empty")
    return {"kind": kind, "utf8_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def profile_identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Project supplied registry fields; never infer a profile/model/effort default."""
    if not isinstance(profile, Mapping):
        raise ValueError("an explicit profile record is required")
    identity: dict[str, Any] = {
        key: _text(profile.get(key), key, maximum=256) for key in _PROFILE_TEXT
    }
    if (
        identity["agent"] != "claude"
        or identity["provider"] != "anthropic"
        or identity["requested_model"] not in {"claude-sonnet-5-5", "claude-opus-5-5"}
        or identity["reasoning_effort"] not in {"low", "medium", "high", "xhigh", "max"}
        or identity["adapter_version"] != "claude-cli-profile-v2"
        or identity["lifecycle_status"] != "active"
        or identity["permission_mode"] not in {"read-only", "workspace-write"}
    ):
        raise ValueError("profile is outside the reviewed Claude execution contract")
    for key in ("transport_support", "capacity_pool_ids"):
        values = profile.get(key)
        if type(values) is not list or not 1 <= len(values) <= 16:
            raise ValueError(f"{key} must be an explicit bounded list")
        normalized = [_text(value, key, maximum=128) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"duplicate {key}")
        identity[key] = sorted(normalized)
    if "offload" not in identity["transport_support"] or identity["capacity_pool_ids"] != [
        "claude-subscription"
    ]:
        raise ValueError("offload transport and the actual shared Claude pool are required")
    if type(profile.get("requires_explicit_selection")) is not bool:
        raise ValueError("requires_explicit_selection must be explicit boolean metadata")
    identity["requires_explicit_selection"] = profile["requires_explicit_selection"]
    return identity


def isolated_environment(
    private_root: str, *, cwd: str, existing_oauth_token: str, limits: Mapping[str, Any]
) -> dict[str, str]:
    """Return a fresh allowlist from explicit inputs; no ambient/default credentials.

    This does not create directories or authenticate the existing supplied token.
    The caller must enforce physical empty 0700 paths before using this mapping.
    """
    root = _path(private_root, "private_root")
    if _path(cwd, "cwd") != f"{root}/cwd":
        raise ValueError("cwd must be the private root's isolated cwd child")
    token = _text(existing_oauth_token, "existing OAuth token", maximum=16384)
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        **environment_controls(limits),
        **{key: f"{root}/{leaf}" for key, leaf in _DIRECTORIES.items()},
        "CLAUDE_CODE_OAUTH_TOKEN": token,
    }


def expected_argv(
    binary_path: str, profile: Mapping[str, Any], *, limits: Mapping[str, Any]
) -> list[str]:
    """Exact reviewed native-JSON stdin argv; shell commands/prompts are excluded."""
    identity = profile_identity(profile)
    checked = validate_limits(limits)
    return [
        _path(binary_path, "binary_path"),
        "-p",
        "--input-format",
        "text",
        "--safe-mode",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
        "--settings",
        '{"disableAllHooks":true}',
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model",
        identity["requested_model"],
        "--effort",
        identity["reasoning_effort"],
        "--output-format",
        "json",
        "--max-turns",
        str(checked["max_turns"]),
    ]


def validate_launch(
    *,
    agent: str,
    profile: Mapping[str, Any],
    binary_path: str,
    binary_sha256: str,
    argv: list[str],
    env: Mapping[str, str],
    cwd: str,
    private_root: str,
    response_protocol: str,
    source_sha256: Mapping[str, str],
    limits: Mapping[str, Any],
    prompt: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Validate actual caller-supplied launch inputs and return sanitized bindings.

    Native JSON is an explicit transport choice, never detected from stdout.
    Production deliverable text cannot be fed to a native-result collector.
    Hashes bind supplied values; they do not verify disk, ownership, confinement,
    served identity, subscription entitlement or role suitability.
    """
    identity = profile_identity(profile)
    checked_limits = validate_limits(limits)
    if (
        type(timeout_seconds) not in (int, float)
        or not 0 < timeout_seconds <= checked_limits["timeout_seconds"]
    ):
        raise ValueError("actual process timeout must fit the explicit role timeout")
    if type(prompt) is not str or len(prompt) > checked_limits["max_prompt_bytes"]:
        raise ValueError("complete prompt must be bounded explicit text")
    if agent != "claude" or agent != identity["agent"]:
        raise ValueError("agent/profile mismatch or unsupported data-only transport")
    if response_protocol != NATIVE_JSON:
        raise ValueError(
            "explicit native JSON protocol required; legacy text needs separate handling"
        )
    if binary_sha256 != CLAUDE_BINARY_SHA256:
        raise ValueError("caller-rehashed binary must match the reviewed build")
    if type(argv) is not list or argv != expected_argv(
        binary_path, identity, limits=checked_limits
    ):
        raise ValueError("actual argv differs from exact data-only native JSON contract")
    if not isinstance(env, Mapping):
        raise ValueError("actual environment must be an explicit mapping")
    expected = isolated_environment(
        private_root,
        cwd=cwd,
        existing_oauth_token=env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        limits=checked_limits,
    )
    if dict(env) != expected:
        raise ValueError("actual environment contains missing, changed or ambient controls")
    if not isinstance(source_sha256, Mapping) or set(source_sha256) != _SOURCE_FILES:
        raise ValueError("exact adapter/dispatcher/contract source pins are required")
    if any(type(value) is not str or not _SHA.fullmatch(value) for value in source_sha256.values()):
        raise ValueError("source pins must be SHA256 values")
    sanitized = {**expected, "CLAUDE_CODE_OAUTH_TOKEN": "REDACTED"}
    # Only the transient child environment may contain the borrowed OAuth value.
    if _contains_credential(
        {
            "profile": identity,
            "argv": argv,
            "environment": sanitized,
            "limits": checked_limits,
            "prompt": prompt,
            "sources": dict(source_sha256),
        },
        expected["CLAUDE_CODE_OAUTH_TOKEN"],
    ):
        raise ValueError("borrowed credential must not enter retained or hashed launch inputs")
    prompt_binding = validate_payload(prompt, limits=checked_limits, kind="prompt")
    controls = environment_controls(checked_limits)
    control_presence = {key: env.get(key) == value for key, value in controls.items()}
    config = {
        "argv": list(argv),
        "environment": sanitized,
        "environment_hash_scope": "all explicit environment fields; OAuth value redacted, not hashed",
        "cwd": cwd,
        "private_root": private_root,
        "response_protocol": response_protocol,
        "prompt_transport": "stdin",
        "controls": controls,
        "control_presence": control_presence,
        "limits": checked_limits,
        "limits_sha256": _digest(checked_limits),
        "timeout_seconds": timeout_seconds,
        "prompt": prompt_binding,
        "automatic_retries": 0,
        "fallbacks": 0,
    }
    binding = {
        "schema": SCHEMA,
        "agent": agent,
        "profile_identity": identity,
        "profile_identity_sha256": _digest(identity),
        "binary_path": binary_path,
        "binary_sha256": binary_sha256,
        "source_sha256": dict(source_sha256),
        "argv_sha256": _digest(argv),
        "environment_sha256": _digest(sanitized),
        "config_sha256": _digest(config),
        "response_protocol": response_protocol,
        "limits_sha256": _digest(checked_limits),
        "model_contract_sha256": checked_limits["model_contract_sha256"],
        "role": checked_limits["role"],
        "prompt_sha256": prompt_binding["sha256"],
        "source": "caller_supplied_launch_inputs_not_runtime_attestation",
        "activation": False,
        "role_qualification": False,
        "served_model": "UNKNOWN",
        "served_effort": "UNKNOWN",
        "entitlement": "UNKNOWN",
    }
    return {"binding": binding, "binding_sha256": _digest(binding), "config": config}
