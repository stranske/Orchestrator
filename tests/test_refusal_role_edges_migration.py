"""The rejected-role edges the tick wrote for REFUSED remote delegations are deleted once, exactly.

THE DEFECT (fixed going forward by #455, 2026-10-04). `tick.remote_tick` wrote an
`influence_type='role', accepted=0` edge from the tick's triage role run to
`remote:<repo>#<n>:<agent>` for every active-tick row, and a refused row records no run. On the
owner's Brain that left 210 such edges, and the tick logs (1,902 ticks) place 208 of them in a tick
that refused their target: 78 point at a run nothing recorded, 25 were written before their run's
first recording and linked to it by that recording's back-fill, 105 were written by later ticks that
refused a target its delegation had left owned. 2 were written in the tick that delegated.

THE TRAP THIS PINS. `record_run` writes by INSERT OR REPLACE, so `runs.ts` is a run's LATEST
recording. A window around `runs.ts` alone deletes a legitimate edge written in the tick of the
run's FIRST recording whenever the target was delegated again later. The first recording survives
as the `created_ts` of the run's trigger/decision completion events, and the selector reads it.

The rows are written here by the production writers on a controlled clock, exactly as the tick
wrote them, and the migration runs on the next open of a Brain that has no marker yet."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import feedback

T0 = 1_786_516_882  # 2026-08-12T06:41:22Z, the tick that delegated stranske/Workflows#2819
HOUR, DAY = 3600, 86400
TICK_META = {"status": "shadow_only", "disagreement": True}  # what tick.remote_tick passes
MARKER = "SELECT detail FROM data_migrations WHERE name=?"
EVERY_EDGE = "SELECT * FROM influence_edges ORDER BY edge_id"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    clock = {"now": T0}
    monkeypatch.setattr(time, "time", lambda: float(clock["now"]))
    return clock


def _at(clock: dict, ts: int) -> None:
    clock["now"] = ts


def _delegate(clock: dict, run_id: str, ts: int, *, accepted_roles=()) -> None:
    """What `dispatcher.delegate_remote` records for a delegation."""
    _at(clock, ts)
    repo_num, agent = run_id.split(":")[1], run_id.split(":")[2]
    feedback.record_run(
        run_id,
        repo_num,
        "implement",
        agent,
        mode="remote",
        pr_number=int(repo_num.split("#")[1]),
        influenced_by_role_run_ids=list(accepted_roles) or None,
    )


def _edge(clock: dict, target: str, role: str, ts: int, *, meta=None, **extra) -> str:
    """What `tick.remote_tick` wrote for a disagreeing role, through the same writer."""
    _at(clock, ts)
    kwargs = {
        "target_run_id": target,
        "influence_type": "role",
        "influence_id": role,
        "source_run_id": role,
        "accepted": False,
        "metadata": TICK_META if meta is None else meta,
        "allow_unlinked": True,  # the tick's writes predate the 2026-08-21 null-target guard
    }
    kwargs.update(extra)
    return str(feedback.record_influence_edge(**kwargs)["edge_id"])


def _edges() -> dict[str, tuple]:
    with feedback._conn() as c:
        return {row[0]: row for row in c.execute(EVERY_EDGE).fetchall()}


def _detail() -> dict:
    with feedback._conn() as c:
        (raw,) = c.execute(MARKER, (feedback.REFUSAL_ROLE_EDGES_MIGRATION,)).fetchone()
    return json.loads(raw)


def _open_as_a_brain_from_before_the_migration() -> None:
    """The live Brain has no marker until the first open after the sync: drop the one this test's
    first open wrote, so the next open runs the migration over the rows seeded since."""
    with feedback._conn() as c:
        c.execute(
            "DELETE FROM data_migrations WHERE name=?", (feedback.REFUSAL_ROLE_EDGES_MIGRATION,)
        )
        c.commit()
    feedback._conn().close()


def _seed(clock: dict) -> dict[str, str]:
    a, b = "remote:o/r#1:gemini", "remote:o/r#2:gemini"
    c, d = "remote:o/r#3:codex", "remote:o/r#4:gemini"
    ids: dict[str, str] = {}
    # A: delegated, one edge in its own tick, then two refusing ticks while it was owned.
    _delegate(clock, a, T0, accepted_roles=["role:triage:gemini:agreed"])
    ids["a_same_tick"] = _edge(clock, a, "role:triage:gemini:a1", T0 + 1)
    ids["a_refused_1"] = _edge(clock, a, "role:triage:gemini:a2", T0 + HOUR)
    ids["a_refused_2"] = _edge(clock, a, "role:triage:gemini:a3", T0 + 2 * HOUR)
    # B: refused every time, never recorded.
    ids["b_never_1"] = _edge(clock, b, "role:triage:gemini:b1", T0)
    ids["b_never_2"] = _edge(clock, b, "role:triage:gemini:b2", T0 + HOUR)
    # C: refused twice, then delegated; the delegation's recording links the two earlier edges.
    ids["c_before_1"] = _edge(clock, c, "role:triage:gemini:c1", T0 - 2 * HOUR)
    ids["c_before_2"] = _edge(clock, c, "role:triage:gemini:c2", T0 - HOUR)
    _delegate(clock, c, T0)
    ids["c_same_tick"] = _edge(clock, c, "role:triage:gemini:c3", T0 + 2)
    # D: delegated, refused, delegated AGAIN (runs.ts moves on), refused.
    _delegate(clock, d, T0)
    ids["d_first_tick"] = _edge(clock, d, "role:triage:gemini:d1", T0)
    ids["d_refused_1"] = _edge(clock, d, "role:triage:gemini:d2", T0 + DAY)
    _delegate(clock, d, T0 + 4 * DAY)
    ids["d_second_tick"] = _edge(clock, d, "role:triage:gemini:d3", T0 + 4 * DAY + 3)
    ids["d_refused_2"] = _edge(clock, d, "role:triage:gemini:d4", T0 + 5 * DAY)
    # Outside the selection, each by exactly one property.
    ids["local_target"] = _edge(clock, "o__r_5-codex-1", "role:triage:gemini:o2", T0)
    ids["other_metadata"] = _edge(
        clock, b, "role:triage:gemini:o3", T0, meta={"status": "rejected", "disagreement": True}
    )
    ids["capability_type"] = _edge(
        clock,
        b,
        "v1",
        T0,
        influence_type="capability",
        source_run_id="role:triage:gemini:b1",
        capability_id="offload",
        capability_version_id="v1",
        accepted=True,
        meta={},
    )
    ids["capability_role"] = _edge(
        clock,
        b,
        "role:triage:gemini:o5",
        T0,
        capability_id="role-triage",
        capability_version_id="v1",
    )
    with feedback._conn() as conn:
        (ids["accepted"],) = conn.execute(
            "SELECT edge_id FROM influence_edges WHERE accepted=1 AND influence_type='role'"
        ).fetchone()
    return ids


DELETED = {
    "a_refused_1",
    "a_refused_2",
    "b_never_1",
    "b_never_2",
    "c_before_1",
    "c_before_2",
    "d_refused_1",
    "d_refused_2",
}
KEPT_SAME_TICK = {"a_same_tick", "c_same_tick", "d_first_tick", "d_second_tick"}


def test_refusal_edges_go_and_same_tick_edges_stay(brain):
    ids = _seed(brain)
    before = _edges()
    assert set(ids.values()) <= set(before), "every seeded edge was written"
    assert before[ids["c_before_1"]][4] is not None, "C's recording linked its earlier edge"
    assert before[ids["b_never_1"]][4] is None, "B's edges point at no run"
    _open_as_a_brain_from_before_the_migration()
    after = _edges()
    gone = {name for name, edge_id in ids.items() if edge_id not in after}
    assert gone == DELETED, gone
    for name, edge_id in ids.items():
        if name not in DELETED:
            assert after[edge_id] == before[edge_id], f"{name} changed"
    assert set(before) - set(after) == {ids[n] for n in DELETED}, "nothing else was deleted"
    detail = _detail()
    assert (detail["examined"], detail["kept_same_tick"], detail["deleted"]) == (12, 4, 8)
    assert detail["deleted_by_reason"] == {
        "never_recorded": 2,
        "before_first_recording": 2,
        "after_a_recording": 4,
    }
    assert detail["deleted_linked"] == 6
    assert set(detail["kept_edge_ids"]) == {ids[n] for n in KEPT_SAME_TICK}
    columns = detail["columns"]
    with feedback._conn() as c:
        table = [row[1] for row in c.execute("PRAGMA table_info(influence_edges)")]
    assert sorted(columns) == sorted(table), "the snapshot carries every column"
    snapshot = {row[0]: dict(zip(columns, row)) for row in detail["rows"]}
    assert set(snapshot) == {ids[n] for n in DELETED}
    for edge_id, row in snapshot.items():
        assert row == dict(zip(table, before[edge_id])), edge_id


def test_a_run_recorded_twice_keeps_the_edge_from_its_first_tick(brain):
    """`runs.ts` holds only the latest recording: the first survives in the completion events."""
    ids = _seed(brain)
    with feedback._conn() as c:
        (runs_ts,) = c.execute("SELECT ts FROM runs WHERE run_id='remote:o/r#4:gemini'").fetchone()
        (first,) = c.execute(
            "SELECT created_ts FROM completion_events "
            "WHERE run_id='remote:o/r#4:gemini' AND phase='decision'"
        ).fetchone()
    assert (runs_ts, first) == (T0 + 4 * DAY, T0), (runs_ts, first)
    _open_as_a_brain_from_before_the_migration()
    after = _edges()
    assert ids["d_first_tick"] in after, "the first delegation's own edge is not a refusal's"
    assert ids["d_second_tick"] in after
    assert ids["d_refused_1"] not in after and ids["d_refused_2"] not in after


def test_a_second_open_deletes_nothing_more(brain):
    _seed(brain)
    _open_as_a_brain_from_before_the_migration()
    once, detail = _edges(), _detail()
    feedback._conn().close()
    assert _edges() == once
    assert _detail() == detail


def test_the_orphan_count_falls_by_exactly_the_never_recorded_edges(brain):
    _seed(brain)
    before = feedback.completion_event_health()["orphan_edges"]
    _open_as_a_brain_from_before_the_migration()
    after = feedback.completion_event_health()["orphan_edges"]
    assert before - after == 2, (before, after)
    assert after == 4, "B's other-metadata, capability and local-target edges stay orphans"


def test_restore_puts_every_row_back_and_the_next_open_keeps_them(brain):
    _seed(brain)
    before = _edges()
    _open_as_a_brain_from_before_the_migration()
    assert len(_edges()) == len(before) - len(DELETED)
    assert feedback.main(["restore-refusal-role-edges"]) == 0
    assert _edges() == before, "every deleted row is back, unchanged"
    feedback._conn().close()
    assert _edges() == before, "the next open does not delete them again"
    assert _detail()["restored"] == len(DELETED)
    again = feedback.restore_refusal_role_edges()
    assert again["restored"] == 0 and again["reason"] == "already restored", again


def test_a_fresh_brain_records_the_migration_with_nothing_examined(brain):
    feedback._conn().close()
    detail = _detail()
    assert (detail["examined"], detail["deleted"], detail["rows"]) == (0, 0, [])


def test_the_selection_names_the_hash_on_the_owners_rows():
    """All 210 rows on the owner's Brain (2026-10-05) carried this hash; the selector computes it
    through the writer's own path, so a drift in either is a red here, not a silent empty run."""
    assert feedback.refusal_role_edge_metadata_hash() == (
        "sha256:5bc0d1a825da60937f87967a20101a500eeaf3e1d5fbee79896d24d6a7656116"
    )
    assert feedback.REFUSAL_ROLE_EDGE_METADATA == TICK_META


def test_a_store_without_the_edge_table_records_nothing_and_runs_alone_still_date():
    """`_migrate_schema` also runs on legacy stores: one without `influence_edges` gets no marker,
    and a run is dated from `runs` alone when `completion_events` is absent."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts INTEGER, source TEXT)")
        feedback._migrate_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "influence_edges" not in tables
        if "data_migrations" in tables:
            assert not conn.execute(
                "SELECT 1 FROM data_migrations WHERE name=?",
                (feedback.REFUSAL_ROLE_EDGES_MIGRATION,),
            ).fetchone(), "no edge table, no migration recorded"
        conn.execute(
            "INSERT INTO runs (run_id, ts, source) VALUES (?,?,?)",
            ("remote:o/r#9:gemini", T0, "orchestrator_remote"),
        )
        assert feedback._run_recording_times(conn, "remote:o/r#9:gemini", {"runs"}) == [T0]
    finally:
        conn.close()
