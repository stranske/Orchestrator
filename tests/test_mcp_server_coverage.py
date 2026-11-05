"""Coverage for uncovered lines in mcp_server.py.

Targets the exception handling paths and edge cases that the selftest does not exercise.
"""

from __future__ import annotations

import sys

import pytest

import mcp_server


@pytest.fixture(autouse=True)
def _hermetic_paths(tmp_path):
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setenv("ORCH_STATE_DIR", str(tmp_path))
        scoped.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
        scoped.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "feedback.db"))
        scoped.setenv("HANDOFF_DIR", str(tmp_path))
        scoped.setenv("ORCH_EXP_DIR", str(tmp_path / "experiments"))
        yield


def test_decline_kinds_exception_path(monkeypatch):
    """_decline_kinds() returns empty list when capability_propensity raises.

    Covers lines 47-48.
    """
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "capability_propensity":
            raise ImportError("test error")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    result = mcp_server._decline_kinds()
    assert result == []


def test_fleet_summary_tick_watchdog_exception(monkeypatch):
    """_fleet_summary() handles tick_watchdog.summary() exception gracefully.

    Covers lines 287-288.
    """
    import types

    # Mock the tick_watchdog module before it's imported
    mock_module = types.ModuleType("tick_watchdog")

    def raise_on_summary(*args, **kwargs):
        raise RuntimeError("watchdog unavailable")

    mock_module.summary = raise_on_summary

    # We need to set this in sys.modules so the import in _fleet_summary finds it

    old_modules = sys.modules.get("tick_watchdog")
    sys.modules["tick_watchdog"] = mock_module

    try:
        result = mcp_server._fleet_summary()
        assert "tick" in result
        assert result["tick"]["state"] == "unavailable"
        assert "error" in result["tick"]
    finally:
        if old_modules:
            sys.modules["tick_watchdog"] = old_modules
        else:
            sys.modules.pop("tick_watchdog", None)


def test_handle_tools_call_exception(monkeypatch):
    """_handle() returns isError=True when tools/call raises.

    Covers lines 423-424.
    """

    def raise_on_call_tool(name, args):
        raise ValueError("call failed")

    monkeypatch.setattr(mcp_server, "_call_tool", raise_on_call_tool)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "test", "arguments": {}},
    }
    result = mcp_server._handle(msg)
    assert result["jsonrpc"] == "2.0"
    assert result["id"] == 1
    assert result["result"]["isError"] is True
    assert "error: call failed" in result["result"]["content"][0]["text"]


def test_handle_unknown_method_returns_none():
    """_handle() returns None for unknown methods without id.

    Covers line 440.
    """
    msg = {"method": "unknown/method"}
    result = mcp_server._handle(msg)
    assert result is None


def test_handle_unknown_method_with_id_returns_error():
    """_handle() returns error for unknown methods with id.

    Also covers line 440 indirectly (the return None case).
    """
    msg = {"jsonrpc": "2.0", "id": 1, "method": "unknown/method"}
    result = mcp_server._handle(msg)
    assert result["jsonrpc"] == "2.0"
    assert result["id"] == 1
    assert result["error"]["code"] == -32601
    assert "method not found" in result["error"]["message"]


# Note: We cannot easily test _forwarded_args and _subscript_key directly
# because they use inspect.getsource() which requires the function to be defined
# at module level in a real file. The selftest already exercises these functions
# indirectly, so we skip direct testing here.


def test_handle_ping():
    """_handle() processes ping method correctly."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    result = mcp_server._handle(msg)
    assert result == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_handle_notifications_initialized():
    """_handle() returns None for notifications/initialized."""
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    result = mcp_server._handle(msg)
    assert result is None
