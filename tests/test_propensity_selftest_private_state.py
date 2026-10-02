"""The propensity selftest runs against PRIVATE stores, and a report reads ONE ledger.

Measured with an audit hook on 54302cc, `capability_propensity --selftest` reached three live stores
on whatever machine ran it: the capability ledger (from `capability_advisor.advise()`,
`capability_matcher_proposals.evaluate()` through `detect()`, and `binding_for()`'s promotion
index), the Brain (about twenty connects, each running the schema and migrations), and the lane
automations' memory files (through `detect()`'s surface records). `capabilities._locked` takes an
exclusive lock even for a read, so on a busy machine the selftest spent most of its wall time
queued behind the tick: 72 s wall for 6 s of CPU with eight `verify.py` runs going.

Part of it was a production defect rather than a test one. `missed_selection(path=X)` called
`_under_use()` with no path, so `detect(path=X)` measured under-use against the live ledger while
every other signal in the same report read X, and the selftests that DID pass a private ledger
reached the live one anyway.

The fix: `_under_use` forwards the path; `_selftest_tick_evidence`'s "real table" part asks
`binding_for` for the committed table only; and `--selftest` runs inside `_private_live_state`,
which swaps every store for a private one and fails the run on any touch of the originals.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import capability_advisor
import capabilities
import capability_matcher_proposals
import capability_propensity as cp
import feedback
import paths


def test_missed_selection_measures_under_use_against_the_ledger_it_was_given(tmp_path, monkeypatch):
    seen: list[dict] = []

    def evaluate(**kwargs):
        seen.append(kwargs)
        return {"rows": []}

    monkeypatch.setattr(capability_matcher_proposals, "evaluate", evaluate)
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)
    cp.missed_selection("fixture-surface", [], path=ledger)
    assert seen == [{"path": ledger}], (
        f"under-use was measured with {seen}, not against the ledger this report was given; the "
        "rest of the report reads that ledger, so the under-use signal came from another population"
    )


def test_binding_for_honors_promoted_empty_against_a_private_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "capabilities.json"
    promoted_cap = "fixture-promoted-cap"
    monkeypatch.setattr(
        capability_advisor,
        "_promoted_index",
        lambda path=None: {cp.TICK_SURFACE: {promoted_cap: "observed in a fixture"}},
    )
    with_promotion = capability_advisor.binding_for(cp.TICK_SURFACE, path=ledger)
    without_promotion = capability_advisor.binding_for(cp.TICK_SURFACE, path=ledger, promoted={})
    assert promoted_cap in with_promotion
    assert promoted_cap not in without_promotion


def test_the_selftest_touches_no_store_outside_its_own(tmp_path):
    """The external referee. The child is handed live stores this test owns, and must leave them
    exactly as it found them: empty. It does not trust the selftest's own tripwire to say so."""
    runtime, state, lanes = tmp_path / "runtime", tmp_path / "state", tmp_path / "lanes"
    for folder in (runtime, state, lanes):
        folder.mkdir()
    (lanes / "memory-2026-10.md").write_text("## 2026-10-01T00:00:00Z\nreverted the break\n")
    inherited = ("ORCH_CAPABILITIES_PATH", "ORCH_FEEDBACK_DB", "ORCH_CAPABILITY_HEARTBEATS")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in inherited and not k.startswith("ORCH_RECORDS_")
    }
    env.update(
        ORCH_LOCAL_RUNTIME=str(runtime),
        ORCH_STATE_DIR=str(state),
        ORCH_RECORDS_OPENER_LANE=str(lanes / "memory-*.md"),
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(paths.MODULE_DIR / "capability_propensity.py"), "--selftest"],
        cwd=paths.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert proc.stdout.count("selftest: OK") >= 11, proc.stdout[-2000:]
    created = sorted(str(p.relative_to(tmp_path)) for d in (runtime, state) for p in d.rglob("*"))
    assert created == [], f"the selftest wrote into the stores it was handed as live: {created}"


def test_private_live_state_swaps_every_store_trips_on_a_touch_and_restores(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    reg, db = live / "capabilities.json", live / "feedback" / "orchestrator.db"
    closer = str(live / "closer" / "memory-*.md")
    monkeypatch.setattr(capabilities, "REG", reg)
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setitem(cp.SURFACE_RECORD_GLOBS, "opener-lane", str(live / "lanes" / "memory-*.md"))
    monkeypatch.setenv("ORCH_RECORDS_CLOSER_LANE", closer)

    with cp._private_live_state() as private:
        assert capabilities.REG.parent == private, capabilities.REG
        assert feedback.DB_PATH.is_relative_to(private), feedback.DB_PATH
        assert all(Path(g).is_relative_to(private) for g in cp.SURFACE_RECORD_GLOBS.values())
        assert "ORCH_RECORDS_CLOSER_LANE" not in os.environ, "an env override would bypass it"
    assert (capabilities.REG, feedback.DB_PATH) == (reg, db)
    assert os.environ["ORCH_RECORDS_CLOSER_LANE"] == closer

    # POSITIVE CONTROLS: each kind of touch the hook watches must fail the run and name the place.
    touches = {
        "a file": lambda: (live / "probe").write_text("touched"),
        "a sqlite connect": lambda: sqlite3.connect(str(live / "probe.db")).close(),
        "a directory listing": lambda: list(live.glob("*")),
    }
    for kind, touch in touches.items():
        with pytest.raises(AssertionError, match="live location") as caught:
            with cp._private_live_state():
                touch()
        assert str(live) in str(caught.value), (kind, str(caught.value))
        assert (capabilities.REG, feedback.DB_PATH) == (reg, db), f"not restored after {kind}"
        assert os.environ["ORCH_RECORDS_CLOSER_LANE"] == closer, f"env not restored after {kind}"

    injected = "ORCH_RECORDS_INJECTED_FIXTURE"
    with cp._private_live_state():
        os.environ[injected] = str(tmp_path / "leak.md")
    assert injected not in os.environ
