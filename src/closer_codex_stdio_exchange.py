"""Finite stdio DATA mechanics for the existing owned transport.

No Popen, native reader, callback, admission issuer or turn writer exists here.
A future dispatcher-owned session must bind these bytes to its actual child and
source plan. Constructing this object never establishes that binding.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from closer_codex_appserver_bootstrap import BootstrapProtocol, ProtocolHeld

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class StdioHeld(RuntimeError):
    pass


class BootstrapExchange:
    """One outstanding request and bounded partial writes; no inference path."""

    def __init__(self, protocol):
        if type(protocol) is not BootstrapProtocol:
            raise StdioHeld("exact_protocol_required")
        self._protocol = protocol
        self._pending = b""
        self._offered = 0
        self._held = None
        self._written = 0
        self._received = 0
        self._frames = 0

    def _hold(self, code):
        self._held = code
        self._pending = b""
        self._offered = 0
        raise StdioHeld(code)

    def _live(self, now):
        if self._held is not None:
            raise StdioHeld(self._held)
        try:
            self._protocol._live(now)
        except ProtocolHeld:
            self._hold("protocol_unavailable")

    def request(self, *, now):
        self._live(now)
        if self._pending or self._offered:
            self._hold("previous_write_incomplete")
        try:
            self._pending = self._protocol.next_frame(now=now)
        except ProtocolHeld:
            self._hold("protocol_unavailable")
        self._frames += 1

    def writable(self, limit, *, now):
        self._live(now)
        if type(limit) is not int or not 0 < limit <= 65536:
            self._hold("write_slice_invalid")
        if self._offered:
            self._hold("write_acknowledgment_required")
        self._offered = min(limit, len(self._pending))
        return self._pending[: self._offered]

    def written(self, count, *, now):
        self._live(now)
        if type(count) is not int or not 0 < count <= self._offered:
            self._hold("write_count_invalid")
        self._pending = self._pending[count:]
        self._written += count
        self._offered = 0

    def received(self, raw, *, now):
        self._live(now)
        if self._pending or self._offered:
            self._hold("response_before_request_written")
        if type(raw) is not bytes or not raw:
            self._hold("native_eof_or_invalid_bytes")
        self._received += len(raw)
        try:
            self._protocol.feed(raw, now=now)
        except ProtocolHeld:
            self._hold("protocol_unavailable")

    def end_of_stream(self, *, now):
        self._live(now)
        self._hold("native_eof_before_source_completion")

    def snapshot(self):
        # Counts contain no raw account/config/error contents or model input.
        return {
            "schema": "codex-stdio-exchange-data/v1",
            "authority": False,
            "transport_binding": "unissued",
            "final_admission": "unissued",
            "turn_writes": 0,
            "written_protocol_bytes": self._written,
            "received_protocol_bytes": self._received,
            "requested_frames": self._frames,
            "pending_write_bytes": len(self._pending),
            "held_code": self._held,
        }
