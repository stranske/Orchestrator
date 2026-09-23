"""The firing monitor is a REPORT, so `review()` must never write the shared capability ledger.

`review()` read the ledger with `capabilities.load`, the WRITING loader. On a ledger it finds out of
date, `load()` creates the file, seeds declared gate rows that are missing, reconciles
declaration-owned fields and retires rows past their expiry, and writes the result back. So a weekly
report whose cadence row says "read-only apart from its own history file" could rewrite the ledger
every capability reads, and its kill switch, `ORCH_FIRING_MONITOR_DISABLED=1`, stopped only the
history write. PR #328 fixed the same defect in `capability_activation_audit.audit()` by reading
with `capabilities.load_declared`, which reconciles an in-memory copy and writes nothing; these
tests pin the monitor to that reader.

Each write case arms exactly ONE of `load()`'s four writes, and first proves that `load()` really
makes it on an identical copy, so no case can pass because its fixture happened to need no write.

The monitor's rail exercise patched the old loader name, so the last two tests run that committed
contract through the real runner and pin its fixture to the payload that regenerates it.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import capabilities
import capability_firing_monitor as monitor
import rail_exercise

NOW = 1_800_000_000
DAY = 86400

# One declared GATE, which `load()` seeds into a ledger that lacks it, and one declared non-gate,
# whose cadence reconciliation owns. They replace the real tables so that each case arms exactly one
# write and none depends on what the real tables declare today.
GATE = {
    "status": "shadow",
    "matcher": {"kind": "tick_phase", "name": "fixture-gate"},
    "trigger_cadence": "daily",
}
DECLARED = {"trigger_cadence": "daily"}

WRITES = ("create", "seed", "reconcile", "expire")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(capabilities, "KNOWN_GATES", {"fixture-gate": GATE})
    monkeypatch.setattr(capabilities, "KNOWN_DECLARATIONS", {"fixture-declared": DECLARED})
    # A missing ledger is built from the feature registry as well as from the gates.
    monkeypatch.setattr(capabilities, "FEATURES_REG", tmp_path / "no-features.json")
    monkeypatch.setattr(monitor, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(monitor, "HISTORY", tmp_path / "state" / "history.json")
    return tmp_path


def _row(cap_id: str, **fields) -> dict:
    row = capabilities._blank_capability(cap_id)
    row.update(
        status="wired",
        matcher={"kind": "tick_phase", "name": cap_id},
        trigger_cadence="daily",
        last_invocation=NOW - DAY,
    )
    row.update(fields)
    return row


def _ledger(folder: Path, write: str | None) -> Path:
    """A ledger that `load()` rewrites for the one reason `write` names, and for no other."""
    path = folder / "capabilities.json"
    if write == "create":
        return path  # absent, so `load()` creates it
    rows = {
        "fixture-gate": _row("fixture-gate", **GATE),
        # A month silent against a daily cadence: `overdue` exactly when the DECLARED cadence is
        # what the monitor reads.
        "fixture-declared": _row("fixture-declared", last_invocation=NOW - 30 * DAY),
        "fixture-expiring": _row("fixture-expiring"),
    }
    if write == "seed":
        del rows["fixture-gate"]
    elif write == "reconcile":
        rows["fixture-declared"]["trigger_cadence"] = None
    elif write == "expire":
        rows["fixture-expiring"]["expiry"] = 1
    capabilities.save(rows, path)
    return path


def _identity(path: Path) -> tuple[bytes, int, int] | None:
    """Bytes, mtime and inode: the writer replaces the file, so a same-second rewrite still shows."""
    if not path.exists():
        return None
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns, stat.st_ino


def _load_rewrites(ledger: Path, scratch: Path) -> bool:
    """Does the WRITING loader rewrite (or create) this ledger? Asked of a copy, never the original."""
    scratch.mkdir()
    copy = scratch / ledger.name
    if ledger.exists():
        shutil.copy2(ledger, copy)
    before = _identity(copy)
    capabilities.load(copy)
    return _identity(copy) != before


def test_the_base_fixture_needs_no_write(isolated):
    # Each case below adds one reason to write, so the base must have none: otherwise a case could
    # pass on a write the base arms rather than on the one it names.
    assert not _load_rewrites(_ledger(isolated, None), isolated / "control")


@pytest.mark.parametrize("write", WRITES)
def test_review_never_writes_the_ledger(isolated, write):
    ledger = _ledger(isolated, write)
    assert _load_rewrites(ledger, isolated / "control"), f"the fixture must arm {write!r}"
    before = _identity(ledger)
    monitor.review(now=NOW, path=ledger)
    assert _identity(ledger) == before, f"review() made load()'s {write!r} write"


def test_the_kill_switch_leaves_nothing_written_on_the_ticks_own_command(
    isolated, monkeypatch, capsys
):
    # orchestrate.sh runs `capability_firing_monitor.py --record --json`, and the cadence registry
    # says ORCH_FIRING_MONITOR_DISABLED=1 stops the write. Before this fix it stopped the history
    # write only; review() could still rewrite the ledger it reads.
    ledger = _ledger(isolated, "reconcile")
    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setattr(monitor, "DISABLED", True)  # what ORCH_FIRING_MONITOR_DISABLED=1 sets
    before = _identity(ledger)
    assert monitor.main(["--record", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recorded"] == {"recorded": False, "reason": "ORCH_FIRING_MONITOR_DISABLED=1"}
    assert _identity(ledger) == before
    assert not monitor.HISTORY.exists()


def test_review_judges_the_declared_cadence_not_the_stale_one_on_disk(isolated):
    # Read-only must not mean stale. `load(create=False)` writes nothing either, but it returns the
    # row as it sits on disk, so the monitor would judge `fixture-declared` by a cadence its
    # declaration no longer has: `no_cadence_declared`, and never overdue. `load_declared`
    # reconciles the copy it returns, which is the view the writing loader gave.
    report = monitor.review(now=NOW, path=_ledger(isolated, "reconcile"))
    assert [row["capability_id"] for row in report["overdue"]] == ["fixture-declared"]
    assert "fixture-declared" not in report["no_cadence_declared"]


RAIL_CAPABILITY = "capability-firing-monitor"


def test_the_monitors_rail_exercise_still_runs_on_its_fixture_ledger():
    # The committed contract, through the real runner: the pass arm must pass and the break arm
    # must break. Its fixture patches the loader review() calls and fails unless that patch was the
    # one read, for the fixture's own path, so a loader renamed again cannot leave it quietly
    # exercising some other ledger.
    path = rail_exercise.CONTRACT_ROOT / RAIL_CAPABILITY / "contract.json"
    row = rail_exercise.run_contract(path, json.loads(path.read_text(encoding="utf-8")))
    assert (row["status"], row["pass_rc"], row["break_ok"]) == ("pass", 0, True), row


def test_every_copy_of_the_monitors_fixture_is_what_its_setup_writes(tmp_path):
    # The runner copies the committed fixture, then runs the contract's setup, which deletes that
    # copy and rewrites it from a base64 payload. So the PAYLOAD is the code that executes, and a
    # committed run.py that differs from it is code that never runs. Several contract directories
    # carry this fixture and only one contract runs it; every copy must still be what the payload
    # writes, because a reader cannot tell the running copy from the others.
    home = rail_exercise.CONTRACT_ROOT / RAIL_CAPABILITY
    contract = json.loads((home / "contract.json").read_text(encoding="utf-8"))
    command = rail_exercise._expand(contract["setup"], tmp_path, tmp_path)
    payload = base64.b64decode(re.search(r"b64decode\('([^']+)'\)", command).group(1))
    # Unretargeted, the payload would write into the scratch tree it was generated in.
    assert str(tmp_path / RAIL_CAPABILITY) in payload.decode("utf-8")
    subprocess.run(command, shell=True, check=True, cwd=rail_exercise.exec_root())
    written = tmp_path / RAIL_CAPABILITY
    names = sorted(p.name for p in written.iterdir())
    assert names == ["baseline.json", "ledger.json", "run.py"], names
    copies = sorted(rail_exercise.CONTRACT_ROOT.glob(f"*/fixtures/{RAIL_CAPABILITY}"))
    assert home / "fixtures" / RAIL_CAPABILITY in copies, copies  # the copy the contract runs
    for copy in copies:
        assert sorted(p.name for p in copy.iterdir()) == names, copy
        for name in ("run.py", "ledger.json"):
            assert (copy / name).read_bytes() == (written / name).read_bytes(), copy / name
        # The payload writes baseline.json in directory-listing order, which is not portable, so
        # the hashes are compared rather than the bytes.
        assert json.loads((copy / "baseline.json").read_text()) == json.loads(
            (written / "baseline.json").read_text()
        ), copy
