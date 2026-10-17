"""Failure-boundary coverage for the recurrence check's pure and offline paths."""

from types import SimpleNamespace

import pytest

import capability_recurrence_check as recurrence  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_paths(tmp_path):
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setenv("ORCH_STATE_DIR", str(tmp_path))
        scoped.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
        scoped.setattr(recurrence.capabilities, "REG", tmp_path / "capabilities.json")
        yield


def test_tick_env_attempt_names_unreadable_bash_and_spawn_failures(monkeypatch, tmp_path):
    """A failed prologue read or process launch is an environment result, never a silent empty env."""
    script = tmp_path / "orchestrate.sh"
    script.write_text("export ORCH_EXAMPLE=1\n_gh_gate() { :; }\n", encoding="utf-8")
    monkeypatch.setattr(recurrence, "ORCHESTRATE", script)

    def unreadable(*_args, **_kwargs):
        raise OSError("cloud mount unavailable")

    monkeypatch.setattr(type(script), "read_text", unreadable)
    assert recurrence._tick_env_attempt()[1]["reason"] == "script_unreadable"

    monkeypatch.undo()
    monkeypatch.setattr(recurrence, "ORCHESTRATE", script)
    monkeypatch.setattr(
        recurrence.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert recurrence._tick_env_attempt()[1]["reason"] == "bash_missing"

    monkeypatch.setattr(
        recurrence.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("EAGAIN"))
    )
    assert recurrence._tick_env_attempt()[1]["reason"] == "spawn_failed"


def test_tick_env_failure_messages_keep_tree_and_machine_causes_distinct():
    missing = recurrence.tick_env_failure_message(
        {"reason": "script_missing", "outcome": "defect", "attempts": [{"detail": "gone"}]}
    )
    aborted = recurrence.tick_env_failure_message(
        {"reason": "nonzero_exit", "outcome": "script_error", "attempts": [{"returncode": 2}]}
    )
    machine = recurrence.tick_env_failure_message(
        {"reason": "timeout", "outcome": "environment", "attempts": [{"detail": "slow"}]}
    )
    assert "DEFECT (tree)" in missing
    assert "PROLOGUE ABORTED" in aborted
    assert "ENVIRONMENT, not the tree" in machine


def test_predicate_checks_turn_missing_dependencies_into_nonfiring_rows(monkeypatch):
    """A recurrence report remains readable when an optional local subsystem is unavailable."""
    import builtins

    checks = (
        (recurrence._predicate_heartbeat, ("offload",), "capabilities"),
        (recurrence._check_deliberate_break_executable, (), "runtime_ac"),
        (recurrence._check_gemini_isolation, (), "adapters"),
        (recurrence._check_keepalive_escalation, (), "keepalive_supervisor"),
        (recurrence._check_synthesis_gate, (), "capabilities"),
        (recurrence._check_model_trial_gate, (), "capabilities"),
        (recurrence._check_thompson, (), "router"),
        (recurrence._check_rail_exercise_cadence, (), "rail_exercise"),
        (recurrence._check_route_weights_export, (), "route_weights_export"),
    )
    real_import = builtins.__import__
    for check, args, blocked_module in checks:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                builtins,
                "__import__",
                lambda name, *a, **kw: (
                    (_ for _ in ()).throw(RuntimeError("unavailable"))
                    if name == blocked_module
                    else real_import(name, *a, **kw)
                ),
            )
            result = check(*args)
        assert result["fires"] is False
        assert result["detail"]["error"] == "unavailable"


def test_external_caller_and_tick_value_failure_paths(monkeypatch):
    import capabilities

    monkeypatch.setattr(
        capabilities,
        "load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    failed = recurrence._external_caller_state("docs-drift-fix-agent")
    assert failed["fires"] is False and failed["external_blocker"] is True

    monkeypatch.undo()
    monkeypatch.setattr(recurrence, "tick_env", lambda: {})
    monkeypatch.setattr(
        recurrence, "_TICK_ENV_DIAG", {"reason": "timeout", "outcome": "environment"}
    )
    monkeypatch.delenv("ORCH_EXAMPLE", raising=False)
    assert recurrence.as_the_tick_sees_it("ORCH_EXAMPLE") == (None, "tick-unresolved:timeout")
    monkeypatch.setenv("ORCH_EXAMPLE", "1")
    assert recurrence.as_the_tick_sees_it("ORCH_EXAMPLE") == ("1", "ambient")
    assert recurrence.tick_value_unknown("tick-unconsulted")
    assert recurrence.tick_value_unknown("tick-unresolved:timeout")


def test_fetch_and_live_label_fallback_never_call_gh_when_offline(monkeypatch):
    calls = []
    monkeypatch.setattr(
        recurrence.subprocess,
        "run",
        lambda *_a, **_k: calls.append(True)
        or SimpleNamespace(returncode=1, stdout="", stderr="no auth"),
    )
    assert recurrence._fetch_issue("Orchestrator", 1) is None
    fixture = {
        "live": True,
        "repo": "Orchestrator",
        "issue": 1,
        "labels": ["bug"],
        "title": "saved",
    }
    assert (
        recurrence._labels_of(fixture, offline=False)[2]
        == "frozen (live read failed) Orchestrator#1"
    )
    assert len(calls) == 2
    assert recurrence._labels_of(fixture, offline=True) == (
        ["bug"],
        "saved",
        "frozen (live read failed) Orchestrator#1",
    )
    assert len(calls) == 2


def test_replay_records_fixture_errors_instead_of_raising(monkeypatch):
    import adversarial
    import runtime_ac_gate

    fixtures = (
        {"capability": "gate", "kind": "gate_required", "source": "s", "labels": [], "title": "t"},
        {"capability": "stakes", "kind": "high_stakes", "source": "s", "labels": [], "title": "t"},
    )
    predicates = (
        {
            "capability": "predicate",
            "source": "s",
            "check": lambda: (_ for _ in ()).throw(RuntimeError("broken")),
        },
    )
    monkeypatch.setattr(
        runtime_ac_gate,
        "eligibility",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gate broken")),
    )
    monkeypatch.setattr(
        adversarial,
        "high_stakes_reason",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stakes broken")),
    )
    monkeypatch.setattr(recurrence, "FIXTURES", fixtures)
    monkeypatch.setattr(recurrence, "PREDICATE_FIXTURES", predicates)
    report = recurrence.replay(offline=True)
    assert report["would_fire"] == 0
    assert "error" in report["rows"][0]["eligibility"]
    assert report["rows"][1]["reason"].startswith("ERROR")
    assert report["rows"][2]["detail"]["error"] == "broken"


def test_replay_uses_live_labels_and_successful_routing_without_network(monkeypatch):
    """Live fixtures retain their provenance while every routing shape stays locally controlled."""
    fixture = {"live": True, "repo": "Orchestrator", "issue": 666, "labels": [], "title": "old"}
    monkeypatch.setattr(
        recurrence,
        "_fetch_issue",
        lambda *_args: {"labels": [{"name": "task:test"}], "title": "current title"},
    )
    assert recurrence._labels_of(fixture, offline=False) == (
        ["task:test"],
        "current title",
        "live Orchestrator#666",
    )

    import adversarial
    import backlog
    import runtime_ac_gate

    monkeypatch.setattr(backlog, "classify", lambda labels: "testgen")
    monkeypatch.setattr(runtime_ac_gate, "eligibility", lambda *_args: {"required": True})
    monkeypatch.setattr(adversarial, "high_stakes_reason", lambda *_args: "risk:major")
    monkeypatch.setattr(
        recurrence,
        "FIXTURES",
        (
            {
                "capability": "routing",
                "kind": "classify",
                "expect_task_type": "testgen",
                "labels": ["task:test"],
                "title": "t",
                "source": "s",
            },
            {
                "capability": "gate",
                "kind": "gate_required",
                "labels": [],
                "title": "t",
                "source": "s",
            },
            {
                "capability": "risk",
                "kind": "high_stakes",
                "labels": [],
                "title": "t",
                "source": "s",
            },
        ),
    )
    monkeypatch.setattr(recurrence, "PREDICATE_FIXTURES", ())
    report = recurrence.replay(offline=True)
    assert report["would_fire"] == report["total"] == 3
    assert report["rows"][0]["actual"] == "testgen"
    assert report["rows"][1]["eligibility"] == {"required": True}
    assert report["rows"][2]["reason"] == "risk:major"


def test_format_report_renders_miss_and_repaired_fire_details():
    text = recurrence.format_report(
        {
            "total": 2,
            "would_fire": 1,
            "would_miss": 1,
            "rows": [
                {
                    "capability": "risk",
                    "kind": "high_stakes",
                    "fires": False,
                    "source": "s",
                    "lane": "closer",
                    "labels": [],
                    "reason": None,
                },
                {
                    "capability": "classify",
                    "kind": "classify",
                    "fires": True,
                    "source": "s",
                    "actual": "testgen",
                    "label_repair_applied": "task:test",
                },
            ],
        }
    )
    assert "lane=closer" in text
    assert "via auto-label 'task:test'" in text
