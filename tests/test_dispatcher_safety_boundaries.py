"""Focused coverage for dispatcher decisions that must stay local and deterministic."""

from __future__ import annotations

import json

import pytest

import dispatcher


@pytest.fixture(autouse=True)
def isolated_dispatcher_boundaries(tmp_path, monkeypatch):
    """A boundary regression may never launch a process or touch live runtime state."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))

    def forbid_process(*_args, **_kwargs):
        pytest.fail("dispatcher boundary test attempted a subprocess or agent launch")

    monkeypatch.setattr(dispatcher.subprocess, "run", forbid_process)
    monkeypatch.setattr(dispatcher.subprocess, "Popen", forbid_process)


def test_routing_metadata_keeps_only_meaningful_assignment_provenance():
    assignment = {
        "reason": "ranked coverage work",
        "exploration": True,
        "exploration_mode": "thompson-hybrid",
        "capacity_state": "ok",
        "capacity_policy": "",
        "selected_profile_id": "profile-1",
        "requested_model": "gpt-test",
        "reasoning_effort": "high",
        "permission_mode": "workspace-write",
        "transport": "local",
        "profile_policy_version": "v1",
        "profile_assignment_probability": 0.25,
        "profile_rng_seed": 7,
        "profile_decision": {"decision_id": "decision-1"},
        "profile_attempt_ids": ["attempt-1"],
        "capability_decision": {"decision": "eligible"},
        "eligible_capability_ids": ["capability-1"],
        "capability_rejection_reasons": {"capability-2": "not-ready"},
        "selected_capability_id": "capability-1",
        "selected_capability_version_id": "capability-1@v1",
        "capability_policy_version": "v2",
        "capability_assignment_probability": 1.0,
        "capability_rng_seed": 11,
        "capability_fallback": False,
        "requested_profile_id": "requested-profile",
    }

    assert dispatcher._routing_metadata(assignment) == {
        "source": "router_assignment",
        "reason": "ranked coverage work",
        "exploration": True,
        "exploration_mode": "thompson-hybrid",
        "capacity_state": "ok",
        "selected_profile_id": "profile-1",
        "requested_model": "gpt-test",
        "reasoning_effort": "high",
        "permission_mode": "workspace-write",
        "transport": "local",
        "profile_policy_version": "v1",
        "profile_assignment_probability": 0.25,
        "profile_rng_seed": 7,
        "profile_decision_id": "decision-1",
        "profile_attempt_ids": ["attempt-1"],
        "capability_decision": {"decision": "eligible"},
        "eligible_capability_ids": ["capability-1"],
        "capability_rejection_reasons": {"capability-2": "not-ready"},
        "selected_capability_id": "capability-1",
        "selected_capability_version_id": "capability-1@v1",
        "capability_policy_version": "v2",
        "capability_assignment_probability": 1.0,
        "capability_rng_seed": 11,
        "capability_fallback": False,
        "requested_profile_id": "requested-profile",
    }
    assert dispatcher._routing_metadata({"target": "o/r#7"}) is None


@pytest.mark.parametrize(
    ("error", "log_tail", "stderr", "expected"),
    [
        (None, None, None, False),
        ("connection reset", None, None, True),
        (None, "TLS handshake failed", None, True),
        (None, None, "network is unreachable", True),
        ("connection refused", None, "permission denied", False),
        ("HTTP 502", None, None, True),
        ("quota exhausted", None, None, False),
    ],
)
def test_transient_network_classifier_never_retries_auth_or_usage_failures(
    error, log_tail, stderr, expected
):
    assert dispatcher._is_transient_network_failure(error, log_tail, stderr) is expected


def test_claim_offload_log_retries_a_taken_name_without_reusing_its_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path)
    monkeypatch.setattr(dispatcher.time, "time_ns", lambda: 123)
    (tmp_path / "offload.codex.123.log").write_text("owned by another run\n")

    run_id, log = dispatcher._claim_offload_log("codex")

    assert run_id == "offload:codex:124"
    assert log == tmp_path / "offload.codex.124.log"
    assert log.exists()
    assert (tmp_path / "offload.codex.123.log").read_text() == "owned by another run\n"


def test_claim_offload_log_fails_loudly_after_the_bounded_collision_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path)
    monkeypatch.setattr(dispatcher, "OFFLOAD_LOG_CLAIM_ATTEMPTS", 2)
    monkeypatch.setattr(dispatcher.time, "time_ns", lambda: 123)
    for ns in (123, 124):
        (tmp_path / f"offload.codex.{ns}.log").write_text("taken\n")

    with pytest.raises(RuntimeError, match="2 consecutive values ending at 124"):
        dispatcher._claim_offload_log("codex")


def test_select_offload_profile_refuses_unresolvable_gemini_before_selection(monkeypatch):
    monkeypatch.setattr(
        dispatcher.execution_profiles,
        "profiles_for_agent",
        lambda *_args, **_kwargs: [{"profile_id": "gemini-profile", "requested_model": "model"}],
    )
    monkeypatch.setattr(dispatcher.adapters, "effective_tier", lambda *_: "mid")
    monkeypatch.setattr(
        dispatcher.adapters,
        "model_health",
        lambda *_: {"resolvable": False, "model": None},
    )
    monkeypatch.setattr(
        dispatcher.execution_profiles,
        "select_profile",
        lambda *_args, **_kwargs: pytest.fail("an unresolved Gemini model must not be selected"),
    )

    assert dispatcher._select_offload_profile("gemini", "mid") is None


def test_select_offload_profile_uses_the_resolved_tier_and_deterministic_envelope(monkeypatch):
    profiles = [
        {"profile_id": "other", "requested_model": "other-model"},
        {"profile_id": "matching", "requested_model": "resolved-model"},
    ]
    seen = {}
    monkeypatch.setattr(
        dispatcher.execution_profiles, "profiles_for_agent", lambda *_args, **_kwargs: profiles
    )
    monkeypatch.setattr(dispatcher.adapters, "effective_tier", lambda *_: "mid")
    monkeypatch.setattr(
        dispatcher.adapters,
        "model_health",
        lambda *_: {"resolvable": True, "model": "resolved-model"},
    )

    def select(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {"selected_profile_id": "matching"}

    monkeypatch.setattr(dispatcher.execution_profiles, "select_profile", select)
    monkeypatch.setattr(
        dispatcher.execution_profiles,
        "get_profile",
        lambda profile_id: {"profile_id": profile_id},
    )

    assert dispatcher._select_offload_profile("gemini", "full") == {"profile_id": "matching"}
    assert seen["args"][:3] == ("offload", None, ["matching"])
    assert seen["kwargs"] == {
        "rng_seed": 0,
        "scores": {"matching": 0.0},
        "exploration": False,
        "exploration_policy": "deterministic-offload-profile",
    }


def test_load_decision_preserves_valid_json_and_fails_closed_on_bad_or_missing_input(
    tmp_path, monkeypatch
):
    decision = tmp_path / "routing-decision.json"
    monkeypatch.setattr(dispatcher, "DECISION_JSON", decision)

    assert dispatcher.load_decision() == {"assignments": []}
    decision.write_text(json.dumps({"assignments": [{"target": "o/r#7"}]}))
    assert dispatcher.load_decision() == {"assignments": [{"target": "o/r#7"}]}
    decision.write_text("not json")
    assert dispatcher.load_decision() == {"assignments": []}


def test_gemini_timeout_alignment_never_exceeds_outer_timeout():
    absent = ["agy", "--print", "prompt"]
    dispatcher._align_gemini_print_timeout(absent, 60)
    assert absent == ["agy", "--print", "prompt"]

    seconds = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(seconds, 75)
    assert seconds == ["agy", "--print-timeout", "1m"]

    near_zero = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(near_zero, 1)
    assert near_zero == ["agy", "--print-timeout", "1m"]


def test_agent_log_tail_uses_relative_cwd_and_redacts_before_truncation(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("secret=sk-super-secret-token\n" + "x" * 20)

    tail = dispatcher._agent_log_tail_from_argv(
        ["agent", "--log-file", "agent.log"], tmp_path, max_chars=12
    )

    assert "sk-super-secret-token" not in tail
    assert len(tail) == 12
    assert dispatcher._agent_log_tail_from_argv(["agent"], tmp_path) == ""


def test_default_offload_timeout_honors_explicit_and_gemini_defaults(monkeypatch):
    monkeypatch.setattr(dispatcher, "DEFAULT_OFFLOAD_TIMEOUT", 1800)
    monkeypatch.setattr(dispatcher, "DEFAULT_GEMINI_OFFLOAD_TIMEOUT", 600)

    assert dispatcher._default_offload_timeout("codex", 5) == 5
    assert dispatcher._default_offload_timeout("gemini", None) == 600
    assert dispatcher._default_offload_timeout("codex", None) == 1800
