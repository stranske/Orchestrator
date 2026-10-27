"""Causal role-selection tests through actual router/dispatcher/campaign paths."""

import copy
import json
import random
from types import SimpleNamespace

import pytest
from test_closer_role_qualification import NOW, record

import adapters
import closer_campaign as campaign
import closer_dispatcher as dispatcher
import closer_resources as resources
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


def test_low_effort_strong_qualification_reaches_actual_router_and_dispatcher(monkeypatch):
    profile, capacity = setup(
        monkeypatch, "claude-sonnet-5-5-low", roles.STRONG_CRITERION_ADJUDICATION
    )
    picked = router.select_agent("closer_exception", capacity, only={"claude"})
    assert picked["selected_profile_id"] == profile["profile_id"]
    routed, status = dispatcher._exception_route(with_status=True)
    assert status == "available" and routed["profile_id"] == profile["profile_id"]
    assert profiles.get_profile(routed["profile_id"])["reasoning_effort"] == "low"
    assert dispatcher._pipeline_capacity() == {"io": 4, "low": 0, "exception": 1}


@pytest.mark.parametrize("profile_id", ["codex-6-luna-high", "claude-sonnet-5-5-high"])
def test_high_effort_and_capacity_alone_never_reach_router_or_dispatcher(monkeypatch, profile_id):
    profile, capacity = setup(monkeypatch, profile_id)
    assert router.select_agent("closer_exception", capacity, only={profile["agent"]}) is None
    assert dispatcher._exception_route(with_status=True)[0] is None
    assert dispatcher._pipeline_capacity() == {"io": 4, "low": 0, "exception": 0}


def test_high_effort_classifier_is_not_rejected_by_legacy_effort_filter(monkeypatch):
    profile, _capacity = setup(monkeypatch, "claude-sonnet-5-5-high", roles.BOUNDED_CLASSIFICATION)
    routed, status = dispatcher._pipeline_low_route(with_status=True, data_only=True)
    assert status == "available" and routed["profile_id"] == profile["profile_id"]
    assert dispatcher._pipeline_capacity() == {"io": 4, "low": 1, "exception": 0}


def test_campaign_and_dispatcher_apply_the_same_classification_role(tmp_path, monkeypatch):
    profile, capacity = setup(monkeypatch, "claude-sonnet-5-5-high", roles.BOUNDED_CLASSIFICATION)
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps(capacity))
    selected = campaign.select_route(path, now=NOW, router=router, profiles=profiles)
    assert selected == {"agent": "claude", "profile_id": profile["profile_id"]}
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    assert "hold" in campaign.select_route(path, now=NOW, router=router, profiles=profiles)


def test_adjudication_qualification_does_not_qualify_repair(monkeypatch):
    profile, capacity = setup(
        monkeypatch, "claude-sonnet-5-5-low", roles.STRONG_CRITERION_ADJUDICATION
    )
    assert dispatcher._profile_exception_qualified(capacity, profile, repair=False)
    assert not dispatcher._profile_exception_qualified(capacity, profile, repair=True)
    assert dispatcher._exception_route(repair=True, with_status=True)[0] is None


def test_shared_pool_busy_blocks_every_profile_on_that_balance(monkeypatch):
    profile, _capacity = setup(monkeypatch, "claude-sonnet-5-5-high", roles.BOUNDED_CLASSIFICATION)
    load = {"pool:" + profile["capacity_pool_ids"][0]: 1}
    assert dispatcher._pipeline_low_route(load=load, with_status=True) == (None, "busy")


@pytest.mark.parametrize(
    "change", ["missing", "expired", "adapter", "build", "transport", "retired", "registry", "cap"]
)
def test_selection_rechecks_role_and_transport_before_invocation(monkeypatch, change):
    profile, _capacity = setup(
        monkeypatch, "claude-sonnet-5-5-low", roles.STRONG_CRITERION_ADJUDICATION
    )
    load = {}
    route = dispatcher._exception_route(load=load)
    assert route is not None
    proof = copy.deepcopy(next(iter(roles.ROLE_QUALIFICATIONS.values())))
    if change == "missing":
        monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    elif change == "expired":
        proof["expires_at"] = NOW
    elif change == "adapter":
        proof["profile_identity"]["adapter_version"] = "stale-adapter"
    elif change == "build":
        proof["confinement"]["adapter_build_sha256"] = "c" * 64
    elif change == "retired":
        monkeypatch.setitem(
            profiles.PROFILE_RETIREMENTS, profile["profile_id"], "unrelated-successor"
        )
    elif change == "registry":
        monkeypatch.setitem(
            profiles.PROFILE_REGISTRY,
            profile["profile_id"],
            dict(profile, requested_model="changed"),
        )
    elif change == "cap":
        monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "cheap")
    else:
        monkeypatch.setattr(adapters, "data_only_problem", lambda *_: "unqualified binary")
    if change != "missing":
        monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {proof["qualification_id"]: proof})
    invoked = []
    with pytest.raises(resources.ResourceBlocked):
        dispatcher._execute_profile_route(
            route, invoked.append, lane="exception", load=load, deadline=NOW + 5
        )
    assert invoked == []
    assert all(value == 0 for value in load.values())


def test_candidate_metadata_cannot_enable_unsupported_read_only_transport(monkeypatch):
    profile, capacity = setup(monkeypatch, "codex-6-luna-low", roles.BOUNDED_CLASSIFICATION)
    assert roles.is_qualified(profile, roles.BOUNDED_CLASSIFICATION)
    assert not roles.is_runtime_qualified(profile, roles.BOUNDED_CLASSIFICATION)
    assert router.select_agent("closer_interpret", capacity, only={"codex"}) is None
    assert dispatcher._pipeline_low_route(with_status=True, data_only=True)[0] is None


def test_qualified_opus_cannot_bypass_explicit_automatic_cap(monkeypatch):
    profile, capacity = setup(
        monkeypatch, "claude-opus-5-5-low", roles.STRONG_CRITERION_ADJUDICATION
    )
    assert dispatcher._exception_route(with_status=True)[0] is not None
    monkeypatch.setenv("ORCH_CLAUDE_MAX_TIER", "mid")
    assert router.select_agent("closer_exception", capacity, only={"claude"}) is None
    assert dispatcher._exception_route(with_status=True)[0] is None
    assert not dispatcher._profile_exception_qualified(capacity, profile, repair=False)


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
