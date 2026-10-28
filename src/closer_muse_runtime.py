"""Source-resident Muse capture bridge; not a qualification or gate issuer.

The original controller must supply its genuine admission/lease and existing
connection. This module cannot enroll a profile or authorize a model answer.
The default executor is the existing bounded pipe/process-group capture, not a
new runner. Shared role/profile routing deliberately remains unchanged.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

MODEL = "muse-spark-1.3"
PROFILE = "muse-spark-1.3-high"
ROLE = "strong_criterion_adjudication"
NATIVE_SHA256 = "9fd40ae9c6dfb847e3ef58b61405c004d0ebbf19ceb0297be966f750b3b84aa0"
MAX_INPUT = 65536
MAX_RAW = 786432
MAX_FINAL = 16384
MAX_SECONDS = 45


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular source/native file required")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
        raise ValueError("bounded regular runtime file required")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        opened = os.fstat(stream.fileno())

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)

    if identity(before) != identity(opened) or identity(before) != identity(path.stat()):
        raise ValueError("runtime file changed during capture")
    return digest.hexdigest()


@dataclass(frozen=True)
class MusePlan:
    profile_id: str
    role: str
    model: str
    effort: str
    argv: tuple[str, ...]
    prompt: bytes
    workspace: Path
    workspace_identity: tuple[int, int, int, int]
    binary_sha256: str
    source_sha256: str
    parser_sha256: str
    capture_sha256: str
    basis_sha256: str

    @property
    def sha256(self) -> str:
        value = asdict(self)
        value["prompt"] = hashlib.sha256(self.prompt).hexdigest()
        value["workspace"] = str(self.workspace)
        return _digest(value)


class RootAdmission(Protocol):
    """Implemented only by original host admission, not by worker/model JSON.

    check must authenticate exact source/profile/producer/basis, a live original
    Muse pool lease, reviewed effective no-tools/startup controls and authorized
    existing connection. Hashes in MusePlan describe input, not that authority.
    """

    def check(self, plan: MusePlan) -> None: ...
    def environment(self, plan: MusePlan) -> dict[str, str]: ...
    def started(self, pid: int) -> None: ...
    def finished(self, pid: int) -> None: ...


def prepare(
    *,
    prompt: str,
    workspace: Path,
    binary: Path,
    basis_sha256: str,
    profile_id: str = PROFILE,
    role: str = ROLE,
) -> MusePlan:
    """Pure source-bound argv preparation; no auth, subprocess or enrollment."""
    import adapters
    import transport_qualification_launcher as launcher

    if profile_id != PROFILE or role != ROLE:
        raise ValueError("exact Spark named role required")
    if type(prompt) is not str or not 0 < len(prompt.encode()) <= MAX_INPUT:
        raise ValueError("bounded complete prompt required")
    if (
        not workspace.is_absolute()
        or workspace.is_symlink()
        or workspace.resolve() != workspace
        or not workspace.is_dir()
        or any(workspace.iterdir())
        or workspace.stat().st_mode & 0o077
    ):
        raise ValueError("empty canonical private workspace required")
    if len(basis_sha256) != 64 or any(c not in "0123456789abcdef" for c in basis_sha256):
        raise ValueError("protected original basis digest required")
    if _sha(binary) != NATIVE_SHA256:
        raise ValueError("installed Muse payload changed")
    argv = (
        str(binary),
        "exec",
        "--provider",
        "meta",
        "--preset",
        "empty",
        "--model",
        MODEL,
        "--reasoning-effort",
        "high",
        "--json",
        "--prompt-file",
        str(workspace / "prompt.txt"),
        "--workspace",
        str(workspace),
        "--worktree",
        "off",
        "--max-model-steps",
        "1",
        "--max-tool-output-bytes",
        "8192",
        "--no-parallel-tool-calls",
        "--disable-web-tools",
        "--disable-reminders",
        "--disable-request-user-input",
        "--no-foreign-personal-context",
        "--no-session-log",
        "--disable-write",
        "--disable-shell",
        "--approval-mode",
        "untrusted",
        "--approval-judge",
        "off",
        "--contributor-mode",
        "off",
    )
    return MusePlan(
        profile_id,
        role,
        MODEL,
        "high",
        argv,
        prompt.encode(),
        workspace,
        (
            workspace.stat().st_dev,
            workspace.stat().st_ino,
            workspace.stat().st_uid,
            workspace.stat().st_gid,
        ),
        NATIVE_SHA256,
        _sha(Path(__file__)),
        hashlib.sha256(inspect.getsource(adapters.muse_terminal_text).encode()).hexdigest(),
        _sha(Path(launcher.__file__)),
        basis_sha256,
    )


def _check(plan: MusePlan) -> None:
    import adapters
    import transport_qualification_launcher as launcher

    if (
        type(plan) is not MusePlan
        or plan.profile_id != PROFILE
        or plan.role != ROLE
        or plan.model != MODEL
        or plan.effort != "high"
        or plan.binary_sha256 != NATIVE_SHA256
        or _sha(Path(plan.argv[0])) != plan.binary_sha256
        or _sha(Path(__file__)) != plan.source_sha256
        or _sha(Path(launcher.__file__)) != plan.capture_sha256
        or hashlib.sha256(inspect.getsource(adapters.muse_terminal_text).encode()).hexdigest()
        != plan.parser_sha256
    ):
        raise ValueError("Muse runtime closure changed")
    # Rebuild before any connection action; mutated argv cannot gain tools or aliases.
    baseline = prepare(
        prompt=plan.prompt.decode(),
        workspace=plan.workspace,
        binary=Path(plan.argv[0]),
        basis_sha256=plan.basis_sha256,
    )
    if baseline != plan:
        raise ValueError("Muse prepared command changed")


def _model_response_lifecycle(events):
    """Only the observed root model-response intent is outside tool scope.

    A label alone is insufficient: join the task to the accepted command/run,
    its proposal and scheduled idempotency key, and its completed lifecycle.
    No policy decision or side-effect arguments are interpreted as authority.
    """
    command = session = None
    started = False
    tasks = {}
    model_tasks = set()
    for record in events:
        kind = record.get("payload_type")
        payload = record.get("payload")
        if type(payload) is not dict:
            raise ValueError("native payload object required")
        if kind == "runtime.command.accepted":
            stream = record.get("stream")
            if type(stream) is not dict:
                raise ValueError("accepted native session stream required")
            command = payload.get("command_id")
            session = stream.get("id")
        elif kind == "run.lifecycle.started":
            started = True
        if kind == "tool.result":
            raise ValueError("tool-bearing run is outside named data-only scope")
        if kind != "task.stream.linked" and not str(kind).startswith("task.lifecycle."):
            continue
        task = payload.get("task_id")
        if (
            not started
            or type(task) is not str
            or not task
            or len(task) > 256
            or payload.get("command_id") != command
            or payload.get("run_stream") != {"kind": "run", "id": command}
            or payload.get("task_stream") != {"kind": "task", "id": task}
            or record.get("stream") != {"kind": "session", "id": session}
            or record.get("causation_id") != command
            or record.get("record_type") != "event"
            or record.get("durability") != "durable"
        ):
            raise ValueError("model-response task/run identity differs")
        common = {"command_id", "kind", "run_stream", "task_id", "task_stream"}
        if kind == "task.stream.linked":
            if (
                set(payload) != common
                or payload.get("kind") != "task_stream_linked"
                or task in tasks
            ):
                raise ValueError("model-response task linkage differs")
            tasks[task] = {"phase": "linked"}
            continue
        event = payload.get("event")
        phase = kind.removeprefix("task.lifecycle.")
        if (
            set(payload) != common | {"event"}
            or payload.get("kind") != "task_lifecycle"
            or type(event) is not dict
            or event.get("kind") != phase
            or event.get("task_id") != task
            or task not in tasks
        ):
            raise ValueError("model-response lifecycle shape differs")
        state = tasks[task]
        if phase == "proposed":
            if state["phase"] != "linked" or set(event) != {"kind", "task_id", "task_kind"}:
                raise ValueError("model-response proposal differs")
            state.update(phase="proposed", task_kind=event["task_kind"])
            if event["task_kind"] == "model.meta.response":
                model_tasks.add(task)
                if len(model_tasks) > 1:
                    raise ValueError("multiple model-response tasks outside one-step scope")
        elif phase == "side_effect_intent":
            if (
                set(event)
                != {
                    "kind",
                    "task_id",
                    "operation",
                    "idempotency_key",
                    "parent_task_id",
                    "cancellation_handle",
                    "policy_decision",
                }
                or state.get("task_kind") != "model.meta.response"
                or state["phase"] != "scheduled"
                or event["operation"] != "model.meta.response"
                or event["idempotency_key"] != state.get("idempotency_key")
                or event["parent_task_id"] is not None
                or event["cancellation_handle"] is not None
                or event["policy_decision"] != "not_applicable"
            ):
                raise ValueError("tool-bearing or unbound model-response intent refused")
            state["phase"] = phase
        elif task in model_tasks:
            if phase == "status":
                if state["phase"] != "started" or set(event) != {
                    "kind",
                    "task_id",
                    "message",
                    "details",
                }:
                    raise ValueError("model-response status differs")
                continue
            preceding = {
                "accepted": "proposed",
                "scheduled": "accepted",
                "started": "side_effect_intent",
                "completed": "started",
            }
            expected_keys = {"kind", "task_id"} | (
                {"idempotency_key"} if phase == "scheduled" else set()
            )
            if (
                phase not in preceding
                or state["phase"] != preceding[phase]
                or set(event) != expected_keys
            ):
                raise ValueError("model-response lifecycle order differs")
            if phase == "scheduled":
                key = event["idempotency_key"]
                if (
                    type(key) is not str
                    or not key
                    or len(key) > 1024
                    or any(ord(c) < 32 for c in key)
                ):
                    raise ValueError("model-response scheduled identity differs")
                state["idempotency_key"] = key
            state["phase"] = phase
    if any(tasks[task]["phase"] != "completed" for task in model_tasks):
        raise ValueError("incomplete model-response task lifecycle")


def _final(raw: bytes) -> str:
    import adapters

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate native JSON member")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonfinite native JSON")

    events = []
    for line in raw.decode("utf8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line, object_pairs_hook=pairs, parse_constant=invalid)
        if type(event) is not dict:
            raise ValueError("native event object required")
        events.append(event)
    _model_response_lifecycle(events)
    text = adapters.muse_terminal_text(raw.decode("utf8"))
    if not text or len(text.encode()) > MAX_FINAL:
        raise ValueError("bounded bound native terminal required")
    return text


def capture(plan: MusePlan, admission: RootAdmission, *, deadline: float) -> dict:
    """One real capture after original root admission; no retry/fallback.

    The host gate creates the prompt file and existing connection aliases outside
    model custody. This function never reads or copies the owner's credentials.
    No model completion is published until the owned group is terminal/absent.
    Returned text still needs original proof/schema/selected-merge validation.
    """
    from transport_qualification_launcher import _capture

    _check(plan)
    if admission is None or not math.isfinite(deadline):
        raise ValueError("original root admission and finite deadline required")
    start = time.monotonic()
    if deadline <= start or deadline > start + MAX_SECONDS:
        raise ValueError("original remaining deadline exceeds Muse bound")
    admission.check(plan)
    env = admission.environment(plan)
    prompt_file = plan.workspace / "prompt.txt"
    current = plan.workspace.stat()
    if (
        plan.workspace.is_symlink()
        or plan.workspace.resolve() != plan.workspace
        or current.st_mode & 0o077
        or (current.st_dev, current.st_ino, current.st_uid, current.st_gid)
        != plan.workspace_identity
    ):
        raise ValueError("original private workspace custody changed")
    if (
        type(env) is not dict
        or not env
        or any(type(k) is not str or type(v) is not str for k, v in env.items())
        or prompt_file.is_symlink()
        or not prompt_file.is_file()
        or prompt_file.read_bytes() != plan.prompt
    ):
        raise ValueError("root connection/prompt custody not established")
    admission.check(plan)
    cleanup = []

    def finished(pid):
        admission.finished(pid)
        cleanup.append(pid)

    result = _capture(
        list(plan.argv),
        plan.prompt,
        plan.workspace,
        dict(env),
        deadline=deadline,
        cap=MAX_RAW,
        on_started=admission.started,
        on_finished=finished,
    )
    if (
        not cleanup
        or result.get("exit_code") != 0
        or result.get("problem")
        or type(result.get("stdout")) is not bytes
        or type(result.get("stderr")) is not bytes
        or len(result["stdout"]) + len(result["stderr"]) > MAX_RAW
    ):
        return {
            "plan_sha256": plan.sha256,
            "capture": result,
            "final": None,
            "problem": "completion_or_cleanup_unknown",
            "retry": False,
            "action_authority": False,
            "role_enrollment_granted": False,
        }
    try:
        final = _final(result["stdout"])
    except (ValueError, TypeError, UnicodeError):
        return {
            "plan_sha256": plan.sha256,
            "capture": result,
            "final": None,
            "problem": "native_terminal_rejected",
            "retry": False,
            "action_authority": False,
            "role_enrollment_granted": False,
        }
    return {
        "plan_sha256": plan.sha256,
        "final": final,
        "capture": result,
        "requested_model": MODEL,
        "requested_effort": "high",
        "served_identity": None,
        "cleanup_terminal": True,
        "action_authority": False,
        "role_enrollment_granted": False,
    }
