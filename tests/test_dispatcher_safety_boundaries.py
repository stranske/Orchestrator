"""Focused coverage for dispatcher decisions that must stay local and deterministic."""

from __future__ import annotations

import json

import pytest

import dispatcher


@pytest.fixture(autouse=True)
def isolated_dispatcher_boundaries(tmp_path, monkeypatch):
    """A boundary regression may never launch a process or touch live runtime state."""
    monkeypatch.setattr(dispatcher, "linked_open_prs", lambda _target: [])
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv(dispatcher.credential_redaction.SECRET_FILES_ENV, "")
    monkeypatch.delenv(dispatcher.credential_redaction.REDACTION_DISABLED_ENV, raising=False)
    monkeypatch.delenv(dispatcher.credential_redaction.EXPOSURE_SCAN_ENV, raising=False)

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


@pytest.mark.parametrize("profile", [False, True])
def test_required_lineage_failure_releases_unlaunched_claim(tmp_path, monkeypatch, profile):
    releases = []
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher.execution_profiles, "get_profile", lambda _: {"profile_id": "p"})
    monkeypatch.setattr(dispatcher.claims, "release", lambda *args: releases.append(args))
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.feedback,
        "record_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("lineage unavailable")),
    )
    plan = {
        "agent": "codex",
        "target": "o/r#1",
        "mode": "medium",
        "lane": "opener",
        "task_type": "implement",
        "cwd": str(tmp_path),
        "wrapped": "never",
        "run_id": "run",
        "review_loop": True,
    }
    if profile:
        plan["selected_profile_id"] = "p"
    with pytest.raises(RuntimeError):
        dispatcher._spawn(plan)
    assert releases == [("o/r#1", "codex")]


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


def test_gemini_timeout_alignment_preserves_seconds_and_receipt_margin():
    absent = ["agy", "--print", "prompt"]
    dispatcher._align_gemini_print_timeout(absent, 60)
    assert absent == ["agy", "--print", "prompt"]

    seconds = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(seconds, 75)
    assert seconds == ["agy", "--print-timeout", "60s"]

    # Preserve sub-minute outer budgets rather than imposing an accidental one-minute floor.
    non_integral = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(non_integral, 95)
    assert non_integral == ["agy", "--print-timeout", "80s"]

    diagnosis = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(diagnosis, 119)
    assert diagnosis == ["agy", "--print-timeout", "104s"]

    two_minutes = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(two_minutes, 135)
    assert two_minutes == ["agy", "--print-timeout", "120s"]

    near_zero = ["agy", "--print-timeout", "99m"]
    dispatcher._align_gemini_print_timeout(near_zero, 1)
    assert near_zero == ["agy", "--print-timeout", "1s"]


def test_agent_log_tail_uses_relative_cwd_and_redacts_before_truncation(tmp_path):
    log = tmp_path / "agent.log"
    token = "sk-" + "ant-oat01-" + "Q" * 40
    log.write_text("diagnostic prefix\nsecret=" + token)
    argv = ["agent", "--log-file", "agent.log"]

    complete = dispatcher._agent_log_tail_from_argv(argv, tmp_path)
    assert token not in complete
    assert "[REDACTED:" in complete

    tail = dispatcher._agent_log_tail_from_argv(argv, tmp_path, max_chars=12)
    assert tail == complete[-12:]
    assert token[-12:] not in tail
    assert len(tail) == 12
    assert dispatcher._agent_log_tail_from_argv(["agent"], tmp_path) == ""


def test_default_offload_timeout_honors_explicit_and_gemini_defaults(monkeypatch):
    monkeypatch.setattr(dispatcher, "DEFAULT_OFFLOAD_TIMEOUT", 1800)
    monkeypatch.setattr(dispatcher, "DEFAULT_GEMINI_OFFLOAD_TIMEOUT", 600)

    assert dispatcher._default_offload_timeout("codex", 5) == 5
    assert dispatcher._default_offload_timeout("gemini", None) == 600
    assert dispatcher._default_offload_timeout("codex", None) == 1800


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"exit": 0, "output": "reviewed"}, 0),
        ({"exit": 0, "output": "", "error": "refused"}, 1),
        ({"exit": 2, "output": ""}, 1),
        (KeyboardInterrupt(), 1),
    ],
)
def test_main_offload_preserves_transport_arguments_and_failure_status(
    tmp_path, monkeypatch, capsys, result, expected
):
    seen = {}
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bounded review")

    def offload(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(dispatcher, "offload", offload)
    assert (
        dispatcher.main(
            [
                "offload",
                "--agent",
                "cursor",
                "--cwd",
                str(tmp_path),
                "--mode",
                "review",
                "--timeout",
                "95",
                "--profile-id",
                "profile-1",
                "--research-round",
                "round-1",
                "--task-type",
                "review",
                "--subject",
                "o/r#1",
                "--isolate",
                "--prompt-file",
                str(prompt),
            ]
        )
        == expected
    )
    assert seen == {
        "args": ("cursor", "bounded review"),
        "kwargs": {
            "cwd": str(tmp_path),
            "mode": "review",
            "timeout": 95,
            "isolate": True,
            "profile_id": "profile-1",
            "research_round": "round-1",
            "task_type": "review",
            "subject_id": "o/r#1",
        },
    }
    captured = capsys.readouterr()
    if isinstance(result, KeyboardInterrupt):
        assert "interrupted by orchestrator" in captured.err
    elif result.get("error"):
        assert "refused" in captured.err
    elif result.get("output"):
        assert captured.out.strip() == "reviewed"


@pytest.mark.parametrize("error", [None, "claimed elsewhere"])
def test_main_delegate_preserves_profile_lineage_and_exit_status(tmp_path, monkeypatch, error):
    seen = {}
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("finite implementation")

    def delegate(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return {"error": error} if error else {"pid": 42}

    monkeypatch.setattr(dispatcher, "delegate", delegate)
    monkeypatch.delenv("ORCH_ROLE_SHADOW", raising=False)
    assert dispatcher.main(
        [
            "delegate",
            "--agent",
            "cursor",
            "--target",
            "o/r#1",
            "--lane",
            "closer",
            "--mode",
            "composer",
            "--profile-id",
            "profile-1",
            "--task-type",
            "testgen",
            "--influenced-by-role-run-id",
            "role-1",
            "--influenced-by-role-run-id",
            "role-2",
            "--prompt-file",
            str(prompt),
        ]
    ) == (1 if error else 0)
    assert seen == {
        "args": ("cursor", "o/r#1", "closer", "finite implementation", "composer"),
        "kwargs": {
            "task_type": "testgen",
            "profile_id": "profile-1",
            "continue_pr": None,
            "influenced_by_role_run_ids": ["role-1", "role-2"],
        },
    }


@pytest.mark.parametrize(
    "argv", [[], ["--dry-run"], ["--no-heartbeat"], ["--dry-run", "--no-heartbeat"]]
)
def test_main_routing_flags_reach_only_the_injected_runner(monkeypatch, argv):
    decision = {"assignments": [{"target": "o/r#1"}]}
    calls = []
    monkeypatch.setattr(dispatcher, "load_decision", lambda: decision)
    monkeypatch.setattr(dispatcher, "run", lambda *a, **kw: calls.append((a, kw)) or {})
    assert dispatcher.main(argv) == 0
    assert calls == [
        ((decision,), {"dry_run": "--dry-run" in argv, "heartbeat": "--no-heartbeat" not in argv})
    ]


def test_main_prompt_and_corpus_routes_never_enter_live_dispatch(monkeypatch, capsys):
    import partitioned_review

    monkeypatch.setattr(dispatcher, "run", lambda *_a, **_kw: pytest.fail("live routing"))
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda task, cwd, agent: f"{agent}:{task}")
    assert dispatcher.main(["--show-prompt"]) == 2
    assert dispatcher.main(["--show-prompt", "cursor"]) == 0
    assert "cursor:SHOW-PROMPT TASK" in capsys.readouterr().out
    seen = []
    monkeypatch.setattr(partitioned_review, "main", lambda argv, **kw: seen.append((argv, kw)) or 7)
    assert dispatcher.main(["review-corpus", "--manifest", "packet.json"]) == 7
    assert seen == [(["--manifest", "packet.json"], {"offload_fn": dispatcher.offload})]


@pytest.mark.parametrize(
    ("reportable", "start_fails"), [(True, False), (True, True), (False, True)]
)
def test_spawn_profile_lineage_is_recorded_before_process_and_closed_on_start_failure(
    tmp_path, monkeypatch, reportable, start_fails
):
    from types import SimpleNamespace

    calls = []
    profile = {"profile_id": "profile-1", "provider": "openai", "requested_model": "test-model"}
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher, "ORCH_DIR", tmp_path)
    monkeypatch.setattr(dispatcher, "CLAIMS_PY", tmp_path / "claims.py")
    monkeypatch.setattr(dispatcher.execution_profiles, "get_profile", lambda _id: profile)
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda _agent: (reportable, "test")
    )
    monkeypatch.setattr(dispatcher.adapters, "wrapper_python", lambda: "/opt/anaconda3/bin/python3")
    monkeypatch.setattr(dispatcher.adapters, "done_marker_cmd", lambda *_a, **_kw: "marker-double")
    monkeypatch.setattr(
        dispatcher.adapters, "record_ledger", lambda *_a, **kw: calls.append(("ledger", kw))
    )
    monkeypatch.setattr(dispatcher.claims, "update_metadata", lambda *_a, **_kw: False)
    for name in [
        "record_run",
        "record_influence_edge",
        "record_profile_decision",
        "record_execution_attempt",
        "attach_profile_attempt_to_decision",
        "complete_profile_attempt_unresolved",
    ]:
        monkeypatch.setattr(
            dispatcher.feedback, name, lambda *a, _name=name, **kw: calls.append((_name, a, kw))
        )

    def popen(argv, **kwargs):
        calls.append(("process", argv, kwargs))
        assert calls[0][0] == "record_run"
        assert any(c[0] == "record_profile_decision" for c in calls)
        if start_fails:
            raise OSError("synthetic process refusal")
        return SimpleNamespace(pid=42)

    monkeypatch.setattr(dispatcher.subprocess, "Popen", popen)
    plan = {
        "agent": "codex",
        "target": "o/r#1",
        "mode": "medium",
        "task_type": "testgen",
        "cwd": str(tmp_path),
        "wrapped": "never-executed",
        "run_id": "run-1",
        "selected_profile_id": "profile-1",
        "profile_decision": {"decision_id": "decision-1"},
        "rejected_role_run_ids": ["role-rejected"],
    }
    if start_fails:
        with pytest.raises(OSError, match="synthetic process refusal"):
            dispatcher._spawn(plan)
    else:
        assert dispatcher._spawn(plan) == 42
        assert (
            "claim metadata update returned false"
            in next((tmp_path / "logs").glob("*.log")).read_text()
        )
    assert plan["routing_metadata"]["profile_attempt_ids"] == ["attempt:profile:run-1"]
    attempts = [c for c in calls if c[0] == "record_execution_attempt"]
    completions = [c for c in calls if c[0] == "complete_profile_attempt_unresolved"]
    assert len(attempts) == int(reportable)
    assert len(completions) == int(reportable and start_fails)
    if attempts:
        assert attempts[0][2]["attempt_id"] == "attempt:profile:run-1"
    if completions:
        assert completions[0][2]["fallback_reason"] == "profile_process_start_failed"
        assert completions[0][2]["status"] == "failed"


@pytest.mark.parametrize(
    ("agent", "override"), [("cursor", False), ("codex", False), ("codex", True)]
)
def test_delegate_preserves_explicit_profile_and_ownership_lineage(
    tmp_path, monkeypatch, agent, override
):
    calls = []
    profile = {
        "agent": agent,
        "profile_id": "profile-1",
        "requested_model": "test-model",
        "reasoning_effort": "medium",
        "permission_mode": "workspace-write",
    }
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        dispatcher.execution_profiles, "default_codex_delegate_profile", lambda *_a: "profile-1"
    )
    monkeypatch.setattr(
        dispatcher.execution_profiles, "codex_operator_tier_override", lambda _m: override
    )
    monkeypatch.setattr(dispatcher.execution_profiles, "get_profile", lambda _id: profile)
    monkeypatch.setattr(dispatcher.claims, "reap_stale", lambda: calls.append("reap"))
    monkeypatch.setattr(dispatcher.claims, "claim", lambda *_a: calls.append("claim") or True)
    monkeypatch.setattr(
        dispatcher.claims, "release", lambda *_a: pytest.fail("successful claim released early")
    )

    def plan(assignment, **kwargs):
        calls.append(("plan", dict(assignment), kwargs))
        return {**assignment, "cwd": str(tmp_path), "run_id": "run-1"}

    monkeypatch.setattr(dispatcher, "plan_dispatch", plan)
    monkeypatch.setattr(dispatcher, "_spawn", lambda d: calls.append(("spawn", dict(d))) or 42)
    monkeypatch.setattr(
        dispatcher, "_delegate_lane_heartbeat", lambda d: calls.append(("heartbeat", d["run_id"]))
    )
    monkeypatch.setattr(
        dispatcher.feedback, "record_run", lambda *a, **kw: calls.append(("record", a, kw))
    )
    result = dispatcher.delegate(
        agent,
        "o/r#1",
        "closer",
        "finite task",
        task_type="testgen",
        profile_id="profile-1" if agent == "cursor" else None,
        influenced_by_role_run_ids=["role-1"],
        influenced_by_skill_event_ids=["skill-1"],
        influenced_by_workflow_ids=["workflow-1"],
        capability_ids=["existing"],
        capability_version_ids=["existing@v1"],
        acceptance_gate_ids=["gate-1"],
    )
    assert result == {
        "pid": 42,
        "log": str(tmp_path / "logs" / f"o__r_1.{agent}.log"),
        "worktree": str(tmp_path),
        "agent": agent,
        "target": "o/r#1",
        "mode": "composer" if agent == "cursor" else "mid",
        "run_id": "run-1",
    }
    assignment = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "plan")
    assert assignment["influenced_by_role_run_ids"] == ["role-1"]
    assert assignment["capability_version_ids"] == ["existing@v1"]
    assert assignment["acceptance_gate_ids"] == ["gate-1"]
    spawned = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "spawn")
    assert spawned["capability_ids"] == [
        "existing",
        dispatcher.DELEGATE_LANE_CAPABILITIES["testgen"],
    ]
    if override:
        assert assignment["reasoning_effort"] == "medium"
        assert "selected_profile_id" not in assignment
        record = next(c for c in calls if isinstance(c, tuple) and c[0] == "record")
        assert record[2]["mode"] == "local"
        assert record[2]["reasoning_level"] == "mid"
    else:
        assert assignment["selected_profile_id"] == "profile-1"
        assert assignment["transport"] == "local"
        assert assignment["profile_assignment_probability"] == 1.0
        assert not any(isinstance(c, tuple) and c[0] == "record" for c in calls)


@pytest.mark.parametrize(
    "failure", ["unknown-profile", "wrong-agent", "claimed", "no-plan", "plan-error"]
)
def test_delegate_refuses_invalid_profiles_or_claims_and_releases_failed_plans(
    monkeypatch, failure
):
    releases = []

    def profile(_id):
        if failure == "unknown-profile":
            raise ValueError("unknown profile")
        return {
            "agent": "vibe" if failure == "wrong-agent" else "cursor",
            "requested_model": "test-model",
            "reasoning_effort": "medium",
            "permission_mode": "workspace-write",
        }

    monkeypatch.setattr(dispatcher.execution_profiles, "get_profile", profile)
    monkeypatch.setattr(dispatcher.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(dispatcher.claims, "claim", lambda *_a: failure != "claimed")
    monkeypatch.setattr(dispatcher.claims, "holder", lambda _t: {"agent": "incumbent"})
    monkeypatch.setattr(dispatcher.claims, "release", lambda *a: releases.append(a))
    monkeypatch.setattr(
        dispatcher,
        "plan_dispatch",
        lambda *_a, **_kw: None if failure == "no-plan" else {"error": "plan refused"},
    )
    monkeypatch.setattr(
        dispatcher, "_spawn", lambda _d: pytest.fail("refused delegation reached spawn")
    )
    result = dispatcher.delegate("cursor", "o/r#1", "closer", "finite task", profile_id="profile-1")
    assert "error" in result
    expected = {
        "unknown-profile": "unknown profile",
        "wrong-agent": "does not belong",
        "claimed": "incumbent",
        "no-plan": "unknown/unbuildable",
        "plan-error": "plan refused",
    }
    assert expected[failure] in result["error"]
    assert releases == ([("o/r#1", "cursor")] if failure in {"no-plan", "plan-error"} else [])


@pytest.mark.parametrize("failing", [False, True])
def test_delegate_heartbeat_binds_run_identity_and_never_blocks_delivery(
    monkeypatch, capsys, failing
):
    import capabilities

    calls = []

    def heartbeat(*a, **kw):
        calls.append((a, kw))
        if failing:
            raise OSError("telemetry unavailable")

    monkeypatch.setattr(capabilities, "heartbeat", heartbeat)
    d = {"task_type": "testgen", "target": "o/r#1", "run_id": "run-1", "agent": "cursor"}
    dispatcher._delegate_lane_heartbeat(d)
    assert len(calls) == 1
    assert calls[0][0] == (dispatcher.DELEGATE_LANE_CAPABILITIES["testgen"], "invocation")
    assert calls[0][1]["metadata"] == {
        "source": "dispatcher.delegate",
        "run_id": "run-1",
        "task_type": "testgen",
        "agent": "cursor",
    }
    assert (
        calls[0][1]["idempotency_key"]
        == f"delegate:run-1:{dispatcher.DELEGATE_LANE_CAPABILITIES['testgen']}"
    )
    if failing:
        assert "telemetry unavailable" in capsys.readouterr().err
