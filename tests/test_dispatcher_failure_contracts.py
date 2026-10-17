"""Hermetic dispatcher regressions for failures after planning but before delivery."""

from __future__ import annotations

import shlex
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatcher as d
import research_subjects


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """One undo stack restores the true process/network functions after every case."""
    m = monkeypatch
    for key in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        m.setenv(key, str(tmp_path / key))
    m.setenv(d.credential_redaction.SECRET_FILES_ENV, "")
    m.delenv(d.credential_redaction.REDACTION_DISABLED_ENV, raising=False)
    m.delenv("ORCH_OFFLOAD_DISABLED", raising=False)
    m.setattr(d, "REAL_HOME", tmp_path / "home")
    m.setattr(d, "AGENT_RUNTIME_DIR", tmp_path / "runtime")
    m.setattr(d, "DISPATCH_LOG_DIR", tmp_path / "logs")
    m.setattr(d.feedback, "DB_PATH", tmp_path / "brain.db")

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected external process or network call")

    m.setattr(d.subprocess, "run", forbidden)
    m.setattr(d.subprocess, "Popen", forbidden)
    m.setattr(socket, "create_connection", forbidden)
    m.setattr(socket.socket, "connect", forbidden)


@pytest.mark.parametrize("helper", [d._env_int, d._env_float])
def test_invalid_numeric_configuration_uses_the_callers_default(monkeypatch, helper):
    monkeypatch.setenv("ORCH_TEST_NUMBER", "not-a-number")
    assert helper("ORCH_TEST_NUMBER", 17) == 17


def test_owner_decisions_preserve_explicit_authority_and_fail_open_on_unreadable_store(monkeypatch):
    seen = []

    def decisions(**kwargs):
        seen.append(kwargs)
        return [{"question": "Publish?", "decision": "Defer"}]

    monkeypatch.setattr(d.feedback, "owner_decisions_for", decisions)
    assert d._owner_decision_block("o/r#7").endswith("- Publish? -> Defer")
    assert seen == [{"repo": "o/r", "target": "o/r#7"}]

    def broken(**kwargs):
        raise OSError("fixture database unavailable")

    monkeypatch.setattr(d.feedback, "owner_decisions_for", broken)
    assert d._owner_decision_block("o/r#7") == ""


def test_runtime_link_tolerates_a_concurrent_creator_without_replacing_it(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "config" / "target"
    source.write_text("fixture settings")

    def race(path, source):
        path.write_text("other writer")
        raise FileExistsError("concurrent creation")

    monkeypatch.setattr(Path, "symlink_to", race)
    d._runtime_link(source, target)
    assert target.read_text() == "other writer"


@pytest.mark.parametrize(
    "config",
    [
        "model = 'fixture'\n",
        "[session_logging]\nenabled = false\n",
        "[session_logging]\nenabled = true\n[other]\nx = 1\n",
    ],
)
def test_vibe_runtime_config_inserts_one_private_log_path_without_changing_other_settings(
    tmp_path, config
):
    source, target, logs = tmp_path / "source.toml", tmp_path / "private.toml", tmp_path / "logs"
    source.write_text(config)
    target.write_text("old output")
    d._write_vibe_runtime_config(source, target, logs)
    result = target.read_text()
    assert result.count(f'save_dir = "{logs}"') == 1
    assert source.read_text() == config
    if "model =" in config:
        assert "model = 'fixture'" in result
    if "[other]" in config:
        assert "x = 1" in result
        assert result.index("save_dir") < result.index("[other]")
    assert "enabled = false" in result if "enabled = false" in config else True


def test_missing_vibe_source_leaves_existing_runtime_config_untouched(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("retained")
    d._write_vibe_runtime_config(tmp_path / "missing", target, tmp_path / "logs")
    assert target.read_text() == "retained"


@pytest.mark.parametrize("xdg", [False, True])
def test_gh_config_fallback_stays_anchored_to_the_dispatchers_home(tmp_path, monkeypatch, xdg):
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if xdg:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / "xdg" / "gh" if xdg else d.REAL_HOME / ".config" / "gh"
    assert d.gh_config_dir() == expected.absolute()


def test_guard_write_failure_removes_private_temporary_file_and_keeps_original(
    tmp_path, monkeypatch
):
    target = tmp_path / "guard" / "gh"
    target.parent.mkdir()
    target.write_text("old guard")
    monkeypatch.setattr(d, "_gh_guard_files", lambda guard: {target: "new guard"})

    def refuse(*args):
        raise PermissionError("fixture replace denied")

    monkeypatch.setattr(d.os, "replace", refuse)
    with pytest.raises(PermissionError, match="fixture replace denied"):
        d._ensure_gh_guard()
    assert target.read_text() == "old guard"
    assert list(target.parent.iterdir()) == [target]


def test_nested_guard_exports_keep_the_original_shell_startup_files(tmp_path, monkeypatch):
    guard = tmp_path / "guard"
    monkeypatch.setattr(d, "_ensure_gh_guard", lambda: guard)
    monkeypatch.setenv("ZDOTDIR", str(guard / "zdotdir"))
    monkeypatch.setenv("BASH_ENV", str(guard / "bashenv.sh"))
    monkeypatch.setenv("ORCH_GH_GUARD_REAL_ZDOTDIR", str(tmp_path / "original zsh"))
    monkeypatch.setenv("ORCH_GH_GUARD_PREV_BASH_ENV", str(tmp_path / "original bash"))
    exports = d._gh_guard_exports()
    assert (
        f"export ORCH_GH_GUARD_REAL_ZDOTDIR={shlex.quote(str(tmp_path / 'original zsh'))}"
        in exports
    )
    assert (
        f"export ORCH_GH_GUARD_PREV_BASH_ENV={shlex.quote(str(tmp_path / 'original bash'))}"
        in exports
    )


def test_muse_observation_window_is_relative_to_actual_start_not_a_calendar_reset():
    assert d._muse_observation_window(20001) == (2001, 20001)


def test_round_subject_wins_and_an_unreadable_registry_preserves_explicit_subject(
    monkeypatch, capsys
):
    monkeypatch.setattr(research_subjects, "round_identity", lambda _: {"subject_id": "o/r#7"})
    assert d._offload_subject("o/r#8", "round") == ("o/r#7", "research_round")
    assert "ignored" in capsys.readouterr().err

    def unavailable(_):
        raise OSError("fixture registry unavailable")

    monkeypatch.setattr(research_subjects, "round_identity", unavailable)
    assert d._offload_subject("o/r#8", "round") == ("o/r#8", "caller")


def test_profile_selector_rejects_model_mismatch_and_reports_registry_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        d.execution_profiles,
        "profiles_for_agent",
        lambda *a, **k: [{"profile_id": "wrong", "requested_model": "wrong"}],
    )
    monkeypatch.setattr(d.adapters, "effective_tier", lambda *a: "mid")
    monkeypatch.setattr(
        d.adapters, "model_health", lambda *a: {"resolvable": True, "model": "wanted"}
    )
    assert d._select_offload_profile("gemini", "mid") is None

    def failed(*a, **k):
        raise OSError("fixture profile failure")

    monkeypatch.setattr(d.execution_profiles, "profiles_for_agent", failed)
    assert d._select_offload_profile("cursor", "mid") is None
    assert "fixture profile failure" in capsys.readouterr().err


def test_run_records_skipped_plans_and_never_spawns_in_dry_run(monkeypatch):
    plans = iter([None, {"error": "refused"}, {"target": "o/r#3"}])
    monkeypatch.setattr(d, "plan_dispatch", lambda *a, **k: next(plans))
    monkeypatch.setattr(d, "_spawn", lambda *a: pytest.fail("dry run spawned"))
    monkeypatch.setattr(d, "write_heartbeat", lambda: pytest.fail("dry run wrote heartbeat"))
    assignments = [{"target": f"o/r#{i}"} for i in (1, 2, 3)]
    result = d.run({"assignments": assignments}, dry_run=True)
    assert result["launched"] == [{"target": "o/r#3", "pid": None}]
    assert [x["reason"] for x in result["skipped"]] == ["unknown agent / unbuildable", "refused"]
    assert result["count"] == 1


@pytest.fixture
def fake_offload(tmp_path, monkeypatch):
    """A transport double captures effects; it never runs the returned shell command."""
    effects = []
    monkeypatch.setattr(d.adapters, "agent_workspace", lambda *a, **k: (tmp_path, None))
    monkeypatch.setattr(d, "_offload_prompt", lambda prompt, *a: prompt)
    monkeypatch.setattr(d, "_select_offload_profile", lambda *a: None)
    monkeypatch.setattr(d.adapters, "can_report_cli_identity", lambda *a: (False, "fixture"))
    monkeypatch.setattr(d.adapters, "build_command", lambda *a, **k: ["fixture-agent"])
    monkeypatch.setattr(d, "_agent_runtime_prelude", lambda *a: "")
    monkeypatch.setattr(d, "_auth_prelude", lambda *a: "")
    monkeypatch.setattr(d.adapters, "model_identity", lambda *a: "fixture-model")
    monkeypatch.setattr(d.adapters, "record_ledger", lambda *a, **k: effects.append(("ledger", k)))
    monkeypatch.setattr(d.feedback, "record_run", lambda *a, **k: effects.append(("run", k)))
    monkeypatch.setattr(d.feedback, "record_cost", lambda *a, **k: effects.append(("cost", k)))
    monkeypatch.setattr(d, "_capability_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(d, "_exercised_capability_ids", lambda *a: [])
    import rate_incidents

    monkeypatch.setattr(rate_incidents, "get_structured_evidence", lambda **k: None)
    monkeypatch.setattr(rate_incidents, "provider_reset_at", lambda *a: None)
    monkeypatch.setattr(d.time, "sleep", lambda *a: pytest.fail("unexpected retry sleep"))
    return effects


def test_offload_process_start_failure_keeps_failure_identity_and_completes_once(
    fake_offload, monkeypatch
):
    calls = []

    def refused(*a, **k):
        calls.append((a, k))
        raise OSError("fixture executable missing")

    monkeypatch.setattr(d.subprocess, "run", refused)
    result = d.offload("cursor", "inspect", timeout=1, max_network_retries=0)
    assert result["exit"] == 126
    assert "fixture executable missing" in result["error"]
    assert result["attempts"] == 1
    assert len(calls) == 1
    assert [k["event"] for kind, k in fake_offload if kind == "ledger"] == ["start", "complete"]
    assert Path(result["log"]).is_file()


def test_offload_start_callback_failure_prevents_transport_and_completion(fake_offload):
    def refused(receipt):
        assert receipt["run_id"].startswith("offload:cursor:")
        raise RuntimeError("fixture admission callback refused")

    with pytest.raises(RuntimeError, match="fixture admission callback refused"):
        d.offload("cursor", "inspect", attempt_started_callback=refused)
    assert [k["event"] for kind, k in fake_offload if kind == "ledger"] == ["start"]
    assert not any(kind == "cost" for kind, _ in fake_offload)


def test_offload_receipt_callback_failure_precedes_completion_and_cost(fake_offload, monkeypatch):
    calls = []

    def completed(*a, **k):
        calls.append(a)
        return SimpleNamespace(returncode=0, stdout="actual fixture result", stderr="")

    def refused(receipt):
        assert receipt["output"] == "actual fixture result"
        raise RuntimeError("fixture receipt persistence failed")

    monkeypatch.setattr(d.subprocess, "run", completed)
    with pytest.raises(RuntimeError, match="fixture receipt persistence failed"):
        d.offload("cursor", "inspect", attempt_receipt_callback=refused)
    assert len(calls) == 1
    assert [k["event"] for kind, k in fake_offload if kind == "ledger"] == ["start"]
    assert not any(kind == "cost" for kind, _ in fake_offload)


def test_offload_refuses_unenforceable_boundary_before_ledger_or_transport(
    fake_offload, monkeypatch
):
    monkeypatch.setattr(
        d.adapters, "write_boundary_roots", lambda *a, **k: (["protected"], ["allowed"])
    )
    monkeypatch.setattr(
        d.adapters, "write_boundary_unenforceable", lambda *a: "fixture unsupported boundary"
    )
    result = d.offload("cursor", "inspect", write_boundary={"protect": ["protected"]})
    assert result["exit"] == 2
    assert result["run_id"] is None
    assert result["error"] == "fixture unsupported boundary"
    assert fake_offload == []


def test_streamed_start_refusal_removes_private_prompt_and_scrubs_provider_log(
    fake_offload, tmp_path, monkeypatch
):
    profile = {"profile_id": "fixture", "provider": "fixture", "requested_model": "fixture"}
    monkeypatch.setattr(d, "_select_offload_profile", lambda *a: profile)
    monkeypatch.setattr(d.execution_profiles, "get_profile", lambda *a: profile)
    monkeypatch.setattr(d, "GEMINI_STREAM_PROMPT_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        d.adapters, "build_command", lambda *a, **k: ["fixture", "--log-file", "ignored"]
    )
    scrubbed = []
    monkeypatch.setattr(
        d.credential_redaction, "scrub_file", lambda path: scrubbed.append(path) or {"kinds": {}}
    )

    def refuse(receipt):
        prompts = list(d.DISPATCH_LOG_DIR.glob("offload-prompt.*.ndjson"))
        assert len(prompts) == 1
        assert prompts[0].stat().st_mode & 0o777 == 0o600
        raise RuntimeError("fixture start refused")

    with pytest.raises(RuntimeError, match="fixture start refused"):
        d.offload("gemini", "large fixture prompt", attempt_started_callback=refuse)
    assert not list(d.DISPATCH_LOG_DIR.glob("offload-prompt.*.ndjson"))
    assert len(scrubbed) == 1
    assert not any(kind == "cost" for kind, _ in fake_offload)


def test_muse_interrupt_persists_receipt_removes_prompt_and_records_interrupted_completion(
    fake_offload, tmp_path, monkeypatch
):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("fixture request")
    monkeypatch.setattr(d.adapters, "muse_prompt_file", lambda *a: prompt)
    receipts = []

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(d.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        d.offload("muse", "inspect", attempt_receipt_callback=receipts.append)
    assert not prompt.exists()
    assert len(receipts) == 1
    assert receipts[0]["exit"] == 130
    assert receipts[0]["error"] == "interrupted by orchestrator"
    assert [k["event"] for kind, k in fake_offload if kind == "ledger"] == ["start", "complete"]
    assert (
        next(k for kind, k in fake_offload if kind == "ledger" and k["event"] == "complete")["exit"]
        == 130
    )


def test_unprofiled_recording_and_meter_failures_preserve_deliverable(fake_offload, monkeypatch):
    def unavailable(*a, **k):
        raise OSError("fixture accounting unavailable")

    cost_calls = []

    def unavailable_cost(*args, **kwargs):
        cost_calls.append((args, kwargs))
        raise OSError("fixture metering unavailable")

    monkeypatch.setattr(d.feedback, "record_run", unavailable)
    monkeypatch.setattr(d.feedback, "record_cost", unavailable_cost)
    monkeypatch.setattr(
        d.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="deliverable", stderr=""),
    )
    result = d.offload("cursor", "inspect", max_network_retries=0)
    assert result["exit"] == 0
    assert result["output"] == "deliverable"
    assert [k["event"] for kind, k in fake_offload if kind == "ledger"] == ["start", "complete"]
    assert len(cost_calls) == 1
    assert cost_calls[0][1]["source"] == "ledger"


def test_profile_recording_failure_removes_muse_prompt_and_never_starts_transport(
    fake_offload, tmp_path, monkeypatch
):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("fixture request")
    profile = {"profile_id": "fixture", "provider": "fixture", "requested_model": "fixture"}
    monkeypatch.setattr(d, "_select_offload_profile", lambda *a: profile)
    monkeypatch.setattr(d.adapters, "muse_prompt_file", lambda *a: prompt)

    def unavailable(*a, **k):
        raise OSError("fixture provenance unavailable")

    monkeypatch.setattr(d.feedback, "record_run", unavailable)
    with pytest.raises(OSError, match="fixture provenance unavailable"):
        d.offload("muse", "inspect")
    assert not prompt.exists()
    assert [k["event"] for kind, k in fake_offload if kind == "ledger"] == ["start"]


def test_offload_boundary_installation_failure_never_records_or_executes_an_invocation(
    fake_offload, monkeypatch
):
    monkeypatch.setattr(
        d.adapters, "write_boundary_roots", lambda *a, **k: (["protected"], ["allowed"])
    )
    monkeypatch.setattr(d.adapters, "write_boundary_unenforceable", lambda *a: None)

    def refused(*args):
        raise OSError("fixture sandbox installation failed")

    monkeypatch.setattr(d.adapters, "apply_write_boundary", refused)
    result = d.offload("cursor", "inspect", write_boundary={"protect": ["protected"]})
    assert result["exit"] == 2
    assert result["error"] == "fixture sandbox installation failed"
    assert result["run_id"] is None
    assert fake_offload == []


@pytest.mark.parametrize("failure", ["provision", "broad-workspace"])
def test_plan_refuses_failed_or_broad_workspace_before_building_agent_command(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(d, "build_prompt", lambda *a, **k: "fixture task")
    monkeypatch.setattr(d.adapters, "model_identity", lambda *a: "fixture-model")
    monkeypatch.setattr(
        d.adapters,
        "build_command",
        lambda *a, **k: pytest.fail("unsafe workspace reached command builder"),
    )

    def provision(*args):
        if failure == "provision":
            raise OSError("fixture provision failure")
        return tmp_path

    monkeypatch.setattr(d.provision, "provision", provision)
    monkeypatch.setattr(d.adapters, "broad_workspace_reason", lambda *a: "fixture broad workspace")
    result = d.plan_dispatch({"agent": "cursor", "target": "o/r#7", "task_type": "implement"})
    expected = (
        "provision failed: fixture provision failure"
        if failure == "provision"
        else "provisioned workspace refused: fixture broad workspace"
    )
    assert result == {"error": expected, "target": "o/r#7", "agent": "cursor"}


def test_live_run_writes_one_heartbeat_before_spawning_and_retains_the_process_identity(
    monkeypatch,
):
    events = []
    monkeypatch.setattr(d, "write_heartbeat", lambda: events.append("heartbeat"))
    monkeypatch.setattr(d, "plan_dispatch", lambda *a, **k: {"target": "o/r#7"})

    def spawn(plan):
        events.append("spawn")
        return 12345

    monkeypatch.setattr(d, "_spawn", spawn)
    result = d.run({"assignments": [{"target": "o/r#7"}]})
    assert events == ["heartbeat", "spawn"]
    assert result["launched"] == [{"target": "o/r#7", "pid": 12345}]
    assert result["count"] == 1


def test_muse_accounting_failure_warns_without_discarding_native_deliverable(
    fake_offload, tmp_path, monkeypatch, capsys
):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("fixture request")
    monkeypatch.setattr(d.adapters, "muse_prompt_file", lambda *a: prompt)
    monkeypatch.setattr(d.adapters, "muse_terminal_text", lambda *a: "native deliverable")
    monkeypatch.setattr(d, "_record_muse_usage", lambda *a: None)
    monkeypatch.setattr(
        d.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="fixture native stream", stderr=""),
    )
    captured = []

    def unavailable(run_id, **kwargs):
        captured.append(kwargs)
        raise OSError("fixture subscription accounting failed")

    monkeypatch.setattr(d.feedback, "record_muse_subscription_cost", unavailable)
    result = d.offload("muse", "inspect", max_network_retries=0)
    assert result["exit"] == 0
    assert result["output"] == "native deliverable"
    assert not prompt.exists()
    assert len(captured) == 1
    assert captured[0]["raw_exit_codes"] == [0]
    assert captured[0]["window_end_ts"] - captured[0]["window_start_ts"] == 18000
    assert "fixture subscription accounting failed" in capsys.readouterr().err
