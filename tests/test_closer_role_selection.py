"""Quality policy cannot stand in for runtime qualification or action authority."""

import pytest

import closer_role_qualification as roles
import closer_role_selection as selection
import execution_profiles


def capacity():
    ps = list(execution_profiles.PROFILE_REGISTRY.values())
    return {
        "agents": {p["agent"]: {"state": "ok"} for p in ps},
        "profiles": {p["profile_id"]: {"state": "ok"} for p in ps},
        "pools": {k: {"state": "ok"} for k in execution_profiles.CAPACITY_POOLS},
    }


def admit(monkeypatch, ids):
    # Explicit unit boundary. This does not create a role record or enroll a model.
    monkeypatch.setattr(roles, "is_runtime_qualified", lambda p, r: p["profile_id"] in ids)


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
