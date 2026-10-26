"""Explicit supervised first-wave launcher; default dry-run, never role authority.

Text plans are data, not authorization. A manually authorized caller must review
exact hashes, hold the EXISTING shared claude-subscription lease and explicitly
call run_wave(execute=True). The explicit lease factory uses the existing shared
pool; the runner never changes a ledger/registry, signs in, reads credentials
or retries a submission. Eight CLI
submissions do NOT establish eight HTTP attempts: native internal recovery and
credential renewal remain outside the observable attempt budget.

Pool integration seam: HeldPoolLease.check(pool_id, run_id) must raise unless the
caller's original shared pool lease remains held. held_subscription_lease wraps
the existing canonical root, SHA256(pool) lane and bound=1 pool_slot ownership.
A finite caller must instead preserve its original reservation and lifecycle
callbacks; this wrapper is for non-finite external calibration only. finite_pool_current_binding() alone does not prove a
lane. No fake capacity, quota, subscription debit or lease acquisition is added.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import closer_role_qualification as roles
from transport_qualification import digest, strict_final
from transport_qualification_corpus import build_corpus
from transport_saved_result import normalize_claude_result

PROFILE = "claude-sonnet-5-5-low"
MODEL = "claude-sonnet-5-5"
POOL = "claude-subscription"
NATIVE_SHA = "c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937"
CONTROLS = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "512",
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",
    "CLAUDE_CODE_DISABLE_REFUSAL_RETRY": "1",
}
LOCK = threading.Lock()
SOURCE_NAMES = (
    "transport_qualification_launcher.py",
    "adapters.py",
    "execution_profiles.py",
    "transport_saved_result.py",
    "transport_qualification.py",
    "transport_qualification_corpus.py",
    "closer_role_qualification.py",
    "closer_resources.py",
)
RESTRICTION = re.compile(
    r"authentication_error|invalid.{0,8}(?:oauth|token)|rate_limit_error|usage limit|"
    r"insufficient credits|credit balance|subscription required|payment required|"
    r"extra usage|pay.as.you.go|payg|billing restriction|api.key authentication",
    re.I,
)
SECRET = re.compile(
    r'sk-[A-Za-z0-9_-]{8,}|Bearer\s+[^\s"\\]+|'
    r"((?i:(?:api[_-]?key|access[_-]?token|oauth[_-]?token|password|secret))"
    r'["\s]*[:=]["\s]*)[^\s",}]+',
    re.I,
)


class HeldPoolLease(Protocol):
    def check(self, pool_id: str, run_id: str) -> None:
        """Raise unless the original externally acquired shared pool lease is held."""


@dataclass
class SubscriptionLease:
    run_id: str
    is_fixture: bool
    active: bool = False
    owner_pid: int = 0
    owner_thread: int = 0

    def check(self, pool_id: str, run_id: str) -> None:
        if (
            not self.active
            or self.owner_pid != os.getpid()
            or self.owner_thread != threading.get_ident()
            or pool_id != POOL
            or not run_id.startswith(self.run_id + "-")
        ):
            raise ValueError("original shared subscription lease is not held")


@contextlib.contextmanager
def held_subscription_lease(
    run_id: str, *, deadline: float, offline_root: Path | None = None
) -> Iterator[SubscriptionLease]:
    """Hold the SAME canonical pool/lane/bound as closer_dispatcher.

    deadline is wall-clock time, matching closer_resources.pool_slot. offline_root
    is only a fixture seam; its minted lease cannot admit NativeRuntime. No quota
    or charge is inferred. This non-finite wrapper is not a finite reservation.
    """
    import closer_resources

    root = (
        offline_root
        if offline_root is not None
        else Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex/handoff"))
        / "closer-profile-execution"
    )
    lane = hashlib.sha256(POOL.encode()).hexdigest()
    lease = SubscriptionLease(run_id, offline_root is not None)
    with closer_resources.pool_slot(root, lane, 1, deadline=deadline):
        lease.active, lease.owner_pid, lease.owner_thread = True, os.getpid(), threading.get_ident()
        try:
            yield lease
        finally:
            lease.active = False


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    """Bounded regular-file hash with identity checks before/after the read."""
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 256 * 1024 * 1024:
        raise ValueError("hash target is not a bounded regular file")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
        opened = os.fstat(stream.fileno())

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)

    if identity(before) != identity(opened) or identity(opened) != identity(path.stat()):
        raise ValueError("hash target changed during read")
    return h.hexdigest()


def fenced_argv(binary: str) -> list[str]:
    """Expected reviewed argv; actual native argv is built by adapters, not this copy."""
    return [
        binary,
        "-p",
        "--input-format",
        "text",
        "--safe-mode",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
        "--settings",
        '{"disableAllHooks":true}',
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model",
        MODEL,
        "--effort",
        "low",
    ]


@dataclass(frozen=True)
class ReviewPins:
    plan_sha256: str
    prompts_sha256: str
    files: dict[str, str]


@dataclass(frozen=True)
class PreparedWave:
    plan_text: str
    prompts_text: str
    pins: ReviewPins


def prepare_wave(plan_text: str, prompts_text: str, pins: ReviewPins) -> PreparedWave:
    """Validate reviewed input bytes only; performs no process or native gate call."""
    if (
        sha(plan_text.encode()) != pins.plan_sha256
        or sha(prompts_text.encode()) != pins.prompts_sha256
    ):
        raise ValueError("reviewed plan/prompt bytes differ")
    plan = strict_final(plan_text)
    # Prompt list is parsed through strict_final so nested duplicate keys/NaN refuse.
    prompts = strict_final('{"prompts":' + prompts_text + "}")["prompts"]
    candidate = plan["candidate"]
    profile = candidate["profile"]
    if (
        profile["profile_id"] != PROFILE
        or profile["agent"] != "claude"
        or profile["requested_model"] != MODEL
        or profile["reasoning_effort"] != "low"
        or profile["capacity_pool_ids"] != [POOL]
    ):
        raise ValueError("only exact first-wave Claude Sonnet low profile is supported")
    launch = candidate["launch"]
    if launch["argv"] != fenced_argv(launch["binary_path"]):
        raise ValueError("planned argv differs from reviewed data-only fence")
    if launch["config_sha256"] != digest(launch["config"]):
        raise ValueError("planned config hash differs")
    calls = plan["calls"]
    cells = calls["cells"]
    if (
        not isinstance(cells, list)
        or not 1 <= len(cells) <= 8
        or calls["concurrency"] != 1
        or type(calls["concurrency"]) is not int
        or calls["retries"] != 0
        or type(calls["retries"]) is not int
        or calls["fallbacks"] != 0
        or type(calls["fallbacks"]) is not int
    ):
        raise ValueError("wave must have 1..8 serial submissions with zero outer retries/fallbacks")
    if not isinstance(prompts, list) or len(prompts) != len(cells):
        raise ValueError("prompt/cell coverage differs")
    keys = []
    for cell, prompt in zip(cells, prompts, strict=True):
        key = (cell["role"], cell["case_id"])
        keys.append(key)
        if (
            key != (prompt["role"], prompt["case_id"])
            or not isinstance(prompt["prompt"], str)
            or not 0 < len(prompt["prompt"].encode()) <= 4096
            or sha(prompt["prompt"].encode()) != cell["prompt_sha256"]
            or cell["prompt_sha256"] != prompt["prompt_sha256"]
            or cell["prompt_bytes"] != len(prompt["prompt"].encode())
        ):
            raise ValueError("exact prompt binding differs")
        cases = build_corpus(cell["role"])["cases"]
        matching = [c for c in cases if c["case_id"] == cell["case_id"]]
        if len(matching) != 1 or cell["input_sha256"] != digest(matching[0]["input"]):
            raise ValueError("frozen corpus input binding differs")
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate role/case cell")
    if set(pins.files) != set(SOURCE_NAMES):
        raise ValueError("review pins must cover the complete declared launcher dependency set")
    if launch["adapter_sha256"] != pins.files["adapters.py"]:
        raise ValueError("planned adapter differs from reviewed source pin")
    return PreparedWave(plan_text, prompts_text, pins)


class NativeRuntime:
    is_fixture = False

    def command(self, launch: dict) -> list[str]:
        import adapters

        binary = adapters._data_only_claude_binary()  # intentionally uncached before EVERY spawn
        if binary != launch["binary_path"] or launch["binary_sha256"] != NATIVE_SHA:
            raise ValueError("native binary identity differs from reviewed launch")
        argv = adapters.build_command(
            "claude",
            "",
            mode="assess",
            profile=PROFILE,
            transport="offload",
            prompt_via_stdin=True,
            data_only=True,
        )
        if argv != launch["argv"]:
            raise ValueError("adapter-built argv differs from exact reviewed argv")
        return argv + ["--output-format", "json", "--max-turns", "1"]


@dataclass(frozen=True)
class FixtureRuntime:
    """Explicit inert test seam. Never resolves, wraps or delegates to native Claude."""

    binary: Path
    approved_sha256: str
    is_fixture = True

    def command(self, launch: dict) -> list[str]:
        data = self.binary.read_bytes()
        if (
            b"INERT_QUALIFICATION_FIXTURE" not in data
            or file_sha(self.binary) != self.approved_sha256
            or launch["binary_sha256"] != self.approved_sha256
            or launch["binary_path"] != str(self.binary.resolve())
        ):
            raise ValueError("inert fixture executable drift")
        return list(launch["argv"]) + ["--output-format", "json", "--max-turns", "1"]


def _source_check(pins: ReviewPins) -> None:
    root = Path(__file__).resolve().parent
    for name, expected in pins.files.items():
        if file_sha(root / name) != expected:
            raise ValueError("reviewed source/config/prompt code drift")


def private_environment(root: Path, oauth_token: str) -> dict[str, str]:
    """Build from scratch; ambient provider/key/proxy/callback variables never enter."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", **CONTROLS}
    for key, leaf in (
        ("HOME", "home"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_CONFIG_HOME", "config"),
        ("TMPDIR", "tmp"),
        ("CLAUDE_CONFIG_DIR", "claude-config"),
    ):
        directory = root / leaf
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    if not oauth_token or "\x00" in oauth_token:
        raise ValueError("explicit existing OAuth context is required")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def redact(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return SECRET.sub(
            lambda m: (m.group(1) or "") + "[REDACTED]",
            value.replace(token, "[REDACTED]") if token else value,
        )
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {redact(str(k), token): redact(v, token) for k, v in value.items()}
    return value


def _persist(root: Path, name: str, value: dict, token: str) -> None:
    data = json.dumps(redact(value, token), sort_keys=True, allow_nan=False).encode()
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=0.5)


def _capture(
    argv: list[str],
    prompt: bytes,
    cwd: Path,
    env: dict[str, str],
    *,
    deadline: float,
    cap: int,
    on_started: Callable[[int], None],
) -> dict:
    """Drain both pipes under one hard retained byte cap; stdin uses nonblocking IO."""
    start = time.monotonic()
    if deadline <= start:
        raise ValueError("submission deadline exhausted before spawn")
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
        shell=False,
        close_fds=True,
    )
    try:
        on_started(proc.pid)
    except BaseException:
        _kill_group(proc)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        raise
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    count = sent = 0
    problem = None
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, kind in (
                (proc.stdout, "stdout"),
                (proc.stderr, "stderr"),
                (proc.stdin, "stdin"),
            ):
                os.set_blocking(pipe.fileno(), False)
                selector.register(
                    pipe, selectors.EVENT_WRITE if kind == "stdin" else selectors.EVENT_READ, kind
                )
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    problem = "timeout"
                    break
                for key, _event in selector.select(min(0.05, remaining)):
                    pipe, kind = cast(Any, key.fileobj), key.data
                    if kind == "stdin":
                        try:
                            sent += os.write(pipe.fileno(), prompt[sent:])
                        except BrokenPipeError:
                            problem = "stdin_closed_early"
                            break
                        if sent == len(prompt):
                            selector.unregister(pipe)
                            pipe.close()
                        continue
                    chunk = os.read(pipe.fileno(), min(8192, cap - count + 1))
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    remaining_bytes = cap - count
                    captured[kind].extend(chunk[:remaining_bytes])
                    count += len(chunk)
                    if count > cap:
                        problem = "combined_output_cap"
                        break
                if problem:
                    break
        if problem is None:
            try:
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                problem = "timeout"
        # Kill any surviving descendants even after the leader has exited.
        if proc.poll() is not None:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                problem = problem or "surviving_process_group"
        if problem or proc.poll() is None:
            _kill_group(proc)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
    return {
        "stdout": bytes(captured["stdout"]),
        "stderr": bytes(captured["stderr"]),
        "exit_code": proc.returncode,
        "problem": problem,
        "stdin_bytes": sent,
        "combined_bytes_observed_at_least": count,
        "seconds": time.monotonic() - start,
    }


def _final_check(normalized: dict, cell: dict) -> dict:
    final_text = normalized["response"]["final"]
    if len(final_text.encode()) > 512 or normalized["response"]["num_turns"] != 1:
        raise ValueError("final size/turn count differs; resumed or multi-turn result unsupported")
    final = strict_final(final_text)
    case = next(c for c in build_corpus(cell["role"])["cases"] if c["case_id"] == cell["case_id"])
    if cell["role"] == roles.STRONG_CRITERION_ADJUDICATION:
        valid = (
            set(final) == {"criterion_id", "verdict", "reason"}
            and isinstance(final.get("reason"), str)
            and bool(final["reason"].strip())
            and final.get("verdict") == case["oracle_truth"]
        )
    else:
        needs = final.get("substantive_needs")
        valid = (
            set(final) == {"criterion_id", "classification", "acceptance", "substantive_needs"}
            and final.get("acceptance") is False
            and final.get("classification") == case["expected_semantic"]["classification"]
            and isinstance(needs, list)
            and all(isinstance(n, str) and bool(n.strip()) for n in needs)
            and (bool(needs) if case["oracle_truth"] == "UNKNOWN" else needs == [])
        )
    if not valid or final.get("criterion_id") != "C1":
        raise ValueError("final C1 role schema or frozen oracle failed")
    return final


def run_wave(
    prepared: PreparedWave,
    *,
    run_id: str,
    receipt_parent: Path,
    held_lease: HeldPoolLease | None = None,
    execute: bool = False,
    oauth_token: str | None = None,
    runtime: Any = None,
    cell_seconds: float = 45,
    wave_seconds: float = 480,
    output_cap: int = 65536,
) -> dict:
    """Explicit execution only; external manual caller owns authorization and lease.

    No plan/account field can authorize this call. No automated CLI execution flag
    exists. FixtureRuntime is an injected test-only path and is labelled offline.
    """
    prepare_wave(prepared.plan_text, prepared.prompts_text, prepared.pins)
    plan = strict_final(prepared.plan_text)
    cells = plan["calls"]["cells"]
    summary: dict[str, Any] = {
        "status": "dry_run",
        "activation": False,
        "role_qualification": False,
        "cli_launch_count": 0,
        "http_attempt_count": "UNKNOWN",
        "native_http_attempts_may_exceed_cli_launches": True,
        "cli_launch_count_source": "successful_Popen_returns",
        "outer_retries": 0,
        "fallbacks": 0,
        "cells_planned": len(cells),
        "receipts": [],
        "plan_sha256": prepared.pins.plan_sha256,
        "prompts_sha256": prepared.pins.prompts_sha256,
        "pool_id": POOL,
        "requested_effort": "low",
        "observed_effort": "UNKNOWN",
        "quota_balance": "UNKNOWN",
        "pool_charge": "UNKNOWN",
        "independent_review_required": True,
    }
    if not execute:
        return summary
    runtime = runtime or NativeRuntime()
    if not runtime.is_fixture and (
        not isinstance(held_lease, SubscriptionLease)
        or held_lease.is_fixture
        or cell_seconds != 45
        or wave_seconds != 480
        or output_cap != 65536
    ):
        raise ValueError(
            "native execution requires original subscription lease and fixed reviewed limits"
        )
    if (
        not isinstance(run_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", run_id)
        or held_lease is None
        or not 0.1 <= cell_seconds <= 45
        or not 0.1 <= wave_seconds <= 480
        or type(output_cap) is not int
        or not 1024 <= output_cap <= 65536
    ):
        raise ValueError("explicit run binding, held lease and bounded limits required")
    if not LOCK.acquire(blocking=False):
        raise ValueError("another supervised wave is active")
    token = (
        oauth_token if oauth_token is not None else os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    )
    root = None
    started = time.monotonic()
    deadline = started + wave_seconds
    try:
        # Exclusive durable wave identity prevents replay after crashes/uncertain starts.
        root = receipt_parent / run_id
        root.mkdir(mode=0o700)
        summary.update(status="running", run_id=run_id, receipt_directory=str(root))
        summary["origin"] = "offline_fixture" if runtime.is_fixture else "supervised_native_capture"
        prompts = strict_final('{"prompts":' + prepared.prompts_text + "}")["prompts"]
        sessions = set()
        for index, (cell, prompt) in enumerate(zip(cells, prompts, strict=True)):
            cell_run = f"{run_id}-{index + 1}"
            record: dict[str, Any] = {
                "run_id": cell_run,
                "pool_id": POOL,
                "profile_id": PROFILE,
                "role": cell["role"],
                "case_id": cell["case_id"],
                "prompt_sha256": cell["prompt_sha256"],
                "execution_binding_sha256": cell.get("execution_binding_sha256"),
                "activation": False,
                "http_attempt_count": "UNKNOWN",
                "external_lease_check": "caller_supplied_held_lease_interface",
                "status": "not_launched",
            }
            stage = "prelaunch_hash_lease_argv"
            try:
                if time.monotonic() + 0.5 >= deadline:
                    raise ValueError("whole wave deadline exhausted")
                prepare_wave(prepared.plan_text, prepared.prompts_text, prepared.pins)
                _source_check(prepared.pins)
                held_lease.check(POOL, cell_run)
                argv = runtime.command(plan["candidate"]["launch"])
                if argv != plan["candidate"]["launch"]["argv"] + [
                    "--output-format",
                    "json",
                    "--max-turns",
                    "1",
                ]:
                    raise ValueError("runtime argv differs")
                private = root / f"cell-{index + 1}"
                private.mkdir(mode=0o700)
                cwd = private / "cwd"
                cwd.mkdir(mode=0o700)
                env = private_environment(private, token)
                record.update(
                    argv=argv,
                    config_sha256=plan["candidate"]["launch"]["config_sha256"],
                    source_pins=prepared.pins.files,
                    binary_sha256=plan["candidate"]["launch"]["binary_sha256"],
                    isolated_home=True,
                    isolated_cwd=True,
                    environment_allowlisted=True,
                    safe_controls=CONTROLS,
                    no_resume_flags=True,
                )
                # Recheck immediately before spawn, after private runtime setup.
                _source_check(prepared.pins)
                held_lease.check(POOL, cell_run)
                if runtime.command(plan["candidate"]["launch"]) != argv:
                    raise ValueError("prelaunch binary/argv drift")
                record["status"] = "starting_outcome_uncertain_until_started_receipt"
                _persist(root, f"attempt-{index + 1}-starting.json", record, token)

                def on_started(pid: int) -> None:
                    summary["cli_launch_count"] += 1
                    record.update(status="started_outcome_pending", process_group=pid)
                    _persist(root, f"attempt-{index + 1}-started.json", record, token)

                stage = "process_supervision"
                captured = _capture(
                    argv,
                    prompt["prompt"].encode(),
                    cwd,
                    env,
                    deadline=min(deadline - 0.5, time.monotonic() + cell_seconds - 0.05),
                    cap=output_cap,
                    on_started=on_started,
                )
                stdout = captured.pop("stdout")
                stderr = captured.pop("stderr")
                record.update(
                    captured,
                    stdout_redacted=stdout.decode("utf-8", errors="replace"),
                    stderr_redacted=stderr.decode("utf-8", errors="replace"),
                    stdout_sha256=sha(stdout),
                    stderr_sha256=sha(stderr),
                )
                if (
                    captured["problem"]
                    or captured["exit_code"] != 0
                    or captured["stdin_bytes"] != len(prompt["prompt"].encode())
                ):
                    raise ValueError("process supervision failed")
                stage = "account_restriction_or_paid_path"
                text = stdout.decode("utf-8", errors="strict")
                if RESTRICTION.search(text + "\n" + stderr.decode("utf-8", errors="replace")):
                    raise ValueError("account restriction or unexpected paid path")
                stage = "native_result_identity_usage_completion"
                normalized = normalize_claude_result(
                    text,
                    run_id=cell_run,
                    pool_id=POOL,
                    requested_model=MODEL,
                    requested_effort="low",
                )
                stage = "tool_activity_and_no_resumed_history"
                native = strict_final(text)
                model_usage = native["modelUsage"][MODEL]
                if (
                    native.get("permission_denials", []) != []
                    or model_usage.get("webSearchRequests", 0) != 0
                    or native.get("subagent_stats", {}).get("spawned", 0) != 0
                    or native.get("usage", {})
                    .get("server_tool_use", {})
                    .get("web_search_requests", 0)
                    != 0
                ):
                    raise ValueError("unexpected tool activity in fenced capture")
                if any(k in native for k in ("resume", "resumed", "resumed_session_id")):
                    raise ValueError("resumed history marker unsupported")
                if normalized["native_session_id"] in sessions:
                    raise ValueError("native session reused across sterile cells")
                sessions.add(normalized["native_session_id"])
                stage = "final_C1_role_schema_frozen_oracle"
                record.update(
                    normalized=normalized,
                    final=_final_check(normalized, cell),
                    status="captured_unverified_calibration",
                )
                held_lease.check(POOL, cell_run)
            except Exception as exc:
                record.update(
                    status="stopped", failure_type=type(exc).__name__, failure_stage=stage
                )
                # Do not persist exception text: arbitrary callbacks can embed secrets.
                summary["status"] = "stopped"
            name = f"cell-{index + 1}.json"
            _persist(root, name, record, token)
            summary["receipts"].append(name)
            if summary["status"] == "stopped":
                break
        else:
            summary["status"] = "completed_unverified_calibration"
        summary["seconds"] = time.monotonic() - started
        _persist(root, "summary.json", summary, token)
        return redact(summary, token)
    finally:
        LOCK.release()


def selftest() -> dict:
    """Only inert fixture processes. Fixtures are package test inputs, not Claude."""
    fixture = (
        Path(__file__).resolve().parent.parent
        / "tests/fixtures/transport_qualification_launcher/inert_child.py"
    )
    with tempfile.TemporaryDirectory(prefix="launcher-inert-selftest-") as temp:
        root = Path(temp)
        binary = root / "success"
        binary.write_bytes(f"#!{sys.executable}\n".encode() + fixture.read_bytes())
        binary.chmod(0o700)
        prepared = synthetic_wave(binary)

        class Lease:
            def check(self, pool_id, run_id):
                assert pool_id == POOL and run_id

        report = run_wave(
            prepared,
            run_id="synthetic-selftest",
            receipt_parent=root,
            held_lease=Lease(),
            execute=True,
            oauth_token="synthetic-not-a-real-token",
            runtime=FixtureRuntime(binary, file_sha(binary)),
        )
        checks = {
            "inert_eight_serial_submissions": report["cli_launch_count"] == 8,
            "completed_calibration_only": report["status"] == "completed_unverified_calibration",
            "no_activation": report["activation"] is False,
            "no_http_attempt_inference": report["http_attempt_count"] == "UNKNOWN",
            "no_outer_retry": report["outer_retries"] == 0,
        }
        if not all(checks.values()):
            raise AssertionError(checks)
        return {
            "synthetic_pipeline_controls_only": True,
            "checked_assertions": checks,
            "activation": False,
        }


def synthetic_wave(binary: Path) -> PreparedWave:
    """Selftest-only reviewed synthetic input factory; grants no native authority."""
    cases = [
        (r, c)
        for r in (roles.BOUNDED_CLASSIFICATION, roles.STRONG_CRITERION_ADJUDICATION)
        for c in build_corpus(r)["cases"][:4]
    ]
    prompts = [
        {
            "role": r,
            "case_id": c["case_id"],
            "prompt": json.dumps(
                {"role": r, "case_id": c["case_id"], "expected": c["expected_semantic"]}
            ),
        }
        for r, c in cases
    ]
    for p in prompts:
        p["prompt_sha256"] = sha(p["prompt"].encode())
    source = Path(__file__).resolve().parent
    pins = {name: file_sha(source / name) for name in SOURCE_NAMES}
    config = {"tools": [], "mcpServers": {}, "settings": {"disableAllHooks": True}}
    plan = {
        "candidate": {
            "profile": {
                "profile_id": PROFILE,
                "agent": "claude",
                "requested_model": MODEL,
                "reasoning_effort": "low",
                "capacity_pool_ids": [POOL],
            },
            "launch": {
                "binary_path": str(binary.resolve()),
                "binary_sha256": file_sha(binary),
                "adapter_sha256": pins["adapters.py"],
                "argv": fenced_argv(str(binary.resolve())),
                "config": config,
                "config_sha256": digest(config),
            },
        },
        "calls": {
            "concurrency": 1,
            "retries": 0,
            "fallbacks": 0,
            "cells": [
                {
                    "role": r,
                    "case_id": c["case_id"],
                    "input_sha256": digest(c["input"]),
                    "prompt_sha256": p["prompt_sha256"],
                    "prompt_bytes": len(p["prompt"].encode()),
                }
                for (r, c), p in zip(cases, prompts, strict=True)
            ],
        },
    }
    plan_text, prompts_text = json.dumps(plan), json.dumps(prompts)
    return prepare_wave(
        plan_text,
        prompts_text,
        ReviewPins(sha(plan_text.encode()), sha(prompts_text.encode()), pins),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            (
                selftest()
                if args.selftest
                else {"status": "dry_run", "cli_launch_count": 0, "activation": False}
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
