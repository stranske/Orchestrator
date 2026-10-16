"""Large Gemini offloads must cross both exec boundaries without prompt argv expansion."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

import adapters
import dispatcher
import execution_profiles
import rate_incidents


def _isolate_offload(monkeypatch, tmp_path):
    # Model discovery is a separate subprocess boundary; a cold hosted cache must not
    # reach the transport failure stub before the prompt file has been created.
    monkeypatch.setattr(adapters, "_model_probe_enabled", lambda: False)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *_: 20)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *_: prompt)
    monkeypatch.setattr(
        dispatcher,
        "_select_offload_profile",
        lambda agent, *_args: (
            execution_profiles.get_profile("gemini-3.8-flash-high") if agent == "gemini" else None
        ),
    )
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dispatcher, "_net_hygiene_prelude", lambda: "")
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *_: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "resolve_model", lambda *_: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dispatcher.adapters, "can_report_cli_identity", lambda *_: (False, "test"))
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")


def test_large_gemini_prompt_uses_lossless_private_ndjson_stdin(tmp_path, monkeypatch):
    """A real shell/provider-stub boundary proves the production E2BIG shape stays bounded.

    The former path embedded this packet in ``bash -lc`` and then in agy's argv.  This stub fails
    if either boundary receives a large argv and returns the digest of the exact Unicode prompt it
    decoded from stdin.  It does not contact a provider.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "agy"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, sys\n"
        "assert max(map(len, sys.argv)) < 16384, 'large prompt leaked into provider argv'\n"
        "row = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
        "assert row['event'] == 'user' and row['message']['role'] == 'user'\n"
        "print(json.dumps({'event': 'result', 'result': {'response': 'transport-sha256=' + hashlib.sha256(row['message']['content'].encode('utf-8')).hexdigest()}}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    packet = ("native \u03b1 $() ` ; ' \\\" \\n\U0001f642\n" * 50_000) + "terminal \u2603"
    expected = hashlib.sha256(packet.encode("utf-8")).hexdigest()

    _isolate_offload(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatcher, "_path_prefix", lambda: f"export PATH={bin_dir}:$PATH")

    result = dispatcher.offload("gemini", packet, cwd=str(workspace), timeout=20)

    assert result["exit"] == 0, result
    assert f"transport-sha256={expected}" in result["output"], result
    assert not list((tmp_path / "logs").glob("offload-prompt.*.ndjson"))
    argv = adapters.build_command("gemini", "", cwd=workspace, prompt_via_stdin=True)
    assert "-p=" in argv and "stream-json" in argv


@pytest.mark.parametrize(
    ("failure", "expected_exit"),
    [
        (lambda argv: subprocess.TimeoutExpired(argv, 20), 124),
        (lambda _argv: OSError("simulated process-start failure"), 126),
    ],
    ids=["timeout", "process-start-failure"],
)
def test_large_gemini_prompt_file_is_private_and_removed_on_boundary_failure(
    tmp_path, monkeypatch, failure, expected_exit
):
    """The short-lived prompt file survives long enough for the subprocess, then is removed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolate_offload(monkeypatch, tmp_path)
    packet = "\u03b1$()\n" * 20_000
    seen = {}

    def fail_at_boundary(argv, **kwargs):
        prompt_files = list((tmp_path / "logs").glob("offload-prompt.*.ndjson"))
        assert len(prompt_files) == 1
        prompt_file = prompt_files[0]
        seen["mode"] = os.stat(prompt_file).st_mode & 0o777
        seen["payload"] = prompt_file.read_text(encoding="utf-8")
        seen["stdin"] = kwargs["stdin"]
        raise failure(argv)

    monkeypatch.setattr(dispatcher.subprocess, "run", fail_at_boundary)
    result = dispatcher.offload("gemini", packet, cwd=str(workspace), timeout=20)

    assert result["exit"] == expected_exit, result
    assert seen["mode"] == 0o600
    assert adapters.gemini_stream_prompt(packet) == seen["payload"]
    assert seen["stdin"] is subprocess.DEVNULL
    assert not list((tmp_path / "logs").glob("offload-prompt.*.ndjson"))


def test_compact_gemini_prompt_retains_the_established_argv_shape(tmp_path):
    compact = "Keep this small; $() and 'quotes' are ordinary prompt text."
    argv = adapters.build_command("gemini", compact, cwd=tmp_path, transport="offload")

    assert "-p=" not in argv and "--input-format" not in argv
    assert argv[argv.index("--print") + 1] == compact


@pytest.mark.parametrize("byte_length", [65_536, 65_537])
def test_dispatcher_selects_transport_at_utf8_byte_boundary(tmp_path, monkeypatch, byte_length):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolate_offload(monkeypatch, tmp_path)
    prompt = "α" * (byte_length // 2) + ("x" if byte_length % 2 else "")
    seen = {}

    def complete(argv, **kwargs):
        seen["stream"] = "--input-format" in argv[-1]
        files = list((tmp_path / "logs").glob("offload-prompt.*.ndjson"))
        if seen["stream"]:
            assert len(files) == 1
            assert json.loads(files[0].read_text())["message"]["content"] == prompt
            output = json.dumps({"event": "result", "result": {"response": "complete"}})
        else:
            assert not files
            output = "complete"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(dispatcher.subprocess, "run", complete)
    result = dispatcher.offload("gemini", prompt, cwd=str(workspace), timeout=20)
    assert result["exit"] == 0, result
    assert seen["stream"] is (byte_length > 65_536)
    assert result["output"] == "complete"
    assert not list((tmp_path / "logs").glob("offload-prompt.*.ndjson"))


@pytest.mark.parametrize(
    "stream",
    [
        {"event": "init"},
        {"event": "result", "result": {"response": None}},
        {"event": "result", "result": {"response": {"text": "unfinished"}}},
    ],
)
def test_stream_without_final_string_response_is_incomplete(tmp_path, monkeypatch, stream):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolate_offload(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(stream), ""),
    )
    result = dispatcher.offload("gemini", "α" * 40_000, cwd=str(workspace), timeout=20)
    assert result["exit"] == 70, result
    assert result["output"] == "", result
    assert "no final string response" in result["error"]
    assert not list((tmp_path / "logs").glob("offload-prompt.*.ndjson"))
