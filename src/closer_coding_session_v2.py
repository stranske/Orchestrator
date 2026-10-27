"""Pure prospective coding-session data contract; no execution or admission authority.

These validators neither authenticate their inputs nor enroll/renew a session.
Only a later reviewed source enrollment may persist a prepared snapshot. Existing
v1 budgets and native-cap guards are intentionally untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

# Loaded-source provenance only, not an OS immutability or permission claim.
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

BUDGET_SCHEMA = "closer-coding-session-budget/v2"
POLICY_SCHEMA = "closer-coding-session-policy/v2"
ADMISSION_SCHEMA = "closer-coding-session-admission/v2"
ADMISSION_FIELD = "original_coding_session_v2"
POLICY_FIELD = "coding_session_v2"
PROFILE = "codex-6.1-sol-high"
LIMIT_FIELDS = (
    "max_runtime_ms",
    "max_cleanup_ms",
    "max_prompt_utf8_bytes",
    "max_captured_output_bytes",
    "max_retained_final_utf8_bytes",
    "max_retained_receipt_bytes",
    "max_outer_submissions",
)
UNKNOWN_FIELDS = (
    "native_model_attempts",
    "native_input_tokens",
    "native_output_tokens",
)
_POLICY_FIELDS = frozenset(
    {"schema", "consumer_id", "agent", "profile_id", "profile_sha256", "budget"}
)
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_CONSUMER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_INTEGER = 2**53 - 1


class SessionContractError(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise SessionContractError(reason)


def _exact(value, fields, reason):
    _need(type(value) is dict and set(value) == set(fields), reason)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def validate_budget(value):
    """Return a new exact budget; every numeric limit must be explicitly supplied.

    Runtime is a ceiling for a NEW source-authenticated session, further capped
    by that session's actual current callback deadline. It is not permission to
    reuse an expired original request or renew an old session. Cleanup is local
    bounded cleanup, not proof of remote cancellation or native token limits.
    """
    _exact(value, {"schema", *LIMIT_FIELDS, *UNKNOWN_FIELDS}, "exact v2 budget required")
    _need(
        type(value["schema"]) is str and value["schema"] == BUDGET_SCHEMA,
        "v2 budget schema differs",
    )
    for field in LIMIT_FIELDS:
        _need(
            type(value[field]) is int and 0 < value[field] <= _SAFE_INTEGER,
            "explicit positive safe integer required: " + field,
        )
    _need(value["max_outer_submissions"] == 1, "one outer submission required")
    _need(
        all(type(value[field]) is str and value[field] == "UNKNOWN" for field in UNKNOWN_FIELDS),
        "native model attempts and token usage must remain UNKNOWN",
    )
    return {
        "schema": BUDGET_SCHEMA,
        **{key: value[key] for key in (*LIMIT_FIELDS, *UNKNOWN_FIELDS)},
    }


def minimum_budget(original, current, policy):
    """Compute data ceilings only; no clock, source authentication or launch occurs."""
    budgets = tuple(validate_budget(value) for value in (original, current, policy))
    return {
        "schema": BUDGET_SCHEMA,
        **{field: min(value[field] for value in budgets) for field in LIMIT_FIELDS},
        **{field: "UNKNOWN" for field in UNKNOWN_FIELDS},
    }


def validate_policy(value):
    _exact(value, _POLICY_FIELDS, "exact v2 source policy required")
    _need(
        type(value["schema"]) is str and value["schema"] == POLICY_SCHEMA,
        "v2 policy schema differs",
    )
    _need(
        type(value["consumer_id"]) is str and _CONSUMER.fullmatch(value["consumer_id"]),
        "exact source consumer identity required",
    )
    _need(
        type(value["agent"]) is str
        and value["agent"] == "codex"
        and type(value["profile_id"]) is str
        and value["profile_id"] == PROFILE,
        "unsupported coding-session provider/profile",
    )
    _need(
        type(value["profile_sha256"]) is str and _SHA.fullmatch(value["profile_sha256"]),
        "source profile digest required",
    )
    return {**value, "budget": validate_budget(value["budget"])}


def validate_admission_snapshot(value):
    """Check snapshot data integrity; its digest is NOT source authentication."""
    optional = {"publication_v1"} if type(value) is dict and "publication_v1" in value else set()
    _exact(
        value, _POLICY_FIELDS | {"policy_sha256"} | optional, "exact v2 admission snapshot required"
    )
    _need(
        type(value["schema"]) is str and value["schema"] == ADMISSION_SCHEMA,
        "v2 admission snapshot schema differs",
    )
    policy = validate_policy(
        {**{key: value[key] for key in _POLICY_FIELDS}, "schema": POLICY_SCHEMA}
    )
    _need(
        type(value["policy_sha256"]) is str
        and value["policy_sha256"] == hashlib.sha256(_canonical(policy)).hexdigest(),
        "original v2 source policy digest differs",
    )
    result = {**policy, "schema": ADMISSION_SCHEMA, "policy_sha256": value["policy_sha256"]}
    if optional:
        import closer_coding_publication as publication

        selected = publication.validate_policy(value["publication_v1"])
        _need(selected["consumer_id"] == policy["consumer_id"], "publication consumer differs")
        result["publication_v1"] = selected
    return result


def snapshot_from_admission(record):
    """Read prospective data without modifying/reinterpreting old v1 records.

    A later caller must first authenticate the real protected record and current
    source session. Mixed v1/v2 coding budgets hold rather than dropping v1 caps.
    """
    _need(
        type(record) is dict
        and type(record.get("schema")) is str
        and record["schema"] == "local-correction-admission/v1",
        "original local admission record required",
    )
    _need("original_coding_budget" not in record, "v1 coding budget cannot be replaced or ignored")
    _need(ADMISSION_FIELD in record, "original v2 coding-session snapshot absent")
    return validate_admission_snapshot(record[ADMISSION_FIELD])


@dataclass(frozen=True)
class AdmissionPolicyDraft:
    """Immutable prepared bytes or a hold reason; never an execution permit."""

    snapshot_utf8: bytes | None
    reason: str


def prepare_admission_policy(consumers):
    """Prepare data for a FUTURE trusted new-enrollment integration only.

    No supplied mapping authenticates itself. Real source enrollment must obtain
    it from its validated SourceInstallation/config while holding the original
    queue and writer custody. This helper does not accept or mutate admissions,
    persist anything, mint authority, or choose numeric/model defaults.
    """
    if type(consumers) is not dict or any(
        type(key) is not str or type(value) is not dict for key, value in consumers.items()
    ):
        return AdmissionPolicyDraft(None, "source_consumer_config_unavailable")
    candidates = [
        (key, value)
        for key, value in consumers.items()
        if type(value.get("agent")) is str and value["agent"] == "codex" and POLICY_FIELD in value
    ]
    if not candidates:
        return AdmissionPolicyDraft(None, "v2_source_policy_absent")
    if len(candidates) != 1:
        return AdmissionPolicyDraft(None, "v2_source_policy_ambiguous")
    consumer_id, registration = candidates[0]
    try:
        policy = validate_policy(registration[POLICY_FIELD])
        _need(
            all(
                type(registration.get(key)) is str
                for key in ("runner", "agent", "profile_id", "profile_sha256")
            )
            and registration["runner"] == "existing-local-controller"
            and policy["consumer_id"] == consumer_id
            and policy["agent"] == registration["agent"]
            and policy["profile_id"] == registration.get("profile_id")
            and policy["profile_sha256"] == registration.get("profile_sha256"),
            "v2 policy differs from source consumer registration",
        )
        _need(
            not {"policy_ceiling", "policy_ceiling_sha256", "output_tokens"} & set(registration),
            "mixed v1/v2 consumer budget cannot be reinterpreted",
        )
        snapshot = {
            **policy,
            "schema": ADMISSION_SCHEMA,
            "policy_sha256": hashlib.sha256(_canonical(policy)).hexdigest(),
        }
        if "publication_v1" in registration:
            import closer_coding_publication as publication

            snapshot["publication_v1"] = publication.validate_consumer_mode(
                consumer_id, registration
            )
        validate_admission_snapshot(snapshot)
    except (TypeError, ValueError):
        return AdmissionPolicyDraft(None, "v2_source_policy_invalid")
    return AdmissionPolicyDraft(_canonical(snapshot), "prepared_data_only_not_enrolled")
