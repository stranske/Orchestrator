"""Inert checks of the actual source-resident capture/terminal bridge."""

import json
import time
from dataclasses import replace

import pytest

import closer_muse_runtime as muse
import transport_qualification_launcher as launcher


def native(text="answer"):
    rows = []
    for kind, payload in [
        (
            "runtime.command.accepted",
            {"kind": "command_accepted", "command_id": "c", "command_kind": "turn.submit"},
        ),
        (
            "session.run.linked",
            {
                "kind": "session_run_linked",
                "command_id": "c",
                "run_stream": {"kind": "run", "id": "c"},
            },
        ),
        (
            "run.lifecycle.started",
            {"kind": "run_started", "command_id": "c", "run_stream": {"kind": "run", "id": "c"}},
        ),
        (
            "run.terminal.completed",
            {
                "kind": "run_terminal",
                "command_id": "c",
                "run_stream": {"kind": "run", "id": "c"},
                "terminal": "completed",
                "text": text,
                "reason": None,
            },
        ),
    ]:
        rows.append(
            {
                "schema_version": 1,
                "payload_schema_version": 1,
                "stream": {"kind": "session", "id": "s"},
                "record_type": "event",
                "durability": "durable",
                "causation_id": "c",
                "payload_type": kind,
                "payload": payload,
            }
        )
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


@pytest.fixture
def plan(tmp_path, monkeypatch):
    workspace = tmp_path.resolve() / "work"
    workspace.mkdir(mode=0o700)
    binary = tmp_path.resolve() / "binary"
    binary.write_bytes(b"inert")
    original = muse._sha
    monkeypatch.setattr(muse, "_sha", lambda p: muse.NATIVE_SHA256 if p == binary else original(p))
    return muse.prepare(
        prompt="one named question", workspace=workspace, binary=binary, basis_sha256="a" * 64
    )


class Gate:
    def __init__(self):
        self.checks = 0
        self.pids = []

    def check(self, plan):
        self.checks += 1

    def environment(self, plan):
        (plan.workspace / "prompt.txt").write_bytes(plan.prompt)
        return {"PATH": "/usr/bin:/bin"}

    def started(self, pid):
        self.pids.append(("started", pid))

    def finished(self, pid):
        self.pids.append(("finished", pid))


def capture_fixture(monkeypatch, *, output=None, cleanup=True, problem=None):
    calls = []

    def inert(argv, prompt, cwd, env, **kw):
        calls.append((argv, prompt, cwd, env, kw["cap"]))
        kw["on_started"](123)
        if cleanup:
            kw["on_finished"](123)
        return {
            "stdout": native() if output is None else output,
            "stderr": b"",
            "exit_code": 0,
            "problem": problem,
        }

    monkeypatch.setattr(launcher, "_capture", inert)
    return calls


def test_real_capture_seam_and_native_terminal(plan, monkeypatch):
    calls = capture_fixture(monkeypatch)
    gate = Gate()
    result = muse.capture(plan, gate, deadline=time.monotonic() + 10)
    assert result["final"] == "answer" and result["cleanup_terminal"]
    assert gate.checks == 2 and gate.pids == [("started", 123), ("finished", 123)]
    assert len(calls) == 1 and calls[0][4] == muse.MAX_RAW
    assert "--preset" in calls[0][0] and "--disable-shell" in calls[0][0]
    assert result["served_identity"] is None and result["action_authority"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        {"model": "muse-wonder-1.0"},
        {"effort": "low"},
        {"profile_id": "muse-wonder-1.0"},
        {"role": "constrained_repair"},
        {"basis_sha256": "b" * 63},
        {"argv": ("/usr/bin/true",)},
        {"source_sha256": "0" * 64},
        {"parser_sha256": "0" * 64},
        {"capture_sha256": "0" * 64},
    ],
)
def test_mutation_refuses_before_root_connection(plan, monkeypatch, mutation):
    calls = capture_fixture(monkeypatch)
    gate = Gate()
    with pytest.raises((ValueError, OSError)):
        muse.capture(replace(plan, **mutation), gate, deadline=time.monotonic() + 10)
    assert not calls and gate.checks == 0 and not (plan.workspace / "prompt.txt").exists()


@pytest.mark.parametrize("problem,cleanup", [("timeout", True), (None, False)])
def test_unknown_preserves_raw_no_completion(plan, monkeypatch, problem, cleanup):
    capture_fixture(monkeypatch, problem=problem, cleanup=cleanup)
    result = muse.capture(plan, Gate(), deadline=time.monotonic() + 10)
    assert result["final"] is None and result["capture"]["stdout"] == native()
    assert result["retry"] is False


@pytest.mark.parametrize(
    "output",
    [
        b"{",
        b'{"x":1,"x":2}',
        native() + native(),
        native("x" * (muse.MAX_FINAL + 1)),
        b'{"payload_type":"tool.result"}\n',
    ],
)
def test_native_rejection_preserves_raw(plan, monkeypatch, output):
    capture_fixture(monkeypatch, output=output)
    result = muse.capture(plan, Gate(), deadline=time.monotonic() + 10)
    assert result["final"] is None and result["capture"]["stdout"] == output
    assert result["problem"] == "native_terminal_rejected"


def test_root_refusal_precedes_capture_and_credentials(plan, monkeypatch):
    calls = capture_fixture(monkeypatch)

    class Denied(Gate):
        def check(self, plan):
            raise ValueError("original_lease_not_held")

    with pytest.raises(ValueError, match="original_lease_not_held"):
        muse.capture(plan, Denied(), deadline=time.monotonic() + 10)
    assert not calls and not (plan.workspace / "prompt.txt").exists()


def test_deadline_limit_before_admission(plan, monkeypatch):
    calls = capture_fixture(monkeypatch)
    gate = Gate()
    with pytest.raises(ValueError, match="remaining deadline"):
        muse.capture(plan, gate, deadline=time.monotonic() + 46)
    assert not calls and gate.checks == 0


def test_connection_setup_cannot_replace_original_workspace(plan, monkeypatch):
    calls = capture_fixture(monkeypatch)

    class Replaced(Gate):
        def environment(self, current):
            current.workspace.rename(current.workspace.with_name("original"))
            current.workspace.mkdir(mode=0o700)
            return super().environment(current)

    with pytest.raises(ValueError, match="workspace custody changed"):
        muse.capture(plan, Replaced(), deadline=time.monotonic() + 10)
    assert not calls


@pytest.mark.parametrize("kind", ["symlink", "permissions"])
def test_post_connection_private_custody_mutations_refused(plan, monkeypatch, kind):
    calls = capture_fixture(monkeypatch)

    class Changed(Gate):
        def environment(self, current):
            result = super().environment(current)
            if kind == "permissions":
                current.workspace.chmod(0o755)
            else:
                original = current.workspace.with_name("original")
                current.workspace.rename(original)
                current.workspace.symlink_to(original, target_is_directory=True)
            return result

    with pytest.raises(ValueError, match="workspace custody changed"):
        muse.capture(plan, Changed(), deadline=time.monotonic() + 10)
    assert not calls


def model_response_native(text="answer"):
    rows = [json.loads(line) for line in native(text).decode().splitlines()]
    template = rows[2]

    def event(kind, payload):
        return {**template, "payload_type": kind, "payload": payload}

    common = {
        "command_id": "c",
        "run_stream": {"kind": "run", "id": "c"},
        "task_id": "t",
        "task_stream": {"kind": "task", "id": "t"},
    }
    lifecycle = [event("task.stream.linked", {**common, "kind": "task_stream_linked"})]
    for kind, extra in [
        ("proposed", {"task_kind": "model.meta.response"}),
        ("accepted", {}),
        ("scheduled", {"idempotency_key": "native-model-response-key"}),
        (
            "side_effect_intent",
            {
                "idempotency_key": "native-model-response-key",
                "operation": "model.meta.response",
                "parent_task_id": None,
                "cancellation_handle": None,
                "policy_decision": "not_applicable",
            },
        ),
        ("started", {}),
        ("completed", {}),
    ]:
        lifecycle.append(
            event(
                "task.lifecycle." + kind,
                {
                    **common,
                    "kind": "task_lifecycle",
                    "event": {"kind": kind, "task_id": "t", **extra},
                },
            )
        )
    return ("\n".join(json.dumps(row) for row in rows[:-1] + lifecycle + rows[-1:]) + "\n").encode()


def test_typed_original_model_response_lifecycle_is_not_a_tool_action():
    assert muse._final(model_response_native()) == "answer"


@pytest.mark.parametrize(
    "damage",
    [
        "operation",
        "task_kind",
        "task_id",
        "task_stream",
        "run",
        "command",
        "session",
        "idempotency",
        "parent",
        "cancellation",
        "policy",
        "extra",
        "missing_proposed",
        "missing_scheduled",
        "missing_completed",
        "duplicate_intent",
        "tool_result",
    ],
)
def test_model_response_exception_is_closed_typed_and_joined(damage):
    rows = [json.loads(line) for line in model_response_native().decode().splitlines()]
    side = next(row for row in rows if row["payload_type"] == "task.lifecycle.side_effect_intent")
    proposed = next(row for row in rows if row["payload_type"] == "task.lifecycle.proposed")
    if damage == "operation":
        side["payload"]["event"]["operation"] = "tool.shell.execute"
    elif damage == "task_kind":
        proposed["payload"]["event"]["task_kind"] = "tool.shell.execute"
    elif damage == "task_id":
        side["payload"]["event"]["task_id"] = "foreign"
    elif damage == "task_stream":
        side["payload"]["task_stream"]["id"] = "foreign"
    elif damage == "run":
        side["payload"]["run_stream"]["id"] = "foreign"
    elif damage == "command":
        side["payload"]["command_id"] = "foreign"
    elif damage == "session":
        side["stream"]["id"] = "foreign"
    elif damage == "idempotency":
        side["payload"]["event"]["idempotency_key"] = "foreign"
    elif damage == "parent":
        side["payload"]["event"]["parent_task_id"] = "foreign"
    elif damage == "cancellation":
        side["payload"]["event"]["cancellation_handle"] = "execute-tool"
    elif damage == "policy":
        side["payload"]["event"]["policy_decision"] = "allowed"
    elif damage == "extra":
        side["payload"]["event"]["tool_call"] = {"name": "shell"}
    elif damage.startswith("missing_"):
        rows = [
            row
            for row in rows
            if row["payload_type"] != "task.lifecycle." + damage.removeprefix("missing_")
        ]
    elif damage == "duplicate_intent":
        rows.insert(rows.index(side) + 1, side)
    elif damage == "tool_result":
        rows.insert(-1, {**side, "payload_type": "tool.result"})
    with pytest.raises(ValueError):
        muse._final(("\n".join(json.dumps(r) for r in rows) + "\n").encode())


def test_malformed_accepted_session_does_not_escape_closed_parser():
    rows = [json.loads(line) for line in model_response_native().decode().splitlines()]
    rows[0]["stream"] = "malformed"
    with pytest.raises(ValueError):
        muse._final(("\n".join(json.dumps(r) for r in rows) + "\n").encode())


def test_runtime_does_not_use_or_modify_qualified_main_adapter(monkeypatch):
    import adapters

    monkeypatch.setattr(
        adapters, "muse_terminal_text", lambda *_: pytest.fail("qualified main adapter reached")
    )
    assert muse._final(model_response_native()) == "answer"


def test_runtime_plan_pins_complete_dedicated_parser_source(plan):
    from pathlib import Path

    import closer_muse_terminal_parser as terminal_parser

    assert plan.parser_sha256 == muse._sha(Path(terminal_parser.__file__))
