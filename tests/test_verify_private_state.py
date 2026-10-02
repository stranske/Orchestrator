"""verify.py runs every child against ONE private copy of the ledger and the Brain.

Until 2026-10-02 every child of a verify run read and wrote this machine's LIVE ledger and Brain.
That was slow — `capabilities._locked` takes an exclusive lock even for a read, so every read queued
behind the tick and any other verify run (the admission gate took 275 s live and 119 s on a copy of
the same file) — and it was unsafe, because the writing loader reconciles declarations from the code
under test and every Brain connection applies that code's migrations: a run on an unmerged branch
could rewrite production state.

What is pinned here, by behaviour:
  * the copy is taken without writing either source — the Brain is opened read-only, so not even
    its migrations reach the live file;
  * a child of the run resolves BOTH paths into the copy, through the variables the modules read;
  * the variables are restored and the copy deleted when the run ends, even when it fails;
  * a source that is absent stays absent and bootstraps privately, a copy that FAILS leaves that
    one file live and says so, and `ORCH_VERIFY_LIVE_STATE=1` changes nothing at all;
  * a rail-exercise sandbox, which moves `ORCH_LOCAL_RUNTIME`, does not inherit the two variables —
    an explicit path wins over that default, so an inherited one would lead out of the sandbox.

DELIBERATE BREAK -> REVERT, performed 2026-10-02, each an exact-string edit reverted by string to a
byte-identical file that ran green again:
  * `?mode=ro` dropped from the Brain source's URI: `test_the_brain_source_is_opened_read_only`
    failed;
  * the restore loop in `private_state`'s `finally` replaced by `pass`:
    `test_the_variables_are_restored_and_the_copy_deleted_even_on_failure` failed;
  * `rail_exercise` popping no variables:
    `test_a_rail_sandbox_does_not_inherit_the_private_state_variables` failed.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import capabilities
import feedback
import paths
import rail_exercise
import verify

LEDGER_ENV, BRAIN_ENV = capabilities.LEDGER_PATH_ENV, feedback.DB_PATH_ENV


def _identity(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns, stat.st_ino


def _live(tmp_path: Path) -> tuple[Path, Path]:
    """A 'live' ledger and Brain this test owns."""
    live = tmp_path / "live"
    (live / "feedback").mkdir(parents=True)
    ledger = live / "capabilities.json"
    capabilities.save({"alpha": capabilities._blank_capability("alpha")}, ledger)
    brain = live / "feedback" / "orchestrator.db"
    with sqlite3.connect(brain) as conn:
        conn.execute("CREATE TABLE probe (n INTEGER)")
        conn.executemany("INSERT INTO probe VALUES (?)", [(1,), (2,), (3,)])
    return ledger, brain


def _sources(ledger: Path, brain: Path) -> tuple[str, Path, str, Path]:
    return (LEDGER_ENV, ledger, BRAIN_ENV, brain)


def _child_paths(env: dict | None = None) -> tuple[str, str]:
    """What a child of this process resolves the two paths to."""
    code = "import capabilities, feedback; print(capabilities.REG); print(feedback.DB_PATH)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=paths.REPO_ROOT,
        env={**(env or os.environ), "PYTHONPATH": str(paths.MODULE_DIR)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    reg, db = proc.stdout.strip().splitlines()[-2:]
    return reg, db


@pytest.fixture
def unset(monkeypatch):
    for key in (LEDGER_ENV, BRAIN_ENV, verify.LIVE_STATE_ENV):
        monkeypatch.delenv(key, raising=False)


def test_the_copy_never_writes_either_source(tmp_path, unset):
    ledger, brain = _live(tmp_path)
    before = (_identity(ledger), _identity(brain))
    root = tmp_path / "private"
    root.mkdir()
    snap = verify.snapshot_state(ledger, brain, root)
    assert snap["ledger"].read_bytes() == ledger.read_bytes()
    with sqlite3.connect(snap["brain"]) as conn:
        assert [n for (n,) in conn.execute("SELECT n FROM probe ORDER BY n")] == [1, 2, 3]
    # Now use the copy the way a child would: every `feedback` connection applies the schema and
    # its migrations. They must land on the copy, never on the source.
    saved = feedback.DB_PATH
    try:
        feedback.DB_PATH = snap["brain"]
        feedback._conn().close()
    finally:
        feedback.DB_PATH = saved
    assert (_identity(ledger), _identity(brain)) == before, "a source was written"
    assert "ledger 0.0 MB" in snap["line"] and "Brain" in snap["line"], snap["line"]


def test_the_brain_source_is_opened_read_only(tmp_path, unset, monkeypatch):
    """Belt and braces: the backup only reads, and a read-only open makes a write impossible."""
    ledger, brain = _live(tmp_path)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(verify.sqlite3, "connect", spy)
    root = tmp_path / "private"
    root.mkdir()
    verify.snapshot_state(ledger, brain, root)
    source = [where for where in opened if brain.name in where and str(root) not in where]
    assert len(source) == 1 and source[0].endswith("?mode=ro"), opened


def test_a_child_of_the_run_reads_the_copy(tmp_path, unset):
    ledger, brain = _live(tmp_path)
    with verify.private_state(_sources(ledger, brain)) as state:
        reg, db = _child_paths()
        assert state["ledger"] is not None and Path(reg) == state["ledger"]
        assert Path(db).parent.parent == state["ledger"].parent
        assert Path(reg) != ledger and Path(db) != brain
        assert state["line"].startswith("private copy"), state["line"]


def test_the_variables_are_restored_and_the_copy_deleted_even_on_failure(
    tmp_path, unset, monkeypatch
):
    ledger, brain = _live(tmp_path)
    monkeypatch.setenv(LEDGER_ENV, "/an/operator/choice.json")
    with pytest.raises(RuntimeError, match="the run failed"):
        with verify.private_state(_sources(ledger, brain)) as state:
            private_root = state["ledger"].parent
            assert os.environ[LEDGER_ENV] != "/an/operator/choice.json"
            raise RuntimeError("the run failed")
    assert os.environ[LEDGER_ENV] == "/an/operator/choice.json"
    assert BRAIN_ENV not in os.environ
    assert not private_root.exists(), "the private copy outlived the run"


def test_an_absent_source_bootstraps_privately_and_stays_absent(tmp_path, unset, monkeypatch):
    monkeypatch.setattr(capabilities, "FEATURES_REG", tmp_path / "no-features.json")
    ledger, brain = tmp_path / "live" / "capabilities.json", tmp_path / "live" / "brain.db"
    with verify.private_state(_sources(ledger, brain)) as state:
        assert "absent at the source" in state["line"], state["line"]
        assert os.environ[LEDGER_ENV] == str(state["ledger"])
        rows = capabilities.load(state["ledger"])  # the writing loader bootstraps it...
        assert state["ledger"].exists() and isinstance(rows, dict)
    assert not ledger.exists() and not brain.exists(), "...privately, never at the source"


def test_a_failed_copy_leaves_that_file_live_and_says_so(tmp_path, unset):
    ledger, _ = _live(tmp_path)
    brain = tmp_path / "live" / "feedback" / "not-a-database.db"
    brain.write_bytes(b"this is not sqlite" * 64)
    with verify.private_state(_sources(ledger, brain)) as state:
        assert "Brain LIVE, because the copy failed" in state["line"], state["line"]
        assert BRAIN_ENV not in os.environ, "a failed copy must not point the run at a bad file"
        assert os.environ[LEDGER_ENV] == str(state["ledger"]), "the ledger copy still applies"


def test_the_opt_out_changes_nothing(tmp_path, unset, monkeypatch):
    ledger, brain = _live(tmp_path)
    monkeypatch.setenv(verify.LIVE_STATE_ENV, "1")
    before = dict(os.environ)
    with verify.private_state(_sources(ledger, brain)) as state:
        assert dict(os.environ) == before
        assert state["ledger"] is None and state["line"].startswith("LIVE"), state
    assert dict(os.environ) == before


def test_the_sources_are_asked_of_the_modules_that_read_them():
    ledger_env, ledger, brain_env, brain = verify._state_sources()
    assert (ledger_env, brain_env) == (LEDGER_ENV, BRAIN_ENV)
    assert (ledger, brain) == (Path(capabilities.REG), Path(feedback.DB_PATH))


def test_a_rail_sandbox_does_not_inherit_the_private_state_variables(tmp_path, monkeypatch):
    monkeypatch.setenv(LEDGER_ENV, str(tmp_path / "verify-copy" / "capabilities.json"))
    monkeypatch.setenv(BRAIN_ENV, str(tmp_path / "verify-copy" / "orchestrator.db"))
    contract_path = tmp_path / "env-probe" / "contract.json"
    (contract_path.parent / "fixtures").mkdir(parents=True)
    contract = {
        "capability_id": "env-probe",
        "run": "true",
        "pass_check": (
            f'test -z "${{{LEDGER_ENV}:-}}" && test -z "${{{BRAIN_ENV}:-}}" '
            '&& test -n "$ORCH_LOCAL_RUNTIME"'
        ),
    }
    result = rail_exercise.run_contract(contract_path, contract)
    assert result["status"] == "skip" and result["reason"] == "no runnable break case", result
    assert result["pass_rc"] == 0, "the sandbox inherited a path that leads out of it"
