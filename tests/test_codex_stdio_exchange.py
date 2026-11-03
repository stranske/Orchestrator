import json

import pytest

from closer_codex_appserver_bootstrap import BootstrapProtocol
from closer_codex_stdio_exchange import BootstrapExchange, StdioHeld


def exchange():
    return BootstrapExchange(
        BootstrapProtocol(
            model="codex-6.1-sol",
            provider="openai",
            effort="high",
            permission_profile="synthetic",
            cwd="/synthetic",
            workspace_roots=["/synthetic"],
            deadline=10.0,
            max_frame_bytes=4096,
            max_total_bytes=8192,
            max_notifications=2,
        )
    )


def drain(e, limit=7):
    raw = b""
    while e.snapshot()["pending_write_bytes"]:
        chunk = e.writable(limit, now=1.0)
        # Actual nonblocking writes may consume less than the offered slice.
        taken = max(1, len(chunk) // 2)
        raw += chunk[:taken]
        e.written(taken, now=1.0)
    return json.loads(raw)


def test_partial_writes_and_fragmented_response_preserve_exact_frame():
    e = exchange()
    e.request(now=1.0)
    frame = drain(e)
    assert frame["method"] == "initialize"
    assert frame["params"]["capabilities"]["explicitGatewayOauth"] is True
    raw = (json.dumps({"id": frame["id"], "result": {}}) + "\n").encode()
    for byte in raw:
        e.received(bytes([byte]), now=1.0)
    e.request(now=1.0)
    assert drain(e) == {"method": "initialized"}
    assert e.snapshot()["turn_writes"] == 0


@pytest.mark.parametrize("action", ["request", "response", "double_offer", "bad_ack"])
def test_incomplete_write_cannot_be_replayed_or_skipped(action):
    e = exchange()
    e.request(now=1.0)
    with pytest.raises(StdioHeld):
        if action == "request":
            e.request(now=1.0)
        elif action == "response":
            e.received(b"{}\n", now=1.0)
        elif action == "double_offer":
            e.writable(5, now=1.0)
            e.writable(5, now=1.0)
        else:
            e.written(1, now=1.0)
    assert e.snapshot()["turn_writes"] == 0


@pytest.mark.parametrize("count", [0, -1, True, 6])
def test_write_count_must_be_actual_positive_offered_subset(count):
    e = exchange()
    e.request(now=1.0)
    e.writable(5, now=1.0)
    with pytest.raises(StdioHeld, match="write_count_invalid"):
        e.written(count, now=1.0)


@pytest.mark.parametrize("boundary", ["request", "write", "ack", "read", "eof"])
def test_original_deadline_applies_to_every_io_boundary(boundary):
    e = exchange()
    e.request(now=1.0)
    with pytest.raises(StdioHeld, match="protocol_unavailable"):
        if boundary == "request":
            e.request(now=10.0)
        elif boundary == "write":
            e.writable(5, now=10.0)
        elif boundary == "ack":
            e.written(1, now=10.0)
        elif boundary == "read":
            e.received(b"{}\n", now=10.0)
        else:
            e.end_of_stream(now=10.0)


def test_warning_and_eof_do_not_expose_native_text_or_clear_hold():
    e = exchange()
    e.request(now=1.0)
    drain(e)
    with pytest.raises(StdioHeld):
        e.received(
            b'{"method":"configWarning","params":{"message":"private synthetic warning"}}\n',
            now=1.0,
        )
    assert "private synthetic warning" not in json.dumps(e.snapshot())
    with pytest.raises(StdioHeld):
        e.request(now=1.0)
    assert e.snapshot()["turn_writes"] == 0
    e = exchange()
    with pytest.raises(StdioHeld, match="native_eof_before_source_completion"):
        e.end_of_stream(now=1.0)


def test_exchange_has_no_process_or_inference_authority():
    e = exchange()
    assert e.snapshot()["authority"] is False
    assert e.snapshot()["transport_binding"] == "unissued"
    assert e.snapshot()["final_admission"] == "unissued"
    assert not hasattr(e, "turn") and not hasattr(e, "admit")
    with pytest.raises(StdioHeld, match="exact_protocol_required"):
        BootstrapExchange({"authority": True})
