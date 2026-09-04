from __future__ import annotations

import json

import pytest

import capabilities
import execution_profiles
import feedback
import langsmith_pull
import model_profile_trial
import observability_dashboard
import periodic_report


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
    """Workflows worker/fallback telemetry plus a separate Terra evaluator."""
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


def test_three_profiles_create_distinct_worker_attempts(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    state_path = tmp_path / "state" / "model-profile-trial.json"
    state = model_profile_trial.finalize_trial(
        trial_manifest,
        trial_attempt_fixture,
        state_path=state_path,
        record_feedback=True,
        now=2_000,
    )
    with feedback._conn() as conn:
        attempts = conn.execute(
            "SELECT attempt_id,profile_id,requested_model,selected_model,reported_model,"
            "resolved_model,fallback_reason,runner_version,cli_version "
            "FROM execution_attempts WHERE operation_role='worker' ORDER BY profile_id"
        ).fetchall()
        runs = conn.execute("SELECT assignment,COUNT(*) FROM runs GROUP BY assignment").fetchall()
        v1_weights = conn.execute("SELECT COUNT(*) FROM route_weights").fetchone()[0]
        v2_weights = conn.execute("SELECT COUNT(*) FROM route_weights_v2").fetchone()[0]
        profile_report = execution_profiles.report(conn, now=2_000)

    assert len(attempts) == 3
    assert len({row[0] for row in attempts}) == 3
    assert {row[1] for row in attempts} == set(model_profile_trial.EXPECTED_PROFILE_IDS)
    assert all(row[4] and row[5] == row[3] for row in attempts)
    assert all(row[7] and row[8] for row in attempts)
    assert runs == [("instrumentation", 3)]
    assert v1_weights == 0 and v2_weights == 0
    assert state["assignment"] == "instrumentation"
    assert state["learning_enabled"] is False
    assert state["capacity_snapshot"]["snapshot_count"] == 1
    assert state["shared_pool_debit"] == {"codex-subscription": 3.0}
    assert state["source_integrity"]["unchanged"] is True
    assert state["route_weight_integrity"]["unchanged"] is True
    assert profile_report["instrumentation_attempts"] == 3
    assert profile_report["instrumentation_excluded_from_learning"] is True
    assert all(row["learning_eligible_attempts"] == 0 for row in profile_report["profiles"])
    assert json.loads(state_path.read_text())["lifecycle"] == "shadow"


def test_evaluator_trace_stays_separate(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    model_profile_trial.finalize_trial(
        trial_manifest,
        trial_attempt_fixture,
        record_feedback=True,
        now=2_000,
    )
    with feedback._conn() as conn:
        worker_rows = conn.execute(
            "SELECT profile_id,reported_model FROM execution_attempts "
            "WHERE operation_role='worker' ORDER BY profile_id"
        ).fetchall()
        evaluator_rows = conn.execute(
            "SELECT operation_role,profile_id,resolved_model FROM execution_attempts "
            "WHERE operation_role='evaluator'"
        ).fetchall()
    assert len(worker_rows) == 3 and {row[0] for row in worker_rows} == set(
        model_profile_trial.EXPECTED_PROFILE_IDS
    ), "evaluator trace contaminated worker attempt"
    assert evaluator_rows == [("evaluator", None, "claude-evaluator-only")]


def test_source_change_stops_before_feedback_write(
    tmp_path, monkeypatch, trial_roots, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    (trial_roots[1] / "worker.yml").write_text("worker: mutated\n")
    with pytest.raises(ValueError, match="source integrity changed"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            record_feedback=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_feedback_recording_requires_explicit_quarantine_database(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "ordinary-feedback.db")
    with pytest.raises(ValueError, match="quarantine database"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            record_feedback=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_source_manifest_prunes_derived_trees_and_enforces_bounds(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "large-object").write_text("must not be read")
    (root / "kept.txt").write_text("kept")
    monkeypatch.setattr(model_profile_trial, "MAX_MANIFEST_FILES", 1)
    manifest = model_profile_trial.source_manifest(root)
    assert manifest["file_count"] == 1
    assert manifest["entries"][0]["path"] == "kept.txt"
    (root / "second.txt").write_text("second")
    (root / "third.txt").write_text("third")
    with pytest.raises(ValueError, match="source manifest exceeds file limit"):
        model_profile_trial.source_manifest(root)


def test_workflows_worker_profile_contract_ingests_all_identity_layers(tmp_path, monkeypatch):
    _use_temp_feedback(tmp_path, monkeypatch)
    feedback.record_run(
        "workflow-worker",
        "owner/repo#14",
        "instrumentation:model_profile_trial",
        "codex",
        assignment="instrumentation",
        source="instrumentation",
    )
    artifact = tmp_path / "langsmith-fleet.ndjson"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": langsmith_pull.SCHEMA_VERSION,
                "run_id": "workflow-worker",
                "operation": "worker_dispatch",
                "operation_role": "worker",
                "status": "success",
                "trace_id": "worker-trace",
                "provider": "openai",
                "model": "generic-trace-model-must-not-resolve-worker",
                "domain": {
                    "profile_id": "codex-5.6-terra-high",
                    "requested_provider": "openai",
                    "requested_model": "gpt-5.6-terra",
                    "selected_model": "gpt-5.6-terra",
                    "reported_model": "gpt-5.6-terra",
                    "fallback_reason": None,
                    "runner_version": "reusable-codex-run@v1",
                    "cli_version": "codex-cli 0.144.0-alpha.4",
                },
                "latency_ms": 250,
                "tokens_in": 10,
                "tokens_out": 2,
            }
        )
        + "\n"
    )
    summary = langsmith_pull.ingest_files([artifact])
    assert summary["worker_profile_records"] == 1
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT profile_id,requested_model,selected_model,reported_model,"
            "resolved_model,runner_version,cli_version FROM execution_attempts "
            "WHERE run_id='workflow-worker' AND operation_role='worker'"
        ).fetchone()
    assert row == (
        "codex-5.6-terra-high",
        "gpt-5.6-terra",
        "gpt-5.6-terra",
        "gpt-5.6-terra",
        None,
        "reusable-codex-run@v1",
        "codex-cli 0.144.0-alpha.4",
    )


def test_trial_readiness_surfaces_in_reports_and_lifecycle(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    model_profile_trial.finalize_trial(
        trial_manifest,
        trial_attempt_fixture,
        state_path=state_path,
        record_feedback=True,
        now=2_000,
    )
    report = periodic_report.build_report(
        window_days=1,
        hypotheses_path=tmp_path / "hypotheses.json",
        features_path=tmp_path / "features.json",
        capabilities_path=tmp_path / "capabilities.json",
        keepalive_corpus_path=tmp_path / "keepalive.jsonl",
        redirect_corpus_path=tmp_path / "redirect.jsonl",
        model_profile_trial_path=state_path,
        probe_langsmith_artifacts=False,
    )
    health = observability_dashboard._data_health(report)
    assert report["model_profile_trial"]["status"] == "complete"
    assert health["model_profile_trial_attempts"] == 3
    assert health["model_profile_trial_source_unchanged"] is True
    gate = capabilities.KNOWN_GATES[model_profile_trial.CAPABILITY_ID]
    assert gate["status"] == "shadow"
    assert gate["flags_defaults"]["learning_enabled"] is False
    assert gate["flags_defaults"]["promotion_allowed"] is False


def test_frozen_sol_trial_remains_valid_after_astra_migration(trial_roots, monkeypatch):
    current = model_profile_trial.EXPECTED_PROFILE_IDS
    monkeypatch.setattr(
        model_profile_trial, "EXPECTED_PROFILE_IDS", model_profile_trial.LEGACY_PROFILE_IDS
    )
    frozen = model_profile_trial.build_trial_manifest(
        *trial_roots, seed=14, now=1000, capacity_state="ok"
    )
    results = trial_attempt_fixture.__wrapped__(frozen)
    monkeypatch.setattr(model_profile_trial, "EXPECTED_PROFILE_IDS", current)
    model_profile_trial.validate_trial_manifest(frozen)
    attempts = model_profile_trial._validate_results(frozen, results)
    assert {item["profile_id"] for item in attempts} == set(model_profile_trial.LEGACY_PROFILE_IDS)
    fresh = model_profile_trial.build_trial_manifest(
        *trial_roots, seed=14, now=1000, capacity_state="ok"
    )
    assert "codex-6-astra-high" in fresh["launch_order"]
    assert fresh["trial_id"] != frozen["trial_id"]
