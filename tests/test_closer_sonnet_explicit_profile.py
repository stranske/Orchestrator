"""Prospective candidate identity, never a historical or production promotion."""

import closer_data_only_execution as execution
import closer_role_qualification as roles
import execution_profiles as profiles

PROFILE_ID = "claude-sonnet-5-5-high-v2"


def test_historical_sonnet_high_identity_remains_exact():
    historical = profiles.get_profile("claude-sonnet-5-5-high")
    assert roles.identity_digest(historical) == (
        "33b1c408f83d43b3b27e8a51814ff100d08efedd538d156d3665578d0942fe24"
    )
    assert historical["adapter_version"] == "claude-cli-profile-v1"


def test_prospective_profile_requires_explicit_selection():
    assert PROFILE_ID not in {row["profile_id"] for row in profiles.profiles_for_agent("claude")}
    assert PROFILE_ID in {
        row["profile_id"] for row in profiles.profiles_for_agent("claude", include_explicit=True)
    }


def test_exact_registered_profile_uses_reviewed_explicit_effort_contract():
    profile = roles.profile_identity(profiles.get_profile(PROFILE_ID))
    assert execution.profile_identity(profile) == profile
    limits = {
        "schema": execution.LIMITS_SCHEMA,
        "role": "strong_criterion_adjudication",
        "model_contract_sha256": "f" * 64,
        "max_prompt_bytes": 65536,
        "max_final_bytes": 8192,
        "max_native_result_bytes": 65536,
        "max_output_tokens": 2048,
        "max_turns": 1,
        "timeout_seconds": 45,
    }  # Synthetic settings only, not a production qualification or default.
    argv = execution.expected_argv("/synthetic/pinned-claude", profile, limits=limits)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--effort") + 1] == "high"


def test_prospective_identity_shares_account_without_qualification():
    profile = profiles.get_profile(PROFILE_ID)
    assert (
        profile["capacity_pool_ids"]
        == profiles.get_profile("claude-opus-5-5-high")["capacity_pool_ids"]
        == ["claude-subscription"]
    )
    assert roles.qualification_problems(profile, "strong_criterion_adjudication", records={})
