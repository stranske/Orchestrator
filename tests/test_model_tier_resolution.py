"""Regression guard for the agy (gemini) seat's model pin.

2026-08-08: `DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"` had rotted through a Google rename, so EVERY
`dispatcher.py offload --agent gemini` exited 1 with `invalid model selection ... not recognized as
a known model or custom model in settings` — while capacity.py still reported the seat `state: ok`
with full headroom. The fleet's designated big-context read seat was dead and nothing said so.

Three layers are guarded here:
  1. the configured model is actually in the installed CLI's advertised list (the live check that
     would have caught the rename on the day it happened);
  2. resolution survives the NEXT rename by auto-picking from that list;
  3. capacity.py reports an unresolvable model as unusable instead of `ok`.

Only test_configured_gemini_model_is_advertised needs the CLI; it skips when agy is absent or the
box is offline, because an unreadable list is not evidence of a bad pin.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

import adapters
import capacity
import env_prereq


def _live_advertised_models() -> list[str]:
    """Ask the installed agy CLI directly — deliberately bypasses the adapter's TTL cache so a
    stale cache entry can never satisfy this guard."""
    if not shutil.which("agy"):
        pytest.skip("agy CLI not installed on this box")
    try:
        proc = subprocess.run(
            ["agy", "models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"agy models unavailable: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"agy models exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    models = adapters.parse_agy_models(proc.stdout)
    if not models:
        pytest.skip("agy models returned no parseable models (offline?)")
    return models


def _live_cursor_models() -> list[str]:
    """Ask cursor-agent directly, with the credential file the fleet sources."""
    if not shutil.which("cursor-agent"):
        pytest.skip("cursor-agent CLI not installed on this box")
    models = adapters.advertised_models("cursor", refresh=True)
    if not models:
        pytest.skip("cursor model catalog unreadable (unauthenticated or offline)")
    return models


def test_configured_gemini_model_is_advertised():
    """THE regression: what we would send to agy must be a model agy actually offers."""
    advertised = _live_advertised_models()
    configured = adapters.gemini_model()
    assert configured in advertised, (
        f"gemini seat is not dispatchable: configured model {configured!r} is not advertised by "
        f"the installed agy CLI. Offered: {', '.join(advertised)}. "
        f"Update adapters.DEFAULT_GEMINI_MODEL (currently {adapters.DEFAULT_GEMINI_MODEL!r})."
    )


def test_default_pin_is_advertised():
    """The pinned fallback must be live too — it is what ships when the probe can't run."""
    advertised = _live_advertised_models()
    assert adapters.DEFAULT_GEMINI_MODEL in advertised, (
        f"DEFAULT_GEMINI_MODEL {adapters.DEFAULT_GEMINI_MODEL!r} has been renamed away; "
        f"agy now offers: {', '.join(advertised)}"
    )


def test_every_gemini_tier_pin_is_advertised():
    """All three tiers dispatch, so all three pins must be live — not just `full`."""
    advertised = _live_advertised_models()
    stale = {t: m for t, m in adapters.MODEL_TIERS["gemini"].items() if m not in advertised}
    assert (
        not stale
    ), f"gemini tier pins no longer advertised by agy: {stale}. Offered: {', '.join(advertised)}"


def test_probe_kill_switch_falls_back_to_pin(monkeypatch):
    """ORCH_MODEL_PROBE=0 => no subprocess, pinned model, still dispatchable."""
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.delenv("ORCH_GEMINI_MODEL", raising=False)
    monkeypatch.setattr(adapters, "_ADVERTISED_MEMO", {})
    monkeypatch.setattr(
        adapters.subprocess,
        "run",
        lambda *a, **k: pytest.fail("kill-switch must not shell out to a CLI"),
    )
    assert adapters.agy_advertised_models() == []
    assert adapters.gemini_model() == adapters.DEFAULT_GEMINI_MODEL


# ---------------------------------------------------------------------------
# Tier map: every agent that HAS tiers must pin all three; the rest must pin none.
# ---------------------------------------------------------------------------


def test_tiered_agents_pin_every_tier():
    """A half-filled tier map silently falls back to the CLI default — catch that."""
    for agent in ("codex", "claude", "gemini"):
        tiers = adapters.MODEL_TIERS[agent]
        missing = [t for t in adapters.MODEL_TIER_NAMES if not tiers.get(t)]
        assert not missing, f"{agent} is missing pins for {missing}"


def test_single_model_agents_pin_nothing():
    """vibe/aider/cursor carry no TIER map: one merged model, a floating alias, and a
    Composer-only policy lane. Tiering them would imply choices they do not have."""
    for agent in ("vibe", "aider", "cursor"):
        assert adapters.MODEL_TIERS[agent] == {}, agent
        for tier in adapters.MODEL_TIER_NAMES:
            assert adapters.resolve_model(agent, tier) is None, (agent, tier)


# ---------------------------------------------------------------------------
# Scarce-seat ceiling: cap routine spend WITHOUT deleting the frontier option.
# ---------------------------------------------------------------------------


def test_claude_routine_work_is_capped_at_mid():
    """Owner policy: claude's weekly is frequently the binding constraint, so routine
    'full' work runs Sonnet 5 rather than Opus 5."""
    assert adapters.tier_ceiling("claude") == "mid"
    assert adapters.effective_tier("claude", "full") == "mid"
    argv = adapters.build_command("claude", "x", mode="full")
    assert argv[argv.index("--model") + 1] == adapters.MODEL_TIERS["claude"]["mid"], argv


def test_ceiling_never_raises_a_tier():
    """A cap must only ever lower spend — cheap work must not get promoted to the ceiling."""
    for tier in ("cheap", "mid"):
        assert adapters.effective_tier("claude", tier) == tier


def test_frontier_model_stays_reachable_three_ways(monkeypatch):
    """THE requirement: a capacity-limited high-quality model must remain AVAILABLE.
    Capping routine spend must not delete the option."""
    # 1. the tier map still names it
    assert adapters.MODEL_TIERS["claude"]["full"] == "claude-opus-5"
    # 2. raising the ceiling reaches it
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "full")
    assert adapters.effective_tier("claude", "full") == "full"
    argv = adapters.build_command("claude", "x", mode="full")
    assert argv[argv.index("--model") + 1] == "claude-opus-5", argv
    monkeypatch.delenv("ORCH_CLAUDE_MAX_TIER")
    # 3. an explicit requested_model bypasses the ceiling entirely
    argv = adapters.build_command("claude", "x", mode="full", requested_model="claude-opus-5")
    assert argv[argv.index("--model") + 1] == "claude-opus-5", argv


def test_uncapped_agents_are_unaffected():
    for agent in ("codex", "gemini"):
        assert adapters.tier_ceiling(agent) is None, agent
        for tier in adapters.MODEL_TIER_NAMES:
            assert adapters.effective_tier(agent, tier) == tier


def test_ceiling_env_override_can_lower_any_seat(monkeypatch):
    monkeypatch.setenv("ORCH_CODEX_MAX_TIER", "cheap")
    assert adapters.effective_tier("codex", "full") == "cheap"
    argv = adapters.build_command("codex", "x", mode="full")
    assert argv[argv.index("--model") + 1] == adapters.MODEL_TIERS["codex"]["cheap"], argv


def test_capacity_reports_the_ceiling_not_the_uncapped_model():
    """The snapshot must not advertise a model routine routing will never spend."""
    tiers = capacity._tier_models("claude")
    assert tiers["full"] == adapters.MODEL_TIERS["claude"]["mid"], tiers
    assert capacity._tier_ceiling("claude") == "mid"


# ---------------------------------------------------------------------------
# Cursor: Composer ONLY (owner policy 2026-08-08)
# ---------------------------------------------------------------------------


def test_cursor_always_pins_composer_explicitly():
    """Omitting --model yields `auto`, which routes across every frontier model cursor sells.
    Composer-only therefore requires an EXPLICIT pin, not an omission."""
    for mode in (None, "composer", "full", "cheap", "mid", "frontier"):
        argv = adapters.build_command("cursor", "x", mode=mode)
        assert "--model" in argv, (mode, argv)
        assert argv[argv.index("--model") + 1] == adapters.CURSOR_COMPOSER_MODEL, (mode, argv)
        assert "auto" not in argv, (mode, argv)


def test_bare_frontier_cannot_blind_spend_the_pool():
    """A stray 'frontier' hint must land on Composer, never on a paid default."""
    argv = adapters.build_command("cursor", "x", mode="frontier")
    assert argv[argv.index("--model") + 1] == adapters.CURSOR_COMPOSER_MODEL, argv
    assert (
        adapters.model_identity("cursor", "frontier") == f"cursor:{adapters.CURSOR_COMPOSER_MODEL}"
    )


def test_router_never_routes_cursor_to_frontier():
    """Policy is enforced in the route table too, not just the adapter."""
    import router

    for task_type, spec in router.ROUTE_TABLE.items():
        for entry in spec["agents"]:
            if entry["agent"] == "cursor":
                assert entry["mode"] == "composer", (task_type, entry)


def test_composer_model_is_advertised_by_cursor():
    """Live guard: the Composer pin must be a model the CLI actually offers."""
    models = _live_cursor_models()
    assert adapters.CURSOR_COMPOSER_MODEL in models, (
        f"CURSOR_COMPOSER_MODEL {adapters.CURSOR_COMPOSER_MODEL!r} is not advertised by "
        f"cursor-agent. Composer ids offered: {[m for m in models if 'composer' in m]}"
    )


def test_tiers_are_distinct_and_ordered_cheap_to_full():
    """cheap/mid/full collapsing to one id would make the tier map decorative."""
    for agent in ("codex", "claude", "gemini"):
        picks = [adapters.MODEL_TIERS[agent][t] for t in adapters.MODEL_TIER_NAMES]
        assert len(set(picks)) == 3, (agent, picks)


def test_build_command_honours_every_tier():
    """The pin must actually reach argv — a tier map nothing dispatches is dead code.

    Expectations are stated post-ceiling, because that is what really dispatches: claude is
    capped at `mid`, so its `full` lane sends Sonnet 5 (see the scarce-seat ceiling tests).
    """
    expected = {
        "codex": ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"),
        "claude": ("claude-haiku-4-5", "claude-sonnet-5", "claude-sonnet-5"),
        "gemini": ("gemini-3.6-flash-low", "gemini-3.6-flash-high", "gemini-3.1-pro-high"),
    }
    for agent, models in expected.items():
        for tier, model in zip(adapters.MODEL_TIER_NAMES, models):
            argv = adapters.build_command(agent, "x", mode=tier, cwd="/tmp")
            assert argv[argv.index("--model") + 1] == model, (agent, tier, argv)
            expected_tier = adapters.effective_tier(agent, tier)
            assert model == adapters.MODEL_TIERS[agent][expected_tier], (agent, tier)


def test_gemini_cheap_and_mid_are_flash_not_pro():
    """The point of the tier map: stop paying Pro rates for cheap large-read work."""
    for tier in ("cheap", "mid"):
        assert "flash" in adapters.MODEL_TIERS["gemini"][tier], tier
    assert "pro" in adapters.MODEL_TIERS["gemini"]["full"]


def test_offload_defaults_to_the_mid_tier():
    """Wiring guard: 'mid' must be reachable from a real path, not just defined."""
    assert adapters.DEFAULT_OFFLOAD_TIER in adapters.MODEL_TIER_NAMES
    assert adapters.DEFAULT_OFFLOAD_TIER == "mid"


def test_non_tier_modes_do_not_pin_a_model():
    """'assess'/None must keep the long-standing 'never pin what we did not verify' behaviour."""
    for mode in (None, "assess"):
        argv = adapters.build_command("codex", "x", mode=mode, cwd="/tmp")
        assert "--model" not in argv, (mode, argv)


def test_tier_env_override_wins(monkeypatch):
    """Operators can retune a tier without editing code."""
    monkeypatch.setenv("ORCH_CODEX_MODEL_MID", "gpt-5.6-luna")
    assert adapters.resolve_model("codex", "mid") == "gpt-5.6-luna"
    health = adapters.model_health("codex", "mid")
    assert health["source"] == "env:ORCH_CODEX_MODEL_MID", health


def _fake_catalog(monkeypatch, models):
    """Patch the real seam every resolver goes through: advertised_models(agent, ...)."""
    monkeypatch.setattr(adapters, "advertised_models", lambda agent, **_: list(models))


def test_rename_auto_resolves_to_a_live_sibling(monkeypatch):
    """A rename must degrade the seat to a live model, never kill it."""
    monkeypatch.delenv("ORCH_GEMINI_MODEL", raising=False)
    _fake_catalog(
        monkeypatch, ["gemini-4.2-flash-high", "gemini-4.1-pro-low", "gemini-4.1-pro-high"]
    )
    health = adapters.model_health("gemini", "full")
    assert health["resolvable"] and health["source"] == "auto_from_catalog", health
    assert health["model"] == "gemini-4.1-pro-high", health  # newest pro, high over low


def test_rename_keeps_a_flash_tier_on_flash(monkeypatch):
    """Auto-resolution must not silently promote a cheap tier onto the pricier Pro seat."""
    monkeypatch.delenv("ORCH_GEMINI_MODEL_CHEAP", raising=False)
    _fake_catalog(monkeypatch, ["gemini-4.2-flash-low", "gemini-4.1-pro-high"])
    health = adapters.model_health("gemini", "cheap")
    assert health["model"] == "gemini-4.2-flash-low", health


def test_non_gemini_rename_is_reported_not_guessed(monkeypatch):
    """codex ids carry no rankable structure; a stale pin must surface, not be swapped."""
    _fake_catalog(monkeypatch, ["gpt-9.9-nova"])
    health = adapters.model_health("codex", "full")
    assert not health["resolvable"], health
    assert "no sibling matched" in health["reason"], health


def test_unreadable_model_list_does_not_condemn_the_pin(monkeypatch):
    """Offline / CLI-missing / unauthenticated is UNKNOWN, not 'model is bad'."""
    monkeypatch.delenv("ORCH_GEMINI_MODEL", raising=False)
    _fake_catalog(monkeypatch, [])
    health = adapters.model_health("gemini", "full")
    assert health["resolvable"] and health["model"] == adapters.DEFAULT_GEMINI_MODEL, health


def test_bad_operator_override_is_honoured_but_reported_broken(monkeypatch):
    """Operator intent wins at dispatch; the health report still tells the truth."""
    monkeypatch.setenv("ORCH_GEMINI_MODEL", "gemini-2.5-pro")
    _fake_catalog(monkeypatch, ["gemini-3.1-pro-high"])
    health = adapters.model_health("gemini", "full")
    assert health["model"] == "gemini-2.5-pro", health  # verbatim, not silently rewritten
    assert not health["resolvable"] and "not advertised" in health["reason"], health


def test_capacity_sheds_seat_when_model_unresolvable(monkeypatch):
    """capacity.py must stop reporting `ok` for a seat that cannot dispatch."""
    monkeypatch.setattr(
        capacity,
        "_model_health",
        lambda agent, tier="full": {
            "agent": agent,
            "tier": tier,
            "model": "gemini-2.5-pro",
            "resolvable": False,
            "source": "pinned_default",
            "advertised": ["gemini-3.1-pro-high"],
            "reason": "gemini-2.5-pro is not advertised by gemini",
        },
    )
    state, reason, meta = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert state == capacity.SHED, (state, reason)
    assert meta["availability"] == "unavailable_model_unresolved", meta
    assert meta["configured_model"] == "gemini-2.5-pro", meta


def test_capacity_gate_is_seat_level_not_gemini_special(monkeypatch):
    """The same gate must protect codex's three pins — that was the point of generalizing it.

    `_shed` is neutralised because it is MACHINE STATE, not the subject. `compute` checks the
    429-shed flag FIRST and returns two values from that path, so on a machine where codex happens
    to be rate-limit shed this test unpacked three from two and died — while passing on CI, where
    nothing is shed. It was silently exercising the shed path rather than the seat-level gate it is
    named for. Isolating the stub makes the check RUN everywhere instead of skipping (CLAUDE.md §1:
    when a check fails only because real state leaked in, the fix is isolation, never a skip).
    """
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(
        capacity,
        "_model_health",
        lambda agent, tier="full": {
            "agent": agent,
            "tier": tier,
            "model": "gpt-5.6-luna",
            "resolvable": tier != "cheap",
            "source": "pinned_default",
            "advertised": ["gpt-5.6-sol"],
            "reason": "gpt-5.6-luna is not advertised by codex",
        },
    )
    state, _reason, meta = capacity.compute("codex", capacity.AGENTS["codex"], None)
    assert state == capacity.SHED, state
    assert meta["unresolvable_tier"] == "cheap", meta


def test_capacity_stays_ok_when_model_resolves(monkeypatch):
    """Control arm: same ledger, resolvable model => the seat is usable and names its model.

    Same `_shed` neutralisation, and it matters MORE here: this is the control arm, so a machine
    with a shed flag would make it agree with the positive case for the wrong reason.
    """
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(
        capacity,
        "_model_health",
        lambda agent, tier="full": {
            "agent": agent,
            "tier": tier,
            "model": "gemini-3.1-pro-high",
            "resolvable": True,
            "source": "pinned_default",
            "advertised": ["gemini-3.1-pro-high"],
            "reason": "advertised",
        },
    )
    state, _reason, meta = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert state != capacity.SHED, state
    assert meta["configured_model"] == "gemini-3.1-pro-high", meta


# ---------------------------------------------------------------------------
# Tier policy per task type (2026-08-08). Stage 1 = cheap tier applied;
# stage 2 (review/testgen -> mid) is asserted as NOT YET applied so it can't be
# quietly forgotten — flip STAGE_2_APPLIED when it lands.
# ---------------------------------------------------------------------------

TIER_POLICY = {
    "mechanical": "cheap",
    "polish": "cheap",
    "codemod": "cheap",
    "review": "mid",
    "testgen": "mid",
    "implement": "full",
    "epic": "full",
    "cross_repo": "full",
    "runtime_ac": "full",
}
STAGE_2_APPLIED = True
_UNPINNED = ("cursor", "vibe", "aider")  # single-lane agents; tier token is documentation only


def _tiered_entries(task_type):
    import router

    return [e for e in router.ROUTE_TABLE[task_type]["agents"] if e["agent"] not in _UNPINNED]


def test_tier_policy_covers_every_task_type():
    """A task type absent from the policy would silently keep whatever mode it had."""
    import router

    assert set(TIER_POLICY) == set(router.ROUTE_TABLE), set(router.ROUTE_TABLE) ^ set(TIER_POLICY)


def test_stage1_cheap_tier_is_applied():
    for task_type in ("mechanical", "polish", "codemod"):
        for entry in _tiered_entries(task_type):
            assert entry["mode"] == "cheap", (task_type, entry)


def test_full_tier_task_types_stay_full():
    """Guard against a well-meaning cost cut on work whose mistakes are expensive."""
    for task_type in ("implement", "epic", "cross_repo", "runtime_ac"):
        for entry in _tiered_entries(task_type):
            assert entry["mode"] == "full", (task_type, entry)


def test_stage2_state_matches_the_flag():
    """Either stage 2 is applied and review/testgen are 'mid', or it is not and they are not.
    This fails the moment the two drift apart, so the staged rollout can't be half-done."""
    modes = {e["mode"] for tt in ("review", "testgen") for e in _tiered_entries(tt)}
    if STAGE_2_APPLIED:
        assert modes == {"mid"}, modes
    else:
        assert "mid" not in modes, f"stage 2 landed but STAGE_2_APPLIED is still False: {modes}"


def test_every_routed_mode_is_dispatchable():
    """Any mode in the route table must be one build_command actually understands."""
    import router

    known = set(adapters.MODEL_TIER_NAMES) | {"composer"}
    for task_type, spec in router.ROUTE_TABLE.items():
        for entry in spec["agents"]:
            mode = entry["mode"]
            assert mode in known or mode.startswith("frontier:"), (task_type, entry)


# ---------------------------------------------------------------------------
# Auth preflight: a lapsed credential must shed the seat, not fail at dispatch.
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_auth_failure_is_detected(monkeypatch):
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(
        adapters.subprocess,
        "run",
        lambda *a, **k: _Proc(
            1, "", "Error: Authentication required. Please run 'agent login' first"
        ),
    )
    health = adapters.auth_health("cursor", refresh=True)
    assert health["checked"] and not health["authenticated"], health


def test_auth_ok_when_probe_succeeds(monkeypatch):
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(
        adapters.subprocess, "run", lambda *a, **k: _Proc(0, "✓ Logged in as someone@example.com")
    )
    health = adapters.auth_health("cursor", refresh=True)
    assert health["authenticated"] and health["checked"], health


def test_exit_zero_but_failed_is_still_a_failure(monkeypatch):
    """THE defect this probe was rewritten for: cursor-agent exits 0 while printing
    'Not logged in' / an invalid-key warning. Returncode-driven logic read that as healthy."""
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(
        adapters.subprocess,
        "run",
        lambda *a, **k: _Proc(0, "\x1b[33m⚠ Warning: The provided API key is invalid.\x1b[0m"),
    )
    health = adapters.auth_health("cursor", refresh=True)
    assert health["checked"] and not health["authenticated"], health
    assert "\x1b[" not in health["reason"], health  # ANSI stripped for readability


def test_status_style_not_logged_in_is_caught(monkeypatch):
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(adapters.subprocess, "run", lambda *a, **k: _Proc(0, "Not logged in"))
    assert not adapters.auth_health("cursor", refresh=True)["authenticated"]


def test_cursor_auth_probe_reproduces_the_fleet_credential_path():
    """The probe must disable the interactive credential store, or it tests the wrong thing:
    `status`/`--list-models` would answer from a stored session the fleet never reads."""
    assert adapters.AUTH_PROBE_ENV["cursor"]["AGENT_CLI_CREDENTIAL_STORE"] == "memory"
    assert (
        "status" not in adapters.AUTH_PROBES["cursor"]
    ), "cursor-agent status reports the interactive login, not the fleet's API key"


def test_nonauth_failure_is_unknown_not_unauthenticated(monkeypatch):
    """A crashed/absent CLI must not be read as a bad credential."""
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(
        adapters.subprocess, "run", lambda *a, **k: _Proc(127, "", "command not found")
    )
    health = adapters.auth_health("cursor", refresh=True)
    assert health["authenticated"] and not health["checked"], health


def test_agents_without_a_probe_are_never_condemned():
    """Seats with no free check must read as unchecked-OK, never as broken."""
    for agent in ("vibe", "aider"):
        health = adapters.auth_health(agent)
        assert health["authenticated"] and not health["checked"], (agent, health)
        assert health["strength"] is None, (agent, health)


def test_presence_only_probes_are_labelled_as_such():
    """claude/codex `auth status` report loggedIn=true for a BOGUS token (verified 2026-08-09),
    so they must never be sold as proof the credential still works."""
    for agent in ("claude", "codex"):
        assert adapters.AUTH_PROBES[agent]["strength"] == "presence", agent


def test_validating_probes_round_trip_the_credential():
    """cursor/gemini probes hit the server, so they genuinely catch a dead credential."""
    for agent in ("cursor", "gemini"):
        assert adapters.AUTH_PROBES[agent]["strength"] == "validates", agent


def test_presence_probe_still_detects_a_missing_credential(monkeypatch):
    """A weak probe is still worth having: absence is detectable even if validity isn't."""
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(adapters.subprocess, "run", lambda *a, **k: _Proc(1, "", "Not logged in"))
    health = adapters.auth_health("claude", refresh=True)
    assert health["checked"] and not health["authenticated"], health


def test_every_seat_has_some_free_signal():
    """No seat may report UNKNOWN: seats without a CLI probe fall back to a credential-file
    check. vibe is the case that forced this — `vibe -p` bills and everything else is a TUI."""
    # The fallback chain is CLI probe -> credential file -> UNKNOWN, and `agent_auth_check`'s own
    # rule is that UNKNOWN is never a failure. A seat with neither an installed CLI nor a
    # credential file therefore has no free signal by design; asserting the absence of UNKNOWN
    # on such a machine measures the machine's installation, not this chain.
    env_prereq.require(env_prereq.seat_has_no_free_signal())
    import agent_auth_check

    for agent in agent_auth_check.AGENTS:
        row = agent_auth_check.check(agent)
        assert row["verdict"] != "UNKNOWN", (agent, row)


def test_file_only_seats_are_labelled_configured_not_ok():
    """A file check must never be sold as a working credential."""
    # CONFIGURED is the verdict for "the credential file is there and holds the key". With the
    # file absent the correct verdict is BROKEN, which the sibling test asserts directly.
    env_prereq.require(env_prereq.credential_file_absent("vibe", "aider"))
    import agent_auth_check

    for agent in ("vibe", "aider"):
        assert agent not in adapters.AUTH_PROBES, agent
        assert agent_auth_check.check(agent)["verdict"] == "CONFIGURED", agent


def test_missing_credential_file_is_broken(monkeypatch, tmp_path):
    """The weakest tier still catches the strongest failure: an absent credential."""
    import agent_auth_check

    monkeypatch.setitem(
        agent_auth_check.CREDENTIAL_FILES, "vibe", (tmp_path / "nope.env", "MISTRAL_API_KEY")
    )
    row = agent_auth_check.check("vibe")
    assert row["verdict"] == "BROKEN" and "missing" in row["detail"], row


def test_every_seat_has_a_refresh_hint():
    """A broken seat must always come with a standard path back to usable."""
    import agent_auth_check

    for agent in agent_auth_check.CREDENTIAL_FILES:
        assert agent_auth_check.REFRESH_HINT.get(agent), agent


def test_presence_pass_is_not_reported_as_verified(monkeypatch):
    """agent_auth_check must show PRESENT, not OK, for a presence-only pass."""
    monkeypatch.setattr(adapters, "_AUTH_MEMO", {})
    monkeypatch.setattr(adapters.subprocess, "run", lambda *a, **k: _Proc(0, '{"loggedIn": true}'))
    import agent_auth_check

    assert agent_auth_check.check("claude")["verdict"] == "PRESENT"


def test_auth_probes_never_spend_tokens():
    """Guard the design rule: only non-billing commands may sit in AUTH_PROBES."""
    banned = {"-p", "--print", "exec", "--prompt"}
    for agent, probe in adapters.AUTH_PROBES.items():
        assert not banned & set(probe), f"{agent} auth probe would bill: {probe}"


def test_capacity_sheds_on_auth_failure(monkeypatch):
    monkeypatch.setattr(
        capacity,
        "_auth_health",
        lambda agent: {
            "agent": agent,
            "authenticated": False,
            "checked": True,
            "reason": "Error: Authentication required",
        },
    )
    state, reason, meta = capacity.compute("cursor", capacity.AGENTS["cursor"], None)
    assert state == capacity.SHED, (state, reason)
    assert meta["availability"] == "unavailable_auth_failed", meta


def test_capacity_ignores_unknown_auth(monkeypatch):
    monkeypatch.setattr(
        capacity,
        "_auth_health",
        lambda agent: {
            "agent": agent,
            "authenticated": True,
            "checked": False,
            "reason": "no probe",
        },
    )
    assert capacity.compute("cursor", capacity.AGENTS["cursor"], None)[0] == capacity.OK


def test_parse_agy_models_ignores_banner_and_prose():
    parsed = adapters.parse_agy_models(
        "Fetching available models...\n"
        "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
        "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
        "\n"
        "some prose line without a tab\n"
    )
    assert parsed == ["gemini-3.1-pro-high", "claude-opus-4-6-thinking"], parsed


# ---------------------------------------------------------------------------
# Capability heartbeats at infrastructure code paths (2026-08-09).
# Infrastructure capabilities are never ROUTED to — they run as part of the tick — so they record
# use at their own entrypoint. The safety property is that this stays inert outside an active tick.
# ---------------------------------------------------------------------------


def test_capability_heartbeats_are_inert_outside_an_active_tick(monkeypatch):
    """ORCH_CAPABILITY_HEARTBEATS is set only by orchestrate.sh for active ticks. Without it,
    every in-process emitter must be silent, or tests and manual runs would pollute the ledger."""
    import capabilities as C

    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    assert C.production_heartbeat("offload", "invocation", ref="x") is False
    assert C.production_heartbeat("windowed-capacity-policy", "invocation", ref="x") is False


def test_infrastructure_heartbeat_never_breaks_its_caller(monkeypatch):
    """capacity.build() runs FIRST in the tick, so a capability-ledger fault (corrupt file, lock
    contention) must not be able to stop capacity from being computed."""
    import capabilities as C
    import capacity

    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    monkeypatch.setattr(
        C,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    try:
        capacity.build()
    except RuntimeError:
        raise AssertionError("capacity.build must survive a heartbeat failure")


def test_offload_survives_a_heartbeat_failure(monkeypatch):
    """Same protection on the dispatch path: recording that a capability ran must never be able
    to prevent the work itself."""
    import capabilities as C
    import dispatcher

    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    monkeypatch.setattr(
        C,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    # Harmless command so the test exercises the guard, not a real agent.
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *a, **k: ["printf", "ok"])
    out = dispatcher.offload("cursor", "x", cwd="/tmp", timeout=30)
    assert isinstance(out, dict) and out.get("exit") == 0, out


def test_daily_heartbeat_coalesces_hot_paths(monkeypatch, tmp_path):
    """event_history is uncapped and heartbeat linear-scans it, so a per-invocation heartbeat on a
    hot path degrades itself. Daily coalescing bounds growth to ~365 events/year/capability."""
    import capabilities as C

    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    ledger = tmp_path / "capabilities.json"
    rec = C._blank_capability("feedback-store")
    rec["status"] = "generated"
    C.save({"feedback-store": rec}, ledger)
    written = [
        C.daily_heartbeat("feedback-store", "invocation", ref="r", path=ledger) for _ in range(25)
    ]
    assert written.count(True) == 1, written
    stored = C.load(ledger)["feedback-store"]
    # Count the events under test, not the whole history. The property is "25 calls leave ONE
    # invocation event", and asserting on the total made an unrelated event break it: `feedback-store`
    # joined KNOWN_DECLARATIONS on 2026-08-22, so a load now also records `declaration_reconciled`.
    # An exact-total assertion turns any new bookkeeping event into a false failure here.
    invocations = [e for e in stored["event_history"] if e.get("type") == "invocation"]
    assert len(invocations) == 1, stored["event_history"]
    assert stored["last_invocation"], "the signal that matters must still be recorded"


def test_daily_heartbeat_respects_the_production_gate(monkeypatch, tmp_path):
    import capabilities as C

    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    ledger = tmp_path / "capabilities.json"
    rec = C._blank_capability("feedback-store")
    rec["status"] = "generated"
    C.save({"feedback-store": rec}, ledger)
    assert C.daily_heartbeat("feedback-store", "invocation", ref="r", path=ledger) is False
    # Same reason as above: assert that NO heartbeat was recorded, not that the history is empty.
    history = C.load(ledger)["feedback-store"]["event_history"]
    assert [e for e in history if e.get("type") == "invocation"] == [], history


def test_brain_write_path_survives_a_ledger_fault(monkeypatch, tmp_path):
    """record_run must not be stoppable by a capability-ledger fault."""
    import capabilities as C
    import feedback

    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    monkeypatch.setattr(
        C, "daily_heartbeat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down"))
    )
    old = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "f.db"
    try:
        feedback.record_run("guarded", "o/r#1", "implement", "codex")
        with feedback._conn() as c:
            assert c.execute("SELECT 1 FROM runs WHERE run_id='guarded'").fetchone()
    finally:
        feedback.DB_PATH = old


def test_every_capability_has_a_heartbeat_call_site():
    """Coverage guard: a capability with no heartbeat call site can never accrue evidence at its
    own code path, so its gate is unliftable by construction. The single exception is documented:
    docs-drift-fix-agent lives in the Workflows repo, not this tree."""
    import subprocess

    import capabilities as C
    import capability_activation_audit as audit

    EXTERNAL = {"docs-drift-fix-agent"}  # lives in Workflows/scripts, wired there or not at all
    VARIABLE_ID = {"capability:reference-sync-hygiene-test-gate"}  # id passed as a variable
    src = subprocess.run(
        ["grep", "-rn", "-A5", "heartbeat(", "--include=*.py", "."], capture_output=True, text=True
    ).stdout
    missing = []
    for cap_id in C.load():
        if cap_id in EXTERNAL or cap_id in VARIABLE_ID or cap_id.startswith("role-"):
            continue
        if f'"{cap_id}"' not in src:
            missing.append(cap_id)
    # A row whose MODULE is not in this checkout has no heartbeat call site because it has no code
    # here at all. That is wait-or-merge, not "the declaration is wrong" — the opposite action from
    # every other way this check goes red. EXTERNAL above is the DELIBERATE version of the same
    # shape, hand-listed; the note names the accidental ones, which nobody can hand-list because
    # they depend on which branch each sibling worktree is on. Same helper as the admission and
    # fixture-coverage checks, so all three agree.
    assert (
        not missing
    ), f"capabilities with no heartbeat call site: {missing}" + audit.absent_entrypoint_note(
        missing
    )


# ---------------------------------------------------------------------------
# Scoped-blocker expiry (root-cause fix 2026-08-10). 24 of 30 blockers were past their own
# expires_at and still blocking, which had emptied the backlog and stalled the review lane.
# ---------------------------------------------------------------------------


def _sentinel(tmp_path, blockers):
    import json

    p = tmp_path / "lane-handoff.json"
    p.write_text(json.dumps({"stop": {"scoped_blockers": blockers}}))
    return p


def test_expired_blocker_stops_blocking(monkeypatch, tmp_path):
    import backlog

    monkeypatch.setattr(
        backlog,
        "SENTINEL",
        _sentinel(
            tmp_path,
            {
                "o/r#1": {
                    "reason": "lapsed",
                    "await_human": True,
                    "expires_at": "2026-01-01T00:00:00Z",
                },
                "o/r#2": {
                    "reason": "current",
                    "await_human": True,
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            },
        ),
    )
    now = 1_800_000_000
    assert backlog.load_scoped_blockers(now=now) == {"o/r#2"}
    assert set(backlog.expired_scoped_blockers(now=now)) == {"o/r#1"}


def test_unbounded_or_unparseable_blocker_keeps_blocking(monkeypatch, tmp_path):
    """Fail-safe: a block with no usable expiry is a DELIBERATE block and must hold."""
    import backlog

    monkeypatch.setattr(
        backlog,
        "SENTINEL",
        _sentinel(
            tmp_path,
            {
                "o/r#3": {"reason": "no expiry", "await_human": True},
                "o/r#4": {"reason": "garbled", "await_human": True, "expires_at": "not-a-date"},
            },
        ),
    )
    now = 1_800_000_000
    assert backlog.load_scoped_blockers(now=now) == {"o/r#3", "o/r#4"}
    assert backlog.expired_scoped_blockers(now=now) == {}


def test_expiry_raises_an_owner_question_once(monkeypatch, tmp_path):
    """An expired human-awaited block must be RAISED, not silently start letting work through —
    and re-running must not re-raise it, or the surface reads as spam."""
    import backlog
    import feedback

    monkeypatch.setattr(
        backlog,
        "SENTINEL",
        _sentinel(
            tmp_path,
            {
                "o/r#5": {
                    "reason": "owner decision pending",
                    "await_human": True,
                    "expires_at": "2026-01-01T00:00:00Z",
                },
                "o/r#6": {
                    "reason": "machine reason",
                    "await_human": False,
                    "expires_at": "2026-01-01T00:00:00Z",
                },
            },
        ),
    )
    old = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "f.db"
    try:
        first = backlog.raise_expired_blocker_questions()
        assert len(first) == 1, first  # only the await_human one
        assert backlog.raise_expired_blocker_questions() == [], "must not re-raise"
        opened = feedback.open_owner_questions(limit=50)
        assert len(opened) == 1 and opened[0]["target"] == "o/r#5", opened
        # Non-blocking by contract: it auto-ratifies rather than accumulating.
        assert feedback.expire_owner_questions(now=2_000_000_000) == 1
        assert feedback.open_owner_questions(limit=50) == []
    finally:
        feedback.DB_PATH = old


def test_aider_is_backup_only_and_vibe_is_a_real_lane():
    """Lane membership is a POLICY, and it was only ever a comment.

    Owner directive 2026-06-21 made aider backup-only, and `capacity.py` says so in prose while
    `router.BACKUP_AGENTS` enforces it — but nothing asserted the pair stayed in agreement, and
    aider still appears in 8 of 9 `ROUTE_TABLE` entries. That gap is not theoretical: reading the
    route table alone, one concludes epsilon-greedy would preferentially explore aider as the
    least-observed agent. It cannot — `router.py` skips backup agents unless explicitly demanded —
    but the table LOOKS like it can, and a policy you can misread from the source is one nobody can
    rely on. Reaffirmed 2026-08-21 (owner): aider is not a lane, vibe is. Pins both directions.
    """
    import capacity
    import router

    assert router.BACKUP_AGENTS == {"aider"}, router.BACKUP_AGENTS

    cap = capacity.build()
    # NOT A LANE: aider must never be the routine choice for any task type.
    for task_type in router.ROUTE_TABLE:
        chosen = router.select_agent(task_type, cap)
        if chosen:
            assert chosen["agent"] != "aider", (task_type, chosen)

    # A LANE: vibe must be eligible in EVERY task type. The failure this catches is silent removal —
    # a table edit that drops vibe from a lane looks like nothing until that lane's evidence stops.
    for task_type, spec in router.ROUTE_TABLE.items():
        agents = [entry["agent"] for entry in spec["agents"]]
        assert "vibe" in agents, (task_type, agents)

    # THE ESCAPE HATCH SURVIVES, and it is why aider's route entries are NOT deleted. The directive
    # kept aider "reachable only on explicit demand", and that path resolves THROUGH the route
    # table — remove the entries and `--agent aider` silently stops routing.
    explicit = router.select_agent("implement", cap, only={"aider"})
    assert explicit and explicit["agent"] == "aider", explicit
