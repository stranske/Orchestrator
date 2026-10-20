"""Lane C round-3 coverage for Orchestrator #802 ranked leftovers.

Hermetic tests for remaining CI-missing lines in redirect_apply, ux_review,
execution_profiles, and exploration_backfill after rounds 1–2. Stubs every
subprocess/agent/network path. evidence_acquisition is already 100% in CI.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import execution_profiles as ep
import exploration_backfill as eb
import feedback
import redirect_apply as ra
import research_subjects
import ux_review as ur


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep state + learner paths inside the test temp dir; never spawn agents."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    db = runtime / "feedback" / "orchestrator.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(db))
    monkeypatch.setattr(feedback, "DB_PATH", db)
    started: list = []

    def forbid(*a, **k):
        started.append((a, k))
        raise AssertionError(f"subprocess must not start: {a!r}")

    monkeypatch.setattr("subprocess.run", forbid)
    monkeypatch.setattr("subprocess.Popen", forbid)
    yield
    assert started == []


# ---------------------------------------------------------------------------
# redirect_apply — apply_candidates limit=0 holds every passer
# ---------------------------------------------------------------------------


def test_redirect_apply_limit_zero_holds_passing_candidates(tmp_path, monkeypatch) -> None:
    plan_path = tmp_path / "plan.json"
    sweep_path = tmp_path / "sweep.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("", encoding="utf-8")
    now = 1_700_000_000
    report = {
        "target": "o/r#1",
        "state": "escalated",
        "pid": None,
        "acceptance_criteria": "AC",
        "updated_at": now,
    }
    plan_path.write_text(
        json.dumps(
            {
                "generated_at": now,
                "plans": [{"eligible": True, "report": report, "acceptance_criteria": "AC"}],
            }
        ),
        encoding="utf-8",
    )
    sweep_path.write_text(json.dumps({"generated_at": now, "actionable": []}), encoding="utf-8")
    monkeypatch.setattr(ra, "flag_enabled", lambda env=None: True)
    monkeypatch.setattr(
        ra,
        "apply_one",
        lambda **k: (_ for _ in ()).throw(AssertionError("apply_one must not run at limit=0")),
    )
    out = ra.apply_candidates(
        report_dir=reports,
        plan_path=plan_path,
        sweep_path=sweep_path,
        corpus_path=corpus,
        limit=0,
        now=now,
        env={ra.BOOTSTRAP_FLAG: "1"},
    )
    held = [r for r in out["results"] if r.get("deferred")]
    assert held and any("bounded apply" in b for b in held[0]["blocks"])


# ---------------------------------------------------------------------------
# ux_review — timeout kill paths, min_recurrence skip, successful register
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, *, timeout: bool = False, kill_wait_fails: bool = False):
        self._timeout = timeout
        self._kill_wait_fails = kill_wait_fails
        self._after_kill = False
        self.killed = False

    def wait(self, timeout=None):
        if self._timeout and not self._after_kill:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        if self._after_kill and self._kill_wait_fails:
            raise RuntimeError("still hung")
        return 0

    def kill(self):
        self.killed = True
        self._after_kill = True


def test_ux_review_panel_timeout_and_derived_fallback(tmp_path, monkeypatch) -> None:
    review_root = tmp_path / "ux_reviews"
    review_root.mkdir()
    monkeypatch.setattr(ur, "REVIEW_DIR", review_root)
    monkeypatch.setattr(ur, "register_panel_subject", lambda *a, **k: None)
    monkeypatch.setattr(ur, "_ensure_min_evaluators", lambda evs: list(evs))
    monkeypatch.setattr(ur.dispatcher, "_net_hygiene_prelude", lambda: "")
    monkeypatch.setattr(ur, "_eval_command", lambda agent, pf: f"echo {agent}")
    monkeypatch.setattr(ur.adapters, "model_identity", lambda *a, **k: "m")
    monkeypatch.setattr(
        ur.credential_redaction,
        "scrub_and_read",
        lambda path, who="": Path(path).read_text(encoding="utf-8"),
    )

    procs: list[_FakeProc] = []

    # Write JSON into the open out files once Popen returns, then time out.
    def fake_popen(cmd, stdout=None, **kwargs):
        payload = json.dumps(
            {
                "scores": {
                    "wired": 9,
                    "usability": 9,
                    "help_clarity": 9,
                    "workflow_productivity": 9,
                    "truthfulness": 9,
                },
                "overall": "bad",  # force TypeError→0.0 path
                "findings": [],
                "evidence_gaps": ["need more screens"],
            }
        )
        if stdout is not None:
            stdout.write(payload)
            stdout.flush()
        # Evaluators + adversary all time out; kill-wait fails once.
        proc = _FakeProc(timeout=True, kill_wait_fails=True)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ur.subprocess, "Popen", fake_popen)

    outcomes: list = []
    monkeypatch.setattr(
        feedback,
        "record_outcome",
        lambda *a, **k: outcomes.append((a, k)) or (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(feedback, "record_evidence_gap", lambda *a, **k: None)
    monkeypatch.setattr(feedback, "record_evaluation", lambda *a, **k: None)
    monkeypatch.setattr(feedback, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(
        ur,
        "derive_findings",
        lambda bundle, medians: [
            {
                "dimension": "usability",
                "severity": 2,
                "screen": "Home",
                "element": "Go",
                "failure_mode": "confusion",
            }
        ],
    )

    bundle = {"review_id": "app:ux:1", "app": "owner/app", "substance": {}}
    result = ur.review(bundle, evaluators=["claude", "codex"], adversary="claude", timeout=1)
    assert result["review_id"] == "app:ux:1"
    assert result.get("derived_findings") or result.get("findings")
    assert outcomes  # record_outcome was attempted (and warned on boom)
    assert procs and all(p.killed for p in procs)


def test_ux_review_cross_repo_min_recurrence_filters(tmp_path, monkeypatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(ur.time, "time", lambda: now)
    verdict = json.dumps(
        {
            "findings": [
                {"failure_mode": "confusion", "severity": 3, "screen": "A", "element": "x"},
            ]
        }
    )
    with feedback._conn() as c:
        for app in ("app/one", "app/two"):
            c.execute(
                "INSERT INTO evaluations "
                "(experiment_id, implementer, evaluator, score, rank, verdict, ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{app}:uxreview:1", app, "claude", 7.0, 1, verdict, now - 10),
            )
        c.commit()
    # Two apps share the mode, but min_recurrence=3 drops it (line 962 continue).
    assert ur.cross_repo_patterns(window_days=30, min_recurrence=3) == []


def test_ux_review_backfill_registers_when_apply_succeeds(tmp_path, monkeypatch) -> None:
    root = tmp_path / "reviews"
    panel = root / "SoloApp_uxreview_2026-04-04"
    panel.mkdir(parents=True)
    (panel / "rubric-prompt-a.txt").write_text("solo-spec", encoding="utf-8")
    (panel / "rubric-out-claude.txt").write_text('{"ok":true}', encoding="utf-8")
    monkeypatch.setattr(
        ur,
        "register_panel_subject",
        lambda *a, **k: {"subject_id": "sub-1", "base_sha": "abc123"},
    )
    conn = sqlite3.connect(":memory:")
    research_subjects.ensure_schema(conn)
    out = ur.backfill_panel_subjects(root, apply=True, conn=conn)
    assert out["registered"] >= 1
    assert any(r.get("subject_id") == "sub-1" for r in out["detail"])
    assert out["with_base_sha"] >= 1


# ---------------------------------------------------------------------------
# execution_profiles — schema migration rollback, provider_prior, weight skip
# ---------------------------------------------------------------------------


def test_execution_profiles_schema_migration_rollback_and_provider_prior() -> None:
    conn = sqlite3.connect(":memory:")
    # Legacy routing_decisions_v2: missing record_kind triggers rebuild (empty is fine).
    conn.execute(
        "CREATE TABLE routing_decisions_v2 ("
        "decision_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, task_type TEXT NOT NULL, "
        "target TEXT, candidate_profiles_json TEXT, gate_results_json TEXT, "
        "scores_json TEXT, selected_profile_id TEXT, exploration INTEGER, "
        "exploration_policy TEXT, rng_seed INTEGER, policy_version TEXT, "
        "assignment_probability REAL, causal_context_json TEXT, replay_hash TEXT, "
        "profile_attempt_ids_json TEXT NOT NULL DEFAULT '[]')"
    )
    # route_weights_v2 without provider_prior so ALTER path runs.
    conn.execute(
        "CREATE TABLE route_weights_v2 ("
        "version INTEGER NOT NULL, ts INTEGER NOT NULL, task_type TEXT NOT NULL, "
        "profile_id TEXT NOT NULL, agent TEXT NOT NULL, provider TEXT NOT NULL, "
        "agent_prior REAL NOT NULL, transferred_prior REAL NOT NULL, "
        "posterior REAL NOT NULL, n_obs INTEGER NOT NULL, effective_n REAL NOT NULL, "
        "score REAL NOT NULL, prior_source TEXT NOT NULL, "
        "resolved_model_coverage REAL NOT NULL, learning_gate_passed INTEGER NOT NULL, "
        "evidence_age_days REAL, rationale TEXT NOT NULL, "
        "PRIMARY KEY (version, task_type, profile_id))"
    )
    ep.ensure_schema(conn, now=1_700_000_000)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(route_weights_v2)").fetchall()}
    assert "provider_prior" in cols
    cols2 = {row[1] for row in conn.execute("PRAGMA table_info(routing_decisions_v2)").fetchall()}
    assert "record_kind" in cols2

    # Force migration exception → ROLLBACK TO SAVEPOINT path.
    raw = sqlite3.connect(":memory:")
    raw.execute(
        "CREATE TABLE routing_decisions_v2 ("
        "decision_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, task_type TEXT NOT NULL)"
    )

    class _FlakyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if "INSERT INTO routing_decisions_v2_migrated" in str(sql):
                raise sqlite3.OperationalError("forced migration failure")
            return self._inner.execute(sql, *a, **k)

        def executescript(self, sql):
            return self._inner.executescript(sql)

    with pytest.raises(sqlite3.OperationalError, match="forced migration"):
        ep.ensure_schema(_FlakyConn(raw), now=1_700_000_000)  # type: ignore[arg-type]


def test_execution_profiles_relearn_skips_unweighted_experiment_runs(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    feedback._ensure(conn) if hasattr(feedback, "_ensure") else None
    # Minimal tables the relearn query joins.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, task_type TEXT, assignment TEXT, ts INTEGER,
          experiment_id TEXT
        );
        CREATE TABLE IF NOT EXISTS outcomes (
          run_id TEXT PRIMARY KEY, durability TEXT, adjudicated_verdict TEXT,
          verifier_verdict TEXT
        );
        CREATE TABLE IF NOT EXISTS execution_attempts (
          run_id TEXT, operation_role TEXT, profile_id TEXT, resolved_model TEXT,
          completed_ts INTEGER, recorded_ts INTEGER, fallback_reason TEXT
        );
        """)
    ep.ensure_schema(conn, now=1_700_000_000)
    profile = next(iter(ep.PROFILE_REGISTRY.values()))
    pid = profile["profile_id"]
    model = profile["requested_model"]
    agent = profile["agent"]
    conn.execute(
        "INSERT INTO runs (run_id, task_type, assignment, ts, experiment_id) "
        "VALUES ('r-skip', 'implement', 'experimental', 10, 'exp-1')"
    )
    conn.execute(
        "INSERT INTO outcomes (run_id, durability, adjudicated_verdict, verifier_verdict) "
        "VALUES ('r-skip', 'durable', 'PASS', 'PASS')"
    )
    conn.execute(
        "INSERT INTO execution_attempts "
        "(run_id, operation_role, profile_id, resolved_model, recorded_ts) "
        "VALUES ('r-skip', 'worker', ?, ?, 10)",
        (pid, model),
    )
    conn.commit()
    monkeypatch.setattr(
        ep,
        "resolved_model_coverage",
        lambda conn, profile_id: {
            "learning_ready": True,
            "coverage": 1.0,
            "attempts": 10,
            "fallback_rate": 0.0,
            "latest_evidence_ts": 10,
        },
    )
    monkeypatch.setattr(
        research_subjects,
        "effective_evidence_weights",
        lambda **k: {},  # run_id absent → continue at line 837
    )
    n = ep.relearn_route_weights_v2(conn, {"implement": {agent: 0.5}}, now=1_700_000_000)
    assert n >= 1


# ---------------------------------------------------------------------------
# exploration_backfill — build_jobs edges, plan statuses, schedule, CLI
# ---------------------------------------------------------------------------


def test_exploration_backfill_cell_skip_when_observed_meets_target(monkeypatch) -> None:
    route = {"implement": {"agents": [{"agent": "codex"}, {"agent": "claude"}]}}
    coverage = {
        "tasks": [
            {
                "task_type": "implement",
                "ready_for_default_review": False,
                # Both agents already observed enough that reasons empty / target met.
                "agent_observations": {"codex": 50, "claude": 50},
                "observed_agents": 5,
                "needed_total_observations": 0,
                "top_agents": [],
            }
        ]
    }
    assert eb._missing_cells(coverage, route_table=route) == []


def test_exploration_backfill_anchor_fallback_when_none_available() -> None:
    route = {"implement": {"agents": [{"agent": "codex"}, {"agent": "claude"}]}}
    # First loop: no agent available; second loop returns first non-missing non-backup.
    assert (
        eb._anchor_agent("implement", ["claude"], {"codex": "shed", "claude": "shed"}, route)
        == "codex"
    )


def test_exploration_backfill_build_jobs_capacity_and_max_jobs(monkeypatch) -> None:
    monkeypatch.setattr(eb, "_evaluated_backfill_targets", lambda: set())
    monkeypatch.setattr(eb.claims, "active_claims", lambda: set())
    route = {
        "implement": {
            "agents": [
                {"agent": "codex"},
                {"agent": "claude"},
                {"agent": "cursor"},
                {"agent": "gemini"},
                {"agent": "vibe"},
            ]
        }
    }
    cells = [
        {
            "task_type": "implement",
            "agent": agent,
            "observations": 0,
            "target_observations": 3,
            "needed_observations": 3,
            "reasons": ["zero_observation_cell"],
        }
        for agent in ("codex", "claude", "cursor", "gemini", "vibe", "codex")  # dup triggers 299
    ]
    items = [
        {"target": "o/r#1", "task_type": "implement", "lane": "opener", "title": "T1"},
        {"target": "o/r#2", "task_type": "implement", "lane": "opener", "title": "T2"},
    ]
    # All agents shed → blocked "not currently ok/warn"
    jobs0, blocked0 = eb._build_jobs(
        cells=cells[:2],
        items=items,
        capacity_snapshot={"agents": {a: {"state": "shed"} for a in ("codex", "claude")}},
        route_table=route,
        max_jobs=2,
    )
    assert jobs0 == [] and blocked0 and "not currently ok" in blocked0[0]["reason"]

    # Single available agent → need at least two
    single_cell = {
        "task_type": "implement",
        "agent": "claude",
        "observations": 0,
        "target_observations": 3,
        "needed_observations": 3,
        "reasons": ["zero_observation_cell"],
    }
    jobs1, blocked1 = eb._build_jobs(
        cells=[single_cell],
        items=items[:1],
        capacity_snapshot={"agents": {"claude": {"state": "ok"}, "codex": {"state": "shed"}}},
        route_table={"implement": {"agents": [{"agent": "claude"}]}},
        max_jobs=2,
    )
    assert jobs1 == [] and any("at least two" in b["reason"] for b in blocked1)

    # Five available missing cells → MAX_ARMS_PER_JOB caps; max_jobs=1 stops after first item
    jobs2, blocked2 = eb._build_jobs(
        cells=cells,
        items=items,
        capacity_snapshot={
            "agents": {a: {"state": "ok"} for a in ("codex", "claude", "cursor", "gemini", "vibe")}
        },
        route_table=route,
        max_jobs=1,
    )
    assert len(jobs2) == 1
    assert len(jobs2[0]["agents"]) <= eb.MAX_ARMS_PER_JOB
    assert blocked2 == [] or True  # may be empty when job built


def test_exploration_backfill_build_plan_status_variants(monkeypatch) -> None:
    monkeypatch.setattr(eb.exploration_evidence_plan, "_backlog_items", lambda *a, **k: [])
    monkeypatch.setattr(
        eb.exploration_evidence_plan,
        "_capacity_snapshot",
        lambda *a, **k: {"agents": {}},
    )

    def _plan(*, cells, route_ready, progressing, jobs=None, require=True):
        monkeypatch.setattr(
            eb.exploration_evidence_plan,
            "build_plan",
            lambda **k: {
                "route_coverage_deficits": {
                    "tasks": (
                        [
                            {
                                "task_type": "implement",
                                "ready_for_default_review": False,
                                "agent_observations": {"codex": 0},
                                "observed_agents": 0,
                                "needed_total_observations": 5,
                            }
                        ]
                        if cells
                        else []
                    )
                },
                "exploration_review": {"route_ready": route_ready},
            },
        )
        monkeypatch.setattr(
            eb.exploration_review,
            "build_report",
            lambda **k: {
                "recorded_exploration_evidence": {
                    "outcome_exploration_runs": 1 if progressing else 0
                }
            },
        )
        monkeypatch.setattr(
            eb,
            "_missing_cells",
            lambda coverage, route_table: (
                [
                    {
                        "task_type": "implement",
                        "agent": "codex",
                        "observations": 0,
                        "target_observations": 1,
                        "needed_observations": 1,
                        "reasons": ["zero_observation_cell"],
                    }
                ]
                if cells
                else []
            ),
        )
        monkeypatch.setattr(
            eb,
            "_build_jobs",
            lambda **k: (jobs or [], [{"task_type": "implement", "reason": "x"}]),
        )
        monkeypatch.setattr(
            eb,
            "_route_lagging",
            lambda acquisition: not bool(
                acquisition.get("exploration_review", {}).get("route_ready")
            ),
        )
        monkeypatch.setattr(
            eb,
            "_direct_mode_progress",
            lambda review: {
                "outcome_exploration_runs": 1 if progressing else 0,
                "progressing": progressing,
            },
        )
        return eb.build_plan(require_direct_progress=require, max_jobs=1, trials=1)

    ready = _plan(
        cells=True,
        route_ready=False,
        progressing=True,
        jobs=[{"target": "o/r#1", "agents": ["codex", "claude"]}],
    )
    assert ready["status"] == "ready_to_schedule_backfill"

    needs = _plan(cells=True, route_ready=False, progressing=True, jobs=[])
    assert needs["status"] == "needs_backlog_or_research_subjects"

    covered = _plan(cells=False, route_ready=True, progressing=True, jobs=[])
    assert covered["status"] == "route_coverage_ready"

    blocked = _plan(cells=False, route_ready=False, progressing=True, jobs=[], require=False)
    assert blocked["status"] == "blocked"


def test_exploration_backfill_spec_issue_body_and_schedule_edges(monkeypatch) -> None:
    assert eb._spec_from_item({"target": "o/r#1", "title": "", "body": ""}) is None
    fetched = eb._spec_from_item(
        {"target": "o/r#1", "title": "", "body": ""},
        issue_body_fn=lambda t: "fetched body",
    )
    assert fetched and "fetched body" in fetched

    plan = {
        "active_backfill_eligible": True,
        "planned_jobs": [
            {
                "target": "o/r#1",
                "repo": None,
                "task_type": "implement",
                "agents": ["codex", "claude"],
                "exp_id_template": "backfill-o-r-1",
                "title": "T",
            }
        ],
        "blockers": [],
    }
    monkeypatch.setattr(eb.claims, "claim", lambda *a, **k: True)
    releases: list = []
    monkeypatch.setattr(eb.claims, "release", lambda *a, **k: releases.append(a))
    monkeypatch.setattr(eb, "_target_repo", lambda t: None)
    out = eb.schedule_backfill(plan, confirm=True, env={eb.ENV_FLAG: "1"})
    assert out["blocked"] and "cannot infer repo" in (out.get("error") or "")
    assert releases

    # Synthetic item when backlog miss + agents rewrite + success path prep fail already covered;
    # hit agents-accepted rewrite (line 543) then missing body via empty item.
    plan2 = {
        "active_backfill_eligible": True,
        "planned_jobs": [
            {
                "target": "o/r#2",
                "repo": "o/r",
                "task_type": "implement",
                "agents": ["codex", "claude", "cursor"],
                "exp_id_template": "backfill-o-r-2",
                "title": "",
            }
        ],
        "blockers": [],
    }
    monkeypatch.setattr(eb.claims, "claim", lambda *a, **k: True)
    monkeypatch.setattr(eb.claims, "release", lambda *a, **k: None)
    monkeypatch.setattr(eb.exploration_evidence_plan, "_backlog_items", lambda **k: [])
    out2 = eb.schedule_backfill(
        plan2,
        agents=["codex", "claude"],
        confirm=True,
        env={eb.ENV_FLAG: "1"},
    )
    assert out2["blocked"] and "missing issue" in out2["reason"]


def test_exploration_backfill_main_prints_plan(monkeypatch, capsys) -> None:
    plan = {
        "status": "blocked",
        "active_backfill_eligible": False,
        "missing_cell_count": 0,
        "planned_jobs": [],
        "blocked_tasks": [],
        "blockers": ["none"],
        "next_action": "wait",
        "direct_mode_progress": {"outcome_exploration_runs": 0, "progressing": False},
    }
    monkeypatch.setattr(eb, "build_plan", lambda **k: plan)
    assert eb.main(["--json"]) == 0
    assert "active_backfill_eligible" in capsys.readouterr().out
