"""Pinned native-wrapper handoff observation, never Desktop scheduler attestation.

The reviewed wrapper and this fixed observer are trusted same-UID host code. A
signature detects substituted JSON; it is not an OS boundary against that user.
No provider, queue or scheduler-control API is used by qualification.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
from pathlib import Path

import closer_mechanisms as mechanisms

POLICY = "native-configured-policy/v1"
OBSERVATION = "native-wrapper-lifecycle/v1"
REQUEST = "native-wrapper-qualification-request/v1"
MARKER = "# BEGIN CLOSER PINNED NATIVE ENTRY v1"
END_MARKER = "# END CLOSER PINNED NATIVE ENTRY v1"
_CURRENT_ENTRY: dict[str, str] = {}


def _hash(path):
    if Path(path).is_symlink():
        raise mechanisms.MechanismError("native pinned artifact is symlinked")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def policy_problem(policy):
    if policy.get("schema") != POLICY or policy.get("mechanism") != mechanisms.NATIVE:
        return "native configured policy contract missing"
    # Same canonical supervising lane, not a provider-inferred automation name.
    mapped = {**policy, "schema": mechanisms.POLICY, "mechanism": mechanisms.CLI}
    problem = mechanisms.configured_policy_problem(mapped)
    return problem.replace("CLI configured", "native configured") if problem else None


def contract(manifest):
    value = manifest.get("mechanism_contract") or {}
    if value.get("schema") != mechanisms.CONTRACT or value.get("mechanism") != mechanisms.NATIVE:
        raise mechanisms.MechanismError("native mechanism contract missing")
    # Concrete supported wrapper. Other actors remain independent capabilities.
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    pin = value.get("wrapper") or {}
    if value.get("actor") != "cursor" or pin.get("path") != str(wrapper):
        raise mechanisms.MechanismError("native wrapper/actor is not supported")
    if wrapper.is_symlink() or wrapper.resolve() != wrapper or _hash(wrapper) != pin.get("sha256"):
        raise mechanisms.MechanismError("native wrapper pin changed or aliased")
    interpreter = Path(value.get("interpreter", {}).get("path", ""))
    if (
        interpreter.resolve() != interpreter
        or not interpreter.is_file()
        or _hash(interpreter) != value.get("interpreter", {}).get("sha256")
        or Path(manifest["runtime"]["python"]).resolve() != interpreter
    ):
        raise mechanisms.MechanismError("native physical interpreter pin differs")
    root = Path(value.get("observation_root", ""))
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not root.is_dir()
        or root.stat().st_mode & 0o777 != 0o700
    ):
        raise mechanisms.MechanismError("native observation root must be canonical/private")
    campaign_root = Path(value.get("campaign_root", ""))
    if not campaign_root.is_absolute() or campaign_root.resolve() != campaign_root:
        raise mechanisms.MechanismError("native campaign root is not canonical")
    if "closer_native_observer.py" not in (manifest.get("modules") or {}):
        raise mechanisms.MechanismError("native observer is outside pinned package")
    problem = policy_problem(manifest.get("policy") or {})
    if problem:
        raise mechanisms.MechanismError(problem)
    return value, wrapper, interpreter, root


def binding(manifest):
    value, _wrapper, _python, _root = contract(manifest)
    return {
        "mechanism": mechanisms.NATIVE,
        "mechanism_contract_digest": mechanisms.digest(value),
        "policy_digest": mechanisms.digest(manifest["policy"]),
        **{key: manifest.get(key) for key in mechanisms.PIN_KEYS},
    }


def wrapper_block(manifest_path, campaign_root, interpreter, launcher):
    """Reviewed opt-in source text; caller writes only a preview, never a host file."""
    args = [
        str(interpreter),
        str(launcher),
        "native-entry",
        "--manifest",
        str(manifest_path),
        "--root",
        str(campaign_root),
    ]
    return (
        MARKER + "\n"
        'if [[ "$LANE" == "closer" && -f "$HOME/.codex/handoff/closer-native-handoff.enabled" ]]; then\n'
        "  " + shlex.join(args) + "\n"
        '  exit "$?"\nfi\n' + END_MARKER + "\n"
    )


def render_cursor_wrapper(source, *, manifest_path, campaign_root, interpreter, launcher):
    """Return opt-in wrapper bytes, before legacy capacity/auth/provider discovery."""
    if MARKER in source or END_MARKER in source:
        raise mechanisms.MechanismError("wrapper already has a native entry block")
    anchor = 'export PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"'
    if (
        source.count(anchor) != 1
        or 'case "$LANE" in' not in source
        or not source.startswith("#!/bin/bash\n")
    ):
        raise mechanisms.MechanismError("unsupported Cursor wrapper source")
    return source.replace(
        anchor,
        wrapper_block(manifest_path, campaign_root, interpreter, launcher) + "\n" + anchor,
        1,
    )


def _entry_argv(manifest, manifest_path):
    value, _wrapper, python, _root = contract(manifest)
    return [
        str(python),
        manifest["launcher"]["path"],
        "native-entry",
        "--manifest",
        str(manifest_path),
        "--root",
        value["campaign_root"],
    ]


def _check_wrapper_entry(manifest, manifest_path):
    value, wrapper, python, root = contract(manifest)
    expected = wrapper_block(
        manifest_path, value["campaign_root"], python, manifest["launcher"]["path"]
    )
    if wrapper.read_text().count(expected) != 1:
        raise mechanisms.MechanismError("wrapper does not hand off to this exact pinned entry")
    if not (Path.home() / ".codex/handoff/closer-native-handoff.enabled").is_file():
        raise mechanisms.MechanismError("native wrapper opt-in is absent")
    return value, wrapper, python, root


def _write(path, value):
    mechanisms._write(path, value)


def _read(path):
    return mechanisms._read(path)


def _wait(path, deadline):
    return mechanisms._wait(path, deadline)


def _signed(path, value, root):
    _write(
        path, {"record": value, "signature": mechanisms._signature(value, mechanisms._key(root))}
    )


def _process_public(value):
    return {key: value[key] for key in ("pid", "started", "argv_sha256", "source")}


def _request(path, manifest):
    root = contract(manifest)[3]
    path = Path(path)
    if (
        path.parent.parent != root
        or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
        or path.name != "request.json"
        or path.parent.is_symlink()
    ):
        raise mechanisms.MechanismError("native request path is outside private observation root")
    request = _read(path)
    action, hard = request.get("action_deadline"), request.get("hard_deadline")
    if (
        request.get("schema") != REQUEST
        or request.get("nonce") != path.parent.name
        or request.get("binding") != binding(manifest)
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in (action, hard)
        )
        or hard - action != 5
        or not 0 < hard - time.monotonic() <= 120
    ):
        raise mechanisms.MechanismError("native request binding/deadline differs")
    return request


def _native_child(manifest, request_path, role):
    request = _request(request_path, manifest)
    _value, _wrapper, _python, root = contract(manifest)
    if request.get("schema") != REQUEST or request.get("binding") != binding(manifest):
        return 78
    directory = Path(request_path).parent
    if directory.parent != root or directory.is_symlink():
        return 78
    deadline = request["action_deadline"]
    if (
        not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or time.monotonic() >= deadline
    ):
        return 78
    if role == "first":
        _write(directory / "child-ready.json", {"nonce": request["nonce"], "pid": os.getpid()})
        handoff = _wait(directory / "child-handoff.json", deadline)
        if handoff.get("nonce") != request["nonce"]:
            return 78
        _write(
            directory / "child-journal.json",
            {"nonce": request["nonce"], "binding": request["binding"], "pid": os.getpid()},
        )
        while time.monotonic() < deadline:
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        return 78
    _write(directory / "restart-ready.json", {"nonce": request["nonce"], "pid": os.getpid()})
    if _wait(directory / "restart-ack.json", deadline).get("nonce") != request["nonce"]:
        return 78
    journal = _read(directory / "child-journal.json")
    if journal.get("nonce") != request["nonce"] or journal.get("binding") != request["binding"]:
        return 78
    _write(
        directory / "child-result.json",
        {**journal, "restarted_pid": os.getpid(), "no_effect": True},
    )
    return 0


def _qualification_lifecycle(manifest, manifest_path, request, directory):
    """Only known no-effect children may be terminated; never a provider/queue child."""
    from closer_mechanisms import _identity

    python = contract(manifest)[2]
    base = [
        str(python),
        manifest["launcher"]["path"],
        "native-noeffect-child",
        "--manifest",
        str(manifest_path),
        "--request",
        str(directory / "request.json"),
    ]
    deadline = request["action_deadline"]
    children = []
    denial = None
    try:
        first_argv = [*base, "--role", "first"]
        first = subprocess.Popen(
            first_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        children.append(first)
        _wait(directory / "child-ready.json", deadline)
        identity = _identity(first.pid, first_argv, deadline=deadline)
        _write(directory / "child-handoff.json", {"nonce": request["nonce"]})
        journal = _wait(directory / "child-journal.json", deadline)
        if journal.get("pid") != first.pid or journal.get("binding") != request["binding"]:
            raise mechanisms.MechanismError("no-effect handoff journal identity differs")
        # Exact birth and argv immediately before terminating our known child.
        if _identity(first.pid, first_argv, deadline=deadline) != identity:
            raise mechanisms.MechanismError("no-effect child changed before controlled crash")
        first.kill()
        first.communicate(timeout=max(0.001, deadline - time.monotonic()))
        if first.returncode != -signal.SIGKILL:
            raise mechanisms.MechanismError("controlled no-effect crash was not observed")
        _write(
            directory / "crash.json",
            {
                "first": _process_public(identity),
                "exit": first.returncode,
                "nonce": request["nonce"],
                "no_effect": True,
            },
        )
        restart_argv = [*base, "--role", "restart"]
        restart = subprocess.Popen(
            restart_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        children.append(restart)
        _wait(directory / "restart-ready.json", deadline)
        restart_identity = _identity(restart.pid, restart_argv, deadline=deadline)
        _write(directory / "restart-ack.json", {"nonce": request["nonce"]})
        stdout, stderr = restart.communicate(timeout=max(0.001, deadline - time.monotonic()))
        if restart.returncode != 0:
            raise mechanisms.MechanismError(f"no-effect restart refused ({restart.returncode})")
        result = _read(directory / "child-result.json")
        if (
            result.get("pid") != first.pid
            or result.get("restarted_pid") != restart.pid
            or result.get("binding") != request["binding"]
        ):
            raise mechanisms.MechanismError("no-effect restart result differs")
        return {
            "first": _process_public(identity),
            "restart": _process_public(restart_identity),
            "restart_pid": restart.pid,
            "crash_exit": first.returncode,
            "result_sha256": _hash(directory / "child-result.json"),
        }
    except PermissionError as exc:
        denial = {"type": type(exc).__name__, "errno": exc.errno, "reason": str(exc)}
        _write(directory / "denial.json", denial)
        raise
    finally:
        # No alternate signal after a denial. Children self-expire at the same
        # original action deadline; retain UNKNOWN if their terminality is absent.
        for child in children:
            if child.poll() is None:
                try:
                    child.communicate(
                        timeout=max(0.001, request["hard_deadline"] - time.monotonic())
                    )
                except subprocess.TimeoutExpired:
                    if denial is None:
                        _write(
                            directory / "cleanup-pending.json", {"pid": child.pid, "unknown": True}
                        )


def _own_identity(pid, argv, deadline):
    if pid not in {os.getpid(), os.getppid()}:
        raise mechanisms.MechanismError("native identity is outside own immediate lineage")
    before = mechanisms._metadata(pid, deadline)
    proof = mechanisms._argv_proof(before, deadline, ancestor=True)
    if proof != {"argc": len(argv), "argv_sha256": mechanisms._argv_digest(argv)}:
        raise mechanisms.MechanismError("native wrapper/entry OS argv differs from exact pin")
    if mechanisms._metadata(pid, deadline) != before:
        raise mechanisms.MechanismError("native wrapper/entry birth changed")
    return {
        "pid": pid,
        "started": before["started"],
        "argv_sha256": proof["argv_sha256"],
        "source": "os-byte-argv",
    }


def entry_active(manifest):
    """Only the observed, lock-held entry in this process may schedule work."""
    _value, _wrapper, _python, root = contract(manifest)
    active = _read(root / "native-active.json")
    return (
        active.get("binding") == binding(manifest)
        and active.get("status") == "submitted"
        and _CURRENT_ENTRY.get(str(root)) == active.get("nonce")
    )


def authorize_worker(manifest, argv):
    if not entry_active(manifest):
        raise mechanisms.MechanismError("native worker handoff requires observed entry")
    if not argv or argv[0] != str(contract(manifest)[2]):
        raise mechanisms.MechanismError("native worker requires pinned physical interpreter")
    fields = {}
    for flag in (
        "state",
        "target",
        "owner",
        "generation",
        "runtime-manifest",
        "mechanism",
        "result",
    ):
        key = "--" + flag
        if argv.count(key) != 1:
            raise mechanisms.MechanismError("native worker handoff argument differs")
        fields[flag] = argv[argv.index(key) + 1]
    if fields["mechanism"] != mechanisms.NATIVE:
        raise mechanisms.MechanismError("native worker handoff mechanism differs")
    result = Path(fields["result"])
    if not re.fullmatch(r"[0-9a-f]{12}", result.stem):
        raise mechanisms.MechanismError("native worker identity is invalid")
    _value, _wrapper, _python, root = contract(manifest)
    nonce = _CURRENT_ENTRY[str(root)]
    handoff_path = root / nonce / ("worker-" + result.stem + ".json")
    if handoff_path.exists() or handoff_path.is_symlink():
        raise mechanisms.MechanismError("native worker handoff already exists")
    _signed(
        handoff_path,
        {
            "schema": "native-worker-handoff/v1",
            "binding": binding(manifest),
            "nonce": nonce,
            "fields": fields,
            "argv_sha256": mechanisms.digest(argv),
        },
        root,
    )


def accept_worker(manifest, *, state, target, owner, generation, result, manifest_path):
    """Reconstitute only a handoff issued before this exact durable worker launch."""
    _value, _wrapper, _python, root = contract(manifest)
    worker_id = Path(result).stem
    if not re.fullmatch(r"[0-9a-f]{12}", worker_id):
        raise mechanisms.MechanismError("native worker identity is invalid")
    active = _read(root / "native-active.json")
    nonce = active.get("nonce", "")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise mechanisms.MechanismError("native invocation identity missing")
    signed = _read(root / nonce / ("worker-" + worker_id + ".json"))
    record = signed["record"]
    fields = {
        "state": state,
        "target": target,
        "owner": owner,
        "generation": str(generation),
        "result": result,
        "runtime-manifest": manifest_path,
        "mechanism": mechanisms.NATIVE,
    }
    if (
        not hmac.compare_digest(
            signed["signature"], mechanisms._signature(record, mechanisms._key(root))
        )
        or record.get("schema") != "native-worker-handoff/v1"
        or record.get("binding") != binding(manifest)
        or record.get("nonce") != nonce
        or record.get("fields") != fields
        or active.get("binding") != binding(manifest)
    ):
        raise mechanisms.MechanismError("native worker handoff differs from exact invocation")
    # This process already passed campaign owner/generation/claim/worker fences.
    # Its signed pre-launch handoff permits qualification during the entry's
    # short submitted-to-terminal transition; arbitrary JSON never does.
    _CURRENT_ENTRY[str(root)] = nonce


def native_entry(manifest, manifest_path, campaign_root):
    # The qualification supervisor already holds this guard in its parent.
    # Every normal invocation holds the same guard through durable terminality,
    # so qualification cannot replace another entry's submitted intent.
    if os.environ.get("CLOSER_NATIVE_QUALIFICATION_REQUEST"):
        return _native_entry(manifest, manifest_path, campaign_root)
    root = contract(manifest)[3]
    with (root / "native-supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise mechanisms.MechanismError(
                "native supervisor or wrapper entry already active"
            ) from exc
        return _native_entry(manifest, manifest_path, campaign_root)


def _native_entry(manifest, manifest_path, campaign_root):
    """Production wrapper entry; policy and observed exact wrapper precede any tick."""
    value, wrapper, _python, root = _check_wrapper_entry(manifest, manifest_path)
    if str(campaign_root) != value["campaign_root"]:
        raise mechanisms.MechanismError("native entry campaign root differs from pin")
    request_name = os.environ.get("CLOSER_NATIVE_QUALIFICATION_REQUEST")
    request = _request(request_name, manifest) if request_name else None
    if not request:
        problems = qualification_problems(manifest)
        if problems:
            raise mechanisms.MechanismError("; ".join(problems))
    deadline = request["action_deadline"] if request else time.monotonic() + 30
    wrapper_identity = _own_identity(
        os.getppid(), ["/bin/bash", str(wrapper), "closer"], deadline=deadline
    )
    entry_identity = _own_identity(
        os.getpid(), _entry_argv(manifest, manifest_path), deadline=deadline
    )
    nonce = request["nonce"] if request else secrets.token_hex(16)
    directory = root / nonce
    if request:
        if (
            request.get("schema") != REQUEST
            or request.get("binding") != binding(manifest)
            or Path(request_name) != directory / "request.json"
            or _read(root / "native-active.json").get("nonce") != nonce
        ):
            raise mechanisms.MechanismError("native qualification request identity differs")
    else:
        directory.mkdir(mode=0o700)
    with (root / "native-entry.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise mechanisms.MechanismError("native wrapper entry already active") from exc
        if (
            not request
            and (root / "native-active.json").exists()
            and _read(root / "native-active.json").get("status") != "terminal"
        ):
            raise mechanisms.MechanismError("prior native wrapper submission remains unknown")
        _write(
            root / "native-active.json",
            {"nonce": nonce, "status": "submitted", "binding": binding(manifest)},
        )
        observation = {
            "schema": OBSERVATION,
            "binding": binding(manifest),
            "nonce": nonce,
            "actor": value["actor"],
            "source": "owned-native-wrapper-process",
            "desktop_qualified": False,
            "scheduler_origin": "unobserved",
            "wrapper": _process_public(wrapper_identity),
            "entry": _process_public(entry_identity),
        }
        _write(directory / "startup.json", observation)
        try:
            if request:
                if _wait(directory / "observer-ack.json", deadline).get("nonce") != nonce:
                    raise mechanisms.MechanismError("native observer acknowledgement differs")
                lifecycle = _qualification_lifecycle(manifest, manifest_path, request, directory)
                observation.update(
                    kind="native-wrapper-qualification",
                    passed=True,
                    lifecycle=lifecycle,
                    phases={
                        phase: {"file": name, "sha256": _hash(directory / name)}
                        for phase, name in {
                            "startup": "startup.json",
                            "handoff": "child-journal.json",
                            "crash_restart": "crash.json",
                            "result_recording": "child-result.json",
                        }.items()
                    },
                )
            else:
                from closer_campaign import tick

                # In-process capability permits this lock-held entry to recheck
                # qualification while its own submission is in progress. JSON
                # alone never issues that capability to another worker process.
                _CURRENT_ENTRY[str(root)] = nonce
                try:
                    result = tick(campaign_root, str(manifest_path), mechanism=mechanisms.NATIVE)
                finally:
                    _CURRENT_ENTRY.pop(str(root), None)
                observation.update(
                    kind="native-wrapper-invocation", passed=True, tick_result=result
                )
            _signed(directory / "terminal.json", observation, root)
            _write(
                root / "native-active.json",
                {"nonce": nonce, "status": "terminal", "binding": binding(manifest)},
            )
            return 0
        except BaseException as exc:
            observation.update(
                passed=False,
                unknown=True,
                error={
                    "type": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                    "reason": str(exc),
                },
            )
            _signed(directory / "terminal.json", observation, root)
            raise


def supervise_native(manifest, manifest_path, *, budget_seconds):
    """Actually invoke the opted-in reviewed wrapper, with no provider/queue path."""
    value, wrapper, _python, root = _check_wrapper_entry(manifest, manifest_path)
    if (
        not isinstance(budget_seconds, (int, float))
        or isinstance(budget_seconds, bool)
        or not 10 <= budget_seconds <= 120
    ):
        raise mechanisms.MechanismError("native qualification budget must be 10..120 seconds")
    with (root / "native-supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise mechanisms.MechanismError("native qualification already active") from exc
        active = root / "native-active.json"
        if active.exists() and _read(active).get("status") != "terminal":
            raise mechanisms.MechanismError("native submission remains unknown; no relaunch")
        mechanisms._key(root, create=True)
        nonce = secrets.token_hex(16)
        directory = root / nonce
        directory.mkdir(mode=0o700)
        hard = time.monotonic() + budget_seconds
        request = {
            "schema": REQUEST,
            "nonce": nonce,
            "binding": binding(manifest),
            "action_deadline": hard - 5,
            "hard_deadline": hard,
        }
        _write(directory / "request.json", request)
        _write(active, {"nonce": nonce, "binding": request["binding"], "status": "submitted"})
        env = dict(
            os.environ,
            CLOSER_NATIVE_QUALIFICATION_REQUEST=str(directory / "request.json"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        from closer_launcher import CREDENTIAL_VARS

        for key in CREDENTIAL_VARS:
            env.pop(key, None)
        proc = subprocess.Popen(
            ["/bin/bash", str(wrapper), "closer"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            while not (directory / "startup.json").is_file():
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=max(0.001, hard - time.monotonic()))
                    _write(
                        directory / "wrapper-refusal.json",
                        {"exit": proc.returncode, "stdout": stdout, "stderr": stderr},
                    )
                    raise mechanisms.MechanismError(
                        "native wrapper refused before observation: "
                        + str(proc.returncode)
                        + ": "
                        + (stdout + stderr)[-2000:]
                    )
                if time.monotonic() >= hard - 5:
                    raise mechanisms.MechanismError(
                        "native wrapper observation missed original deadline"
                    )
                time.sleep(0.02)
            startup = _read(directory / "startup.json")
            wrapper_proof = mechanisms._identity(
                proc.pid, ["/bin/bash", str(wrapper), "closer"], deadline=hard - 5
            )
            if (
                startup.get("wrapper") != _process_public(wrapper_proof)
                or startup.get("binding") != request["binding"]
            ):
                raise mechanisms.MechanismError(
                    "native wrapper observation does not match owned process"
                )
            entry = startup["entry"]
            independently = mechanisms._identity(
                entry["pid"], _entry_argv(manifest, manifest_path), deadline=hard - 5
            )
            if entry != _process_public(independently) or entry["pid"] == proc.pid:
                raise mechanisms.MechanismError("native entry process observation differs")
            chain = mechanisms._lineage(entry["pid"], deadline=hard - 5)
            if not any(
                row["pid"] == proc.pid and row["started"] == wrapper_proof["started"]
                for row in chain
            ):
                raise mechanisms.MechanismError("native entry is outside owned wrapper lineage")
            _write(directory / "observer-ack.json", {"nonce": nonce})
            stdout, stderr = proc.communicate(timeout=max(0.001, hard - time.monotonic()))
            signed = _read(directory / "terminal.json")
            record = signed["record"]
            if (
                proc.returncode != 0
                or record.get("passed") is not True
                or record.get("binding") != request["binding"]
            ):
                raise mechanisms.MechanismError("native no-effect lifecycle did not complete")
            if not hmac.compare_digest(
                signed["signature"], mechanisms._signature(record, mechanisms._key(root))
            ):
                raise mechanisms.MechanismError("native lifecycle signature differs")
            _write(root / "native-qualification.json", signed)
            return record
        except BaseException:
            # No fallback to legacy wrapper/provider and no second submission.
            # Its known no-effect children share the original action deadline.
            if proc.poll() is None:
                try:
                    proc.communicate(timeout=max(0.001, hard - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            raise


def qualification_problems(manifest):
    try:
        _value, _wrapper, _python, root = contract(manifest)
        signed = _read(root / "native-qualification.json")
        record = signed["record"]
        if not hmac.compare_digest(
            signed["signature"], mechanisms._signature(record, mechanisms._key(root))
        ):
            raise mechanisms.MechanismError("native observer signature differs")
        if (
            record.get("schema") != OBSERVATION
            or record.get("kind") != "native-wrapper-qualification"
            or record.get("binding") != binding(manifest)
            or record.get("passed") is not True
            or record.get("desktop_qualified") is not False
            or record.get("source") != "owned-native-wrapper-process"
            or record.get("actor") != "cursor"
            or record.get("scheduler_origin") != "unobserved"
        ):
            raise mechanisms.MechanismError("native lifecycle belongs to another capability or pin")
        directory = root / record["nonce"]
        if directory.parent != root or directory.is_symlink():
            raise mechanisms.MechanismError("native observation artifact path escaped")
        expected = {
            "startup": "startup.json",
            "handoff": "child-journal.json",
            "crash_restart": "crash.json",
            "result_recording": "child-result.json",
        }
        for phase, name in expected.items():
            ref = record["phases"][phase]
            if ref != {"file": name, "sha256": _hash(directory / name)}:
                raise mechanisms.MechanismError("native phase artifact changed: " + phase)
        result = _read(directory / "child-result.json")
        if (
            result.get("binding") != record["binding"]
            or result.get("nonce") != record["nonce"]
            or result.get("no_effect") is not True
            or _read(directory / "crash.json").get("exit") != -signal.SIGKILL
        ):
            raise mechanisms.MechanismError("native result belongs to another package or lifecycle")
        active = _read(root / "native-active.json")
        if active.get("status") != "terminal" and _CURRENT_ENTRY.get(str(root)) != active.get(
            "nonce"
        ):
            raise mechanisms.MechanismError("native submission remains unknown")
        return []
    except (OSError, ValueError, KeyError, TypeError, mechanisms.MechanismError) as exc:
        return [f"native handoff qualification held: {exc}"]


def status(manifest):
    _value, _wrapper, _python, root = contract(manifest)
    active = _read(root / "native-active.json") if (root / "native-active.json").exists() else None
    problems = qualification_problems(manifest)
    return {
        "mechanism": mechanisms.NATIVE,
        "source": "owned-native-wrapper-process",
        "desktop_qualified": False,
        "scheduler_origin": "unobserved",
        "qualified": not problems,
        "problems": problems,
        "active": active,
        "binding": binding(manifest),
    }


def _selftest():
    """Pure contract controls; never inspect or invoke a host wrapper."""
    source = '#!/bin/bash\nLANE="${1:?}"\ncase "$LANE" in\ncloser|opener) ;;\nesac\nexport PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"\nlegacy_provider\n'
    kwargs = dict(
        manifest_path="/private/pin.json",
        campaign_root="/private/campaigns",
        interpreter="/private/python",
        launcher="/private/closer_launcher.py",
    )
    rendered = render_cursor_wrapper(source, **kwargs)
    assert (
        rendered.index(MARKER) < rendered.index("export PATH") < rendered.index("legacy_provider")
    )
    assert 'exit "$?"' in rendered and rendered.count(MARKER) == 1
    try:
        render_cursor_wrapper(rendered, **kwargs)
    except mechanisms.MechanismError:
        pass
    else:
        raise AssertionError("double native entry admitted")
    assert policy_problem({})
    try:
        contract({})
    except mechanisms.MechanismError:
        pass
    else:
        raise AssertionError("unbound native manifest admitted")
    key = b"x" * 32
    assert mechanisms._signature({"target": "owner/repo#1"}, key) != mechanisms._signature(
        {"target": "owner/repo#2"}, key
    )
    print("closer_native_observer selftest: PASS (offline; no host/provider/queue actions)")
    return 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--selftest"]:
        raise SystemExit(_selftest())
    raise SystemExit("Use the pinned closer_launcher native entrypoint")
