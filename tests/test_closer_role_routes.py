"""Causal role-selection tests through actual router/dispatcher/campaign paths."""

import random
from types import SimpleNamespace

import pytest

# These are synthetic runtime/effort/cap causality tests, not production cohort evidence.
from test_closer_neutral_routing import synthetic_quality_boundary  # noqa: F401
from test_closer_role_qualification import NOW, record

import adapters
import closer_dispatcher as dispatcher
import closer_role_qualification as roles
import execution_profiles as profiles
import router


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(roles, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "b" * 64)
    monkeypatch.setattr(
        adapters,
        "data_only_problem",
        lambda agent, *_: None if agent == "claude" else "unsupported",
    )
    monkeypatch.setattr(dispatcher, "_closer_learned", lambda *_: None)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    original = router.select_agent
    monkeypatch.setattr(
        router,
        "select_agent",
        lambda *a, **kw: original(*a, **kw, simulate=True, exploration_rate=0),
    )


def setup(monkeypatch, profile_id, role=None):
    profile = profiles.get_profile(profile_id)
    capacity = {
        "generated_at": NOW,
        "agents": {profile["agent"]: {"state": "ok"}},
        "profiles": {profile_id: {"state": "ok"}},
        "pools": {pool: {"state": "ok"} for pool in profile["capacity_pool_ids"]},
    }
    monkeypatch.setattr(dispatcher, "_fresh_capacity_report", lambda: capacity)
    if role:
        proof = record(profile, role)
        if role == roles.BOUNDED_CLASSIFICATION:
            descriptor = dispatcher._controlled_model_contract("closer_interpret")
            proof.update(
                production_model_contract=descriptor,
                execution_limits={
                    "schema": "closer-data-only-limits/v1",
                    "role": role,
                    "model_contract_sha256": descriptor["contract_sha256"],
                    "max_prompt_bytes": 65536,
                    "max_final_bytes": 8192,
                    "max_native_result_bytes": 65536,
                    "max_output_tokens": 2048,
                    "max_turns": 1,
                    "timeout_seconds": 45,
                },
            )
        monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {proof["qualification_id"]: proof})
    return profile, capacity


def test_adjudication_qualification_does_not_qualify_repair(monkeypatch):
    profile, capacity = setup(
        monkeypatch, "claude-sonnet-5-5-low", roles.STRONG_CRITERION_ADJUDICATION
    )
    assert dispatcher._profile_exception_qualified(capacity, profile, repair=False)
    assert not dispatcher._profile_exception_qualified(capacity, profile, repair=True)
    assert dispatcher._exception_route(repair=True, with_status=True)[0] is None


def test_missing_role_proof_is_not_reported_as_provider_quota(monkeypatch):
    _profile, _capacity = setup(monkeypatch, "claude-sonnet-5-5-low")
    assert dispatcher._pipeline_low_route(with_status=True, data_only=True) == (
        None,
        "qualification_unavailable",
    )
    assert dispatcher._exception_route(with_status=True) == (None, "qualification_unavailable")
    pending = dispatcher.role_qualification_pending("offline/example#1", "exact-revision")
    assert pending["pending_kind"] == "external_event"
    assert pending["pending_evidence"]["revision"] == "exact-revision"
    assert pending["pending_evidence"]["role"] == roles.STRONG_CRITERION_ADJUDICATION
    assert "capacity" not in pending.get("resource_evidence", {})
    assert pending["authority"] is False


@pytest.mark.parametrize("seed", range(10))
def test_generic_review_automatic_profile_selection_respects_explicit_cap(monkeypatch, seed):
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "mid")
    cohort = profiles.profiles_for_agent("claude", transport="offload")
    capacity = {
        "agents": {"claude": {"state": "ok"}},
        "profiles": {p["profile_id"]: {"state": "ok"} for p in cohort},
        "pools": {pool: {"state": "ok"} for p in cohort for pool in p["capacity_pool_ids"]},
    }
    picked = router.select_agent(
        "review",
        capacity,
        only={"claude"},
        profile_transport="offload",
        learned={},
        rng=random.Random(seed),
    )
    assert picked is not None
    selected = profiles.get_profile(picked["selected_profile_id"])
    assert selected["requested_model"] in {
        adapters.MODEL_TIERS["claude"]["cheap"],
        adapters.MODEL_TIERS["claude"]["mid"],
    }
