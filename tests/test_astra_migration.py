import adapters
import execution_profiles


def test_full_tier_routes_astra_and_sol_is_historical_only():
    assert adapters.resolve_model("codex", "full") == "gpt-6-astra"
    assert adapters.resolve_model("codex", "mid") == "gpt-5.6-terra"
    assert adapters.resolve_model("codex", "cheap") == "gpt-5.6-luna"
    active = {p["requested_model"] for p in execution_profiles.profiles_for_agent("codex")}
    assert "gpt-6-astra" in active
    assert "gpt-5.6-sol" not in active
    assert execution_profiles.PROFILE_RETIREMENTS["codex-5.6-sol-high"] == "codex-6-astra-high"


def test_existing_profile_database_survives_full_tier_retirement(monkeypatch):
    import copy
    import sqlite3

    current = execution_profiles.PROFILE_REGISTRY
    before = copy.deepcopy(current)
    before.pop("codex-6-astra-high")
    with sqlite3.connect(":memory:") as conn:
        monkeypatch.setattr(execution_profiles, "PROFILE_REGISTRY", before)
        execution_profiles.ensure_schema(conn)
        old = conn.execute(
            "SELECT definition_json FROM execution_profiles WHERE profile_id=?",
            ("codex-5.6-sol-high",),
        ).fetchone()[0]
        monkeypatch.setattr(execution_profiles, "PROFILE_REGISTRY", current)
        execution_profiles.ensure_schema(conn)
        assert (
            conn.execute(
                "SELECT definition_json FROM execution_profiles WHERE profile_id=?",
                ("codex-5.6-sol-high",),
            ).fetchone()[0]
            == old
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM execution_profiles WHERE profile_id=?",
                ("codex-6-astra-high",),
            ).fetchone()[0]
            == 1
        )
