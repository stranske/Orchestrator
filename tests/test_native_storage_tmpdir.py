"""Owned native scratch routes through existing storage/dispatch paths, offline."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import test_storage_lifecycle as lifecycle_fixtures
from test_closer_reasoning_boundary import native_world as native_world

import adapters
import dispatcher
import storage_lifecycle as storage


@pytest.fixture
def job(tmp_path):
    root = tmp_path.resolve() / "job"
    root.mkdir(mode=0o700)
    control = tmp_path.resolve() / "control"
    manager = storage.Reservations(control, tmp_path, floor=0, free=lambda _: 100000)
    manager.admit("job1", "fixture", 0, 1, owner_token="test-token")
    storage.write_json(root / "owner.json", {"job": "job1", "token": "test-token"})
    info = root.stat()
    manager.bind("job1", root=str(root), root_identity=[info.st_dev, info.st_ino])
    return {
        "ORCH_STORAGE_RUN_ROOT": str(root),
        "ORCH_STORAGE_RUN_ID": "job1",
        "ORCH_STORAGE_OWNER_TOKEN": "test-token",
        "ORCH_STORAGE_STATE_DIR": str(control),
    }


def test_native_prelude_uses_owned_tmp_only(job, tmp_path, monkeypatch):
    for key, value in job.items():
        monkeypatch.setenv(key, value)
    shared = tmp_path / "shared-agent-runtime"
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", shared)
    monkeypatch.setattr(
        dispatcher, "_gh_guard_exports", lambda: ["export FIXTURE_GH_GUARD=unchanged"]
    )
    prelude = dispatcher._agent_runtime_prelude("claude")
    code = "import json,os;print(json.dumps({k:os.environ.get(k) for k in ['TMPDIR','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','FIXTURE_GH_GUARD']}))"
    result = subprocess.run(
        ["/bin/bash", "-c", prelude + shlex.join([sys.executable, "-c", code])],
        capture_output=True,
        text=True,
        check=True,
    )
    env = json.loads(result.stdout)
    assert Path(env["TMPDIR"]).is_relative_to(Path(job["ORCH_STORAGE_RUN_ROOT"]))
    assert env["XDG_CACHE_HOME"] == str(shared / "claude/.cache")
    assert env["XDG_CONFIG_HOME"] == str(shared / "claude/.config")
    assert env["XDG_DATA_HOME"] == str(shared / "claude/.local/share")
    assert env["FIXTURE_GH_GUARD"] == "unchanged"


def test_direct_adapter_routes_owned_tmp_without_mutating_input(job):
    original = {**job, "TMPDIR": "/legacy/tmp", "GH_CONFIG_DIR": "/existing/auth/config"}
    result = adapters.agent_subprocess_env("claude", original)
    assert result is not original
    assert Path(result["TMPDIR"]).is_relative_to(Path(job["ORCH_STORAGE_RUN_ROOT"]))
    assert result["GH_CONFIG_DIR"] == original["GH_CONFIG_DIR"]
    assert original["TMPDIR"] == "/legacy/tmp"


@pytest.mark.parametrize("change", ["token", "partial", "mode", "symlink", "ledger", "nested"])
def test_invalid_job_refuses_without_shared_tmp_fallback(job, tmp_path, change):
    env = dict(job)
    root = Path(job["ORCH_STORAGE_RUN_ROOT"])
    if change == "token":
        env["ORCH_STORAGE_OWNER_TOKEN"] = "wrong"
    elif change == "partial":
        env.pop("ORCH_STORAGE_RUN_ID")
    elif change == "mode":
        root.chmod(0o755)
    elif change == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        env["ORCH_STORAGE_RUN_ROOT"] = str(alias)
    elif change == "ledger":
        p = Path(job["ORCH_STORAGE_STATE_DIR"]) / "reservations.json"
        value = json.loads(p.read_text())
        value["jobs"]["job1"]["status"] = "uncertain"
        storage.write_json(p, value)
    elif change == "nested":
        (root / "tmp").mkdir(mode=0o700)
        (root / "tmp/native-tmp").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(storage.StorageBlocked):
        storage.owned_agent_tmpdir("claude", env)


def test_absent_context_preserves_direct_environment():
    env = {"TMPDIR": "/legacy/tmp"}
    assert adapters.agent_subprocess_env("claude", env) is env


@pytest.mark.parametrize("code", [0, 7])
def test_native_scratch_success_or_failure_retires_with_job_not_shared_runtime(
    job, tmp_path, monkeypatch, code
):
    for key, value in job.items():
        monkeypatch.setenv(key, value)
    shared = tmp_path / "shared"
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", shared)
    monkeypatch.setattr(dispatcher, "_gh_guard_exports", lambda: [])
    prelude = dispatcher._agent_runtime_prelude("claude")
    program = (
        "import os,pathlib,sys;pathlib.Path(os.environ['TMPDIR'],'native.txt').write_text('scratch');print('compact failure evidence',file=sys.stderr);sys.exit("
        + str(code)
        + ")"
    )
    completed = subprocess.run(
        ["/bin/bash", "-c", prelude + shlex.join([sys.executable, "-c", program])],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == code
    root = Path(job["ORCH_STORAGE_RUN_ROOT"])
    identity = (root.stat().st_dev, root.stat().st_ino)
    retained = tmp_path / "retained-stderr.txt"
    retained.write_text(completed.stderr)
    assert (root / "tmp/native-tmp/claude/native.txt").read_text() == "scratch"
    cache_before = (shared / "claude/.cache").stat().st_ino
    assert storage.retire(root, identity, job["ORCH_STORAGE_OWNER_TOKEN"]) == []
    assert not root.exists()
    assert (shared / "claude/.cache").stat().st_ino == cache_before
    assert retained.read_text() == "compact failure evidence\n"


def test_invalid_context_offload_is_explicit_no_start(job, monkeypatch):
    for key, value in job.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ORCH_STORAGE_OWNER_TOKEN", "wrong")
    monkeypatch.delenv("ORCH_OFFLOAD_DISABLED", raising=False)
    monkeypatch.setattr(
        dispatcher, "_isolate_offload_cwd", lambda *_a, **_kw: pytest.fail("workspace copied")
    )
    monkeypatch.setattr(
        dispatcher.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("provider launched")
    )
    result = dispatcher.offload("claude", "inert", mode="assess", isolate=True)
    assert result["storage_context_refused"] and result["exit"] == 2
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert result["next_trigger"] == "reconcile_owned_storage_context"


def test_invalid_context_plan_is_named_no_start_error(job, monkeypatch):
    for key, value in job.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ORCH_STORAGE_OWNER_TOKEN", "wrong")
    result = dispatcher.plan_dispatch({"agent": "claude"}, dry_run=False)
    assert result["storage_context_refused"] and result["process_started"] is False


def test_actual_closer_cli_keeps_proven_storage_refusal_recoverable(
    native_world, monkeypatch, capsys
):
    import closer_dispatcher as closer
    import closer_resources

    w = native_world
    refused = {
        "agent": "claude",
        "exit": 2,
        "output": "",
        "run_id": None,
        "storage_context_refused": True,
        "process_started": False,
        "outcome_uncertain": False,
        "error": "owned storage marker differs",
    }
    monkeypatch.setattr(dispatcher, "offload", lambda *_a, **_kw: dict(refused))
    assert closer.main(w.argv) == 78
    capsys.readouterr()
    shard = (
        closer_resources.shard_dir(closer_resources.scoped_root(w.ledger), w.snapshot["target"])
        / "state.json"
    )
    row = json.loads(shard.read_text())["frontiers"][w.frontier["frontier_id"]]["items"][
        w.snapshot["target"]
    ]
    assert row["status"] == "held" and row["stage_status"] == "completed"
    result = row["results"]["interpret"]
    assert result["classification"] == "storage_context_unavailable"
    assert result["next_trigger"] == "reconcile_owned_storage_context"
    assert all(
        result["pending_evidence"]["refusal"].get(key) == value for key, value in refused.items()
    )
    assert w.events["provider"] == w.events["effect"] == 0
    assert all(path.read_bytes() == raw for path, raw in w.originals.items())


@pytest.mark.parametrize("name", ["owner.json", "lock"])
def test_fifo_metadata_refuses_without_blocking(job, name):
    import os

    path = (
        Path(job["ORCH_STORAGE_RUN_ROOT"]) / name
        if name == "owner.json"
        else Path(job["ORCH_STORAGE_STATE_DIR"]) / name
    )
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(storage.StorageBlocked):
        storage.owned_agent_tmpdir("claude", job)


def test_nested_path_replacement_during_validation_is_refused(job, tmp_path, monkeypatch):
    real_open = storage.os.open
    agent_dir = Path(job["ORCH_STORAGE_RUN_ROOT"]) / "tmp/native-tmp/claude"
    outside = tmp_path / "outside"
    outside.mkdir()

    def opened(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if path == "claude" and kw.get("dir_fd") is not None:
            agent_dir.rename(agent_dir.with_name("original"))
            agent_dir.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(storage.os, "open", opened)
    with pytest.raises(storage.StorageBlocked, match="changed"):
        storage.owned_agent_tmpdir("claude", job)


def test_owned_routing_uses_explicit_ledger_when_runtime_is_staged(job, monkeypatch):
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", "/unrelated/staged-runtime-copy")
    env = {**job, "ORCH_LOCAL_RUNTIME": "/unrelated/staged-runtime-copy"}
    assert (
        storage.owned_agent_tmpdir("claude", env)
        == Path(job["ORCH_STORAGE_RUN_ROOT"]) / "tmp/native-tmp/claude"
    )


def test_direct_invalid_context_refuses_before_command(job, tmp_path, monkeypatch):
    env = {**job, "ORCH_STORAGE_OWNER_TOKEN": "wrong"}
    monkeypatch.setattr(adapters, "build_command", lambda *_a, **_kw: pytest.fail("command built"))
    result = adapters.dispatch("claude", "inert", cwd=str(tmp_path), env=env)
    assert result["storage_context_refused"] and result["changed_files"] is False
    assert result["process_started"] is False


def test_context_changed_before_early_muse_prelude_is_no_start(job, tmp_path, monkeypatch):
    for key, value in job.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ORCH_OFFLOAD_DISABLED", raising=False)
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *_a, **_kw: None)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda p, *_a, **_kw: p)

    def invalidated(_agent):
        raise storage.StorageBlocked("fixture ownership invalidated before native prelude")

    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", invalidated)
    monkeypatch.setattr(
        dispatcher.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("provider launched")
    )
    result = dispatcher.offload("muse", "inert", cwd=str(tmp_path), mode="assess")
    assert result["storage_context_refused"] and result["process_started"] is False


def test_native_subdirectory_cross_filesystem_identity_refused(job, monkeypatch):
    from types import SimpleNamespace

    opened_agent = []
    actual_open, actual_fstat = storage.os.open, storage.os.fstat

    def opened(path, flags, *args, **kwargs):
        fd = actual_open(path, flags, *args, **kwargs)
        if path == "claude" and kwargs.get("dir_fd") is not None:
            opened_agent.append(fd)
        return fd

    def attributes(fd):
        info = actual_fstat(fd)
        if opened_agent and fd == opened_agent[-1]:
            return SimpleNamespace(
                st_dev=info.st_dev + 1, st_mode=info.st_mode, st_uid=info.st_uid, st_ino=info.st_ino
            )
        return info

    monkeypatch.setattr(storage.os, "open", opened)
    monkeypatch.setattr(storage.os, "fstat", attributes)
    with pytest.raises(storage.StorageBlocked, match="filesystem"):
        storage.owned_agent_tmpdir("claude", job)


@pytest.fixture
def supervisor_job(tmp_path):
    return lifecycle_fixtures.job.__wrapped__(tmp_path)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_actual_supervisor_retires_native_tmp_on_success_and_failure(
    supervisor_job, tmp_path, monkeypatch, exit_code
):

    args = supervisor_job
    args.command[-1] = str(exit_code)
    observed = tmp_path / "native-temp-path.txt"
    shared = tmp_path / "shared-agent-runtime"
    monkeypatch.setenv("PYTHONPATH", str(Path(dispatcher.__file__).parent))
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setenv("ORCH_GEMINI_MODEL_PROBE", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("ORCH_AGENT_RUNTIME_DIR", str(shared))
    monkeypatch.setenv("NATIVE_TEMP_OBSERVATION", str(observed))
    program = (
        "import os,pathlib;root=pathlib.Path(os.environ['TMPDIR']);"
        "(root/'native.txt').write_text('inert native scratch');"
        "pathlib.Path(os.environ['NATIVE_TEMP_OBSERVATION']).write_text(str(root))"
    )
    child = (
        "import shlex,subprocess,sys;from dispatcher import _agent_runtime_prelude;"
        "subprocess.run(['bash','-c',_agent_runtime_prelude('claude')+"
        + "shlex.join([sys.executable,'-c',"
        + repr(program)
        + "])],check=True)"
    )
    script = Path(args.command[1])
    script.write_text(
        script.read_text().replace(
            'exit "$1"\n', shlex.join([sys.executable, "-c", child]) + '\nexit "$1"\n'
        )
    )
    assert storage.run_job(args, free=lambda _: storage.GIB, probe=lambda _: None) == exit_code
    receipts = list((args.state_dir / "receipts").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    root = Path(receipt["root"])
    assert Path(observed.read_text()).is_relative_to(root / "tmp/native-tmp/claude")
    assert not (root / "tmp").exists()
    assert (shared / "claude/.cache").is_dir()
    assert receipt["terminal_owned_footprint"]["entries"] > 0
    if exit_code:
        assert (
            root / "staging/verify-before-sync.fake/state/input"
        ).read_text() == "unique-failure-state"
        assert receipt["status"] == "fixtures_retired_failure_retained"
    else:
        assert not root.exists()
