"""exp_abcd's selftest runs against a PRIVATE Brain, never the one it inherits.

THE INCIDENT (2026-10-02, ~05:33Z). The exec mirror's `verify.py`, run right after a sync, reported
`1 module selftest(s) failed: exp_abcd`. The traceback ended in `feedback._conn -> _migrate_schema`,
at the `UPDATE runs SET assignment = ...` that runs on every connection open. `exp_abcd.py
--selftest` run alone passed, and at least three other verify runs were live at the time, which is
normal on that machine.

THE CAUSE. Measured at 5cc7654: of the selftest's 34 `feedback._conn()` calls, 33 ran inside the
two blocks that swap `feedback.DB_PATH` for a private store. The other one, `_selftest ->
evaluate_prompt -> _active_evidence_contract -> feedback.active_evidence_types`, ran before either
block with the inherited path still in place: the live Brain. `_conn()` WRITES on every open (the
schema, the migrations, a commit) under sqlite's default 5 s busy timeout, so the selftest wrote
learner state on whatever machine ran it, and with concurrent verify runs and the hourly tick that
write could fail to get the lock. Same defect class as PR #358 (selftest scratch at fixed /tmp
paths) and PR #367 (capability_propensity's selftest reaching the live stores).

Deterministic: nothing sleeps, polls or races. The selftest is handed a decoy as the store it
inherits, standing in for the live Brain, so even a regression never opens the real one. Every
`_conn()` call is traced, and the decoy's directory must never come into existence at all, which
also catches a path that reaches the inherited store without going through `_conn()`.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import exp_abcd
import feedback


def test_every_brain_connection_in_the_selftest_opens_a_private_store(tmp_path, monkeypatch):
    live = tmp_path / "inherited-runtime"
    inherited = live / "feedback" / "orchestrator.db"
    monkeypatch.setattr(feedback, "DB_PATH", inherited)
    real_conn = feedback._conn
    calls: list[tuple[Path, str]] = []

    def traced_conn():
        chain = " -> ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_stack()[:-1]
            if Path(frame.filename).name in ("exp_abcd.py", "feedback.py")
        )
        calls.append((feedback.DB_PATH, chain))
        return real_conn()

    # feedback's own functions resolve `_conn` through the module's globals at call time, so this
    # catches `active_evidence_types()` as well as the selftest's direct `feedback._conn()`.
    monkeypatch.setattr(feedback, "_conn", traced_conn)
    exp_abcd._selftest()

    assert len(calls) > 0, (
        "the tracer saw no _conn() call: either it was never installed or the selftest no longer "
        "opens any Brain, and in both cases this test proves nothing until it is re-anchored"
    )
    on_inherited = [chain for path, chain in calls if path == inherited]
    assert on_inherited == [], (
        f"{len(on_inherited)} of {len(calls)} _conn() calls opened the Brain the selftest "
        "inherited, which outside this test is the live one; each open writes the schema, the "
        "migrations and a commit:\n" + "\n".join(on_inherited)
    )
    created = sorted(str(p.relative_to(tmp_path)) for p in live.rglob("*"))
    assert not live.exists(), f"the inherited Brain's location was created: {created}"
    assert feedback.DB_PATH == inherited, "the selftest did not restore the Brain it inherited"
