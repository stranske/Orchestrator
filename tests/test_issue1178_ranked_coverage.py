"""Hermetic coverage for issue #1178's ranked targets besides rail_exercise.

CI baseline is the combined run on main @ ddb2fba6 (2026-11-05). These tests execute
the statements that run listed as missing. Lines that only fire when a guard regresses
are left to the PR body instead of being padded.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import agent_auth_check
import capability_compiler
import capacity
import durability_sweep
import experiment_recovery
import exploration_evidence_plan
import feedback
import research_usage_guard
import synthesis_promotion
import verifier_evidence

NOW = 1_700_000_000


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep state, the ledger, and every process spawn inside this test's temp dir."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    handoff = tmp_path / "handoff"
    state.mkdir()
    runtime.mkdir()
    handoff.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(handoff))
    monkeypatch.delenv("ORCH_FEEDBACK_DB", raising=False)
    monkeypatch.delenv("ORCH_CAPABILITIES_PATH", raising=False)
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback.db")
    monkeypatch.setattr(capacity, "HANDOFF", handoff)
    monkeypatch.setattr(capacity, "LEDGER", handoff / "capacity-ledger.ndjson")
    monkeypatch.setattr(capacity, "OUT", handoff / "capacity.json")
    monkeypatch.setattr(capacity, "SHED_DIR", handoff / "capacity-shed")
    monkeypatch.setattr("capabilities.production_heartbeat", lambda *args, **kwargs: None)

    started: list[tuple] = []

    def forbid(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("tests must not start a process")

    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(subprocess, "call", forbid)
    yield
    assert started == [], f"a process was started: {started}"


def _quiet_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    monkeypatch.setattr(capacity, "_unresolvable_tier", lambda agent: None)
    monkeypatch.setattr(
        capacity,
        "_model_health",
        lambda agent, tier="full": {"model": "gemini-test", "resolvable": True},
    )
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)


def _ledger(rows: list[dict]) -> None:
    capacity.LEDGER.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class _RaisingConnection:
    def __enter__(self):
        raise sqlite3.OperationalError("offline")

    def __exit__(self, *args) -> bool:
        return False


def _raising_conn(*args, **kwargs) -> _RaisingConnection:
    return _RaisingConnection()


def _ready_review(**updates) -> dict:
    review = {
        "thresholds": {
            "min_recorded_exploration_outcomes_per_mode": 1,
            "min_task_observations": 1,
            "min_observed_agents": 1,
            "min_top_agent_observations": 1,
            "min_ready_tasks": 1,
            "max_zero_cell_rate": 0.5,
        },
        "recorded_exploration_evidence": {
            "ready_for_direct_comparison": True,
            "mode_counts": [],
        },
        "ready_task_count": 2,
        "task_count": 2,
        "zero_observation_cell_rate": 0.0,
        "tasks": [],
        "route_weights_version": 1,
        "current_default": "thompson-hybrid",
        "recommendation": "consider_thompson_hybrid_default",
        "status": "ok",
        "reason": "measured",
    }
    review.update(updates)
    return review


def _human_plan() -> dict:
    return {
        "stage": "stage_1_needs_backlog_or_research_targets",
        "next_action": "refresh backlog",
        "exploration_review": {
            "recommendation": "wait",
            "direct_ready": False,
            "route_ready": False,
        },
        "direct_mode_deficits": [],
        "route_coverage_deficits": {
            "ready_task_count": 0,
            "task_count": 1,
            "needed_ready_task_count": 1,
            "zero_observation_cell_rate": 0.0,
        },
        "candidate_task_types": [{"recommended": False, "task_type": "implement"}],
        "supervised_collection": {"windows": []},
        "readiness": {"blocks_to_stage_2_or_review": []},
    }


def _auth(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    payload = {
        "checked": False,
        "authenticated": True,
        "reason": "present",
        "strength": None,
    }
    payload.update(overrides)

    def health(agent, tier="full", refresh=False):
        return {"model": "pinned", "resolvable": True, "reason": "listed"}

    monkeypatch.setattr(agent_auth_check.adapters, "auth_health", lambda *a, **k: payload)
    monkeypatch.setattr(agent_auth_check.adapters, "model_health", health)


def _cred(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> Path:
    path = tmp_path / "seat.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setitem(agent_auth_check.CREDENTIAL_FILES, "vibe", (path, "MISTRAL_API_KEY"))
    return path


def _memory_guard() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    research_usage_guard.ensure_schema(conn)
    return conn


class _ClosingConnection:
    """Proxy whose close() can be observed. sqlite3.Connection.close is read-only."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _close_spy(monkeypatch: pytest.MonkeyPatch) -> list[_ClosingConnection]:
    opened: list[_ClosingConnection] = []
    real_conn = feedback._conn

    def wrapped(*args, **kwargs) -> _ClosingConnection:
        proxy = _ClosingConnection(real_conn(*args, **kwargs))
        opened.append(proxy)
        return proxy

    monkeypatch.setattr(feedback, "_conn", wrapped)
    return opened


def test_ccusage_returns_the_active_block(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed(
            json.dumps({"blocks": [{"isActive": False}, {"isActive": True, "totalTokens": 9}]})
        )

    monkeypatch.setattr(capacity.shutil, "which", lambda name: "/usr/bin/ccusage")
    monkeypatch.setattr(capacity.subprocess, "run", fake_run)
    block = capacity._ccusage_active_block()
    assert block["totalTokens"] == 9
    assert calls == [["/usr/bin/ccusage", "blocks", "--active", "-j"]]


def test_finite_number_rejects_an_integer_too_large_for_float() -> None:
    assert capacity._finite_number(10**10000) is None


def test_adapter_failures_leave_capacity_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError(name)

    monkeypatch.setitem(sys.modules, "adapters", Boom())
    assert capacity._model_health("gemini") is None
    assert capacity._unresolvable_tier("gemini") is None
    assert capacity._auth_health("gemini") is None
    assert capacity._tier_models("gemini") == {}
    assert capacity._tier_ceiling("gemini") is None
    assert capacity._gemini_model_health() is None


def test_count_model_sheds_warns_and_stays_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_capacity(monkeypatch)
    cfg = {"model": "count", "window": "daily", "limit": 10}
    _ledger([{"agent": "batch", "ts": NOW - 5, "count": 10}])
    state, reason, _meta = capacity.compute("batch", cfg, None)
    assert state == capacity.SHED and reason == "10/10 requests this daily"
    _ledger([{"agent": "batch", "ts": NOW - 5, "count": 8}])
    state, reason, _meta = capacity.compute("batch", cfg, None)
    assert state == capacity.WARN and reason == "8/10 requests this daily"
    _ledger([{"agent": "batch", "ts": NOW - 5, "count": 1}])
    state, reason, _meta = capacity.compute("batch", cfg, None)
    assert state == capacity.OK and reason == "1/10 requests this daily"


def test_dollar_model_sheds_warns_and_stays_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_capacity(monkeypatch)
    cfg = {"model": "dollar", "window": "daily", "limit_usd": 10}
    _ledger([{"agent": "spend", "ts": NOW - 5, "cost_usd": 10}])
    state, reason, _meta = capacity.compute("spend", cfg, None)
    assert state == capacity.SHED and reason == "$10.00/$10 this daily"
    _ledger([{"agent": "spend", "ts": NOW - 5, "cost_usd": 8}])
    state, reason, _meta = capacity.compute("spend", cfg, None)
    assert state == capacity.WARN and reason == "$8.00/$10 this daily"
    _ledger([{"agent": "spend", "ts": NOW - 5, "cost_usd": 1}])
    state, reason, _meta = capacity.compute("spend", cfg, None)
    assert state == capacity.OK and reason == "$1.00/$10 this daily"


def test_plan_near_cap_warns_before_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_capacity(monkeypatch)
    cfg = {
        "model": "plan_then_credit",
        "plan_window": "daily",
        "plan_limit": 10,
        "credit_window": "monthly",
        "credit_usd": 25.0,
    }
    _ledger([{"agent": "aider", "ts": NOW - 5, "count": 9}])
    state, reason, _meta = capacity.compute("aider", cfg, None)
    assert state == capacity.WARN
    assert reason == "plan 9/10 req this daily (near plan cap)"


def _windowed_rows() -> list:
    return [
        "",
        "not json",
        {"agent": "gemini", "ts": "nope", "task_type": "implement"},
        {"agent": "gemini", "ts": NOW + 50, "task_type": "implement"},
        {"agent": "gemini", "ts": NOW - 5, "event": "complete", "task_type": "implement"},
        {"agent": "other", "ts": NOW - 5, "task_type": "implement"},
        {"agent": "gemini", "ts": NOW - 5, "task_type": "implement"},
        {"agent": "gemini", "ts": NOW - 6, "task_type": "implement"},
    ]


def test_windowed_ledger_skips_and_nears_weekly_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_capacity(monkeypatch)
    capacity.LEDGER.write_text(
        "\n".join(row if isinstance(row, str) else json.dumps(row) for row in _windowed_rows())
        + "\n",
        encoding="utf-8",
    )
    cfg = {
        "model": "windowed_prepaid",
        "window_soft_cap": 20.0,
        "weekly_soft_cap": 10.0,
        "reserve_fraction": 0.0,
        "drain_minutes": 0,
    }
    state, reason, meta = capacity.compute("gemini", cfg, None)
    assert state == capacity.WARN
    assert "near weekly soft budget" in reason
    assert meta["next_action"].startswith("Prefer ok seats for marginal work")
    assert meta["used_weekly"] == 8.0


def test_windowed_prepaid_nears_five_hour_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_capacity(monkeypatch)
    _ledger(
        [
            {"agent": "gemini", "ts": NOW - 5, "task_type": "implement"},
            {"agent": "gemini", "ts": NOW - 6, "task_type": "implement"},
        ]
    )
    cfg = {
        "model": "windowed_prepaid",
        "window_soft_cap": 10.0,
        "weekly_soft_cap": 1000.0,
        "reserve_fraction": 0.0,
        "drain_minutes": 0,
    }
    state, reason, meta = capacity.compute("gemini", cfg, None)
    assert state == capacity.WARN
    assert "near 5h soft budget" in reason
    assert "~" in reason and "min to window refresh" in reason
    assert meta["next_action"].startswith("Prefer ok seats for marginal work")


def test_unknown_capacity_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_capacity(monkeypatch)
    state, reason, meta = capacity.compute("ghost", {"model": "vacuum"}, None)
    assert state == capacity.UNKNOWN
    assert reason == "no capacity model for agent"
    assert meta == {}


def test_experiment_pacing_reads_nested_state_and_pool() -> None:
    paced = capacity.experiment_pacing(
        {
            "agents": {"codex": {"pacing_state": {"state": "warn"}, "state": "ok"}},
            "pools": {"codex-subscription": {"state": "shed"}},
        }
    )
    assert paced["codex"] == "codex-subscription=shed"


def test_calibrate_codex_returns_both_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import pacing_usage

    monkeypatch.setattr(pacing_usage, "read_rows", lambda path: [])
    monkeypatch.setattr(pacing_usage, "calibrate", lambda *args, **kwargs: (True, "calibrated"))
    assert capacity.main(["calibrate-codex"]) == 0
    assert capsys.readouterr().out.strip() == "calibrated"
    monkeypatch.setattr(pacing_usage, "calibrate", lambda *args, **kwargs: (False, "no samples"))
    assert capacity.main(["calibrate-codex"]) == 1
    assert capsys.readouterr().out.strip() == "no samples"


def test_pick_agent_reports_when_nothing_is_dispatchable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import router

    monkeypatch.setattr(capacity, "build", lambda *args, **kwargs: {"agents": {}})
    monkeypatch.setattr(router, "select_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert capacity.main(["pick-agent", "--candidates", "nobody"]) == 1
    assert "unmeasured: no dispatchable candidate" in capsys.readouterr().err


def test_json_loader_rejects_invalid_and_non_object_payloads(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert exploration_evidence_plan._load_json_file(broken) is None
    listed = tmp_path / "list.json"
    listed.write_text("[1]", encoding="utf-8")
    assert exploration_evidence_plan._load_json_file(listed) is None


def test_capacity_snapshot_uses_a_file_or_an_empty_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps({"agents": {"codex": {"state": "ok"}}}), encoding="utf-8")
    assert exploration_evidence_plan._capacity_snapshot(path)["agents"]["codex"]["state"] == "ok"

    def explode(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(capacity, "build", explode)
    assert exploration_evidence_plan._capacity_snapshot(tmp_path / "missing.json") == {"agents": {}}


def test_outcome_query_failure_is_an_empty_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feedback, "_conn", _raising_conn)
    assert exploration_evidence_plan._outcomes_by_task(7) == {}


def test_route_weight_failure_counts_every_agent_as_unobserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exploration_evidence_plan.exploration_review,
        "_route_agents",
        lambda *args, **kwargs: ["codex"],
    )

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("offline")

    monkeypatch.setattr(feedback, "current_weights", explode)
    assert exploration_evidence_plan._route_agent_observations(
        "implement", version=1, route_table={}
    ) == {"codex": 0}


def test_low_risk_opener_outside_the_route_table_is_named() -> None:
    rows = exploration_evidence_plan._candidate_task_types(
        [{"lane": "opener", "task_type": "implement", "target": "owner/repo#1"}],
        {"implement": 2},
        {"tasks": []},
    )
    assert rows[0]["reason"] == "not in route table"
    assert rows[0]["recommended"] is False


def _planned(monkeypatch: pytest.MonkeyPatch, **review) -> dict:
    monkeypatch.setattr(
        exploration_evidence_plan.exploration_review,
        "build_report",
        lambda **kwargs: _ready_review(**review),
    )
    monkeypatch.setattr(feedback, "_conn", _raising_conn)
    return exploration_evidence_plan.build_plan(
        backlog_payload={"items": []},
        capacity_payload={"agents": {}},
        route_table={},
    )


def test_missing_zero_cell_rate_is_not_a_clean_table(monkeypatch: pytest.MonkeyPatch) -> None:
    def review(**kwargs):
        payload = _ready_review(
            current_default="thompson-hybrid",
            recommendation="consider_thompson_hybrid_default",
        )
        payload.pop("zero_observation_cell_rate")
        return payload

    monkeypatch.setattr(exploration_evidence_plan.exploration_review, "build_report", review)
    monkeypatch.setattr(feedback, "_conn", _raising_conn)
    plan = exploration_evidence_plan.build_plan(
        backlog_payload={"items": []},
        capacity_payload={"agents": {}},
        route_table={"implement": {}},
    )
    assert plan["exploration_review"]["route_ready"] is False
    assert plan["stage"] == "stage_1_needs_backlog_or_research_targets"


def test_thompson_default_review_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _planned(
        monkeypatch,
        current_default="thompson-hybrid",
        recommendation="consider_thompson_hybrid_default",
    )
    assert plan["stage"] == "stage_4_default_review_complete"
    assert "Thompson-hybrid" in plan["next_action"]


def test_epsilon_default_review_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _planned(
        monkeypatch,
        current_default="epsilon-greedy",
        recommendation="keep_epsilon_greedy",
    )
    assert plan["stage"] == "stage_4_default_review_complete"
    assert plan["next_action"].startswith("keep epsilon-greedy")


def test_ready_review_does_not_change_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _planned(
        monkeypatch,
        current_default="epsilon-greedy",
        recommendation="collect_more",
    )
    assert plan["stage"] == "stage_4_default_review_ready"
    assert plan["next_action"].startswith("run the default review")


def test_format_human_names_when_nothing_is_recommended() -> None:
    text = exploration_evidence_plan.format_human(_human_plan())
    assert "candidate task types: none currently recommended" in text


def test_main_prints_the_human_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(exploration_evidence_plan, "build_plan", lambda **kwargs: _human_plan())
    assert exploration_evidence_plan.main([]) == 0
    assert "none currently recommended" in capsys.readouterr().out


def test_spec_fetch_uses_the_issue_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "issue"]
        return _Completed(json.dumps({"title": "Title", "body": "Body"}))

    monkeypatch.setattr(experiment_recovery.subprocess, "run", fake_run)
    body = experiment_recovery._spec_for("owner/repo#12", cache={})
    assert body == "# Title\n\nBody"


def test_spec_fetch_falls_back_when_gh_output_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        experiment_recovery.subprocess,
        "run",
        lambda *args, **kwargs: _Completed("{", returncode=0),
    )
    body = experiment_recovery._spec_for("owner/repo#13", cache={})
    assert "spec unavailable" in body


def test_restore_ignores_an_unusable_arm_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experiment_recovery, "arm_diff", lambda *args, **kwargs: "diff\n")
    result = experiment_recovery.restore_experiment(
        {
            "exp_id": "tick-1-subj",
            "repo": "owner/repo",
            "agents": ["codex"],
            "target": "owner/repo#1",
            "task_type": "implement",
            "ts": "not-a-clock",
        },
        apply=True,
        exp_dir=tmp_path,
        gh_fn=lambda repo, number: "real spec",
    )
    assert result["applied"] is True
    log = next((tmp_path / "tick-1-subj").glob("*.log"))
    assert "recovered from branch" in log.read_text(encoding="utf-8")


def test_broken_model_tier_is_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cred(monkeypatch, tmp_path, "export MISTRAL_API_KEY=abc\n")
    _auth(monkeypatch, checked=True, authenticated=True, strength="validates", reason="ok")

    def health(agent, tier="full", refresh=False):
        return {
            "model": "gone",
            "resolvable": tier != "cheap",
            "reason": "gone is not advertised",
        }

    monkeypatch.setattr(agent_auth_check.adapters, "model_health", health)
    row = agent_auth_check.check("vibe")
    assert row["verdict"] == "BROKEN"
    assert row["detail"] == "model (cheap): gone is not advertised"


def test_validated_auth_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cred(monkeypatch, tmp_path, "export MISTRAL_API_KEY=abc\n")
    _auth(monkeypatch, checked=True, authenticated=True, strength="validates", reason="server ok")
    row = agent_auth_check.check("vibe")
    assert row["verdict"] == "OK"
    assert row["detail"] == "server ok"


def test_present_file_without_the_key_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cred(monkeypatch, tmp_path, "OTHER=1\n")
    _auth(monkeypatch)
    row = agent_auth_check.check("vibe")
    assert row["verdict"] == "BROKEN"
    assert "MISTRAL_API_KEY not set in" in row["detail"]


def test_file_only_credential_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cred(monkeypatch, tmp_path, "export MISTRAL_API_KEY=abc\n")
    _auth(monkeypatch)
    row = agent_auth_check.check("vibe")
    assert row["verdict"] == "CONFIGURED"
    assert "file check only" in row["detail"]
    assert row["auth_strength"] == "config"


def test_unreadable_credential_file_is_an_environment_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _cred(monkeypatch, tmp_path, "export MISTRAL_API_KEY=abc\n")
    _auth(monkeypatch)
    monkeypatch.setattr(agent_auth_check, "CRED_READ_ATTEMPTS", 1)
    real_read = Path.read_text

    def boom(self, *args, **kwargs):
        if self == path:
            raise OSError("busy")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    row = agent_auth_check.check("vibe")
    assert row["verdict"] == "UNKNOWN"
    assert "ENVIRONMENT fault" in row["detail"]


def test_render_names_a_file_that_lacks_its_key() -> None:
    text = agent_auth_check._render(
        [
            {
                "agent": "vibe",
                "verdict": "BROKEN",
                "detail": "key missing",
                "credential_file": {
                    "present": True,
                    "key_present": False,
                    "path": "/tmp/seat.env",
                    "expected_key": "MISTRAL_API_KEY",
                },
                "refresh_hint": None,
            }
        ]
    )
    assert "credential file lacks MISTRAL_API_KEY: /tmp/seat.env" in text


def test_selftest_read_failure_is_exercised_without_a_live_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_auth_check.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        agent_auth_check,
        "run",
        lambda agents=None: [
            {"agent": agent, "verdict": "UNKNOWN"} for agent in agent_auth_check.AGENTS
        ],
    )
    agent_auth_check._selftest()


def test_main_exit_follows_the_broken_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cred(monkeypatch, tmp_path, "export MISTRAL_API_KEY=abc\n")
    _auth(monkeypatch, checked=True, authenticated=False, reason="logged out")
    assert agent_auth_check.main(["--agent", "vibe", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["verdict"] == "BROKEN"
    _auth(monkeypatch, checked=True, authenticated=True, strength="validates", reason="ok")
    assert agent_auth_check.main(["--agent", "vibe"]) == 0
    assert "OK" in capsys.readouterr().out


def test_guard_schema_adds_columns_missing_from_an_older_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE research_usage_opportunities (
          opportunity_id TEXT PRIMARY KEY,
          ts INTEGER NOT NULL,
          exp_id TEXT NOT NULL,
          repo TEXT NOT NULL,
          subject TEXT NOT NULL,
          signature TEXT NOT NULL,
          decision TEXT NOT NULL,
          reason TEXT,
          estimated_prompt_bytes INTEGER NOT NULL,
          estimated_prompt_tokens INTEGER NOT NULL,
          evaluator_count INTEGER NOT NULL,
          is_manual INTEGER NOT NULL,
          is_missing_spec INTEGER NOT NULL,
          alerts_json TEXT,
          metadata_json TEXT
        )
        """)
    research_usage_guard.ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_usage_opportunities)")}
    assert {"evaluator_agents_json", "terminal_outcome", "completed_at"} <= columns


def test_observed_usage_survives_missing_experiment_files(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (experiment_id TEXT, agent TEXT, ts INTEGER, task_type TEXT)")
    for index in range(3):
        conn.execute(
            "INSERT INTO runs VALUES (?, 'codex', ?, 'review')",
            (f"tick-{index}-alpha", NOW - 10),
        )
    root = tmp_path / "experiments"
    root.mkdir()
    report = research_usage_guard.observe_recent_research_usage(
        conn=conn, window_days=7, now=NOW, experiment_dir=root
    )
    assert report["panel_count"] == 3
    assert any(
        alert["type"] == "observed_repeated_subject_panels" and alert["panel_count"] == 3
        for alert in report["alerts"]
    )


def test_guard_limit_parsers_reject_bad_numbers() -> None:
    with pytest.raises(ValueError, match="negative guard setting"):
        research_usage_guard._int_limit({"LIMIT": "-1"}, "LIMIT", 1)
    with pytest.raises(ValueError, match="invalid numeric guard setting"):
        research_usage_guard._float_limit({"LIMIT": "nope"}, "LIMIT", 0.2)
    with pytest.raises(ValueError, match="outside"):
        research_usage_guard._float_limit({"LIMIT": "2"}, "LIMIT", 0.2)


def _admit(**kwargs) -> dict:
    payload = {
        "exp_id": "tick-9-subject",
        "repo": "owner/repo",
        "subject": "subject",
        "spec_text": "a distinct spec",
        "base_sha": "abc",
        "now": NOW,
        "env": {"ORCH_RESEARCH_ARM": "0"},
    }
    payload.update(kwargs)
    return research_usage_guard.assess_and_record_opportunity(**payload)


def test_manual_bypass_closes_the_connection_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = _close_spy(monkeypatch)
    result = _admit(is_manual=True, env={})
    assert result["reason"] == "manual_override_bypass"
    assert closed and closed[0].closed


def test_disabled_research_arm_closes_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = _close_spy(monkeypatch)
    result = _admit()
    assert result["reason"] == "research_arm_disabled"
    assert closed and closed[0].closed


def test_invalid_guard_config_closes_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = _close_spy(monkeypatch)
    result = _admit(env={"ORCH_RESEARCH_ARM": "1", "ORCH_GUARD_MAX_EVAL_CALLS_24H": "-3"})
    assert result["decision"] == "blocked_by_anomaly"
    assert result["reason"] == "guard_config_invalid"
    assert closed and closed[0].closed


def _armed_env(**limits: str) -> dict:
    env = {
        "ORCH_RESEARCH_ARM": "1",
        "ORCH_GUARD_MAX_EVAL_CALLS_24H": "100",
        "ORCH_GUARD_MAX_EVAL_CALLS_1H": "100",
        "ORCH_GUARD_MAX_PROMPT_BYTES_24H": "100000000",
        "ORCH_GUARD_MAX_PROMPT_BYTES_1H": "100000000",
        "ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT": "100",
        "ORCH_GUARD_MAX_SUBJECT_SHARE_24H": "1",
    }
    env.update(limits)
    return env


def test_prompt_byte_budget_blocks_without_calling_it_an_anomaly() -> None:
    conn = _memory_guard()
    result = _admit(
        conn=conn,
        spec_text="x" * 80,
        env=_armed_env(ORCH_GUARD_MAX_PROMPT_BYTES_24H="1"),
    )
    assert result["decision"] == "blocked_by_limit"
    assert result["reason"] == "prompt_byte_limit_exceeded"


def test_prompt_byte_spike_is_an_anomaly() -> None:
    conn = _memory_guard()
    result = _admit(
        conn=conn,
        spec_text="y" * 80,
        env=_armed_env(ORCH_GUARD_MAX_PROMPT_BYTES_1H="1"),
    )
    assert result["decision"] == "blocked_by_anomaly"
    assert result["reason"] == "prompt_byte_spike"


def test_subject_share_spike_after_three_admissions() -> None:
    conn = _memory_guard()
    for index in range(3):
        research_usage_guard._save_opportunity(
            conn,
            NOW - 10,
            f"exp-{index}",
            "owner/repo",
            "same-subject",
            f"sig-{index}",
            "admitted",
            "admitted",
            10,
            1,
            1,
            [],
            False,
            False,
            [],
            True,
            False,
        )
    result = _admit(
        conn=conn,
        subject="same-subject",
        spec_text="fresh spec",
        env=_armed_env(
            ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT="0", ORCH_GUARD_MAX_SUBJECT_SHARE_24H="0.1"
        ),
    )
    assert result["decision"] == "blocked_by_anomaly"
    assert result["reason"] == "repeated_subject_share_spike"


def test_usage_report_ignores_corrupt_json_and_names_a_budget_block() -> None:
    conn = _memory_guard()
    research_usage_guard._save_opportunity(
        conn,
        NOW,
        "exp",
        "owner/repo",
        "subject",
        "sig",
        "blocked_by_limit",
        "prompt_byte_limit_exceeded",
        10,
        1,
        1,
        ["codex"],
        False,
        False,
        [{"type": "prompt_byte_limit_exceeded"}],
        False,
        False,
    )
    conn.execute(
        "UPDATE research_usage_opportunities SET evaluator_agents_json=?, alerts_json=?",
        ("not-json", "not-json"),
    )
    report = research_usage_guard.generate_usage_report(conn=conn, now=NOW, window_days=7)
    assert report["health_status"] == "BUDGET_BLOCKED"
    assert report["alerts_summary"] == {}
    assert report["evaluator_call_estimates"] == {}


def test_usage_report_closes_a_connection_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = _close_spy(monkeypatch)
    report = research_usage_guard.generate_usage_report(now=NOW, window_days=1)
    assert report["health_status"] == "OK"
    assert closed and closed[0].closed


def test_write_usage_report_defaults_to_the_state_dir() -> None:
    conn = _memory_guard()
    report = research_usage_guard.write_usage_report(conn=conn)
    path = Path(research_usage_guard.os.environ["ORCH_STATE_DIR"]) / "research-usage-report.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["health_status"] == report["health_status"]


def test_verifier_cursor_rejects_an_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    cursor = Path(str(db) + ".verifier-refresh.json")
    cursor.write_text(json.dumps({"after": "z" * 5000}), encoding="utf-8")
    _rows, path, problem = durability_sweep._verifier_refresh_batch([])
    assert path == cursor
    assert problem == "ValueError"


def test_verifier_cursor_rejects_a_non_string_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    Path(str(db) + ".verifier-refresh.json").write_text('{"after": 1}', encoding="utf-8")
    _rows, _path, problem = durability_sweep._verifier_refresh_batch([])
    assert problem == "ValueError"


def test_saving_a_cursor_without_a_path_is_a_no_op() -> None:
    assert durability_sweep._save_verifier_refresh_cursor(None, ("run",)) is None


def test_cursor_replace_and_cleanup_failures_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(*args, **kwargs):
        raise OSError("replace")

    def fail_unlink(self, *args, **kwargs):
        raise OSError("unlink")

    monkeypatch.setattr(durability_sweep.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    named = durability_sweep._save_verifier_refresh_cursor(
        tmp_path / "cursor.json", ("run-id", "owner/repo", 1)
    )
    assert named == "OSError"


def test_refresh_skips_an_unusable_decision_and_counts_a_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [("run-1", "owner/repo", 7)]
    monkeypatch.setattr(durability_sweep, "_missing_verifier_runs", lambda: rows)
    monkeypatch.setattr(
        durability_sweep,
        "_verifier_refresh_batch",
        lambda candidates: (list(candidates), None, None),
    )
    monkeypatch.setattr(verifier_evidence, "decision_matches_identity", lambda *a, **k: False)
    monkeypatch.setattr(verifier_evidence, "_valid_state", lambda *a, **k: False)
    monkeypatch.setattr(verifier_evidence, "is_unusable_observation", lambda *a, **k: False)

    def fetch(repo, numbers):
        return {number: {"head_sha": "abc", "evaluated_sha": "abc"} for number in numbers}

    skipped = durability_sweep.refresh_verifier_verdicts(dry_run=True, _fetch_fn=fetch)
    assert skipped["verifier_recorded"] == 0
    monkeypatch.setattr(verifier_evidence, "decision_matches_identity", lambda *a, **k: True)
    counted = durability_sweep.refresh_verifier_verdicts(dry_run=True, _fetch_fn=fetch)
    assert counted["verifier_recorded"] == 1


def test_merged_ci_green_fails_closed_on_a_bad_response() -> None:
    assert (
        durability_sweep._merged_ci_green(
            "owner/repo", "a" * 40, lambda args: ({"errors": ["x"]}, None)
        )
        is False
    )
    assert (
        durability_sweep._merged_ci_green("owner/repo", "a" * 40, lambda args: (None, "boom"))
        is False
    )


def test_non_dict_workflow_barrier_is_skipped() -> None:
    source = capability_compiler.reference_workflow_source()
    source["barriers"] = ["not-a-dict"]
    with pytest.raises(capability_compiler.WorkflowCompileError, match="must be an object"):
        capability_compiler.compile_workflow_rail(source)


def test_skill_frontmatter_without_a_name_is_unnamed(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\ndescription: only\n---\n", encoding="utf-8")
    assert capability_compiler._skill_frontmatter_name(skill) is None


def test_skill_package_content_hash_must_match(tmp_path: Path) -> None:
    skill = tmp_path / "pkg"
    packaged = skill / ".orchestrator"
    packaged.mkdir(parents=True)
    (skill / "notes.txt").write_text("hello", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A bounded skill\n---\nSee (notes.txt)\n",
        encoding="utf-8",
    )
    digest = capability_compiler._sha256_file(skill / "notes.txt")
    manifest = {
        "schema": capability_compiler.SKILL_MANIFEST_SCHEMA,
        "version": capability_compiler.SKILL_VERSION,
        "name": "demo",
        "resources": [{"target": "notes.txt", "content_hash": digest}],
        "content_hash": "0" * 64,
    }
    (packaged / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match="skill package content hash mismatch"):
        capability_compiler.validate_skill_package(skill)


def test_generated_role_manifest_rejects_loose_schemas() -> None:
    required = [
        "schema",
        "version",
        "manifest_id",
        "capability_id",
        "source_ir_ref",
        "source_fingerprint",
        "name",
        "description",
        "authority",
        "route_as",
        "input_schema",
        "output_schema",
        "selector",
        "capacity_policy",
        "prompt_protocol",
        "prompt_hash",
        "lifecycle",
        "shadow_only",
        "profile_agnostic",
        "generated_at",
    ]
    manifest = dict.fromkeys(required, "x")
    manifest["schema"] = capability_compiler.ROLE_MANIFEST_SCHEMA
    manifest["version"] = capability_compiler.ROLE_VERSION
    manifest["shadow_only"] = True
    manifest["profile_agnostic"] = True
    manifest["input_schema"] = {}
    manifest["output_schema"] = {}
    with pytest.raises(AssertionError, match="invalid strict schemas"):
        capability_compiler._validate_generated_role_manifest(manifest)


def test_duplicate_role_name_is_rejected() -> None:
    candidate = capability_compiler.reference_role_candidate(now=NOW)
    candidate.graph["role_contract"]["name"] = "redirect"
    decision = capability_compiler.compile_role_candidate(candidate, now=NOW)
    assert decision["status"] == "rejected"
    assert decision["target"] is None
    assert "duplicate existing role: redirect" in decision["rejection_reasons"]


def test_role_selector_equals_requires_a_string() -> None:
    candidate = capability_compiler.reference_role_candidate(now=NOW)
    candidate.graph["role_contract"]["selector"]["value"] = 1
    decision = capability_compiler.compile_role_candidate(candidate, now=NOW)
    assert "generated role selector equals value must be a string" in decision["rejection_reasons"]


def test_deterministic_role_pattern_routes_to_a_workflow() -> None:
    candidate = capability_compiler.reference_role_candidate(now=NOW)
    candidate.graph["decision_mode"] = "deterministic"
    decision = capability_compiler.compile_role_candidate(candidate, now=NOW)
    assert decision["status"] == "routed"
    assert decision["target"] == "workflow"
    assert "deterministic pattern: route to workflow" in decision["rejection_reasons"]


def test_live_reconcile_refuses_with_and_without_the_ship_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(synthesis_promotion, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(synthesis_promotion, "_followup_ship_gate_enabled", lambda: False)
    assert synthesis_promotion.main(["reconcile", str(tmp_path), "--live"]) == 2
    assert "ORCH_FOLLOWUP_SHIP_GATE" in capsys.readouterr().err
    monkeypatch.setattr(synthesis_promotion, "_followup_ship_gate_enabled", lambda: True)
    assert synthesis_promotion.main(["reconcile", str(tmp_path), "--live"]) == 2
    assert "sole launch and delivery controller" in capsys.readouterr().err
