"""Truthful requested effort/profile wiring; offline commands and no-effect fixtures only."""

from __future__ import annotations

import hashlib
import json

import pytest

import adapters
import capacity
import dispatcher
import execution_profiles as profiles
import router


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "claude-sonnet-5-5-high",
            "48164b6fdd4cfb36bf7a1234937042c81a37277c1a88291b637a33cef89602e9",
        ),
        ("muse-wonder-1.0", "a8de3a6feadd49a8d8f3654b26febd65e991097e36f3e12f50962b4189f60527"),
        ("cursor-composer-2.5", "8ae8aaf216f3bf850bf80160e08fbff85c54630025b6316b3857b55513dbbdcd"),
    ],
)
def test_historical_profile_definitions_are_unchanged(name, expected):
    # Fixed canonical snapshots from 0e79223; never relabel historical attempts.
    raw = json.dumps(profiles.get_profile(name), sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == expected


@pytest.mark.parametrize(
    "agent,profile_id,effort,flag",
    [
        ("claude", "claude-sonnet-5-5-low", "low", "--effort"),
        ("claude", "claude-sonnet-5-5-high", "high", "--effort"),
        ("muse", "muse-wonder-1.0-low", "low", "--reasoning-effort"),
        ("muse", "muse-wonder-1.0", "high", "--reasoning-effort"),
    ],
)
def test_exact_profiles_emit_model_and_effort_without_widening_rails(
    agent, profile_id, effort, flag
):
    profile = profiles.get_profile(profile_id)
    command = adapters.build_command(agent, "offline", profile=profile_id, transport="offload")
    assert command[command.index("--model") + 1] == profile["requested_model"]
    assert command[command.index(flag) + 1] == effort == profile["reasoning_effort"]
    assert profile["capacity_pool_ids"] == [agent + "-subscription"]
    assert profile["adapter_version"] == agent + "-cli-profile-v" + (
        "2" if effort == "low" else "1"
    )
    if agent == "muse":
        assert {
            "--no-session-log",
            "--no-foreign-personal-context",
            "--disable-approval",
            "--json",
        } <= set(command)
        assert "--disable-sandbox" not in command and "--yolo" not in command
    else:
        assert "--dangerously-skip-permissions" in command


@pytest.mark.parametrize("agent,flag", [("claude", "--effort"), ("muse", "--reasoning-effort")])
def test_legacy_unspecified_effort_keeps_existing_cli_default(agent, flag):
    assert flag not in adapters.build_command(agent, "offline")


@pytest.mark.parametrize(
    "agent,effort",
    [
        ("claude", "auto"),
        ("claude", "ultra"),
        ("claude", ""),
        ("muse", "auto"),
        ("muse", "HIGH"),
        ("muse", ""),
    ],
)
def test_unsupported_explicit_effort_refuses_instead_of_silent_default(agent, effort):
    model = "claude-sonnet-5-5" if agent == "claude" else "muse-wonder-1.0"
    with pytest.raises(ValueError, match="unsupported explicit"):
        adapters.build_command(agent, "offline", requested_model=model, reasoning_effort=effort)


def test_claude_unsupported_model_effort_pair_refuses():
    with pytest.raises(ValueError, match="unsupported explicit Claude"):
        adapters.build_command(
            "claude", "offline", requested_model="claude-haiku-4-5", reasoning_effort="low"
        )


@pytest.mark.parametrize(
    "agent,profile_id", [("claude", "claude-sonnet-5-5-low"), ("muse", "muse-wonder-1.0-low")]
)
@pytest.mark.parametrize(
    "override", [{"reasoning_effort": "high"}, {"requested_model": "other-model"}]
)
def test_profile_overrides_cannot_contradict_registered_low(agent, profile_id, override):
    with pytest.raises(ValueError, match="contradicts immutable"):
        adapters.build_command(
            agent, "offline", profile=profile_id, transport="offload", **override
        )


def test_cursor_low_is_named_explicit_only_and_defaults_remain_composer():
    name = "cursor-gpt-5.6-luna-low"
    profile = profiles.get_profile(name)
    assert profile["requires_explicit_selection"] and profile["reasoning_effort"] == "low"
    assert profile["capacity_pool_ids"] == ["cursor-subscription"]
    defaults = profiles.profiles_for_agent("cursor", transport="offload")
    assert [p["profile_id"] for p in defaults] == ["cursor-composer-2.5"]
    assert name in {
        p["profile_id"]
        for p in profiles.profiles_for_agent("cursor", transport="offload", include_explicit=True)
    }
    assert [
        p["profile_id"]
        for p in router._automatic_profiles(
            {"agent": "cursor", "mode": "composer"}, transport="offload"
        )
    ] == ["cursor-composer-2.5"]
    assert (
        dispatcher._select_offload_profile("cursor", "composer")["profile_id"]
        == "cursor-composer-2.5"
    )
    default = adapters.build_command("cursor", "offline")
    assert default[default.index("--model") + 1] == adapters.CURSOR_COMPOSER_MODEL
    explicit = adapters.build_command("cursor", "offline", profile=name, transport="offload")
    assert explicit[explicit.index("--model") + 1] == "gpt-5.6-luna-low"
    assert "--effort" not in explicit and "--reasoning-effort" not in explicit
    assert capacity.profile_pool_ids()[name] == ["cursor-subscription"]
    debit = capacity.debit_profile_pools(
        [{"event": "start", "selected_profile_id": name, "count": 1}]
    )
    assert debit == {"cursor-subscription": 1.0}
