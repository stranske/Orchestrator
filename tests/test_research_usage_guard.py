#!/usr/bin/env python3
"""Unit tests for Orchestrator research usage guard, recovery semantics, and deduplication controls."""

import json
import os
import sqlite3
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

import adapters
import capabilities
import exp_abcd
import experiment_recovery
import feedback
import research_usage_guard


@pytest.fixture(autouse=True)
def _isolate_test_state(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    monkeypatch.setenv("ORCH_EXP_DIR", str(tmp_path / "experiments"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")


def _write_evaluable_experiment(expdir: Path, name: str, *, mtime: float) -> Path:
    edir = expdir / name
    edir.mkdir(parents=True)
    (edir / "meta.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repo": "owner/repo",
                "base": "main",
                "exp_id": name,
                "subject": name,
                "agents": ["codex"],
            }
        )
    )
    (edir / "spec.md").write_text(f"Implement the reviewed change for {name}.")
    (edir / "codex.log").write_text("done")
    old = time.time() - 3_600
    os.utime(edir / "codex.log", (old, old))
    os.utime(edir / "meta.json", (old, old))
    os.utime(edir, (mtime, mtime))
    return edir


def test_research_arm_opt_in_default(monkeypatch):
    """Requirement 1: Unattended research must be opt-in by default (ORCH_RESEARCH_ARM=0)."""
    orchestrate_sh = Path(__file__).parent.parent / "orchestrate.sh"
    sh_text = orchestrate_sh.read_text(encoding="utf-8")
    assert 'export ORCH_RESEARCH_ARM="${ORCH_RESEARCH_ARM:-0}"' in sh_text


@pytest.mark.parametrize("guard_exit", [0, 1])
def test_research_usage_guard_cadence_branch_stamps_and_artifacts(tmp_path, guard_exit):
    """Execute the exact shell branch without launching the rest of the production tick."""

    orchestrate = Path(__file__).parent.parent / "orchestrate.sh"
    source = orchestrate.read_text(encoding="utf-8")
    start = source.index("if _cadence_due research-usage-guard")
    end = source.index("if _cadence_due relearn", start)
    cadence_branch = source[start:end]

    fake_orch = tmp_path / "fake-orch"
    fake_orch.mkdir()
    (fake_orch / "research_usage_guard.py").write_text(textwrap.dedent("""
            import json
            import os
            import sys
            from pathlib import Path

            output = Path(sys.argv[sys.argv.index("--write-report") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"health_status": "OK"}) + "\\n")
            print("fake deterministic usage guard")
            raise SystemExit(int(os.environ["FAKE_GUARD_EXIT"]))
            """).lstrip())
    state_dir = tmp_path / "state"
    stamp_dir = tmp_path / "stamps"
    stamp_dir.mkdir()
    harness = textwrap.dedent(f"""
        set -euo pipefail
        ORCH={fake_orch!s}
        STAMP_DIR={stamp_dir!s}
        _cadence_due() {{ return 0; }}
        _attempt_ok() {{ return 0; }}
        _mark_success() {{ touch "$STAMP_DIR/.last-$1"; rm -f "$STAMP_DIR/.fail-$1"; }}
        _mark_fail() {{ echo 1 > "$STAMP_DIR/.fail-$1"; }}
        {cadence_branch}
        """)
    env = dict(os.environ, ORCH_STATE_DIR=str(state_dir), FAKE_GUARD_EXIT=str(guard_exit))
    completed = subprocess.run(["bash", "-c", harness], env=env, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    assert (state_dir / "research-usage-report.json").exists()
    assert "fake deterministic usage guard" in (stamp_dir / "research-usage-report.log").read_text()
    assert (stamp_dir / ".last-research-usage-guard").exists() is (guard_exit == 0)
    assert (stamp_dir / ".fail-research-usage-guard").exists() is (guard_exit != 0)


def test_research_usage_guard_has_declared_lifecycle():
    declared = capabilities.KNOWN_GATES["research-usage-guard"]
    assert declared["matcher"] == {"kind": "tick_phase", "name": "research-usage-guard"}
    for field in (
        "entrypoint",
        "trigger_cadence",
        "flags_defaults",
        "output_artifact",
        "downstream_consumer",
        "learning_sink",
        "evidence_threshold",
    ):
        assert declared[field], field


def test_structured_spec_provenance_during_recovery(tmp_path):
    """Requirement 2: Recovery must set structured spec provenance in meta.json."""
    row = {
        "exp_id": "test-exp-1",
        "repo": "owner/repo",
        "target": "owner/repo#99",
        "agents": ["codex"],
        "task_type": "implement",
        "ts": int(time.time()),
    }

    # Simulate unfetchable gh issue -> fallback stub
    def unfetchable_gh(repo, num):
        return ""

    expdir = tmp_path / "experiments"

    # We mock arm_diff so recovery succeeds
    def mock_arm_diff(repo, exp_id, agent, base="main"):
        return "diff --git a/f.py b/f.py\n+new line"

    old_arm_diff = experiment_recovery.arm_diff
    experiment_recovery.arm_diff = mock_arm_diff
    try:
        res = experiment_recovery.restore_experiment(
            row, apply=True, exp_dir=expdir, gh_fn=unfetchable_gh
        )
        assert res["applied"] is True
        edir = expdir / "test-exp-1"
        meta = json.loads((edir / "meta.json").read_text())
        assert meta["missing_spec"] is True
        assert meta["spec_provenance"] == "missing_spec_stub"

        spec_text = (edir / "spec.md").read_text()
        assert experiment_recovery.is_missing_spec(meta, spec_text) is True
    finally:
        experiment_recovery.arm_diff = old_arm_diff


def test_missing_spec_zero_llm_dispatch_in_followup(tmp_path, monkeypatch):
    """Requirement 3: Missing-spec recovered experiments must never launch LLM judges or synthesis."""
    expdir = tmp_path / "experiments"
    edir = expdir / "missing-spec-exp"
    edir.mkdir(parents=True)

    meta = {
        "schema_version": 2,
        "repo": "owner/repo",
        "base": "main",
        "exp_id": "missing-spec-exp",
        "subject": "subj-missing",
        "agents": ["codex"],
        "members": [{"member_id": "codex", "agent": "codex", "legacy": True}],
        "recovered_from_branches": True,
        "missing_spec": True,
        "spec_provenance": "missing_spec_stub",
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    stub_spec = (
        f"{experiment_recovery.MISSING_SPEC_STUB_HEADER}\n[spec unavailable at recovery time]"
    )
    (edir / "spec.md").write_text(stub_spec)
    (edir / "codex.log").write_text("done")

    # Set mtime to past
    old_ts = time.time() - 3600
    os.utime(edir / "codex.log", (old_ts, old_ts))
    os.utime(edir / "meta.json", (old_ts, old_ts))

    evaluate_called = []
    synthesize_called = []

    def mock_evaluate(*args, **kwargs):
        evaluate_called.append(args)
        return {}

    def mock_synthesize(*args, **kwargs):
        synthesize_called.append(args)
        return {}

    lifecycles = []

    def mock_lifecycle(exp_id, lc, reason=None):
        lifecycles.append((exp_id, lc, reason))

    monkeypatch.setattr(exp_abcd, "EXP_DIR", expdir)
    monkeypatch.setattr(exp_abcd, "evaluate", mock_evaluate)
    monkeypatch.setattr(exp_abcd, "synthesize", mock_synthesize)

    exp_abcd.followup(
        max_experiments=5,
        evaluate_fn=mock_evaluate,
        synthesize_fn=mock_synthesize,
        subject_lifecycle_fn=mock_lifecycle,
    )

    assert (
        len(evaluate_called) == 0
    ), "LLM judges must NEVER be launched for missing-spec recovered experiments"
    assert (
        len(synthesize_called) == 0
    ), "Synthesis must NEVER be launched for missing-spec recovered experiments"

    # Verify idempotent terminal skip artifact
    skip_p = edir / "followup-skip.json"
    assert skip_p.exists()
    skip_data = json.loads(skip_p.read_text())
    assert skip_data["reason"] == "missing_spec_recovered"
    assert skip_data["spec_provenance"] == "missing_spec_stub"

    # Verify lifecycle marked
    assert any(lc[1] == "skipped" and lc[2] == "missing_spec_recovered" for lc in lifecycles)


def test_missing_spec_skip_survives_usage_ledger_failure(tmp_path, monkeypatch):
    expdir = tmp_path / "experiments"
    edir = expdir / "missing-spec-ledger-down"
    edir.mkdir(parents=True)
    (edir / "meta.json").write_text(
        json.dumps(
            {
                "repo": "owner/repo",
                "agents": ["codex"],
                "missing_spec": True,
                "spec_provenance": "missing_spec_stub",
            }
        )
    )
    (edir / "spec.md").write_text(experiment_recovery.MISSING_SPEC_STUB_HEADER)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", expdir)
    monkeypatch.setenv("ORCH_OBJECTIVE_ANCHOR", "0")
    monkeypatch.setattr(
        research_usage_guard,
        "assess_and_record_opportunity",
        lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("ledger unavailable")),
    )
    evaluate_called = []
    result = exp_abcd.followup(
        evaluate_fn=lambda *args, **kwargs: evaluate_called.append(args),
        subject_lifecycle_fn=lambda *args, **kwargs: None,
    )
    assert not evaluate_called
    assert result["processed"][0]["reason"] == "missing_spec_recovered"
    skip = json.loads((edir / "followup-skip.json").read_text())
    assert skip["guard_record_error"] == "ledger unavailable"


def test_missing_spec_skip_does_not_consume_evaluator_dispatch_cap(tmp_path, monkeypatch):
    expdir = tmp_path / "experiments"
    missing = expdir / "newer-missing-spec"
    missing.mkdir(parents=True)
    (missing / "meta.json").write_text(
        json.dumps(
            {
                "repo": "owner/repo",
                "base": "main",
                "missing_spec": True,
                "spec_provenance": "missing_spec_stub",
            }
        )
    )
    (missing / "spec.md").write_text(experiment_recovery.MISSING_SPEC_STUB_HEADER)
    _write_evaluable_experiment(expdir, "older-real-spec", mtime=time.time() - 100)
    os.utime(missing, (time.time(), time.time()))

    monkeypatch.setattr(exp_abcd, "EXP_DIR", expdir)
    monkeypatch.setenv("ORCH_OBJECTIVE_ANCHOR", "0")
    monkeypatch.setenv("ORCH_FOLLOWUP_SHIP_GATE", "0")
    dispatched = []

    def evaluate_once(repo, spec_path, exp_id, evaluators, timeout):
        dispatched.append((exp_id, list(evaluators)))
        return {"evaluators": list(evaluators), "objective_anchors": None}

    result = exp_abcd.followup(
        max_experiments=1,
        collect_fn=lambda *args: {
            "diffs": {"codex": {"bytes": 4, "diff": "diff --git a/a b/a\n+x\n"}}
        },
        evaluate_fn=evaluate_once,
        subject_lifecycle_fn=lambda *args, **kwargs: None,
    )
    assert dispatched == [("older-real-spec", ["vibe"])]
    assert {row["exp_id"] for row in result["processed"]} == {
        "newer-missing-spec",
        "older-real-spec",
    }
    with sqlite3.connect(feedback.DB_PATH) as db:
        recorded = db.execute(
            "SELECT evaluator_count,evaluator_agents_json FROM research_usage_opportunities "
            "WHERE exp_id='older-real-spec' AND decision='admitted'"
        ).fetchone()
    assert recorded == (1, '["vibe"]')


def test_malformed_followup_panel_skips_without_aborting_tick(tmp_path, monkeypatch):
    expdir = tmp_path / "experiments"
    _write_evaluable_experiment(expdir, "bad-panel", mtime=time.time())
    monkeypatch.setattr(exp_abcd, "EXP_DIR", expdir)
    monkeypatch.setenv("ORCH_FOLLOWUP_EVALUATORS", ",,")
    collect_calls = []
    result = exp_abcd.followup(
        collect_fn=lambda *args: collect_calls.append(args),
        evaluate_fn=lambda *args, **kwargs: pytest.fail("malformed panel dispatched a judge"),
        subject_lifecycle_fn=lambda *args, **kwargs: None,
    )
    assert not collect_calls
    assert result["skipped"] == [
        {
            "exp_id": "bad-panel",
            "reason": "invalid-followup-evaluators",
            "detail": "ORCH_FOLLOWUP_EVALUATORS must contain at least one evaluator",
        }
    ]


def test_unreadable_candidate_diff_defers_before_admission(tmp_path, monkeypatch):
    expdir = tmp_path / "experiments"
    _write_evaluable_experiment(expdir, "unreadable-diff", mtime=time.time())
    monkeypatch.setattr(exp_abcd, "EXP_DIR", expdir)
    missing_path = tmp_path / "vanished.diff"
    evaluate_calls = []
    result = exp_abcd.followup(
        collect_fn=lambda *args: {"diffs": {"codex": {"bytes": 10, "path": str(missing_path)}}},
        evaluate_fn=lambda *args, **kwargs: evaluate_calls.append(args),
        subject_lifecycle_fn=lambda *args, **kwargs: None,
    )
    assert not evaluate_calls
    assert result["skipped"][0]["reason"] == "candidate-diff-unreadable"
    assert "vanished.diff" in result["skipped"][0]["detail"]


def test_failed_admission_is_not_automatically_retried(tmp_path):
    db = sqlite3.connect(tmp_path / "failed.db")
    env = {"ORCH_RESEARCH_ARM": "1"}
    common = {
        "repo": "owner/repo",
        "subject": "subject",
        "spec_text": "stable spec",
        "base_sha": "base",
        "candidate_diffs": {"candidate": "diff"},
        "evaluator_agents": ["vibe"],
        "env": env,
        "conn": db,
    }
    admitted = research_usage_guard.assess_and_record_opportunity(exp_id="first", **common)
    research_usage_guard.update_opportunity_outcome(admitted["opportunity_id"], "failed", conn=db)
    retry = research_usage_guard.assess_and_record_opportunity(exp_id="retry", **common)
    assert retry["decision"] == "duplicate"
    assert retry["reason"] == "duplicate_signature_failed"
    db.close()


def test_direct_evaluate_rejects_missing_spec_before_dispatch(tmp_path, monkeypatch):
    """The invariant also holds when a caller bypasses followup and invokes evaluate directly."""

    expdir = tmp_path / "experiments"
    edir = expdir / "missing-direct"
    edir.mkdir(parents=True)
    (edir / "meta.json").write_text(
        json.dumps(
            {
                "repo": "owner/repo",
                "agents": ["codex"],
                "missing_spec": True,
                "spec_provenance": "missing_spec_stub",
            }
        )
    )
    spec_path = edir / "spec.md"
    spec_path.write_text(experiment_recovery.MISSING_SPEC_STUB_HEADER)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", expdir)

    with pytest.raises(exp_abcd.MissingSpecNotEvaluableError, match="objective-only"):
        exp_abcd.evaluate("owner/repo", str(spec_path), "missing-direct", ["vibe"])


def test_signature_deduplication_across_restarts(tmp_path, monkeypatch):
    """Requirement 4: Prevent repeated unchanged followup panels by a stable signature."""
    repo = "owner/repo"
    spec = "spec content"
    base = "main"
    diffs = {"codex": "diff content a", "cursor": "diff content b"}

    sig1 = research_usage_guard.compute_followup_signature(repo, spec, base, diffs)
    sig2 = research_usage_guard.compute_followup_signature(repo, spec, base, diffs)
    assert sig1 == sig2

    # Assess opportunity 1 -> admitted
    opp1 = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-sig-1",
        repo=repo,
        subject="subj-sig",
        spec_text=spec,
        base_sha=base,
        candidate_diffs=diffs,
        evaluator_count=2,
    )
    assert opp1["decision"] == "admitted"

    # Assess opportunity 2 with same signature -> duplicate
    opp2 = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-sig-2",
        repo=repo,
        subject="subj-sig",
        spec_text=spec,
        base_sha=base,
        candidate_diffs=diffs,
        evaluator_count=2,
    )
    assert opp2["decision"] == "duplicate"
    assert opp2["eligible"] is False

    # A restart long after the evaluator timeout remains fail-closed. A hard-killed process may
    # have spent the call before it could reconcile the terminal outcome.
    opp3 = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-sig-3",
        repo=repo,
        subject="subj-sig",
        spec_text=spec,
        base_sha=base,
        candidate_diffs=diffs,
        evaluator_count=2,
        now=int(time.time()) + 2 * 86_400,
    )
    assert opp3["decision"] == "duplicate"


def test_followup_signature_reads_production_collect_paths(tmp_path):
    diff_path = tmp_path / "candidate.diff"
    diff_path.write_text("diff --git a/a.py b/a.py\n+one\n")
    first = exp_abcd._collected_candidate_diffs(
        {"diffs": {"member-a": {"path": str(diff_path), "bytes": diff_path.stat().st_size}}}
    )
    first_signature = research_usage_guard.compute_followup_signature(
        "owner/repo", "spec", "base", first
    )

    diff_path.write_text("diff --git a/a.py b/a.py\n+two\n")
    second = exp_abcd._collected_candidate_diffs(
        {"diffs": {"member-a": {"path": str(diff_path), "bytes": diff_path.stat().st_size}}}
    )
    second_signature = research_usage_guard.compute_followup_signature(
        "owner/repo", "spec", "base", second
    )
    assert first_signature != second_signature
    assert first["member-a"].endswith("+one\n")
    assert second["member-a"].endswith("+two\n")


def test_usage_guard_limits_and_anomaly_blocking(tmp_path, monkeypatch):
    """Requirement 5: Deterministic local usage guard, rolling limits, and anomaly spike blocking."""
    db = sqlite3.connect(tmp_path / "test_guard.db")
    research_usage_guard.ensure_schema(db)
    now = 1_700_000_000

    env_limits = {
        "ORCH_RESEARCH_ARM": "1",
        "ORCH_GUARD_MAX_EVAL_CALLS_24H": "6",
        "ORCH_GUARD_MAX_EVAL_CALLS_1H": "4",
        "ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT": "2",
    }

    # First opportunity -> admitted
    opp1 = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-1",
        repo="o/r",
        subject="subj-A",
        spec_text="spec 1",
        base_sha="sha1",
        candidate_diffs={"codex": "d1"},
        evaluator_count=2,
        env=env_limits,
        conn=db,
        now=now,
    )
    assert opp1["decision"] == "admitted"

    # Second opportunity for subj-A -> admitted
    opp2 = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-2",
        repo="o/r",
        subject="subj-A",
        spec_text="spec 2",
        base_sha="sha2",
        candidate_diffs={"codex": "d2"},
        evaluator_count=2,
        env=env_limits,
        conn=db,
        now=now + 10,
    )
    assert opp2["decision"] == "admitted"

    # Third opportunity for subj-A -> repeated subject anomaly spike!
    opp3 = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-3",
        repo="o/r",
        subject="subj-A",
        spec_text="spec 3",
        base_sha="sha3",
        candidate_diffs={"codex": "d3"},
        evaluator_count=2,
        env=env_limits,
        conn=db,
        now=now + 20,
    )
    assert opp3["decision"] == "blocked_by_anomaly"
    assert opp3["eligible"] is False
    assert any(a["type"] == "repeated_subject_spike" for a in opp3["alerts"])

    # Test manual/supervised bypass
    opp_manual = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-3-man",
        repo="o/r",
        subject="subj-A",
        spec_text="spec 3",
        base_sha="sha3",
        candidate_diffs={"codex": "d3"},
        evaluator_count=2,
        is_manual=True,
        env=env_limits,
        conn=db,
        now=now + 30,
    )
    assert opp_manual["decision"] == "admitted"
    assert opp_manual["bypassed"] is True

    # Test report generation
    report = research_usage_guard.generate_usage_report(conn=db, now=now + 40)
    assert report["total_opportunities"] == 4
    assert report["health_status"] == "ANOMALY_BLOCKED"

    db.close()


def test_disabled_research_arm_defers_before_model_dispatch(tmp_path):
    db = sqlite3.connect(tmp_path / "disabled.db")
    opportunity = research_usage_guard.assess_and_record_opportunity(
        exp_id="exp-disabled",
        repo="owner/repo",
        subject="subject-disabled",
        spec_text="real spec",
        base_sha="abc123",
        candidate_diffs={"candidate": "diff"},
        evaluator_agents=["vibe"],
        env={},
        conn=db,
        now=1_700_000_000,
    )
    assert opportunity["decision"] == "deferred"
    assert opportunity["reason"] == "research_arm_disabled"
    assert opportunity["eligible"] is False
    db.close()


def test_observed_usage_detector_catches_guard_bypasses_and_legacy_panels(tmp_path):
    db = sqlite3.connect(tmp_path / "observed.db")
    db.execute("CREATE TABLE runs (ts INTEGER,task_type TEXT,experiment_id TEXT,agent TEXT)")
    now = 1_700_000_000
    exp_id = "tick-1699999990-owner-repo-42"
    for agent in ("claude", "codex", "cursor", "vibe"):
        db.execute(
            "INSERT INTO runs(ts,task_type,experiment_id,agent) VALUES (?,?,?,?)",
            (now - 10, "review", exp_id, agent),
        )
    expdir = tmp_path / "experiments" / exp_id
    expdir.mkdir(parents=True)
    (expdir / "meta.json").write_text(
        json.dumps({"missing_spec": True, "spec_provenance": "missing_spec_stub"})
    )
    (expdir / "spec.md").write_text(experiment_recovery.MISSING_SPEC_STUB_HEADER)
    db.commit()

    observed = research_usage_guard.observe_recent_research_usage(
        conn=db,
        window_days=7,
        now=now,
        experiment_dir=tmp_path / "experiments",
    )
    assert observed["panel_count"] == 1
    assert observed["evaluator_calls"] == 4
    assert observed["missing_spec_evaluator_calls"] == 4
    assert observed["wide_panel_count"] == 1
    assert observed["active_alert_count"] == 2
    db.close()


def test_observed_usage_detector_fails_visible_without_runs_table(tmp_path):
    db = sqlite3.connect(tmp_path / "no-runs.db")
    observed = research_usage_guard.observe_recent_research_usage(
        conn=db,
        window_days=7,
        now=1_700_000_000,
        experiment_dir=tmp_path / "experiments",
    )
    assert observed["telemetry_available"] is False
    assert observed["active_alert_count"] == 1
    assert observed["alerts"][0]["type"] == "observed_usage_telemetry_unavailable"
    report = research_usage_guard.generate_usage_report(conn=db, window_days=7, now=1_700_000_000)
    assert report["health_status"] == "OBSERVABILITY_UNAVAILABLE"
    db.close()


def test_observed_usage_detector_reports_invalid_config_without_crashing(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "invalid-config.db")
    db.execute("CREATE TABLE runs (ts INTEGER,task_type TEXT,experiment_id TEXT,agent TEXT)")
    monkeypatch.setenv("ORCH_GUARD_MAX_PANELS_PER_SUBJECT_24H", "not-an-integer")
    observed = research_usage_guard.observe_recent_research_usage(
        conn=db,
        window_days=7,
        now=1_700_000_000,
        experiment_dir=tmp_path / "experiments",
    )
    assert observed["telemetry_available"] is True
    assert observed["active_alert_count"] == 1
    assert observed["alerts"][0]["type"] == "observed_usage_config_invalid"
    assert observed["alerts"][0]["setting"] == "ORCH_GUARD_MAX_PANELS_PER_SUBJECT_24H"
    report = research_usage_guard.generate_usage_report(conn=db, now=1_700_000_000)
    assert report["health_status"] == "OBSERVED_ANOMALY"
    db.close()


def test_stale_dispatch_stays_deduplicated_and_visible(tmp_path):
    db = sqlite3.connect(tmp_path / "stale.db")
    db.execute("CREATE TABLE runs (ts INTEGER,task_type TEXT,experiment_id TEXT,agent TEXT)")
    now = 1_700_000_000
    env = {"ORCH_RESEARCH_ARM": "1"}
    admitted = research_usage_guard.assess_and_record_opportunity(
        exp_id="tick-1699980000-owner-repo-7",
        repo="owner/repo",
        subject="owner-repo-7",
        spec_text="stable spec",
        base_sha="abc123",
        candidate_diffs={"candidate": "diff"},
        evaluator_agents=["vibe"],
        env=env,
        conn=db,
        now=now - 3 * 3_600,
    )
    assert admitted["decision"] == "admitted"

    duplicate = research_usage_guard.assess_and_record_opportunity(
        exp_id="tick-1699999990-owner-repo-7",
        repo="owner/repo",
        subject="owner-repo-7",
        spec_text="stable spec",
        base_sha="abc123",
        candidate_diffs={"candidate": "diff"},
        evaluator_agents=["vibe"],
        env=env,
        conn=db,
        now=now,
    )
    assert duplicate["decision"] == "duplicate"
    report = research_usage_guard.generate_usage_report(conn=db, now=now)
    assert report["stale_dispatching_opportunities"] == 1
    assert report["health_status"] == "STALE_DISPATCH"
    db.close()


def test_same_second_opportunities_remain_distinct_denominator_rows(tmp_path):
    db = sqlite3.connect(tmp_path / "denominator.db")
    env = {"ORCH_RESEARCH_ARM": "1"}
    kwargs = {
        "exp_id": "same-exp",
        "repo": "owner/repo",
        "subject": "same-subject",
        "spec_text": "same spec",
        "base_sha": "same-sha",
        "candidate_diffs": {"candidate": "same diff"},
        "evaluator_agents": ["vibe"],
        "env": env,
        "conn": db,
        "now": 1_700_000_000,
    }
    first = research_usage_guard.assess_and_record_opportunity(**kwargs)
    second = research_usage_guard.assess_and_record_opportunity(**kwargs)
    third = research_usage_guard.assess_and_record_opportunity(**kwargs)
    assert first["decision"] == "admitted"
    assert second["decision"] == "duplicate"
    assert third["decision"] == "duplicate"
    assert len({first["opportunity_id"], second["opportunity_id"], third["opportunity_id"]}) == 3
    assert db.execute("SELECT COUNT(*) FROM research_usage_opportunities").fetchone()[0] == 3
    db.close()


def test_evaluator_panel_size_semantics(tmp_path):
    """Requirement 6: Direct evaluate retains 4 evaluators default; followup can use smaller panel."""
    # Direct evaluate with evaluators=None
    specs_direct = exp_abcd._normalize_evaluator_specs(
        requested=None,
        default_agents=["codex", "cursor"],
        implementer_agents=["codex", "cursor"],
    )
    assert (
        len(specs_direct) == 4
    ), "Direct evaluate with evaluators=None must default to 4 evaluators"

    # Explicit small panel (e.g. followup panel)
    specs_followup = exp_abcd._normalize_evaluator_specs(
        requested=["codex", "claude"],
        default_agents=["codex", "cursor"],
        implementer_agents=["codex", "cursor"],
    )
    assert len(specs_followup) == 2, "Explicit panel of 2 evaluators must preserve 2 evaluators"

    # The unattended one-judge panel is exact even when that judge also implemented an arm.
    specs_single = exp_abcd._normalize_evaluator_specs(
        requested=["vibe"],
        default_agents=["vibe", "codex"],
        implementer_agents=["vibe", "codex"],
    )
    assert [row["agent"] for row in specs_single] == ["vibe"]

    with pytest.raises(ValueError, match="must not be empty"):
        exp_abcd._normalize_evaluator_specs(
            requested=[],
            default_agents=["codex", "cursor"],
            implementer_agents=["codex", "cursor"],
        )


def test_ledger_telemetry_isolation():
    """Requirement 7: Verify test suite writes LEDGER into temp state and not host path."""
    host_ledger = adapters.HOME / ".codex" / "handoff" / "capacity-ledger.ndjson"
    host_mtime_before = host_ledger.stat().st_mtime if host_ledger.exists() else None

    # Calling record_ledger during unit test should write to isolated LEDGER
    assert str(adapters.LEDGER).startswith(str(Path(os.environ.get("HANDOFF_DIR"))))
    adapters.record_ledger("codex", count=1, cost_usd=0.0)

    if host_ledger.exists():
        assert host_ledger.stat().st_mtime == host_mtime_before
