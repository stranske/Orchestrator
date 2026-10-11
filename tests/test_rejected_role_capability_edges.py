"""A capability edge derived from a role edge takes the role edge's arm, and the history is corrected.

THE DEFECT (2026-10-11). `capability_outcome_bridge.backfill_role_capability_edges` joined ANY role
edge and wrote an ACCEPTED capability edge onto its target, then propagated the target's outcome
onto it. A rejected role edge says the run went ahead AGAINST its role, so the role's capability
was credited with an outcome its advice did not produce. On the owner's Brain 334 such edges
existed: 329 derived from the rejected edges the tick wrote for delegations it REFUSED (whose role
edges `feedback.REFUSAL_ROLE_EDGES_MIGRATION` deleted, #491), and 5 from the 3 rejected role edges
that remain. The 329 were 104 of role-triage's 128 accepted-arm terminal outcomes in
`capability_effectiveness`, and 130 of offload's 206.

THE RULE THIS PINS. An accepted role edge derives an accepted capability edge, as before. A
rejected role edge onto a RECORDED run derives a COUNTERFACTUAL capability edge: the control arm,
carrying exactly the grade its role edge was given and never back-propagating. Neither kind is
derived onto a run nothing recorded, and neither inheritance site treats a counterfactual edge as an
attribution. The once-only migration deletes every capability edge from a role run whose pair has
no accepted role edge, keeps the rows in its marker, and its restore returns the Brain exactly.

Rows are written by the production writers on a controlled clock; the "old bridge" rows are written
through the same writer call the bridge made before this change."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import capability_effectiveness
import capability_outcome_bridge as bridge
import feedback
import outcomes

T0 = 1_786_516_882  # 2026-08-12T06:41:22Z
HOUR, DAY = 3600, 86400
TICK_META = {"status": "shadow_only", "disagreement": True}  # what tick.remote_tick passes
VERSIONS = {"role-triage": "cv:triage", "role-prompt": "cv:prompt", "offload": "cv:offload"}
EVERY_EDGE = "SELECT * FROM influence_edges ORDER BY edge_id"
CAPABILITY_EDGES = (
    "SELECT capability_id, accepted, counterfactual, outcome_verdict, merged, durability "
    "FROM influence_edges WHERE influence_type='capability' AND target_run_id=? "
    "ORDER BY capability_id, accepted"
)


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        feedback,
        "_resolve_capability_versions",
        lambda ids: [VERSIONS[i] for i in ids] if all(i in VERSIONS for i in ids) else [],
    )
    clock = {"now": T0}
    monkeypatch.setattr(time, "time", lambda: float(clock["now"]))
    return clock


def _at(clock: dict, ts: int) -> None:
    clock["now"] = ts


def _role(clock: dict, run_id: str, ts: int = T0, *, role: str = "triage") -> None:
    """A role run whose proposal an offload produced: tagged `role-<role>` and `offload`."""
    _at(clock, ts)
    feedback.record_role_run(run_id, role, "snapshot", "gemini", backend_run_id="offload:1")


def _delegate(clock: dict, run_id: str, ts: int = T0, *, accepted_roles=()) -> None:
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


def _disagree(clock: dict, role: str, target: str, ts: int = T0, **extra) -> str:
    """What `tick.remote_tick` writes when a delegation goes ahead against its triage role."""
    _at(clock, ts)
    kwargs = {
        "target_run_id": target,
        "influence_type": "role",
        "influence_id": role,
        "source_run_id": role,
        "accepted": False,
        "metadata": TICK_META,
    }
    kwargs.update(extra)
    return str(feedback.record_influence_edge(**kwargs)["edge_id"])


def _old_bridge(role: str, target: str) -> None:
    """What `backfill_role_capability_edges` wrote for ANY role edge before this change."""
    with feedback._conn() as c:
        for cap_id, version in c.execute(
            "SELECT capability_id, capability_version_id FROM influence_edges "
            "WHERE target_run_id=? AND influence_type='capability'",
            (role,),
        ).fetchall():
            feedback._record_influence_edge_in_conn(
                c,
                target_run_id=target,
                influence_type="capability",
                influence_id=version,
                source_run_id=role,
                accepted=True,
                capability_id=cap_id,
                capability_version_id=version,
                allow_unlinked=True,
            )
            feedback._propagate_outcome_lineage_in_conn(c, target)
        c.commit()


def _capability_edges(target: str) -> list[tuple]:
    with feedback._conn() as c:
        return c.execute(CAPABILITY_EDGES, (target,)).fetchall()


def _edges() -> dict[str, tuple]:
    with feedback._conn() as c:
        return {row[0]: row for row in c.execute(EVERY_EDGE).fetchall()}


def _grade_disagreements() -> dict:
    """The production grader for rejected triage edges. It reads the PR's state first, and a run
    that already has its outcome is graded from that outcome, which every target here has."""
    return outcomes.backfill_triage_disagreements(_state_fn=lambda target: {"state": "CLOSED"})


# ---- the bridge -------------------------------------------------------------------------------


def test_an_accepted_role_edge_still_derives_accepted_credit(brain):
    _role(brain, "role:triage:gemini:1")
    _delegate(brain, "remote:o/r#1:gemini")
    feedback.join_role_to_outcome("role:triage:gemini:1", "remote:o/r#1:gemini", accepted=True)
    feedback.record_outcome(
        "remote:o/r#1:gemini", adjudicated_verdict="PASS", merged=1, durability="durable"
    )
    result = bridge.backfill_role_capability_edges()
    assert (result["backfilled"], result["counterfactual"]) == (2, 0), result
    assert _capability_edges("remote:o/r#1:gemini") == [
        ("offload", 1, 0, "PASS", 1, "durable"),
        ("role-triage", 1, 0, "PASS", 1, "durable"),
    ]


def test_a_run_that_overruled_its_role_is_the_control_arm_never_credit(brain):
    _role(brain, "role:triage:gemini:2")
    _delegate(brain, "remote:o/r#2:gemini")
    _disagree(brain, "role:triage:gemini:2", "remote:o/r#2:gemini")
    feedback.record_outcome(
        "remote:o/r#2:gemini", adjudicated_verdict="PASS", merged=1, durability="durable"
    )
    assert len(_grade_disagreements()["graded"]) == 1
    result = bridge.backfill_role_capability_edges()
    assert (result["backfilled"], result["counterfactual"]) == (2, 2), result
    assert result["counterfactual_graded"] == 2, result
    assert _capability_edges("remote:o/r#2:gemini") == [
        ("offload", 0, 1, "PASS", 1, "durable"),
        ("role-triage", 0, 1, "PASS", 1, "durable"),
    ], "the run's own outcome, in the counterfactual arm"
    with feedback._conn() as c:
        assert not c.execute(
            "SELECT 1 FROM outcomes WHERE run_id='role:triage:gemini:2'"
        ).fetchone(), "a rejected role inherits no PASS"
        control = [
            row
            for row in capability_effectiveness._edge_rows(c)
            if row["run_id"] == "remote:o/r#2:gemini"
        ]
        rows = bridge.collect(lookback_days=36500, conn=c)
    assert {(row["capability_id"], row["counterfactual"]) for row in control} == {
        ("offload", True),
        ("role-triage", True),
    }
    tagged = {row["run_id"]: row["capability_ids"] for row in rows}
    assert tagged["remote:o/r#2:gemini"] == [], "a counterfactual edge credits no outcome"
    again = bridge.backfill_role_capability_edges()
    assert (again["backfilled"], again["counterfactual_graded"]) == (0, 0), again


def test_a_grade_that_arrives_later_reaches_the_control_arm_on_the_next_pass(brain):
    _role(brain, "role:triage:gemini:3")
    _delegate(brain, "remote:o/r#3:gemini")
    _disagree(brain, "role:triage:gemini:3", "remote:o/r#3:gemini")
    first = bridge.backfill_role_capability_edges()
    assert (first["counterfactual"], first["counterfactual_graded"]) == (2, 0), first
    assert [row[3:] for row in _capability_edges("remote:o/r#3:gemini")] == [(None,) * 3] * 2
    feedback.record_outcome(
        "remote:o/r#3:gemini",
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class="unattributed_delegation",
    )
    assert _grade_disagreements()["graded"][0]["verdict"] == "UNATTRIBUTED"
    second = bridge.backfill_role_capability_edges()
    assert (second["backfilled"], second["counterfactual_graded"]) == (0, 2), second
    assert [row[1:] for row in _capability_edges("remote:o/r#3:gemini")] == [
        (0, 1, "UNATTRIBUTED", 0, "abandoned")
    ] * 2, "exactly the role edge's grade, its exclusion included"
    assert bridge.backfill_role_capability_edges()["counterfactual_graded"] == 0


def test_a_rejected_edge_onto_a_run_nothing_recorded_derives_nothing_until_it_is(brain):
    _role(brain, "role:triage:gemini:4")
    # What the tick wrote for a REFUSED delegation before #455: a run id nothing recorded.
    _disagree(brain, "role:triage:gemini:4", "remote:o/r#4:gemini", allow_unlinked=True)
    result = bridge.backfill_role_capability_edges()
    assert (result["backfilled"], result["skipped"]) == (0, 2), result
    assert _capability_edges("remote:o/r#4:gemini") == []
    _delegate(brain, "remote:o/r#4:gemini", T0 + HOUR)
    later = bridge.backfill_role_capability_edges()
    assert (later["counterfactual"], later["skipped"]) == (2, 0), later


def test_one_capability_edge_per_run_and_an_accepted_role_edge_wins(brain):
    _role(brain, "role:triage:gemini:5a")
    _role(brain, "role:triage:gemini:5b")
    _delegate(brain, "remote:o/r#5:gemini")
    _disagree(brain, "role:triage:gemini:5b", "remote:o/r#5:gemini")
    feedback.join_role_to_outcome("role:triage:gemini:5a", "remote:o/r#5:gemini", accepted=True)
    result = bridge.backfill_role_capability_edges()
    assert (result["backfilled"], result["counterfactual"]) == (2, 0), result
    assert [row[:3] for row in _capability_edges("remote:o/r#5:gemini")] == [
        ("offload", 1, 0),
        ("role-triage", 1, 0),
    ]


def test_a_counterfactual_attribution_is_never_inherited(brain):
    """Both inheritance sites, `record_run` and the bridge, inherit accepted attribution only."""
    _role(brain, "role:triage:gemini:6")
    feedback.record_influence_edge(
        target_run_id="role:triage:gemini:6",
        influence_type="capability",
        influence_id="cv:prompt",
        accepted=False,
        capability_id="role-prompt",
        capability_version_id="cv:prompt",
    )
    _delegate(brain, "remote:o/r#6:gemini", accepted_roles=["role:triage:gemini:6"])
    assert [row[:2] for row in _capability_edges("remote:o/r#6:gemini")] == [
        ("offload", 1),
        ("role-triage", 1),
    ], "record_run inherited the accepted attribution only"
    _delegate(brain, "remote:o/r#7:gemini")
    feedback.join_role_to_outcome("role:triage:gemini:6", "remote:o/r#7:gemini", accepted=True)
    bridge.backfill_role_capability_edges()
    assert [row[:2] for row in _capability_edges("remote:o/r#7:gemini")] == [
        ("offload", 1),
        ("role-triage", 1),
    ], "the bridge inherited the accepted attribution only"


# ---- the once-only correction of the history -------------------------------------------------


def _seed_history(clock: dict) -> dict[str, list[str]]:
    """The owner's Brain before 2026-10-11, one row per case, every derived row written by the
    old bridge. Returns edge ids by case."""
    refused, delegated = "remote:o/r#10:gemini", "remote:o/r#11:gemini"
    inherited, legacy = "remote:o/r#12:gemini", "remote:o/r#13:gemini"
    orphan_pair, other = "remote:o/r#14:gemini", "remote:o/r#15:codex"
    unrelated = "remote:o/r#16:gemini"
    for role in ("r10", "r11", "r12", "r13", "r14"):
        _role(clock, f"role:triage:gemini:{role}", T0 - DAY)
    before = set(_edges())
    # A REFUSAL: the tick's rejected edge onto a run nothing recorded. #491 deletes the role edge.
    _disagree(clock, "role:triage:gemini:r10", refused, T0, allow_unlinked=True)
    _old_bridge("role:triage:gemini:r10", refused)
    # A DELEGATION against triage, its rejected edge in the same tick. #491 keeps the role edge.
    _delegate(clock, delegated, T0)
    _disagree(clock, "role:triage:gemini:r11", delegated, T0 + 1)
    feedback.record_outcome(delegated, adjudicated_verdict="FAIL", merged=0, durability="abandoned")
    _old_bridge("role:triage:gemini:r11", delegated)
    # record_run's own inheritance: an accepted role edge, written in the same call.
    _delegate(clock, inherited, T0, accepted_roles=["role:triage:gemini:r12"])
    # An accepted role edge made after the fact, and the old bridge's accepted derivation from it.
    _delegate(clock, legacy, T0)
    feedback.join_role_to_outcome("role:triage:gemini:r13", legacy, accepted=True)
    _old_bridge("role:triage:gemini:r13", legacy)
    # A capability edge from a role run whose pair has no role edge at all.
    _delegate(clock, orphan_pair, T0)
    _old_bridge("role:triage:gemini:r14", orphan_pair)
    # A capability edge whose source is not a role run.
    _delegate(clock, other, T0)
    _delegate(clock, unrelated, T0)
    _at(clock, T0 + 2)
    feedback.record_influence_edge(
        target_run_id=unrelated,
        influence_type="capability",
        influence_id="cv:offload",
        source_run_id=other,
        accepted=True,
        capability_id="offload",
        capability_version_id="cv:offload",
    )
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT edge_id, influence_type, source_run_id, target_run_id FROM influence_edges"
        ).fetchall()
    cases: dict[str, list[str]] = {}
    for edge_id, kind, source, target in rows:
        if edge_id in before:
            name = "role_run_self"
        elif kind == "role":
            name = f"role:{target}"
        elif source == other:
            name = "non_role_source"
        else:
            name = f"capability:{target}"
        cases.setdefault(name, []).append(edge_id)
    return cases


DELETED_CASES = {
    "capability:remote:o/r#10:gemini",
    "capability:remote:o/r#11:gemini",
    "capability:remote:o/r#14:gemini",
}


def _open_as_a_brain_from_before_both_migrations() -> None:
    """The live Brain had neither marker when these rows were written: drop the ones the first
    open of this test wrote, so the next open runs #491's migration and then this one, in order."""
    with feedback._conn() as c:
        c.execute(
            "DELETE FROM data_migrations WHERE name IN (?,?)",
            (
                feedback.REFUSAL_ROLE_EDGES_MIGRATION,
                feedback.REJECTED_ROLE_CAPABILITY_EDGES_MIGRATION,
            ),
        )
        c.commit()
    feedback._conn().close()


def _detail() -> dict:
    with feedback._conn() as c:
        (raw,) = c.execute(
            "SELECT detail FROM data_migrations WHERE name=?",
            (feedback.REJECTED_ROLE_CAPABILITY_EDGES_MIGRATION,),
        ).fetchone()
    return json.loads(raw)


def test_the_derived_edges_without_an_accepted_role_edge_go_and_nothing_else(brain):
    cases = _seed_history(brain)
    assert len(cases["capability:remote:o/r#10:gemini"]) == 2, "the old bridge derived from it"
    assert len(cases["capability:remote:o/r#12:gemini"]) == 2, "record_run inherited both"
    before = _edges()
    _open_as_a_brain_from_before_both_migrations()
    after = _edges()
    deleted = {edge_id for name in DELETED_CASES for edge_id in cases[name]}
    refusal_role_edge = set(cases["role:remote:o/r#10:gemini"])
    assert set(before) - set(after) == deleted | refusal_role_edge, "exactly these rows went"
    for edge_id in set(after):
        assert after[edge_id] == before[edge_id], f"{edge_id} changed"
    detail = _detail()
    assert detail["deleted"] == 6
    assert detail["deleted_by_source"] == {
        "refusal_pair": 2,
        "rejected_role_edge": 2,
        "no_role_edge": 2,
    }, detail["deleted_by_source"]
    assert detail["deleted_by_capability"] == {"offload": 3, "role-triage": 3}
    assert detail["deleted_with_verdict"] == 2, "the delegation's two carried its FAIL"
    columns = detail["columns"]
    with feedback._conn() as c:
        table = [row[1] for row in c.execute("PRAGMA table_info(influence_edges)")]
    assert sorted(columns) == sorted(table), "the snapshot carries every column"
    snapshot = {row[0]: dict(zip(columns, row)) for row in detail["rows"]}
    assert set(snapshot) == deleted
    for edge_id, row in snapshot.items():
        assert row == dict(zip(table, before[edge_id])), edge_id


def test_the_bridge_then_derives_the_control_arm_and_nothing_else(brain):
    _seed_history(brain)
    _open_as_a_brain_from_before_both_migrations()
    result = bridge.backfill_role_capability_edges()
    assert (result["backfilled"], result["counterfactual"]) == (2, 2), result
    assert [row[:3] for row in _capability_edges("remote:o/r#11:gemini")] == [
        ("offload", 0, 1),
        ("role-triage", 0, 1),
    ]
    assert [row[:3] for row in _capability_edges("remote:o/r#16:gemini")] == [
        ("offload", 1, 0)
    ], "the non-role source's own edge, untouched"
    assert _capability_edges("remote:o/r#10:gemini") == [], "a refusal derives nothing"
    assert _capability_edges("remote:o/r#14:gemini") == [], "no role edge, nothing to derive"


def test_a_second_open_deletes_nothing_more(brain):
    _seed_history(brain)
    _open_as_a_brain_from_before_both_migrations()
    once, detail = _edges(), _detail()
    feedback._conn().close()
    assert (_edges(), _detail()) == (once, detail)


def test_restore_returns_the_brain_as_it_was_even_after_the_bridge_has_run(brain):
    _seed_history(brain)
    original = _edges()
    _open_as_a_brain_from_before_both_migrations()
    assert bridge.backfill_role_capability_edges()["counterfactual"] == 2
    assert feedback.main(["restore-rejected-role-capability-edges"]) == 0
    assert feedback.restore_refusal_role_edges()["restored"] == 1
    assert _edges() == original, "every row back, every counterfactual replacement gone"
    assert bridge.backfill_role_capability_edges()["backfilled"] == 0, "nothing derived again"
    feedback._conn().close()
    assert _edges() == original, "the next open deletes nothing again"
    restored = _detail()
    assert (restored["restored"], restored["removed_counterfactual"]) == (6, 2), restored
    again = feedback.restore_rejected_role_capability_edges()
    assert again["restored"] == 0 and again["reason"] == "already restored", again


def test_the_orphan_count_falls_by_exactly_the_unlinked_rows_deleted(brain):
    _seed_history(brain)
    with feedback._conn() as c:
        unlinked = c.execute(
            "SELECT COUNT(*) FROM influence_edges WHERE influence_type='capability' "
            "AND target_event_id IS NULL AND source_run_id != target_run_id"
        ).fetchone()[0]
    assert unlinked == 2, "the refusal's two derived edges point at no run"
    before = feedback.completion_event_health()["orphan_edges"]
    _open_as_a_brain_from_before_both_migrations()
    after = feedback.completion_event_health()["orphan_edges"]
    assert before - after == 3, "the 2 derived edges and #491's refusal role edge"


def test_a_fresh_brain_records_the_migration_with_nothing_deleted(brain):
    feedback._conn().close()
    detail = _detail()
    assert (detail["deleted"], detail["rows"]) == (0, [])


def test_a_store_without_the_edge_table_records_nothing():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts INTEGER, source TEXT)")
        feedback._migrate_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "data_migrations" in tables:
            assert not conn.execute(
                "SELECT 1 FROM data_migrations WHERE name=?",
                (feedback.REJECTED_ROLE_CAPABILITY_EDGES_MIGRATION,),
            ).fetchone(), "no edge table, no migration recorded"
    finally:
        conn.close()
