from __future__ import annotations

import pytest

import feedback
import model_profile_trial


@pytest.fixture
def trial_roots(tmp_path):
    orchestrator = tmp_path / "orchestrator-source"
    workflows = tmp_path / "workflows-source"
    orchestrator.mkdir()
    workflows.mkdir()
    (orchestrator / "control.py").write_text("CONTROL = 'unchanged'\n")
    (workflows / "worker.yml").write_text("worker: read-only\n")
    return orchestrator, workflows


@pytest.fixture
def trial_manifest(trial_roots):
    return model_profile_trial.build_trial_manifest(
        *trial_roots,
        seed=14,
        now=1_000,
        capacity_state="ok",
    )


@pytest.fixture
def trial_attempt_fixture(trial_manifest):
    attempts = []
    for request in trial_manifest["requests"]:
        profile_id = request["profile_id"]
        selected = request["requested_model"]
        artifact_ref = f"workflows:trial:{profile_id}"
        attempts.append(
            {
                "run_id": request["run_id"],
                "profile_id": profile_id,
                "operation_role": "worker",
                "requested_model": request["requested_model"],
                "selected_model": selected,
                "reported_model": selected,
                "provider_resolved_provider": "openai",
                "provider_resolved_model": selected,
                "fallback_reason": None,
                "runner_version": "workflows/reusable-codex-run@profile-contract-v1",
                "cli_version": "codex-cli 0.144.0-alpha.4 (app-bundled)",
                "status": "success",
                "latency_s": 1.5,
                "tokens_in": 12,
                "tokens_out": 4,
                "artifact_ref": artifact_ref,
                "packet_hash": trial_manifest["packet_hash"],
                "acknowledged": True,
                "identity_evidence": {
                    "schema": model_profile_trial.IDENTITY_EVIDENCE_SCHEMA,
                    "version": model_profile_trial.IDENTITY_EVIDENCE_VERSION,
                    "authority": "workflows-read-only-trial-artifact/v1",
                    "artifact_ref": artifact_ref,
                    "artifact_sha256": "sha256:" + ("a" * 64),
                },
            }
        )
    terra_run = next(
        request["run_id"]
        for request in trial_manifest["requests"]
        if request["profile_id"] == "codex-5.6-terra-high"
    )
    return {
        "schema": model_profile_trial.RESULT_SCHEMA,
        "version": 1,
        "trial_id": trial_manifest["trial_id"],
        "packet_hash": trial_manifest["packet_hash"],
        "acknowledged": True,
        "attempts": attempts,
        "auxiliary_traces": [
            {
                "run_id": terra_run,
                "trace_id": "terra-evaluator-trace",
                "operation": "evaluate_pr_compare",
                "operation_role": "evaluator",
                "provider": "anthropic",
                "model": "claude-evaluator-only",
                "status": "success",
            }
        ],
    }


def _use_temp_feedback(tmp_path, monkeypatch):
    db = tmp_path / "quarantine-feedback.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    return db


@pytest.mark.parametrize(
    ("writer", "fail_on"),
    [
        ("_record_run_in_conn", 2),
        ("_record_outcome_in_conn", 2),
        ("record_execution_trace", 1),
    ],
)
def test_ingest_is_one_transaction_and_a_mid_write_failure_leaves_nothing(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, writer, fail_on
):
    _use_temp_feedback(tmp_path, monkeypatch)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    calls = {"n": 0}
    original = getattr(feedback, writer)

    def flaky(*args, **kwargs):
        calls["n"] += 1
        value = original(*args, **kwargs)
        if calls["n"] == fail_on:
            raise RuntimeError("simulated mid-write failure")
        return value

    monkeypatch.setattr(feedback, writer, flaky)
    with pytest.raises(RuntimeError, match="simulated mid-write"):
        feedback.ingest_profile_trial(
            trial_attempt_fixture,
            manifest=trial_manifest,
            attempts=attempts,
            auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
            ts=2_000,
        )
    assert calls["n"] == fail_on
    with feedback._conn() as conn:
        for table in (
            "runs",
            "execution_attempts",
            "outcomes",
            "execution_traces",
            "completion_events",
            "influence_edges",
            "profile_trial_ingests",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_ingest_is_idempotent_on_trial_id(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    first = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_000,
    )
    tables = (
        "runs",
        "execution_attempts",
        "outcomes",
        "execution_traces",
        "completion_events",
        "influence_edges",
        "profile_trial_ingests",
        "route_weights",
        "route_weights_v2",
    )
    with feedback._conn() as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        outcomes = conn.execute(
            "SELECT run_id,verifier_verdict,adjudicated_verdict,merged,ci_status,durability "
            "FROM outcomes ORDER BY run_id"
        ).fetchall()
        assert outcomes == [
            (run_id, None, None, None, None, "pending")
            for run_id in sorted(attempt["run_id"] for attempt in attempts)
        ]
        assert len(before["runs"]) == len(outcomes) == 3
        worker_attempts = conn.execute(
            "SELECT run_id FROM execution_attempts WHERE operation_role='worker' ORDER BY run_id"
        ).fetchall()
        assert worker_attempts == [(row[0],) for row in outcomes]
        assert len(before["execution_attempts"]) == 4  # Three workers and the evaluator trace.
        assert len(before["execution_traces"]) == 1
    second = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_001,
    )
    assert first["status"] == "ingested"
    assert second["status"] == "already_ingested"
    assert second["applied_ts"] == first["applied_ts"] == 2_000
    assert len(first["recorded_attempt_ids"]) == 3
    assert second["recorded_attempt_ids"] == []
    with feedback._conn() as conn:
        after = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
    assert after == before
    assert len(after["profile_trial_ingests"]) == 1


def test_finalize_refuses_ingest_when_identity_is_unverified(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    trial_attempt_fixture["identity_verified"] = False
    with pytest.raises(ValueError, match="identity is unverified"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            record_feedback=True,
            ingest_brain=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
