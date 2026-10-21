"""Data-only uses a qualified no-tools CLI, never just an assessment prompt."""

import hashlib
import inspect
import json
import subprocess
from pathlib import Path

import pytest

import adapters
import dispatcher
import execution_profiles


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


def test_actual_cli_stdin_large_and_controls(box):
    root, cli, capture = box
    prompt = "PRIVATE_INPUT_Only!\n" + ("full historical material;" * 20000) + "  \n"
    result = dispatcher.offload("claude", prompt, cwd=str(root), mode="assess", data_only=True)
    assert result["exit"] == 0, result
    got = json.loads(capture.read_text())
    assert got["stdin"] == prompt
    assert prompt not in " ".join(got["argv"])
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
    assert result["request_metadata"]["transport"] == "claude-data-only-text-stdin"
    assert not list((root / "logs").glob("offload-prompt.*"))
    assert "PRIVATE_INPUT_Only!" not in Path(result["log"]).read_text()
    assert list((root / "logs").glob("*.provider-result-*.json")), list((root / "logs").iterdir())


@pytest.mark.parametrize("agent", ["codex", "gemini", "muse", "cursor", "vibe", "aider"])
def test_unsupported_before_any_provision_or_log(box, monkeypatch, agent):
    root, _, _ = box

    def forbidden(*a, **k):
        raise AssertionError("unsupported invocation touched work")

    monkeypatch.setattr(adapters, "agent_workspace", forbidden)
    monkeypatch.setattr(dispatcher, "_claim_offload_log", forbidden)
    monkeypatch.setattr(dispatcher.subprocess, "run", forbidden)
    out = dispatcher.offload(agent, "input", mode="assess", data_only=True)
    assert out["exit"] == 2 and out["run_id"] is None
    assert out["unsupported_assessment_tool_boundary"]
    assert not (root / "logs").exists()


@pytest.mark.parametrize("mode,retries", [("mid", None), ("assess", 1)])
def test_wrong_mode_or_retry_rejected_early(box, mode, retries):
    root, _, _ = box
    out = dispatcher.offload(
        "claude", "input", mode=mode, data_only=True, max_network_retries=retries
    )
    assert out["exit"] == 2 and out["run_id"] is None
    assert not (root / "logs").exists()


def test_binary_drift_held_early(box):
    root, cli, _ = box
    cli.write_text(cli.read_text() + "\n# changed\n")
    out = dispatcher.offload("claude", "input", mode="assess", data_only=True)
    assert out["exit"] == 2 and "not qualified" in out["error"]
    assert not (root / "logs").exists()


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


def test_timeout_retains_unknown_once_and_cleans_prompt(box, monkeypatch):
    root, _, _ = box
    calls = []

    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(dispatcher.subprocess, "run", timeout)
    out = dispatcher.offload("claude", "secret", cwd=str(root), mode="assess", data_only=True)
    assert len(calls) == 1
    assert out["outcome_uncertain"] and out["retry_safe"] is False
    assert out["run_id"] and out["exit"] == 124
    assert not list((root / "logs").glob("offload-prompt.*"))
    receipts = list((root / "logs").glob("*.provider-result-*.json"))
    assert receipts


def test_start_callback_failure_cleans_private_prompt(box):
    root, _, capture = box

    def fail(_):
        raise RuntimeError("prelaunch callback refused")

    with pytest.raises(RuntimeError, match="prelaunch callback refused"):
        dispatcher.offload(
            "claude",
            "secret",
            cwd=str(root),
            mode="assess",
            data_only=True,
            attempt_started_callback=fail,
        )
    assert not capture.exists()
    assert not list((root / "logs").glob("offload-prompt.*"))


def test_interruption_retains_once_and_cleans(box, monkeypatch):
    root, _, _ = box
    calls = []

    def interrupt(command, **kwargs):
        calls.append(command)
        raise KeyboardInterrupt()

    monkeypatch.setattr(dispatcher.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.offload("claude", "secret", cwd=str(root), mode="assess", data_only=True)
    assert len(calls) == 1
    assert not list((root / "logs").glob("offload-prompt.*"))
    receipt = json.loads(next((root / "logs").glob("*.provider-result-*.json")).read_text())
    assert receipt["result"]["outcome_uncertain"]
    assert receipt["result"]["retry_safe"] is False


def test_missing_cli_is_early_unsupported(box, monkeypatch):
    root, _, _ = box
    monkeypatch.setattr(adapters.shutil, "which", lambda *_: None)
    assert "absent" in adapters.data_only_problem("claude")
    out = dispatcher.offload("claude", "secret", mode="assess", data_only=True)
    assert out["exit"] == 2 and not (root / "logs").exists()


def test_observed_process_ownership_callbacks_preserved(box, monkeypatch):
    root, _, capture = box
    observations = []
    real_run = dispatcher.subprocess.run

    def birth_transport(command, **kwargs):
        # External OS inspection is simulated; the owned CLI itself is a real Popen.
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "synthetic-test-birth", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(dispatcher.subprocess, "run", birth_transport)
    result = dispatcher.offload(
        "claude",
        "self-contained input",
        cwd=str(root),
        mode="assess",
        data_only=True,
        process_starting_callback=lambda row: observations.append(("starting", dict(row))),
        process_started_callback=lambda row: observations.append(("started", dict(row))),
        process_finished_callback=lambda row: observations.append(("finished", dict(row))),
    )
    assert result["exit"] == 0, result
    assert capture.exists()
    assert [kind for kind, _ in observations] == ["starting", "started", "finished"]
    observed = observations[1][1]
    assert observed["run_id"] == result["run_id"]
    assert observed["os_started"] and observed["pid"] > 0
    assert "--safe-mode" in observed["provider_argv"]
    assert "self-contained input" not in " ".join(observed["provider_argv"])
    assert not list((root / "logs").glob("offload-prompt.*"))


def test_final_binary_check_prevents_spawn_after_log_claim(box, monkeypatch):
    root, cli, capture = box
    claim = dispatcher._claim_offload_log

    def changed(agent):
        result = claim(agent)
        cli.write_text(cli.read_text() + "\n# raced update\n")
        return result

    monkeypatch.setattr(dispatcher, "_claim_offload_log", changed)
    with pytest.raises(ValueError, match="not qualified"):
        dispatcher.offload(
            "claude", "self-contained input", cwd=str(root), mode="assess", data_only=True
        )
    assert not capture.exists()
    assert not list((root / "logs").glob("offload-prompt.*"))


def test_input_unlink_failure_scrubs_and_holds_actual_result(box, monkeypatch):
    root, _, _ = box
    real_unlink = Path.unlink

    def refuse_input_unlink(path, *args, **kwargs):
        if path.name.startswith("offload-prompt.claude."):
            raise PermissionError("synthetic denied unlink")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_input_unlink)
    out = dispatcher.offload(
        "claude", "private unlogged input", cwd=str(root), mode="assess", data_only=True
    )
    assert out["raw_exit"] == 0 and out["exit"] == 70
    assert out["outcome_uncertain"] and out["retry_safe"] is False
    assert out["output"] == "Assessment delivered; no acceptance authority."
    assert out["cleanup_errors"][0]["input_scrubbed"]
    assert next((root / "logs").glob("offload-prompt.*")).read_bytes() == b""
    receipt = json.loads(next((root / "logs").glob("*.provider-result-*.json")).read_text())
    assert receipt["result"]["output"] == out["output"]


def test_actual_profiled_offload_stdin_keeps_model_effort(box):
    root, _, capture = box
    result = dispatcher.offload(
        "claude",
        "full self-contained input",
        cwd=str(root),
        mode="assess",
        data_only=True,
        profile_id="claude-sonnet-5-5-low",
    )
    assert result["exit"] == 0, result
    got = json.loads(capture.read_text())
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
    ]
