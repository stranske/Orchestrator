"""Inert supervised-launch controls. No native CLI is resolved or invoked."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import transport_qualification_launcher as launcher

FIXTURE = Path(__file__).parent / "fixtures/transport_qualification_launcher/inert_child.py"
TOKEN = "synthetic-existing-oauth-token-DO-NOT-RETAIN"


class Lease:
    def __init__(self):
        self.calls = []

    def check(self, pool_id, run_id):
        assert pool_id == launcher.POOL
        self.calls.append(run_id)


@pytest.fixture(autouse=True)
def prohibit_native_gate(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("test attempted native runtime")

    monkeypatch.setattr(launcher.NativeRuntime, "command", forbidden)


@pytest.fixture
def wave(tmp_path):
    binary = tmp_path / "success"
    binary.write_bytes(f"#!{sys.executable}\n".encode() + FIXTURE.read_bytes())
    binary.chmod(0o700)
    prepared = launcher.synthetic_wave(binary)
    return binary, prepared


def run(wave, tmp_path, *, mode="success", **kwargs):
    binary, _ = wave
    if mode != binary.name:
        new = binary.with_name(mode)
        new.write_bytes(binary.read_bytes())
        new.chmod(0o700)
        binary = new
    prepared = launcher.synthetic_wave(binary)
    return launcher.run_wave(
        prepared,
        run_id="fixture-wave",
        receipt_parent=tmp_path,
        held_lease=Lease(),
        execute=True,
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(binary, launcher.file_sha(binary)),
        **kwargs,
    )


def read_receipt(report, index=1):
    return json.loads((Path(report["receipt_directory"]) / f"cell-{index}.json").read_text())


def test_default_dry_run_does_not_spawn_resolve_or_create_receipts(wave, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run attempted process")

    monkeypatch.setattr(launcher.subprocess, "Popen", forbidden)
    report = launcher.run_wave(wave[1], run_id="dry", receipt_parent=tmp_path)
    assert report["status"] == "dry_run" and report["cli_launch_count"] == 0
    assert not (tmp_path / "dry").exists()


def test_eight_serial_inert_launches_keep_effort_quota_and_authority_unknown(
    wave, tmp_path, monkeypatch
):
    original = launcher.subprocess.Popen
    processes = []

    def observed(*args, **kwargs):
        assert all(p.poll() is not None for p in processes)
        assert kwargs["shell"] is False and kwargs["start_new_session"] is True
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(launcher.subprocess, "Popen", observed)
    report = run(wave, tmp_path)
    assert report["cli_launch_count"] == len(processes) == 8
    assert report["status"] == "completed_unverified_calibration"
    assert report["activation"] is False and report["role_qualification"] is False
    assert report["http_attempt_count"] == "UNKNOWN"
    assert report["observed_effort"] == "UNKNOWN"
    assert report["quota_balance"] == report["pool_charge"] == "UNKNOWN"
    assert report["outer_retries"] == report["fallbacks"] == 0
    assert report["origin"] == "offline_fixture"
    for i in range(1, 9):
        record = read_receipt(report, i)
        assert record["role"] in ("bounded_classification", "strong_criterion_adjudication")
        assert record["final"]["criterion_id"] == "C1"
        assert record["normalized"]["observed"]["effort"] == "UNKNOWN"
        assert record["normalized"]["usage"]["provider_attested"] is False


def test_actual_child_controls_environment_stdin_and_private_paths(wave, tmp_path, monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_RETRY_WATCHDOG",
        "NODE_OPTIONS",
        "AWS_PROFILE",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "PYTHONPATH",
        "CLAUDE_CODE_USE_BEDROCK",
    ):
        monkeypatch.setenv(key, "ambient-must-not-reach-child")
    report = run(wave, tmp_path)
    record = read_receipt(report)
    observed = json.loads(record["stderr_redacted"])
    env = observed["fixture_env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "[REDACTED]"
    assert all(env[k] == v for k, v in launcher.CONTROLS.items())
    assert "ambient-must-not-reach-child" not in json.dumps(observed)
    allowed = set(launcher.CONTROLS) | {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "TMPDIR",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    assert set(env) <= allowed
    prompt = json.loads(wave[1].prompts_text)[0]["prompt"]
    assert observed["fixture_prompt"] == prompt
    assert prompt not in observed["fixture_argv"]
    assert observed["fixture_argv"][-4:] == ["--output-format", "json", "--max-turns", "1"]
    assert "--verbose" not in observed["fixture_argv"] and "--bare" not in observed["fixture_argv"]
    private = Path(report["receipt_directory"]) / "cell-1"
    for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "TMPDIR", "CLAUDE_CONFIG_DIR"):
        path = Path(env[key])
        assert path.is_relative_to(private)
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert Path(observed["fixture_cwd"]).resolve() == (private / "cwd").resolve()


@pytest.mark.parametrize(
    "mode",
    [
        "timeout",
        "oversize",
        "nonzero",
        "restriction",
        "malformed",
        "wrongmodel",
        "wrongcriterion",
        "resumed",
        "multiturn",
    ],
)
def test_failure_stops_whole_wave_without_retry(wave, tmp_path, mode):
    kwargs = {"cell_seconds": 0.3} if mode == "timeout" else {}
    if mode == "oversize":
        kwargs["output_cap"] = 1024
    report = run(wave, tmp_path, mode=mode, **kwargs)
    assert report["status"] == "stopped"
    assert report["cli_launch_count"] == 1
    assert report["receipts"] == ["cell-1.json"]
    record = read_receipt(report)
    assert record["status"] == "stopped"
    assert record["failure_type"] == "ValueError"
    assert report["outer_retries"] == 0
    if mode == "timeout":
        assert record["problem"] == "timeout"
        assert record["seconds"] < 2
        with pytest.raises(ProcessLookupError):
            os.killpg(record["process_group"], 0)
    if mode == "oversize":
        assert record["problem"] == "combined_output_cap"
        assert (
            len(record["stdout_redacted"].encode()) + len(record["stderr_redacted"].encode())
            <= 1024
        )
        assert record["combined_bytes_observed_at_least"] > 1024


def test_receipts_are_exclusive_durable_and_private_with_no_raw_token(wave, tmp_path):
    report = run(wave, tmp_path, mode="secret")
    root = Path(report["receipt_directory"])
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.glob("*.json"):
        text = path.read_text()
        assert TOKEN not in text and "sk-ant-SECRET_SYNTHETIC_123456" not in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads((root / "attempt-1-starting.json").read_text())["status"].startswith(
        "starting_outcome_uncertain"
    )
    started = json.loads((root / "attempt-1-started.json").read_text())
    assert started["status"] == "started_outcome_pending" and started["process_group"] > 1
    with pytest.raises(FileExistsError):
        run(wave, tmp_path, mode="secret")


def test_binary_hash_drift_refuses_before_spawn(wave, tmp_path):
    binary, prepared = wave
    runtime = launcher.FixtureRuntime(binary, launcher.file_sha(binary))
    binary.write_bytes(binary.read_bytes() + b"\n# drift\n")
    report = launcher.run_wave(
        prepared,
        run_id="drift",
        receipt_parent=tmp_path,
        execute=True,
        held_lease=Lease(),
        oauth_token=TOKEN,
        runtime=runtime,
    )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 0


def test_source_drift_refuses_before_spawn_without_modifying_existing_source(
    wave, tmp_path, monkeypatch
):
    original = launcher.file_sha

    def changed(path):
        return "0" * 64 if path.name == "adapters.py" else original(path)

    monkeypatch.setattr(launcher, "file_sha", changed)
    report = launcher.run_wave(
        wave[1],
        run_id="drift",
        receipt_parent=tmp_path,
        execute=True,
        held_lease=Lease(),
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(wave[0], original(wave[0])),
    )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 0


def test_lease_loss_after_first_result_stops_before_next_submission(wave, tmp_path):
    class Lost(Lease):
        def check(self, pool_id, run_id):
            super().check(pool_id, run_id)
            if len(self.calls) >= 3:
                raise ValueError("lease lost")

    report = launcher.run_wave(
        wave[1],
        run_id="lost",
        receipt_parent=tmp_path,
        execute=True,
        held_lease=Lost(),
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(wave[0], launcher.file_sha(wave[0])),
    )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 1


def test_start_receipt_exists_before_popen_and_started_receipt_before_result(
    wave, tmp_path, monkeypatch
):
    original = launcher.subprocess.Popen

    def observed(*args, **kwargs):
        root = tmp_path / "fixture-wave"
        assert (root / "attempt-1-starting.json").exists()
        assert not (root / "attempt-1-started.json").exists()
        raise OSError("synthetic no start")

    monkeypatch.setattr(launcher.subprocess, "Popen", observed)
    report = run(wave, tmp_path)
    assert report["cli_launch_count"] == 0 and report["status"] == "stopped"
    assert not (Path(report["receipt_directory"]) / "attempt-1-started.json").exists()
    monkeypatch.setattr(launcher.subprocess, "Popen", original)


def test_post_spawn_receipt_failure_still_counts_and_kills_process(wave, tmp_path, monkeypatch):
    original = launcher._persist
    original_popen = launcher.subprocess.Popen
    pids = []

    def recording_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    def failed(root, name, value, token):
        if name.endswith("-started.json"):
            raise OSError("synthetic fsync failure")
        return original(root, name, value, token)

    monkeypatch.setattr(launcher, "_persist", failed)
    monkeypatch.setattr(launcher.subprocess, "Popen", recording_popen)
    try:
        report = run(wave, tmp_path, mode="timeout")
        assert report["status"] == "stopped" and report["cli_launch_count"] == 1
        assert len(pids) == 1
        with pytest.raises(ProcessLookupError):
            os.killpg(pids[0], 0)
    finally:
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_whole_wave_deadline_stops_without_launch(wave, tmp_path):
    report = run(wave, tmp_path, wave_seconds=0.1)
    assert report["status"] == "stopped" and report["cli_launch_count"] == 0


@pytest.mark.parametrize(
    "change", ["prompt", "profile", "model", "parallel", "retry", "too_many", "argv", "config"]
)
def test_plan_drift_or_unsafe_plan_refuses_as_data(wave, change):
    prepared = wave[1]
    plan, prompts = json.loads(prepared.plan_text), json.loads(prepared.prompts_text)
    if change == "prompt":
        prompts[0]["prompt"] += " mutation"
    elif change == "profile":
        plan["candidate"]["profile"]["profile_id"] = "claude-opus-5-5-high"
    elif change == "model":
        plan["candidate"]["profile"]["requested_model"] = "claude-opus-5-5"
    elif change == "parallel":
        plan["calls"]["concurrency"] = 2
    elif change == "retry":
        plan["calls"]["retries"] = 1
    elif change == "too_many":
        plan["calls"]["cells"] *= 2
    elif change == "argv":
        plan["candidate"]["launch"]["argv"] += ["--verbose"]
    else:
        plan["candidate"]["launch"]["config"]["tools"] = ["shell"]
    a, b = json.dumps(plan), json.dumps(prompts)
    pins = replace(
        prepared.pins, plan_sha256=launcher.sha(a.encode()), prompts_sha256=launcher.sha(b.encode())
    )
    with pytest.raises(ValueError):
        launcher.prepare_wave(a, b, pins)


def test_pool_factory_uses_same_hashed_lane_and_held_active_state_only_in_temp_root(tmp_path):
    with launcher.held_subscription_lease(
        "offline", deadline=time.time() + 1, offline_root=tmp_path
    ) as lease:
        assert lease.is_fixture
        lease.check(launcher.POOL, "offline-1")
        lane = launcher.sha(launcher.POOL.encode())
        assert (tmp_path / "pools" / lane / "slot-0").exists()
        with pytest.raises(ValueError):
            lease.check("invented-model-pool", "offline-1")
    with pytest.raises(ValueError):
        lease.check(launcher.POOL, "offline-1")


def test_native_path_refuses_fixture_lease_and_custom_limits_without_native_invocation(
    wave, tmp_path
):
    with launcher.held_subscription_lease(
        "offline", deadline=time.time() + 1, offline_root=tmp_path / "pools"
    ) as lease:
        with pytest.raises(ValueError):
            launcher.run_wave(
                wave[1],
                run_id="offline",
                receipt_parent=tmp_path,
                held_lease=lease,
                execute=True,
                oauth_token=TOKEN,
            )


def test_in_process_concurrent_wave_is_rejected(wave, tmp_path):
    launcher.LOCK.acquire()
    try:
        with pytest.raises(ValueError, match="another supervised"):
            run(wave, tmp_path)
    finally:
        launcher.LOCK.release()


def test_emitted_selftest_uses_only_inert_processes_and_verify_runner(monkeypatch):
    import verify

    result = verify.run_selftests(["transport_qualification_launcher"], jobs=1)
    assert result["ok"] == ["transport_qualification_launcher"]
    assert result["failed"] == {} and result["skipped"] == {}


def test_flat_exec_mirror_selftest_uses_retained_fixture(tmp_path):
    flat = tmp_path / "exec-mirror"
    flat.mkdir()
    source = Path(launcher.__file__).resolve().parent
    for name in launcher.SOURCE_NAMES:
        (flat / name).write_bytes((source / name).read_bytes())
    fixture = flat / "tests/fixtures/transport_qualification_launcher/inert_child.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(FIXTURE.read_bytes())
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, transport_qualification_launcher as m; print(json.dumps(m.selftest()))",
        ],
        cwd=flat,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(flat), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    report = json.loads(child.stdout)
    assert report["synthetic_pipeline_controls_only"] is True
    assert report["activation"] is False
    assert all(report["checked_assertions"].values())


def test_descendant_holding_pipes_triggers_group_kill_and_whole_wave_stop(
    wave, tmp_path, monkeypatch
):
    killed = []
    original = launcher._kill_group

    def observed(proc):
        killed.append(proc.pid)
        return original(proc)

    monkeypatch.setattr(launcher, "_kill_group", observed)
    report = run(wave, tmp_path, mode="descendant", cell_seconds=0.35)
    assert report["cli_launch_count"] == 1 and report["status"] == "stopped"
    row = read_receipt(report)
    assert killed == [row["process_group"]]
    assert row["failure_type"] == "ValueError"  # not unproven cleanup (RuntimeError)
    with pytest.raises(ProcessLookupError):
        os.killpg(row["process_group"], 0)


def test_hard_cap_combines_stdout_and_stderr(wave, tmp_path):
    report = run(wave, tmp_path, mode="mixedcap", output_cap=1024)
    row = read_receipt(report)
    assert report["status"] == "stopped" and report["cli_launch_count"] == 1
    assert row["problem"] == "combined_output_cap"
    assert len(row["stdout_redacted"]) + len(row["stderr_redacted"]) <= 1024


def test_fenced_tool_activity_stops_before_next_submission(wave, tmp_path):
    report = run(wave, tmp_path, mode="toolactivity")
    row = read_receipt(report)
    assert report["status"] == "stopped" and report["cli_launch_count"] == 1
    assert row["failure_stage"] == "tool_activity_and_no_resumed_history"


def test_unicode_and_trailing_whitespace_prompt_reaches_child_losslessly(wave, tmp_path):
    binary, prepared = wave
    plan, prompts = json.loads(prepared.plan_text), json.loads(prepared.prompts_text)
    prompt_doc = json.loads(prompts[0]["prompt"])
    prompt_doc["unicode"] = "snowman ☃ and é"
    prompts[0]["prompt"] = json.dumps(prompt_doc, ensure_ascii=False) + "\n  "
    raw = prompts[0]["prompt"].encode()
    prompts[0]["prompt_sha256"] = plan["calls"]["cells"][0]["prompt_sha256"] = launcher.sha(raw)
    plan["calls"]["cells"][0]["prompt_bytes"] = len(raw)
    a, b = json.dumps(plan), json.dumps(prompts)
    pins = replace(
        prepared.pins, plan_sha256=launcher.sha(a.encode()), prompts_sha256=launcher.sha(b.encode())
    )
    prepared = launcher.prepare_wave(a, b, pins)
    report = launcher.run_wave(
        prepared,
        run_id="unicode",
        receipt_parent=tmp_path,
        held_lease=Lease(),
        execute=True,
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(binary, launcher.file_sha(binary)),
    )
    observed = json.loads(read_receipt(report)["stderr_redacted"])
    assert observed["fixture_prompt"].encode() == raw


def test_binary_drift_between_cells_is_rehashed_and_stops_wave(wave, tmp_path, monkeypatch):
    binary, prepared = wave
    original = launcher._persist

    def changed_after_first(root, name, value, token):
        original(root, name, value, token)
        if name == "cell-1.json":
            binary.write_bytes(binary.read_bytes() + b"\n# inert fixture drift after first cell\n")

    monkeypatch.setattr(launcher, "_persist", changed_after_first)
    report = launcher.run_wave(
        prepared,
        run_id="late-drift",
        receipt_parent=tmp_path,
        held_lease=Lease(),
        execute=True,
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(binary, launcher.file_sha(binary)),
    )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 1
    assert read_receipt(report, 1)["status"] == "captured_unverified_calibration"
    assert read_receipt(report, 2)["status"] == "stopped"
    assert not (Path(report["receipt_directory"]) / "attempt-2-starting.json").exists()


def test_actual_runtime_binding_matches_child_and_prelaunch_receipt(wave, tmp_path):
    report = run(wave, tmp_path)
    record = read_receipt(report)
    observed = json.loads(record["stderr_redacted"])
    config = record["actual_config"]
    binding = record["actual_execution_binding"]
    assert config["argv"] == observed["fixture_argv"] == record["argv"]
    assert config["cwd"] == observed["fixture_cwd"]
    env = {k: v for k, v in observed["fixture_env"].items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    assert {k: v for k, v in config["environment"].items() if k != "CLAUDE_CODE_OAUTH_TOKEN"} == env
    assert config["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == "REDACTED"
    assert config["oauth_context"] == {
        "variable": "CLAUDE_CODE_OAUTH_TOKEN",
        "present": True,
        "source": "parameter",
        "value": "REDACTED",
    }
    assert config["budgets"]["max_cli_submissions"] == 8
    assert config["budgets"]["combined_stdout_stderr_bytes"] == 65536
    assert config["controls"] == launcher.CONTROLS
    assert record["actual_config_sha256"] == launcher.digest(config)
    assert binding["actual_config_sha256"] == launcher.digest(config)
    assert record["actual_execution_binding_sha256"] == launcher.digest(binding)
    assert binding["run_id"] == record["run_id"]
    assert binding["pool_id"] == launcher.POOL and binding["profile_id"] == launcher.PROFILE
    assert binding["requested_model"] == launcher.MODEL and binding["requested_effort"] == "low"
    assert binding["role"] == record["role"] and binding["prompt_sha256"] == record["prompt_sha256"]
    assert binding["binary_sha256"] == record["binary_sha256"]
    assert binding["source_pins"] == record["source_pins"]
    assert "config_sha256" not in record and "execution_binding_sha256" not in record
    assert record["planned_lineage"]["config_sha256"] != record["actual_config_sha256"]
    starting = json.loads(
        (Path(report["receipt_directory"]) / "attempt-1-starting.json").read_text()
    )
    assert starting["actual_config"] == config
    assert starting["actual_execution_binding_sha256"] == record["actual_execution_binding_sha256"]
    serialized = json.dumps(record)
    assert TOKEN not in serialized and launcher.sha(TOKEN.encode()) not in serialized


def test_actual_config_and_execution_hashes_change_with_child_capture_controls_and_paths(
    wave, tmp_path
):
    binary, prepared = wave
    plan = json.loads(prepared.plan_text)
    cell = plan["calls"]["cells"][0]
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, **launcher.CONTROLS}
    kwargs = dict(
        argv=launcher.fenced_argv(str(binary)) + ["--output-format", "json", "--max-turns", "1"],
        env=env,
        cwd=tmp_path / "cwd",
        private=tmp_path,
        oauth_source="parameter",
        run_id="binding-1",
        cell=cell,
        launch=plan["candidate"]["launch"],
        pins=prepared.pins,
        cell_seconds=45,
        wave_seconds=480,
        output_cap=65536,
        capture_deadline=123.0,
    )
    original = launcher.actual_launch_binding(**kwargs)
    for field in ("capture_flag", "guard", "private_home", "cwd", "cap"):
        changed = deepcopy(kwargs)
        if field == "capture_flag":
            changed["argv"][-1] = "2"
        elif field == "guard":
            changed["env"]["CLAUDE_CODE_NO_MODEL_FALLBACK"] = "0"
        elif field == "private_home":
            changed["env"]["HOME"] = str(tmp_path / "other-home")
        elif field == "cwd":
            changed["cwd"] = tmp_path / "other-cwd"
        else:
            changed["output_cap"] = 1024
        result = launcher.actual_launch_binding(**changed)
        assert result["actual_config_sha256"] != original["actual_config_sha256"]
        assert (
            result["actual_execution_binding_sha256"] != original["actual_execution_binding_sha256"]
        )
    # OAuth value itself is neither retained nor hashed. Only presence/source bind.
    changed = deepcopy(kwargs)
    changed["env"]["CLAUDE_CODE_OAUTH_TOKEN"] = "another-synthetic-secret-token"
    assert launcher.actual_launch_binding(**changed) == original
    changed["oauth_source"] = "existing_environment"
    assert (
        launcher.actual_launch_binding(**changed)["actual_config_sha256"]
        != original["actual_config_sha256"]
    )


def _pool_record(root):
    return root / "pools" / launcher.sha(launcher.POOL.encode()) / "slot-0.json"


def _refuses_reacquisition(root):
    import closer_resources

    with pytest.raises(closer_resources.ResourceBlocked):
        with launcher.held_subscription_lease(
            "must-refuse", deadline=time.time(), offline_root=root
        ):
            raise AssertionError("uncertain pool reservation was reused")


def test_supervisor_death_leaves_live_child_durable_fence(wave, tmp_path):
    binary = wave[0].with_name("timeout")
    binary.write_bytes(wave[0].read_bytes())
    binary.chmod(0o700)
    pool = tmp_path / "durable-pool"
    script = """
import sys,time
from pathlib import Path
import transport_qualification_launcher as l
binary, root = Path(sys.argv[1]), Path(sys.argv[2])
p = l.synthetic_wave(binary)
with l.held_subscription_lease('supervisor-death', deadline=time.time(), offline_root=root) as lease:
    l.run_wave(p,run_id='supervisor-death',receipt_parent=root.parent,held_lease=lease,
               execute=True,oauth_token='synthetic',runtime=l.FixtureRuntime(binary,l.file_sha(binary)),
               cell_seconds=4)
"""
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(launcher.__file__).parent)}
    supervisor = subprocess.Popen(
        [sys.executable, "-c", script, str(binary), str(pool)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    child = None
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                row = json.loads(_pool_record(pool).read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                row = {}
            if row.get("status") == "uncertain":
                child = row["process_group"]
                break
            assert supervisor.poll() is None
            time.sleep(0.01)
        assert child is not None
        assert row["process_birth"].startswith("os-create-time:")
        supervisor.kill()
        supervisor.wait(timeout=2)
        os.killpg(child, 0)  # detached child is still alive after supervisor death
        _refuses_reacquisition(pool)
        assert json.loads(_pool_record(pool).read_text())["status"] == "uncertain"
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)
        if child is not None:
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_failed_group_cleanup_keeps_durable_fence_after_flock_release(wave, tmp_path, monkeypatch):
    binary = wave[0].with_name("timeout")
    binary.write_bytes(wave[0].read_bytes())
    binary.chmod(0o700)
    prepared = launcher.synthetic_wave(binary)
    pool = tmp_path / "cleanup-pool"

    def failed_cleanup(proc):
        raise RuntimeError("inert cleanup failure")

    monkeypatch.setattr(launcher, "_kill_group", failed_cleanup)
    child = None
    try:
        with launcher.held_subscription_lease(
            "cleanup", deadline=time.time(), offline_root=pool
        ) as lease:
            report = launcher.run_wave(
                prepared,
                run_id="cleanup",
                receipt_parent=tmp_path,
                held_lease=lease,
                execute=True,
                oauth_token=TOKEN,
                runtime=launcher.FixtureRuntime(binary, launcher.file_sha(binary)),
                cell_seconds=0.25,
            )
        child = read_receipt(report)["process_group"]
        assert report["status"] == "stopped" and report["cli_launch_count"] == 1
        os.killpg(child, 0)
        assert json.loads(_pool_record(pool).read_text())["status"] == "uncertain"
        _refuses_reacquisition(pool)
    finally:
        if child is not None:
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_spawn_journal_gap_preserves_assigned_starting_intent(wave, tmp_path, monkeypatch):
    pool = tmp_path / "starting-pool"

    def uncertain_spawn(*args, **kwargs):
        raise OSError("inert uncertain spawn result")

    monkeypatch.setattr(launcher.subprocess, "Popen", uncertain_spawn)
    with launcher.held_subscription_lease("gap", deadline=time.time(), offline_root=pool) as lease:
        report = launcher.run_wave(
            wave[1],
            run_id="gap",
            receipt_parent=tmp_path,
            held_lease=lease,
            execute=True,
            oauth_token=TOKEN,
            runtime=launcher.FixtureRuntime(wave[0], launcher.file_sha(wave[0])),
        )
    row = json.loads(_pool_record(pool).read_text())
    assert row["status"] == "starting" and row["run_id"] == "gap-1"
    assert report["status"] == "stopped" and report["cli_launch_count"] == 0
    _refuses_reacquisition(pool)


def test_started_journal_failure_and_failed_cleanup_keep_starting_fence(
    wave, tmp_path, monkeypatch
):
    import closer_resources

    binary = wave[0].with_name("timeout")
    binary.write_bytes(wave[0].read_bytes())
    binary.chmod(0o700)
    prepared = launcher.synthetic_wave(binary)
    pool = tmp_path / "journal-failure-pool"

    def failed_journal(*args):
        raise OSError("inert start journal failure")

    def failed_cleanup(proc):
        raise RuntimeError("inert cleanup failure")

    monkeypatch.setattr(closer_resources, "finite_pool_process_started", failed_journal)
    monkeypatch.setattr(launcher, "_kill_group", failed_cleanup)
    child = None
    try:
        with launcher.held_subscription_lease(
            "journal-gap", deadline=time.time(), offline_root=pool
        ) as lease:
            report = launcher.run_wave(
                prepared,
                run_id="journal-gap",
                receipt_parent=tmp_path,
                held_lease=lease,
                execute=True,
                oauth_token=TOKEN,
                runtime=launcher.FixtureRuntime(binary, launcher.file_sha(binary)),
            )
        child = read_receipt(report)["process_group"]
        row = json.loads(_pool_record(pool).read_text())
        assert row["status"] == "starting" and row["run_id"] == "journal-gap-1"
        assert report["cli_launch_count"] == 1
        _refuses_reacquisition(pool)
    finally:
        if child is not None:
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "key,value",
    [
        ("preflight_seconds_max", 121),
        ("wall_seconds_per_call_max", 46),
        ("total_wall_seconds_max", 481),
        ("max_cli_submissions", 9),
        ("native_max_output_tokens_per_request", 513),
        ("outer_retries", 1),
        ("payg_allowed", True),
    ],
)
def test_executable_plan_budgets_must_match_exact_reviewed_contract(wave, key, value):
    p = wave[1]
    plan = json.loads(p.plan_text)
    plan["bounds"][key] = value
    text = json.dumps(plan)
    pins = replace(p.pins, plan_sha256=launcher.sha(text.encode()))
    with pytest.raises(ValueError, match="budgets"):
        launcher.prepare_wave(text, p.prompts_text, pins)


def test_aggregate_preflight_budget_stops_before_first_spawn(wave, tmp_path, monkeypatch):
    clock = [0.0]
    original = launcher._source_check

    def expensive(pins):
        original(pins)
        clock[0] += 121

    monkeypatch.setattr(launcher, "_source_check", expensive)
    report = launcher.run_wave(
        wave[1],
        run_id="preflight",
        receipt_parent=tmp_path,
        held_lease=Lease(),
        execute=True,
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(wave[0], launcher.file_sha(wave[0])),
        _preflight_clock=lambda: clock[0],
    )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 0
    assert report["preflight_seconds"] >= 120


def test_preflight_budget_accumulates_across_cells_and_excludes_child_time(
    wave, tmp_path, monkeypatch
):
    clock = [0.0]
    original_check = launcher._source_check
    original_capture = launcher._capture

    def expensive(pins):
        original_check(pins)
        clock[0] += 35

    def child_time(*args, **kwargs):
        clock[0] += 10000  # paused process interval must not spend preflight
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(launcher, "_source_check", expensive)
    monkeypatch.setattr(launcher, "_capture", child_time)
    report = launcher.run_wave(
        wave[1],
        run_id="aggregate",
        receipt_parent=tmp_path,
        held_lease=Lease(),
        execute=True,
        oauth_token=TOKEN,
        runtime=launcher.FixtureRuntime(wave[0], launcher.file_sha(wave[0])),
        _preflight_clock=lambda: clock[0],
    )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 1
    assert report["preflight_seconds"] == 140
    assert read_receipt(report, 1)["status"] == "captured_unverified_calibration"


def test_lease_setup_time_counts_against_preflight_and_whole_wave(wave, tmp_path):
    with launcher.held_subscription_lease(
        "lease-time", deadline=time.time(), offline_root=tmp_path / "lease-time-pool"
    ) as lease:
        lease.request_started -= 121  # causal elapsed acquisition/setup, no real wait
        report = launcher.run_wave(
            wave[1],
            run_id="lease-time",
            receipt_parent=tmp_path,
            held_lease=lease,
            execute=True,
            oauth_token=TOKEN,
            runtime=launcher.FixtureRuntime(wave[0], launcher.file_sha(wave[0])),
        )
    assert report["status"] == "stopped" and report["cli_launch_count"] == 0
    assert report["preflight_seconds"] >= 120 and report["seconds"] >= 121


def test_kill_group_does_not_equate_reaped_leader_with_group_absence(monkeypatch):
    class LeaderGone:
        pid = 123456789
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(launcher.os, "killpg", lambda *args: None)
    monkeypatch.setattr(launcher, "_group_absent", lambda _pid: False)
    with pytest.raises(RuntimeError, match="absence is unproven"):
        launcher._kill_group(LeaderGone())


def test_pool_acquisition_is_fail_fast_even_with_later_caller_deadline(tmp_path, monkeypatch):
    import closer_resources

    observed = []
    original = closer_resources.pool_slot

    def capture(root, lane, bound, *, deadline, **kwargs):
        observed.append(deadline)
        return original(root, lane, bound, deadline=deadline, **kwargs)

    monkeypatch.setattr(closer_resources, "pool_slot", capture)
    before = time.time()
    with launcher.held_subscription_lease(
        "immediate", deadline=before + 1000, offline_root=tmp_path
    ):
        pass
    assert before <= observed[0] <= time.time()
    assert observed[0] < before + 1


def test_lease_active_flag_clears_even_when_final_journal_update_fails(tmp_path, monkeypatch):
    import closer_resources

    def failed(*args):
        raise OSError("inert lease journal failure")

    monkeypatch.setattr(closer_resources, "finite_pool_no_start", failed)
    lease = None
    with pytest.raises(OSError):
        with launcher.held_subscription_lease(
            "journal-close", deadline=time.time(), offline_root=tmp_path
        ) as lease:
            assert lease.active
    assert lease is not None and lease.active is False
