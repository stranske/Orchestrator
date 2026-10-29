"""Evidence-bound Closer role eligibility, independent of effort and capacity.

Only reviewed records in this module qualify a production role. Capacity
reports, candidate metadata and worker output are not qualification authority.
Reviewed current native records are finite and role-specific; fixture success
does not qualify a real model. Consumers must additionally check installed transport confinement
and acquire the existing shared provider pools before invoking a worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, TypeGuard

SCHEMA = "closer-role-qualification/v1"
BOUNDED_CLASSIFICATION = "bounded_classification"
STRONG_CRITERION_ADJUDICATION = "strong_criterion_adjudication"
CONSTRAINED_REPAIR = "constrained_repair"
TASK_ROLES = {
    "closer_interpret": BOUNDED_CLASSIFICATION,
    "closer_correction_classification": BOUNDED_CLASSIFICATION,
    "closer_exception": STRONG_CRITERION_ADJUDICATION,
    "closer_repair": CONSTRAINED_REPAIR,
}
ROLES = frozenset(TASK_ROLES.values())
# Qualification of a repair worker is not permission to run any of these
# operations: the bound action plan must independently admit the exact fix.
MECHANICAL_OPERATIONS = frozenset({"format", "lint_fix", "docstring_fix"})
# One independently reviewed native observation; expiry is not auto-refreshed.
ROLE_QUALIFICATIONS: dict[str, dict[str, Any]] = {
    "sonnet-current-role-qualification:3e7ba0521480a5d44d6a081e5a43b8895aeded9eba9080dbe8b8ebdca98fb009": {
        "schema": "closer-role-qualification/v1",
        "status": "qualified",
        "qualification_id": "sonnet-current-role-qualification:3e7ba0521480a5d44d6a081e5a43b8895aeded9eba9080dbe8b8ebdca98fb009",
        "role": "strong_criterion_adjudication",
        "profile_identity": {
            "profile_id": "claude-sonnet-5-5-high-v2",
            "agent": "claude",
            "provider": "anthropic",
            "requested_model": "claude-sonnet-5-5",
            "reasoning_effort": "high",
            "permission_mode": "workspace-write",
            "adapter_version": "claude-cli-profile-v2",
            "prompt_version": "orchestrator-prompt-v1",
            "lifecycle_status": "active",
            "transport_support": ["experiment", "local", "offload"],
            "capacity_pool_ids": ["claude-subscription"],
            "requires_explicit_selection": True,
        },
        "profile_identity_sha256": "c6ea6091c54feec59a7405a17795236d7362aa19290d8b11cd77333271076bcd",
        "qualified_at": 1793161032.187778,
        "expires_at": 1793247432.187778,
        "transport": "offload",
        "named_criterion_only": True,
        "confinement": {
            "data_only": True,
            "tools": False,
            "mcp": False,
            "hooks": False,
            "project_context": False,
            "adapter_build_sha256": "c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937",
        },
        "execution_limits": {
            "schema": "closer-data-only-limits/v1",
            "role": "strong_criterion_adjudication",
            "model_contract_sha256": "1cb727375e9fa27344d89e02a7dbcd1881b613d2045a14fab5e02a30530ee293",
            "max_prompt_bytes": 65536,
            "max_final_bytes": 16384,
            "max_native_result_bytes": 65536,
            "max_output_tokens": 4096,
            "max_turns": 1,
            "timeout_seconds": 60,
        },
        "production_model_contract": {
            "schema": "closer-production-model-contract/v1",
            "role": "strong_criterion_adjudication",
            "sources": {
                "closer_dispatcher.py": "81635a53d3005a6e578f7128b9ed1dae219b872dc6e946f5b4ec1047038b7ead",
                "dispatcher.py": "220f34ca499e83db81405245d1cf56b21f3bb7b16ca0c8bf55ba66bd8bbd4854",
                "adapters.py": "4156636c6251e480d2f7bd22dced52fd344028f65e4931b5018127b75f77fbeb",
                "closer_data_only_execution.py": "68c9dbac68bcf6ee98b403130b3b008f4412c9f8c8eccf8c2b85021038ad14f2",
                "closer_claude_role_admission.py": "7aaccbdb1a42b8fdec775b27a11199fc572093af80f8c860a909ed56fe1897ae",
                "transport_saved_result.py": "468697f68fb973c5a37ea7dcbd1226508664935ad997662a90718fbc5087996d",
                "transport_qualification.py": "b54e9e8d529647615650b9d999eec6a943cb53416fd30f73fce13ad7ad6aedc1",
                "closer_model_contracts.py": "d0e95012d1cb48e8d275bf1ba3221ed9d931ede48da2321cb1a36bd769a66d62",
                "closer_role_selection.py": "ee7b429db1e0f48afccfc50ebc2de4e9eb05ada2b64e9bea3998df6dd84ce651",
                "closer_semantic_assessment.py": "922b5a684a4cb6e2c5f051a8ecde8ae779fc9a07962dcb926e501d45ebef870d",
                "router.py": "7702a4d6d79d9c5ff13c10d90fa3d3b0c28b15884ed959be84abd228b037c01b",
                "closer_execution_contracts.py": "1e449e01f72b316422910463ebcfa88050ffe868d80f7ec6e76bbd80edb5186a",
                "closer_adjudication_policy.py": "9ecc5df250160104104c561b6bcfbbb35921891e0cbec6cf0f098a924feb15e1",
                "closer_disposition.py": "a726f59d1376dacb9b78117bbc7071bd67f7c1c3412e885c9fd9c721c309ca9c",
                "closer_evidence.py": "96bca7b0d0939d77cfcab7c4fe21af4945fa5d0aaff71b67bd14199635ab54a4",
                "closer_mechanical_source.py": "af61c2722c4794717eddf36e8986d725978285167d220d2b86c063f25a8bfc67",
                "closer_mechanical_catalog.py": "86e732b5f9611dbd70b7bdb28011cf189b5ac8ca8b19af9de07542eeb2520b78",
                "closer_mechanical_executor.py": "21a753583dc734278bd2cd5709a70b181898bb759fbd4e56baa54db15549d7f0",
                "closer_local_tool_confinement.py": "5152c424069a58d6cf4f93ce43cf2a2c22d81a38e3ffb0cd03d21b179c40365f",
                "config/closer_role_selection.json": "266263f0a44319b08400fb7f3c1a7e93004b338f0548ea2b70e859332777afbf",
            },
            "model_contract": {
                "request_schema": "closer-criterion-request/v2",
                "response_schema": "closer-criterion-response/v1",
                "prompt_builder": "closer_execution_contracts.build_adjudication_prompt",
                "response_validator": "closer_execution_contracts.validate_adjudication_response",
                "merge": "closer_execution_contracts.merge_selected_response",
                "max_prompt_bytes": 65536,
                "retained_io_required": True,
                "acceptance_authority": False,
                "evidence_policy": "closer-adjudication-evidence/v2",
                "prompt_version": "closer-adjudication-prompt/v2",
                "controller_action_policy": "closer-adjudication-action-policy/v1",
            },
            "authority": False,
            "contract_sha256": "1cb727375e9fa27344d89e02a7dbcd1881b613d2045a14fab5e02a30530ee293",
        },
        "evidence_refs": [
            {
                "reference": "retained-native-evidence:sha256:cfce77f73e166ae3daa3ae68ac17bc763d872c353019e6367763593f98222e9c",
                "sha256": "cfce77f73e166ae3daa3ae68ac17bc763d872c353019e6367763593f98222e9c",
            },
            {
                "reference": "reviewed-model-quality:sha256:7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0",
                "sha256": "7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0",
            },
        ],
    },
    "sonnet-current-role-qualification:c256f1adb15405e8aa82e2baab729c38f1a0edb9994c529079e99ec3d56358f2": {
        "schema": "closer-role-qualification/v1",
        "status": "qualified",
        "qualification_id": "sonnet-current-role-qualification:c256f1adb15405e8aa82e2baab729c38f1a0edb9994c529079e99ec3d56358f2",
        "role": "strong_criterion_adjudication",
        "profile_identity": {
            "profile_id": "claude-sonnet-5-5-high-v2",
            "agent": "claude",
            "provider": "anthropic",
            "requested_model": "claude-sonnet-5-5",
            "reasoning_effort": "high",
            "permission_mode": "workspace-write",
            "adapter_version": "claude-cli-profile-v2",
            "prompt_version": "orchestrator-prompt-v1",
            "lifecycle_status": "active",
            "transport_support": ["experiment", "local", "offload"],
            "capacity_pool_ids": ["claude-subscription"],
            "requires_explicit_selection": True,
        },
        "profile_identity_sha256": "c6ea6091c54feec59a7405a17795236d7362aa19290d8b11cd77333271076bcd",
        "qualified_at": 1793255742.791692,
        "expires_at": 1793342142.791692,
        "transport": "offload",
        "named_criterion_only": True,
        "confinement": {
            "data_only": True,
            "tools": False,
            "mcp": False,
            "hooks": False,
            "project_context": False,
            "adapter_build_sha256": "c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937",
        },
        "execution_limits": {
            "schema": "closer-data-only-limits/v1",
            "role": "strong_criterion_adjudication",
            "model_contract_sha256": "239df39539465467e89dc47c852f17330ea3700a485806ef69b21933c52af92a",
            "max_prompt_bytes": 65536,
            "max_final_bytes": 16384,
            "max_native_result_bytes": 65536,
            "max_output_tokens": 4096,
            "max_turns": 1,
            "timeout_seconds": 60,
        },
        "production_model_contract": {
            "schema": "closer-production-model-contract/v1",
            "role": "strong_criterion_adjudication",
            "sources": {
                "adapters.py": "4156636c6251e480d2f7bd22dced52fd344028f65e4931b5018127b75f77fbeb",
                "closer_adjudication_policy.py": "9ecc5df250160104104c561b6bcfbbb35921891e0cbec6cf0f098a924feb15e1",
                "closer_claude_role_admission.py": "7aaccbdb1a42b8fdec775b27a11199fc572093af80f8c860a909ed56fe1897ae",
                "closer_data_only_execution.py": "68c9dbac68bcf6ee98b403130b3b008f4412c9f8c8eccf8c2b85021038ad14f2",
                "closer_dispatcher.py": "d78b8d6642b64b94d366544a1515a5ffcd463bf6d101d04251fcaab8ac304256",
                "closer_disposition.py": "a726f59d1376dacb9b78117bbc7071bd67f7c1c3412e885c9fd9c721c309ca9c",
                "closer_evidence.py": "96bca7b0d0939d77cfcab7c4fe21af4945fa5d0aaff71b67bd14199635ab54a4",
                "closer_execution_contracts.py": "1e449e01f72b316422910463ebcfa88050ffe868d80f7ec6e76bbd80edb5186a",
                "closer_local_tool_confinement.py": "5152c424069a58d6cf4f93ce43cf2a2c22d81a38e3ffb0cd03d21b179c40365f",
                "closer_mechanical_catalog.py": "86e732b5f9611dbd70b7bdb28011cf189b5ac8ca8b19af9de07542eeb2520b78",
                "closer_mechanical_executor.py": "21a753583dc734278bd2cd5709a70b181898bb759fbd4e56baa54db15549d7f0",
                "closer_mechanical_source.py": "af61c2722c4794717eddf36e8986d725978285167d220d2b86c063f25a8bfc67",
                "closer_model_contracts.py": "d0e95012d1cb48e8d275bf1ba3221ed9d931ede48da2321cb1a36bd769a66d62",
                "closer_role_selection.py": "ee7b429db1e0f48afccfc50ebc2de4e9eb05ada2b64e9bea3998df6dd84ce651",
                "closer_semantic_assessment.py": "922b5a684a4cb6e2c5f051a8ecde8ae779fc9a07962dcb926e501d45ebef870d",
                "config/closer_role_selection.json": "266263f0a44319b08400fb7f3c1a7e93004b338f0548ea2b70e859332777afbf",
                "dispatcher.py": "220f34ca499e83db81405245d1cf56b21f3bb7b16ca0c8bf55ba66bd8bbd4854",
                "router.py": "7702a4d6d79d9c5ff13c10d90fa3d3b0c28b15884ed959be84abd228b037c01b",
                "transport_qualification.py": "b54e9e8d529647615650b9d999eec6a943cb53416fd30f73fce13ad7ad6aedc1",
                "transport_saved_result.py": "468697f68fb973c5a37ea7dcbd1226508664935ad997662a90718fbc5087996d",
            },
            "model_contract": {
                "acceptance_authority": False,
                "controller_action_policy": "closer-adjudication-action-policy/v1",
                "evidence_policy": "closer-adjudication-evidence/v2",
                "max_prompt_bytes": 65536,
                "merge": "closer_execution_contracts.merge_selected_response",
                "prompt_builder": "closer_execution_contracts.build_adjudication_prompt",
                "prompt_version": "closer-adjudication-prompt/v2",
                "request_schema": "closer-criterion-request/v2",
                "response_schema": "closer-criterion-response/v1",
                "response_validator": "closer_execution_contracts.validate_adjudication_response",
                "retained_io_required": True,
            },
            "authority": False,
            "contract_sha256": "239df39539465467e89dc47c852f17330ea3700a485806ef69b21933c52af92a",
        },
        "evidence_refs": [
            {
                "reference": "retained-native-evidence:sha256:82ff806f22863e3f5fca5a942c2cdc666f100a6cfbb922f7f066b8e807ffb20e",
                "sha256": "82ff806f22863e3f5fca5a942c2cdc666f100a6cfbb922f7f066b8e807ffb20e",
            },
            {
                "reference": "reviewed-model-quality:sha256:7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0",
                "sha256": "7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0",
            },
        ],
    },
}
_LOGGER = logging.getLogger(__name__)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTITY_TEXT = (
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


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _finite(value: Any) -> TypeGuard[int | float]:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def profile_identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact semantic execution identity; missing fields never infer defaults."""
    if not isinstance(profile, Mapping):
        raise ValueError("profile identity must be an object")
    if any(not _text(profile.get(key)) for key in _IDENTITY_TEXT):
        raise ValueError("profile identity has missing or invalid fields")
    if profile["agent"].casefold() == "aider":
        raise ValueError("Aider is not an eligible Closer worker")
    if profile["lifecycle_status"] != "active":
        raise ValueError("profile lifecycle is not active")
    if profile["permission_mode"] not in {"read-only", "workspace-write"}:
        raise ValueError("profile permission mode is unsupported")
    result = {key: profile[key] for key in _IDENTITY_TEXT}
    for key in ("transport_support", "capacity_pool_ids"):
        values = profile.get(key)
        if (
            not isinstance(values, list)
            or not values
            or any(not _text(value) for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"profile {key} is missing or invalid")
        result[key] = sorted(values)
    if "offload" not in result["transport_support"]:
        raise ValueError("profile has no supported offload transport")
    explicit = profile.get("requires_explicit_selection", False)
    if type(explicit) is not bool:
        raise ValueError("profile explicit selection flag is invalid")
    result["requires_explicit_selection"] = explicit
    return result


def identity_digest(profile: Mapping[str, Any]) -> str:
    value = profile_identity(profile)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _evidence_problems(record: Mapping[str, Any], role: str) -> list[str]:
    problems = []
    refs = record.get("evidence_refs")
    if (
        not isinstance(refs, list)
        or not refs
        or any(
            not isinstance(ref, Mapping)
            or not _text(ref.get("reference"))
            or not isinstance(ref.get("sha256"), str)
            or not _SHA256.fullmatch(ref["sha256"])
            for ref in refs
        )
    ):
        problems.append("role qualification lacks bound evidence references")
    if role in {BOUNDED_CLASSIFICATION, STRONG_CRITERION_ADJUDICATION}:
        confinement = record.get("confinement")
        if not isinstance(confinement, Mapping) or not (
            confinement.get("data_only") is True
            and all(
                confinement.get(key) is False
                for key in ("tools", "mcp", "hooks", "project_context")
            )
            and isinstance(confinement.get("adapter_build_sha256"), str)
            and _SHA256.fullmatch(confinement["adapter_build_sha256"])
        ):
            problems.append("read-only role lacks explicit confinement evidence")
    if role == CONSTRAINED_REPAIR:
        repair = record.get("repair_capabilities")
        operations = repair.get("allowlisted_operations") if isinstance(repair, Mapping) else None
        if not isinstance(repair, Mapping) or not (
            repair.get("mechanical_only") is True
            and repair.get("substantive_edits") is False
            and all(
                repair.get(key) is True
                for key in (
                    "isolated_workspace",
                    "path_allowlist_enforced",
                    "operation_allowlist_enforced",
                    "test_required",
                )
            )
            and isinstance(operations, list)
            and bool(operations)
            and all(isinstance(operation, str) for operation in operations)
            and len(set(operations)) == len(operations)
            and set(operations) <= MECHANICAL_OPERATIONS
        ):
            problems.append("repair role lacks constrained mechanical capability evidence")
    if role == STRONG_CRITERION_ADJUDICATION and record.get("named_criterion_only") is not True:
        problems.append("adjudication role lacks named-criterion capability evidence")
    return problems


def _selected_record(
    profile: Mapping[str, Any], role: str, current: float, source: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]] | None:
    """Select one live record while retaining only fully valid expired history."""
    matches = []
    for key, record in source.items():
        if not _text(key) or not isinstance(record, Mapping):
            return None
        bound = record.get("profile_identity")
        if (
            record.get("role") == role
            and isinstance(bound, Mapping)
            and bound.get("profile_id") == profile["profile_id"]
        ):
            matches.append((key, record))
    active = []
    for key, record in matches:
        started, expires = record.get("qualified_at"), record.get("expires_at")
        if _finite(started) and _finite(expires) and 0 <= started < expires <= current:
            # An expired row can remain only as complete historical evidence.
            # Validate its original observation in a one-row registry; that
            # recursive call is necessarily live at `started` and cannot renew it.
            if qualification_problems(profile, role, now=started, records={key: record}):
                return None
            continue
        if _finite(started) and _finite(expires) and 0 <= started <= current < expires:
            active.append((key, record))
            continue
        return None
    return active[0] if len(active) == 1 else None


def qualification_problems(
    profile: Mapping[str, Any],
    role: str,
    *,
    now: float | None = None,
    records: Mapping[str, Any] | None = None,
) -> list[str]:
    """Check one exact reviewed record; ``records`` is an offline fixture seam.

    No file, provider, environment setting or capacity report is consulted.
    Expiry removes eligibility; it never supplies action or retry authority.
    """
    if not isinstance(role, str) or role not in ROLES:
        return ["unknown Closer role"]
    try:
        identity = profile_identity(profile)
        digest = identity_digest(profile)
    except ValueError as exc:
        return [str(exc)]
    current = time.time() if now is None else now
    if not _finite(current) or current < 0:
        return ["qualification observation time is invalid"]
    source = ROLE_QUALIFICATIONS if records is None else records
    if not isinstance(source, Mapping):
        return ["role qualification registry is invalid"]
    selected = _selected_record(profile, role, current, source)
    if selected is None:
        return ["role qualification is missing or ambiguous"]
    key, record = selected
    problems = []
    if record.get("schema") != SCHEMA or record.get("status") != "qualified":
        problems.append("role qualification schema or status is invalid")
    if record.get("qualification_id") != key:
        problems.append("role qualification identity differs")
    if (
        record.get("profile_identity") != identity
        or record.get("profile_identity_sha256") != digest
    ):
        problems.append("role qualification profile identity differs")
    if record.get("transport") != "offload":
        problems.append("role qualification transport differs")
    if role == CONSTRAINED_REPAIR and identity["permission_mode"] != "workspace-write":
        problems.append("repair profile lacks workspace-write permission")
    started, expires = record.get("qualified_at"), record.get("expires_at")
    if not (
        _finite(started)
        and _finite(expires)
        and 0 <= started <= current < expires
        and started < expires
    ):
        problems.append("role qualification is expired or has invalid original times")
    return problems + _evidence_problems(record, role)


def is_qualified(
    profile: Mapping[str, Any],
    role: str,
    *,
    now: float | None = None,
    records: Mapping[str, Any] | None = None,
) -> bool:
    return not qualification_problems(profile, role, now=now, records=records)


def qualified_record(
    profile: Mapping[str, Any],
    role: str,
    *,
    now: float | None = None,
    records: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the same validated unique record for separate runtime build checks.

    The returned copy confers no transport or action authority. In particular,
    consumers must bind its confinement build digest to the installed adapter
    before launch rather than trusting a successful profile-level predicate.
    """
    source = ROLE_QUALIFICATIONS if records is None else records
    if not isinstance(source, Mapping):
        return None
    snapshot = deepcopy(dict(source))
    current = time.time() if now is None else now
    selected = _selected_record(profile, role, current, snapshot)
    if selected is None or qualification_problems(profile, role, now=current, records=snapshot):
        return None
    return dict(selected[1])


def is_runtime_qualified(
    profile: Mapping[str, Any],
    role: str,
    *,
    now: float | None = None,
    records: Mapping[str, Any] | None = None,
) -> bool:
    """Bind role evidence to the currently supported installed confinement build.

    This read-only check never invokes a provider or provisions an adapter.
    Missing role evidence avoids even the installed binary probe. Repair uses
    its separate mechanical capability contract; exact action-plan and workspace
    admission still belongs to the guarded action path.
    """
    record = qualified_record(profile, role, now=now, records=records)
    if record is None:
        return False
    import execution_profiles

    profile_id = profile["profile_id"]
    if profile_id in execution_profiles.PROFILE_RETIREMENTS:
        return False
    registered = execution_profiles.PROFILE_REGISTRY.get(profile_id)
    if not isinstance(registered, Mapping):
        return False
    try:
        if profile_identity(registered) != profile_identity(profile):
            return False
    except ValueError:
        return False
    if role == CONSTRAINED_REPAIR:
        return True
    # Only this adapter currently has an actual data-only confinement path.
    # A candidate or arbitrary build hash cannot enable another transport.
    if profile["agent"] != "claude":
        return False
    import adapters

    if record["confinement"]["adapter_build_sha256"] != getattr(
        adapters, "DATA_ONLY_CLAUDE_SHA256", None
    ):
        return False
    try:
        problem = adapters.data_only_problem("claude", "assess")
        if problem is not None:
            _LOGGER.debug(
                "Runtime confinement probe refused profile %s role %s: %s",
                profile_id,
                role,
                problem,
            )
            return False
        return True
    except (OSError, ValueError, RuntimeError) as exc:
        _LOGGER.debug(
            "Runtime confinement probe failed for profile %s role %s: %s", profile_id, role, exc
        )
        return False


# One reviewed source admission, NOT a new physical qualification. The original
# ROLE_QUALIFICATIONS row/proof/times/limits remain immutable. This module is a
# required member of the existing before-effect Root installation closure and
# is deliberately outside the twenty model-source fingerprints.
_CLOSED_SONNET_SOURCE_ADMISSION = {
    "schema": "closer-reviewed-source-admission/v1",
    "qualification_id": "sonnet-current-role-qualification:3e7ba0521480a5d44d6a081e5a43b8895aeded9eba9080dbe8b8ebdca98fb009",
    "qualification_record_sha256": "837713af72c5a701bd1b50757458632597aaf8f482ef2f2109811b101d402482",
    "historical_contract_sha256": "1cb727375e9fa27344d89e02a7dbcd1881b613d2045a14fab5e02a30530ee293",
    "execution_contract_sha256": "239df39539465467e89dc47c852f17330ea3700a485806ef69b21933c52af92a",
    "profile_identity_sha256": "c6ea6091c54feec59a7405a17795236d7362aa19290d8b11cd77333271076bcd",
    "task_type": "closer_exception",
    "role": STRONG_CRITERION_ADJUDICATION,
}


def _source_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def execution_source_support(record, profile_identity, descriptor, *, task_type, checked_at):
    """Separate historical qualification from a new exact-current execution.

    This source relation grants no qualification, freshness, transport or action
    authority. Each consumer retains its original current/historical role-time
    checks and the production result validator remains exact-current. No caller
    flag, alias, source normalization or future-pair inference is supported.
    """
    if not all(isinstance(value, Mapping) for value in (record, profile_identity, descriptor)):
        raise ValueError("qualification/source identity is not an object")
    limits = deepcopy(record.get("execution_limits"))
    role = TASK_ROLES.get(task_type)
    if (
        isinstance(limits, dict)
        and record.get("production_model_contract") == descriptor
        and limits.get("model_contract_sha256") == descriptor.get("contract_sha256")
        and limits.get("role") == role
    ):
        return {"limits": limits, "admission": None}
    pair = _CLOSED_SONNET_SOURCE_ADMISSION
    try:
        old = record["production_model_contract"]
        old_body = {k: v for k, v in old.items() if k != "contract_sha256"}
        body = {k: v for k, v in descriptor.items() if k != "contract_sha256"}
        valid = (
            task_type == pair["task_type"]
            and role == record["role"] == descriptor["role"] == pair["role"]
            and record["qualification_id"] == pair["qualification_id"]
            and _source_digest(record) == pair["qualification_record_sha256"]
            and record["profile_identity"] == profile_identity
            and _source_digest(profile_identity) == pair["profile_identity_sha256"]
            and old["contract_sha256"]
            == _source_digest(old_body)
            == pair["historical_contract_sha256"]
            and descriptor["contract_sha256"]
            == _source_digest(body)
            == pair["execution_contract_sha256"]
            and limits["model_contract_sha256"] == pair["historical_contract_sha256"]
            and limits["role"] == role
            and _finite(checked_at)
            and record["qualified_at"] <= checked_at < record["expires_at"]
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError("qualification does not support this exact production source")
    admission = {
        **pair,
        "mode": "reviewed_current_source_admission",
        "observation_mode": "historical_physical_qualification",
        "original_qualified_at": record["qualified_at"],
        "original_expires_at": record["expires_at"],
        "original_limits_sha256": _source_digest(limits),
        "physical_qualification_performed": False,
        "freshness_renewed": False,
        "authority": False,
    }
    # This is a NEW runtime limit document, never a rewritten historical one.
    # Every role/schema/numerical bound is copied unchanged; only the truthful
    # current execution descriptor differs from the historical qualification.
    limits["model_contract_sha256"] = descriptor["contract_sha256"]
    return {"limits": limits, "admission": admission}


def execution_record_matches(record, expected, *, checked_at):
    """Validate one sealed execution's distinct qualification/source identities."""
    try:
        support = execution_source_support(
            record,
            expected["profile_identity"],
            expected["production_model_contract"],
            task_type=expected["task_type"],
            checked_at=checked_at,
        )
        return (
            expected["limits"] == support["limits"]
            and expected.get("qualification_source_admission") == support["admission"]
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
