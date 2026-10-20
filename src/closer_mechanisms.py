"""Explicit execution capabilities. CLI evidence never attests a Desktop scheduler.

The qualification child has no queue, provider, GitHub or claim API. The trusted
supervisor alone observes OS processes and signs its no-effect lifecycle record.
The local signature detects accidental/caller JSON substitution, not a privilege
boundary against a malicious provider running as the same OS user. The host and
reviewed supervisor are trusted; no secret or signing-key path enters the prompt.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import shlex
import signal
import stat
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

DESKTOP = "desktop-scheduler/v1"
NATIVE = "native-handoff/v1"
CLI = "cli-offload/v1"
CONTRACT = "closer-mechanism/v1"
POLICY = "cli-configured-policy/v1"
OBSERVATION = "cli-supervised-lifecycle/v1"
MECHANISMS = (DESKTOP, NATIVE, CLI)
AUTOMATIONS = Path.home() / ".codex" / "automations"
# Canonical supervising lane ownership is separate from the offload provider.
# Only this reviewed owner mapping can select policy TOMLs; no provider inference.
WORKERS = {("closer", "codex"): "imi-merge-verify-closer"}
CONTROLLER = "reviewed-repo-automation-controller"
PHASES = ("startup", "handoff", "crash_restart", "result_recording")
PIN_KEYS = (
    "package_digest",
    "candidate_sha",
    "command_digest",
    "runtime_digest",
    "state_digest",
    "state_versions",
)


class MechanismError(RuntimeError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise MechanismError("observation file unsafe or too large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise MechanismError("observation is not an object")
    return value


def _write(path: Path, value: dict) -> None:
    encoded = json.dumps(value, sort_keys=True).encode()
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def mechanism(manifest: dict, requested: str | None = None) -> str:
    contract = manifest.get("mechanism_contract")
    if contract is not None and not isinstance(contract, dict):
        raise MechanismError("invalid execution mechanism contract")
    selected = contract.get("mechanism") if isinstance(contract, dict) else DESKTOP
    if selected not in MECHANISMS or (contract is not None and contract.get("schema") != CONTRACT):
        raise MechanismError("unknown execution mechanism contract")
    if requested is not None and requested != selected:
        raise MechanismError("execution mechanism differs from pinned host contract")
    policy = manifest.get("policy") or {}
    if selected == CLI:
        if not isinstance(contract, dict):
            raise MechanismError("CLI mechanism contract missing")
        if (
            not isinstance(contract.get("agent"), str)
            or not contract["agent"]
            or not isinstance(contract.get("profile_id"), str)
            or not contract["profile_id"]
            or contract["agent"] != policy.get("agent")
        ):
            raise MechanismError("CLI qualification must pin exactly one agent/profile")
        if policy.get("schema") != POLICY or policy.get("mechanism") != CLI:
            raise MechanismError("CLI mechanism lacks its explicit configured policy")
        _policy_worker(policy)
        from closer_agent_launchers import validate_profile

        if validate_profile(contract["agent"], contract["profile_id"]):
            raise MechanismError("CLI qualification profile differs from approved provider")
        if "closer_mechanisms.py" not in (manifest.get("modules") or {}):
            raise MechanismError("mechanism reader is outside pinned package closure")
    elif policy.get("schema") == POLICY:
        raise MechanismError("CLI policy cannot authorize another execution mechanism")
    return selected


def _policy_worker(policy: dict) -> str:
    from closer_agent_launchers import APPROVED_AGENTS

    lane, owner, agent = policy.get("lane"), policy.get("worker_owner"), policy.get("agent")
    if not isinstance(lane, str) or not isinstance(owner, str):
        raise MechanismError("configured supervising lane and worker_owner missing")
    if not isinstance(agent, str) or agent not in APPROVED_AGENTS:
        raise MechanismError("configured offload provider is outside approved pool")
    worker = WORKERS.get((lane, owner))
    if worker is None:
        raise MechanismError("configured supervising lane/owner has no canonical reader")
    return worker


def configured_policy_problem(policy: dict) -> str | None:
    """Read only the reviewed lane/controller TOMLs, now; never app readback."""
    try:
        if policy.get("schema") != POLICY or policy.get("mechanism") != CLI:
            raise MechanismError("wrong policy mechanism")
        worker = _policy_worker(policy)
        expected = [AUTOMATIONS / name / "automation.toml" for name in (CONTROLLER, worker)]
        pins = policy.get("toml_sha256")
        if not isinstance(pins, dict) or set(pins) != {str(p) for p in expected}:
            raise MechanismError(
                "controller/selected lane policy set differs from canonical allowlist"
            )
        for path in expected:
            # resolve equality rejects aliases and symlinked ancestors as well as the file.
            if path.resolve() != path or path.is_symlink():
                raise MechanismError("canonical policy retargeted")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != pins[str(path)]:
                raise MechanismError("configured policy changed since host pin")
            if tomllib.loads(raw.decode()).get("status") != "ACTIVE":
                raise MechanismError("controller or selected worker paused or unknown")
    except (OSError, ValueError, TypeError, MechanismError) as exc:
        return f"CLI configured policy held: {exc}"
    return None


def binding(manifest: dict) -> dict:
    mechanism(manifest, CLI)
    return {
        "mechanism": CLI,
        "mechanism_contract_digest": digest(manifest["mechanism_contract"]),
        "policy_digest": digest(manifest["policy"]),
        **{key: manifest.get(key) for key in PIN_KEYS},
    }


def _root(manifest: dict) -> Path:
    name = manifest["mechanism_contract"].get("observation_root")
    if not isinstance(name, str) or not Path(name).is_absolute():
        raise MechanismError("CLI supervisor observation root missing")
    root = Path(name)
    if root.resolve() != root or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise MechanismError("CLI supervisor root must be canonical private directory")
    return root


def _key(root: Path, *, create: bool = False) -> bytes:
    path = root / "supervisor.key"
    if create and not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise MechanismError("supervisor key unsafe")
    key = path.read_bytes()
    if len(key) != 32:
        raise MechanismError("supervisor key invalid")
    return key


def _signature(record: dict, key: bytes) -> str:
    return hmac.new(key, json.dumps(record, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def qualification_problems(manifest: dict) -> list[str]:
    """Fixed pinned observer; unsigned caller receipts and other mechanisms hold."""
    try:
        selected = mechanism(manifest)
        if selected != CLI:
            return [f"{selected} has no CLI qualification authority"]
        root = _root(manifest)
        if _read(root / "active.json").get("status") != "terminal":
            raise MechanismError("CLI qualification still has an unresolved submission")
        signed = _read(root / "qualification.json")
        record = signed["record"]
        if not hmac.compare_digest(signed["signature"], _signature(record, _key(root))):
            raise MechanismError("supervisor observation signature differs")
        if record.get("schema") != OBSERVATION or record.get("binding") != binding(manifest):
            raise MechanismError("supervised lifecycle belongs to another mechanism/package/policy")
        if (
            record.get("agent") != manifest["mechanism_contract"]["agent"]
            or record.get("profile_id") != manifest["mechanism_contract"]["profile_id"]
        ):
            raise MechanismError("CLI observation agent/profile differs from selected execution")
        if record.get("passed") is not True or record.get("desktop_qualified") is not False:
            raise MechanismError("supervisor has not qualified this CLI lifecycle")
        directory = root / record["nonce"]
        if directory.resolve().parent != root or directory.is_symlink():
            raise MechanismError("qualification artifacts escape supervised root")
        for phase in PHASES:
            ref = record["phases"][phase]
            path = directory / ref["file"]
            if path.parent != directory or path.is_symlink():
                raise MechanismError("phase evidence path unsafe")
            if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                raise MechanismError("supervised phase evidence changed")
        result = _read(directory / "result.json")
        if result.get("binding") != record["binding"] or result.get("nonce") != record["nonce"]:
            raise MechanismError("durable CLI result not bound to observed run")
    except (OSError, ValueError, KeyError, TypeError, MechanismError) as exc:
        return [f"CLI qualification held: {exc}"]
    return []


def _remaining(deadline: float | None) -> float:
    remaining = 5.0 if deadline is None else min(5.0, deadline - time.monotonic())
    if remaining <= 0:
        raise MechanismError("original observation budget exhausted")
    return remaining


def _identity(pid: int, argv: list[str], *, deadline: float | None = None) -> dict:
    if type(pid) is not int or pid <= 1:
        raise MechanismError("invalid supervised child PID")
    done = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        timeout=_remaining(deadline),
        check=True,
    )
    text = done.stdout.strip()
    # ps lstart has five whitespace-separated fields on supported macOS/Linux.
    parts = text.split(None, 5)
    if len(parts) != 6 or parts[5] != " ".join(argv):
        raise MechanismError("OS child command does not match exact no-effect launcher")
    return {"pid": pid, "started": " ".join(parts[:5]), "argv": argv, "source": "os-ps"}


def _lineage(pid: int, *, deadline: float | None = None) -> list[dict]:
    """Capture OS ancestry back to this supervisor; no session-store discovery."""
    rows = []
    for _ in range(32):
        done = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "ppid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=_remaining(deadline),
            check=True,
        )
        parts = done.stdout.strip().split(None, 6)
        if len(parts) != 7:
            raise MechanismError("OS provider ancestry unavailable")
        parent = int(parts[0])
        rows.append(
            {
                "pid": pid,
                "parent_pid": parent,
                "started": " ".join(parts[1:6]),
                "command_sha256": hashlib.sha256(parts[6].encode()).hexdigest(),
                "command": parts[6],
            }
        )
        if parent == os.getpid():
            return rows
        if parent <= 1 or parent == pid:
            break
        pid = parent
    raise MechanismError("launcher is not descended from this supervised submission")


def _wait_terminal(identity: dict, deadline: float) -> None:
    while time.monotonic() < deadline:
        done = subprocess.run(
            ["ps", "-p", str(identity["pid"]), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=_remaining(deadline),
        )
        if done.returncode == 1 or done.stdout.strip().startswith("Z"):
            return
        if done.returncode != 0:
            raise MechanismError("supervised child terminal observation unavailable")
        time.sleep(0.02)
    raise MechanismError("supervised child is not terminal; no next provider submission")


def _wait(path: Path, deadline: float) -> dict:
    while time.monotonic() < deadline:
        if path.is_file():
            return _read(path)
        time.sleep(0.02)
    raise MechanismError("selected CLI did not produce the required launcher observation")


def qualification_child(manifest: dict, request_path: str, role: str) -> int:
    """No-effect production entry: only private qualification files, no queue APIs."""
    if configured_policy_problem(manifest.get("policy") or {}):
        return 78
    root = _root(manifest)
    request_file = Path(request_path)
    if request_file.resolve().parent.parent != root or request_file.is_symlink():
        raise MechanismError("qualification request outside pinned observer root")
    request = _read(request_file)
    if (
        request.get("binding") != binding(manifest)
        or request.get("nonce") != request_file.parent.name
    ):
        raise MechanismError("qualification request binding differs")
    hard, action = request.get("original_deadline"), request.get("action_deadline")
    if (
        not isinstance(hard, (int, float))
        or isinstance(hard, bool)
        or not isinstance(action, (int, float))
        or isinstance(action, bool)
    ):
        raise MechanismError("qualification child lacks original bounded deadlines")
    if (
        not math.isfinite(hard)
        or not math.isfinite(action)
        or hard - action != 5
        or hard <= time.time()
    ):
        raise MechanismError("qualification child lacks original bounded deadlines")
    deadline = time.monotonic() + max(0, action - time.time())
    directory = request_file.parent
    ready = {"pid": os.getpid(), "nonce": request["nonce"], "binding": request["binding"]}
    _write(directory / f"{role}-ready.json", ready)
    if role == "first":
        handoff = _wait(directory / "handoff.json", deadline)
        if handoff.get("nonce") != request["nonce"]:
            return 78
        _write(directory / "journal.json", {**ready, "token": handoff["token"]})
        time.sleep(max(0, deadline - time.monotonic()))  # Never extend the original action budget.
        return 78
    if role != "restart":
        return 78
    ack = _wait(directory / "restart-observed.json", deadline)
    if ack.get("nonce") != request["nonce"]:
        return 78
    journal = _read(directory / "journal.json")
    _write(
        directory / "result.json",
        {**ready, "token": journal["token"], "resumed_from_pid": journal["pid"]},
    )
    return 0


def _supervise_cli_locked(
    manifest_path: str,
    agent: str,
    profile_id: str,
    *,
    budget_seconds: int,
    timeout: int = 60,
    offload=None,
) -> dict:
    """Two bounded real CLI submissions; transport success alone never qualifies.

    Only the exact observed no-effect launcher is signalled. Unknown provider or
    child identity outcomes hold; there is no fallback or resubmission of them.
    """
    from closer_agent_launchers import offload_profiles, profile_offload_mode, validate_profile
    from closer_campaign import load_manifest

    manifest = load_manifest(manifest_path)
    mechanism(manifest, CLI)
    problem = configured_policy_problem(manifest["policy"])
    if (
        problem
        or validate_profile(agent, profile_id)
        or agent != manifest["mechanism_contract"]["agent"]
        or profile_id != manifest["mechanism_contract"]["profile_id"]
    ):
        raise MechanismError(problem or "selected CLI/profile differs from configured lane")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise MechanismError("qualification provider budget must be 1..120 seconds per phase")
    if type(budget_seconds) is not int or not 6 <= budget_seconds <= 2 * timeout + 5:
        raise MechanismError("explicit overall budget must reserve five cleanup seconds")
    original_deadline = time.time() + budget_seconds
    original_monotonic_deadline = time.monotonic() + budget_seconds
    action_monotonic_deadline = original_monotonic_deadline - 5
    deadlines = {
        "original_deadline": original_deadline,
        "action_deadline": original_deadline - 5,
        "cleanup_reserve_seconds": 5,
    }
    root = _root(manifest)
    key = _key(root, create=True)
    active_path = root / "active.json"
    if active_path.exists() and _read(active_path).get("status") != "terminal":
        raise MechanismError("earlier CLI qualification outcome needs reconciliation")
    nonce = secrets.token_hex(24)
    directory = root / nonce
    directory.mkdir(mode=0o700)
    request = {
        "binding": binding(manifest),
        **deadlines,
        "nonce": nonce,
        "agent": agent,
        "profile_id": profile_id,
    }
    _write(directory / "request.json", request)
    _write(active_path, {"nonce": nonce, "status": "running", "binding": request["binding"]})
    if offload is None:
        from dispatcher import offload
    profile = next(p for p in offload_profiles(agent) if p["profile_id"] == profile_id)
    processes = []
    transports = []
    outcome: dict[str, Any] = {}
    thread = None
    known_child = None
    child_signal_attempted = False
    cleanup = []
    cleanup_denied = False
    try:
        for role in ("first", "restart"):
            phase_timeout = min(timeout, int(action_monotonic_deadline - time.monotonic()))
            if phase_timeout < 1:
                raise MechanismError("original qualification budget exhausted")
            problem = configured_policy_problem(manifest["policy"])
            if problem:
                raise MechanismError(problem)
            argv = [
                manifest["runtime"]["python"],
                manifest["launcher"]["path"],
                "qualification-child",
                "--manifest",
                str(Path(manifest_path).resolve()),
                "--mechanism",
                CLI,
                "--request",
                str(directory / "request.json"),
                "--role",
                role,
            ]
            outcome = {}
            known_child = None
            child_signal_attempted = False
            starts, completions, spawn_events = [], [], []
            _write(
                directory / f"{role}-intent.json",
                {
                    **request,
                    "argv": argv,
                    "original_deadline": original_deadline,
                    "timeout_seconds": phase_timeout,
                    "role": role,
                },
            )

            def started(event):
                starts.append(dict(event))
                _write(directory / f"{role}-provider-started.json", dict(event))

            def completed(event):
                completions.append(dict(event))
                _write(directory / f"{role}-provider-completed.json", dict(event))

            def process_started(event):
                if (
                    len(starts) != 1
                    or event.get("run_id") != starts[0].get("run_id")
                    or event.get("agent") != agent
                    or event.get("profile_id") != profile_id
                    or event.get("source") != "trusted-popen-transport/v1"
                ):
                    raise MechanismError("spawn observation differs from selected CLI invocation")
                spawn_events.append(event)  # Trusted in-memory veto channel, never receipt input.
                _write(directory / f"{role}-provider-spawn.json", dict(event))

            def submit():
                try:
                    outcome["result"] = offload(
                        agent,
                        "Invoke exactly this no-effect qualification command using your terminal tool. "
                        "Do not edit files or run other commands. The first-role child is deliberately "
                        "crashed by its supervisor after handoff; report that expected completion and stop. Exact commands: "
                        + shlex.join(argv),
                        cwd=str(directory),
                        mode=profile_offload_mode(profile),
                        timeout=phase_timeout,
                        profile_id=profile_id,
                        task_type="closer_cli_qualification",
                        max_network_retries=0,
                        attempt_started_callback=started,
                        attempt_receipt_callback=completed,
                        process_started_callback=process_started,
                    )
                    _write(directory / f"{role}-provider-returned.json", dict(outcome["result"]))
                except Exception as exc:
                    outcome["error"] = f"{type(exc).__name__}: {exc}"
                    _write(directory / f"{role}-provider-error.json", {"error": outcome["error"]})

            thread = threading.Thread(target=submit, daemon=True)
            thread.start()
            deadline = min(time.monotonic() + phase_timeout, action_monotonic_deadline)
            ready = _wait(directory / f"{role}-ready.json", deadline)
            if ready.get("nonce") != nonce or ready.get("binding") != request["binding"]:
                raise MechanismError("launcher handshake differs from supervised request")
            identity = _identity(ready["pid"], argv, deadline=deadline)
            known_child = dict(identity)
            ancestry = _lineage(ready["pid"], deadline=deadline)
            transport = _wait(directory / f"{role}-provider-spawn.json", deadline)
            transport_rows = [
                row
                for row in ancestry
                if row["pid"] == transport.get("pid")
                and row["started"] == " ".join(transport.get("os_started", "").split())
            ]
            expected_provider = transport.get("provider_argv")
            if not isinstance(expected_provider, list) or not expected_provider:
                raise MechanismError("trusted transport lacks exact selected provider argv")
            provider_rows = [
                row for row in ancestry if row["command"] == " ".join(expected_provider)
            ]
            if len(transport_rows) != 1 or len(provider_rows) != 1:
                raise MechanismError(
                    "launcher lacks the actual selected CLI ancestor/transport birth"
                )
            if ancestry.index(provider_rows[0]) > ancestry.index(transport_rows[0]):
                raise MechanismError("selected provider is outside its observed transport lineage")
            identity["transport_pid"] = transport["pid"]
            identity["provider_pid"] = provider_rows[0]["pid"]
            identity["provider_started"] = provider_rows[0]["started"]
            identity["provider_argv_sha256"] = digest(expected_provider)
            ancestry = [{k: v for k, v in row.items() if k != "command"} for row in ancestry]
            if len(starts) != 1 or not starts[0].get("run_id") or starts[0].get("agent") != agent:
                raise MechanismError("launcher has no bound actual CLI submission")
            identity["provider_run_id"] = starts[0]["run_id"]
            identity["os_ancestry"] = ancestry
            _write(directory / f"{role}-os-observation.json", identity)
            processes.append(identity)
            if role == "first":
                _write(directory / "handoff.json", {"nonce": nonce, "token": secrets.token_hex(24)})
                journal = _wait(directory / "journal.json", deadline)
                if journal.get("pid") != identity["pid"] or journal.get("nonce") != nonce:
                    raise MechanismError("handoff did not bind observed launcher")
                if _identity(identity["pid"], argv, deadline=deadline) != {
                    k: identity[k] for k in ("pid", "started", "argv", "source")
                }:
                    raise MechanismError("child identity changed before controlled crash")
                child_signal_attempted = True
                try:
                    os.kill(identity["pid"], signal.SIGKILL)
                except OSError as signal_exc:
                    cleanup_denied = isinstance(signal_exc, PermissionError)
                    if cleanup_denied:
                        for spawn in spawn_events:
                            spawn["cleanup_veto"] = {
                                "type": type(signal_exc).__name__,
                                "errno": signal_exc.errno,
                                "reason": str(signal_exc),
                                "denied_child_pid": identity["pid"],
                            }
                    cleanup.append(
                        {
                            "target": "qualification-child",
                            "pid": identity["pid"],
                            "phase": "controlled-crash",
                            "error": {
                                "type": type(signal_exc).__name__,
                                "errno": signal_exc.errno,
                                "reason": str(signal_exc),
                            },
                        }
                    )
                    raise
            else:
                _write(directory / "restart-observed.json", {"nonce": nonce})
                result = _wait(directory / "result.json", deadline)
                if (
                    result.get("resumed_from_pid") != processes[0]["pid"]
                    or result.get("pid") != identity["pid"]
                    or result.get("nonce") != nonce
                    or result.get("binding") != request["binding"]
                    or result.get("token") != journal.get("token")
                ):
                    raise MechanismError("restart durable result has wrong process lineage")
            _wait_terminal(identity, deadline)
            _write(
                directory / f"{role}-terminal.json",
                {"pid": identity["pid"], "nonce": nonce, "observed_terminal": True},
            )
            thread.join(max(0, deadline - time.monotonic()))
            actual = outcome.get("result") or {}
            transports.append(actual)
            if (
                thread.is_alive()
                or outcome.get("error")
                or actual.get("outcome_uncertain")
                or actual.get("retry_safe") is False
                or actual.get("completion_persistence") is not None
                or len(completions) != 1
                or completions[0].get("run_id") != starts[0]["run_id"]
                or actual.get("run_id") != starts[0]["run_id"]
                or type(completions[0].get("raw_exit", completions[0].get("exit"))) is not int
                or completions[0].get("raw_exit", completions[0].get("exit")) != 0
                or actual.get("exit") != 0
                or actual.get("disabled") is True
            ):
                raise MechanismError("CLI completion unknown; reconcile before any new submission")
        phases = {
            name: {
                "file": file,
                "sha256": hashlib.sha256((directory / file).read_bytes()).hexdigest(),
            }
            for name, file in zip(
                PHASES, ["first-ready.json", "journal.json", "restart-ready.json", "result.json"]
            )
        }
        record = {
            "schema": OBSERVATION,
            **deadlines,
            "binding": request["binding"],
            "nonce": nonce,
            "agent": agent,
            "profile_id": profile_id,
            "processes": processes,
            "phases": phases,
            "passed": True,
            "desktop_qualified": False,
            "queue_action_accepted": False,
        }
        _write(
            root / "qualification.json", {"record": record, "signature": _signature(record, key)}
        )
        _write(directory / "transport-results.json", {"attempts": transports})
        _write(active_path, {"nonce": nonce, "status": "terminal"})
        return record
    except Exception as exc:
        # Cleanup is inside the immutable original budget, never a fresh attempt.
        # Only the exact observed no-effect child and its private transport group
        # can be signalled; unknown identity or a denial remains UNKNOWN.
        if known_child is not None and not child_signal_attempted:
            child_signal_attempted = True
            event = {"target": "qualification-child", "pid": known_child["pid"]}
            try:
                if time.monotonic() >= original_monotonic_deadline:
                    raise MechanismError("original hard budget exhausted before cleanup")
                if (
                    _identity(
                        known_child["pid"],
                        known_child["argv"],
                        deadline=original_monotonic_deadline,
                    )
                    != known_child
                ):
                    raise MechanismError("cleanup child identity changed")
                os.kill(known_child["pid"], signal.SIGKILL)
                event["signal_sent"] = True
            except Exception as cleanup_exc:
                cleanup_denied = isinstance(cleanup_exc, PermissionError)
                if cleanup_denied:
                    for spawn in spawn_events:
                        spawn["cleanup_veto"] = {
                            "type": type(cleanup_exc).__name__,
                            "errno": getattr(cleanup_exc, "errno", None),
                            "reason": str(cleanup_exc),
                            "denied_child_pid": known_child["pid"],
                        }
                event["error"] = {
                    "type": type(cleanup_exc).__name__,
                    "errno": getattr(cleanup_exc, "errno", None),
                    "reason": str(cleanup_exc),
                }
            cleanup.append(event)
        if thread is not None:
            thread.join(max(0, action_monotonic_deadline - time.monotonic()))
            if thread.is_alive() and not cleanup_denied:
                event = {"target": "transport-process-group"}
                try:
                    transport = _read(directory / f"{role}-provider-spawn.json")
                    pid = transport["pid"]
                    event["process_group"] = pid
                    matched = False
                    for candidate in (transport["transport_argv"], transport["provider_argv"]):
                        try:
                            if time.monotonic() >= original_monotonic_deadline:
                                raise MechanismError(
                                    "original hard budget exhausted during cleanup"
                                )
                            current = _identity(
                                pid,
                                candidate,
                                deadline=original_monotonic_deadline,
                            )
                        except (MechanismError, subprocess.CalledProcessError):
                            continue
                        if current["started"] == " ".join(transport["os_started"].split()):
                            matched = True
                            break
                    if not matched or os.getpgid(pid) != pid or os.getsid(pid) != pid:
                        raise MechanismError("cleanup transport identity/session not proven")
                    os.killpg(pid, signal.SIGKILL)
                    event["signal_sent"] = True
                except Exception as cleanup_exc:
                    event["error"] = {
                        "type": type(cleanup_exc).__name__,
                        "errno": getattr(cleanup_exc, "errno", None),
                        "reason": str(cleanup_exc),
                    }
                cleanup.append(event)
            thread.join(max(0, original_monotonic_deadline - time.monotonic()))
        held = {
            **deadlines,
            "cleanup": cleanup,
            "provider_thread_drained": thread is None or not thread.is_alive(),
            "schema": OBSERVATION,
            "binding": request["binding"],
            "nonce": nonce,
            "passed": False,
            "desktop_qualified": False,
            "queue_action_accepted": False,
            "reason": str(exc),
            "provider_error": outcome.get("error"),
            "provider_result": outcome.get("result"),
            "processes": processes,
            "transports": transports,
        }
        _write(directory / "held.json", held)
        _write(active_path, {"nonce": nonce, "status": "unknown", "reason": str(exc)})
        return held


def supervise_cli(
    manifest_path: str,
    agent: str,
    profile_id: str,
    *,
    budget_seconds: int,
    timeout: int = 60,
    offload=None,
) -> dict:
    """Serialize qualification attempts; unresolved intent never starts a new budget."""
    from closer_campaign import load_manifest

    manifest = load_manifest(manifest_path)
    mechanism(manifest, CLI)
    root = _root(manifest)
    with (root / "supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MechanismError("CLI supervisor already owns an active submission") from exc
        try:
            return _supervise_cli_locked(
                manifest_path,
                agent,
                profile_id,
                budget_seconds=budget_seconds,
                timeout=timeout,
                offload=offload,
            )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def supervision_status(manifest_path: str) -> dict:
    """Read-only restart reconciliation evidence; never launches, clears or retries."""
    from closer_campaign import load_manifest

    manifest = load_manifest(manifest_path)
    mechanism(manifest, CLI)
    root = _root(manifest)
    active = _read(root / "active.json")
    nonce = active.get("nonce")
    if (
        not isinstance(nonce, str)
        or len(nonce) != 48
        or any(c not in "0123456789abcdef" for c in nonce)
    ):
        raise MechanismError("invalid retained supervisor nonce")
    directory = root / nonce
    evidence = {}
    for role in ("first", "restart"):
        for kind in (
            "intent",
            "provider-started",
            "provider-completed",
            "provider-returned",
            "provider-error",
            "os-observation",
            "terminal",
        ):
            path = directory / f"{role}-{kind}.json"
            if path.exists():
                evidence[f"{role}-{kind}"] = _read(path)
    return {
        "schema": "cli-supervision-readback/v1",
        "mechanism": CLI,
        "active": active,
        "evidence": evidence,
        "retry_authorized": False,
        "qualification_problems": qualification_problems(manifest),
    }


def _selftest() -> int:
    assert mechanism({}) == DESKTOP
    assert configured_policy_problem({}) is not None
    assert qualification_problems({})
    try:
        mechanism({"mechanism_contract": {"schema": CONTRACT, "mechanism": "foreign/v1"}})
    except MechanismError:
        pass
    else:
        raise AssertionError("unknown execution mechanism admitted")
    print("closer_mechanisms selftest: PASS (offline; no provider or native actions)")
    return 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--selftest"]:
        raise SystemExit(_selftest())
    raise SystemExit("Use the pinned closer_launcher entrypoint")
