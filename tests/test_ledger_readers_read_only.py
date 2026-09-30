"""Ledger READERS never write the shared capability ledger.

`capabilities.load` is the WRITING loader. On a ledger it finds out of date it creates the file,
seeds declared gate rows that are missing, reconciles declaration-owned fields and retires rows past
their expiry, then writes the result back. PR #328 moved the activation audit, and PR #337 the
firing monitor, to `capabilities.load_declared`, which reconciles an in-memory copy and writes
nothing. Twelve more read sites still took the writing load, and the guard that forbids it,
`test_verifying_the_system_never_writes_the_live_ledger`, saw none of them: its regex needed `REG`
directly after `load(`, so `load(path or capabilities.REG)` and a bare `load()` both passed it.

Three of those readers run against the LIVE ledger inside verification (the admission gate, the
advisor's front-door selftest and the evidence-acquisition selftest), `switch_review` is a report
whose cadence row says "report-only otherwise", and the matcher-proposals report is read by the
propensity detector on every run. Each reader here is pinned by BEHAVIOUR: a case arms exactly one
of `load()`'s four writes, first proves that `load()` really makes it on an identical copy, and
then asserts the reader leaves the ledger untouched.

The switch-review rail exercise patched the old loader name, so the last two tests run that
committed fixture through the real runner and pin every copy of it to the payload that writes it.
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
import capability_activation_audit as activation_audit
import capability_admission as admission
import capability_advisor as advisor
import capability_effectiveness as effectiveness
import capability_matcher_proposals as proposals
import capability_opportunity as opportunity
import capability_outcome_bridge as bridge
import evidence_acquisition
import feedback
import rail_exercise
import switch_review

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

# What `admit()` and `report()` accept in place of `_context()`, which reads the activation audit
# and the consult sites. Every requirement predicate catches its own errors, so a thin context
# still drives the ledger read, which is all these cases are about. `_context` has its own case.
ADMISSION_CTX = {
    "audit_rows": {},
    "fixtures": set(),
    "known_controls": set(),
    "bound_surfaces": {},
    "reached_surfaces": set(),
    "consult_reach": {},
}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(capabilities, "KNOWN_GATES", {"fixture-gate": GATE})
    monkeypatch.setattr(capabilities, "KNOWN_DECLARATIONS", {"fixture-declared": DECLARED})
    # A missing ledger is built from the feature registry as well as from the gates.
    monkeypatch.setattr(capabilities, "FEATURES_REG", tmp_path / "no-features.json")
    # Every input these readers take from the machine besides the ledger: the process table, the
    # exec mirror, GitHub, the Brain and the live activation audit.
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **_: {"suspect": False})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {"suspect": False})
    monkeypatch.setattr(activation_audit, "audit", lambda **_: {"rows": []})
    monkeypatch.setattr(opportunity, "_task_type_counts", lambda conn=None: {})
    monkeypatch.setattr(opportunity, "_role_invocation_counts", lambda conn=None: {})
    monkeypatch.setattr(effectiveness, "_edge_rows", lambda conn=None: [])
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
        "fixture-declared": _row("fixture-declared"),
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
    """Bytes, mtime and inode: the writer replaces the file, so a same-second rewrite shows."""
    if not path.exists():
        return None
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns, stat.st_ino


def _load_rewrites(ledger: Path, scratch: Path) -> bool:
    """Does the WRITING loader rewrite (or create) this ledger? Asked of a copy, never of it."""
    scratch.mkdir()
    copy = scratch / ledger.name
    if ledger.exists():
        shutil.copy2(ledger, copy)
    before = _identity(copy)
    capabilities.load(copy)
    return _identity(copy) != before


def _default_ledger(monkeypatch, ledger: Path) -> None:
    """Point every spelling of "the default ledger" at `ledger`.

    Two readers take no path and read the default. The fixed code names `capabilities.REG`, which is
    read when it runs; a bare `capabilities.load()` uses the default bound when `load` was DEFINED,
    which no patch of `REG` reaches. Both are repointed, so neither the fix nor a revert of it can
    reach a real ledger from this file.
    """
    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setattr(capabilities.load, "__defaults__", (ledger,))
    monkeypatch.setattr(capabilities.load_declared, "__defaults__", (ledger,))


def _feedable(ledger: Path, monkeypatch) -> object:
    _default_ledger(monkeypatch, ledger)
    return evidence_acquisition.feedable(now=NOW)


def _versions(ledger: Path, monkeypatch) -> object:
    _default_ledger(monkeypatch, ledger)
    return feedback._resolve_capability_versions(["fixture-gate"])


# One entry per read site that took the writing load. `admit` and `report` both reach `admit`'s
# read and only `report` reaches its own, so each site has a case that fails when only it regresses.
# `admit` is asked about `fixture-declared` because every ledger but the absent one holds that row.
READERS = {
    "capability_admission._context": lambda ledger, _mp: admission._context(ledger),
    "capability_admission.admit": lambda ledger, _mp: admission.admit(
        "fixture-declared", path=ledger, ctx=dict(ADMISSION_CTX)
    ),
    "capability_admission.report": lambda ledger, _mp: admission.report(
        path=ledger, ctx=dict(ADMISSION_CTX)
    ),
    "capability_advisor.advise": lambda ledger, _mp: advisor.advise(
        "add pytest coverage for the retry helper", path=ledger, record=False
    ),
    "capability_advisor.learned_associations": lambda ledger, _mp: advisor.learned_associations(
        path=ledger
    ),
    "capability_effectiveness.measure": lambda ledger, _mp: effectiveness.measure(path=ledger),
    "capability_matcher_proposals.evaluate": lambda ledger, _mp: proposals.evaluate(
        path=ledger, task_counts={}
    ),
    "capability_opportunity.report": lambda ledger, _mp: opportunity.report(path=ledger, env={}),
    "capability_outcome_bridge._known_capability_ids": lambda ledger, _mp: (
        bridge._known_capability_ids(ledger)
    ),
    "evidence_acquisition.feedable": _feedable,
    "feedback._resolve_capability_versions": _versions,
    "switch_review.review": lambda ledger, _mp: switch_review.review(
        now=NOW, env={"ORCH_RANGE_LANE_ROLLOUT": "1"}, path=ledger
    ),
}


def test_the_base_fixture_needs_no_write(isolated):
    # Each case below adds one reason to write, so the base must have none: otherwise a case could
    # pass on a write the base arms rather than on the one it names.
    assert not _load_rewrites(_ledger(isolated, None), isolated / "control")


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("reader", sorted(READERS))
def test_reader_never_writes_the_ledger(isolated, monkeypatch, reader, write):
    ledger = _ledger(isolated, write)
    assert _load_rewrites(ledger, isolated / "control"), f"the fixture must arm {write!r}"
    before = _identity(ledger)
    try:
        READERS[reader](ledger, monkeypatch)
    except ValueError:
        # `admit()` refuses a capability the ledger does not hold, and a ledger that does not exist
        # holds none. That refusal is the read-only answer; creating the file was the defect.
        if (reader, write) != ("capability_admission.admit", "create"):
            raise
    assert _identity(ledger) == before, f"{reader} made load()'s {write!r} write"


@pytest.mark.parametrize(
    "reader", ["capability_effectiveness.measure", "capability_opportunity.report"]
)
def test_reports_show_the_declared_status_not_the_stale_one_on_disk(isolated, monkeypatch, reader):
    # Read-only must not mean stale. `load(create=False)` writes nothing either, and the guard
    # allows it, but it returns rows exactly as they sit on disk. `load_declared` reconciles the
    # copy it returns, which is the view the writing loader gave: the gate row says `wired` on disk
    # and its declaration says `shadow`, so a raw read would report a state the system is not
    # acting on.
    ledger = isolated / "capabilities.json"
    capabilities.save({"fixture-gate": _row("fixture-gate", **{**GATE, "status": "wired"})}, ledger)
    rows = {row["capability_id"]: row for row in READERS[reader](ledger, monkeypatch)["rows"]}
    assert rows["fixture-gate"]["status"] == "shadow", rows["fixture-gate"]


RAIL_CAPABILITY = "switch-review"


def test_the_switch_review_rail_exercise_runs_on_its_fixture_ledger():
    # The committed contract is SKIPPED by its own `skip_reason`: it is a declared specification gap
    # whose two arms both print status FAIL. Skipped is not unrunnable, and a fixture that crashes
    # would stay invisible until someone lifts the skip, so this lifts it in memory and runs the
    # real runner. Both arms must run to completion (the fixture stops unless review() read its own
    # ledger through the patched loader) and the break arm's check must pass.
    path = rail_exercise.CONTRACT_ROOT / RAIL_CAPABILITY / "contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    assert contract.pop("skip_reason"), "the contract runs now: assert its verdict here instead"
    row = rail_exercise.run_contract(path, contract)
    commands = row["commands"]
    rcs = {name: [c["rc"] for c in commands[name]] for name in ("setup", "run", "break_run")}
    assert rcs == {"setup": [0], "run": [0], "break_run": [0]}, commands
    assert row["break_ok"] is True, row


def test_every_copy_of_the_switch_review_fixture_is_what_its_setup_writes(tmp_path):
    # The runner copies the committed fixture, then runs the contract's setup, which deletes that
    # copy and rewrites it from a base64 payload. So the PAYLOAD is the code that executes, and a
    # committed run.py that differs from it is code that never runs. Seven contract directories
    # carry this fixture and only one contract would run it; every copy must still be what the
    # payload writes, because a reader cannot tell the running copy from the others.
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
