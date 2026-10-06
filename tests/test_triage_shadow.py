import hashlib
import json
from pathlib import Path
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
    candidates = items()

    def replay(**kw):
        return roles.run_triage_agent(backend="codex", cap={}, learned={}, **kw)

    result = triage_shadow.record_cycle(
        list(reversed(candidates)),
        path=path,
        runner=replay,
        dispatch=False,
        proposal_json=proposal(),
    )
    assert result["candidate_count"] == len(candidates)
    assert result["candidate_targets"] == [item["target"] for item in candidates]
    assert result["rule_pick"] == "stranske/Orchestrator#1"
    assert result["triage_top_three"] == [
        "stranske/Orchestrator#3",
        "stranske/Orchestrator#2",
        "stranske/Orchestrator#4",
    ]
    assert result["triage_valid"] and not result["live_proposal"]
    assert result["role_run_id"] is None and result["backend_run_id"] is None
    assert result["backend"] == "codex" and result["decision_source"] == "triage_agent"
    assert (
        result["snapshot_sha256"]
        == hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
    )
    assert (
        result["implementation_sha256"]
        == hashlib.sha256(Path(triage_shadow.__file__).read_bytes()).hexdigest()
    )
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
def test_the_step_never_mutates_labels_or_dispatches(brain, monkeypatch, capsys, entrypoint):
    def refuse(*a, **kw):
        pytest.fail("shadow step attempted worker dispatch")

    monkeypatch.setattr(dispatcher, "delegate_remote", refuse)
    monkeypatch.setattr(dispatcher, "delegate", refuse)
    monkeypatch.setattr(backlog, "scoped_blocker_source", lambda: "absent")
    monkeypatch.setattr(backlog, "scoped_blocker_entries", lambda: {})
    monkeypatch.setattr(backlog, "load_scoped_blockers", lambda: set())
    monkeypatch.setattr(roles, "_role_capability_event", lambda *a, **kw: None)
    reads = []

    def read_only(argv, **kw):
        reads.append(argv)
        if argv[1:3] == ["search", "issues"]:
            tier = argv[argv.index("--label") + 1]
            rows = [
                {**item, "repository": {"nameWithOwner": item["repository"]}}
                for item in items()
                if tier in item["labels"]
            ]
        elif argv[1:3] == ["pr", "list"]:
            rows = []
        else:
            assert argv[1:3] == ["api", "graphql"], argv
            query = argv[argv.index("-f") + 1]
            assert query.startswith("query=query(") and "mutation" not in query
            rows = {
                "data": {
                    "repository": {
                        "issue": {
                            "closedByPullRequestsReferences": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    offloads = []
    run_triage_agent = roles.run_triage_agent

    def advisory_offload(backend, prompt, **kw):
        offloads.append(kw)
        assert kw["mode"] == roles.ROLE_REGISTRY["triage"].mode
        assert kw["cwd"] != "."
        return {"run_id": "shadow-backend", "exit": 0, "output": json.dumps(proposal())}

    def live_role(**kw):
        return run_triage_agent(backend="codex", cap={}, learned={}, **kw)

    monkeypatch.setattr(triage_shadow.subprocess, "run", read_only)
    monkeypatch.setattr(dispatcher, "offload", advisory_offload)
    monkeypatch.setattr(roles, "run_triage_agent", live_role)
    monkeypatch.setenv("ORCH_TRIAGE_SHADOW", "1")
    if entrypoint == "cli":
        assert triage_shadow.main([]) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        candidates = triage_shadow.discover_candidates()
        result = triage_shadow.record_cycle(candidates)
    assert result["shadow"] and result["candidate_count"] == 4
    assert result["live_proposal"] and result["role_run_id"]
    assert result["backend_run_id"] == "shadow-backend"
    assert result["triage_top_three"] == [
        rec["target"] for rec in proposal()["recommendations"][:3]
    ]
    assert json.loads(triage_shadow.corpus_path().read_text()) == result
    assert len(offloads) == 1
    assert [argv[argv.index("--label") + 1] for argv in reads[:3]] == [
        "priority:high",
        "priority:normal",
        "priority:low",
    ]
    assert all("--state" in argv and "open" in argv for argv in reads[:4])


@pytest.mark.parametrize(
    ("state", "verdict", "merged", "durability"),
    [("MERGED", "PASS", 1, "pending"), ("CLOSED", "FAIL", 0, "abandoned")],
)
def test_backfill_grades_an_ungraded_disagreement_from_pr_state(
    brain, state, verdict, merged, durability
):
    feedback.record_role_run("triage", "triage", "snapshot", "codex", proposal=proposal())
    feedback.record_run("worker", "stranske/Orchestrator#3", "implement", "codex", mode="local")
    feedback.join_role_to_outcome("triage", "worker", accepted=False)
    looked_up = []

    def pr_state(target):
        looked_up.append(target)
        return {"state": state}

    result = outcomes.backfill_triage_disagreements(_state_fn=pr_state)
    assert looked_up == ["stranske/Orchestrator#3"]
    assert result == {
        "source": "backfill",
        "candidates": 1,
        "graded": [
            {
                "target": "stranske/Orchestrator#3",
                "verdict": verdict,
                "durability": durability,
                "accepted": False,
            }
        ],
        "pending": [],
    }
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,merged,durability FROM influence_edges "
            "WHERE source_run_id='triage' AND target_run_id='worker'"
        ).fetchone() == (0, 1, verdict, merged, durability)
        assert conn.execute(
            "SELECT adjudicated_verdict,merged,durability FROM outcomes WHERE run_id='worker'"
        ).fetchone() == (verdict, merged, durability)
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
    monkeypatch.setattr(
        roles, "run_triage_agent", lambda **kw: pytest.fail("incomplete discovery called the role")
    )

    def fake(*a, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="lookup failed")

    with pytest.raises(RuntimeError, match="lookup failed"):
        triage_shadow.discover_candidates(run=fake)

    def truncated(*a, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps([{}] * 1000), stderr="")

    with pytest.raises(ValueError, match="truncated"):
        triage_shadow.discover_candidates(run=truncated)

    for repository in (None, "stranske/Orchestrator", {}, {"nameWithOwner": None}):

        def malformed_repository(*a, **kw):
            row = {**items()[0], "repository": repository}
            return SimpleNamespace(returncode=0, stdout=json.dumps([row]), stderr="")

        with pytest.raises(ValueError, match="repository is UNKNOWN"):
            triage_shadow.discover_candidates(run=malformed_repository)

    monkeypatch.setattr(backlog, "scoped_blocker_source", lambda: "absent")
    monkeypatch.setattr(backlog, "scoped_blocker_entries", lambda: {})
    monkeypatch.setattr(backlog, "load_scoped_blockers", lambda: set())
    good_refs = {"nodes": [], "pageInfo": {"hasNextPage": False}}
    for refs, errors in (
        (good_refs, [{"message": "partial GraphQL response"}]),
        ({"nodes": None, "pageInfo": {"hasNextPage": False}}, []),
        ({"nodes": [{"state": None}], "pageInfo": {"hasNextPage": False}}, []),
        ({"nodes": [{"state": "UNKNOWN"}], "pageInfo": {"hasNextPage": False}}, []),
        ({"nodes": [], "pageInfo": {"hasNextPage": None}}, []),
        ({"nodes": [], "pageInfo": {"hasNextPage": True}}, []),
        ({"nodes": []}, []),
    ):

        def incomplete_linkage(argv, **kw):
            if argv[1:3] == ["search", "issues"]:
                rows = [{**items()[0], "repository": {"nameWithOwner": "stranske/Orchestrator"}}]
            elif argv[1:3] == ["pr", "list"]:
                rows = []
            else:
                rows = {
                    "errors": errors,
                    "data": {"repository": {"issue": {"closedByPullRequestsReferences": refs}}},
                }
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

        monkeypatch.setattr(triage_shadow.subprocess, "run", incomplete_linkage)
        assert triage_shadow.main([]) == 1
        assert not triage_shadow.corpus_path().exists()


def test_replay_and_invalid_proposals_never_score_as_live_rankings(brain):
    path = brain / "rows.jsonl"
    triage_shadow.record_cycle(
        items(), path=path, runner=lambda **k: {"proposal": proposal()}, dispatch=False
    )
    triage_shadow.record_cycle(
        items(), path=path, runner=lambda **k: {"errors": ["provider failed"]}
    )
    assert triage_shadow.summary(path)["triage"]["judged"] == 0


def test_summary_counts_only_known_durable_target_outcomes(brain, monkeypatch):
    monkeypatch.setattr(triage_shadow.time, "time_ns", lambda: 100_000_000_000)
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


def test_summary_does_not_credit_an_outcome_before_the_live_cycle(brain):
    path = brain / "rows.jsonl"
    feedback.record_run("old", "stranske/Orchestrator#3", "implement", "codex", mode="local")
    feedback.record_outcome("old", merged=True, durability="durable")
    with feedback._conn() as conn:
        conn.execute("UPDATE runs SET ts=99 WHERE run_id='old'")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ts": 100_000_000_000,
                "triage_valid": True,
                "live_proposal": True,
                "rule_pick": "stranske/Orchestrator#1",
                "triage_top_three": ["stranske/Orchestrator#3"],
            }
        )
        + "\n"
    )
    assert triage_shadow.summary(path)["triage"]["judged"] == 0
    with feedback._conn() as conn:
        conn.execute("UPDATE runs SET ts=101 WHERE run_id='old'")
    assert triage_shadow.summary(path)["triage"]["judged"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("rule_pick", 2**100),
        ("rule_pick", {}),
        ("triage_top_three", [2**100]),
        ("triage_top_three", [{}]),
        ("ts", True),
        ("ts", "bad"),
        ("ts", 2**1000),
    ],
)
def test_malformed_live_evidence_never_reaches_sqlite(tmp_path, monkeypatch, field, value):
    row = {
        "schema_version": 1,
        "ts": 100_000_000_000,
        "triage_valid": True,
        "live_proposal": True,
        "rule_pick": "stranske/Orchestrator#1",
        "triage_top_three": ["stranske/Orchestrator#3"],
    }
    row[field] = value
    path = tmp_path / "rows.jsonl"
    path.write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(feedback, "_conn", lambda: pytest.fail("malformed evidence opened Brain"))
    assert "UNKNOWN" in triage_shadow.summary_line(path)


def test_invalid_utf8_evidence_is_unknown(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b"\xff")
    assert "UNKNOWN" in triage_shadow.summary_line(path)


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
        json.dumps(
            {
                "schema_version": 1,
                "triage_valid": True,
                "live_proposal": True,
                "triage_top_three": ["stranske/Orchestrator#1"],
                "rule_pick": "stranske/Orchestrator#1",
                "ts": 100_000_000_000,
            }
        )
        + "\n"
    )

    def unavailable():
        raise sqlite3.OperationalError("unreadable fixture")

    monkeypatch.setattr(feedback, "_conn", unavailable)
    text = triage_shadow.summary_line(path)
    assert "UNKNOWN" in text and "0 judged" not in text


def test_malformed_corpus_cannot_report_a_complete_zero_judged_population(tmp_path, monkeypatch):
    path = tmp_path / "malformed.jsonl"
    path.write_text("not json\n")
    monkeypatch.setattr(feedback, "_conn", lambda: pytest.fail("malformed corpus opened Brain"))
    text = triage_shadow.summary_line(path)
    assert "UNKNOWN" in text and "malformed corpus rows 1" in text
    assert "0 judged" not in text
