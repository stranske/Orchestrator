"""Data-only uses a qualified no-tools CLI, never just an assessment prompt."""

import hashlib
import inspect
import json
from pathlib import Path

import pytest
from test_dispatcher_controlled_data_only import run as controlled_run
from test_dispatcher_controlled_data_only import world as world

import adapters
import dispatcher
import execution_profiles


@pytest.fixture
def controlled_box(world):
    return world


@pytest.fixture
def box(tmp_path, monkeypatch):
    cli = tmp_path / "claude"
    capture = tmp_path / "capture.json"
    cli.write_text(
        "#!/usr/bin/env python3\nimport sys,json,os\nfrom pathlib import Path\n"
        f'Path({str(capture)!r}).write_text(json.dumps({{"argv":sys.argv,"stdin":sys.stdin.read()}}))\n'
        'print("Assessment delivered; no acceptance authority.")\n'
    )
    cli.chmod(0o700)
    monkeypatch.setattr(
        adapters, "DATA_ONLY_CLAUDE_SHA256", hashlib.sha256(cli.read_bytes()).hexdigest()
    )
    real_which = adapters.shutil.which
    monkeypatch.setattr(
        adapters.shutil, "which", lambda n: str(cli) if n == "claude" else real_which(n)
    )
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher, "_path_prefix", lambda: ":")
    monkeypatch.setattr(dispatcher, "_net_hygiene_prelude", lambda: "")
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda p, *a, **k: p)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *a: None)
    monkeypatch.setattr(adapters, "can_report_cli_identity", lambda *a: (False, "fixture"))
    monkeypatch.setattr(adapters, "record_ledger", lambda *a, **k: None)
    monkeypatch.setattr(adapters, "model_identity", lambda *a: "fixture")
    for name in ("record_run", "record_cost", "record_outcome"):
        monkeypatch.setattr(dispatcher.feedback, name, lambda *a, **k: None)
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "4")
    return tmp_path, cli, capture


def test_actual_cli_stdin_large_and_controls(controlled_box):
    world = controlled_box
    prompt = "PRIVATE_INPUT_Only!\n" + ("historical material;" * 2500) + "  \n"
    result = controlled_run(world, prompt=prompt)
    assert result["exit"] == 0, result
    got = json.loads(world.capture.read_text())
    assert got["stdin"] == prompt and prompt not in " ".join(got["argv"])
    for flag in (
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ):
        assert flag in got["argv"]
    assert got["argv"][got["argv"].index("--tools") + 1] == ""
    assert json.loads(got["argv"][got["argv"].index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert json.loads(got["argv"][got["argv"].index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--dangerously-skip-permissions" not in got["argv"]
    assert result["attempts"] == 1
    assert (
        result["request_metadata"]["content_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    )
    assert result["request_metadata"]["transport"] == "claude-native-json-stdin/v1"
    assert not list((world.root / "logs").glob("offload-prompt.*"))
    assert "PRIVATE_INPUT_Only!" not in Path(result["log"]).read_text()
    assert list((world.root / "logs").glob("*.provider-result-*.json"))


@pytest.mark.parametrize("agent", ["codex", "gemini", "muse", "cursor", "vibe", "aider"])
def test_unsupported_before_any_provision_or_log(controlled_box, agent):
    world = controlled_box
    out = controlled_run(world, agent=agent)
    assert out["exit"] == 2 and out["run_id"] is None
    assert out["unsupported_assessment_tool_boundary"]
    assert world.events["auth"] == 0 and not world.events["ledger"]
    assert not (world.root / "logs").exists() and not world.capture.exists()


@pytest.mark.parametrize("mode,retries", [("mid", None), ("assess", 1)])
def test_wrong_mode_or_retry_rejected_early(controlled_box, mode, retries):
    world = controlled_box
    out = controlled_run(world, mode=mode, max_network_retries=retries)
    assert out["exit"] == 2 and out["run_id"] is None
    assert not (world.root / "logs").exists() and world.events["auth"] == 0


def test_binary_drift_held_early(controlled_box):
    world = controlled_box
    world.cli.write_text(world.cli.read_text() + "\n# changed\n")
    out = controlled_run(world)
    assert out["exit"] == 2 and "not qualified" in out["error"]
    assert not (world.root / "logs").exists() and not world.capture.exists()


def test_profile_model_and_effort_preserved(box):
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    argv = adapters.build_command(
        "claude",
        "",
        mode="assess",
        profile=profile,
        transport="offload",
        prompt_via_stdin=True,
        data_only=True,
    )
    assert argv[argv.index("--model") + 1] == profile["requested_model"]
    assert argv[argv.index("--effort") + 1] == profile["reasoning_effort"]
    ordinary = adapters.build_command("claude", "ordinary", mode="assess", profile=profile)
    assert ordinary[:4] == ["claude", "-p", "ordinary", "--dangerously-skip-permissions"]
    assert "--safe-mode" not in ordinary


def test_timeout_retains_unknown_once_and_cleans_prompt(controlled_box):
    world = controlled_box
    world.configure("timeout")
    out = controlled_run(world, timeout=0.2)
    assert len(world.events["started"]) == 1
    assert out["outcome_uncertain"] and out["retry_safe"] is False
    assert out["run_id"] and out["exit"] == 124 and out["attempts"] == 1
    assert not list((world.root / "logs").glob("offload-prompt.*"))
    assert list((world.root / "logs").glob("*.provider-result-*.json"))


def test_start_callback_failure_cleans_private_prompt(controlled_box):
    world = controlled_box

    def fail(_):
        raise RuntimeError("prelaunch callback refused")

    with pytest.raises(RuntimeError, match="prelaunch callback refused"):
        controlled_run(world, attempt_started_callback=fail)
    assert not world.capture.exists()
    assert not list((world.root / "logs").glob("offload-prompt.*"))


def test_interruption_retains_once_and_cleans(controlled_box, monkeypatch):
    world = controlled_box
    calls = []

    def interrupt(command, **kwargs):
        calls.append(command)
        raise KeyboardInterrupt()

    monkeypatch.setattr(dispatcher.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        controlled_run(world)
    assert len(calls) == 1
    assert not list((world.root / "logs").glob("offload-prompt.*"))
    receipt = json.loads(next((world.root / "logs").glob("*.provider-result-*.json")).read_text())
    assert receipt["result"]["outcome_uncertain"] and receipt["result"]["retry_safe"] is False


def test_missing_cli_is_early_unsupported(box, monkeypatch):
    root, _, _ = box
    monkeypatch.setattr(adapters.shutil, "which", lambda *_: None)
    assert "absent" in adapters.data_only_problem("claude")
    out = dispatcher.offload("claude", "secret", mode="assess", data_only=True)
    assert out["exit"] == 2 and not (root / "logs").exists()


def test_observed_process_ownership_callbacks_preserved(controlled_box):
    world = controlled_box
    result = controlled_run(world, prompt="self-contained input")
    assert result["exit"] == 0, result
    assert world.capture.exists()
    assert len(world.events["started"]) == len(world.events["finished"]) == 1
    observed = world.events["started"][0]
    assert observed["run_id"] == result["run_id"]
    assert observed["os_started"] and observed["pid"] > 0
    assert "--safe-mode" in observed["provider_argv"]
    assert "self-contained input" not in " ".join(observed["provider_argv"])
    assert not list((world.root / "logs").glob("offload-prompt.*"))


@pytest.mark.parametrize("failure", ["changed", "missing", "oserror"])
def test_final_binary_check_prevents_spawn_and_closes_started_run(
    controlled_box, monkeypatch, failure
):
    world = controlled_box
    claim = dispatcher._claim_offload_log
    qualified_binary = adapters._data_only_claude_binary
    claimed = False
    receipts, started = [], []

    def changed(agent):
        nonlocal claimed
        result = claim(agent)
        claimed = True
        if failure == "changed":
            world.cli.write_text(world.cli.read_text() + "\n# raced update\n")
        elif failure == "missing":
            world.cli.unlink()
        return result

    def binary():
        if claimed and failure == "oserror":
            raise PermissionError("synthetic final binary read refused")
        return qualified_binary()

    monkeypatch.setattr(dispatcher, "_claim_offload_log", changed)
    monkeypatch.setattr(adapters, "_data_only_claude_binary", binary)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *a, **k: started.append(a[0]))
    monkeypatch.setattr(
        dispatcher, "run_observed_offload", lambda *a, **k: pytest.fail("refused binary spawned")
    )
    out = controlled_run(
        world,
        prompt="self-contained private input",
        attempt_receipt_callback=lambda value: receipts.append(dict(value)),
    )
    assert out["exit"] == 2 and out["process_started"] is False
    assert out["unsupported_assessment_tool_boundary"] is True and out["outcome_uncertain"] is False
    assert out["attempts"] == 1 and not out.get("retried")
    events = world.events["ledger"]
    assert [value["event"] for value in events] == ["start", "complete"]
    assert all(value["run_id"] == out["run_id"] for value in events)
    assert events[1]["exit"] == 2 and started == [out["run_id"]]
    assert len(receipts) == 1 and receipts[0]["run_id"] == out["run_id"]
    assert not world.capture.exists() and not world.events["started"]
    assert not list((world.root / "logs").glob("offload-prompt.*"))
    retained = json.loads(next((world.root / "logs").glob("*.provider-result-*.json")).read_text())
    assert retained["run_id"] == out["run_id"] and retained["result"]["process_started"] is False
    assert retained["result"]["exit"] == 2
    assert "self-contained private input" not in Path(out["log"]).read_text()


@pytest.mark.parametrize("error", [ValueError("callback refused"), OSError("callback refused")])
def test_non_binary_callback_failure_is_not_converted_to_refusal(controlled_box, error):
    world = controlled_box

    def callback(_):
        raise error

    with pytest.raises(type(error), match="callback refused"):
        controlled_run(world, attempt_started_callback=callback)
    assert [row["event"] for row in world.events["ledger"]] == ["start"]
    assert not world.capture.exists()
    assert not list((world.root / "logs").glob("offload-prompt.*"))
    assert not list((world.root / "logs").glob("*.provider-result-*.json"))


def test_input_unlink_failure_scrubs_and_holds_actual_result(controlled_box, monkeypatch):
    world = controlled_box
    real_unlink = Path.unlink

    def refuse_input_unlink(path, *args, **kwargs):
        if path.name.startswith("offload-prompt.claude."):
            raise PermissionError("synthetic denied unlink")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_input_unlink)
    out = controlled_run(world, prompt="private unlogged input")
    assert out["raw_exit"] == 0 and out["exit"] == 70
    assert out["outcome_uncertain"] and out["retry_safe"] is False
    assert json.loads(out["output"])["classification"] == "relevant"
    assert out["cleanup_errors"][0]["input_scrubbed"]
    assert next((world.root / "logs").glob("offload-prompt.*")).read_bytes() == b""
    receipt = json.loads(next((world.root / "logs").glob("*.provider-result-*.json")).read_text())
    assert receipt["result"]["output"] == out["output"]


def test_actual_profiled_offload_stdin_keeps_model_effort(controlled_box):
    world = controlled_box
    result = controlled_run(world, prompt="full self-contained input")
    assert result["exit"] == 0, result
    got = json.loads(world.capture.read_text())
    assert got["stdin"] == "full self-contained input"
    assert got["argv"][got["argv"].index("--model") + 1] == "claude-sonnet-5-5"
    assert got["argv"][got["argv"].index("--effort") + 1] == "low"
    assert result["request_metadata"]["cli_sha256"] == adapters.DATA_ONLY_CLAUDE_SHA256


def test_opt_in_does_not_shift_existing_positional_parameters():
    assert list(inspect.signature(dispatcher.offload).parameters) == [
        "agent",
        "prompt",
        "cwd",
        "mode",
        "timeout",
        "isolate",
        "profile_id",
        "research_round",
        "task_type",
        "write_boundary",
        "subject_id",
        "attempt_receipt_callback",
        "max_network_retries",
        "attempt_started_callback",
        "process_started_callback",
        "process_starting_callback",
        "request_deadline_mono",
        "finite_frontier_id",
        "finite_frontier_target",
        "process_finished_callback",
        "data_only",
        "data_only_limits",
        "trusted_auth_supplier",
        "data_only_operation_id",
    ]


@pytest.mark.parametrize(
    "error",
    [PermissionError("synthetic denied binary"), FileNotFoundError("synthetic absent binary")],
)
def test_initial_binary_oserror_is_no_start_without_ledger(controlled_box, monkeypatch, error):
    world = controlled_box

    def unavailable():
        raise error

    monkeypatch.setattr(adapters, "_data_only_claude_binary", unavailable)
    result = controlled_run(world, prompt="private input")
    assert result["exit"] == 2 and result["process_started"] is False
    assert result["run_id"] is None and result["outcome_uncertain"] is False
    assert not world.events["ledger"] and not world.capture.exists()
    assert not (world.root / "logs").exists()


def test_no_spawn_refusal_retention_failure_stays_uncertain_without_retry(
    controlled_box, monkeypatch
):
    world = controlled_box
    original_claim = dispatcher._claim_offload_log

    def changed(agent):
        result = original_claim(agent)
        world.cli.write_text(world.cli.read_text() + "\n# changed before spawn\n")
        return result

    def unavailable_retention(*_a, **_kw):
        raise OSError("synthetic result store unavailable")

    monkeypatch.setattr(dispatcher, "_claim_offload_log", changed)
    monkeypatch.setattr(dispatcher, "_retain_provider_result", unavailable_retention)
    monkeypatch.setattr(
        dispatcher, "run_observed_offload", lambda *a, **k: pytest.fail("refused binary spawned")
    )
    result = controlled_run(world, prompt="private input")
    assert result["exit"] == 70 and result["raw_exit"] == 2
    assert result["process_started"] is False and result["outcome_uncertain"] is True
    assert result["retry_safe"] is False and result["run_id"]
    events = world.events["ledger"]
    assert [event["event"] for event in events] == ["start"]
    assert events[0]["run_id"] == result["run_id"]
    assert not world.capture.exists() and not list((world.root / "logs").glob("offload-prompt.*"))
