"""The ledger read cache: one parse per distinct file content, never a stale ledger, never shared.

`capabilities.load(create=False)` — and so `load_declared`, which every verification reader uses —
parsed the whole ledger on every call, then copied the result through a second JSON round trip, and
did both while holding the ledger's EXCLUSIVE lock. Measured 2026-10-02 on the live 18 MB ledger:
one run of the admission gate made 307 such loads and the advisor selftest 578, against a file that
never changed, at about 0.2 s a load.

The cache keys on the BYTES read, never on mtime, size or inode, so these cases pin what makes that
safe: a repeated read does not parse, a rewrite that keeps size, mtime AND inode is still seen, no
caller can reach another caller's copy, and the lock is released before any parsing starts. The
last case pins the admission report's own share of the reads: one per report, not one per row.

DELIBERATE BREAK -> REVERT, performed 2026-10-02. Each is an exact-string edit whose anchor matched
once, then the whole file run, then a revert by string to a byte-identical file that ran green:
  * the cache hit compared lengths instead of bytes (a size-keyed cache), and
    `test_a_rewrite_that_keeps_size_mtime_and_inode_is_still_seen` failed;
  * the read path parsed inside the lock again: `test_the_lock_is_released_before_parsing` failed;
  * a cache hit returned one shared object: `test_no_caller_can_reach_another_callers_copy` failed;
  * `report()` stopped handing its ledger to `admit()`:
    `test_an_admission_report_reads_the_ledger_once` failed.
"""

from __future__ import annotations

import fcntl
import json
import marshal
import os
from pathlib import Path

import pytest

import capabilities
import capability_admission as admission


class _Counting:
    """Stands in for a module inside `capabilities`, counting one function and delegating the rest."""

    def __init__(self, module, name: str, probe=None):
        self._module, self._name, self._probe = module, name, probe
        self.calls = 0

    def __getattr__(self, attr):
        real = getattr(self._module, attr)
        if attr != self._name:
            return real

        def counted(*args, **kwargs):
            self.calls += 1
            if self._probe:
                self._probe()
            return real(*args, **kwargs)

        return counted


def _ledger(folder: Path, note: str = "aaaa") -> Path:
    path = folder / "capabilities.json"
    row = capabilities._blank_capability("alpha")
    row["notes"] = note
    capabilities.save({"alpha": row}, path)
    return path


def test_an_unchanged_ledger_is_parsed_once(tmp_path, monkeypatch):
    path = _ledger(tmp_path)
    parses = _Counting(json, "loads")
    monkeypatch.setattr(capabilities, "json", parses)
    first = capabilities.load_declared(path)
    for _ in range(4):
        assert capabilities.load_declared(path) == first
    assert parses.calls == 1, f"five reads of one unchanged ledger parsed it {parses.calls} times"


def test_a_rewrite_that_keeps_size_mtime_and_inode_is_still_seen(tmp_path):
    """The case a stat-keyed cache gets wrong: written in place, same length, mtime put back."""
    path = _ledger(tmp_path, "aaaa")
    before = path.stat()
    assert capabilities.load(path, create=False)["alpha"]["notes"] == "aaaa"
    raw = path.read_bytes()
    assert raw.count(b'"aaaa"') == 1, "the fixture must carry the note exactly once"
    with path.open("r+b") as handle:
        handle.write(raw.replace(b'"aaaa"', b'"bbbb"'))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    same = (after.st_size, after.st_mtime_ns, after.st_ino)
    assert same == (before.st_size, before.st_mtime_ns, before.st_ino), "the fixture must collide"
    assert capabilities.load(path, create=False)["alpha"]["notes"] == "bbbb"


def test_no_caller_can_reach_another_callers_copy(tmp_path):
    path = _ledger(tmp_path)
    first = capabilities.load(path, create=False)
    history = list(first["alpha"]["event_history"])
    first["alpha"]["notes"] = "mutated"
    first["alpha"]["event_history"].append({"type": "mutated"})
    first["beta"] = {}
    second = capabilities.load(path, create=False)
    assert sorted(second) == ["alpha"]
    assert second["alpha"]["notes"] == "aaaa"
    assert second["alpha"]["event_history"] == history
    second["alpha"]["notes"] = "again"
    assert capabilities.load(path, create=False)["alpha"]["notes"] == "aaaa"


def test_the_lock_is_released_before_parsing(tmp_path, monkeypatch):
    """Parsing under the lock held every other reader and writer for the whole parse."""
    path = _ledger(tmp_path)
    lock = path.with_name(path.name + ".lock")
    held: list[bool] = []

    def probe() -> None:
        # A second open is a second open-file description, so its flock contends with the
        # loader's even inside one process.
        with lock.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                held.append(False)

    monkeypatch.setattr(capabilities, "json", _Counting(json, "loads", probe))
    monkeypatch.setattr(capabilities, "marshal", _Counting(marshal, "loads", probe))
    capabilities.load(path, create=False)  # a miss: parsed from JSON
    capabilities.load(path, create=False)  # a hit: copied out of the cache
    assert held == [False, False], f"the ledger lock was held while parsing: {held}"


def test_the_schema_is_checked_on_a_cached_read_too(tmp_path, monkeypatch):
    path = _ledger(tmp_path)
    capabilities.load(path, create=False)
    monkeypatch.setattr(capabilities, "SCHEMA_VERSION", capabilities.SCHEMA_VERSION + 1)
    with pytest.raises(ValueError, match="unsupported capability ledger schema"):
        capabilities.load(path, create=False)


def test_a_writer_after_a_cached_read_is_seen_by_both_loaders(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(capabilities, "KNOWN_GATES", {})
    monkeypatch.setattr(capabilities, "KNOWN_DECLARATIONS", {})
    path = _ledger(tmp_path, "aaaa")
    assert capabilities.load(path, create=False)["alpha"]["notes"] == "aaaa"
    rows = capabilities.load(path, create=False)
    rows["alpha"]["notes"] = "a longer note than before"
    capabilities.save(rows, path)
    assert capabilities.load(path, create=False)["alpha"]["notes"] == "a longer note than before"
    assert capabilities.load(path)["alpha"]["notes"] == "a longer note than before"


def test_an_admission_report_reads_the_ledger_once(tmp_path, monkeypatch):
    """Each row used to reload the whole ledger: one read per live row on top of the report's own."""
    rows = {}
    for cap_id in ("t-one", "t-two", "t-three"):
        row = capabilities._blank_capability(cap_id)
        row["status"] = "wired"
        rows[cap_id] = row
    path = tmp_path / "capabilities.json"
    capabilities.save(rows, path)
    reads: list[Path] = []
    real = capabilities.load_declared

    def counted(where=capabilities.REG):
        reads.append(Path(where))
        return real(where)

    monkeypatch.setattr(admission.capabilities, "load_declared", counted)
    ctx = {
        "audit_rows": {},
        "fixtures": set(),
        "known_controls": set(),
        "bound_surfaces": {},
        "reached_surfaces": set(),
        "consult_reach": {},
    }
    rep = admission.report(path=path, ctx=ctx)
    assert rep["total"] == 3, rep["total"]
    assert reads == [path], f"one report read the ledger {len(reads)} times"
