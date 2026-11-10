"""The local loop's outcomes are read from its own LOCAL runs, not only from hosted keepalive run ids."""
import sqlite3

import value_chain_monitor as vcm


def _db(tmp_path, runs, outcomes):
    path = tmp_path / "brain.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE runs (run_id TEXT, ts REAL, target TEXT, mode TEXT)")
    con.execute("CREATE TABLE outcomes (run_id TEXT, adjudicated_verdict TEXT, verifier_verdict TEXT, "
                "durability TEXT, merged INTEGER, failure_class TEXT)")
    con.executemany("INSERT INTO runs VALUES (?,?,?,?)", runs)
    con.executemany("INSERT INTO outcomes VALUES (?,?,?,?,?,?)", outcomes)
    con.commit()
    con.close()
    return path


def test_local_round_outcome_is_counted(tmp_path):
    target = "stranske/Orchestrator#1293"
    db = _db(tmp_path,
             runs=[("stranske__Orchestrator_1293-codex-2", 2.0, target, "local"),
                   ("stranske__Orchestrator_1293-codex-1", 1.0, target, "local")],
             outcomes=[("stranske__Orchestrator_1293-codex-1", "PASS", "PASS", "durable", 1, None)])
    out = vcm._local_loop_outcomes(db, [target])
    assert out["acted_on"] == 1
    assert out["outcome_success"] == 1  # the graded run wins over a newer ungraded one


def test_hosted_keepalive_run_still_counts(tmp_path):
    target = "stranske/Workflows#4564"
    db = _db(tmp_path,
             runs=[(f"keepalive:{target}:codex", 1.0, "other", "remote")],
             outcomes=[(f"keepalive:{target}:codex", "PASS", None, "pending", 1, None)])
    out = vcm._local_loop_outcomes(db, [target])
    assert out["acted_on"] == 1


def test_finished_pr_without_any_run_is_not_acted_on(tmp_path):
    db = _db(tmp_path, runs=[], outcomes=[])
    assert vcm._local_loop_outcomes(db, ["stranske/Ready#1"])["acted_on"] == 0
