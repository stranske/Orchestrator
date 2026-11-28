"""ledger_reconcile's per-row Brain writers close their connections (tick failure since 2026-10-24).

`with conn:` only commits. A sqlite3 connection's statement cache refers back to the connection, so an
unclosed one is freed only by a full GC pass. The reconcile calls these writers once per run (~12,000
per tick); under the launchd tick's 256 open-file limit the 254th call raised "unable to open database
file", and every hourly reconcile failed for a month while interactive runs (1M-file limit) passed.
"""

from __future__ import annotations

import gc
import os

import pytest

import adapters
import feedback
import ledger_reconcile

CALLS = 40


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    feedback.record_run("r0", "example/repo#1", "implement", "codex")
    return tmp_path


def _growth(call) -> int:
    call(0)  # schema creation and one-time opens happen outside the measurement
    gc.collect()
    gc.disable()  # a GC pass would hide the leak; the tick's leak is exactly what GC had not reached
    try:
        before = _open_fds()
        for i in range(1, CALLS + 1):
            call(i)
        return _open_fds() - before
    finally:
        gc.enable()


@pytest.mark.parametrize(
    "writer",
    [
        lambda i: feedback.record_cost(f"r{i}", cost_usd=0.01, source="ledger"),
        lambda i: feedback.record_resume_token(f"r{i}", "codex", "session", f"t{i}"),
        lambda i: ledger_reconcile._profile_id_from_attempt(f"r{i}"),
        lambda i: ledger_reconcile._settle_provider_limit_death(
            f"r{i}", {"subcategory": "rate_limit"}, dry_run=True
        ),
    ],
    ids=["record_cost", "record_resume_token", "profile_id_from_attempt", "settle_provider_limit"],
)
def test_per_row_writers_do_not_hold_open_files(brain, writer):
    assert _growth(writer) <= 2, f"{CALLS} calls left connections open"
