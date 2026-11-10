"""Launch gate: a local-only seat whose launch binary is missing is SHED, never picked (aider 47/47)."""

import os
import stat

import adapters
import capacity


def _exe(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_local_launch_reports_missing_absolute_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "1")
    monkeypatch.setattr(adapters, "AIDER_BIN", tmp_path / "aider-venv" / "bin" / "aider")
    result = adapters.local_launch("aider")
    assert result["checked"] is True
    assert result["launchable"] is False
    assert "launch binary missing" in result["reason"]


def test_local_launch_finds_binary_on_dispatch_path_and_rejects_dangling_symlink(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "1")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setattr(adapters, "LOCAL_AGENT_PATH_DIRS", (str(bindir),))
    monkeypatch.setenv("PATH", "/nonexistent")
    _exe(bindir / "vibe")
    assert adapters.local_launch("vibe")["launchable"] is True
    (bindir / "vibe").unlink()
    os.symlink(tmp_path / "gone", bindir / "vibe")  # what a half-finished upgrade leaves behind
    result = adapters.local_launch("vibe")
    assert result["checked"] is True and result["launchable"] is False


def test_local_launch_is_unchecked_when_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "0")
    monkeypatch.setattr(adapters, "AIDER_BIN", tmp_path / "missing")
    result = adapters.local_launch("aider")
    assert result["checked"] is False and result["launchable"] is True


def test_classify_sheds_local_only_seat_without_binary(monkeypatch):
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(
        capacity,
        "_local_launch",
        lambda agent: {
            "checked": True,
            "launchable": False,
            "binary": "/x/aider",
            "reason": "launch binary missing: /x/aider",
        },
    )
    monkeypatch.setattr(capacity, "_local_only", lambda agent: True)
    state, reason, meta = capacity._classify("aider", capacity.AGENTS["aider"], None)
    assert state == capacity.SHED
    assert meta["availability"] == "unavailable_cli_missing"
    assert "drainable=install" in meta["next_action"]


def test_classify_never_sheds_hosted_capable_seat_for_a_missing_local_binary(monkeypatch):
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(
        capacity,
        "_local_launch",
        lambda agent: {
            "checked": True,
            "launchable": False,
            "binary": "codex",
            "reason": "launch binary missing: codex",
        },
    )
    monkeypatch.setattr(capacity, "_local_only", lambda agent: False)
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    out = capacity._classify("codex", capacity.AGENTS["codex"], None)
    assert not (out[0] == capacity.SHED and "not launchable" in str(out[1]))


def test_local_only_matches_router_keepalive_set():
    assert capacity._local_only("aider") is True
    assert capacity._local_only("vibe") is True
    assert capacity._local_only("codex") is False
