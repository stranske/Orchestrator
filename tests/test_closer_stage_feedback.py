"""Native assessment quality feeds the existing learner without fabricating delivery."""

import json

import pytest

import closer_stage_feedback as stage
import execution_profiles
import feedback
import router

HEAD = "a" * 40
TARGET = "stranske/Orchestrator#123"
PROFILE = "codex-6-luna-low"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **kw: None)
    with feedback._conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id,agent,task_type,routing_metadata) VALUES(?,?,?,?)",
            (
                "native-run",
                "codex",
                "closer_interpret",
                json.dumps({"selected_profile_id": PROFILE, "subject_id": TARGET}),
            ),
        )


def record(**changes):
    args = dict(
        result={"run_id": "native-run", "exit": 0},
        task_type="closer_interpret",
        target=TARGET,
        head=HEAD,
        agent="codex",
        profile_id=PROFILE,
        basis={"head": HEAD},
    )
    args.update(changes)
    stage.record_stage(**args)


def test_exit_zero_is_not_quality_or_source_completion(brain):
    record()
    with feedback._conn() as conn:
        assert feedback._closer_stage_rewards(conn, "closer_interpret") == {}
        payload = json.loads(
            conn.execute("SELECT payload_json FROM completion_events").fetchone()[0]
        )["closer_stage"]
        assert payload["accepted"] is None
        assert payload["guarded_operation_success"] is None
        assert payload["source_completion"] is False
        assert payload["observed_cost_usd"] is None
        assert payload["actual_marginal_dollars"] is None
        assert payload["opportunity_cost"] is None
        assert payload["measured_usage"] == {"tokens_in": None, "tokens_out": None}
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["accepted", "ordinary_nonpass"])
def test_independent_quality_idempotently_reaches_existing_learner(brain, status):
    record(validation={"status": status})
    record(validation={"status": status})
    with feedback._conn() as conn:
        assert feedback._closer_stage_rewards(conn, "closer_interpret") == {"native-run": 1.0}
        assert feedback._closer_stage_rewards(conn, "implement") == {}
        assert conn.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_malformed_response_is_negative_quality_but_unknown_is_not(brain):
    record(validation={"status": "blocked"}, structurally_invalid=True)
    with feedback._conn() as conn:
        assert feedback._closer_stage_rewards(conn, "closer_interpret") == {"native-run": 0.0}
    record(result={"run_id": "native-run", "exit": 1}, validation={"status": "unknown"})
    with feedback._conn() as conn:
        assert feedback._closer_stage_rewards(conn, "closer_interpret") == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "stranske/Other#1"},
        {"profile_id": "codex-6-astra-high"},
        {"result": {"run_id": "unregistered", "exit": 0}},
    ],
)
def test_unbound_events_cannot_create_quality(brain, changes):
    record(validation={"status": "accepted"}, **changes)
    with feedback._conn() as conn:
        assert feedback._closer_stage_rewards(conn, "closer_interpret") == {}
        assert conn.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] == 0


def test_capacity_is_scoped_to_selected_profile_and_its_pools():
    profile = execution_profiles.get_profile("claude-sonnet-5-5-high")
    cap = {
        "agents": {"claude": {"state": "ok", "auth": {"authenticated": True}}},
        "profiles": {profile["profile_id"]: {"state": "ok"}},
        "pools": {key: {"state": "ok"} for key in profile["capacity_pool_ids"]},
    }
    cap["pools"]["hosted-claude-oauth"] = {"state": "shed", "reason": "401 invalid OAuth"}
    assert router._closer_profile_capacity(profile, cap)
    cap["pools"][profile["capacity_pool_ids"][0]] = {"state": "shed"}
    assert not router._closer_profile_capacity(profile, cap)
    cap["pools"][profile["capacity_pool_ids"][0]] = {"state": "ok"}
    assert router._closer_profile_capacity(profile, cap)


def test_native_stage_quality_changes_existing_routing_posterior_without_outcome(brain):
    import time

    with feedback._conn() as conn:
        conn.execute("UPDATE runs SET ts=? WHERE run_id='native-run'", (int(time.time()),))
    record(validation={"status": "accepted"})
    version = feedback.relearn_quality({"closer_interpret": {"codex": 0.5, "claude": 0.5}})
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT posterior,n_obs,rationale FROM route_weights WHERE version=? AND agent='codex'",
            (version,),
        ).fetchone()
        assert row[0] > 0.5 and row[1] == 1
        assert "outcome_runs=0" in row[2]
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0
