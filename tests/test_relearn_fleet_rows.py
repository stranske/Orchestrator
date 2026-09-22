"""The route-weight learners read the fleet's outcomes, under the detection floor, with honest telemetry.

Until 2026-09-21 both learners kept only `assignment='experimental'` rows, so codex's implement cell
rested on 27 outcomes while the Brain held about 1,490 attributed fleet outcomes for it, and the
export ranked local eval panels. The owner's decision: the fleet's durability-labelled outcomes are the
evidence the learner exists to read. They enter under the 2026-08-29 broke-later detection floor the
receiver rail already uses, near-empty cost telemetry is imputed rather than read as cheap, and every
rationale names the population so the mix stays auditable. `ORCH_RELEARN_FLEET_ROWS=0` is the way back.
"""

from __future__ import annotations

import time

import pytest

import feedback
import router

PRIORS = {"implement": {"codex": 0.5, "cursor": 0.5}}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.delenv("ORCH_RELEARN_FLEET_ROWS", raising=False)
    return tmp_path


def _fleet_run(rid: str, agent: str, *, durable: bool = True, assignment: str = "assigned"):
    feedback.record_run(
        rid,
        f"stranske/Repo#{rid[-3:]}",
        "implement",
        agent,
        mode="remote",
        source="keepalive",
        assignment=assignment,
    )
    feedback.record_outcome(
        rid,
        adjudicated_verdict="PASS" if durable else "FAIL",
        merged=True,
        durability="durable" if durable else "broke_later",
    )


def _cell(version: int, agent: str) -> dict:
    with feedback._conn() as c:
        row = c.execute(
            "SELECT posterior, n_obs, rationale, cost_per_success FROM route_weights "
            "WHERE version=? AND task_type='implement' AND agent=?",
            (version, agent),
        ).fetchone()
    return {"posterior": row[0], "n_obs": row[1], "rationale": row[2], "cps": row[3]}


def test_fleet_outcomes_move_the_weights_and_are_named_in_the_rationale(brain):
    for i in range(10):
        _fleet_run(f"keepalive:o/r#c{i:02d}:codex", "codex", durable=True)
    for i in range(10):
        _fleet_run(f"keepalive:o/r#u{i:02d}:cursor", "cursor", durable=(i < 3))
    v = feedback.relearn_quality(PRIORS)
    codex, cursor = _cell(v, "codex"), _cell(v, "cursor")
    assert codex["n_obs"] == 10 and cursor["n_obs"] == 10
    assert codex["posterior"] > 0.5 > cursor["posterior"], (codex, cursor)
    assert "population=fleet" in codex["rationale"] and "fleet_rows=10" in codex["rationale"]
    assert "pre_detection_skipped=0" in codex["rationale"] and "telemetry=ok" in codex["rationale"]
    assert feedback.current_weights("implement", v)[0]["agent"] == "codex"


def test_rows_judged_before_broke_later_detection_are_counted_but_not_scored(brain):
    for i in range(6):
        _fleet_run(f"keepalive:o/r#p{i:02d}:codex", "codex", durable=True)
    with feedback._conn() as c:
        c.execute(
            "UPDATE outcomes SET durability_checked_ts=? WHERE run_id LIKE 'keepalive:o/r#p0%'",
            (feedback.DURABILITY_DETECTION_SINCE - 86400,),
        )
    v = feedback.relearn_quality(PRIORS)
    codex = _cell(v, "codex")
    assert codex["n_obs"] == 0 and codex["posterior"] == 0.5, codex
    assert "pre_detection_skipped=6" in codex["rationale"] and "fleet_rows=0" in codex["rationale"]


def test_the_kill_switch_restores_the_experimental_only_population(brain, monkeypatch):
    for i in range(8):
        _fleet_run(f"keepalive:o/r#k{i:02d}:codex", "codex", durable=True)
    feedback.record_run("exp-1", "o/r#exp", "implement", "cursor", mode="local")
    feedback.record_outcome("exp-1", adjudicated_verdict="PASS", merged=True, durability="durable")
    monkeypatch.setenv("ORCH_RELEARN_FLEET_ROWS", "0")
    v = feedback.relearn_quality(PRIORS)
    codex, cursor = _cell(v, "codex"), _cell(v, "cursor")
    assert (
        codex["n_obs"] == 0 and codex["posterior"] == 0.5
    ), "fleet rows are out when the switch is off"
    assert cursor["n_obs"] == 1 and "population=experimental" in cursor["rationale"]
    monkeypatch.delenv("ORCH_RELEARN_FLEET_ROWS")
    v2 = feedback.relearn_quality(PRIORS)
    assert _cell(v2, "codex")["n_obs"] == 8


def test_near_empty_cost_telemetry_is_imputed_not_read_as_cheap(brain):
    """The rule that lived only in relearn(): cursor's costed runs carried ~200 tokens each."""
    for i in range(10):
        _fleet_run(f"keepalive:o/r#t{i:02d}:codex", "codex", durable=True)
        feedback.record_cost(
            f"keepalive:o/r#t{i:02d}:codex", tokens_in=200_000, tokens_out=20_000, cost_usd=0.9
        )
        _fleet_run(f"keepalive:o/r#s{i:02d}:cursor", "cursor", durable=True)
        feedback.record_cost(
            f"keepalive:o/r#s{i:02d}:cursor", tokens_in=150, tokens_out=50, cost_usd=0.05
        )
    v = feedback.relearn_quality(PRIORS)
    codex, cursor = _cell(v, "codex"), _cell(v, "cursor")
    assert "telemetry=ok" in codex["rationale"] and "effort_src=m" in codex["rationale"]
    assert "telemetry=implausible" in cursor["rationale"], cursor["rationale"]
    assert "effort_src=m" not in cursor["rationale"], "cost and tokens must not read as measured"
    assert (
        cursor["cps"] == 0.0
    ), "the stored column stays MEASURED-or-zero, never a fabricated number"
    # identical outcomes, so the ranking must not be decided by five-cent rows
    assert feedback.current_weights("implement", v)[0]["agent"] == "codex"


def test_legacy_relearn_reads_the_same_population_under_the_same_floor(brain):
    for i in range(5):
        _fleet_run(f"keepalive:o/r#l{i:02d}:codex", "codex", durable=True)
    _fleet_run("keepalive:o/r#old:codex", "codex", durable=True)
    with feedback._conn() as c:
        c.execute(
            "UPDATE outcomes SET durability_checked_ts=? WHERE run_id='keepalive:o/r#old:codex'",
            (feedback.DURABILITY_DETECTION_SINCE - 1,),
        )
    v = feedback.relearn(PRIORS)
    with feedback._conn() as c:
        n_obs = c.execute(
            "SELECT n_obs FROM route_weights WHERE version=? AND task_type='implement' AND agent='codex'",
            (v,),
        ).fetchone()[0]
    assert n_obs == 5, "five post-detection fleet rows count, the pre-detection one does not"


def test_the_detection_floor_has_one_definition():
    assert router.DURABILITY_DETECTION_SINCE is feedback.DURABILITY_DETECTION_SINCE
    assert (
        router.DURABILITY_DETECTION_SINCE_DATE
        == feedback.DURABILITY_DETECTION_SINCE_DATE
        == "2026-08-29"
    )
    assert (
        time.strftime("%Y-%m-%d", time.gmtime(feedback.DURABILITY_DETECTION_SINCE)) == "2026-08-29"
    )
