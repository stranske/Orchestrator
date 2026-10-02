"""Ledger READS take the lock SHARED; everything that writes the ledger takes it EXCLUSIVE.

Until 2026-10-02 `capabilities._locked` took an exclusive flock for every caller, so a reader of the
ledger queued behind every other reader on the machine as well as behind the tick. A read only needs
to keep WRITERS out while it reads the bytes, so `load(create=False)` — the path every report and
every verification reader takes through `load_declared` — now holds the lock shared.

Pinned by behaviour, probing the lock from outside at the exact moment it matters:
  * while `load(create=False)` reads the bytes, ANOTHER reader can take the lock and a writer
    cannot: readers share, writers are still kept out;
  * while any writer replaces the file, neither a reader nor another writer can take the lock;
  * structurally, no `with _locked(...)` block that writes the ledger passes `shared=True`, and the
    default stays exclusive, so a call site that forgets the flag keeps the safe behaviour.

The probes use LOCK_NB, so a broken lock fails a case at once instead of hanging it. flock locks
belong to an open file description, so a second `open()` of the lock file contends with the
loader's even inside one process.

DELIBERATE BREAK -> REVERT, performed 2026-10-02. Each was an exact-string edit of
`capabilities.py` whose anchor matched once, then the whole file run, then a revert to a
byte-identical file that ran green again:
  * the read path back to the exclusive lock, and the read path with no lock at all: each failed
    `test_a_read_shares_the_lock_with_readers_and_keeps_writers_out`;
  * `_locked` taking the shared lock for everyone: all five writer cases failed;
  * `save()` alone taking the shared lock: the structural case and the four writer cases that save
    a fixture failed;
  * the default flipped to shared: the structural case and all five writer cases failed.
"""

from __future__ import annotations

import ast
import fcntl
import inspect
from pathlib import Path

import pytest

import capabilities
import paths

NOW = 1_800_000_000


def _row(cap_id: str, **fields) -> dict:
    row = capabilities._blank_capability(cap_id)
    row.update(fields)
    return row


def _ledger(folder: Path, **rows: dict) -> Path:
    path = folder / "capabilities.json"
    capabilities.save(rows or {"alpha": _row("alpha")}, path)
    return path


def _probe(lock: Path) -> dict[str, bool]:
    """Could another reader, and could a writer, take the ledger's lock right now?"""
    out: dict[str, bool] = {}
    for name, op in (("reader", fcntl.LOCK_SH), ("writer", fcntl.LOCK_EX)):
        with lock.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), op | fcntl.LOCK_NB)
            except BlockingIOError:
                out[name] = False
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                out[name] = True
    return out


def _lock_of(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def test_a_read_shares_the_lock_with_readers_and_keeps_writers_out(tmp_path, monkeypatch):
    path = _ledger(tmp_path)
    seen: list[dict[str, bool]] = []
    real = Path.read_bytes

    def probing(self):
        if self == path:
            seen.append(_probe(_lock_of(path)))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", probing)
    assert sorted(capabilities.load(path, create=False)) == ["alpha"]
    assert sorted(capabilities.load_declared(path)) == ["alpha"]
    assert seen == [{"reader": True, "writer": False}] * 2, (
        f"while the ledger's bytes were read: {seen}. A reader must not exclude another reader, "
        "and it must still keep a writer out"
    )


def _writers(tmp_path: Path) -> dict:
    expiring = {"status": "wired", "expiry": 1, "trigger_cadence": "daily"}
    return {
        "save": lambda: capabilities.save({"alpha": _row("alpha", notes="b")}, _ledger(tmp_path)),
        "register": lambda: capabilities.register("beta", {}, path=_ledger(tmp_path)),
        "heartbeat": lambda: capabilities.heartbeat(
            "alpha", "invocation", timestamp=NOW, path=_ledger(tmp_path)
        ),
        "sweep": lambda: capabilities.sweep(
            _ledger(tmp_path, gone=_row("gone", **expiring)), now=NOW
        ),
        "bootstrap": lambda: capabilities.load(tmp_path / "absent" / "capabilities.json"),
    }


@pytest.mark.parametrize("writer", ["save", "register", "heartbeat", "sweep", "bootstrap"])
def test_every_write_holds_the_lock_exclusively(writer, tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(capabilities, "FEATURES_REG", tmp_path / "no-features.json")
    run = _writers(tmp_path)[writer]
    seen: list[dict[str, bool]] = []
    real = capabilities._write_ledger_unlocked

    def probing(where: Path, caps: dict) -> None:
        seen.append(_probe(_lock_of(where)))
        real(where, caps)

    monkeypatch.setattr(capabilities, "_write_ledger_unlocked", probing)
    run()
    assert seen, f"{writer} wrote nothing, so this case checked nothing"
    assert all(
        probe == {"reader": False, "writer": False} for probe in seen
    ), f"{writer} replaced the ledger while another process could take its lock: {seen}"


def test_no_block_that_writes_the_ledger_takes_the_shared_lock():
    source = (paths.MODULE_DIR / "capabilities.py").read_text(encoding="utf-8")
    checked, offending = 0, []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_locked"):
                continue
            checked += 1
            shared = any(
                kw.arg == "shared"
                and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                for kw in call.keywords
            )
            writes = any(
                isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_write_ledger_unlocked"
                for n in ast.walk(node)
            )
            if shared and writes:
                offending.append(node.lineno)
    assert checked, "no `with _locked(...)` block was found, so nothing was checked"
    assert offending == [], f"capabilities.py writes the ledger under a SHARED lock at {offending}"
    default = inspect.signature(capabilities._locked).parameters["shared"].default
    assert default is False, "the lock must default to EXCLUSIVE; only a pure read opts out"
