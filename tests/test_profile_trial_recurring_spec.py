"""The two-by-three plan reuses the strategy experiment's frozen spec, or it refuses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import feedback
import model_profile_trial as trial

SPEC = "Acceptance criteria: keep the frozen recurring-work body unchanged.\n"
EXP_ID = "strategy434-20261006"


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    orchestrator = tmp_path / "orchestrator"
    workflows = tmp_path / "workflows"
    orchestrator.mkdir()
    workflows.mkdir()
    (orchestrator / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workflows / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    return orchestrator, workflows


def _seal(
    tmp_path: Path,
    spec: str = SPEC,
    *,
    spec_file: Path | None = None,
    spec_sha256: str | None = None,
    exp_id: str = EXP_ID,
    write_meta: bool = True,
) -> tuple[Path, Path]:
    state = tmp_path / "state"
    experiments = tmp_path / "experiments"
    experiment = experiments / exp_id
    experiment.mkdir(parents=True)
    spec_path = experiment / "spec.md"
    spec_path.write_text(spec, encoding="utf-8")
    recorded = spec_path if spec_file is None else spec_file
    (experiment / "strategy.json").write_text(
        json.dumps({"exp_id": exp_id, "spec_file": str(recorded)}), encoding="utf-8"
    )
    if write_meta:
        sealed = (
            hashlib.sha256(spec.encode("utf-8")).hexdigest() if spec_sha256 is None else spec_sha256
        )
        (experiment / "meta.json").write_text(json.dumps({"spec_sha256": sealed}), encoding="utf-8")
    receipt = state / "capability-program" / "strategy-experiment.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"exp_id": exp_id, "status": "completed"}), encoding="utf-8")
    return state, experiments


def test_current_plan_binds_two_profiles_and_the_frozen_spec(tmp_path):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    trial.validate_trial_manifest(manifest)
    packet = manifest["frozen_packet"]
    assert packet["frozen_spec"] == SPEC
    assert packet["exp_id"] == EXP_ID
    assert packet["work_shape"] == "strategy-experiment-frozen-spec"
    assert packet["spec_sha256"] == "sha256:" + hashlib.sha256(SPEC.encode("utf-8")).hexdigest()
    assert manifest["selected_profile_ids"] == list(trial.CURRENT_TRIAL_PROFILE_IDS)
    assert manifest["instances_per_profile"] == 3
    assert len(manifest["requests"]) == 6
    for profile_id in trial.CURRENT_TRIAL_PROFILE_IDS:
        assert sum(row["profile_id"] == profile_id for row in manifest["requests"]) == 3
    assert not (state / "capability-program" / "profile-trial.json").exists()


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", ".hidden", 7, None])
def test_receipt_with_unsafe_experiment_id_is_refused(tmp_path, bad_id):
    state, experiments = _seal(tmp_path)
    receipt = state / "capability-program" / "strategy-experiment.json"
    receipt.write_text(json.dumps({"exp_id": bad_id}), encoding="utf-8")
    with pytest.raises(trial.RecurringWorkUnavailable, match="no experiment id"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)


def test_absent_receipt_stub_mismatch_and_foreign_spec_are_refused(tmp_path):
    orchestrator, workflows = _sources(tmp_path)
    with pytest.raises(trial.RecurringWorkUnavailable, match="receipt is absent"):
        trial.build_current_recurring_trial(
            orchestrator,
            workflows,
            state_dir=tmp_path / "missing-state",
            experiments_root=tmp_path / "missing-experiments",
        )

    state, experiments = _seal(tmp_path / "stub", spec="[SPEC_PROVENANCE: MISSING_SPEC_STUB]\n")
    with pytest.raises(trial.RecurringWorkUnavailable, match="missing-spec stub"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)

    state, experiments = _seal(tmp_path / "sha", spec_sha256="0" * 64)
    with pytest.raises(trial.RecurringWorkUnavailable, match="spec_sha256"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)

    foreign = tmp_path / "foreign" / "other.md"
    foreign.parent.mkdir()
    foreign.write_text(SPEC, encoding="utf-8")
    state, experiments = _seal(tmp_path / "foreign-spec", spec_file=foreign)
    with pytest.raises(trial.RecurringWorkUnavailable, match="not the frozen spec.md"):
        trial.resolve_strategy_frozen_packet(state_dir=state, experiments_root=experiments)
    assert not (state / "capability-program" / "profile-trial.json").exists()


def test_prepare_current_cli_writes_the_plan_and_not_the_program_summary(tmp_path, capsys):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    output = tmp_path / "outside" / "manifest.json"
    status = trial.main(
        [
            "prepare-current",
            "--orchestrator-root",
            str(orchestrator),
            "--workflows-root",
            str(workflows),
            "--output",
            str(output),
            "--seed",
            "14",
            "--capacity-state",
            "ok",
            "--state-dir",
            str(state),
            "--experiments-root",
            str(experiments),
        ]
    )
    assert status == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert printed["trial_id"] == manifest["trial_id"]
    assert manifest["frozen_packet"]["frozen_spec"] == SPEC
    assert len(manifest["requests"]) == 6
    assert not (state / "capability-program" / "profile-trial.json").exists()


def _measured_results(root: Path, manifest: dict) -> dict:
    """Six unverified measurements; request fields are not trusted provider provenance."""
    attempts = []
    for request in manifest["requests"]:
        artifact = root / f"identity-{request['launch_ordinal']}.json"
        identity = {
            "profile_id": request["profile_id"],
            "provider_resolved_provider": request["provider"],
            "provider_resolved_model": request["requested_model"],
        }
        artifact.write_text(
            json.dumps(
                {
                    **identity,
                    "object": "turn.completed",
                    "server_model": request["requested_model"],
                    "provenance": trial.UNVERIFIED_SESSION_AUTHORITY,
                }
            ),
            encoding="utf-8",
        )
        digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        attempts.append(
            {
                **identity,
                "run_id": request["run_id"],
                "operation_role": "worker",
                "requested_model": request["requested_model"],
                "selected_model": request["requested_model"],
                "reported_model": request["requested_model"],
                "runner_version": "offline-recurring",
                "cli_version": "offline-recurring",
                "status": "success",
                "packet_hash": manifest["packet_hash"],
                "acknowledged": True,
                "tokens_in": 10 * request["launch_ordinal"],
                "tokens_out": request["launch_ordinal"],
                "identity_evidence": {
                    "schema": trial.IDENTITY_EVIDENCE_SCHEMA,
                    "version": trial.IDENTITY_EVIDENCE_VERSION,
                    "authority": trial.UNVERIFIED_SESSION_AUTHORITY,
                    "artifact_ref": str(artifact),
                    "artifact_sha256": digest,
                },
            }
        )
    return {
        "schema": trial.RESULT_SCHEMA,
        "version": trial.SCHEMA_VERSION,
        "trial_id": manifest["trial_id"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "identity_verified": False,
        "attempts": attempts,
        "quality_by_profile": {
            "codex-5.6-terra-high": 0.5,
            "codex-6-astra-high": 0.75,
        },
    }


def test_measured_recurring_trial_records_spec_quality_cost_and_identity(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    results = _measured_results(tmp_path, manifest)
    state_out = trial.finalize_trial(manifest, results, record_feedback=True, now=2000)
    summary = json.loads((state / "capability-program" / "profile-trial.json").read_text())
    assert state_out["attempt_count"] == 6
    assert state_out["brain_ingest_enabled"] is False
    assert summary["exp_id"] == EXP_ID
    assert summary["work_shape"] == "strategy-experiment-frozen-spec"
    assert summary["spec_sha256"] == manifest["frozen_packet"]["spec_sha256"]
    assert (summary["profile_count"], summary["instance_count"]) == (2, 6)
    assert summary["identity_verified"] is False
    assert summary["quality_by_profile"] == results["quality_by_profile"]
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 6
        assert (
            conn.execute("SELECT COUNT(DISTINCT attempt_id) FROM execution_attempts").fetchone()[0]
            == 6
        )
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0
    for profile_id in trial.CURRENT_TRIAL_PROFILE_IDS:
        attempts = [row for row in results["attempts"] if row["profile_id"] == profile_id]
        assert summary["cost_tokens_by_profile"][profile_id] == {
            "tokens_in": sum(row["tokens_in"] for row in attempts),
            "tokens_out": sum(row["tokens_out"] for row in attempts),
        }
        assert summary["provider_resolved_identity_by_profile"][profile_id] == {
            "provider": attempts[0]["provider_resolved_provider"],
            "model": attempts[0]["provider_resolved_model"],
        }
        assert len(attempts) == 3


def test_publish_current_records_measured_usage_quality_and_identity(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    results = _measured_results(tmp_path, manifest)
    for attempt in results["attempts"]:
        if attempt["profile_id"] == trial.CURRENT_TRIAL_PROFILE_IDS[0]:
            attempt["tokens_in"] = 0
            attempt["tokens_out"] = 0
    published = trial.publish_measured_recurring_trial(manifest, results, now=2000)
    summary = json.loads((state / "capability-program" / "profile-trial.json").read_text())
    assert published["attempt_count"] == 6
    assert (
        len(published["recorded_attempt_ids"]) == len(set(published["recorded_attempt_ids"])) == 6
    )
    assert published["brain_ingest_enabled"] is False
    assert (summary["profile_count"], summary["instance_count"]) == (2, 6)
    assert summary["identity_verified"] is False
    assert summary["quality_by_profile"] == results["quality_by_profile"]
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 6
        assert (
            conn.execute("SELECT COUNT(DISTINCT attempt_id) FROM execution_attempts").fetchone()[0]
            == 6
        )
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0
    assert summary["exp_id"] == EXP_ID
    zero_profile = trial.CURRENT_TRIAL_PROFILE_IDS[0]
    assert summary["cost_tokens_by_profile"][zero_profile] == {"tokens_in": 0, "tokens_out": 0}
    measured = trial.CURRENT_TRIAL_PROFILE_IDS[1]
    measured_attempts = [row for row in results["attempts"] if row["profile_id"] == measured]
    assert summary["cost_tokens_by_profile"][measured] == {
        "tokens_in": sum(row["tokens_in"] for row in measured_attempts),
        "tokens_out": sum(row["tokens_out"] for row in measured_attempts),
    }
    assert summary["provider_resolved_identity_by_profile"][measured]["model"] == (
        measured_attempts[0]["provider_resolved_model"]
    )


def test_publish_current_refuses_unmeasured_usage_before_any_write(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    results = _measured_results(tmp_path, manifest)
    results["attempts"][3].pop("tokens_out")
    with pytest.raises(ValueError, match="tokens_out"):
        trial.publish_measured_recurring_trial(manifest, results, now=2000)
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "quarantine-feedback.db").exists()


def test_publish_current_cli_writes_the_program_summary(tmp_path, monkeypatch, capsys):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    manifest_path = tmp_path / "manifest.json"
    results_path = tmp_path / "results.json"
    state_path = tmp_path / "outside-state.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    results_path.write_text(json.dumps(_measured_results(tmp_path, manifest)), encoding="utf-8")
    status = trial.main(
        [
            "publish-current",
            "--manifest",
            str(manifest_path),
            "--results",
            str(results_path),
            "--state",
            str(state_path),
        ]
    )
    assert status == 0
    printed = json.loads(capsys.readouterr().out)
    summary = json.loads((state / "capability-program" / "profile-trial.json").read_text())
    assert printed["attempt_count"] == 6
    assert summary["work_shape"] == "strategy-experiment-frozen-spec"
    assert summary["spec_sha256"] == manifest["frozen_packet"]["spec_sha256"]
    assert (summary["profile_count"], summary["instance_count"]) == (2, 6)
    assert set(summary["provider_resolved_identity_by_profile"]) == set(
        trial.CURRENT_TRIAL_PROFILE_IDS
    )


def test_publish_current_cli_refuses_a_missing_count_without_writing(tmp_path, monkeypatch, capsys):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    results = _measured_results(tmp_path, manifest)
    results["attempts"][0]["tokens_in"] = None
    manifest_path = tmp_path / "manifest.json"
    results_path = tmp_path / "results.json"
    state_path = tmp_path / "outside-state.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    results_path.write_text(json.dumps(results), encoding="utf-8")
    status = trial.main(
        [
            "publish-current",
            "--manifest",
            str(manifest_path),
            "--results",
            str(results_path),
            "--state",
            str(state_path),
        ]
    )
    assert status == 2
    assert "tokens_in" in capsys.readouterr().err
    assert not state_path.exists()
    assert not (state / "capability-program" / "profile-trial.json").exists()


def test_publish_measured_recurring_trial_refuses_empty_frozen_spec(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    manifest["frozen_packet"]["frozen_spec"] = "   \n"
    manifest["packet_hash"] = trial._sha256(manifest["frozen_packet"])
    results = _measured_results(tmp_path, manifest)
    with pytest.raises(ValueError, match="nonempty frozen_spec"):
        trial.publish_measured_recurring_trial(manifest, results, now=2000)
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "quarantine-feedback.db").exists()


def test_publish_measured_recurring_trial_refuses_bad_exp_id(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    manifest["frozen_packet"]["exp_id"] = "bad/id"
    manifest["packet_hash"] = trial._sha256(manifest["frozen_packet"])
    results = _measured_results(tmp_path, manifest)
    with pytest.raises(ValueError, match="valid strategy exp_id"):
        trial.publish_measured_recurring_trial(manifest, results, now=2000)
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "quarantine-feedback.db").exists()


def test_publish_measured_recurring_trial_refuses_forged_spec_seal(tmp_path, monkeypatch):
    state, experiments = _seal(tmp_path)
    orchestrator, workflows = _sources(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "quarantine-feedback.db")
    manifest = trial.build_current_recurring_trial(
        orchestrator,
        workflows,
        seed=14,
        now=1000,
        capacity_state="ok",
        state_dir=state,
        experiments_root=experiments,
    )
    manifest["frozen_packet"]["spec_sha256"] = "sha256:" + ("e" * 64)
    manifest["packet_hash"] = trial._sha256(manifest["frozen_packet"])
    results = _measured_results(tmp_path, manifest)
    with pytest.raises(ValueError, match="spec_sha256 does not seal"):
        trial.publish_measured_recurring_trial(manifest, results, now=2000)
    assert not (state / "capability-program" / "profile-trial.json").exists()
    assert not (tmp_path / "quarantine-feedback.db").exists()


def test_prepare_current_cli_refuses_a_missing_spec_without_writing(tmp_path, capsys):
    orchestrator, workflows = _sources(tmp_path)
    output = tmp_path / "outside" / "manifest.json"
    status = trial.main(
        [
            "prepare-current",
            "--orchestrator-root",
            str(orchestrator),
            "--workflows-root",
            str(workflows),
            "--output",
            str(output),
            "--state-dir",
            str(tmp_path / "state"),
            "--experiments-root",
            str(tmp_path / "experiments"),
        ]
    )
    assert status == 2
    assert "receipt is absent" in capsys.readouterr().err
    assert not output.exists()
