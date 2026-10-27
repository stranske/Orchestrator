"""Real profile registry allocation, without a fixed provider or effort affinity."""

import random

import pytest
from closer_role_support import fixture_role_verdict

import closer_role_qualification as roles
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
    assert fixture_role_verdict(profile, roles.TASK_ROLES[task])
    assert profile["permission_mode"] == "workspace-write"
    assert selected["neutral_prior"] is True


def test_needed_cold_jobs_sample_equally_admissible_providers():
    cap = capacity()
    cap["agents"]["codex"]["pacing"] = {"state": "ok", "routing_weight": 1.0}
    selections = [pick("closer_exception", cap, rng=random.Random(n)) for n in range(60)]
    assert {p["agent"] for p in selections} == AGENTS
    assert all(set(p["eligible_provider_ids"]) == AGENTS for p in selections)
    assert all(p["cold_start"] and p["evidence_state"] == "sparse" for p in selections)


def test_routine_job_uses_qualified_profiles_and_missing_profile_never_falls_back():
    cap = capacity()
    for n in range(12):
        selected = pick("closer_interpret", cap, rng=random.Random(n))
        assert fixture_role_verdict(
            execution_profiles.get_profile(selected["selected_profile_id"]),
            roles.BOUNDED_CLASSIFICATION,
        )
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


@pytest.fixture(autouse=True)
def synthetic_role_boundary(monkeypatch):
    # Allocator/capacity tests use an explicit synthetic qualification oracle.
    from closer_role_support import install_synthetic_role_verdict

    install_synthetic_role_verdict(monkeypatch)


@pytest.fixture(autouse=True)
def synthetic_quality_boundary(monkeypatch):
    # This file tests neutral allocation, not the reviewed production cohort.
    # Its existing runtime predicate is synthetic, so quality scope is explicit
    # fixture data too. Real committed-policy causality is tested separately.
    import closer_role_selection as selection

    subjects = {
        key: {
            "profile_id": key,
            "identity": {
                name: profile[name]
                for name in ("agent", "provider", "requested_model", "reasoning_effort")
            },
            "quality_status": "guarded_eligible",
            "runtime_enrollment_granted": False,
            "limits": [],
            "quality_evidence": {"id": "synthetic-routing-fixture", "sha256": "0" * 64},
        }
        for key, profile in execution_profiles.PROFILE_REGISTRY.items()
    }
    policy = {
        "schema": selection.SCHEMA,
        "policy_version": "synthetic-neutral-routing-test",
        "subjects": subjects,
        "default_categories": {role: role for role in roles.ROLES},
        "categories": {
            role: {
                "role": role,
                "candidates": list(subjects),
                "requirements": [],
                "terminal_fallback": "fixture_hold",
            }
            for role in roles.ROLES
        },
        "fallback_rules": {},
    }
    monkeypatch.setattr(selection, "load_policy", lambda: policy)
