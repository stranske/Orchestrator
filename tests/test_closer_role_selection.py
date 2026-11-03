"""Quality policy cannot stand in for runtime qualification or action authority."""

import random

import pytest

import closer_role_qualification as roles
import closer_role_selection as selection
import execution_profiles
import router


def capacity():
    ps = list(execution_profiles.PROFILE_REGISTRY.values())
    return {
        "agents": {p["agent"]: {"state": "ok"} for p in ps},
        "profiles": {p["profile_id"]: {"state": "ok"} for p in ps},
        "pools": {k: {"state": "ok"} for k in execution_profiles.CAPACITY_POOLS},
    }


def pick(task="closer_exception", **kw):
    return router.select_agent(
        task,
        capacity(),
        profile_transport="offload",
        exploration_rate=0,
        rng=random.Random(2),
        simulate=True,
        **kw
    )


def admit(monkeypatch, ids):
    # Explicit unit boundary. This does not create a role record or enroll a model.
    monkeypatch.setattr(roles, "is_runtime_qualified", lambda p, r: p["profile_id"] in ids)


def test_quality_without_runtime_record_never_dispatches(monkeypatch):
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    assert selection.quality_scope(
        execution_profiles.get_profile("codex-6.1-sol-high"),
        roles.STRONG_CRITERION_ADJUDICATION,
    )
    assert pick(only={"codex"}) is None


def test_sol_high_is_strong_and_sonnet_v2_optin_is_role_scoped(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high", "claude-sonnet-5-5-high-v2"})
    assert pick(only={"codex"})["selected_profile_id"] == "codex-6.1-sol-high"
    p = pick(only={"claude"})
    assert p["selected_profile_id"] == "claude-sonnet-5-5-high-v2"
    assert p["closer_selection"]["action_authority"] is False
    assert "claude-sonnet-5-5-high-v2" not in {
        p["profile_id"] for p in execution_profiles.profiles_for_agent("claude")
    }


def test_missing_profiles_and_wrong_effort_never_alias():
    p = execution_profiles.get_profile("cursor-composer-2.5")
    assert (
        selection.quality_scope(p, roles.STRONG_CRITERION_ADJUDICATION, "named_diagnostic") is None
    )
    p = execution_profiles.get_profile("codex-6.1-sol-high")
    p["reasoning_effort"] = "low"
    assert selection.quality_scope(p, roles.STRONG_CRITERION_ADJUDICATION) is None
    assert (
        selection.candidates_for_agent("muse", roles.STRONG_CRITERION_ADJUDICATION, "offload") == []
    )
    assert (
        selection.candidates_for_agent(
            "codex", roles.BOUNDED_CLASSIFICATION, "offload", "ordinary_static"
        )
        == []
    )


@pytest.mark.parametrize("category", ["unknown", "protected_named_authority", "ordinary_static"])
def test_unknown_protected_or_wrong_role_category_holds(monkeypatch, category):
    admit(monkeypatch, set(execution_profiles.PROFILE_REGISTRY))
    assert pick(closer_category=category) is None


def test_bounded_scope_must_be_trusted_host_explicit(monkeypatch):
    admit(monkeypatch, set(execution_profiles.PROFILE_REGISTRY))
    assert pick("closer_interpret") is None
    assert pick("closer_correction_classification") is None


def test_neutral_diverse_pool_not_fixed_sonnet(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high", "claude-sonnet-5-5-high-v2"})
    picks = [
        router.select_agent(
            "closer_exception",
            capacity(),
            profile_transport="offload",
            exploration_rate=0,
            rng=random.Random(n),
            simulate=True,
        )
        for n in range(20)
    ]
    assert {p["agent"] for p in picks} == {"codex", "claude"}


def test_independent_request_requires_admitted_distinct_known_provider(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high", "claude-sonnet-5-5-high-v2"})
    assert pick(independent_of_provider="anthropic")["requested_provider"] == "openai"
    assert pick(independent_of_provider="anthropic", only={"claude"}) is None


def test_operator_model_pin_cannot_become_unprofiled_closer(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high"})
    monkeypatch.setenv("ORCH_CODEX_MODEL_MID", "gpt-6-luna")
    assert pick(only={"codex"}) is None


def test_matching_model_pin_preserves_qualified_high_effort(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high"})
    monkeypatch.setenv("ORCH_CODEX_MODEL_MID", "gpt-6.1-sol")
    p = pick(only={"codex"})
    assert p["selected_profile_id"] == "codex-6.1-sol-high"
    assert p["reasoning_effort"] == "high"


def test_runtime_expiry_on_final_recheck_never_unprofiled_fallback(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high"})
    original = router._route_profiles
    calls = []

    def changing(*a, **kw):
        calls.append(1)
        return original(*a, **kw) if len(calls) == 1 else []

    monkeypatch.setattr(router, "_route_profiles", changing)
    assert pick(only={"codex"}) is None


def test_pool_shed_can_choose_another_admitted_provider(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high", "claude-sonnet-5-5-high-v2"})
    cap = capacity()
    cap["pools"]["codex-subscription"]["state"] = "shed"
    p = router.select_agent(
        "closer_exception",
        cap,
        profile_transport="offload",
        exploration_rate=0,
        rng=random.Random(1),
        simulate=True,
    )
    assert p["requested_provider"] == "anthropic"


def test_unknown_execution_never_becomes_retry_or_action():
    p = selection.fallback_decision(
        roles.STRONG_CRITERION_ADJUDICATION,
        None,
        "unknown_execution",
        cleanup_terminal=True,
    )
    assert p["action"] == "hold_do_not_repeat" and p["dispatch"] is False
    p = selection.fallback_decision(
        roles.STRONG_CRITERION_ADJUDICATION, None, "known_answer_rejected"
    )
    assert p["action"] == "hold_do_not_repeat"
    p = selection.fallback_decision(
        roles.STRONG_CRITERION_ADJUDICATION, None, "unavailable_before_start"
    )
    assert p["new_runtime_and_budget_admission_required"] and not p["dispatch"]


def test_direct_profile_route_also_obeys_quality_scope(monkeypatch):
    import closer_dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_profile_exception_qualified", lambda *a, **kw: True)
    monkeypatch.setattr(dispatcher, "_fresh_capacity_report", capacity)
    assert dispatcher._validated_pipeline_exception_route("cursor-composer-2.5") is None
    assert (
        dispatcher._validated_pipeline_exception_route("codex-6.1-sol-high")["closer_selection"][
            "action_authority"
        ]
        is False
    )


def test_pipeline_preselection_can_enumerate_explicit_sonnet(monkeypatch):
    import closer_dispatcher as dispatcher

    admit(monkeypatch, {"claude-sonnet-5-5-high-v2"})
    monkeypatch.setattr(dispatcher, "_fresh_capacity_report", capacity)
    monkeypatch.setattr(dispatcher, "_closer_learned", lambda _: None)
    route, status = dispatcher._exception_route(with_status=True)
    assert status == "available"
    assert route["profile_id"] == "claude-sonnet-5-5-high-v2"


def test_deterministic_repair_and_unbound_static_are_qualification_holds(monkeypatch):
    import closer_dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_fresh_capacity_report", capacity)
    assert dispatcher._exception_route(repair=True, with_status=True) == (
        None,
        "qualification_unavailable",
    )
    assert dispatcher._pipeline_low_route(closer_category="ordinary_static", with_status=True) == (
        None,
        "qualification_unavailable",
    )


@pytest.mark.parametrize("origin", ["", "unknown", True, {}, []])
def test_invalid_independence_origin_holds(monkeypatch, origin):
    admit(monkeypatch, {"codex-6.1-sol-high", "claude-sonnet-5-5-high-v2"})
    assert pick(independent_of_provider=origin) is None


def test_known_nonpool_producer_can_use_distinct_admitted_provider(monkeypatch):
    admit(monkeypatch, {"codex-6.1-sol-high", "claude-sonnet-5-5-high-v2"})
    assert pick(independent_of_provider="google")["requested_provider"] in {"openai", "anthropic"}


def test_controlled_contract_binds_installed_policy_and_consuming_sources(monkeypatch, tmp_path):
    import hashlib

    import closer_dispatcher as closer

    policy = tmp_path / "selection.json"
    policy.write_bytes(selection.POLICY_PATH.read_bytes())
    monkeypatch.setattr(selection, "POLICY_PATH", policy)
    before = closer._controlled_model_contract("closer_exception")
    assert (
        before["sources"]["config/closer_role_selection.json"]
        == hashlib.sha256(policy.read_bytes()).hexdigest()
    )
    assert "closer_role_selection.py" in before["sources"]
    assert "router.py" in before["sources"]
    policy.write_bytes(policy.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source-owned bytes"):
        closer._controlled_model_contract("closer_exception")


def test_policy_source_refuses_symlink_or_nonregular_bytes(tmp_path):
    actual = tmp_path / "actual.json"
    actual.write_bytes(selection.POLICY_PATH.read_bytes())
    link = tmp_path / "link.json"
    link.symlink_to(actual)
    with pytest.raises(ValueError, match="source alias"):
        selection.load_policy(link)
    with pytest.raises(ValueError, match="physical source"):
        selection.load_policy(tmp_path)


def test_bounded_classifier_receivers_hold_without_host_category(monkeypatch):
    import closer_dispatcher as closer

    monkeypatch.setattr(
        closer, "_fresh_capacity_report", lambda: pytest.fail("unmapped category reached capacity")
    )
    assert closer._pipeline_low_route(with_status=True) == (None, "qualification_unavailable")
    assert closer._exception_route(substantive=False, with_status=True) == (
        None,
        "qualification_unavailable",
    )


@pytest.mark.parametrize("flat", [False, True])
def test_policy_loader_uses_actual_source_shape_without_host_fallback(tmp_path, flat):
    import json
    import subprocess
    import sys

    import paths

    root = tmp_path / "isolated-image"
    modules = root if flat else root / "src"
    modules.mkdir(parents=True)
    (root / "config").mkdir()
    for name in ("paths.py", "closer_role_selection.py"):
        (modules / name).write_bytes((paths.MODULE_DIR / name).read_bytes())
    policy = (
        root / selection.POLICY_FILENAME if flat else root / "config" / selection.POLICY_FILENAME
    )
    policy.write_bytes(selection.POLICY_PATH.read_bytes())
    code = (
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "import paths,closer_role_selection as s;"
        "print(json.dumps([str(paths.REPO_ROOT),str(s.POLICY_PATH),s.load_policy()['schema']]))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(modules)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == [
        str(root.resolve()),
        str(policy.resolve()),
        selection.SCHEMA,
    ]
