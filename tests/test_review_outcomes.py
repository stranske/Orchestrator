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
    opener = tmp_path / "opener-core"
    (opener / "targets").mkdir(parents=True)
    monkeypatch.setenv("OPENER_CORE_STATE", str(opener))
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


@pytest.mark.parametrize("decision", ["repair", "evidence"])
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
    # no round record at all is reported, never read as "nothing judged"
    assert out == {
        "ingest": {},
        "rounds": {"rounds_missing": 1},
        "opener_rounds": {"rounds_missing": 1},
        "label": {},
    }
    text = ro.format_probe(ro.probe())
    assert "graded 0 (PASS 0, FAIL 0)" in text
    assert "post-deploy outcomes 0 / 100" in text


# --- round records (second unit): both lanes' rounds.jsonl, history included -----------------


def rounds(state, *entries):
    """Append rounds: each entry is (iso ts, [items])."""
    with (state / "rounds.jsonl").open("a") as fh:
        for ts, items in entries:
            fh.write(json.dumps({"ts": ts, "items": items}) + "\n")


def closer_item(decision, *, run_id=None, provider="gemini", target=SUBJECT, pr=PR):
    judge = {"decision": decision, "provider": provider, "strong": False}
    if run_id:
        judge["run_id"] = run_id
    return {"target": target, "pr": pr, "action": "judge", "judge": judge}


def ts_of(iso):
    import calendar
    import time

    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def test_a_round_judgement_with_its_run_id_is_recorded_and_graded(brain):
    judge_run("offload:gemini:30")
    rounds(brain, ("2026-11-27T10:00:00Z", [closer_item("close", run_id="offload:gemini:30")]))
    merge("durable")
    out = ro.run()
    assert out["rounds"] == {"recorded": 1}
    assert outcome("offload:gemini:30")[:2] == ("PASS", "durable")
    assert ro.run()["rounds"] == {"known": 1}  # every tick re-reads the file; first record kept


def test_an_old_round_without_a_run_id_joins_the_one_run_inside_it(brain):
    t0, t1 = ts_of("2026-11-20T10:00:00Z"), ts_of("2026-11-20T10:15:00Z")
    judge_run("offload:gemini:31", ts=t0 - 60)  # before the previous round ended: not this one
    judge_run("offload:gemini:32", ts=t0 + 300)
    judge_run("offload:codex:33", agent="codex", ts=t0 + 400)  # another agent
    rounds(
        brain,
        ("2026-11-20T10:00:00Z", []),
        ("2026-11-20T10:15:00Z", [closer_item("close")]),
    )
    merge("durable")
    assert ro.run()["rounds"] == {"recorded": 1}
    assert outcome("offload:gemini:32")[:2] == ("PASS", "durable")
    assert outcome("offload:gemini:31") is None
    assert t1 > t0


def test_two_same_agent_runs_in_one_round_are_ambiguous_not_guessed(brain):
    t0 = ts_of("2026-11-20T10:00:00Z")
    judge_run("offload:gemini:34", ts=t0 + 100)
    judge_run("offload:gemini:35", ts=t0 + 200)
    rounds(
        brain,
        ("2026-11-20T10:00:00Z", []),
        ("2026-11-20T10:15:00Z", [closer_item("close")]),
        ("2026-11-20T11:00:00Z", [closer_item("close", target="stranske/Workflows#77")]),
    )
    assert ro.ingest_rounds(ro.PRODUCER) == {"ambiguous": 1, "no_run": 1}


def test_a_round_run_id_for_another_issue_is_refused(brain):
    judge_run("offload:gemini:36", subject="stranske/Workflows#99")
    rounds(brain, ("2026-11-27T10:00:00Z", [closer_item("close", run_id="offload:gemini:36")]))
    assert ro.ingest_rounds(ro.PRODUCER) == {"subject_mismatch": 1}


def opener_run(run_id, agent="codex", subject=SUBJECT, ts=None):
    feedback.record_run(
        run_id,
        "offload:" + str(ro.opener_state() / "scratch"),
        "review",
        agent,
        mode="offload",
        routing_metadata={"subject_id": subject, "subject_source": "caller"},
    )
    if ts is not None:
        with feedback._conn() as c:
            c.execute("UPDATE runs SET ts=? WHERE run_id=?", (ts, run_id))


def opener_row(target=SUBJECT, **fields):
    import re

    path = ro.opener_state() / "targets" / (re.sub(r"[^a-z0-9]+", "_", target.lower()) + ".json")
    path.write_text(json.dumps({"target": target, "state": "pr_linked", **fields}))


def opener_item(decision, action, run_id, target=SUBJECT):
    return {
        "target": target,
        "action": action,
        "judge": {"decision": decision, "provider": "codex", "run_id": run_id},
    }


def test_an_acted_opener_start_is_graded_by_the_merge_of_the_pr_it_led_to(brain):
    opener_run("offload:codex:40")
    rounds(
        ro.opener_state(),
        ("2026-11-27T10:00:00Z", [opener_item("start", "start", "offload:codex:40")]),
    )
    assert ro.run()["opener_rounds"] == {"no_pr_yet": 1}  # waits for the opener to link a PR
    opener_row(pr=PR)
    merge("broke_later")
    out = ro.run()
    assert out["opener_rounds"] == {"recorded": 1}
    verdict, durability, _, notes = outcome("offload:codex:40")
    assert (verdict, durability) == ("FAIL", "broke_later")
    assert notes.startswith(ro.OPENER_NOTE_PREFIX)
    assert ro.restore() == 1


def test_an_opener_judgement_the_round_did_not_act_on_is_skipped(brain):
    opener_run("offload:codex:41")
    opener_row(pr=PR)
    rounds(
        ro.opener_state(),
        ("2026-11-27T10:00:00Z", [opener_item("start", "format-unfixed", "offload:codex:41")]),
    )
    assert ro.ingest_rounds(ro.OPENER) == {"not_acted": 1}


def test_an_opener_park_is_recorded_but_has_no_rule(brain):
    opener_run("offload:codex:42")
    rounds(
        ro.opener_state(),
        ("2026-11-27T10:00:00Z", [opener_item("park", "park", "offload:codex:42")]),
    )
    merge("durable")
    out = ro.run()
    assert out["opener_rounds"] == {"recorded": 1}
    assert out["label"] == {"awaiting_rule:park": 1}
    assert outcome("offload:codex:42") is None


def test_probe_counts_the_opener_chain_apart_from_the_pass_number(brain):
    opener_run("offload:codex:43", ts=300)
    opener_row(pr=PR)
    rounds(
        ro.opener_state(),
        ("2026-11-27T10:00:00Z", [opener_item("start", "start", "offload:codex:43")]),
    )
    merge("durable")
    ro.run()
    res = ro.probe(since=200)
    assert res["opener"]["totals"]["outcome"] == 1
    assert res["opener"]["families"]["codex"]["pass"] == 1
    assert res["pass_number"]["value"] == 0  # the pass number is the closer judge's
    assert (
        "review outcomes (opener judge): judge runs 1; decision recorded 1; start 1; graded 1"
        in (ro.format_probe(res))
    )


# --- lever 4: closer `followup` by the fate of the issue it filed; opener `done` by a reopen -----

FILED = "stranske/Workflows#50"
NOW = ts_of("2026-11-27T12:00:00Z")


def fate(state="CLOSED", reason="COMPLETED", prs=(), reopened=()):
    return {
        "state": state,
        "stateReason": reason if state == "CLOSED" else None,
        "closedAt": "2026-11-20T00:00:00Z" if state == "CLOSED" else None,
        "timelineItems": {"nodes": [{"createdAt": r} for r in reopened]},
        "closedByPullRequestsReferences": {"nodes": [{"number": n, "merged": m} for n, m in prs]},
    }


def github(fates: dict, calls: list | None = None):
    """A GraphQL stand-in answering `issue_fates`' aliased query from {`repo#n`: record}."""
    import re

    def gh(query):
        if calls is not None:
            calls.append(query)
        out = {}
        for alias, owner, name, number in re.findall(
            r'(i\d+): repository\(owner:"([^"]+)",name:"([^"]+)"\)\{issue\(number:(\d+)\)', query
        ):
            out[alias] = {"issue": fates.get(f"{owner}/{name}#{number}")}
        return out

    return gh


def filed_followup(brain, run_id="offload:gemini:60", *, provider="gemini", route=None, acted=True):
    """A judged `followup` (an old round, no run id) and, a round later, the item that filed it."""
    judge_run(run_id, agent=provider, ts=ts_of("2026-11-10T10:05:00Z"))
    item = closer_item("followup", provider=provider)
    filing = dict(item, action="followup", followup=int(FILED.rsplit("#", 1)[1]))
    if route:
        filing["route"] = route
    rounds(
        brain,
        ("2026-11-10T10:00:00Z", []),
        ("2026-11-10T10:15:00Z", [item]),
        *([("2026-11-10T11:15:00Z", [filing])] if acted else []),
    )
    ro.ingest_rounds(ro.PRODUCER)


def implemented(durability, pr=51):
    """The filed follow-up's own merge, as the sweep judged it."""
    run_id = f"keepalive:stranske/Workflows#{pr}:codex"
    feedback.record_run(
        run_id, f"stranske/Workflows#{pr}", "implement", "codex", pr_number=pr, source="keepalive"
    )
    feedback.record_outcome(run_id, adjudicated_verdict="PASS", merged=1, durability=durability)


def test_a_followup_whose_filed_issue_merged_durably_is_a_pass(brain):
    filed_followup(brain)
    implemented("durable")
    calls: list = []
    out = ro.label(now=NOW, _gh=github({FILED: fate(prs=[(51, True), (52, False)])}, calls))
    assert out["written"] == 1 and out["graded"] == 1
    verdict, durability, fclass, notes = outcome("offload:gemini:60")
    assert (verdict, durability, fclass) == ("PASS", "durable", None)
    assert notes.startswith(ro.NOTE_PREFIX) and FILED in notes and "#51" in notes
    assert feedback._is_success(durability, verdict, None)
    # final: the issue is not read again
    assert ro.label(now=NOW, _gh=github({}, calls)).get("written", 0) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["NOT_PLANNED", "DUPLICATE"])
def test_a_followup_whose_filed_issue_was_closed_not_done_is_a_fail(brain, reason):
    filed_followup(brain)
    ro.label(now=NOW, _gh=github({FILED: fate(reason=reason)}))
    verdict, durability, fclass, notes = outcome("offload:gemini:60")
    assert (verdict, durability, fclass) == ("FAIL", "abandoned", None)
    assert reason.lower() in notes
    assert not feedback._is_success(durability, verdict, None)


@pytest.mark.parametrize(
    "record, waits",
    [
        (fate(state="OPEN"), "followup_open"),
        (fate(), "closed_without_merge"),
        (fate(prs=[(51, True)]), "no_merge_row"),
        (None, "gh_unanswered"),
    ],
)
def test_a_followup_without_a_final_fate_waits_and_writes_nothing(brain, record, waits):
    filed_followup(brain)
    assert ro.label(now=NOW, _gh=github({FILED: record}))[waits] == 1
    assert outcome("offload:gemini:60") is None


def test_a_followup_whose_filed_work_merged_and_failed_is_not_graded(brain):
    # A failed merge grades whoever implemented the follow-up, not the claim that it was real work.
    filed_followup(brain)
    implemented("reverted")
    assert (
        ro.label(now=NOW, _gh=github({FILED: fate(prs=[(51, True)])}))["followup_merge_failed"] == 1
    )
    assert outcome("offload:gemini:60") is None


def test_a_backlog_route_and_an_unfiled_followup_have_no_rule(brain):
    filed_followup(brain, route="backlog")
    judge_run("offload:codex:61", agent="codex")  # judged followup, never filed
    rounds(brain, ("2026-11-27T10:00:00Z", [closer_item("followup", run_id="offload:codex:61")]))
    ro.ingest_rounds(ro.PRODUCER)
    out = ro.label(now=NOW, _gh=github({FILED: fate(reason="NOT_PLANNED")}))
    assert out["followup_backlog_route"] == 1 and out["followup_not_acted"] == 1
    assert outcome("offload:gemini:60") is None


def test_a_filing_is_never_credited_to_another_agents_judgement(brain):
    # The latest decision on the issue is gemini's; the filing item names codex: unattributed.
    filed_followup(brain)
    rounds(
        brain,
        (
            "2026-11-10T12:15:00Z",
            [dict(closer_item("followup", provider="codex"), action="followup", followup=50)],
        ),
    )
    with feedback._conn() as c:
        filed, counts = ro.acted_followups(c)
    assert set(filed) == {"offload:gemini:60"} and counts["followup_unattributed"] == 1


def test_a_weak_and_a_strong_judge_in_one_round_are_told_apart_by_agent(brain):
    judge_run("offload:gemini:63", ts=ts_of("2026-11-10T10:05:00Z"))
    judge_run("offload:codex:64", agent="codex", ts=ts_of("2026-11-10T10:06:00Z"))
    rounds(
        brain,
        ("2026-11-10T10:00:00Z", []),
        (
            "2026-11-10T10:15:00Z",
            [closer_item("evidence"), closer_item("followup", provider="codex")],
        ),
        (
            "2026-11-10T11:15:00Z",
            [dict(closer_item("followup", provider="codex"), action="followup", followup=50)],
        ),
    )
    ro.ingest_rounds(ro.PRODUCER)
    with feedback._conn() as c:
        assert ro.acted_followups(c)[0] == {"offload:codex:64": (FILED, "followup")}


def test_a_filing_item_with_its_run_id_is_attributed_directly(brain):
    judge_run("offload:claude:62", agent="claude")
    item = closer_item("followup", provider="claude", run_id="offload:claude:62")
    rounds(brain, ("2026-11-27T10:00:00Z", [dict(item, action="followup", followup=50)]))
    ro.ingest_rounds(ro.PRODUCER)
    with feedback._conn() as c:
        assert ro.acted_followups(c)[0] == {"offload:claude:62": (FILED, "followup")}


def opener_done(run_id="offload:codex:70", when="2026-11-10T10:00:00Z"):
    opener_run(run_id)
    rounds(ro.opener_state(), (when, [opener_item("done", "closed-done", run_id)]))
    ro.ingest_rounds(ro.OPENER)


def test_an_opener_done_that_stays_closed_past_the_grace_is_a_pass(brain):
    opener_done()
    ro.label(now=NOW, _gh=github({SUBJECT: fate()}))
    verdict, durability, _f, notes = outcome("offload:codex:70")
    assert (verdict, durability) == ("PASS", "durable")
    assert notes.startswith(ro.OPENER_NOTE_PREFIX) and "7 days" in notes


def test_an_opener_done_that_was_reopened_is_a_fail(brain):
    opener_done()
    ro.label(now=NOW, _gh=github({SUBJECT: fate(reopened=["2026-11-11T09:00:00Z"])}))
    assert outcome("offload:codex:70")[:2] == ("FAIL", "reopened")


def test_an_opener_done_inside_the_grace_waits(brain):
    opener_done(when="2026-11-25T10:00:00Z")
    assert ro.label(now=NOW, _gh=github({SUBJECT: fate()}))["done_in_grace"] == 1
    assert outcome("offload:codex:70") is None
    # an earlier reopen (before the judge closed it) is not this close's reopen
    opener_done("offload:codex:71", when="2026-11-10T10:00:00Z")
    with feedback._conn() as c:
        c.execute("DELETE FROM review_decisions WHERE run_id='offload:codex:70'")
    ro.label(now=NOW, _gh=github({SUBJECT: fate(reopened=["2026-11-01T00:00:00Z"])}))
    assert outcome("offload:codex:71")[:2] == ("PASS", "durable")


def test_the_probe_prints_each_rule_including_a_drained_zero(brain):
    filed_followup(brain)
    implemented("durable")
    ro.label(now=NOW, _gh=github({FILED: fate(prs=[(51, True)])}))
    text = ro.format_probe(ro.probe())
    assert "rule closer followup: decisions 1, graded 1 (PASS 1, FAIL 0)" in text
    assert "rule opener done: decisions 0, graded 0 (PASS 0, FAIL 0)" in text
