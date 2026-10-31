"""Hermetic coverage for the first ranked #1025 capability boundaries.

The advisor's front-door selftest normally skips on a fresh checkout because its
three rows are accumulated local ledger state.  A synthetic ledger lets pytest
exercise the real matching and rendering contract without consulting that state.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import capabilities
import capability_advisor as advisor
import env_prereq


def _row(name: str, task_type: str) -> dict:
    row = capabilities._blank_capability(name)
    row.update(
        status="generated",
        matcher={"field": "task_type", "operator": "in", "value": [task_type]},
    )
    return row


def test_advisor_front_door_runs_against_a_hermetic_registered_ledger(monkeypatch, tmp_path):
    """Real task signals must reach their named capability and render usable advice."""
    ledger = tmp_path / "capabilities.json"
    capabilities.save(
        {
            "offload": _row("offload", "offload"),
            "codemod-campaign": _row("codemod-campaign", "codemod"),
            "testgen-lane": _row("testgen-lane", "testgen"),
        },
        ledger,
    )
    monkeypatch.setattr(capabilities, "REG", ledger)
    assert env_prereq.ledger_rows_absent("offload", "codemod-campaign", "testgen-lane") is None

    # The selftest's cases are the escaped-defect examples.  Calling it here is
    # meaningful only with the fixture above: it uses advise() and format_advice()
    # unchanged rather than a mocked answer.
    advisor._selftest_front_door()


def test_acting_window_returns_unknown_when_monitor_cannot_be_read(monkeypatch):
    """An unavailable monitor is unknown, never silently converted to zero activity."""
    monitor = SimpleNamespace(acted_on_index=lambda *_args: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setitem(sys.modules, "value_chain_monitor", monitor)

    assert capabilities.acting_in_trailing_window("offload", now=1_700_000_000) is None
