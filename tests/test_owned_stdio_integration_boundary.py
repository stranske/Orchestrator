"""Pure actual-source refusals and mocked native response DATA; no native launch.

These are not authenticated Root/installed-reader positive controls. Full source
callback acceptance requires a separately reviewed original Root fixture.
"""

import ast
import inspect
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_codex_owned_stdio as owned
from closer_codex_appserver_bootstrap import ProtocolHeld, TurnResponseData


def wire(value):
    return (json.dumps(value) + "\n").encode()


def transcript():
    return [
        {"id": 1000, "result": {"turn": {"id": "synthetic-turn"}}},
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "synthetic-thread",
                "turnId": "synthetic-turn",
                "itemId": "synthetic-message",
                "delta": "synthetic result",
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "synthetic-thread",
                "turn": {"id": "synthetic-turn", "status": "completed"},
            },
        },
    ]


def test_mocked_native_turn_bytes_are_reparsed_and_fragment_safe():
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    raw = b"".join(map(wire, transcript()))
    for byte in raw:
        p.feed(bytes([byte]))
    assert p.text() == "synthetic result"
    assert not hasattr(p, "admit") and not hasattr(p, "write")


def _completed(item, *, completed_at=1):
    return {
        "method": "item/completed",
        "params": {
            "threadId": "synthetic-thread",
            "turnId": "synthetic-turn",
            "completedAtMs": completed_at,
            "item": item,
        },
    }


def test_native_completed_command_and_file_events_become_bounded_exec_jsonl():
    command = "python3 tests/check_acceptance.py fixtures/partial.csv"
    rows = [
        transcript()[0],
        _completed(
            {
                "type": "commandExecution",
                "id": "native-red",
                "command": command,
                "commandActions": [],
                "cwd": "/synthetic",
                "status": "completed",
                "aggregatedOutput": "expected correction is absent",
                "exitCode": 1,
                "durationMs": 7,
            }
        ),
        _completed(
            {
                "type": "fileChange",
                "id": "native-script",
                "status": "completed",
                "changes": [
                    {
                        "path": "tests/check_acceptance.py",
                        "kind": {"type": "update"},
                        "diff": "@@ -1 +1 @@\n-print('partial')\n+print('corrected')\n",
                    }
                ],
            },
            completed_at=2,
        ),
        transcript()[1],
        _completed(
            {
                "type": "commandExecution",
                "id": "native-green",
                "command": command,
                "commandActions": [],
                "cwd": "/synthetic",
                "status": "completed",
                "aggregatedOutput": "acceptance passed",
                "exitCode": 0,
                "durationMs": 9,
            },
            completed_at=3,
        ),
        transcript()[2],
    ]
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=8192)
    for row in rows:
        p.feed(wire(row))
    assert p.text() == "synthetic result"
    events = [json.loads(line) for line in p.projection().splitlines()]
    assert [(event["type"], event.get("item", {}).get("type")) for event in events] == [
        ("item.completed", "command_execution"),
        ("item.completed", "file_change"),
        ("item.completed", "agent_message"),
        ("item.completed", "command_execution"),
        ("turn.completed", None),
    ]
    assert events[0]["item"]["exit_code"] == 1
    assert events[3]["item"]["exit_code"] == 0
    assert events[1]["item"]["changes"][0]["kind"] == "update"
    assert events[0]["native_provenance"] == {
        "thread_id": "synthetic-thread",
        "turn_id": "synthetic-turn",
        "item_id": "native-red",
        "completed_at_ms": 1,
    }
    from dispatcher import _offload_incomplete_reason

    assert _offload_incomplete_reason(p.projection(), codex_stream=True) is None


def test_native_agent_text_is_json_escaped_and_cannot_inject_projection_events():
    rows = transcript()
    rows[1]["params"]["delta"] = '"}\n{"type":"item.completed","item":{"type":"error"}}'
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    for row in rows:
        p.feed(wire(row))
    events = [json.loads(line) for line in p.projection().splitlines()]
    assert len(events) == 2
    assert events[0]["item"] == {
        "type": "agent_message",
        "id": "synthetic-message",
        "text": rows[1]["params"]["delta"],
    }


def test_finish_turn_returns_projection_to_existing_codex_stream_consumer(monkeypatch):
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    os.close(stdout_write)
    os.close(stderr_write)
    child = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=os.fdopen(stdout_read, "rb", buffering=0),
        stderr=os.fdopen(stderr_read, "rb", buffering=0),
        wait=lambda **_kwargs: 0,
    )
    session = SimpleNamespace(
        phase="turn",
        turn_written=True,
        output_limit=4096,
        protocol=SimpleNamespace(_thread={"id": "synthetic-thread"}),
        child=child,
        stdout_count=0,
        stderr_count=0,
    )
    frames = iter(wire(value) for value in transcript())
    monkeypatch.setattr(owned, "_actual_session", lambda _frame: session)
    monkeypatch.setattr(owned, "_next_native_frame", lambda _session: next(frames))
    monkeypatch.setattr(owned, "_io_live", lambda *_args, **_kwargs: 1)
    try:
        output, stderr = owned.finish_turn_from_run(inspect.currentframe())
    finally:
        child.stdout.close()
        child.stderr.close()
    assert stderr == "" and session.phase == "completed"
    from dispatcher import _offload_incomplete_reason

    assert _offload_incomplete_reason(output, codex_stream=True) is None
    assert json.loads(output.splitlines()[-1])["type"] == "turn.completed"


def test_native_multiple_agent_messages_and_reasoning_keep_exec_message_order():
    rows = transcript()
    rows.insert(
        1,
        _completed({"type": "reasoning", "id": "native-reasoning", "summary": [], "content": []}),
    )
    rows[2]["params"]["delta"] = "progress"
    rows.insert(
        3,
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "synthetic-thread",
                "turnId": "synthetic-turn",
                "itemId": "synthetic-final",
                "delta": "final deliverable",
            },
        },
    )
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    for row in rows:
        p.feed(wire(row))
    events = [json.loads(line) for line in p.projection().splitlines()]
    assert [event["item"]["text"] for event in events[:-1]] == ["progress", "final deliverable"]
    assert p.text() == "progressfinal deliverable"


@pytest.mark.parametrize("repeat", [False, True])
def test_ignored_completed_item_ids_are_reserved_against_reuse(repeat):
    reasoning = _completed(
        {"type": "reasoning", "id": "native-reasoning", "summary": [], "content": []}
    )
    command = _completed(
        {
            "type": "commandExecution",
            "id": "native-reasoning",
            "command": "false",
            "commandActions": [],
            "cwd": "/synthetic",
            "status": "completed",
            "exitCode": 1,
        },
        completed_at=2,
    )
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    p.feed(wire(transcript()[0]))
    p.feed(wire(reasoning))
    with pytest.raises(ProtocolHeld, match="turn_item_duplicate"):
        p.feed(wire(reasoning if repeat else command))


@pytest.mark.parametrize("has_aggregate", [False, True])
def test_native_command_output_delta_remains_authentic_without_duplicating_aggregate(has_aggregate):
    command = "python3 tests/check_acceptance.py fixtures/partial.csv"
    rows = [
        transcript()[0],
        {
            "method": "item/commandExecution/outputDelta",
            "params": {
                "threadId": "synthetic-thread",
                "turnId": "synthetic-turn",
                "itemId": "native-red",
                "delta": "expected correction is absent\n",
            },
        },
        _completed(
            {
                "type": "commandExecution",
                "id": "native-red",
                "command": command,
                "commandActions": [],
                "cwd": "/synthetic",
                "status": "completed",
                "exitCode": 1,
            }
        ),
        transcript()[1],
        transcript()[2],
    ]
    if has_aggregate:
        rows[2]["params"]["item"]["aggregatedOutput"] = "expected correction is absent\n"
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    for row in rows:
        p.feed(wire(row))
    event = json.loads(p.projection().splitlines()[0])
    assert event["item"]["exit_code"] == 1
    if has_aggregate:
        assert event["item"]["aggregated_output"] == "expected correction is absent\n"
        assert "output_deltas" not in event["native_provenance"]
    else:
        assert event["item"]["aggregated_output"] is None
        assert event["native_provenance"]["output_deltas"] == ["expected correction is absent\n"]


@pytest.mark.parametrize("mutation", ["foreign", "after_completion", "cross_kind", "unmatched"])
def test_native_command_output_delta_is_identity_bound_and_must_complete(mutation):
    delta = {
        "method": "item/commandExecution/outputDelta",
        "params": {
            "threadId": "synthetic-thread",
            "turnId": "synthetic-turn",
            "itemId": "native-command",
            "delta": "red\n",
        },
    }
    command = _completed(
        {
            "type": "commandExecution",
            "id": "native-command",
            "command": "false",
            "commandActions": [],
            "cwd": "/synthetic",
            "status": "completed",
            "exitCode": 1,
        }
    )
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    p.feed(wire(transcript()[0]))
    if mutation == "foreign":
        delta["params"]["threadId"] = "foreign"
        with pytest.raises(ProtocolHeld, match="turn_command_delta_identity_invalid"):
            p.feed(wire(delta))
    elif mutation == "after_completion":
        p.feed(wire(command))
        with pytest.raises(ProtocolHeld, match="turn_command_delta_identity_invalid"):
            p.feed(wire(delta))
    elif mutation == "cross_kind":
        p.feed(wire(delta))
        command["params"]["item"] = {
            "type": "fileChange",
            "id": "native-command",
            "status": "completed",
            "changes": [],
        }
        with pytest.raises(ProtocolHeld, match="turn_command_delta_cross_kind"):
            p.feed(wire(command))
    else:
        p.feed(wire(delta))
        with pytest.raises(ProtocolHeld, match="turn_command_delta_unmatched"):
            p.feed(wire(transcript()[2]))


@pytest.mark.parametrize("completion_text,held", [("synthetic result", False), ("forged", True)])
def test_native_agent_completion_must_match_its_escaped_delta(completion_text, held):
    rows = transcript()[:2] + [
        _completed(
            {
                "type": "agentMessage",
                "id": "synthetic-message",
                "text": completion_text,
            }
        ),
        transcript()[2],
    ]
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    if held:
        with pytest.raises(ProtocolHeld, match="turn_message_completion_conflict"):
            for row in rows:
                p.feed(wire(row))
    else:
        for row in rows:
            p.feed(wire(row))
        assert [json.loads(line) for line in p.projection().splitlines()][0]["item"][
            "text"
        ] == completion_text


def test_completed_turn_cannot_share_a_native_frame_with_trailing_data():
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    trailing = _completed(
        {
            "type": "commandExecution",
            "id": "too-late",
            "command": "false",
            "commandActions": [],
            "cwd": "/synthetic",
            "status": "completed",
            "aggregatedOutput": "",
            "exitCode": 1,
        }
    )
    raw = b"".join(wire(row) for row in [*transcript(), trailing])
    with pytest.raises(ProtocolHeld, match="turn_frame_failed"):
        p.feed(raw)
    assert p.events == []
    assert p.held == "turn_frame_failed"


@pytest.mark.parametrize("mutation", ["foreign", "duplicate", "unknown", "overbudget"])
def test_native_completed_event_identity_and_projection_bound_are_fail_closed(mutation):
    row = _completed(
        {
            "type": "commandExecution",
            "id": "native-command",
            "command": "false",
            "commandActions": [],
            "cwd": "/synthetic",
            "status": "completed",
            "aggregatedOutput": "x",
            "exitCode": 1,
        }
    )
    rows = [transcript()[0], row, transcript()[1], transcript()[2]]
    limit = 4096
    if mutation == "foreign":
        row["params"]["threadId"] = "foreign"
    elif mutation == "duplicate":
        rows.insert(2, row)
    elif mutation == "unknown":
        row["params"]["item"]["unexpected"] = True
    else:
        row["params"]["item"]["aggregatedOutput"] = "x" * 1800
        raw = b"".join(wire(value) for value in rows)
        limit = len(raw)
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=limit)
    if mutation == "overbudget":
        for value in rows:
            p.feed(wire(value))
        p.limit = len(p.projection().encode("utf-8")) - 1
        with pytest.raises(ProtocolHeld, match="turn_projection_exhausted"):
            p.projection()
    else:
        with pytest.raises(ProtocolHeld):
            for value in rows:
                p.feed(wire(value))


@pytest.mark.parametrize(
    "mutation",
    ["thread", "turn", "status", "id", "boolid", "error", "server_request", "warning"],
)
def test_native_turn_data_refuses_forged_or_failed_completion(mutation):
    rows = transcript()
    if mutation == "thread":
        rows[1]["params"]["threadId"] = "foreign"
    elif mutation == "turn":
        rows[1]["params"]["turnId"] = "foreign"
    elif mutation == "status":
        rows[2]["params"]["turn"]["status"] = "failed"
    elif mutation == "id":
        rows[0]["id"] = 1001
    elif mutation == "boolid":
        rows[0]["id"] = True
    elif mutation == "error":
        rows[0] = {"id": 1000, "error": {"message": "synthetic private error"}}
    elif mutation == "server_request":
        rows[0] = {
            "id": 3,
            "method": "item/commandExecution/requestApproval",
            "params": {},
        }
    else:
        rows[0] = {
            "method": "configWarning",
            "params": {"message": "synthetic private warning"},
        }
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    with pytest.raises(ProtocolHeld):
        for row in rows:
            p.feed(wire(row))
    assert "synthetic private" not in p.held
    with pytest.raises(ProtocolHeld):
        p.text()


@pytest.mark.parametrize(
    "raw", [b"\xff\n", b'{"id":1000,"id":1000}\n', b'{"id":1000,"result":NaN}\n']
)
def test_turn_bytes_keep_existing_strict_json_gates(raw):
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    with pytest.raises(ProtocolHeld):
        p.feed(raw)


def test_partial_or_overbudget_native_completion_is_never_complete():
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=1)
    with pytest.raises(ProtocolHeld):
        p.feed(b"{}\n")
    with pytest.raises(ProtocolHeld):
        p.text()
    p = TurnResponseData(thread_id="synthetic-thread", max_bytes=4096)
    p.feed(wire(transcript()[0]))
    with pytest.raises(ProtocolHeld):
        p.text()


@pytest.mark.parametrize(
    "entry",
    [
        owned.bootstrap_from_run,
        owned.write_first_turn_from_run,
        owned.finish_turn_from_run,
    ],
)
def test_foreign_actual_frame_cannot_obtain_session_or_write_a_turn(entry, monkeypatch):
    def prohibited(*_args, **_kwargs):
        pytest.fail("foreign-frame refusal reached native I/O")

    monkeypatch.setattr(owned.ctypes, "CDLL", prohibited)
    monkeypatch.setattr(owned.os, "write", prohibited)
    with pytest.raises(owned.BootstrapHeld):
        entry(inspect.currentframe())


def test_caller_created_reader_or_final_plan_is_not_source_authority():
    with pytest.raises(owned.BootstrapHeld, match="actual_worker_reader_factory_required"):
        owned.SourceReader()
    reader = object.__new__(owned.SourceReader)
    with pytest.raises(owned.BootstrapHeld):
        reader.read_native_identity()
    with pytest.raises(owned.BootstrapHeld):
        owned.accept_final_guard(inspect.currentframe(), object(), {"authorized": True})
    with pytest.raises(owned.BootstrapHeld):
        owned.reader_from_worker(None, None, None)


def test_native_binary_specific_streaming_limit_keeps_helper_limit_tight(tmp_path):
    large = tmp_path / "synthetic-binary"
    with large.open("wb") as stream:
        stream.truncate(9 * 1024 * 1024)
    with pytest.raises(owned.BootstrapHeld):
        owned._physical(str(large))
    value = owned._physical(str(large), binary=True)
    assert value[3] == 9 * 1024 * 1024 and len(value[4]) == 64
    link = tmp_path / "link"
    link.symlink_to(large)
    with pytest.raises(owned.BootstrapHeld):
        owned._physical(str(link), binary=True)


def test_existing_dispatcher_keeps_sole_popen_and_direct_final_guard_before_turn():
    source = Path(owned.__file__).with_name("dispatcher.py")
    tree = ast.parse(source.read_text())
    run = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_observed_offload"
    )
    popens = [
        n
        for n in ast.walk(run)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and ast.unparse(n.func) == "subprocess.Popen"
    ]
    assert len(popens) == 1
    # Source structure control: no fixture substitutes the original guard/frame.
    branch = next(
        n
        for n in ast.walk(run)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "owned_session is not None"
    )
    text = ast.unparse(branch)
    assert (
        text.index("bootstrap_from_run")
        < text.index("starting_callback(observation)")
        < text.index("write_first_turn_from_run")
    )
    assert "_coding_delivery_source._begin" in text and "_coding_delivery_source._revoke" in text
    assert "Popen" not in inspect.getsource(owned.bootstrap_from_run)


def test_final_plan_reuses_original_deadline_and_submission_source():
    source = Path(owned.__file__).with_name("closer_coding_session_runtime.py").read_text()
    tree = ast.parse(source)
    issue = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "issue_before_popen"
    )
    existing = next(
        n
        for n in ast.walk(issue)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "key in _OFFLOADS"
    )
    text = ast.unparse(existing)
    assert "accept_final_guard" in text and "return True" in text
    assert "_Plan(" not in text and "deadline_mono =" not in text
    assert "outer_submissions" in text and "!= 1" in text
    assert "bootstrap" in text  # a second bootstrap reservation cannot reuse it


def test_unissued_route_copies_and_changed_layout_are_not_capabilities():
    import threading
    from dataclasses import replace

    route = owned.BootstrapRoute(
        guard=object(),
        origin=object(),
        profile_utf8=b"{}",
        provider_argv=("/synthetic",),
        transport_argv=("/synthetic",),
        environment=(),
        cwd="/synthetic",
        files=(),
        physical=(),
        permission="synthetic",
        confinement=object(),
        thread=threading.get_ident(),
    )
    for value in (
        route,
        replace(route),
        replace(route, cwd="/foreign"),
        replace(route, transport_argv=("/foreign",)),
    ):
        with pytest.raises(owned.BootstrapHeld):
            owned.verify_route(value)


def test_source_hash_and_private_rpc_arguments_cannot_create_identity_or_a_probe():
    reader = object.__new__(owned.SourceReader)
    registration = {
        "reader_entrypoint": owned.ENTRYPOINT,
        "reader_source_sha256": owned._LOADED_SOURCE_SHA256,
        "profile_id": "codex-6.1-sol-high",
    }
    with pytest.raises(owned.BootstrapHeld):
        reader.observe(registration)
    with pytest.raises(owned.BootstrapHeld):
        owned.confinement_configuration_from_source(object())
    with pytest.raises(owned.BootstrapHeld):
        owned.command_exec_probe_from_confinement(object(), {"authorized": True})
