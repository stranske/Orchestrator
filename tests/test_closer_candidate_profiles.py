"""Offline candidate inventory and explicit Claude command-policy contracts."""

import hashlib
import json

import pytest

import adapters
import dispatcher
import execution_profiles as profiles


@pytest.fixture(autouse=True)
def offline_policy(monkeypatch):
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.delenv("ORCH_CLAUDE_MAX_TIER", raising=False)
    for tier in ("CHEAP", "MID", "FULL"):
        monkeypatch.delenv("ORCH_CLAUDE_MODEL_" + tier, raising=False)


def test_routine_full_claude_reaches_registered_opus_without_default_ceiling():
    command = adapters.build_command("claude", "offline", mode="full")
    profile = profiles.get_profile("claude-opus-5-5-high")
    assert adapters.tier_ceiling("claude") is None
    assert adapters.effective_tier("claude", "full") == "full"
    assert command[command.index("--model") + 1] == profile["requested_model"]
    assert adapters.model_identity("claude", "full") == "claude-opus-5-5"


@pytest.mark.parametrize(
    "cap,expected",
    [("cheap", "claude-haiku-4-5"), ("mid", "claude-sonnet-5-5"), ("full", "claude-opus-5-5")],
)
def test_explicit_claude_caps_still_limit_routine_tier(monkeypatch, cap, expected):
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", cap)
    command = adapters.build_command("claude", "offline", mode="full")
    assert command[command.index("--model") + 1] == expected
    assert adapters.model_identity("claude", "full") == expected
    # A cap must never raise a smaller requested tier.
    cheap = adapters.build_command("claude", "offline", mode="cheap")
    assert cheap[cheap.index("--model") + 1] == "claude-haiku-4-5"


@pytest.mark.parametrize("effort", ["low", "high"])
def test_registered_opus_profiles_pin_explicit_model_effort_and_shared_pool(effort):
    profile = profiles.get_profile("claude-opus-5-5-" + effort)
    command = adapters.build_command("claude", "offline", profile=profile, transport="offload")
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert command[command.index("--effort") + 1] == effort
    assert profile["adapter_version"] == "claude-cli-profile-v2"
    assert (
        profile["capacity_pool_ids"]
        == profiles.get_profile("claude-sonnet-5-5-high")["capacity_pool_ids"]
    )
    assert profile["permission_mode"] == "workspace-write"
    assert "role_qualified" not in profile


def test_explicit_opus_profile_cannot_be_silently_relabelled_by_cap(monkeypatch):
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "mid")
    command = adapters.build_command("claude", "offline", profile="claude-opus-5-5-high")
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    with pytest.raises(ValueError, match="contradicts immutable"):
        adapters.build_command(
            "claude", "offline", profile="claude-opus-5-5-high", requested_model="other-model"
        )


@pytest.mark.parametrize(
    "cap,expected,profile_expected",
    [
        (None, "claude-opus-5-5", True),
        ("mid", "claude-sonnet-5-5", True),
        ("cheap", "claude-haiku-4-5", False),
    ],
)
def test_automatic_offload_profile_then_command_respects_actual_cap(
    monkeypatch, cap, expected, profile_expected
):
    if cap:
        monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", cap)
    selected = dispatcher._select_offload_profile("claude", "full")
    assert bool(selected) is profile_expected
    command = adapters.build_command("claude", "offline", mode="full", profile=selected)
    assert command[command.index("--model") + 1] == expected
    if selected:
        assert selected["requested_model"] == expected


@pytest.mark.parametrize("mode", [None, "mid", "offload", "assess"])
def test_automatic_profile_never_falls_back_above_an_unregistered_cheap_cap(monkeypatch, mode):
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "cheap")
    assert dispatcher._select_offload_profile("claude", mode) is None
    command = adapters.build_command("claude", "offline", mode=mode, profile=None)
    assert command[command.index("--model") + 1] == "claude-haiku-4-5"


@pytest.mark.parametrize("mode", [None, "assess", "offload"])
def test_unprofiled_non_tier_command_respects_explicit_mid_cap(monkeypatch, mode):
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "mid")
    selected = dispatcher._select_offload_profile("claude", mode)
    assert selected["requested_model"] == "claude-sonnet-5-5"
    # Refusal or absence of an automatic profile must preserve the same cap.
    command = adapters.build_command("claude", "offline", mode=mode, profile=None)
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"


@pytest.mark.parametrize("mode", [None, "assess", "offload"])
def test_uncapped_non_tier_command_keeps_existing_cli_default(mode):
    command = adapters.build_command("claude", "offline", mode=mode, profile=None)
    assert "--model" not in command


def test_automatic_profile_cap_matches_registered_model_without_qualifying_role(monkeypatch):
    opus = profiles.get_profile("claude-opus-5-5-high")
    sonnet = profiles.get_profile("claude-sonnet-5-5-high")
    assert adapters.automatic_profile_within_cap(opus)
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "mid")
    assert not adapters.automatic_profile_within_cap(opus)
    assert adapters.automatic_profile_within_cap(sonnet)
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "cheap")
    assert not adapters.automatic_profile_within_cap(sonnet)
    assert not adapters.automatic_profile_within_cap(
        {**sonnet, "profile_id": "unregistered-haiku", "requested_model": "claude-haiku-4-5"}
    )


def test_explicit_cap_rejects_unknown_model_even_if_profile_is_registered(monkeypatch):
    profile = profiles.get_profile("claude-sonnet-5-5-high")
    unknown = {**profile, "profile_id": "offline-unknown", "requested_model": "unknown-model"}
    monkeypatch.setitem(profiles.PROFILE_REGISTRY, unknown["profile_id"], unknown)
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "full")
    assert not adapters.automatic_profile_within_cap(unknown)


def test_selftest_restores_explicit_claude_cap(monkeypatch):
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "mid")
    observed = []

    def exercise(*, gaps):
        observed.append(adapters.tier_ceiling("claude"))

    monkeypatch.setattr(adapters, "_selftest_inner", exercise)
    adapters._selftest()
    assert observed == [None]
    assert adapters.tier_ceiling("claude") == "mid"


@pytest.mark.parametrize("effort", ["unknown", "", 3, True])
def test_opus_effort_rejects_unsupported_values(effort):
    with pytest.raises(ValueError, match="unsupported explicit Claude"):
        adapters.build_command(
            "claude", "offline", requested_model="claude-opus-5-5", reasoning_effort=effort
        )


def test_historical_sonnet_profile_identity_is_preserved():
    raw = json.dumps(
        profiles.get_profile("claude-sonnet-5-5-high"), sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(raw).hexdigest() == (
        "48164b6fdd4cfb36bf7a1234937042c81a37277c1a88291b637a33cef89602e9"
    )


def test_candidate_inventory_never_registers_unknown_or_qualifies_models():
    rows = profiles.CLOSER_CANDIDATE_MODELS
    assert {row["agent"] for row in rows} == {"codex", "claude", "gemini", "cursor", "vibe", "muse"}
    assert all(row["qualification_status"] == "candidate_only" for row in rows)
    assert all("role_qualified" not in row for row in rows)
    unknown = [row for row in rows if row["model_label"].startswith("Muse Spark")]
    assert {row["model_label"] for row in unknown} == {"Muse Spark 1.2", "Muse Spark 1.3"}
    assert all(
        row["requested_model"] is None
        and row["registered_profile_ids"] == ()
        and row["identifier_status"] == "unverified"
        for row in unknown
    )
    haiku = next(row for row in rows if row["model_label"] == "Claude Haiku")
    assert haiku["identifier_status"] == "known_unregistered"
    assert haiku["registered_profile_ids"] == ()
    assert all("spark" not in p["requested_model"] for p in profiles.profiles_for_agent("muse"))


@pytest.mark.parametrize("agent", ["codex", "gemini", "cursor", "vibe", "muse"])
def test_broad_candidates_do_not_enable_unqualified_data_only_transports(agent):
    assert adapters.data_only_problem(agent, "assess") == (
        "data-only tool boundary unsupported for " + agent
    )
