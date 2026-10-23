"""Hermetic coverage for issue #843's remaining ranked targets.

Order (repo ranking): durability_sweep, router, feature_scan,
capability_effectiveness, redirect_shadow, strategy_experiment.
range_lane_rollout was covered in an earlier commit on this PR.
Every subprocess/gh/network call is stubbed; ORCH_* state is temp-isolated.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import capability_effectiveness as effectiveness
import durability_sweep as sweep
import feature_scan
import feedback
import redirect_shadow as shadow
import router
import strategy_experiment as strategy


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "brain.db"))
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(tmp_path / "capabilities.json"))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    # router._selftest assigns this module attribute directly. Register its undo
    # before invoking it so later test modules retain the real dynamic accessor.
    monkeypatch.setattr(router.claims, "_handoff_dir", lambda: tmp_path / "handoff")
    attempted: list[object] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError(f"issue843 test started an external process/network: {args!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    yield
    assert attempted == [], attempted


# ----- durability_sweep (3) -------------------------------------------------


def test_canned_gh_stub_rejects_unexpected_commands(monkeypatch):
    """Lines 2218/2382: the selftest canned-gh stubs must fail loudly on surprise argv."""
    hits: list[str] = []
    real_classify = sweep.classify_durability
    real_run_json = sweep._run_json

    def probing_classify(*args, **kwargs):
        # Only probe while classify_with_canned_gh has swapped in its fake.
        if sweep._run_json is not real_run_json:
            with pytest.raises(AssertionError, match="unexpected gh call"):
                sweep._run_json(["gh", "unexpected", "shape"])
            hits.append("live")
        return real_classify(*args, **kwargs)

    monkeypatch.setattr(sweep, "classify_durability", probing_classify)
    sweep._selftest_live_revert_scan(int(time.time()))
    assert hits, "canned-gh unexpected-call guard was never exercised"

    hits.clear()
    real_sweep = sweep.sweep_durability

    def probing_sweep(*args, **kwargs):
        if sweep._run_json is not real_run_json:
            with pytest.raises(AssertionError, match="unexpected gh call"):
                sweep._run_json(["gh", "unexpected", "shape"])
            hits.append("cached")
        return real_sweep(*args, **kwargs)

    monkeypatch.setattr(sweep, "sweep_durability", probing_sweep)
    sweep._selftest_revert_search_cached_per_repo(int(time.time()))
    assert hits, "per-repo canned-gh unexpected-call guard was never exercised"


def test_selftest_pops_absent_orch_state_dir(monkeypatch, tmp_path):
    """Line 2707: when ORCH_STATE_DIR was unset on entry, restore must pop it."""
    monkeypatch.delenv("ORCH_STATE_DIR", raising=False)
    # Keep the selftest off the live ledger / brain paths.
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "selftest-brain.db")
    sweep._selftest()
    assert "ORCH_STATE_DIR" not in os.environ


# ----- router (54) ----------------------------------------------------------


def _scored(agent: str, state: str = "ok", idx: int = 0, score=None):
    entry = {"agent": agent, "late": False, "mode": "remote"}
    tier = score if score is not None else (0, 0, 0 if state == "ok" else 1, 0, 0.0, idx)
    return (tier, entry, state, idx)


def test_router_helpers_cover_edge_returns():
    assert router._drain_urgency("codex", {"agents": {}}) == 0.0
    assert (
        router._drain_urgency(
            "codex",
            {
                "agents": {
                    "codex": {
                        "soft_units_5h": 0,
                        "estimated_units_5h": 0,
                        "minutes_to_window_refresh": 30,
                    }
                }
            },
        )
        == 0.0
    )
    assert (
        router._drain_urgency(
            "codex",
            {
                "agents": {
                    "codex": {
                        "soft_units_5h": 100,
                        "estimated_units_5h": 10,
                        "minutes_to_window_refresh": -1,
                    }
                }
            },
        )
        == 0.0
    )
    # Successful urgency path (not in CI miss list, but keeps the helper honest).
    urgency = router._drain_urgency(
        "codex",
        {
            "agents": {
                "codex": {
                    "soft_units_5h": 100,
                    "estimated_units_5h": 20,
                    "minutes_to_window_refresh": 60,
                }
            }
        },
    )
    assert urgency < 0

    assert router._learned_n_obs({"codex": 3}, "codex") == 0
    assert router._learned_n_obs(None, "codex") == 0
    post, score, n = router._learned_posterior_score(
        {"codex": {"posterior": "bad", "score": object(), "n_obs": "x"}},
        "codex",
        0.5,
    )
    assert post == pytest.approx(0.5) and score == pytest.approx(0.5) and n == 0
    post, score, n = router._learned_posterior_score(
        {"codex": {"posterior": None, "score": None, "n_obs": None}},
        "codex",
        0.4,
    )
    assert post == pytest.approx(0.4) and n == 0

    assert router.entry_agent(_scored("claude")) == "claude"
    assert router.entry_agent(("not-a-row",)) == "?"


def test_router_heartbeat_and_exploration_short_circuits(monkeypatch):
    import sys

    import capabilities as caps

    monkeypatch.setattr(
        caps, "daily_heartbeat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    router._capability_heartbeat("thompson-hybrid-routing", "invocation", ref="x")
    with patch.dict(sys.modules, {"capabilities": None}):
        router._capability_heartbeat("thompson-hybrid-routing", "invocation")

    assert router._thompson_exploration_choice([_scored("codex")], {}, random.Random(0)) is None
    alone = [_scored("codex"), _scored("claude", score=(0, 0, 0, 1, 0.0, 1))]
    # Different policy tier prefix → empty pool → None.
    alone[1] = ((1, 0, 0, 0, 0.0, 1), alone[1][1], "ok", 1)
    assert router._thompson_exploration_choice(alone, {}, random.Random(0)) is None

    assert router._exploration_choice([_scored("codex")], {}, random.Random(0)) is None
    assert router._exploration_choice(alone, {}, random.Random(0)) is None


def test_router_pacing_choice_edge_branches(monkeypatch):
    rng = random.Random(1)
    # Winner not in PACING_POOLS → early return of scored[0].
    scored = [_scored("gemini"), _scored("codex", idx=1)]
    picked, decision = router._pacing_choice(scored, {"agents": {}}, {}, rng)
    assert picked[1]["agent"] == "gemini" and decision is None

    # Line 561: winner is a pacing-pool agent, but after tier filter the pool list is empty.
    # Shrink PACING_POOLS so the winner qualifies at entry, then no eligible row remains in-pool.
    monkeypatch.setattr(router, "PACING_POOLS", {"codex", "ghost"})
    scored = [
        ((0, 0, 0, 0, 0.0, 0), {"agent": "codex", "late": False}, "shed", 0),
        ((0, 0, 0, 0, 0.0, 1), {"agent": "gemini", "late": False}, "ok", 1),
    ]
    cap = {
        "agents": {
            "codex": {"pacing": {"state": "shed", "routing_weight": 0.0}, "state": "shed"},
            "gemini": {"pacing": {"state": "ok", "routing_weight": 1.0}, "state": "ok"},
        }
    }
    # With non_shed = [gemini] only, best tier is gemini's; pools among that tier is empty.
    picked, decision = router._pacing_choice(scored, cap, {}, rng)
    assert decision is None and picked[1]["agent"] == "gemini"

    scored = [
        ((0, 0, 0, 0, 0.0, 0), {"agent": "codex", "late": False}, "ok", 0),
        ((0, 0, 0, 0, 0.0, 1), {"agent": "gemini", "late": False}, "ok", 1),
    ]
    monkeypatch.setattr(router, "PACING_POOLS", {"codex", "claude"})
    cap = {"agents": {"codex": {"pacing": {}}, "gemini": {"pacing": {}}}}
    picked, decision = router._pacing_choice(scored, cap, {}, rng)
    # Unmeasured pacing on the pool → return eligible[0], None (line 569).
    assert decision is None

    # Measured pacing with zero posterior mass → weight rewrite (line 573).
    cap = {
        "agents": {
            "codex": {
                "pacing": {"state": "ok", "routing_weight": 1.0},
                "state": "ok",
            },
            "claude": {
                "pacing": {"state": "ok", "routing_weight": 1.0},
                "state": "ok",
            },
        }
    }
    scored = [
        ((0, 0, 0, 0, 0.0, 0), {"agent": "codex", "late": False}, "ok", 0),
        ((0, 0, 0, 0, 0.0, 1), {"agent": "claude", "late": False}, "ok", 1),
    ]
    learned = {
        "codex": {"posterior": 0.0, "score": 0.0, "n_obs": 1, "rank": 0},
        "claude": {"posterior": 0.0, "score": 0.0, "n_obs": 1, "rank": 1},
    }
    # _learned_posterior_score clamps posterior to >= 0.001, so craft via monkeypatch below.
    with patch.object(router, "_learned_posterior_score", return_value=(0.0, 0.0, 1)):
        picked, decision = router._pacing_choice(scored, cap, learned, rng)
    assert decision is not None
    assert decision["policy"] == "posterior*pacing"


def test_router_select_unknown_task_and_profile_miss(monkeypatch):
    assert router.select_agent("not-a-real-task", {"agents": {}}) is None

    # Profile envelope with empty selected_profile_id → None (line 856).
    cap = {"agents": {"codex": {"state": "ok", "pacing": {"state": "ok", "routing_weight": 1.0}}}}
    monkeypatch.setattr(
        router,
        "_route_profiles",
        lambda entry, spec, transport: [
            {
                "profile_id": "p1",
                "agent": "codex",
                "requested_model": "m",
                "reasoning_effort": "x",
                "permission_mode": "default",
                "prior_offset": 0.0,
            }
        ],
    )
    monkeypatch.setattr(
        router.execution_profiles,
        "select_profile",
        lambda *a, **k: {
            "selected_profile_id": "",
            "candidate_profile_ids": ["p1"],
            "policy_version": "t",
            "assignment_probability": 0.0,
            "rng_seed": 0,
        },
    )
    monkeypatch.setattr(
        router.execution_profiles,
        "get_profile",
        lambda pid: {
            "profile_id": pid,
            "agent": "codex",
            "requested_model": "m",
            "reasoning_effort": "x",
            "permission_mode": "default",
        },
    )
    monkeypatch.setattr(
        router.execution_profiles,
        "codex_operator_tier_override",
        lambda mode: False,
    )
    monkeypatch.setattr(
        router.execution_profiles,
        "default_codex_profile",
        lambda task_type, mode: "p1",
    )
    assert router.select_agent("implement", cap, simulate=True, profile_seed=1) is None


def test_router_learned_ranks_and_plan_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(
        router.feedback,
        "current_weights",
        lambda tt: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert router.learned_ranks() is None

    monkeypatch.setattr(
        router.feedback,
        "current_weights",
        lambda tt: [{"agent": "codex", "n_obs": 2, "posterior": 0.7, "score": 0.8}],
    )
    ranks = router.learned_ranks()
    assert ranks["implement"]["codex"]["rank"] == 0

    import capabilities as caps

    monkeypatch.setattr(
        caps, "load", lambda create=False: (_ for _ in ()).throw(ValueError("bad ledger"))
    )
    decision = router.plan(
        [{"target": "o/r#1", "task_type": "implement", "lane": "opener"}],
        {"agents": {"codex": {"state": "ok"}}},
        dry_run=True,
        max_concurrent=1,
        capability_records=None,
    )
    assert "assignments" in decision

    # Claim race lost (lines 1223-1234).
    monkeypatch.setattr(router.claims, "reap_stale", lambda: [])
    monkeypatch.setattr(router.claims, "active_claims", lambda: {})
    monkeypatch.setattr(router.claims, "claim", lambda *a, **k: False)
    monkeypatch.setattr(router.claims, "holder", lambda tgt: {"agent": "claude"})
    monkeypatch.setattr(
        router,
        "select_agent",
        lambda *a, **k: {
            "agent": "codex",
            "state": "ok",
            "capacity_policy": "",
            "mode": "remote",
        },
    )
    decision = router.plan(
        [{"target": "o/r#9", "task_type": "implement", "lane": "opener"}],
        {"agents": {"codex": {"state": "ok"}}},
        dry_run=False,
        max_concurrent=1,
        capability_records={},
    )
    assert any(r.get("reason") == "claim_race_lost" for r in decision.get("rejections", []))


def test_router_main_cli_paths(monkeypatch, tmp_path, capsys):
    # Only test CLI dispatch here; the real selftest restoration is tested below.
    called: list[bool] = []
    monkeypatch.setattr(router, "_selftest", lambda: called.append(True))
    assert router.main(["--selftest"]) == 0
    assert called == [True]

    monkeypatch.setattr(router, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(
        router,
        "recommend_receiver",
        lambda task_type, allowed=(): {"agent": "claude", "task_type": task_type},
    )
    assert router.main(["--recommend-receiver", "implement", "--allowed", "codex,claude"]) == 0
    assert json.loads(capsys.readouterr().out)["agent"] == "claude"

    monkeypatch.setattr(router, "load_capacity", lambda: {"agents": {}})
    monkeypatch.setattr(router, "load_backlog", lambda: [])
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    monkeypatch.setattr(
        router,
        "plan",
        lambda *a, **k: {"assignments": [], "rejections": [], "reaped": []},
    )
    monkeypatch.setenv("ORCH_MAX_CONCURRENT", "not-int")
    assert router.main(["--dry-run"]) == 0
    assert "assignments" in json.loads(capsys.readouterr().out)

    handoff = tmp_path / "handoff-write"
    monkeypatch.setattr(router, "HANDOFF", handoff)
    monkeypatch.setattr(router, "DECISION_JSON", handoff / "routing-decision.json")
    assert router.main([]) == 0
    assert (handoff / "routing-decision.json").exists()


def test_router_selftest_restores_preset_exploration_env(monkeypatch, tmp_path):
    """Lines 1973/1977: preset ORCH_EXPLORATION_* must be restored, not popped."""
    monkeypatch.setenv("ORCH_EXPLORATION_RATE", "0.33")
    monkeypatch.setenv("ORCH_EXPLORATION_MODE", "thompson-hybrid")
    monkeypatch.delenv("HANDOFF_DIR", raising=False)
    monkeypatch.setattr(router, "_capability_heartbeat", lambda *a, **k: None)
    # Skip pre-capture probes that would otherwise spawn model CLIs.
    monkeypatch.setattr(router, "_selftest_gemini_policy", lambda: None)
    monkeypatch.setattr(router.execution_profiles, "_selftest", lambda: None)
    # Abort once inside the try body so the finally restore still runs.
    monkeypatch.setattr(
        router,
        "plan",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("abort-selftest-body")),
    )
    with pytest.raises(RuntimeError, match="abort-selftest-body"):
        router._selftest()
    assert os.environ.get("ORCH_EXPLORATION_RATE") == "0.33"
    assert os.environ.get("ORCH_EXPLORATION_MODE") == "thompson-hybrid"


# ----- feature_scan (13) ----------------------------------------------------


def test_feature_scan_heartbeat_errors_and_unparseable(monkeypatch, tmp_path):
    import capabilities as caps

    monkeypatch.setattr(
        caps,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hb")),
    )
    feature_scan._capability_heartbeat()  # must not raise

    bad = tmp_path / "broken.py"
    bad.write_text("def oops(:\n", encoding="utf-8")
    ok, why = feature_scan.is_reusable_structure(bad)
    assert ok is False and "unparseable" in why

    missing = tmp_path / "nope.py"
    ok, why = feature_scan.is_reusable_structure(missing)
    assert ok is False and "unparseable" in why


def test_feature_scan_apply_and_format_unlogged(monkeypatch, tmp_path):
    reg = tmp_path / "features.json"
    reg.write_text("{}", encoding="utf-8")
    root = tmp_path / "mods"
    root.mkdir()
    (root / "new_thing.py").write_text(
        '"""Reusable."""\ndef _selftest():\n    pass\n', encoding="utf-8"
    )
    rep = feature_scan.scan(path=reg, root=root)
    assert rep["unlogged_count"] == 1
    text = feature_scan.format_report(rep)
    assert "RULE OF THREE" in text and "new-thing" in text

    dry = feature_scan.apply_scan(rep, dry_run=True, path=reg)
    assert dry["dry_run"] is True and dry["recorded"] == ["new-thing"]
    assert feature_scan.scan(path=reg, root=root)["unlogged_count"] == 1

    applied = feature_scan.apply_scan(rep, dry_run=False, path=reg)
    assert applied["dry_run"] is False and "new-thing" in applied["recorded"]

    # Error path in apply_scan (lines 137-138).
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(feature_scan.features, "record_use", boom)
    err = feature_scan.apply_scan(
        {"unlogged": [{"name": "x", "module": "x.py"}]}, dry_run=False, path=None
    )
    assert any("ERROR" in row for row in err["recorded"])

    # record_use without path kw (line 135).
    calls: list[dict] = []

    def capture(name, source, **kwargs):
        calls.append({"name": name, **kwargs})

    monkeypatch.setattr(feature_scan.features, "record_use", capture)
    feature_scan.apply_scan(
        {"unlogged": [{"name": "y", "module": "y.py"}]}, dry_run=False, path=None
    )
    assert calls and "path" not in calls[0]


# ----- capability_effectiveness (7) -----------------------------------------


def test_effectiveness_lift_and_main_cli(monkeypatch, tmp_path, capsys):
    import capabilities as caps

    ledger = tmp_path / "capabilities.json"
    row = caps._blank_capability("role-triage")
    row["status"] = "shadow"
    caps.save({"role-triage": row}, ledger)

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    # Enough terminal durable samples on both arms for lift (lines 434-435).
    edges = []
    for i in range(5):
        edges.append(
            {
                "capability_id": "role-triage",
                "accepted": 1,
                "counterfactual": 0,
                "outcome_verdict": "PASS",
                "durability": "durable",
                "failure_class": None,
                "target_run_id": f"run-a-{i}",
                "merged": 1,
                "subject_key": f"s-a-{i}",
            }
        )
    for i in range(5):
        edges.append(
            {
                "capability_id": "role-triage",
                "accepted": 0,
                "counterfactual": 1,
                "outcome_verdict": "FAIL",
                "durability": "broke_later",
                "failure_class": None,
                "target_run_id": f"run-c-{i}",
                "merged": 1,
                "subject_key": f"s-c-{i}",
            }
        )

    monkeypatch.setattr(effectiveness, "_edge_rows", lambda conn=None: edges)
    monkeypatch.setattr(caps, "usage_rate", lambda _c: {"invocations_per_week": 1.0})
    # _arm_stats needs real shape — call measure with a FakeConn that returns empty for SQL
    # and rely on patched _edge_rows.

    class FakeConn:
        def execute(self, *_a, **_k):
            return _Cursor([])

        def close(self):
            pass

    # Patch _arm_stats to return measured rates so lift computes.
    def fake_arm_stats(arm_edges):
        if not arm_edges:
            return {
                "durable_rate": None,
                "status": "no_edges",
                "verdict": {"rate": None},
            }
        durable = sum(1 for e in arm_edges if e.get("durability") == "durable")
        rate = durable / len(arm_edges)
        return {
            "durable_rate": rate,
            "status": "measured",
            "verdict": {"rate": rate},
            "n": len(arm_edges),
        }

    monkeypatch.setattr(effectiveness, "_arm_stats", fake_arm_stats)
    rep = effectiveness.measure(path=ledger, conn=FakeConn())
    row_out = next(r for r in rep["rows"] if r["capability_id"] == "role-triage")
    assert row_out["lift"] == pytest.approx(1.0)
    assert row_out["verdict"] == "helps"

    # main() without --selftest (lines 948-950); FakeConn.close covers selftest-only lines
    # indirectly when we invoke main with stubs.
    monkeypatch.setattr(effectiveness, "measure", lambda **k: rep)
    monkeypatch.setattr(effectiveness, "format_report", lambda r: "effectiveness-report\n")
    assert effectiveness.main([]) == 0
    assert "effectiveness-report" in capsys.readouterr().out
    assert effectiveness.main(["--json"]) == 0
    assert "role-triage" in capsys.readouterr().out

    # Lines 910/925 are FakeConn.close/EmptyConn.close inside _selftest. measure() only
    # closes connections it opened itself (`close = conn is None`), so those methods are
    # never called while the code is correct — noted as uncoverable without a prod change.


def test_effectiveness_closes_only_connections_it_opened(monkeypatch):
    """AC pin for capability_effectiveness 910/925: caller-supplied conns stay open.

    `_edge_rows` sets `close = conn is None` and only then calls `c.close()`. The selftest's
    FakeConn.close / EmptyConn.close therefore never run while that contract holds.
    """
    owned_closed: list[str] = []
    borrowed_closed: list[str] = []

    class _Cursor:
        def fetchall(self):
            return []

    class OwnedConn:
        def execute(self, *_a, **_k):
            return _Cursor()

        def close(self):
            owned_closed.append("owned")

    class BorrowedConn:
        def execute(self, *_a, **_k):
            return _Cursor()

        def close(self):
            borrowed_closed.append("borrowed")

    monkeypatch.setattr(feedback, "_conn", OwnedConn)
    assert effectiveness._edge_rows(conn=None) == []
    assert owned_closed == ["owned"]
    assert borrowed_closed == []

    assert effectiveness._edge_rows(conn=BorrowedConn()) == []
    assert borrowed_closed == [], "caller-supplied conn must not be closed"
    assert owned_closed == ["owned"]


# ----- redirect_shadow (1) --------------------------------------------------


def test_redirect_record_without_proposal_source_raises(monkeypatch, tmp_path):
    """CI's remaining redirect_shadow miss is the selftest else-assert (line 1383).

    That branch only runs when record_redirect incorrectly accepts a bare report — covering
    it requires breaking the production guard. The success/failure contract of the guard
    itself is what we pin here instead.
    """
    with pytest.raises(ValueError, match="--dispatch or --proposal-json"):
        shadow.record_redirect(
            {"run_id": "x", "target": "o/r#1"},
            "AC",
            corpus_path=tmp_path / "c.jsonl",
        )


def test_redirect_bare_report_rejection_keeps_else_assert_unreachable(tmp_path):
    """AC pin for redirect_shadow 1383: correct code never reaches the selftest else.

    The selftest's `else: raise AssertionError(...)` only fires if record_redirect accepts a
    bare report. Pin the raise path so a silent acceptance would fail this suite first.
    """
    report = {"run_id": "bare", "target": "owner/repo#9", "agent": "codex"}
    corpus = tmp_path / "unused-issue843.jsonl"
    with pytest.raises(ValueError, match="--dispatch or --proposal-json") as caught:
        shadow.record_redirect(report, "AC", corpus_path=corpus)
    assert "--dispatch or --proposal-json" in str(caught.value)
    assert not corpus.exists(), "rejection must not write a corpus event"


# ----- strategy_experiment (116) --------------------------------------------


def test_strategy_invalid_single_arm_raises_before_selftest_assert_false():
    """AC pin for strategy_experiment 1037: invalid single arm raises ValueError.

    The selftest's `assert False` after that call only runs if normalize_arm fails to raise.
    """
    with pytest.raises(ValueError, match="single strategy"):
        strategy.normalize_arm({"strategy": "single", "agents": ["claude", "cursor"]})


def test_strategy_coerce_and_normalize_arms_edges():
    assert strategy._coerce_agents("codex") == ["codex"]
    assert strategy._coerce_agents(["codex", "claude"]) == ["codex", "claude"]
    with pytest.raises(ValueError, match="invalid agents"):
        strategy._coerce_agents(3)
    with pytest.raises(ValueError, match="at least one agent"):
        strategy._coerce_agents(["", "  "])
    with pytest.raises(ValueError, match="duplicate agent"):
        strategy._coerce_agents(["codex", "codex"])

    arm = strategy.normalize_arm("claude", 0)
    assert arm["strategy"] == "single" and arm["agents"] == ["claude"]
    arm = strategy.normalize_arm({"parallel": ["codex", "cursor"], "synthesize": True}, 1)
    assert arm["strategy"] == "parallel" and arm["synthesize"] is True
    arm = strategy.normalize_arm({"single": ["vibe"]}, 2)
    assert arm["strategy"] == "single"
    arm = strategy.normalize_arm({"agents": ["a", "b"]}, 3)
    assert arm["strategy"] == "parallel"
    with pytest.raises(ValueError, match="lacks agents"):
        strategy.normalize_arm({"synthesize": True}, 0)
    with pytest.raises(ValueError, match="invalid strategy arm"):
        strategy.normalize_arm(12, 0)
    with pytest.raises(ValueError, match="unsupported strategy"):
        strategy.normalize_arm({"strategy": "cascade", "agents": ["a"]}, 0)
    with pytest.raises(ValueError, match="single strategy"):
        strategy.normalize_arm({"strategy": "single", "agents": ["a", "b"]}, 0)
    with pytest.raises(ValueError, match="parallel strategy"):
        strategy.normalize_arm({"strategy": "parallel", "agents": ["a"]}, 0)
    with pytest.raises(ValueError, match="pair strategy"):
        strategy.normalize_arm({"strategy": "pair", "agents": ["a"]}, 0)

    pair = strategy.normalize_arm({"strategy": "pair", "agents": ["codex", "vibe"]}, 0)
    assert pair["members"][0]["role"] == "implement"
    assert pair["members"][1]["role"] == "review"

    with pytest.raises(ValueError, match="exactly"):
        strategy.subject_arms("codex", "vibe", instances=2)
    with pytest.raises(ValueError, match="requires an implementation"):
        strategy.subject_arms("", "vibe")
    arms = strategy.subject_arms("codex", "vibe")
    assert len(arms) == 6


def test_strategy_shape_rate_and_open_pr_helpers():
    with pytest.raises(RuntimeError, match="malformed shape agent"):
        strategy._shape_rate({"agents": ["not-a-dict"]})  # truthy non-dict
    with pytest.raises(RuntimeError, match="malformed shape outcome"):
        strategy._shape_rate({"agents": {"codex": "x"}})
    with pytest.raises(RuntimeError, match="invalid broke-later"):
        strategy._shape_rate({"agents": {"codex": {"broke_later_rate": 2, "bad": 1, "durable": 3}}})
    with pytest.raises(RuntimeError, match="invalid shape outcome counts"):
        strategy._shape_rate(
            {"agents": {"codex": {"broke_later_rate": 0.5, "bad": -1, "durable": 3}}}
        )
    assert (
        strategy._shape_rate(
            {"agents": {"codex": {"broke_later_rate": 0.5, "bad": 1, "durable": 1}}}
        )
        is None
    )
    assert (
        strategy._shape_rate(
            {"agents": {"codex": {"broke_later_rate": 0.25, "bad": 1, "durable": 3}}}
        )
        == 0.25
    )
    assert (
        strategy._shape_rate(
            {"agents": {"codex": {"broke_later_rate": None, "bad": 1, "durable": 3}}}
        )
        is None
    )

    node = {
        "number": 3,
        "timelineItems": {
            "nodes": [
                {
                    "source": {
                        "__typename": "PullRequest",
                        "state": "OPEN",
                        "body": "see issue",
                        "headRefName": "x",
                        "repository": {"nameWithOwner": "other/r"},
                    }
                },
                {
                    "source": {
                        "__typename": "PullRequest",
                        "state": "OPEN",
                        "body": "Fixes #3",
                        "headRefName": "codex/issue-3",
                        "repository": {"nameWithOwner": "o/r"},
                    }
                },
            ]
        },
    }
    assert strategy._open_pr_links_issue(node, "o/r") is True
    assert strategy._open_pr_links_issue({"number": 1, "timelineItems": {"nodes": []}}) is False


def test_strategy_gh_subjects_success_and_pagination(monkeypatch):
    payload = {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "__typename": "PullRequest",
                            "state": "OPEN",
                            "body": "WIP",
                            "headRefName": "x",
                            "repository": {"nameWithOwner": "o/r"},
                        }
                    ],
                },
                "issues": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 2,
                            "body": "## Acceptance Criteria\n- pytest coverage test",
                            "title": "tests: add coverage",
                            "updatedAt": "2026-10-01T00:00:00Z",
                            "labels": {"nodes": [{"name": "test"}]},
                            "closedByPullRequestsReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [],
                            },
                            "timelineItems": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [],
                            },
                        }
                    ],
                },
            }
        }
    }

    def fake_run(args, **kwargs):
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = strategy._gh_subjects_for_shape({"repos": ["o/r"], "key": "tests"}, target="o/r#2")
    assert rows and rows[0]["target"] == "o/r#2"

    # Target filter skips other repos.
    assert strategy._gh_subjects_for_shape({"repos": ["a/b"]}, target="o/r#1") == []

    # Pagination incomplete → UNKNOWN.
    payload["data"]["repository"]["pullRequests"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        strategy._gh_subjects_for_shape({"repos": ["o/r"]})
    payload["data"]["repository"]["pullRequests"]["pageInfo"]["hasNextPage"] = False
    payload["data"]["repository"]["issues"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        strategy._gh_subjects_for_shape({"repos": ["o/r"]})
    payload["data"]["repository"]["issues"]["pageInfo"]["hasNextPage"] = False
    payload["data"]["repository"]["issues"]["nodes"][0]["closedByPullRequestsReferences"][
        "pageInfo"
    ]["hasNextPage"] = True
    with pytest.raises(RuntimeError, match="linkage pagination"):
        strategy._gh_subjects_for_shape({"repos": ["o/r"]})
    payload["data"]["repository"]["issues"]["nodes"][0]["closedByPullRequestsReferences"][
        "pageInfo"
    ]["hasNextPage"] = False
    payload["data"]["repository"]["issues"]["nodes"][0]["timelineItems"]["pageInfo"][
        "hasNextPage"
    ] = True
    with pytest.raises(RuntimeError, match="timeline pagination"):
        strategy._gh_subjects_for_shape({"repos": ["o/r"]})


def test_strategy_select_subject_and_freeze(tmp_path):
    shapes = tmp_path / "shapes.json"
    shapes.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fleet-shapes unavailable"):
        strategy.select_subject(shapes, issue_fetcher=lambda s: [])

    shapes.write_text(json.dumps({"shapes": [{"key": "x", "recurring": False}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no recurring shape"):
        strategy.select_subject(shapes, issue_fetcher=lambda s: [])

    shapes.write_text(
        json.dumps(
            {
                "generated_at": "t0",
                "shapes": [
                    {
                        "key": "tests|a|tests",
                        "recurring": True,
                        "prs": 2,
                        "commit_type": "tests",
                        "path_classes": ["tests"],
                        "agents": {"codex": {"broke_later_rate": 0.4, "bad": 2, "durable": 3}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    issue = {
        "target": "o/r#2",
        "repo": "o/r",
        "title": "tests: add coverage",
        "body": "## Acceptance Criteria\n- pytest coverage test",
    }
    got = strategy.select_subject(shapes, issue_fetcher=lambda s: [issue])
    assert got["shape"] == "tests|a|tests"
    with pytest.raises(RuntimeError, match="no open unlinked subject"):
        strategy.select_subject(shapes, issue_fetcher=lambda s: [])

    spec = strategy.freeze_subject_spec(issue, tmp_path / "edir")
    assert spec.read_text(encoding="utf-8") == issue["body"]


def test_strategy_normalize_load_read_format_and_prepare(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="at least two arms"):
        strategy.normalize_arms(["codex"])

    hyp_path = tmp_path / "hyp.json"
    hyp_path.write_text(
        json.dumps(
            [
                {
                    "id": "H9",
                    "arms": ["codex", {"strategy": "single", "agents": ["claude"]}],
                    "task_type": "implement",
                }
            ]
        ),
        encoding="utf-8",
    )
    hyp, arms = strategy.load_hypothesis_arms("H9", hyp_path)
    assert hyp["id"] == "H9" and len(arms) == 2
    with pytest.raises(ValueError, match="not found"):
        strategy.load_hypothesis_arms("Hx", hyp_path)
    hyp_path.write_text(json.dumps([{"id": "H9", "arms": "bad"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="arms must be a list"):
        strategy.load_hypothesis_arms("H9", hyp_path)

    assert strategy.read_arms_json('["codex", "claude"]') == ["codex", "claude"]
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(json.dumps({"arms": ["a", "b"]}), encoding="utf-8")
    assert strategy.read_arms_json(str(arms_file)) == ["a", "b"]
    with pytest.raises(ValueError, match="JSON list"):
        strategy.read_arms_json('{"no": 1}')

    spec = tmp_path / "spec.md"
    spec.write_text("body", encoding="utf-8")
    plan = strategy.build_strategy_plan(
        "o/r",
        str(spec),
        "exp-1",
        ["codex", {"strategy": "pair", "agents": ["codex", "vibe"]}],
        hypothesis="H9",
        exp_dir=tmp_path / "experiments",
    )
    text = strategy._format_plan(plan)
    assert "Strategy experiment plan: exp-1" in text and "hypothesis: H9" in text

    # prepare_subject_experiment happy + refusal paths.
    subject = {
        "target": "o/r#2",
        "repo": "o/r",
        "body": "body",
        "shape": "tests",
        "broke_later_rate": 0.4,
        "updated_at": "t",
        "shape_match_basis": "x",
        "shapes_generated_at": "t0",
    }
    edir = tmp_path / "experiments" / "exp-exists"
    edir.mkdir(parents=True)
    (edir / "strategy.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        strategy.prepare_subject_experiment(
            subject,
            exp_id="exp-exists",
            agent="codex",
            reviewer="vibe",
            exp_dir=tmp_path / "experiments",
        )

    with pytest.raises(RuntimeError, match="claim refused"):
        strategy.prepare_subject_experiment(
            subject,
            exp_id="exp-new",
            agent="codex",
            reviewer="vibe",
            exp_dir=tmp_path / "experiments",
            claim_fn=lambda *a, **k: False,
        )

    prepared_meta = {"prepared": {"launched": [{"pid": 11}, {"pid": 12}]}}
    updates: list[dict] = []

    def fake_prepare(plan, prepare_fn=None):
        meta_path = Path(plan["metadata_path"]).parent / "meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return prepared_meta

    monkeypatch.setattr(strategy, "prepare_strategy_experiment", fake_prepare)
    monkeypatch.setattr(
        strategy.claims,
        "update_metadata",
        lambda *a, **k: updates.append(k) or True,
    )
    out = strategy.prepare_subject_experiment(
        subject,
        exp_id="exp-new",
        agent="codex",
        reviewer="vibe",
        exp_dir=tmp_path / "experiments",
        claim_fn=lambda *a, **k: True,
        prepare_fn=lambda *a, **k: {"launched": []},
    )
    assert out == prepared_meta
    assert updates and updates[0].get("pids") == [11, 12]


def test_strategy_refresh_and_main_cli(monkeypatch, tmp_path, capsys):
    assert strategy.refresh_evaluation_result("missing", exp_dir=tmp_path) is None

    edir = tmp_path / "exp-r"
    edir.mkdir()
    (edir / "strategy.json").write_text("{}", encoding="utf-8")
    (edir / "eval-maps.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="evaluator identities"):
        strategy.refresh_evaluation_result("exp-r", exp_dir=tmp_path)

    (edir / "eval-maps.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    monkeypatch.setattr(
        strategy, "write_evaluation_result", lambda *a, **k: tmp_path / "receipt.json"
    )
    assert (
        strategy.refresh_evaluation_result("exp-r", exp_dir=tmp_path) == tmp_path / "receipt.json"
    )

    monkeypatch.setattr(strategy, "_capability_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(strategy, "_selftest", lambda: None)
    assert strategy.main(["--selftest"]) == 0

    assert strategy.main(["--refresh-result"]) == 2
    assert "requires --exp-id" in capsys.readouterr().err

    monkeypatch.setattr(strategy, "refresh_evaluation_result", lambda *a, **k: None)
    assert strategy.main(["--refresh-result", "--exp-id", "x"]) == 1

    monkeypatch.setattr(strategy, "refresh_evaluation_result", lambda *a, **k: tmp_path / "r.json")
    monkeypatch.setattr(strategy.exp_abcd, "EXP_DIR", tmp_path)
    (tmp_path / "x").mkdir(exist_ok=True)
    (tmp_path / "x" / "strategy-receipt.json").write_text(
        json.dumps({"acceptance_status": "completed"}), encoding="utf-8"
    )
    assert strategy.main(["--refresh-result", "--exp-id", "x", "--json"]) == 0

    assert strategy.main(["--subject", "o/r#1"]) == 2
    monkeypatch.setenv("ORCH_STRATEGY_EXPERIMENT", "0")
    assert (
        strategy.main(
            [
                "--subject",
                "o/r#1",
                "--exp-id",
                "e1",
                "--agent",
                "codex",
                "--reviewer",
                "vibe",
                "--prepare",
                "--confirm-strategy",
            ]
        )
        == 2
    )

    monkeypatch.setenv("ORCH_STRATEGY_EXPERIMENT", "1")
    monkeypatch.setattr(
        strategy,
        "select_subject",
        lambda *a, **k: {
            "target": "o/r#1",
            "repo": "o/r",
            "body": "b",
            "shape": "s",
            "broke_later_rate": 0.1,
        },
    )
    monkeypatch.setattr(
        strategy,
        "prepare_subject_experiment",
        lambda *a, **k: strategy.build_strategy_plan(
            "o/r",
            str(tmp_path / "spec.md"),
            "e1",
            ["codex", "claude"],
            exp_dir=tmp_path,
        ),
    )
    (tmp_path / "spec.md").write_text("spec", encoding="utf-8")
    assert (
        strategy.main(
            [
                "--subject",
                "o/r#1",
                "--exp-id",
                "e1",
                "--agent",
                "codex",
                "--reviewer",
                "vibe",
                "--prepare",
                "--confirm-strategy",
                "--json",
            ]
        )
        == 0
    )

    monkeypatch.setattr(
        strategy, "select_subject", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert (
        strategy.main(
            [
                "--subject",
                "o/r#1",
                "--exp-id",
                "e1",
                "--agent",
                "codex",
                "--reviewer",
                "vibe",
                "--prepare",
                "--confirm-strategy",
            ]
        )
        == 1
    )

    assert strategy.main([]) == 2  # missing repo/spec/exp
    assert strategy.main(["--repo", "o/r", "--spec-file", "s", "--exp-id", "e"]) == 2

    monkeypatch.setattr(
        strategy,
        "_build_plan_from_args",
        lambda args: strategy.build_strategy_plan(
            "o/r", str(tmp_path / "spec.md"), "e2", ["codex", "claude"], exp_dir=tmp_path
        ),
    )
    assert (
        strategy.main(
            [
                "--repo",
                "o/r",
                "--spec-file",
                str(tmp_path / "spec.md"),
                "--exp-id",
                "e2",
                "--arms-json",
                '["codex","claude"]',
                "--json",
            ]
        )
        == 0
    )

    # prepare without confirm flag
    assert (
        strategy.main(
            [
                "--repo",
                "o/r",
                "--spec-file",
                str(tmp_path / "spec.md"),
                "--exp-id",
                "e2",
                "--arms-json",
                '["codex","claude"]',
                "--prepare",
            ]
        )
        == 2
    )

    monkeypatch.setattr(
        strategy, "_build_plan_from_args", lambda args: (_ for _ in ()).throw(ValueError("bad"))
    )
    assert (
        strategy.main(
            [
                "--repo",
                "o/r",
                "--spec-file",
                str(tmp_path / "spec.md"),
                "--exp-id",
                "e2",
                "--arms-json",
                '["codex","claude"]',
            ]
        )
        == 1
    )


def test_strategy_remaining_ci_gaps(monkeypatch, tmp_path, capsys):
    """Cover the last CI-miss lines: open-pr continue, matches_shape, heartbeat, prepare text."""
    # Line 200: open PR skipped when typename/state don't match.
    assert (
        strategy._open_pr_links_issue(
            {
                "number": 1,
                "timelineItems": {
                    "nodes": [
                        {"source": {"__typename": "Issue", "state": "OPEN", "body": "Fixes #1"}},
                        {
                            "source": {
                                "__typename": "PullRequest",
                                "state": "CLOSED",
                                "body": "Fixes #1",
                            }
                        },
                    ]
                },
            }
        )
        is False
    )
    # Line 362: no conventional title and no inference → reject before label checks.
    assert not strategy._matches_shape(
        {
            "title": "chore: unrelated work",
            "body": "## Acceptance Criteria\n- pytest coverage test",
            "labels": [],
        },
        {
            "key": "tests",
            "commit_type": "tests",
            "path_classes": ["tests"],
        },
    )
    # Inferred fix-shape match via bug label (past line 362).
    assert strategy._matches_shape(
        {
            "title": "failure in parser",
            "body": "## Acceptance Criteria\n- must implement the module function",
            "labels": ["bug"],
        },
        {
            "key": "fix|code",
            "commit_type": "fix",
            "path_classes": ["code"],
        },
    )
    # Line 366: paused / human labels reject.
    assert not strategy._matches_shape(
        {
            "title": "tests: x",
            "body": "## Acceptance Criteria\n- pytest test",
            "labels": ["agents:paused"],
        },
        {
            "key": "tests",
            "commit_type": "tests",
            "path_classes": ["tests"],
        },
    )
    # Line 1037: invalid single arm in selftest try/except — call normalize directly already
    # covered; invoke the selftest fragment that asserts False.
    with pytest.raises(ValueError, match="single strategy"):
        strategy.normalize_arm({"strategy": "single", "agents": ["claude", "cursor"]})

    # Lines 1166-1167: heartbeat swallows import/runtime errors.
    import capabilities as caps

    monkeypatch.setattr(
        caps,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hb")),
    )
    strategy._capability_heartbeat()
    # Also the ImportError path.
    import sys

    monkeypatch.setitem(sys.modules, "capabilities", None)
    strategy._capability_heartbeat()
    # Restore for later tests in this module.
    sys.modules["capabilities"] = caps

    # Lines 1243/1247: prepare success + non-json format path.
    monkeypatch.setattr(strategy, "_capability_heartbeat", lambda *a, **k: None)
    spec = tmp_path / "spec.md"
    spec.write_text("s", encoding="utf-8")
    plan = strategy.build_strategy_plan(
        "o/r", str(spec), "e3", ["codex", "claude"], exp_dir=tmp_path
    )
    monkeypatch.setattr(strategy, "_build_plan_from_args", lambda args: plan)
    monkeypatch.setattr(strategy, "prepare_strategy_experiment", lambda p: {**p, "prepared": True})
    monkeypatch.setenv("ORCH_STRATEGY_EXPERIMENT", "1")
    assert (
        strategy.main(
            [
                "--repo",
                "o/r",
                "--spec-file",
                str(spec),
                "--exp-id",
                "e3",
                "--arms-json",
                '["codex","claude"]',
                "--prepare",
                "--confirm-strategy",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Strategy experiment plan" in out or "prepared" in out


def test_strategy_build_plan_from_args(tmp_path, monkeypatch):
    hyp_path = tmp_path / "hyp.json"
    hyp_path.write_text(
        json.dumps([{"id": "H4", "arms": ["codex", "claude"], "task_type": "implement"}]),
        encoding="utf-8",
    )
    spec = tmp_path / "spec.md"
    spec.write_text("s", encoding="utf-8")
    args = strategy.parse_args(
        [
            "--repo",
            "o/r",
            "--spec-file",
            str(spec),
            "--exp-id",
            "e",
            "--hypothesis",
            "H4",
            "--hypotheses-path",
            str(hyp_path),
        ]
    )
    plan = strategy._build_plan_from_args(args)
    assert plan["hypothesis"] == "H4"

    args = strategy.parse_args(
        [
            "--repo",
            "o/r",
            "--spec-file",
            str(spec),
            "--exp-id",
            "e",
            "--arms-json",
            '["codex","claude"]',
        ]
    )
    plan = strategy._build_plan_from_args(args)
    assert plan["hypothesis"] is None
