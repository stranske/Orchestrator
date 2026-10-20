"""Lane C round-2 coverage for Orchestrator #802 ranked targets.

Hermetic tests for remaining gaps in redirect_apply, evidence_acquisition, ux_review,
execution_profiles, and exploration_backfill. Stubs every subprocess/agent/network path.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import evidence_acquisition as ea
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
# evidence_acquisition — last tick-line CLI branch
# ---------------------------------------------------------------------------


def test_evidence_acquisition_tick_line_requires_plan() -> None:
    with pytest.raises(SystemExit):
        ea.main(["tick-line"])


# ---------------------------------------------------------------------------
# redirect_apply — remaining production + helper paths
# ---------------------------------------------------------------------------


def test_redirect_apply_heartbeat_swallows_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        ra.capabilities,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    ra._heartbeat("invocation", ref="o/r#1")  # must not raise


def test_redirect_apply_flag_as_the_tick_sees_it_paths(monkeypatch) -> None:
    import capability_recurrence_check as crc

    monkeypatch.setenv(ra.BOOTSTRAP_FLAG, "1")
    enabled, source = ra.flag_as_the_tick_sees_it()
    assert enabled is True and source == "ambient"

    monkeypatch.delenv(ra.BOOTSTRAP_FLAG, raising=False)

    monkeypatch.setattr(
        crc, "tick_env", lambda **k: (_ for _ in ()).throw(RuntimeError("no tick env"))
    )
    enabled, source = ra.flag_as_the_tick_sees_it()
    assert enabled is False and "unresolved" in source

    monkeypatch.setattr(crc, "tick_env", lambda **k: {})
    enabled, source = ra.flag_as_the_tick_sees_it()
    assert enabled is False and source == "unset"

    monkeypatch.setattr(crc, "tick_env", lambda **k: {ra.BOOTSTRAP_FLAG: "1"})
    enabled, source = ra.flag_as_the_tick_sees_it()
    assert enabled is True and source == "tick"


def test_redirect_apply_replay_stalls_clears_heartbeat_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(ra, "_replay_stalls", lambda directory: {"total": 0, "authorized": 0})
    out = ra.replay_stalls(tmp_path)
    assert out["total"] == 0
    assert "ORCH_CAPABILITY_HEARTBEATS" not in __import__("os").environ


def test_redirect_apply_authorize_placeholder_command() -> None:
    gate = {
        "synced_role_outcomes": 0,
        "synced_needed": 1,
        "linked_disagreements": 0,
        "disagreements_needed": 1,
        "ready_for_supervised_apply": False,
        "bootstrap_needed": True,
    }
    plan = {
        "target": "o/r#1",
        "action": "redirect",
        "accepted_role_run_id": "rr1",
        "prompt_text": "x",
        "prompt_file": "/tmp/x",
        "steps": [
            {
                "id": "delegate-retry",
                "commands": [["echo", "<next-agent>"]],
            }
        ],
    }
    auth = ra.authorize(
        plan_obj=plan,
        role_run_id="rr1",
        decision_source="redirect_agent",
        errors=None,
        pid_alive=False,
        claim_holder=None,
        prior_agent="codex",
        gate=gate,
        applied_targets=set(),
        applies_today=0,
        flag_on=False,
        lane_state="stalled",
        recommended_action="redirect",
    )
    assert any("placeholder" in b for b in auth["blocks"])


def test_redirect_apply_apply_one_valueerror_from_apply_plan(tmp_path, monkeypatch) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        ra, "gate_state", lambda *a, **k: {"bootstrap_needed": False, "disagreements_needed": 0}
    )
    monkeypatch.setattr(ra, "_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(ra, "_applied_history", lambda *a, **k: (set(), 0))
    monkeypatch.setattr(ra.claims, "holder", lambda *a, **k: None)
    monkeypatch.setattr(ra.redirect_shadow, "record_proposal", lambda **k: None)
    monkeypatch.setattr(ra.redirect_shadow, "record_apply", lambda **k: None)
    monkeypatch.setattr(
        ra,
        "authorize",
        lambda **k: {
            "allowed": True,
            "blocks": [],
            "target": "o/r#1",
            "role_run_id": "rr",
            "flag_on": True,
            "would_mutate": True,
            "closes_disagreement_deficit": False,
        },
    )
    monkeypatch.setattr(
        ra.redirect_plan,
        "apply_plan",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("lane refused")),
    )

    def role_runner(*a, **k):
        return {
            "plan": {
                "target": "o/r#1",
                "action": "redirect",
                "prompt_text": "p",
                "prompt_file": "f",
            },
            "role_run_id": "rr",
            "decision_source": "redirect_agent",
            "proposal": {},
            "baseline": {},
            "errors": [],
        }

    out = ra.apply_one(
        report={"target": "o/r#1", "pid": None, "state": "stalled"},
        acceptance_criteria="",
        role_runner=role_runner,
        env={ra.BOOTSTRAP_FLAG: "1"},
        corpus_path=corpus,
    )
    assert out["authorization"]["allowed"] is False
    assert out["authorization"]["would_mutate"] is False
    assert "lane refused" in out["authorization"]["blocks"]
    assert out["apply_result"]["applied"] is False


def test_redirect_apply_stage2_population_ineligible_and_bad_plans(tmp_path) -> None:
    now = 1_700_000_000
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"generated_at": now, "plans": "not-a-list"}), encoding="utf-8")
    unreadable = ra.stage2_population(bad, now=now)
    assert unreadable["status"] == "unreadable"

    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "generated_at": now,
                "plans": [
                    {"eligible": False, "report": {"target": "o/r#1"}},
                    {"eligible": True, "report": {}},
                    {"eligible": True, "report": {"target": "o/r#2"}, "acceptance_criteria": "ac"},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = ra.stage2_population(plan, now=now)
    assert out["status"] == "current"
    assert out["ineligible"] == 2
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["report"]["target"] == "o/r#2"


def test_redirect_apply_sweep_skips_non_dict_and_empty_target(tmp_path) -> None:
    now = 1_700_000_000
    sweep = tmp_path / "sweep.json"
    sweep.write_text(
        json.dumps(
            {
                "generated_at": now,
                "actionable": [
                    "not-a-dict",
                    {"state": "stalled"},  # no target
                    {"state": "stalled", "target": "o/r#9", "agent": "keepalive"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rows, meta = ra.sweep_stalled_candidates(sweep, now=now)
    assert meta["status"] == "current"
    assert len(rows) == 1
    assert rows[0]["report"]["target"] == "o/r#9"


def test_redirect_apply_judged_inputs_skips_invalid_proposals(tmp_path) -> None:
    corpus = tmp_path / "c.jsonl"
    events = [
        {
            "kind": "redirect_proposal",
            "source": "live-dispatch",
            "target": "o/r#1",
            "ts": 10,
            "valid_proposal": False,
            "report_sha256": "a",
            "acceptance_criteria_sha256": "b",
        },
        {
            "kind": "redirect_proposal",
            "source": "live-dispatch",
            "target": "o/r#1",
            "ts": 20,
            "valid_proposal": True,
            "report_sha256": "a",
            "acceptance_criteria_sha256": "b",
            "proposal_action": "redirect",
        },
    ]
    corpus.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    answers, last = ra.judged_inputs(corpus)
    assert last["o/r#1"] == 20
    assert answers[("o/r#1", "a", "b")]["action"] == "redirect"


def test_redirect_apply_format_apply_verdicts() -> None:
    skipped = ra.format_apply(
        {
            "skipped": "flag off",
            "offloads_spent": 0,
            "authorized": 0,
            "applied": 0,
            "results": [],
            "candidate_counts": {"supervisor": 0, "sweep": 0, "eligible": 0},
        },
        flag_on=False,
    )
    assert any("UNKNOWN" in line for line in skipped)

    lines = ra.format_apply(
        {
            "offloads_spent": 1,
            "authorized": 2,
            "applied": 1,
            "passing_screen": 1,
            "reports_seen": 2,
            "stale_reports": 0,
            "population": {},
            "current_candidates": 1,
            "candidate_counts": {"supervisor": 1, "sweep": 0, "eligible": 1},
            "deferred_by_cap": 1,
            "max_offloads": 1,
            "results": [
                {"target": "a/r#1", "applied": True, "blocks": []},
                {"target": "a/r#2", "authorized": True, "blocks": ["x"]},
                {"target": "a/r#3", "offloaded": True, "blocks": ["y"]},
                {"target": "a/r#4", "deferred": True, "blocks": ["z"]},
                {"target": "a/r#5", "blocks": ["screen"]},
            ],
        },
        flag_on=True,
    )
    joined = "\n".join(lines)
    assert "APPLIED" in joined
    assert "authorized (flag off)" in joined
    assert "refused" in joined
    assert "deferred (no offload)" in joined
    assert "screened out" in joined


def test_redirect_apply_corpus_digest_and_recording_runner(tmp_path) -> None:
    missing = tmp_path / "missing.jsonl"
    digest = ra._corpus_digest(missing)
    assert len(digest) == 64
    spent: list = []
    runner = ra._recording_role_runner(spent)
    assert runner(1, 2) == {}
    assert spent == [(1, 2)]


def test_redirect_apply_main_screen_and_apply_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ra,
        "screen_candidates",
        lambda **k: {
            "candidate_counts": {"supervisor": 1, "sweep": 0, "eligible": 0},
            "passing_screen": 0,
            "current_candidates": 1,
            "reports_seen": 1,
            "stale_reports": 0,
            "population": {},
            "candidates": [{"target": "o/r#1", "passes_screen": False, "blocks": ["dead"]}],
            "note": "screen note",
        },
    )
    assert ra.main(["--screen", "--json"]) == 0
    assert '"passing_screen"' in capsys.readouterr().out

    assert ra.main(["--screen"]) == 0
    out = capsys.readouterr().out
    assert "o/r#1" in out and "dead" in out and "screen note" in out

    monkeypatch.setattr(
        ra,
        "apply_candidates",
        lambda **k: {
            "offloads_spent": 0,
            "authorized": 0,
            "applied": 0,
            "passing_screen": 0,
            "reports_seen": 0,
            "results": [],
            "candidate_counts": {},
        },
    )
    assert ra.main(["--apply", "--json", "--dry-run"]) == 0
    assert "offloads_spent" in capsys.readouterr().out

    monkeypatch.setattr(
        ra,
        "status",
        lambda *a, **k: {
            "flag_on": False,
            "flag_source": "ambient",
            "gate": {
                "synced_role_outcomes": 0,
                "synced_needed": 1,
                "linked_disagreements": 0,
                "disagreements_needed": 1,
                "ready_for_supervised_apply": False,
                "bootstrap_needed": True,
            },
            "drainable": 0,
            "current_candidates": 0,
            "reports_on_disk": 0,
            "stale_reports": 0,
            "drainable_population": {},
            "applies_today": 0,
            "daily_bound": 1,
            "unlinked_applied": None,
            "candidate_counts": {},
        },
    )
    assert ra.main(["--status", "--json"]) == 0
    assert "bootstrap_needed" in capsys.readouterr().out

    monkeypatch.setattr(
        ra, "link_applied_outcomes", lambda **k: {"linked": 0, "failed": 0, "pending": 0}
    )
    assert ra.main(["--link-outcomes", "--json"]) == 0
    assert "pending" in capsys.readouterr().out

    monkeypatch.setattr(ra, "replay_stalls", lambda p: {"total": 1, "authorized": 1})
    assert ra.main(["--replay-stalls", str(tmp_path), "--json"]) == 0


# ---------------------------------------------------------------------------
# ux_review — pure helpers and hermetic Brain/CLI paths
# ---------------------------------------------------------------------------


def test_ux_review_stuck_probability_merge_and_spread() -> None:
    a = {
        "screen": "H",
        "element": "E",
        "failure_mode": "confusion",
        "severity": 2,
        "confidence": 0.4,
        "stuck_probability": 0.2,
        "click_path": ["x"],
    }
    b = {
        **a,
        "severity": 3,
        "confidence": 0.9,
        "stuck_probability": 0.8,
    }
    merged = ur.dedupe_findings([a, b])
    assert merged[0]["stuck_probability"] == 0.8

    group = ur._merge_finding_group([a, b])
    assert group["stuck_probability"] == 0.8

    spread = ur.finding_severity_spread(
        {"claude": [a], "codex": [{**a, "severity": 5}]},
        ur.finding_key(a),
    )
    assert spread == 3
    assert ur.finding_severity_spread({"claude": [a]}, ur.finding_key(a)) == 0


def test_ux_review_substance_unprobed_and_coverage_gaps() -> None:
    assert ur._substance_verdict({"output_a": None, "output_b": "x"}) == "unprobed"
    gaps = ur.substance_gaps(
        {
            "substance": [{"surface": "Budget", "figure": "total"}],
            "coverage": [
                {"driven": False, "note": "needs substance probe", "surface": "Cost"},
                {"driven": True, "note": "ok"},
                "skip-me",
            ],
        }
    )
    assert any("Budget" in g for g in gaps)
    assert any("Cost" in g for g in gaps)


def test_ux_review_derive_findings_sev_zero_branch() -> None:
    # threshold above DERIVE_THRESHOLD so severity_from_median can return 0
    out = ur.derive_findings(
        {"screens": [{"name": "Home"}], "scenarios": [], "wired": {"findings": []}},
        {dim: 6.5 for dim in ur.DIMENSIONS},
        threshold=10.0,
    )
    assert out == []


def test_ux_review_normalize_gaps_and_aggregate_edge_paths() -> None:
    assert ur._normalize_evidence_gaps(
        {"evidence_gaps": ["", "  gap  ", {"gap": "gap"}, "GAP"]}
    ) == ["gap"]

    agg = ur.aggregate_panel(
        {
            "claude": None,
            "codex": {
                "scores": {"wired": "bad", "usability": 3},
                "overall": "nope",
                "findings": [
                    {
                        "screen": "H",
                        "element": "E",
                        "failure_mode": "confusion",
                        "severity": 4,
                        "click_path": ["a"],
                    }
                ],
            },
            "cursor": {
                "scores": {"usability": 8},
                "overall": 7,
                "findings": [
                    {
                        "screen": "H",
                        "element": "E",
                        "failure_mode": "confusion",
                        "severity": 2,
                        "click_path": ["a"],
                    }
                ],
            },
            "gemini": {
                "scores": {"usability": 7},
                "overall": 7,
                "findings": [
                    {
                        "screen": "H",
                        "element": "E",
                        "failure_mode": "confusion",
                        "severity": 2,
                        "click_path": ["a"],
                    }
                ],
            },
        },
        {"findings": []},
        n_evaluators=4,
    )
    assert any(
        k.startswith("finding:") for k in agg["consensus_flags"] if agg["consensus_flags"][k]
    )


def test_ux_review_synthesize_improvements_legacy_fix_hint() -> None:
    out = ur.synthesize_improvements(
        {
            "findings": [
                {
                    "dimension": "usability",
                    "failure_mode": "confusion",
                    "severity": 3,
                    "fix_hint": "add labels",
                    "element": "Run",
                }
            ]
        }
    )
    assert out["improvements"][0]["fix_hints"] == ["add labels"]


def test_ux_review_discover_and_backfill_panels(tmp_path, monkeypatch) -> None:
    assert ur.discover_historical_panels(tmp_path / "missing") == []

    root = tmp_path / "reviews"
    # directory name that does not match PANEL_DIR_RE
    junk = root / "not-a-panel"
    junk.mkdir(parents=True)
    (junk / "rubric-prompt-x.txt").write_text("spec", encoding="utf-8")

    panel = root / "DemoApp_uxreview_2026-01-01"
    panel.mkdir(parents=True)
    (panel / "rubric-prompt-claude.txt").write_text("same-spec", encoding="utf-8")
    # no arm outputs -> skipped for no arms
    panels = ur.discover_historical_panels(root)
    assert panels and panels[0]["arms"] == []

    dry = ur.backfill_panel_subjects(root, apply=False)
    assert any(s["reason"] == "no arm produced output" for s in dry["skipped"])

    (panel / "rubric-out-claude.txt").write_text('{"scores":{}}', encoding="utf-8")
    (panel / "rubric-prompt-codex.txt").write_text("different-spec", encoding="utf-8")
    panels2 = ur.discover_historical_panels(root)
    assert panels2[0]["spec_variants"] == 2
    multi = ur.backfill_panel_subjects(root, apply=False)
    assert any("rubric differs" in s["reason"] for s in multi["skipped"])

    # registration-failed path under apply=True
    good = root / "GoodApp_uxreview_2026-02-02"
    good.mkdir()
    (good / "rubric-prompt-a.txt").write_text("one-spec", encoding="utf-8")
    (good / "rubric-out-claude.txt").write_text('{"ok":true}', encoding="utf-8")
    monkeypatch.setattr(ur, "register_panel_subject", lambda *a, **k: None)
    conn = sqlite3.connect(":memory:")
    research_subjects.ensure_schema(conn)
    applied = ur.backfill_panel_subjects(root, apply=True, conn=conn)
    assert any(s["reason"] == "registration failed" for s in applied["skipped"])

    # Matching single-spec panel with arms: dry-run registers without writing.
    single = root / "SoloApp_uxreview_2026-03-03"
    single.mkdir()
    (single / "rubric-prompt-a.txt").write_text("solo-spec", encoding="utf-8")
    (single / "rubric-out-claude.txt").write_text('{"ok":true}', encoding="utf-8")
    dry_ok = ur.backfill_panel_subjects(root, apply=False)
    assert any(r["review_id"] == single.name for r in dry_ok["detail"])
    assert dry_ok["registered"] >= 1


def test_ux_review_cross_repo_patterns(tmp_path, monkeypatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(ur.time, "time", lambda: now)
    verdict = json.dumps(
        {
            "findings": [
                {
                    "failure_mode": "confusion",
                    "severity": 4,
                    "screen": "Home",
                    "element": "Go",
                },
                {"failure_mode": "confusion", "severity": 1},
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
        c.execute(
            "INSERT INTO evaluations "
            "(experiment_id, implementer, evaluator, score, rank, verdict, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("x:uxreview:1", "app/three", "claude", 7.0, 1, "{bad", now - 10),
        )
        c.execute(
            "INSERT INTO evaluations "
            "(experiment_id, implementer, evaluator, score, rank, verdict, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("y:uxreview:1", "app/four", "claude", 7.0, 1, "", now - 10),
        )
        c.commit()

    patterns = ur.cross_repo_patterns(window_days=30, min_recurrence=2)
    assert patterns and patterns[0]["failure_mode"] == "confusion"
    assert patterns[0]["occurrences"] >= 2


def test_ux_review_main_cli_paths(tmp_path, monkeypatch, capsys) -> None:
    assert ur.main([]) == 2
    assert capsys.readouterr().out

    monkeypatch.setattr(
        ur, "backfill_panel_subjects", lambda **k: {"registered": [], "skipped": []}
    )
    assert ur.main(["--backfill-panels"]) == 0
    assert "registered" in capsys.readouterr().out

    monkeypatch.setattr(ur, "_selftest", lambda: None)
    assert ur.main(["--selftest"]) == 0

    bundle = tmp_path / "b.json"
    bundle.write_text(json.dumps({"app": "a/b", "review_id": "r"}), encoding="utf-8")
    monkeypatch.setattr(ur, "review", lambda *a, **k: {"ok": True})
    assert ur.main(["--bundle", str(bundle), "--evaluators", "claude,codex"]) == 0
    assert "ok" in capsys.readouterr().out


def test_ux_review_resolve_base_sha_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ur.provision, "canonical_path", lambda app: None)
    assert ur.resolve_panel_base_sha({"app": "missing/app"}) is None

    fake = tmp_path / "repo"
    fake.mkdir()
    monkeypatch.setattr(ur.provision, "canonical_path", lambda app: str(fake))

    def fake_run(*a, **k):
        return SimpleNamespace(stdout="abc123\n", returncode=0)

    monkeypatch.setattr(ur.subprocess, "run", fake_run)
    assert ur.resolve_panel_base_sha({"app": "o/r"}) == "abc123"

    def fail_run(*a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(ur.subprocess, "run", fail_run)
    assert ur.resolve_panel_base_sha({"app": "o/r"}) is None


# ---------------------------------------------------------------------------
# execution_profiles — validation, retirement, empty selection, attach errors
# ---------------------------------------------------------------------------


def test_execution_profiles_validate_rejects_bad_shapes() -> None:
    base = dict(ep.get_profile("codex-6.1-sol-medium"))
    bad_perm = {**base, "permission_mode": "yolo"}
    with pytest.raises(ValueError, match="permission_mode"):
        ep.validate_profile(bad_perm)
    empty_pools = {**base, "capacity_pool_ids": []}
    with pytest.raises(ValueError, match="capacity pool"):
        ep.validate_profile(empty_pools)
    unknown = {**base, "capacity_pool_ids": ["not-a-real-pool"]}
    with pytest.raises(ValueError, match="unknown capacity"):
        ep.validate_profile(unknown)


def test_execution_profiles_retirement_cycle_and_empty_eligible(monkeypatch) -> None:
    monkeypatch.setitem(ep.PROFILE_RETIREMENTS, "cycle-a", "cycle-b")
    monkeypatch.setitem(ep.PROFILE_RETIREMENTS, "cycle-b", "cycle-a")
    with pytest.raises(ValueError, match="successor cycle"):
        ep.resolve_production_profile_id("cycle-a")

    # self-successor returns current
    monkeypatch.setitem(ep.PROFILE_RETIREMENTS, "selfish", "selfish")
    assert ep.resolve_production_profile_id("selfish") == "selfish"

    env = ep.select_profile(
        "implement",
        "o/r#1",
        ["codex-6.1-sol-medium"],
        rng_seed=1,
        gate_results={"codex-6.1-sol-medium": {"eligible": False}},
    )
    assert env["selected_profile_id"] is None
    assert env["assignment_probability"] == 0.0

    # Retired candidate is gated off unless allow_retired_profiles.
    retired = next(iter(ep.PROFILE_RETIREMENTS))
    gated = ep.select_profile("implement", None, [retired], rng_seed=2)
    assert gated["selected_profile_id"] is None

    explored = ep.select_profile(
        "implement",
        None,
        ["codex-6.1-sol-medium", "codex-6.1-sol-high"],
        rng_seed=3,
        exploration=True,
    )
    assert explored["selected_profile_id"] in {
        "codex-6.1-sol-medium",
        "codex-6.1-sol-high",
    }
    assert explored["assignment_probability"] == 0.5


def test_execution_profiles_replay_and_attach_errors() -> None:
    env = ep.select_profile("implement", None, ["codex-6.1-sol-medium"], rng_seed=7)
    broken = {**env, "replay_hash": "deadbeef"}
    with pytest.raises(ValueError, match="not replayable"):
        ep.replay_decision(broken)

    conn = sqlite3.connect(":memory:")
    ep.ensure_schema(conn, now=1_700_000_000)
    with pytest.raises(ValueError, match="unknown routing decision"):
        ep.attach_profile_attempt(conn, "missing", "attempt")
    ep.record_decision(conn, env, ts=1_700_000_000)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS execution_attempts ("
        "attempt_id TEXT PRIMARY KEY, profile_id TEXT)"
    )
    with pytest.raises(ValueError, match="unknown execution attempt"):
        ep.attach_profile_attempt(conn, env["decision_id"], "missing-attempt")


def test_execution_profiles_immutable_pool_and_profile_guards() -> None:
    conn = sqlite3.connect(":memory:")
    ep.ensure_schema(conn, now=1_700_000_000)
    pool_id = next(iter(ep.CAPACITY_POOLS))
    conn.execute(
        "UPDATE capacity_pools SET definition_json=? WHERE pool_id=?",
        ("{}", pool_id),
    )
    with pytest.raises(ValueError, match="immutable capacity pool"):
        ep.ensure_schema(conn, now=1_700_000_001)

    conn2 = sqlite3.connect(":memory:")
    ep.ensure_schema(conn2, now=1_700_000_000)
    pid = "codex-6.1-sol-medium"
    conn2.execute(
        "UPDATE execution_profiles SET definition_json=? WHERE profile_id=?",
        ("{}", pid),
    )
    with pytest.raises(ValueError, match="immutable execution profile"):
        ep.ensure_schema(conn2, now=1_700_000_001)


# ---------------------------------------------------------------------------
# exploration_backfill — helpers, plan statuses, schedule gates, CLI
# ---------------------------------------------------------------------------


def test_exploration_backfill_evaluated_targets_from_feedback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(eb, "_target_from_backfill_exp", lambda exp_id: "o/r#9")
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO evaluations "
            "(experiment_id, implementer, evaluator, score, rank, verdict, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("backfill-abc", "codex", "claude", 1.0, 1, "{}", 1),
        )
        c.commit()
    assert "o/r#9" in eb._evaluated_backfill_targets()

    class Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(feedback, "_conn", lambda: Boom())
    assert eb._evaluated_backfill_targets() == set()


def test_exploration_backfill_missing_cells_and_target_parsers(tmp_path, monkeypatch) -> None:
    route = {"implement": {"agents": [{"agent": "codex"}, {"agent": "claude"}, {"agent": "aider"}]}}
    coverage = {
        "tasks": [
            {"task_type": None},
            {"task_type": "implement", "ready_for_default_review": True},
            {
                "task_type": "implement",
                "ready_for_default_review": False,
                "agent_observations": {"codex": 0, "claude": 5},
                "observed_agents": 1,
                "needed_total_observations": 10,
                "needed_observed_agents": 2,
                "needed_top_agent_observations": 5,
            },
        ]
    }
    cells = eb._missing_cells(coverage, route_table=route)
    assert cells and all(c["agent"] != "aider" for c in cells)

    spec = tmp_path / "spec.md"
    spec.write_text("Title\nTarget: owner/repo#9\n", encoding="utf-8")
    assert eb._target_from_backfill_spec(spec) == "owner/repo#9"
    assert eb._target_from_backfill_spec(tmp_path / "missing.md") is None
    empty = tmp_path / "empty.md"
    empty.write_text("no target line\n", encoding="utf-8")
    assert eb._target_from_backfill_spec(empty) is None

    monkeypatch.setattr(eb.exp_abcd, "exp_paths", lambda exp_id: tmp_path / exp_id)
    exp = tmp_path / "backfill-x"
    exp.mkdir()
    (exp / "meta.json").write_text(json.dumps({"backfill_target": "a/b#1"}), encoding="utf-8")
    assert eb._target_from_backfill_exp("backfill-x") == "a/b#1"
    (exp / "meta.json").write_text("{", encoding="utf-8")
    (exp / "spec.md").write_text("Target: c/d#2\n", encoding="utf-8")
    assert eb._target_from_backfill_exp("backfill-x") == "c/d#2"


def test_exploration_backfill_items_by_task_and_anchor(monkeypatch) -> None:
    monkeypatch.setattr(eb.claims, "active_claims", lambda: {"taken/r#1"})
    monkeypatch.setattr(eb, "_evaluated_backfill_targets", lambda: {"done/r#2"})
    items = [
        {"target": "ok/r#3", "task_type": "implement", "lane": "opener"},
        {"target": "taken/r#1", "task_type": "implement", "lane": "opener"},
        {"target": "done/r#2", "task_type": "implement", "lane": "opener"},
        {"target": "other/r#4", "task_type": "implement", "lane": "closer"},
    ]
    by_task = eb._items_by_task(items)
    assert by_task == {"implement": [items[0]]}

    route = {"implement": {"agents": [{"agent": "codex"}, {"agent": "claude"}]}}
    states = {"codex": "ok", "claude": "shed"}
    assert eb._anchor_agent("implement", ["claude"], states, route) == "codex"
    # Every route agent is already in the missing set → no anchor left.
    assert eb._anchor_agent("implement", ["codex", "claude"], {"codex": "shed"}, route) is None


def test_exploration_backfill_build_jobs_and_format(monkeypatch) -> None:
    monkeypatch.setattr(eb, "_evaluated_backfill_targets", lambda: set())
    monkeypatch.setattr(eb.claims, "active_claims", lambda: set())
    route = {"implement": {"agents": [{"agent": "codex"}, {"agent": "claude"}]}}
    cells = [
        {
            "task_type": "implement",
            "agent": "claude",
            "observations": 0,
            "target_observations": 3,
            "needed_observations": 3,
            "reasons": ["zero_observation_cell"],
        }
    ]
    items = [
        {
            "target": "o/r#1",
            "task_type": "implement",
            "lane": "opener",
            "title": "T",
        }
    ]
    jobs, blocked = eb._build_jobs(
        cells=cells,
        items=items,
        capacity_snapshot={"agents": {"codex": {"state": "ok"}, "claude": {"state": "ok"}}},
        route_table=route,
        max_jobs=2,
    )
    assert jobs and len(jobs[0]["agents"]) >= 2
    assert blocked == []

    no_items, blocked2 = eb._build_jobs(
        cells=cells,
        items=[],
        capacity_snapshot={"agents": {"codex": {"state": "ok"}, "claude": {"state": "ok"}}},
        route_table=route,
        max_jobs=2,
    )
    assert no_items == [] and blocked2 and "no unclaimed" in blocked2[0]["reason"]

    plan = {
        "status": "blocked",
        "active_backfill_eligible": False,
        "missing_cell_count": 1,
        "planned_jobs": jobs,
        "blocked_tasks": [],
        "blockers": ["example blocker"],
        "next_action": "inspect",
        "direct_mode_progress": {"outcome_exploration_runs": 0, "progressing": False},
    }
    text = eb.format_human(plan)
    assert "blockers:" in text and "planned jobs:" in text and "o/r#1" in text

    plan2 = {**plan, "planned_jobs": [], "blocked_tasks": blocked2}
    text2 = eb.format_human(plan2)
    assert "blocked task types:" in text2


def test_exploration_backfill_build_plan_status_branches(monkeypatch) -> None:
    monkeypatch.setattr(
        eb.exploration_evidence_plan,
        "build_plan",
        lambda **k: {
            "route_coverage_deficits": {"tasks": []},
            "exploration_review": {"route_ready": False},
        },
    )
    monkeypatch.setattr(
        eb.exploration_review,
        "build_report",
        lambda **k: {"recorded_exploration_evidence": {"outcome_exploration_runs": 0}},
    )
    monkeypatch.setattr(eb.exploration_evidence_plan, "_backlog_items", lambda *a, **k: [])
    monkeypatch.setattr(
        eb.exploration_evidence_plan,
        "_capacity_snapshot",
        lambda *a, **k: {"agents": {}},
    )
    plan = eb.build_plan(require_direct_progress=True, max_jobs=1, trials=1)
    assert plan["status"] in {
        "blocked",
        "waiting_for_direct_mode_progress",
        "route_coverage_ready",
        "needs_backlog_or_research_subjects",
    }
    assert "no missing route cells" in " ".join(plan["blockers"]) or plan["missing_cell_count"] == 0


def test_exploration_backfill_schedule_gates(monkeypatch, tmp_path) -> None:
    plan = {
        "active_backfill_eligible": True,
        "planned_jobs": [
            {
                "target": "o/r#1",
                "repo": "o/r",
                "task_type": "implement",
                "agents": ["codex", "claude"],
                "exp_id_template": "backfill-o-r-1",
                "title": "T",
                "counts_when": "after evaluate",
            }
        ],
        "blockers": [],
    }
    assert "requires" in eb.schedule_backfill(plan, confirm=False, env={})["error"]
    assert eb.schedule_backfill(
        {**plan, "active_backfill_eligible": False},
        confirm=True,
        env={eb.ENV_FLAG: "1"},
    )["blocked"]
    assert eb.schedule_backfill(plan, target="missing", confirm=True, env={eb.ENV_FLAG: "1"})[
        "blocked"
    ]
    assert eb.schedule_backfill(
        plan,
        agents=["codex"],
        confirm=True,
        env={eb.ENV_FLAG: "1"},
    )["blocked"]

    monkeypatch.setattr(eb.claims, "claim", lambda *a, **k: False)
    assert eb.schedule_backfill(plan, confirm=True, env={eb.ENV_FLAG: "1"})["reason"] == (
        "target already claimed"
    )

    releases: list = []
    monkeypatch.setattr(eb.claims, "claim", lambda *a, **k: True)
    monkeypatch.setattr(eb.claims, "release", lambda *a, **k: releases.append(a))
    monkeypatch.setattr(
        eb.exploration_evidence_plan,
        "_backlog_items",
        lambda **k: [{"target": "o/r#1", "title": "", "body": ""}],
    )
    out = eb.schedule_backfill(plan, confirm=True, env={eb.ENV_FLAG: "1"})
    assert out["blocked"] and "missing issue" in out["reason"]
    assert releases

    monkeypatch.setattr(
        eb.exploration_evidence_plan,
        "_backlog_items",
        lambda **k: [{"target": "o/r#1", "title": "T", "body": "do it", "task_type": "implement"}],
    )

    def prepare(*a, **k):
        raise RuntimeError("launch failed")

    out2 = eb.schedule_backfill(plan, confirm=True, env={eb.ENV_FLAG: "1"}, prepare_fn=prepare)
    assert out2["blocked"] and "failed to launch" in out2["reason"]


def test_exploration_backfill_schedule_success_and_main(monkeypatch, tmp_path, capsys) -> None:
    plan = {
        "active_backfill_eligible": True,
        "planned_jobs": [
            {
                "target": "o/r#1",
                "repo": "o/r",
                "task_type": "implement",
                "agents": ["codex", "claude"],
                "exp_id_template": "backfill-o-r-1",
                "title": "T",
                "profiles": None,
                "counts_when": "after evaluate",
            }
        ],
        "blockers": [],
        "status": "ready_to_schedule_backfill",
        "missing_cell_count": 1,
        "next_action": "run",
        "direct_mode_progress": {"outcome_exploration_runs": 1, "progressing": True},
    }
    exp_dir = tmp_path / "exp"
    exp_dir.mkdir()
    (exp_dir / "meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(eb.claims, "claim", lambda *a, **k: True)
    monkeypatch.setattr(eb.claims, "release", lambda *a, **k: None)
    monkeypatch.setattr(
        eb.exploration_evidence_plan,
        "_backlog_items",
        lambda **k: [{"target": "o/r#1", "title": "T", "body": "body", "task_type": "implement"}],
    )
    monkeypatch.setattr(eb.exp_abcd, "exp_paths", lambda exp_id: exp_dir)
    monkeypatch.setattr(eb.exp_abcd, "research_v2_arms", lambda agents, profiles: agents)

    def prepare(repo, spec, exp_id, arms, task_type):
        return {"ok": True, "exp_id": exp_id}

    out = eb.schedule_backfill(plan, confirm=True, env={eb.ENV_FLAG: "1"}, prepare_fn=prepare)
    assert out["active"] is True
    meta = json.loads((exp_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["backfill_target"] == "o/r#1"

    monkeypatch.setattr(eb, "build_plan", lambda **k: plan)
    monkeypatch.setattr(eb, "schedule_backfill", lambda *a, **k: {"active": True})
    assert eb.main(["--apply", "--confirm-backfill", "--json"]) == 0
    assert "active" in capsys.readouterr().out

    monkeypatch.setattr(eb, "_selftest", lambda: None)
    assert eb.main(["--selftest"]) == 0
