"""Retain a cleanup refusal without signaling an unverified process."""

import json
from types import SimpleNamespace

import pytest
from test_closer_native_production_composition import _fixture_worker_cleanup


def test_identity_refusal_retains_owned_boundary_and_prior_exception(tmp_path, monkeypatch):
    import test_closer_native_production_composition as composition

    obj = SimpleNamespace(
        path=tmp_path / "state.json",
        read=lambda: {"workers": [{"worker_id": "worker-1", "pid": 7654321}]},
    )
    monkeypatch.setattr(
        composition.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="different process\n"),
    )
    monkeypatch.setattr(
        composition.os, "killpg", lambda *_a: pytest.fail("unverified process signaled")
    )
    receipt = tmp_path / "refusal.json"
    try:
        raise RuntimeError("original test-body failure")
    except RuntimeError:
        with pytest.raises(AssertionError, match="refusing cleanup") as caught:
            _fixture_worker_cleanup(obj, {"module_path": "/owned/module.py"}, tmp_path, receipt)
    result = json.loads(receipt.read_text())
    assert result["prior_exception_type"] == "RuntimeError"
    assert result["observed_lstart_command"] == "different process\n"
    assert result["signal_attempted"] is False
    assert result["result_exists"] is False
    assert result["expected"][0] == str(obj.path)
    assert '"prior_exception_type": "RuntimeError"' in str(caught.value)


def test_cleanup_requests_untruncated_owned_argv_without_relaxing_identity(tmp_path, monkeypatch):
    import signal

    import test_closer_native_production_composition as composition

    obj = SimpleNamespace(
        path=tmp_path / "long-owned-state.json",
        read=lambda: {"workers": [{"worker_id": "worker-2", "pid": 7654322}]},
    )
    module_path = "/owned/" + "long-component/" * 8 + "closer_campaign.py"
    result = composition.campaign.result_path(obj.path, "worker-2")
    full = f"Mon Oct 26 16:13:12 2026 /opt/python3.14 {module_path} --state {obj.path} --result {result}\n"
    commands = []
    signals = []

    def observe(command, **kwargs):
        commands.append(command)
        if "stat=" in command:
            return SimpleNamespace(returncode=1, stdout="")
        # This is the exact GNU ps width failure retained from the hosted job.
        return SimpleNamespace(
            returncode=0,
            stdout=full if "-ww" in command else "Mon Oct 26 16:13:12 2026 /opt/python3.14 /\n",
        )

    monkeypatch.setattr(composition.subprocess, "run", observe)
    monkeypatch.setattr(composition.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(composition.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    receipt = tmp_path / "wide-cleanup.json"
    _fixture_worker_cleanup(obj, {"module_path": module_path}, tmp_path, receipt)
    assert signals == [(7654322, signal.SIGTERM)]
    assert len([cmd for cmd in commands if "command=" in cmd]) == 2
    assert all("-ww" in cmd for cmd in commands if "command=" in cmd)
    assert json.loads(receipt.read_text())["terminated_test_workers"] == [7654322]
