"""Value-chain monitor accounting for `comparative-tilt` from Brain run records."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import capabilities
import capability_admission as admission
import feedback
import local_loop
import router
import value_chain_monitor as monitor

T0 = 1_795_800_000  # 2026-11-27
NOW = T0 + 60


def _tilted_decision(agent: str = "gemini") -> dict:
    return {
        "chosen": agent,
        "route_source": "route_weights",
        "policy": "posterior*pacing",
        "comparative_tilt": {
            "floor": 0.25,
            "shares": {"gemini": 1.0},
            "tier": [
                {"agent": "gemini", "base": 0.5, "tilted": 0.625},
                {"agent": "codex", "base": 0.5, "tilted": 0.125},
            ],
        },
    }


def _untilt_decision(agent: str = "codex") -> dict:
    return {
        "chosen": agent,
        "route_source": "route_weights",
        "policy": "posterior*pacing",
    }


@pytest.fixture
def tilt_env(monkeypatch, tmp_path):
    db_path = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(time, "time", lambda: float(T0))
    ledger = tmp_path / "capabilities.json"
    capabilities.load(ledger)
    return ledger, db_path


def _tilt_section(ledger, db_path, *, env: dict[str, str] | None = None) -> dict:
    inputs = monitor._brain(db_path, NOW - monitor.WINDOW_DAYS * 86400, NOW)
    return monitor.report(now=NOW, path=ledger, env=env or {}, inputs=inputs)


def _tilt_row(section: dict) -> dict:
    return next(r for r in section["rows"] if r["capability"] == "comparative-tilt")


def test_tilt_row_counts_the_chain_from_run_records(tilt_env):
    """3 local implement runs: one tilted+durable, one decided without tilt, one caller_pinned."""
    ledger, db_path = tilt_env

    # 1. Local implement run with a tilted route_decision and durable outcome.
    feedback.record_run(
        "local:o/r#1:gemini",
        "o/r#1",
        "implement",
        "gemini",
        mode="local",
        pr_number=1,
        routing_metadata={"caller_pinned": True, "route_decision": _tilted_decision("gemini")},
    )
    feedback.record_outcome(
        "local:o/r#1:gemini",
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
    )

    # 2. Local implement run with a route_decision but no comparative_tilt.
    feedback.record_run(
        "local:o/r#2:codex",
        "o/r#2",
        "implement",
        "codex",
        mode="local",
        pr_number=2,
        routing_metadata={"caller_pinned": True, "route_decision": _untilt_decision("codex")},
    )
    feedback.record_outcome(
        "local:o/r#2:codex",
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
    )

    # 3. Local implement run that is caller_pinned with no route_decision.
    feedback.record_run(
        "local:o/r#3:claude",
        "o/r#3",
        "implement",
        "claude",
        mode="local",
        pr_number=3,
        routing_metadata={"caller_pinned": True},
    )
    feedback.record_outcome(
        "local:o/r#3:claude",
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
    )

    section = _tilt_section(ledger, db_path)
    row = _tilt_row(section)
    assert row["situation_count"] == 2, row
    assert row["invocation_count"] == 1, row
    assert row["usable"] == 1, row
    assert row["acted_on"] == 1, row
    assert row["outcome"] == 1, row
    assert row["outcome_success"] == 1, row
    assert row["first_break"] == "works", row
    assert row["success_scope"] == (
        "router-chosen local implement starts drawn with the tilt, merged and judged durable"
    )
    assert "usable_reason" not in row

    # acted_on_index shares the same count as the monitor row.
    index, _ = monitor.acted_on_index(db_path, NOW, ["comparative-tilt"])
    assert index == {"comparative-tilt": 1}


def test_tilt_row_is_unmeasured_not_zero_when_the_brain_is_unreadable(tilt_env, tmp_path):
    """Pointing the reader at a missing database leaves all counts None with a reason, not 0."""
    ledger, _ = tilt_env
    missing_db = tmp_path / "does-not-exist.db"

    section = _tilt_section(ledger, missing_db)
    row = _tilt_row(section)
    assert row["situation_count"] is None, row
    assert row["invocation_count"] is None, row
    assert row["usable"] is None, row
    assert row["acted_on"] is None, row
    assert row["outcome"] is None, row
    assert row["outcome_success"] is None, row
    assert row["first_break"] is None, row
    assert row.get("usable_reason"), row
    assert section["errors"], section
    assert any("Brain evidence unavailable" in err for err in section["errors"]), section["errors"]

    rendered = "\n".join(monitor.format_lines(section))
    assert "comparative-tilt: demand unmeasured" in rendered, rendered
    assert f"usable unmeasured ({row['usable_reason']})" in rendered, rendered
    assert "first_break unmeasured" in rendered, rendered

    idx, reason = monitor.acted_on_index(missing_db, NOW, ["comparative-tilt"])
    assert idx is None
    assert "Brain evidence unavailable" in reason


def test_tilt_row_enforces_tier_usability_agent_and_durability_boundaries(tilt_env):
    """Verify usable/acted_on/outcome/success filters and mode/task_type isolation."""
    ledger, db_path = tilt_env

    # Tilted run with zero base sum -> invoked, NOT usable.
    zero_base = {
        "chosen": "gemini",
        "comparative_tilt": {
            "floor": 0.25,
            "shares": {"gemini": 1.0},
            "tier": [{"agent": "gemini", "base": 0.0, "tilted": 0.5}],
        },
    }
    feedback.record_run(
        "local:o/r#10:gemini",
        "o/r#10",
        "implement",
        "gemini",
        mode="local",
        routing_metadata={"route_decision": zero_base},
    )

    # Tilted usable run with empty agent -> usable, NOT acted_on.
    feedback.record_run(
        "local:o/r#11:none",
        "o/r#11",
        "implement",
        "",
        mode="local",
        routing_metadata={"route_decision": _tilted_decision("gemini")},
    )

    # Tilted usable run with broke_later -> acted_on and outcome, NOT success.
    feedback.record_run(
        "local:o/r#12:gemini",
        "o/r#12",
        "implement",
        "gemini",
        mode="local",
        pr_number=12,
        routing_metadata={"route_decision": _tilted_decision("gemini")},
    )
    feedback.record_outcome(
        "local:o/r#12:gemini",
        adjudicated_verdict="PASS",
        merged=1,
        durability="broke_later",
    )

    # Non-local and non-implement runs with tilted decisions must be ignored.
    feedback.record_run(
        "remote:o/r#13:gemini",
        "o/r#13",
        "implement",
        "gemini",
        mode="remote",
        routing_metadata={"route_decision": _tilted_decision("gemini")},
    )
    feedback.record_run(
        "local:o/r#14:gemini",
        "o/r#14",
        "review",
        "gemini",
        mode="local",
        routing_metadata={"route_decision": _tilted_decision("gemini")},
    )

    # Also check a malformed routing_metadata JSON string does not crash and is ignored.
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, mode, routing_metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("local:o/r#15:badjson", T0, "o/r#15", "implement", "gemini", "local", "{not-json"),
        )

    section = _tilt_section(ledger, db_path, env={"ORCH_LOCAL_COMPARATIVE": "0"})
    row = _tilt_row(section)
    assert row["situation_count"] == 3, row
    assert row["invocation_count"] == 3, row
    assert row["usable"] == 2, row
    assert row["acted_on"] == 1, row
    assert row["outcome"] == 1, row
    assert row["outcome_success"] == 0, row
    assert row["first_break"] == "no_success", row
    assert row["input_off"] == ["input_off:ORCH_LOCAL_COMPARATIVE"], row

    # Bounded window index respects run timestamps.
    win_in, _ = monitor.acted_on_window_index(db_path, {"comparative-tilt": (T0 - 10, T0 + 10)})
    assert win_in == {"comparative-tilt": 1}
    win_out, _ = monitor.acted_on_window_index(db_path, {"comparative-tilt": (T0 + 100, T0 + 200)})
    assert win_out == {"comparative-tilt": 0}


@pytest.mark.parametrize("raw", ["0", " 0 ", "false", " FALSE ", "no", " NO ", "off", " off "])
def test_tilt_row_input_off_matches_router_disabled_values(tilt_env, monkeypatch, raw):
    """Disabled-value semantics for ORCH_LOCAL_COMPARATIVE match router._local_comparative_enabled."""
    ledger, db_path = tilt_env
    feedback.record_run(
        "local:o/r#20:gemini",
        "o/r#20",
        "implement",
        "gemini",
        mode="local",
        routing_metadata={"route_decision": _tilted_decision("gemini")},
    )
    monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", raw)
    assert not router._local_comparative_enabled()

    section = _tilt_section(ledger, db_path, env={"ORCH_LOCAL_COMPARATIVE": raw})
    row = _tilt_row(section)
    assert row["input_off"] == ["input_off:ORCH_LOCAL_COMPARATIVE"], (raw, row)


@pytest.mark.parametrize(
    "env", [{}, {"ORCH_LOCAL_COMPARATIVE": ""}, {"ORCH_LOCAL_COMPARATIVE": "1"}]
)
def test_tilt_row_input_off_omitted_when_unset_empty_or_enabled(tilt_env, monkeypatch, env):
    """Unset, empty, or enabled ORCH_LOCAL_COMPARATIVE values do not emit input_off."""
    ledger, db_path = tilt_env
    feedback.record_run(
        "local:o/r#21:gemini",
        "o/r#21",
        "implement",
        "gemini",
        mode="local",
        routing_metadata={"route_decision": _tilted_decision("gemini")},
    )
    monkeypatch.delenv("ORCH_LOCAL_COMPARATIVE", raising=False)
    if "ORCH_LOCAL_COMPARATIVE" in env:
        monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", env["ORCH_LOCAL_COMPARATIVE"])
    assert router._local_comparative_enabled()

    section = _tilt_section(ledger, db_path, env=env)
    row = _tilt_row(section)
    assert row["input_off"] == [], (env, row)


def test_partial_tilt_runs_read_failure_stays_unmeasured_in_admission_gate(tilt_env, tmp_path):
    """When edges load but comparative-tilt runs fail, admission reports unmeasured with the reason."""
    _, _ = tilt_env
    partial_db = tmp_path / "partial.db"
    with sqlite3.connect(partial_db) as con:
        con.executescript("""
            CREATE TABLE completion_events (
                event_id TEXT,
                created_ts INTEGER,
                validation_status TEXT,
                producer TEXT,
                run_id TEXT,
                capability_id TEXT,
                capability_version_id TEXT,
                payload_json TEXT
            );
            CREATE TABLE influence_edges (
                edge_id TEXT,
                capability_id TEXT,
                capability_version_id TEXT,
                target_run_id TEXT,
                target_event_id TEXT,
                source_event_id TEXT,
                accepted INTEGER,
                counterfactual INTEGER,
                created_ts INTEGER
            );
            CREATE TABLE outcomes (
                run_id TEXT,
                adjudicated_verdict TEXT,
                verifier_verdict TEXT,
                durability TEXT,
                merged INTEGER,
                failure_class TEXT,
                durability_checked_ts INTEGER
            );
            CREATE TABLE runs (
                run_id TEXT,
                role_name TEXT,
                mode TEXT,
                pr_number INTEGER,
                routing_metadata TEXT
            );
            """)

    index, reason = monitor.acted_on_index(partial_db, NOW, ["comparative-tilt", "other-cap"])
    assert index == {"comparative-tilt": None, "other-cap": 0}
    assert reason.startswith("comparative-tilt runs unavailable:"), reason

    win_index, win_reason = monitor.acted_on_window_index(
        partial_db, {"comparative-tilt": (T0 - 10, T0 + 10)}
    )
    assert win_index == {"comparative-tilt": None}
    assert win_reason.startswith("comparative-tilt runs unavailable:"), win_reason

    (tmp_path / "x.py").write_text("def consume():\n    return 1\n")
    cap = capabilities._blank_capability("comparative-tilt")
    cap["downstream_consumer"] = "x.py:consume"
    cap["learning_sink"] = "feedback.outcomes"
    cap["event_history"] = [
        {"timestamp": NOW - admission.CONSUMER_GRACE_SECONDS - 86400, "type": "invocation"}
    ]
    ctx = {
        "audit_rows": {},
        "fixtures": set(),
        "module_dir": tmp_path,
        "repo_root": tmp_path,
        "acted_on": index,
        "acted_on_reason": reason,
        "now": NOW,
    }
    cls, ok, detail = admission.consumer_class(cap, ctx)
    assert cls == "unmeasured", (cls, ok, detail)
    assert ok is True
    assert reason in detail


def test_local_loop_preview_suppresses_tilt_heartbeat_while_live_records_it(monkeypatch, tmp_path):
    """Tick.next_round(apply=False) suppresses comparative-tilt heartbeat; live draw records it."""
    export_path = tmp_path / router.ROUTE_WEIGHTS_EXPORT_NAME
    export_path.write_text(
        json.dumps(
            {
                "version": 1,
                "comparative": {
                    "route_enabled": True,
                    "assignment": {"implement": {"gemini": 10}},
                },
            }
        )
    )
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_COMPARATIVE", "1")
    monkeypatch.setattr(router, "_COMPARATIVE_EXPORT_CACHE", {})
    monkeypatch.setattr(
        router,
        "load_capacity",
        lambda: {
            "agents": {
                "gemini": {
                    "state": "ok",
                    "policy": "ok",
                    "pacing": {"routing_weight": 1.0, "state": "ok"},
                },
                "claude": {
                    "state": "ok",
                    "policy": "ok",
                    "pacing": {"routing_weight": 1.0, "state": "ok"},
                },
                "codex": {
                    "state": "ok",
                    "policy": "ok",
                    "pacing": {"routing_weight": 1.0, "state": "ok"},
                },
            }
        },
    )
    monkeypatch.setattr(
        router,
        "learned_ranks",
        lambda: {
            "implement": {
                "gemini": {"rank": 0, "posterior": 0.7, "score": 0.7, "n_obs": 10},
                "claude": {"rank": 1, "posterior": 0.7, "score": 0.7, "n_obs": 10},
                "codex": {"rank": 2, "posterior": 0.7, "score": 0.7, "n_obs": 10},
            }
        },
    )
    monkeypatch.setattr(router, "_exploration_rate", lambda *_a, **_k: 0.0)

    heartbeats: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        router,
        "_capability_heartbeat",
        lambda cid, event_type, *, ref="": heartbeats.append((cid, event_type, ref)),
    )

    pr = {
        "number": 3,
        "headRefOid": "abc12345",
        "headRefName": "codex/issue-7",
        "isDraft": False,
        "body": "Closes #7",
    }
    issue = {"number": 7, "title": "t", "body": "## Tasks\n- [ ] work\n"}
    base_state = {
        "target": "o/r#3",
        "agent": "codex",
        "tried": ["codex"],
        "unproductive": local_loop.STALL_ROUNDS,
        "rounds": [{"n": 1}, {"n": 2}],
    }
    launches: list[dict] = []

    def fake_launch(agent, target, prompt, rationale, **kwargs):
        launches.append({"agent": agent, "target": target, **kwargs})
        return {"run_id": "local:o/r#3:rotated", "log": "/tmp/run.log"}

    preview_state = dict(base_state, rounds=list(base_state["rounds"]))
    preview_tick = local_loop.Tick(
        gh=None,
        apply=False,
        now=lambda: float(T0),
        capacity=lambda: {"ok": True, "reason": "local agents 0/3"},
        rotate=local_loop.default_rotate,
        launch=fake_launch,
    )
    preview_res = preview_tick.next_round(
        preview_state, pr, issue, "o/r", {local_loop.LABEL}, "CI red", [], None
    )
    assert preview_res["action"] == "would-round", preview_res
    assert preview_state.get("route_decision", {}).get("comparative_tilt"), preview_state
    assert heartbeats == [], f"preview must not record live heartbeats: {heartbeats}"
    assert launches == []

    live_state = dict(base_state, rounds=list(base_state["rounds"]))
    live_tick = local_loop.Tick(
        gh=None,
        apply=True,
        now=lambda: float(T0),
        capacity=lambda: {"ok": True, "reason": "local agents 0/3"},
        rotate=local_loop.default_rotate,
        launch=fake_launch,
    )
    live_res = live_tick.next_round(
        live_state, pr, issue, "o/r", {local_loop.LABEL}, "CI red", [], None
    )
    assert live_res["action"] == "round", live_res
    assert len(launches) == 1
    assert launches[0].get("route_decision", {}).get("comparative_tilt")
    assert any(
        cid == "comparative-tilt" and event_type == "invocation"
        for cid, event_type, _ in heartbeats
    ), heartbeats
