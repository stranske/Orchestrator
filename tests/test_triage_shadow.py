import json
from types import SimpleNamespace

import pytest

import backlog
import dispatcher
import feedback
import outcomes
import roles
import triage_shadow


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


def items():
    return triage_shadow.candidates_from_search(
        [
            {
                "repository": "stranske/Orchestrator",
                "number": n,
                "title": "Implement",
                "body": "Concrete tasks and acceptance",
                "createdAt": date,
                "labels": ["priority:" + tier],
            }
            for n, date, tier in [
                (1, "2026-01-02", "high"),
                (2, "2026-01-01", "normal"),
                (3, "2026-01-03", "high"),
                (4, "2026-01-04", "low"),
            ]
        ],
        backlog.SUPPORTED_REPOS,
    )


def proposal():
    return {
        "summary": "Advisory only",
        "confidence": "high",
        "global_risks": [],
        "batches": [],
        "recommendations": [
            {
                "target": f"stranske/Orchestrator#{n}",
                "action": "work_now",
                "priority": rank,
                "reason": "Bounded work",
                "batch_id": None,
            }
            for rank, n in enumerate((3, 2, 4, 1), 1)
        ],
    }


def test_a_cycle_records_triage_top_three_beside_the_rule_pick(brain):
    path = brain / "shadow.jsonl"

    def replay(**kw):
        return roles.run_triage_agent(backend="codex", cap={}, learned={}, **kw)

    result = triage_shadow.record_cycle(
        items(), path=path, runner=replay, dispatch=False, proposal_json=proposal()
    )
    assert result["rule_pick"] == "stranske/Orchestrator#1"
    assert result["triage_top_three"] == [
        "stranske/Orchestrator#3",
        "stranske/Orchestrator#2",
        "stranske/Orchestrator#4",
    ]
    assert result["triage_valid"] and not result["live_proposal"]
    assert json.loads(path.read_text()) == result


def test_the_step_never_mutates_labels_or_dispatches(brain, monkeypatch):
    def refuse(*a, **kw):
        pytest.fail("shadow step attempted worker dispatch")

    monkeypatch.setattr(dispatcher, "delegate_remote", refuse)
    monkeypatch.setattr(dispatcher, "delegate", refuse)
    monkeypatch.setattr(backlog, "scoped_blocker_source", lambda: "absent")
    monkeypatch.setattr(backlog, "scoped_blocker_entries", lambda: {})

    def read_only(argv, **kw):
        assert argv[1:3] in (["search", "issues"], ["pr", "list"]), argv
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(triage_shadow.subprocess, "run", read_only)
    assert triage_shadow.discover_candidates() == []
    result = triage_shadow.record_cycle([], path=brain / "rows.jsonl")
    assert result["shadow"] and result["candidate_count"] == 0


def test_backfill_grades_an_ungraded_disagreement_from_pr_state(brain):
    feedback.record_role_run("triage", "triage", "snapshot", "codex", proposal=proposal())
    feedback.record_run("worker", "stranske/Orchestrator#3", "implement", "codex", mode="local")
    feedback.join_role_to_outcome("triage", "worker", accepted=False)
    result = outcomes.backfill_triage_disagreements(_state_fn=lambda target: {"state": "MERGED"})
    assert result["source"] == "backfill" and result["graded"][0]["verdict"] == "PASS"
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,durability FROM influence_edges "
            "WHERE source_run_id='triage' AND target_run_id='worker'"
        ).fetchone() == (0, 1, "PASS", "pending")
        assert (
            "source=backfill"
            in conn.execute("SELECT notes FROM outcomes WHERE run_id='worker'").fetchone()[0]
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM outcomes WHERE run_id='triage'").fetchone()[0] == 0
        )
    assert (
        outcomes.backfill_triage_disagreements(_state_fn=lambda t: pytest.fail("duplicate lookup"))[
            "candidates"
        ]
        == 0
    )


@pytest.mark.parametrize("state", [{"state": "OPEN"}, {"lookup_status": "lookup_failed"}, None])
def test_backfill_unknown_or_open_does_not_become_a_verdict(brain, state):
    feedback.record_role_run("triage", "triage", "snapshot", "codex", proposal=proposal())
    feedback.record_run("worker", "stranske/Orchestrator#3", "implement", "codex", mode="local")
    feedback.join_role_to_outcome("triage", "worker", accepted=False)
    assert outcomes.backfill_triage_disagreements(_state_fn=lambda t: state)["graded"] == []
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_shadow_filter_retains_supported_and_excludes_scoped_and_linked(brain):
    found = items()
    filtered = triage_shadow.candidates_from_search(
        found,
        backlog.SUPPORTED_REPOS,
        scoped={"stranske/Orchestrator#1"},
        linked={"stranske/Orchestrator#3"},
    )
    assert [row["target"] for row in filtered] == [
        "stranske/Orchestrator#2",
        "stranske/Orchestrator#4",
    ]


def test_a_failed_or_truncated_population_never_calls_the_role(brain, monkeypatch):
    def fake(*a, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="lookup failed")

    with pytest.raises(RuntimeError, match="lookup failed"):
        triage_shadow.discover_candidates(run=fake)

    def truncated(*a, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps([{}] * 1000), stderr="")

    with pytest.raises(ValueError, match="truncated"):
        triage_shadow.discover_candidates(run=truncated)


def test_replay_and_invalid_proposals_never_score_as_live_rankings(brain):
    path = brain / "rows.jsonl"
    triage_shadow.record_cycle(
        items(), path=path, runner=lambda **k: {"proposal": proposal()}, dispatch=False
    )
    triage_shadow.record_cycle(
        items(), path=path, runner=lambda **k: {"errors": ["provider failed"]}
    )
    assert triage_shadow.summary(path)["triage"]["judged"] == 0


def test_summary_counts_only_known_durable_target_outcomes(brain):
    path = brain / "rows.jsonl"
    triage_shadow.record_cycle(
        items(),
        path=path,
        runner=lambda **k: {
            "proposal": proposal(),
            "role_run_id": "role",
            "backend_run_id": "offload",
        },
    )
    feedback.record_run("worker", "stranske/Orchestrator#3", "implement", "codex", mode="local")
    feedback.record_outcome("worker", merged=True, adjudicated_verdict="PASS", durability="pending")
    assert triage_shadow.summary(path)["triage"]["judged"] == 0
    feedback.record_outcome("worker", durability="durable")
    result = triage_shadow.summary(path)
    assert result["triage"] == {"judged": 1, "merged_durable": 1}
    assert result["rule"]["judged"] == 0


def test_backfill_preserves_existing_verifier_concerns(brain):
    feedback.record_role_run("triage", "triage", "snapshot", "codex", proposal=proposal())
    feedback.record_run("worker", "stranske/Orchestrator#3", "implement", "codex", mode="local")
    feedback.join_role_to_outcome("triage", "worker", accepted=False)
    feedback.record_outcome(
        "worker", merged=True, adjudicated_verdict="CONCERNS", durability="pending"
    )
    result = outcomes.backfill_triage_disagreements(_state_fn=lambda t: {"state": "MERGED"})
    assert result["graded"][0]["verdict"] == "CONCERNS"
    with feedback._conn() as conn:
        assert (
            conn.execute(
                "SELECT adjudicated_verdict FROM outcomes WHERE run_id='worker'"
            ).fetchone()[0]
            == "CONCERNS"
        )


def test_empty_corpus_does_not_open_the_brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_conn", lambda: pytest.fail("empty summary opened Brain"))
    assert "n/a (0 judged)" in triage_shadow.summary_line(tmp_path / "absent.jsonl")


def test_unreadable_outcomes_are_unknown(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "shadow.jsonl"
    path.write_text(
        json.dumps({"schema_version": 1, "triage_valid": True, "live_proposal": True}) + "\n"
    )

    def unavailable():
        raise sqlite3.OperationalError("unreadable fixture")

    monkeypatch.setattr(feedback, "_conn", unavailable)
    text = triage_shadow.summary_line(path)
    assert "UNKNOWN" in text and "0 judged" not in text
