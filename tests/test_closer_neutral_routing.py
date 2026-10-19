"""Real profile registry allocation, without a fixed provider or effort affinity."""

import random

import pytest

import execution_profiles
import router

AGENTS = {"codex", "claude", "gemini", "cursor", "vibe", "muse"}


def capacity():
    profiles = [
        p for a in AGENTS for p in execution_profiles.profiles_for_agent(a, transport="offload")
    ]
    return {
        "agents": {a: {"state": "ok"} for a in AGENTS},
        "profiles": {p["profile_id"]: {"state": "ok"} for p in profiles},
        "pools": {pool: {"state": "ok"} for p in profiles for pool in p["capacity_pool_ids"]},
    }


def pick(task, cap, **kwargs):
    return router.select_agent(
        task, cap, profile_transport="offload", exploration_rate=0, simulate=True, **kwargs
    )


@pytest.mark.parametrize("agent", sorted(AGENTS))
@pytest.mark.parametrize("task", ["closer_exception", "closer_repair"])
def test_each_qualified_provider_can_do_substantive_work(task, agent):
    selected = pick(task, capacity(), only={agent}, rng=random.Random(3))
    assert selected["agent"] == agent
    profile = execution_profiles.get_profile(selected["selected_profile_id"])
    assert profile["reasoning_effort"] in {"medium", "high"}
    assert profile["permission_mode"] == "workspace-write"
    assert selected["neutral_prior"] is True


def test_needed_cold_jobs_sample_equally_admissible_providers():
    selections = [pick("closer_exception", capacity(), rng=random.Random(n)) for n in range(60)]
    assert {p["agent"] for p in selections} == AGENTS
    assert all(set(p["eligible_provider_ids"]) == AGENTS for p in selections)
    assert all(p["cold_start"] and p["evidence_state"] == "sparse" for p in selections)


def test_routine_job_never_buys_high_effort_and_missing_profile_never_falls_back():
    cap = capacity()
    for n in range(12):
        selected = pick("closer_interpret", cap, rng=random.Random(n))
        assert execution_profiles.get_profile(selected["selected_profile_id"])[
            "reasoning_effort"
        ] in {"none", "minimal", "low"}
    cap["profiles"] = {}
    assert pick("closer_interpret", cap) is None


def test_native_quota_shed_then_fresh_eligibility_without_provider_ban():
    cap = capacity()
    cap["agents"]["gemini"] = {"state": "shed", "reason": "native quota", "reset_at": 1792800000}
    assert pick("closer_exception", cap, only={"gemini"}) is None
    cap["agents"]["gemini"] = {"state": "ok"}
    assert pick("closer_exception", cap, only={"gemini"})["agent"] == "gemini"


def test_one_observation_does_not_remove_other_qualified_providers():
    selected = pick(
        "closer_exception",
        capacity(),
        learned={
            a: {"rank": 0 if a == "claude" else 1, "n_obs": int(a == "claude")} for a in AGENTS
        },
    )
    assert set(selected["eligible_provider_ids"]) == AGENTS
    assert selected["evidence_state"] == "sparse"
    assert pick("closer_exception", capacity(), only={"aider"}) is None
