import pytest

import execution_profiles as profiles


@pytest.mark.parametrize("effort", ["low", "high"])
def test_historical_flash_profiles_remain_addressable(effort):
    old = f"gemini-3.7-flash-{effort}"
    successor_effort = "low" if effort == "low" else "medium"
    new = f"gemini-4-argon-{successor_effort}"
    assert profiles.get_profile(old)["requested_model"] == old
    assert profiles.resolve_production_profile_id(old) == new
    automatic = {p["profile_id"] for p in profiles.profiles_for_agent("gemini")}
    assert old not in automatic
    assert new in automatic
    decision = profiles.select_profile(
        "review", None, [old], rng_seed=7, allow_retired_profiles=True
    )
    assert profiles.replay_decision(decision)["selected_profile_id"] == old
