"""A closer-judge review run is graded by what became of the merge it judged (P3 part B).

THE DEFECT (measured 2026-11-27 on the live Brain). 2,017 `review` runs since 2026-11-10, 34 with an
outcome, none of them about the work. The closer's judge (838 of those runs) decides whether an issue
whose PR merged under a non-PASS verifier may close; the decision and the PR were kept only in the
closer's ledger row, dropped when the target completes, before the merge is swept.

THE RULE. Each ledger judgement is copied into `review_decisions` (run id, decision, `repo#issue`,
`repo#pr` from the ledger KEY). A `close` decision takes the durability the sweep gave the merge of
that PR: durable is PASS, a failed merge is FAIL, an unjudgeable merge is excluded from learning, a
pending one waits. Other decisions have no rule yet and are never graded.

The rows here are written by the production writers (`feedback.record_run` / `record_outcome`)."""

from __future__ import annotations

import json

import pytest

import feedback
import review_outcomes as ro

SUBJECT = "stranske/Workflows#10"
PR = 11


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    state = tmp_path / "closer-core"
    (state / "targets").mkdir(parents=True)
    monkeypatch.setenv("CLOSER_CORE_STATE", str(state))
    return state


def judge_run(run_id, agent="gemini", subject=SUBJECT, task_type="review", ts=None):
    feedback.record_run(
        run_id,
        "offload:" + str(ro.closer_state() / "scratch"),
        task_type,
        agent,
        mode="offload",
        routing_metadata={"subject_id": subject, "subject_source": "caller"},
    )
    if ts is not None:
        with feedback._conn() as c:
            c.execute("UPDATE runs SET ts=? WHERE run_id=?", (ts, run_id))


def ledger(state, run_id, decision, *, key=f"{SUBJECT}@{PR}", name="t"):
    row = {
        "key": key,
        "judgement": {
            "at": 1795000000.5,
            "key": "x",
            "strong": False,
            "result": {"decision": decision, "run_id": run_id, "provider": "gemini"},
        },
    }
    (state / "targets" / f"{name}.json").write_text(json.dumps(row))


def merge(durability, *, pr=PR, merged=1, agent="codex"):
    run_id = f"keepalive:stranske/Workflows#{pr}:{agent}"
    feedback.record_run(
        run_id, f"stranske/Workflows#{pr}", "implement", agent, pr_number=pr, source="keepalive"
    )
    feedback.record_outcome(
        run_id, adjudicated_verdict="PASS", merged=merged, durability=durability
    )


def outcome(run_id):
    with feedback._conn() as c:
        return c.execute(
            "SELECT adjudicated_verdict, durability, failure_class, notes FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


def test_close_on_a_durable_merge_is_a_pass(brain):
    judge_run("offload:gemini:1")
    ledger(brain, "offload:gemini:1", "close")
    merge("durable")
    out = ro.run()
    assert out["ingest"]["recorded"] == 1
    assert out["label"]["graded"] == 1
    verdict, durability, fclass, notes = outcome("offload:gemini:1")
    assert (verdict, durability, fclass) == ("PASS", "durable", None)
    assert notes.startswith(ro.NOTE_PREFIX) and "stranske/Workflows#11" in notes
    # The learner reads it as outcome evidence and as a success.
    assert feedback._has_outcome_evidence(durability, verdict, None, failure_class=fclass)
    assert feedback._is_success(durability, verdict, None)


@pytest.mark.parametrize("durability", sorted(ro.FAILED_MERGES))
def test_close_on_a_merge_that_failed_later_is_a_fail(brain, durability):
    judge_run("offload:codex:2", agent="codex")
    ledger(brain, "offload:codex:2", "close")
    merge(durability)
    ro.run()
    verdict, stored, fclass, _ = outcome("offload:codex:2")
    assert (verdict, stored, fclass) == ("FAIL", durability, None)
    assert not feedback._is_success(stored, verdict, None)


def test_a_pending_merge_waits_and_then_grades(brain):
    judge_run("offload:gemini:3")
    ledger(brain, "offload:gemini:3", "close")
    merge("pending")
    assert ro.run()["label"]["pending"] == 1
    assert outcome("offload:gemini:3") is None
    feedback.record_outcome("keepalive:stranske/Workflows#11:codex", durability="durable")
    assert ro.run()["label"]["graded"] == 1
    assert outcome("offload:gemini:3")[:2] == ("PASS", "durable")


def test_an_unjudgeable_merge_is_excluded_from_learning(brain):
    judge_run("offload:gemini:4")
    ledger(brain, "offload:gemini:4", "close")
    merge("unjudgeable")
    assert ro.run()["label"]["excluded"] == 1
    verdict, durability, fclass, _ = outcome("offload:gemini:4")
    assert (verdict, durability, fclass) == (None, "unjudgeable", feedback.UNJUDGEABLE_MERGE)
    assert fclass in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert not feedback._has_outcome_evidence(durability, verdict, None, failure_class=fclass)


def test_a_rejudged_merge_replaces_the_grade(brain):
    judge_run("offload:gemini:5")
    ledger(brain, "offload:gemini:5", "close")
    merge("durable")
    ro.run()
    feedback.record_outcome("keepalive:stranske/Workflows#11:codex", durability="broke_later")
    out = ro.run()["label"]
    assert out["written"] == 1
    assert outcome("offload:gemini:5")[:2] == ("FAIL", "broke_later")
    assert ro.run()["label"].get("written", 0) == 0  # idempotent once it matches


@pytest.mark.parametrize("decision", ["repair", "followup", "evidence"])
def test_other_decisions_have_no_rule_and_are_never_graded(brain, decision):
    judge_run("offload:codex:6", agent="codex")
    ledger(brain, "offload:codex:6", decision)
    merge("durable")
    assert ro.run()["label"] == {f"awaiting_rule:{decision}": 1}
    assert outcome("offload:codex:6") is None


def test_ingest_takes_only_runs_that_are_this_subjects_review(brain):
    judge_run("offload:gemini:7", subject="stranske/Workflows#99")  # another issue
    ledger(brain, "offload:gemini:7", "close", name="a")
    judge_run("offload:gemini:8", task_type="offload")  # not a review run
    ledger(brain, "offload:gemini:8", "close", name="b")
    ledger(brain, "offload:gemini:missing", "close", name="c")  # no Brain run
    ledger(brain, None, "close", name="d")  # the closer's CLI fallback: no run id
    ledger(brain, "offload:gemini:9", "close", key="not-a-key", name="e")
    (brain / "targets" / "f.json").write_text("{broken")
    counts = ro.ingest()
    assert counts == {
        "subject_mismatch": 2,
        "unknown_run": 1,
        "no_run_id": 1,
        "malformed": 1,
        "unreadable": 1,
    }
    with feedback._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0


def test_the_first_record_of_a_run_is_kept(brain):
    judge_run("offload:gemini:10")
    ledger(brain, "offload:gemini:10", "close")
    assert ro.ingest()["recorded"] == 1
    ledger(brain, "offload:gemini:10", "repair")
    assert ro.ingest()["known"] == 1
    with feedback._conn() as c:
        assert c.execute("SELECT decision, review_of FROM review_decisions").fetchall() == [
            ("close", "stranske/Workflows#11")
        ]


def test_another_writers_outcome_is_never_touched(brain):
    judge_run("offload:gemini:11")
    feedback.record_outcome(
        "offload:gemini:11", adjudicated_verdict="FAIL", notes="research-program"
    )
    ledger(brain, "offload:gemini:11", "close")
    merge("durable")
    assert ro.run()["label"]["foreign_outcome"] == 1
    assert outcome("offload:gemini:11")[0] == "FAIL"


def test_only_the_judged_prs_merge_counts(brain):
    judge_run("offload:gemini:12")
    ledger(brain, "offload:gemini:12", "close")
    merge("broke_later", pr=12)  # a different PR on the same repo
    assert ro.run()["label"]["no_merge_row"] == 1
    assert outcome("offload:gemini:12") is None


def test_restore_deletes_only_this_modules_rows(brain):
    judge_run("offload:gemini:13")
    ledger(brain, "offload:gemini:13", "close")
    merge("durable")
    ro.run()
    assert ro.restore() == 1
    assert outcome("offload:gemini:13") is None
    assert outcome("keepalive:stranske/Workflows#11:codex") is not None


def test_probe_counts_the_chain_and_the_post_deploy_pass_number(brain):
    judge_run("offload:gemini:20", ts=100)
    ledger(brain, "offload:gemini:20", "close", name="a")
    judge_run("offload:codex:21", agent="codex", subject="stranske/Workflows#30", ts=300)
    ledger(brain, "offload:codex:21", "close", key="stranske/Workflows#30@31", name="b")
    judge_run("offload:codex:22", agent="codex", ts=300)  # answered unusably: no ledger judgement
    merge("durable")
    merge("broke_later", pr=31)
    ro.run()
    res = ro.probe(since=200)
    assert res["totals"]["judge_runs"] == 3
    assert res["totals"]["decision_recorded"] == 2
    assert res["totals"]["outcome"] == 2
    assert res["families"]["codex"]["fail"] == 1
    assert res["families"]["gemini"]["pass"] == 1
    assert res["pass_number"]["value"] == 1  # only the run made after `since`
    assert res["pass_number"]["passed"] is False
    text = ro.format_probe(res)
    assert "post-deploy outcomes 1 / 100" in text
    assert "decisions 1 of 2 judge runs" in text


def test_a_drained_rail_prints_zeroes_not_silence(brain):
    out = ro.run()
    assert out == {"ingest": {}, "label": {}}
    text = ro.format_probe(ro.probe())
    assert "graded 0 (PASS 0, FAIL 0)" in text
    assert "post-deploy outcomes 0 / 100" in text
