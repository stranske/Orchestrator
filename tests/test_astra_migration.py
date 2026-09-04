import adapters
import execution_profiles


def test_full_tier_routes_astra_and_sol_is_historical_only():
    assert adapters.resolve_model("codex", "full") == "gpt-6-astra"
    assert adapters.resolve_model("codex", "mid") == "gpt-5.6-terra"
    assert adapters.resolve_model("codex", "cheap") == "gpt-5.6-luna"
    active = {p["requested_model"] for p in execution_profiles.profiles_for_agent("codex")}
    assert "gpt-6-astra" in active
    assert "gpt-5.6-sol" not in active
    assert (
        execution_profiles.get_profile("codex-5.6-sol-high")["successor_profile_id"]
        == "codex-6-astra-high"
    )
