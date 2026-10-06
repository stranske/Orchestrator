"""A row this tree declares is registered by the tree's first writing load, never by a lucky caller.

THE INCIDENT (2026-10-05). `value-chain-monitor` was declared in `capabilities.KNOWN_DECLARATIONS`,
and its row was registered only by the weekly switch review, run from the live exec mirror. The
pre-sync verdict on the tree that declared it judged a copy of the live ledger, which lacked the
row, so the row's recurrence fixture made one test skip more than the mirror's ceiling allows
(22 > 21) and the sync was refused. Only that sync could deploy the caller that registers the row,
so every later sync would have been refused the same way: a gate whose drain needs the gate open.

THE FIX, in three parts, each pinned here or beside its code:
  * every writing load seeds a missing `KNOWN_DECLARATIONS` entry that declares a status, as it
    already seeded a missing `KNOWN_GATES` row (`capabilities._seed_declared_rows`);
  * `scripts/verify_before_sync.sh` makes that load on its scratch copy before verify.py runs
    (`capabilities.py seed-declared`; tests/test_verify_before_sync.py);
  * a check that skips for such a row says the skip drains at the first writing load
    (`env_prereq.DECLARED_UNREGISTERED_MARK`), and verify.py counts those skips beside the ceiling.

A seeded row is what the production caller wrote, at the declared status and NEVER active: a
capability is not activated by code existence (CLAUDE.md §1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import capabilities
import capability_recurrence_check as recurrence
import env_prereq
import paths

STATUS_DECLARED = sorted(n for n, d in capabilities.KNOWN_DECLARATIONS.items() if "status" in d)
OVERLAYS = sorted(n for n, d in capabilities.KNOWN_DECLARATIONS.items() if "status" not in d)


def _ledger(tmp_path, rows: dict | None = None):
    path = tmp_path / "capabilities.json"
    capabilities._write_ledger_unlocked(path, rows or {})
    return path


def test_the_declared_set_is_every_gate_and_every_status_declaration():
    assert STATUS_DECLARED, "no KNOWN_DECLARATIONS entry declares a status; this test is vacuous"
    assert "value-chain-monitor" in STATUS_DECLARED, STATUS_DECLARED
    assert capabilities.declared_row_ids() == sorted({*capabilities.KNOWN_GATES, *STATUS_DECLARED})
    for name in OVERLAYS:
        assert name not in capabilities.declared_row_ids(), name


def test_a_seeded_declaration_is_what_the_production_caller_wrote_plus_who_wrote_it(tmp_path):
    """`register(name, KNOWN_DECLARATIONS[name])` is what switch_review wrote until 2026-10-06."""
    for name in STATUS_DECLARED:
        by_caller = tmp_path / f"{name}-registered.json"
        capabilities.register(name, capabilities.KNOWN_DECLARATIONS[name], by_caller)
        expected = capabilities.load(by_caller, create=False)[name]

        seeded_path = _ledger(tmp_path / name)
        report = capabilities.seed_declared(seeded_path)
        assert name in report["seeded"] and report["missing"] == [], report
        seeded = capabilities.load(seeded_path, create=False)[name]

        events = seeded.pop("event_history")
        assert expected.pop("event_history") == []
        assert seeded == expected, (name, seeded, expected)
        assert [e["type"] for e in events] == [capabilities.DECLARED_ROW_EVENT], events
        assert events[0]["activation_inferred"] is False, events
        assert seeded["status"] != "active", seeded["status"]


def test_an_overlay_is_never_seeded(tmp_path):
    path = _ledger(tmp_path)
    capabilities.load(path)
    rows = capabilities.load(path, create=False)
    assert OVERLAYS, "no overlay declaration exists; this test is vacuous"
    assert not set(OVERLAYS) & set(rows), sorted(set(OVERLAYS) & set(rows))


def test_seeding_never_touches_an_existing_row_whatever_its_status(tmp_path):
    """A retirement is a lifecycle decision; re-registering the row would undo it."""
    name = STATUS_DECLARED[0]
    retired = {
        **capabilities._blank_capability(name),
        "status": "retired",
        "event_history": [{"timestamp": 1, "type": "transition", "from": "wired", "to": "retired"}],
    }
    path = _ledger(tmp_path, {name: retired})
    report = capabilities.seed_declared(path)
    assert name not in report["seeded"], report
    after = capabilities.load(path, create=False)[name]
    assert after["status"] == "retired", after["status"]
    assert all(e["type"] != capabilities.DECLARED_ROW_EVENT for e in after["event_history"])


def test_the_reader_still_seeds_nothing(tmp_path):
    """`load_declared` answers the ledger as it stands; only a WRITING load registers a row."""
    path = _ledger(tmp_path)
    before = path.read_bytes()
    rows = capabilities.load_declared(path)
    assert capabilities.missing_declared_rows(rows) == capabilities.declared_row_ids()
    assert path.read_bytes() == before


def test_every_declared_status_is_one_a_load_may_register(tmp_path):
    """A declaration may never assert `active` (nor a not-live status) for a load to register.

    Both paths would honour it: seeding would register the row active from code existence, and the
    reconciler's status floor would lift any live row up to it. So the tables may not declare one.
    """
    for table in (capabilities.KNOWN_GATES, capabilities.KNOWN_DECLARATIONS):
        for name, declared in table.items():
            if "status" in declared:
                assert declared["status"] in capabilities.SEEDABLE_STATES, (name, declared)


def test_a_declaration_a_load_may_not_register_fails_loudly(tmp_path, monkeypatch):
    """Left unseeded, it stays named as missing, and `seed-declared` exits non-zero saying so."""
    monkeypatch.setattr(capabilities, "KNOWN_GATES", {})
    monkeypatch.setattr(capabilities, "KNOWN_DECLARATIONS", {"too-eager": {"status": "active"}})
    path = _ledger(tmp_path)
    report = capabilities.seed_declared(path)
    assert report["seeded"] == [] and report["missing"] == ["too-eager"], report
    assert "too-eager" not in capabilities.load(path, create=False)
    assert "NOT SEEDED too-eager" in capabilities.format_seed_report(report)


def test_every_declared_row_validates_and_has_a_recurrence_fixture(tmp_path):
    """A row the load seeds must pass the tick's validation and the set-coverage gate, or seeding
    it would turn the first tick's `validate` (which aborts the tick) or the next verdict red."""
    path = _ledger(tmp_path)
    capabilities.seed_declared(path)
    result = capabilities.validate_ledger(path)
    assert result["valid"], result["errors"]
    fixtures = {f.get("capability") for f in [*recurrence.FIXTURES, *recurrence.PREDICATE_FIXTURES]}
    missing = sorted(set(capabilities.declared_row_ids()) - fixtures)
    assert not missing, f"declared rows without a recurrence fixture: {missing}"


def test_a_declared_absence_reads_as_drainable_and_history_does_not(tmp_path, monkeypatch):
    name = STATUS_DECLARED[0]
    monkeypatch.setattr(capabilities, "REG", _ledger(tmp_path))
    only = env_prereq.ledger_rows_absent(name)
    assert only and env_prereq.DECLARED_UNREGISTERED_MARK in only, only
    assert name in only and "first writing load" in only, only
    history = env_prereq.ledger_rows_absent("no-such-capability")
    assert history and env_prereq.DECLARED_UNREGISTERED_MARK not in history, history
    assert "registered by running the system" in history, history
    mixed = env_prereq.ledger_rows_absent(name, "no-such-capability")
    assert mixed and env_prereq.DECLARED_UNREGISTERED_MARK not in mixed, mixed
    assert "would not clear" in mixed, mixed
    capabilities.seed_declared(capabilities.REG)
    assert env_prereq.ledger_rows_absent(name) is None


@pytest.mark.parametrize("as_json", [False, True])
def test_the_seed_declared_command_reports_each_state(tmp_path, as_json):
    """The line the pre-sync script prints: what it registered, then that nothing was left."""
    path = _ledger(tmp_path)
    capabilities.seed_declared(path)
    rows = capabilities.load(path, create=False)
    del rows["value-chain-monitor"]
    capabilities._write_ledger_unlocked(path, rows)
    env = {**os.environ, "ORCH_CAPABILITIES_PATH": str(path), "ORCH_LOCAL_RUNTIME": str(tmp_path)}
    argv = [sys.executable, str(paths.MODULE_DIR / "capabilities.py")]
    argv += ["--json", "seed-declared"] if as_json else ["seed-declared"]

    def run() -> str:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout.strip()

    first, second = run(), run()
    if as_json:
        assert json.loads(first)["seeded"] == ["value-chain-monitor"], first
        assert json.loads(second) == {**json.loads(first), "seeded": []}, second
    else:
        total = len(capabilities.declared_row_ids())
        assert f"seeded 1 of the {total}" in first and "(value-chain-monitor)" in first, first
        assert f"none seeded, {path} already holds all {total}" in second, second
