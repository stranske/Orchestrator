from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import feedback
import model_profile_trial

TRIAL_FEEDBACK_TABLES = (
    "runs",
    "execution_attempts",
    "outcomes",
    "execution_traces",
    "completion_events",
    "influence_edges",
    "profile_trial_ingests",
)


def _assert_trial_feedback_tables_empty(conn) -> None:
    for table in TRIAL_FEEDBACK_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        count = 0 if not exists else conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, table


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
        for table in TRIAL_FEEDBACK_TABLES:
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
        _assert_trial_feedback_tables_empty(conn)


def test_finalize_refuses_ingest_when_identity_verified_is_omitted(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="identity is unverified"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            record_feedback=True,
            ingest_brain=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def _bind_authoritative_identity(tmp_path, trial_attempt_fixture):
    artifact_dir = tmp_path / "identity-artifacts"
    artifact_dir.mkdir()
    for attempt in trial_attempt_fixture["attempts"]:
        artifact_path = artifact_dir / f"{attempt['profile_id']}-identity.json"
        artifact_path.write_text(
            json.dumps({"profile_id": attempt["profile_id"], "acknowledged": True}),
            encoding="utf-8",
        )
        digest = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        attempt["artifact_ref"] = str(artifact_path)
        attempt["identity_evidence"]["artifact_ref"] = str(artifact_path)
        attempt["identity_evidence"]["artifact_sha256"] = digest
    trial_attempt_fixture["identity_verified"] = True
    return trial_attempt_fixture


def test_finalize_writes_measured_quality_by_profile(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    results = copy.deepcopy(trial_attempt_fixture)
    _bind_authoritative_identity(tmp_path, results)
    quality = {
        "codex-6-astra-high": 0.82,
        "codex-5.6-terra-high": 0.76,
        "codex-5.6-luna-high": 0.79,
    }
    results["quality_by_profile"] = quality
    model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        record_feedback=True,
        ingest_brain=True,
        now=2_000,
    )
    summary = json.loads(
        (tmp_path / "capability-program" / "profile-trial.json").read_text(encoding="utf-8")
    )
    assert summary["identity_verified"] is True
    assert summary["brain_ingest_enabled"] is True
    assert summary["quality_by_profile"] == quality


def test_finalize_refuses_ingest_when_identity_artifact_hash_mismatches(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    results = copy.deepcopy(trial_attempt_fixture)
    _bind_authoritative_identity(tmp_path, results)
    artifact_path = Path(results["attempts"][0]["identity_evidence"]["artifact_ref"])
    artifact_path.write_text('{"tampered": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            results,
            record_feedback=True,
            ingest_brain=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def test_transport_recording_does_not_block_later_verified_ingest(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    results = copy.deepcopy(trial_attempt_fixture)
    _bind_authoritative_identity(tmp_path, results)
    quality = {
        "codex-6-astra-high": 0.82,
        "codex-5.6-terra-high": 0.76,
        "codex-5.6-luna-high": 0.79,
    }
    results["quality_by_profile"] = quality
    model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        record_feedback=True,
        ingest_brain=False,
        now=2_000,
    )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    verified = copy.deepcopy(results)
    state = model_profile_trial.finalize_trial(
        trial_manifest,
        verified,
        record_feedback=True,
        ingest_brain=True,
        now=2_001,
    )
    assert state["brain_ingest_enabled"] is True
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 1


def test_transport_recording_preserves_measured_program_summary(
    tmp_path, monkeypatch, trial_roots, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    program_dir = tmp_path / "capability-program"
    program_dir.mkdir(parents=True)
    preserved = {
        "trial_id": trial_manifest["trial_id"],
        "updated_at": 1_500,
        "profile_count": 3,
        "instance_count": 3,
        "identity_verified": True,
        "brain_ingest_enabled": True,
        "quality_by_profile": {
            "codex-6-astra-high": 0.9,
            "codex-5.6-terra-high": 0.8,
            "codex-5.6-luna-high": 0.85,
        },
        "cost_tokens_by_profile": {},
        "aggregate_tokens_in": 0,
        "aggregate_tokens_out": 0,
    }
    (program_dir / "profile-trial.json").write_text(json.dumps(preserved), encoding="utf-8")
    model_profile_trial.finalize_trial(
        trial_manifest,
        trial_attempt_fixture,
        record_feedback=True,
        ingest_brain=False,
        now=2_000,
    )
    summary = json.loads((program_dir / "profile-trial.json").read_text(encoding="utf-8"))
    assert summary["identity_verified"] is True
    assert summary["brain_ingest_enabled"] is True
    assert summary["quality_by_profile"] == preserved["quality_by_profile"]
    assert summary["updated_at"] == 2_000
