"""Optional trusted transport observation: local processes only, no real CLI provider."""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import time

import pytest
from test_offload_failure_evidence import isolated_offload

import dispatcher


@pytest.fixture(autouse=True)
def private_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_REDACT_SECRET_FILES", "")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))


def test_default_offload_keeps_original_subprocess_run_path(tmp_path, monkeypatch):
    isolated_offload(tmp_path, monkeypatch)
    calls = []

    def original(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "offline transport result", "")

    monkeypatch.setattr(dispatcher.subprocess, "run", original)
    monkeypatch.setattr(
        dispatcher,
        "run_observed_offload",
        lambda *a, **kw: pytest.fail("optional path used without observer"),
    )
    result = dispatcher.offload("cursor", "offline", cwd=str(tmp_path))
    assert result["exit"] == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == ["bash", "-lc"]
    assert calls[0][1]["capture_output"] and calls[0][1]["text"]


def test_real_owned_transport_callback_has_birth_argv_and_exact_request(tmp_path):
    command = [
        sys.executable,
        "-c",
        "import time; print('owned fixture',flush=True); time.sleep(.1)",
    ]
    observed = []
    result = dispatcher.run_observed_offload(
        command,
        callback=observed.append,
        observation={
            "run_id": "offline-owned-transport",
            "agent": "codex",
            "profile_id": "offline-profile",
            "provider_argv": command,
        },
        cwd=str(tmp_path),
        timeout=2,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0 and "owned fixture" in result.stdout
    assert len(observed) == 1 and observed[0]["pid"] > 1
    assert observed[0]["transport_argv"] == command
    assert observed[0]["provider_argv"] == command
    assert observed[0]["os_started"]
    assert observed[0]["source"] == "trusted-popen-transport/v1"


def test_observation_callback_failure_is_after_spawn_with_bounded_cleanup(tmp_path):
    def failed(event):
        assert event["pid"] > 1
        raise PermissionError("offline observer denied after spawn")

    started = time.monotonic()
    with pytest.raises(dispatcher.ObservedOffloadFailure) as caught:
        dispatcher.run_observed_offload(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            callback=failed,
            observation={"run_id": "offline-failed"},
            cwd=str(tmp_path),
            timeout=2,
            stdin=subprocess.DEVNULL,
        )
    assert caught.value.observation["pid"] > 1
    assert caught.value.returncode == -9
    assert time.monotonic() - started < 5


def test_observed_timeout_preserves_original_budget_and_partial_output(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        dispatcher.run_observed_offload(
            [
                sys.executable,
                "-c",
                "import sys,time; print('partial-out',flush=True); print('partial-err',file=sys.stderr,flush=True); time.sleep(30)",
            ],
            callback=lambda _event: None,
            observation={"run_id": "offline-timeout"},
            cwd=str(tmp_path),
            timeout=0.3,
            stdin=subprocess.DEVNULL,
        )
    assert caught.value.timeout == 0.3
    assert "partial-out" in caught.value.output
    assert "partial-err" in caught.value.stderr
    assert caught.value.raw_exit == -9


@pytest.mark.parametrize("fault", ["callback", "timeout"])
def test_offload_retains_postspawn_unknown_and_never_network_retries(tmp_path, monkeypatch, fault):
    isolated_offload(tmp_path, monkeypatch)
    observations, receipts, starts = [], [], []

    raw_prompt = "offline-prompt-must-not-enter-observer-receipt"
    observed = {
        "pid": 999,
        "run_id": "fixture-only",
        "provider_argv": ["offline-provider", raw_prompt],
        "transport_argv": ["bash", "-lc", raw_prompt],
    }

    def instrumented(command, **kwargs):
        observations.append(command)
        if fault == "callback":
            raise dispatcher.ObservedOffloadFailure(
                "offline callback failure",
                observed,
                -9,
                "retained-out",
                "retained-err",
            )
        exc = subprocess.TimeoutExpired(
            command, kwargs["timeout"], output="retained-out", stderr="retained-err"
        )
        exc.raw_exit = -9
        exc.observation = observed
        raise exc

    monkeypatch.setattr(dispatcher, "run_observed_offload", instrumented)
    result = dispatcher.offload(
        "cursor",
        "offline",
        cwd=str(tmp_path),
        max_network_retries=3,
        process_started_callback=lambda _: None,
        attempt_started_callback=starts.append,
        attempt_receipt_callback=receipts.append,
    )
    assert len(observations) == len(starts) == len(receipts) == 1
    assert result["outcome_uncertain"] and result["retry_safe"] is False
    assert result["process_started"] and result.get("disabled") is not True
    assert receipts[0]["run_id"] == result["run_id"]
    public = result["process_observation"]
    assert public["provider_argv_argc"] == 2 and public["provider_argv_sha256"]
    assert "provider_argv" not in public and "transport_argv" not in public
    assert raw_prompt not in json.dumps(result) and raw_prompt not in json.dumps(receipts)
    assert observed["provider_argv"][-1] == raw_prompt  # Trusted cleanup input retained in memory.
    if fault == "timeout":
        assert result["partial_stdout"] == "retained-out"
        assert result["partial_stderr"] == "retained-err"
    else:
        assert result["output"] == "retained-out"


def test_observed_cleanup_denial_retains_exact_error_and_pending_transport(tmp_path, monkeypatch):
    import errno
    import os

    children = []
    original_popen = subprocess.Popen

    def owned_popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        if kwargs.get("start_new_session"):
            children.append(child)
        return child

    def denied(pid, sig):
        raise PermissionError(errno.EPERM, "simulated owned-group cleanup denial")

    monkeypatch.setattr(dispatcher.subprocess, "Popen", owned_popen)
    monkeypatch.setattr(dispatcher.os, "killpg", denied)
    started = time.monotonic()
    try:
        with pytest.raises(dispatcher.ObservedOffloadFailure) as caught:
            dispatcher.run_observed_offload(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                callback=lambda event: (_ for _ in ()).throw(ValueError("offline bad observation")),
                observation={"run_id": "offline-denied-cleanup"},
                cwd=str(tmp_path),
                timeout=1,
                stdin=subprocess.DEVNULL,
            )
        evidence = caught.value.observation
        assert evidence["cleanup_error"] == {
            "type": "PermissionError",
            "errno": errno.EPERM,
            "reason": "[Errno 1] simulated owned-group cleanup denial",
            "process_group": children[0].pid,
        }
        assert evidence["cleanup_terminal"] is False
        assert evidence["transport_returncode"] is None
        assert time.monotonic() - started < 4
    finally:
        # This is a simulated denial, not a live permission failure. Test-owned
        # cleanup uses the actual Popen handle after the source preserved UNKNOWN.
        for child in children:
            os.kill(child.pid, 9)
            child.wait(timeout=2)


def test_slow_transport_birth_probe_uses_remaining_original_timeout(tmp_path, monkeypatch):
    clock = itertools.chain([10.0, 10.1], itertools.count(10.2, 0.05))
    calls = []
    signals = []

    class Child:
        pid = 12345
        stdin = stdout = stderr = None
        returncode = -9

        def communicate(self, *, timeout):
            assert 0 < timeout <= 2
            return "", ""

        def poll(self):
            return self.returncode

    def stalled(command, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    def signal_group(pid, sig):
        signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(dispatcher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(dispatcher.subprocess, "run", stalled)
    monkeypatch.setattr(dispatcher.os, "killpg", signal_group)
    with pytest.raises(dispatcher.ObservedOffloadFailure) as caught:
        dispatcher.run_observed_offload(
            ["offline-no-spawn"],
            callback=lambda _: pytest.fail("birth was not observed"),
            observation={"run_id": "offline-slow-birth"},
            cwd=str(tmp_path),
            timeout=0.25,
            stdin=subprocess.DEVNULL,
        )
    assert calls == [pytest.approx(0.15)]
    assert "TimeoutExpired" in str(caught.value)
    assert caught.value.returncode == -9
    assert signals[0] == (Child.pid, 9)
    assert all(pid == Child.pid and sig in {0, 9} for pid, sig in signals)
    assert sum(sig != 0 for _, sig in signals) == 1


def test_expired_before_birth_does_not_persist_prompt_in_error(tmp_path, monkeypatch):
    clock = itertools.chain([10.0, 11.0], itertools.count(11.1, 0.05))
    prompt = "offline-prompt-must-not-be-persisted-in-timeout-error"
    signals = []

    class Child:
        pid = 12345
        stdin = stdout = stderr = None
        returncode = -9

        def communicate(self, *, timeout):
            return "", ""

        def poll(self):
            return self.returncode

    def signal_group(pid, sig):
        signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(dispatcher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(dispatcher.subprocess, "run", lambda *a, **kw: pytest.fail("expired ps"))
    monkeypatch.setattr(dispatcher.os, "killpg", signal_group)
    with pytest.raises(dispatcher.ObservedOffloadFailure) as caught:
        dispatcher.run_observed_offload(
            ["bash", "-lc", prompt],
            callback=lambda _: pytest.fail("expired observer"),
            observation={"provider_argv": ["offline-provider", prompt]},
            cwd=str(tmp_path),
            timeout=0.25,
            stdin=subprocess.DEVNULL,
        )
    assert "TimeoutExpired" in str(caught.value)
    assert prompt not in str(caught.value)
    public = dispatcher._public_process_observation(caught.value.observation)
    assert prompt not in json.dumps(public)
    assert signals[0] == (Child.pid, 9)
    assert all(pid == Child.pid and sig in {0, 9} for pid, sig in signals)
    assert sum(sig != 0 for _, sig in signals) == 1
