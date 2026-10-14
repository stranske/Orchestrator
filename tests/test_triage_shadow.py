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


@pytest.fixture
def consumable(brain, monkeypatch):
    """Real fixture role/worker records, with no provider or production state writes."""
    import capabilities

    monkeypatch.setattr(
        capabilities, "load", lambda **kw: {"role-triage": {"capability_version_id": "triage-v1"}}
    )
    monkeypatch.setattr(triage_shadow.time, "time_ns", lambda: 100_000_000_000)
    feedback.record_role_run(
        "fixture-triage", "triage", "snapshot", "codex", proposal=proposal(), ts=90
    )
    for _ in range(20):
        triage_shadow.record_cycle(
            items(),
            runner=lambda **kw: {
                "proposal": proposal(),
                "role_run_id": "fixture-triage",
                "backend_run_id": "fixture-backend",
            },
        )
    return [row["target"] for row in items()]


def test_consumed_ranking_records_an_accepted_edge_when_the_opener_follows(consumable, monkeypatch):
    import capabilities

    targets = consumable + ["stranske/Orchestrator#5"]
    result = triage_shadow.consume(targets, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    assert result["consumed"] is True
    assert result["ordered_targets"] == [
        consumable[1],
        consumable[2],
        consumable[3],
        consumable[0],
        targets[-1],
    ]
    assert len(result["ordered_targets"]) == len(set(targets))
    consumed = json.loads(triage_shadow.consumed_path().read_text())
    assert consumed["picked"] == result["ordered_targets"][0]
    assert consumed["role_run_id"] == result["role_run_id"] == "fixture-triage"
    assert consumed["capability_version_id"] == "triage-v1"
    # A ledger revision after consumption cannot change the version receiving credit.
    monkeypatch.setattr(
        capabilities, "load", lambda **kw: {"role-triage": {"capability_version_id": "triage-v2"}}
    )
    feedback.record_run(
        "earlier-worker", consumed["picked"], "implement", "codex", mode="local", ts=100
    )
    feedback.record_completion_event("earlier-worker", status="succeeded")
    feedback.record_run(
        "fixture-worker", result["ordered_targets"][0], "implement", "codex", mode="local", ts=102
    )
    feedback.record_run(
        "later-worker", consumed["picked"], "implement", "codex", mode="local", ts=103
    )
    feedback.record_completion_event("later-worker", status="succeeded")
    # Wait for the first subsequent matching run, even if a later run completes first.
    assert outcomes.link_triage_consumption(now=103) == {
        "followed": [],
        "not_followed": [],
        "unresolved": [],
        "pending": [consumed["ts"]],
    }
    assert not triage_shadow.linked_path().exists()
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 0
    feedback.record_completion_event("fixture-worker", status="succeeded")

    # The Brain commits before the JSONL link is appended. An interruption in
    # that gap must leave a retryable row without awarding a second edge.
    def interrupted_append(path, row):
        assert path == triage_shadow.linked_path()
        raise OSError("fixture link append interrupted")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(triage_shadow, "_append", interrupted_append)
        with pytest.raises(OSError, match="fixture link append interrupted"):
            outcomes.link_triage_consumption(now=104)
    assert not triage_shadow.linked_path().exists()
    with feedback._conn() as conn:
        edge_before_retry = conn.execute(
            "SELECT edge_id FROM influence_edges WHERE accepted=1"
        ).fetchall()
    assert len(edge_before_retry) == 1
    assert len(outcomes.link_triage_consumption(now=104)["followed"]) == 1
    with feedback._conn() as conn:
        assert (
            conn.execute("SELECT edge_id FROM influence_edges WHERE accepted=1").fetchall()
            == edge_before_retry
        )
        assert conn.execute(
            "SELECT e.capability_id,e.capability_version_id,e.accepted,e.source_run_id,"
            "e.target_run_id,r.target FROM influence_edges e "
            "JOIN runs r ON r.run_id=e.target_run_id WHERE e.influence_type='capability'"
        ).fetchall() == [
            ("role-triage", "triage-v1", 1, "fixture-triage", "fixture-worker", consumed["picked"])
        ]
    assert json.loads(triage_shadow.linked_path().read_text()) == {
        "consumed_ts": consumed["ts"],
        "consumption_id": consumed["consumption_id"],
        "status": "followed",
        "run_id": "fixture-worker",
    }
    assert outcomes.link_triage_consumption(now=105) == {
        "followed": [],
        "not_followed": [],
        "unresolved": [],
        "pending": [],
    }
    text = triage_shadow.summary_line()
    assert "consumed 1, followed 1, not followed 0, open 0" in text
    assert "top-1 agrees with the rule pick 0 of 20 valid cycles" in text


def test_same_timestamp_consumptions_link_and_report_independently(consumable):
    first = triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    remaining = [t for t in consumable if t != first["ordered_targets"][0]]
    second = triage_shadow.consume(remaining, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    assert first["consumed"] and second["consumed"]
    for index, result in enumerate([first, second]):
        run_id = f"collision-worker-{index}"
        feedback.record_run(
            run_id, result["ordered_targets"][0], "implement", "codex", mode="local", ts=102
        )
        feedback.record_completion_event(run_id, status="succeeded")
    assert len(outcomes.link_triage_consumption(now=103, limit=1)["followed"]) == 1
    assert "consumed 2, followed 1, not followed 0, open 1" in triage_shadow.summary_line()
    assert len(outcomes.link_triage_consumption(now=104)["followed"]) == 1
    assert "consumed 2, followed 2, not followed 0, open 0" in triage_shadow.summary_line()
    assert outcomes.link_triage_consumption(now=105)["followed"] == []
    with feedback._conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM influence_edges WHERE accepted=1").fetchone()[0] == 2
        )


@pytest.mark.parametrize(
    "case,reason",
    [
        ("off", "flag_off"),
        ("cycles", "cycles_below_minimum:19/20"),
        ("stale", "ranking_stale:7201/7200"),
        ("missing", "no_ranking"),
    ],
)
def test_consume_refuses_with_a_named_reason_and_leaves_order_unchanged(consumable, case, reason):
    now = 101
    env = {"ORCH_TRIAGE_CONSUME": "1"}
    if case == "off":
        env = {}
    elif case == "cycles":
        lines = triage_shadow.corpus_path().read_text().splitlines()
        triage_shadow.corpus_path().write_text("\n".join(lines[:19]) + "\n")
    elif case == "stale":
        now = 7301
    else:
        triage_shadow.ranking_path().unlink()
    result = triage_shadow.consume(consumable, env=env, now=now)
    assert result == {
        "consumed": False,
        "reason": reason,
        "ordered_targets": consumable,
        "role_run_id": None,
    }
    assert not triage_shadow.consumed_path().exists()


def test_ranking_publication_is_live_only_and_covers_remaining_rule_order(brain):
    recs = proposal()
    for rec in recs["recommendations"]:
        if rec["target"] != "stranske/Orchestrator#3":
            rec["action"] = "skip"
    triage_shadow.record_cycle(items(), runner=lambda **kw: {"proposal": recs}, dispatch=False)
    assert not triage_shadow.ranking_path().exists()
    triage_shadow.record_cycle(items(), runner=lambda **kw: {"errors": ["backend failed"]})
    assert not triage_shadow.ranking_path().exists()
    row = triage_shadow.record_cycle(
        items(),
        runner=lambda **kw: {
            "proposal": recs,
            "role_run_id": "fixture-role",
            "backend_run_id": "fixture-backend",
        },
    )
    rank = json.loads(triage_shadow.ranking_path().read_text())
    assert rank == {
        "ts": row["ts"],
        "role_run_id": row["role_run_id"],
        "snapshot_sha256": row["snapshot_sha256"],
        "max_age_s": 7200,
        "ordered_targets": [
            "stranske/Orchestrator#3",
            "stranske/Orchestrator#1",
            "stranske/Orchestrator#2",
            "stranske/Orchestrator#4",
        ],
    }
    assert triage_shadow.summary()["valid_cycles"] == 1


@pytest.mark.parametrize("stage", ["create", "write", "flush", "fsync", "close", "replace"])
def test_atomic_ranking_failure_preserves_previous_snapshot_and_cleans_up(
    brain, monkeypatch, stage
):
    row = {"ts": 100_000_000_000, "role_run_id": "fixture-role", "snapshot_sha256": "fixture"}
    triage_shadow._publish_ranking(row, ["stranske/Orchestrator#1"])
    path = triage_shadow.ranking_path()
    previous = path.read_bytes()
    original_tempfile = triage_shadow.tempfile.NamedTemporaryFile

    def fail(*args, **kwargs):
        raise OSError(f"{stage} failed")

    class FailingStream:
        def __init__(self, **kwargs):
            if stage == "create":
                fail()
            self.stream = original_tempfile(**kwargs)
            self.name = self.stream.name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            if stage == "close":
                fail()

        def write(self, text):
            if stage == "write":
                self.stream.write(text[:1])
                fail()
            return self.stream.write(text)

        def flush(self):
            if stage == "flush":
                fail()
            self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    with monkeypatch.context() as patch:
        patch.setattr(triage_shadow.tempfile, "NamedTemporaryFile", FailingStream)
        if stage in {"fsync", "replace"}:
            patch.setattr(triage_shadow.os, stage, fail)
        with pytest.raises(OSError, match=f"{stage} failed"):
            triage_shadow.record_cycle(
                items(),
                runner=lambda **kwargs: {
                    "proposal": proposal(),
                    "role_run_id": "next-role",
                    "backend_run_id": "next-backend",
                },
            )
    assert path.read_bytes() == previous
    assert not list(path.parent.glob(".triage-ranking-*"))
    # Publication failure must not erase the completed shadow observation.
    assert json.loads(triage_shadow.corpus_path().read_text())["role_run_id"] == "next-role"
    recovered = triage_shadow.record_cycle(
        items(),
        runner=lambda **kwargs: {
            "proposal": proposal(),
            "role_run_id": "recovered-role",
            "backend_run_id": "recovered-backend",
        },
    )
    assert json.loads(path.read_text())["ts"] == recovered["ts"]
    assert json.loads(path.read_text())["role_run_id"] == "recovered-role"
    assert not list(path.parent.glob(".triage-ranking-*"))


def test_consumption_never_injects_removed_targets_and_never_hides_new_ones(consumable):
    current = ["stranske/Orchestrator#99", consumable[0], consumable[1]]
    result = triage_shadow.consume(current, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    assert result["ordered_targets"] == [consumable[1], consumable[0], current[0]]
    row = json.loads(triage_shadow.consumed_path().read_text())
    assert (
        row["candidate_sha256"]
        == hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
    )


def test_unfollowed_consumption_times_out_without_credit(consumable):
    triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    consumed = json.loads(triage_shadow.consumed_path().read_text())
    # An earlier run and a later unrelated run cannot earn credit.
    feedback.record_run("old-worker", consumable[1], "implement", "codex", mode="local", ts=100)
    feedback.record_completion_event("old-worker", status="succeeded")
    feedback.record_run(
        "unrelated", "stranske/Orchestrator#99", "implement", "codex", mode="local", ts=102
    )
    feedback.record_completion_event("unrelated", status="succeeded")
    assert outcomes.link_triage_consumption(now=101 + 48 * 3600 - 1)["not_followed"] == []
    assert outcomes.link_triage_consumption(now=101 + 48 * 3600)["not_followed"] == [
        {
            "consumed_ts": 101_000_000_000,
            "consumption_id": consumed["consumption_id"],
            "status": "not_followed",
            "run_id": None,
        }
    ]
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["future", "malformed", "duplicate", "no_version"])
def test_unknown_consumption_evidence_refuses_without_writing(consumable, monkeypatch, kind):
    import capabilities

    if kind == "no_version":
        monkeypatch.setattr(capabilities, "load", lambda **kw: {})
    else:
        rank = json.loads(triage_shadow.ranking_path().read_text())
        if kind == "future":
            rank["ts"] = 102_000_000_000
        elif kind == "duplicate":
            rank["ordered_targets"] *= 2
        else:
            rank["ordered_targets"] = None
        triage_shadow.ranking_path().write_text(json.dumps(rank))
    result = triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    assert not result["consumed"]
    assert result["ordered_targets"] == consumable
    assert not triage_shadow.consumed_path().exists()


def test_consume_cli_uses_explicit_targets_and_never_dispatches(
    consumable, monkeypatch, capsys, brain
):
    target_file = brain / "targets.json"
    target_file.write_text(json.dumps(consumable))
    monkeypatch.setenv("ORCH_TRIAGE_CONSUME", "1")
    monkeypatch.setattr(triage_shadow.time, "time", lambda: 101)
    monkeypatch.setattr(
        triage_shadow, "discover_candidates", lambda: pytest.fail("consumer rediscovered work")
    )
    assert triage_shadow.main(["--consume", "--candidates", str(target_file)]) == 0
    assert json.loads(capsys.readouterr().out)["consumed"]


def test_disjoint_ranking_cannot_claim_the_new_rule_pick(consumable):
    current = ["stranske/Orchestrator#90", "stranske/Orchestrator#91"]
    result = triage_shadow.consume(current, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    assert result == {
        "consumed": False,
        "reason": "ranking_no_candidates",
        "ordered_targets": current,
        "role_run_id": None,
    }
    assert not triage_shadow.consumed_path().exists()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_legacy_timestamp_links_require_unique_receipts(consumable, ambiguous):
    first = triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    if ambiguous:
        triage_shadow.consume(
            [t for t in consumable if t != first["ordered_targets"][0]],
            env={"ORCH_TRIAGE_CONSUME": "1"},
            now=101,
        )
    rows = triage_shadow._read_rows(triage_shadow.consumed_path())
    for row in rows:
        row.pop("consumption_id")
    triage_shadow.consumed_path().write_text("".join(json.dumps(row) + "\n" for row in rows))
    triage_shadow._append(
        triage_shadow.linked_path(),
        {"consumed_ts": rows[0]["ts"], "status": "followed", "run_id": "legacy-worker"},
    )
    if ambiguous:
        assert "UNKNOWN" in triage_shadow.summary_line()
        with pytest.raises(ValueError, match="ambiguous legacy"):
            outcomes.link_triage_consumption(now=104)
    else:
        assert "consumed 1, followed 1, not followed 0, open 0" in triage_shadow.summary_line()
        assert outcomes.link_triage_consumption(now=104)["followed"] == []
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 0


@pytest.mark.parametrize("invalid_id", [["bad"], {}, True, None, ""])
def test_malformed_persisted_consumption_ids_are_unknown(consumable, invalid_id):
    triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    row = json.loads(triage_shadow.consumed_path().read_text())
    row["consumption_id"] = invalid_id
    triage_shadow.consumed_path().write_text(json.dumps(row) + "\n")
    assert "UNKNOWN" in triage_shadow.summary_line()
    with pytest.raises(ValueError, match="invalid consumption_id"):
        outcomes.link_triage_consumption(now=104)
    # Linked receipts must use the same validation, even with a valid consumption.
    row["consumption_id"] = "valid-id"
    triage_shadow.consumed_path().write_text(json.dumps(row) + "\n")
    triage_shadow._append(
        triage_shadow.linked_path(),
        {
            "consumed_ts": row["ts"],
            "consumption_id": invalid_id,
            "status": "followed",
            "run_id": "fixture-worker",
        },
    )
    assert "UNKNOWN" in triage_shadow.summary_line()


def test_duplicate_legacy_receipts_refuse_before_any_credit(consumable):
    result = triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    row = json.loads(triage_shadow.consumed_path().read_text())
    row.pop("consumption_id")
    triage_shadow.consumed_path().write_text((json.dumps(row) + "\n") * 2)
    feedback.record_run(
        "legacy-worker", result["ordered_targets"][0], "implement", "codex", mode="local", ts=102
    )
    feedback.record_completion_event("legacy-worker", status="succeeded")
    with pytest.raises(ValueError, match="duplicate consumption identity"):
        outcomes.link_triage_consumption(now=104, limit=1)
    assert "UNKNOWN" in triage_shadow.summary_line()
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 0


def test_unlinked_legacy_retry_preserves_brain_attempt_identity(consumable, monkeypatch):
    result = triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    row = json.loads(triage_shadow.consumed_path().read_text())
    row.pop("consumption_id")
    triage_shadow.consumed_path().write_text(json.dumps(row) + "\n")
    feedback.record_run(
        "legacy-worker", result["ordered_targets"][0], "implement", "codex", mode="local", ts=102
    )
    feedback.record_completion_event("legacy-worker", status="succeeded")
    original = feedback.record_capability_consumption
    attempts = []

    def record(**kwargs):
        attempts.append(kwargs["attempt_id"])
        return original(**kwargs)

    monkeypatch.setattr(feedback, "record_capability_consumption", record)
    with monkeypatch.context() as interrupted:

        def fail_append(path, record):
            raise OSError("legacy append interrupted")

        interrupted.setattr(triage_shadow, "_append", fail_append)
        with pytest.raises(OSError, match="legacy append interrupted"):
            outcomes.link_triage_consumption(now=103)
    assert len(outcomes.link_triage_consumption(now=104)["followed"]) == 1
    assert attempts == [str(row["ts"]), str(row["ts"])]
    assert outcomes.link_triage_consumption(now=105)["followed"] == []
    with feedback._conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM influence_edges WHERE accepted=1").fetchone()[0] == 1
        )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("second_status", ["followed", "not_followed"])
def test_duplicate_link_receipts_refuse_before_any_credit(consumable, legacy, second_status):
    result = triage_shadow.consume(consumable, env={"ORCH_TRIAGE_CONSUME": "1"}, now=101)
    row = json.loads(triage_shadow.consumed_path().read_text())
    link = {"consumed_ts": row["ts"], "status": "followed", "run_id": "fixture-worker"}
    if not legacy:
        link["consumption_id"] = row["consumption_id"]
    triage_shadow._append(triage_shadow.linked_path(), link)
    triage_shadow._append(triage_shadow.linked_path(), dict(link, status=second_status))
    feedback.record_run(
        "fixture-worker", result["ordered_targets"][0], "implement", "codex", mode="local", ts=102
    )
    feedback.record_completion_event("fixture-worker", status="succeeded")
    before = triage_shadow.linked_path().read_bytes()
    assert "UNKNOWN" in triage_shadow.summary_line()
    with pytest.raises(ValueError, match="duplicate linked consumption identity"):
        outcomes.link_triage_consumption(now=104)
    assert triage_shadow.linked_path().read_bytes() == before
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 0
