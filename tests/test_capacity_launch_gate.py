"""Launch gate: a local-only seat whose launch binary is missing is SHED, never picked.

Found when a since-removed seat (aider) failed 47 of 47 starts on a missing binary, 11-05..11-10."""

import os
import stat

import adapters
import capacity


def _exe(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_local_launch_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "1")
    monkeypatch.setattr(adapters, "LOCAL_AGENT_PATH_DIRS", (str(tmp_path / "empty"),))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(adapters, "EXTRA_LAUNCH_CANDIDATES", {})  # no host path can leak in
    result = adapters.local_launch("vibe")
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
    monkeypatch.setattr(adapters, "LOCAL_AGENT_PATH_DIRS", (str(tmp_path / "missing"),))
    monkeypatch.setenv("PATH", "/nonexistent")
    result = adapters.local_launch("vibe")
    assert result["checked"] is False and result["launchable"] is True


def test_classify_sheds_local_only_seat_without_binary(monkeypatch):
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(
        capacity,
        "_local_launch",
        lambda agent: {
            "checked": True,
            "launchable": False,
            "binary": "/x/vibe",
            "reason": "launch binary missing: /x/vibe",
        },
    )
    monkeypatch.setattr(capacity, "_local_only", lambda agent: True)
    state, reason, meta = capacity._classify("vibe", capacity.AGENTS["vibe"], None)
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
    meta = out[2] if len(out) > 2 and isinstance(out[2], dict) else {}
    assert meta.get("policy") != "launch-binary-missing"
    assert meta.get("availability") != "unavailable_cli_missing"


def test_local_only_matches_router_keepalive_set():
    assert capacity._local_only("muse") is True
    assert capacity._local_only("vibe") is True
    assert capacity._local_only("codex") is False


def test_codex_app_bundled_binary_makes_it_launchable(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "1")
    monkeypatch.setattr(adapters, "LOCAL_AGENT_PATH_DIRS", (str(tmp_path / "empty"),))
    monkeypatch.setenv("PATH", "/nonexistent")
    bundled = _exe(tmp_path / "codex")
    monkeypatch.setattr(adapters, "EXTRA_LAUNCH_CANDIDATES", {"codex": (str(bundled),)})
    result = adapters.local_launch("codex")
    assert result["launchable"] is True and result["path"] == str(bundled)


def test_codex_without_any_binary_stays_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "1")
    monkeypatch.setattr(adapters, "LOCAL_AGENT_PATH_DIRS", (str(tmp_path / "empty"),))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(adapters, "EXTRA_LAUNCH_CANDIDATES", {"codex": (str(tmp_path / "gone"),)})
    result = adapters.local_launch("codex")
    assert result["checked"] is True and result["launchable"] is False


def test_launch_dirs_match_the_dispatch_path_prefix():
    import dispatcher

    prefix = dispatcher._path_prefix()
    dirs = prefix.split('PATH="', 1)[1].rsplit(":$PATH", 1)[0].split(":")
    assert tuple(dirs) == adapters.LOCAL_AGENT_PATH_DIRS


def test_a_crashing_launch_check_is_reported_not_silent(monkeypatch):
    def boom(agent):
        raise OSError("disk gone")

    monkeypatch.setattr(adapters, "local_launch", boom)
    result = capacity._local_launch("vibe")
    assert result["checked"] is False and result["launchable"] is True
    assert "OSError: disk gone" in result["reason"]


def test_build_row_keeps_the_crash_text_in_launch_reason(monkeypatch, tmp_path):
    def boom(agent):
        raise OSError("disk gone")

    monkeypatch.setattr(adapters, "local_launch", boom)
    monkeypatch.setattr(capacity, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "shed")
    built = capacity.build(ccusage_block=None)
    launch = built["agents"]["vibe"]["launch"]
    assert launch["checked"] is False
    assert "OSError: disk gone" in launch["reason"]


def test_launch_check_records_no_capability_use(monkeypatch, tmp_path):
    import capabilities

    calls = []
    monkeypatch.setattr(capabilities, "daily_heartbeat", lambda *a, **k: calls.append(a) or True)
    adapters.build_command("gemini", "launch-check", cwd=tmp_path, record_use=False)
    assert calls == []
    adapters.build_command("gemini", "real prompt", cwd=tmp_path)
    assert [c[0] for c in calls] == ["agy-runtime-isolation"]

    seen = {}
    real = adapters.build_command

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, cwd=tmp_path, **kwargs)

    monkeypatch.setenv("ORCH_LAUNCH_CHECK", "1")
    monkeypatch.setattr(adapters, "build_command", spy)
    adapters.local_launch("gemini")
    assert seen.get("record_use") is False
