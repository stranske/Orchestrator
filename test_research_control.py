import os
import random
import sqlite3

import claims
import feedback
import research_scheduler
import research_subjects
import tick


def _brain() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(feedback.SCHEMA)
    feedback._migrate_schema(conn)
    return conn


def _capacity() -> dict:
    return {
        "agents": {
            "cursor": {"state": "ok"},
            "codex": {"state": "ok"},
            "gemini": {"state": "ok"},
            "vibe": {"state": "ok"},
        }
    }


def test_research_tick_preserves_task_type(tmp_path, monkeypatch):
    conn = _brain()
    monkeypatch.setattr(claims, "_handoff_dir", lambda: tmp_path)
    item = {
        "target": "owner/repo#1",
        "task_type": "testgen",
        "lane": "opener",
        "title": "Generate missing coverage",
        "body": "Add reliable tests for the parser",
    }
    job = {
        "id": item["target"],
        "target": item["target"],
        "task_type": "testgen",
        "item": item,
        "arms": ["codex", "cursor"],
        "capacity_cost": 2,
        "info_value": 1.0,
    }
    monkeypatch.setattr(
        research_scheduler,
        "build_research_plan",
        lambda *args, **kwargs: {
            "status": "planned",
            "spare": {"codex": 1, "cursor": 1},
            "budget": 2,
            "candidates": [job],
            "planned": [job],
            "skipped": [
                {
                    "target": "owner/repo#duplicate",
                    "task_type": "testgen",
                    "reason": "duplicate_candidate_in_plan",
                }
            ],
            "blocked_reasons": [],
        },
    )
    captured = {}

    def prepare(repo, spec_file, exp_id, agents, task_type="implement"):
        captured.update(
            {
                "repo": repo,
                "exp_id": exp_id,
                "agents": agents,
                "task_type": task_type,
            }
        )
        return {
            "exp_id": exp_id,
            "repo": repo,
            "base_sha": "abc123",
            "launched": [
                {
                    "agent": "codex",
                    "pid": os.getpid(),
                    "log": str(tmp_path / "codex.log"),
                    "worktree": str(tmp_path / "worktree"),
                }
            ],
        }

    result = tick.research_tick(
        [item],
        _capacity(),
        dry_run=False,
        env={"ORCH_RESEARCH_ARM": "1"},
        conn=conn,
        prepare_fn=prepare,
        issue_body_fn=lambda target: item["body"],
        unevaluated_cap=99,
    )
    assert result["active"], result
    assert captured["task_type"] == "testgen", (
        f"expected testgen, got {captured['task_type']}"
    )
    run_task_type = conn.execute(
        "SELECT task_type FROM research_subjects WHERE exp_id=?",
        (captured["exp_id"],),
    ).fetchone()[0]
    assert run_task_type == "testgen"
    duplicate_event = conn.execute(
        "SELECT COUNT(*) FROM research_subject_events "
        "WHERE decision='rejected' AND reason='duplicate_candidate_in_plan'"
    ).fetchone()[0]
    assert duplicate_event == 1
    claims.release(item["target"], "research")
    conn.close()


def test_production_range_item_is_reserved_before_research(tmp_path, monkeypatch):
    monkeypatch.setattr(claims, "_handoff_dir", lambda: tmp_path)
    # remote_tick now records bounded role-selector evidence; keep this
    # production-shaped fixture out of the live Brain.
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(
        tick.dispatcher,
        "delegate_remote",
        lambda agent, target, task_type, dry_run: {"applied": not dry_run, "skip": None},
    )
    captured = {}

    def research_probe(*args, **kwargs):
        captured.update(kwargs)
        return {"status": "blocked", "planned": [], "active": False}

    item = {
        "target": "owner/repo#2",
        "task_type": "testgen",
        "lane": "opener",
    }
    result = tick.remote_tick(
        [item],
        _capacity(),
        dry_run=False,
        do_ingest=False,
        research_tick_fn=research_probe,
    )
    assert result["chosen"] and result["chosen"][0]["target"] == item["target"], result
    assert item["target"] in captured["excluded_targets"]
    assert captured["production_reserve"]
    assert claims.holder(item["target"]) is None, "production reservation created research claim"


def test_correlated_research_arms_have_subject_effective_sample_count():
    conn = _brain()
    research_subjects.ensure_schema(conn)
    identities = []
    for index in range(3):
        identity = research_subjects.subject_identity(
            f"owner/repo#{index + 1}",
            "testgen",
            f"spec {index + 1}",
            "abc123",
            ["codex", "cursor"],
        )
        identities.append(identity)
        research_subjects.record_subject(
            identity,
            lifecycle="evaluated",
            exp_id=f"exp-{index + 1}",
            conn=conn,
        )
    for index in range(20):
        conn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"correlated-{index}", 1, "o/r#1", "testgen", "codex", "exp-1", "experimental"),
        )
    for index in (2, 3):
        conn.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,experiment_id,assignment) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"independent-{index}", 1, f"o/r#{index}", "testgen", "codex", f"exp-{index}", "experimental"),
        )
    weights = research_subjects.effective_evidence_weights(
        conn=conn, task_type="testgen"
    )
    assert len(weights) == 22
    assert abs(sum(weights.values()) - 3.0) < 1e-9, (
        f"expected effective sample count 3, got {sum(weights.values())}"
    )
    conn.close()


def test_same_subject_is_rejected_while_active():
    conn = _brain()
    research_subjects.ensure_schema(conn)
    kwargs = {
        "target": "owner/repo#4",
        "task_type": "testgen",
        "spec": "same frozen spec",
        "base_sha": "abc123",
        "arms": ["codex", "cursor"],
        "conn": conn,
        "now": 2_000_000_000,
        "unevaluated_cap": 99,
    }
    first = research_subjects.assess_candidate(**kwargs)
    assert first["eligible"] and first["reason"] == "admitted", first
    research_subjects.record_subject(
        first,
        lifecycle="active",
        exp_id="active-exp",
        conn=conn,
        now=2_000_000_000,
    )
    second = research_subjects.assess_candidate(**kwargs)
    assert not second["eligible"] and second["reason"] == "subject_active", second
    conn.close()


def test_relearn_uses_independent_subject_effective_count(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    with feedback._conn() as conn:
        research_subjects.ensure_schema(conn)
        for index in range(3):
            identity = research_subjects.subject_identity(
                f"owner/repo#{index + 1}",
                "subject-learn",
                f"spec {index + 1}",
                "abc123",
                ["codex"],
            )
            research_subjects.record_subject(
                identity,
                lifecycle="evaluated",
                exp_id=f"subject-exp-{index + 1}",
                conn=conn,
            )
    run_ids = []
    for index in range(20):
        run_id = f"correlated-{index}"
        run_ids.append(run_id)
        feedback.record_run(
            run_id,
            "owner/repo#1",
            "subject-learn",
            "codex",
            experiment_id="subject-exp-1",
        )
    for index in (2, 3):
        run_id = f"independent-{index}"
        run_ids.append(run_id)
        feedback.record_run(
            run_id,
            f"owner/repo#{index}",
            "subject-learn",
            "codex",
            experiment_id=f"subject-exp-{index}",
        )
    for run_id in run_ids:
        feedback.record_outcome(
            run_id,
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
    version = feedback.relearn_quality({"subject-learn": {"codex": 0.5}})
    weight = feedback.current_weights("subject-learn", version)[0]
    assert weight["n_obs"] == 3
    with feedback._conn() as conn:
        rationale = conn.execute(
            "SELECT rationale FROM route_weights WHERE version=? "
            "AND task_type='subject-learn' AND agent='codex'",
            (version,),
        ).fetchone()[0]
    assert "raw_run_n=22" in rationale
    assert "independent_subject_n=3.0" in rationale


def test_subject_and_supersession_weights_compose_multiplicatively(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    now = 2_000_000_000
    with feedback._conn() as conn:
        research_subjects.ensure_schema(conn)
        for index in range(3):
            identity = research_subjects.subject_identity(
                f"owner/repo#{index + 1}",
                "weighted-learn",
                f"spec {index + 1}",
                "abc123",
                ["codex"],
            )
            research_subjects.record_subject(
                identity,
                lifecycle="evaluated",
                exp_id=f"weighted-exp-{index + 1}",
                conn=conn,
                now=now,
            )
    for index in range(20):
        run_id = f"old-correlated-{index}"
        feedback.record_run(
            run_id,
            "owner/repo#1",
            "weighted-learn",
            "codex",
            experiment_id="weighted-exp-1",
            ts=now - 100,
        )
        feedback.record_execution_attempt(
            run_id,
            operation_role="worker",
            resolved_provider="openai",
            resolved_model="gpt-old",
            status="success",
            completed_ts=now - 100,
        )
        feedback.record_outcome(
            run_id, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
    for index in (2, 3):
        run_id = f"new-independent-{index}"
        feedback.record_run(
            run_id,
            f"owner/repo#{index}",
            "weighted-learn",
            "codex",
            experiment_id=f"weighted-exp-{index}",
            ts=now,
        )
        feedback.record_execution_attempt(
            run_id,
            operation_role="worker",
            resolved_provider="openai",
            resolved_model="gpt-new",
            status="success",
            completed_ts=now,
        )
        feedback.record_outcome(
            run_id, adjudicated_verdict="FAIL", merged=False, durability="reverted"
        )
    monkeypatch.setattr(feedback.time, "time", lambda: now)
    version = feedback.relearn_quality({"weighted-learn": {"codex": 0.5}})
    weight = feedback.current_weights("weighted-learn", version)[0]
    assert weight["n_obs"] == 3
    assert abs(weight["posterior"] - (4.5 / 10.5)) < 1e-9
    with feedback._conn() as conn:
        rationale = conn.execute(
            "SELECT rationale FROM route_weights WHERE version=?",
            (version,),
        ).fetchone()[0]
    assert "eff_n=2.5" in rationale


def test_research_plan_reserves_global_unevaluated_slots(monkeypatch):
    conn = _brain()
    jobs = [
        {
            "id": f"owner/repo#{index}",
            "target": f"owner/repo#{index}",
            "task_type": "testgen",
            "item": {"target": f"owner/repo#{index}"},
            "subject_id": f"subject-{index}",
            "subject_family_id": f"family-{index}",
            "capacity_cost": 1,
            "info_value": 1.0,
        }
        for index in (1, 2)
    ]
    monkeypatch.setattr(
        research_scheduler, "research_job_candidates", lambda *args, **kwargs: jobs
    )
    plan = research_scheduler.build_research_plan(
        [], _capacity(), conn=conn, max_jobs=2, unevaluated_cap=1, budget=2
    )
    assert len(plan["planned"]) == 1
    assert plan["remaining_backlog_slots"] == 1
    assert any(
        row.get("reason") == "unevaluated_backlog_batch_reserve"
        for row in plan["skipped"]
    )
    conn.close()
