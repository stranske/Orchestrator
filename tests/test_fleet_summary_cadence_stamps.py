"""fleet_summary reports every cadence stamp, read from where its writer puts it.

`cadence_stamps` was a hand-copied tuple of five names, all read from the state dir. Four were a
subset of `cadence_registry.CADENCE_STEPS`, so the list could only drift as steps were added or
renamed. The fifth, `.last-ship-gate`, is written by `exp_abcd.followup` under the EXPERIMENTS dir,
so the summary reported it as None on every call while the gate kept stamping, which reads as "never
ran" rather than "looked in the wrong place".
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import cadence_registry
import exp_abcd
import mcp_server

BRAIN_TABLES = (
    "runs",
    "outcomes",
    "costs",
    "evaluations",
    "human_calibration",
    "owner_questions",
    "resume_tokens",
)


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A private state dir and experiments dir, an empty heartbeat dir and an empty Brain."""
    state = tmp_path / "state"
    experiments = tmp_path / "experiments"
    state.mkdir()
    experiments.mkdir()

    @contextmanager
    def empty_brain() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(":memory:")
        try:
            for table in BRAIN_TABLES:
                conn.execute(f"CREATE TABLE {table} (id INTEGER)")
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(mcp_server, "STATE_DIR", state)
    monkeypatch.setattr(mcp_server, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", experiments)
    monkeypatch.setattr(mcp_server.feedback, "_conn", empty_brain)
    return state, experiments


def _stamp(path: Path, mtime: int) -> None:
    path.touch()
    os.utime(path, (mtime, mtime))


def test_every_registry_stamp_is_reported_with_its_own_mtime(dirs: tuple[Path, Path]) -> None:
    state, _ = dirs
    stamped = [
        row["success_stamp"] for row in cadence_registry.CADENCE_STEPS if row.get("success_stamp")
    ]
    for offset, stamp in enumerate(stamped):
        _stamp(state / stamp, 1_700_000_000 + offset)

    stamps = mcp_server._fleet_summary()["cadence_stamps"]

    # Distinct mtimes, so each key is proven to read its own file rather than merely to exist.
    for offset, stamp in enumerate(stamped):
        assert stamps.get(stamp.removeprefix(".")) == 1_700_000_000 + offset, (stamp, stamps)
    # Nothing beyond the registry and the ship gate: a name kept by hand is a name that drifts.
    ship_gate = exp_abcd.ship_gate_stamp().name.removeprefix(".")
    assert set(stamps) == {stamp.removeprefix(".") for stamp in stamped} | {ship_gate}, stamps


def test_the_ship_gate_stamp_is_read_from_the_experiments_dir(dirs: tuple[Path, Path]) -> None:
    state, experiments = dirs
    gate = exp_abcd.ship_gate_stamp()
    # Before touching anything: the path follows the patched EXP_DIR, so this writes no live stamp.
    assert gate.parent == experiments, gate
    _stamp(gate, 1_700_000_000)
    # A decoy where the summary used to look. Reading it, or reading nothing, both fail below.
    _stamp(state / gate.name, 1_600_000_000)

    stamps = mcp_server._fleet_summary()["cadence_stamps"]

    assert stamps["last-ship-gate"] == 1_700_000_000, stamps


def test_an_unresolvable_ship_gate_path_is_not_reported_as_never_ran(
    dirs: tuple[Path, Path], monkeypatch
) -> None:
    # The key the summary uses when exp_abcd resolves, taken before exp_abcd is made unimportable.
    key = exp_abcd.ship_gate_stamp().name.removeprefix(".")
    monkeypatch.setitem(sys.modules, "exp_abcd", None)  # `import exp_abcd` now raises ImportError

    summary = mcp_server._fleet_summary()

    # Same key as when it resolves, and a reason rather than None, which would say "never ran".
    reason = summary["cadence_stamps"][key]
    assert isinstance(reason, str) and reason.startswith("unavailable: "), summary
    assert "exp_abcd" in reason, reason
    # The rest of the summary still answers.
    assert summary["db_volumes"] == dict.fromkeys(BRAIN_TABLES, 0), summary
