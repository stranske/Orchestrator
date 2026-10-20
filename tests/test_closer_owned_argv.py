"""Byte-exact owned process argv only; real fixtures never invoke an AI provider."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

import closer_mechanisms as mechanisms


def test_real_owned_multiline_unicode_long_argv_and_cleanup_identity():
    argv = [
        sys.executable,
        "-c",
        "import time; time.sleep(20)",
        "line one\nline two — 雪",
        "x" * 16000,
        "",
        "space  spaced",
    ]
    child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 8
        identity = mechanisms._identity(child.pid, argv, deadline=deadline)
        assert identity["argv_sha256"] == mechanisms._argv_digest(argv)
        rows = mechanisms._lineage(child.pid, deadline=deadline)
        assert rows[0]["argv_sha256"] == identity["argv_sha256"]
        assert "command" not in rows[0] and "argv" not in rows[0]
        assert mechanisms._identity(child.pid, argv, deadline=deadline) == identity
        with pytest.raises(mechanisms.MechanismError, match="does not match"):
            mechanisms._identity(child.pid, argv[:-1] + ["wrong"], deadline=deadline)
    finally:
        child.terminate()
        child.wait(timeout=2)


def test_darwin_parser_hashes_only_exact_argc_not_opaque_trailing_bytes():
    args = [b"/offline/python", "multi\n—雪".encode(), b"", b"long" * 1000]
    raw = (
        len(args).to_bytes(4, sys.byteorder, signed=True)
        + b"/offline/python\0\0\0"
        + b"\0".join(args)
        + b"\0"
    )
    sentinel = b"OPAQUE_TEST_SENTINEL_MUST_NOT_ENTER_PROOF=not-a-credential\0"
    buffer = ctypes.create_string_buffer(raw + sentinel)
    proof = mechanisms._darwin_args_prefix(buffer, len(buffer))
    assert proof == {
        "argc": len(args),
        "argv_sha256": hashlib.sha256(b"\0".join(args) + b"\0").hexdigest(),
    }
    assert "OPAQUE_TEST" not in json.dumps(proof)
    assert proof["argv_sha256"] != hashlib.sha256(raw + sentinel).hexdigest()


def test_unowned_chain_never_requests_argv(monkeypatch):
    monkeypatch.setattr(
        mechanisms,
        "_metadata",
        lambda pid, deadline: {"pid": pid, "parent_pid": 1, "started": "offline"},
    )
    monkeypatch.setattr(
        mechanisms, "_argv_proof", lambda *a, **kw: pytest.fail("unowned argv read")
    )
    with pytest.raises(mechanisms.MechanismError, match="not descended"):
        mechanisms._identity(1234, ["offline"], deadline=time.monotonic() + 1)


def test_argv_permission_denial_has_no_fallback_or_prompt_in_error(monkeypatch):
    row = {"pid": 1234, "parent_pid": os.getpid(), "started": "offline"}
    monkeypatch.setattr(mechanisms, "_owned_chain", lambda *a, **kw: [row])
    calls = []

    def denied(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            78,
            json.dumps(
                {"error": {"type": "PermissionError", "errno": 1, "reason": "offline denied"}}
            ),
            "",
        )

    monkeypatch.setattr(mechanisms.subprocess, "run", denied)
    with pytest.raises(PermissionError, match="offline denied"):
        mechanisms._identity(
            1234, ["offline", "prompt-is-not-logged"], deadline=time.monotonic() + 1
        )
    assert len(calls) == 1
    assert "prompt-is-not-logged" not in repr(calls)


@pytest.mark.parametrize("malformed", [False, True])
def test_mac_sysctl_opaque_extras_zeroed_on_success_and_parse_failure(monkeypatch, malformed):
    args = b"/offline/python\0multi\nbytes\0"
    argc = 2 if not malformed else 0
    raw = argc.to_bytes(4, sys.byteorder, signed=True) + b"/offline/python\0\0" + args
    opaque = b"OPAQUE_FIXTURE_ONLY_DO_NOT_INSPECT=tail\0"

    class API:
        buffer = None

        def __call__(self, mib, count, output, length, _new, _size):
            if count == 2:
                assert list(mib) == [1, 8]
                ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = 4096
            else:
                assert list(mib) == [1, 49, 1234]
                self.buffer = output
                ctypes.memmove(output, raw + opaque, len(raw + opaque))
                ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = len(raw + opaque)
            return 0

    api = API()
    fake = type("Library", (), {"sysctl": api})()
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: fake)
    monkeypatch.setattr(mechanisms.sys, "platform", "darwin")
    if malformed:
        with pytest.raises(mechanisms.MechanismError, match="argument count"):
            mechanisms._os_argv_proof(1234)
    else:
        assert mechanisms._os_argv_proof(1234) == {
            "argc": 2,
            "argv_sha256": hashlib.sha256(args).hexdigest(),
        }
    assert bytes(api.buffer) == b"\0" * len(api.buffer)


def test_linux_argument_reader_keeps_fixed_size_cap(monkeypatch):
    import io

    monkeypatch.setattr(mechanisms.sys, "platform", "linux")
    monkeypatch.setattr(mechanisms, "MAX_ARGV_BYTES", 32)
    paths = []

    def opened(path, mode):
        paths.append(str(path))
        assert mode == "rb"
        return io.BytesIO(b"x" * 33)

    monkeypatch.setattr(mechanisms.Path, "open", opened)
    with pytest.raises(mechanisms.MechanismError, match="oversized"):
        mechanisms._os_argv_proof(1234)
    assert paths == ["/proc/1234/cmdline"]


def test_linux_exact_argument_bytes_preserve_multiline_unicode_and_empty(monkeypatch):
    import io

    raw = b"/offline/python\0" + "multiline\n—雪".encode() + b"\0\0long\0"
    monkeypatch.setattr(mechanisms.sys, "platform", "linux")
    monkeypatch.setattr(mechanisms.Path, "open", lambda path, mode: io.BytesIO(raw))
    assert mechanisms._os_argv_proof(1234) == {
        "argc": 4,
        "argv_sha256": hashlib.sha256(raw).hexdigest(),
    }
