import adapters
import execution_profiles


def test_full_tier_routes_sol_and_astra_remains_explicit():
    assert adapters.resolve_model("codex", "full") == "gpt-6.1-sol"
    assert adapters.resolve_model("codex", "mid") == "gpt-5.6-terra"
    assert adapters.resolve_model("codex", "cheap") == "gpt-6-luna"
    active = {p["requested_model"] for p in execution_profiles.profiles_for_agent("codex")}
    assert "gpt-6-astra" in active
    assert "gpt-6.1-sol" in active
    assert "gpt-5.6-sol" not in active
    assert execution_profiles.default_codex_profile("implement", "full") == "codex-6.1-sol-high"
    assert execution_profiles.get_profile("codex-6-astra-high")["reasoning_effort"] == "high"


def test_retired_sol_luna_profiles_resolve_successors_and_stay_addressable(tmp_path, monkeypatch):
    binary = tmp_path / "codex-profile-bin"
    binary.touch()
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN", binary)
    assert (
        execution_profiles.resolve_production_profile_id("codex-5.6-sol-high")
        == "codex-6.1-sol-high"
    )
    assert (
        execution_profiles.resolve_production_profile_id("codex-5.6-luna-low") == "codex-6-luna-low"
    )
    legacy = execution_profiles.get_profile("codex-5.6-sol-high")
    assert legacy["lifecycle_status"] == "active"
    assert legacy["requested_model"] == "gpt-5.6-sol"
    successor = execution_profiles.get_profile("codex-6.1-sol-high")
    argv = adapters.build_command("codex", "x", profile=successor, transport="local")
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    legacy_argv = adapters.build_command("codex", "x", profile=legacy, transport="local")
    assert legacy_argv[legacy_argv.index("--model") + 1] == "gpt-5.6-sol"
    automatic = execution_profiles.select_profile(
        "implement",
        "o/r#current",
        ["codex-5.6-sol-high", "codex-6.1-sol-high"],
        rng_seed=0,
        scores={"codex-5.6-sol-high": 1.0, "codex-6.1-sol-high": 0.0},
        exploration=True,
    )
    assert automatic["selected_profile_id"] == "codex-6.1-sol-high"
    assert automatic["gate_results"]["codex-5.6-sol-high"]["eligible"] is False
    explicit_trial = execution_profiles.select_profile(
        "model_profile_trial",
        "o/r#legacy-canary",
        ["codex-5.6-sol-high"],
        rng_seed=0,
        allow_retired_profiles=True,
    )
    assert explicit_trial["selected_profile_id"] == "codex-5.6-sol-high"


def test_global_codex_fallback_keeps_exact_profile_model_and_effort(tmp_path, monkeypatch):
    global_codex = tmp_path / "codex"
    global_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    global_codex.chmod(0o755)
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN", tmp_path / "absent-app-codex")
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN_EXPLICIT", False)
    monkeypatch.setattr(
        adapters.shutil, "which", lambda name: str(global_codex) if name == "codex" else None
    )
    profile = execution_profiles.get_profile("codex-6.1-sol-medium")
    argv = adapters.build_command("codex", "x", profile=profile, transport="local")
    assert argv[0] == str(global_codex)
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="medium"'


def test_existing_profile_database_accepts_new_immutable_profiles(tmp_path, monkeypatch):
    import copy
    import sqlite3

    current = execution_profiles.PROFILE_REGISTRY
    new_ids = {
        "codex-6.1-sol-low",
        "codex-6.1-sol-medium",
        "codex-6.1-sol-high",
        "codex-6-luna-low",
        "codex-6-luna-high",
    }
    before = copy.deepcopy({pid: p for pid, p in current.items() if pid not in new_ids})
    with sqlite3.connect(tmp_path / "old-profiles.db") as conn:
        monkeypatch.setattr(execution_profiles, "PROFILE_REGISTRY", before)
        execution_profiles.ensure_schema(conn)
        old = dict(conn.execute("SELECT profile_id, definition_json FROM execution_profiles"))
        assert set(old) == set(before)
        monkeypatch.setattr(execution_profiles, "PROFILE_REGISTRY", current)
        execution_profiles.ensure_schema(conn)
        updated = dict(conn.execute("SELECT profile_id, definition_json FROM execution_profiles"))
        assert {pid: updated[pid] for pid in old} == old
        assert set(updated) == set(current)
        assert new_ids <= set(updated)
