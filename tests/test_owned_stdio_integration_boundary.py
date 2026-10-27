"""Pure actual-source refusals and mocked native response DATA; no native launch.

These are not authenticated Root/installed-reader positive controls. Full source
callback acceptance requires a separately reviewed original Root fixture.
"""

import ast
import inspect
import json
from pathlib import Path

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
