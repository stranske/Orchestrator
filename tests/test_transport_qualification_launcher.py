"""Inert supervised-launch controls. No native CLI is resolved or invoked."""

import json
import stat
import sys
import time
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

    def failed(root, name, value, token):
        if name.endswith("-started.json"):
            raise OSError("synthetic fsync failure")
        return original(root, name, value, token)

    monkeypatch.setattr(launcher, "_persist", failed)
    report = run(wave, tmp_path, mode="timeout")
    assert report["status"] == "stopped" and report["cli_launch_count"] == 1


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
    assert killed == [read_receipt(report)["process_group"]]


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
