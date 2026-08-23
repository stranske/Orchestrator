from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

import execution_profiles
import feedback
import model_profile_trial
import model_profile_trial_bridge as bridge


def _manifest(tmp_path):
    orchestrator = tmp_path / "orchestrator"
    workflows = tmp_path / "workflows"
    orchestrator.mkdir()
    workflows.mkdir()
    (orchestrator / "control.py").write_text("CONTROL=True\n")
    (workflows / "worker.yml").write_text("worker: read-only\n")
    return model_profile_trial.build_trial_manifest(
        orchestrator, workflows, seed=14, now=1_000, capacity_state="ok"
    )


def _capacity():
    return {"generated_at": 1_000, "agents": {"codex": {"state": "ok"}}}


def _registries(tmp_path, *, safe=True):
    pinned_runner = "stranske/Workflows/.github/workflows/reusable-model-profile-trial.yml@" + (
        "1" * 40
    )
    profiles = {}
    for profile_id in model_profile_trial.EXPECTED_PROFILE_IDS:
        profile = execution_profiles.get_profile(profile_id)
        row = {
            "agent": "codex",
            "model": profile["requested_model"],
            "fallback_model": "gpt-5.5",
            "runner": "reusable-model-profile-trial",
            "capacity_pool": "codex-standard",
            "safety": "read-only" if safe else "standard",
            "lifecycle": "trial",
        }
        if safe:
            row.update(
                reasoning_effort="high",
                permission_mode="read-only",
                runner_ref=pinned_runner,
            )
        profiles[profile_id] = row
    registry = {"execution_profiles": profiles}
    if safe:
        registry["model_profile_trial_contract"] = {
            "mode": "read-only",
            "artifact_schema": bridge.REMOTE_ARTIFACT_SCHEMA,
            "identity_authority": bridge.REMOTE_ARTIFACT_AUTHORITY,
            "collector_identity_authority": bridge.REMOTE_IDENTITY_AUTHORITY,
            "runner_ref": pinned_runner,
        }
    models = {
        "models": [
            {
                "model_id": execution_profiles.get_profile(profile_id)["requested_model"],
                "worker_profile": True,
                "lifecycle": "trial",
            }
            for profile_id in model_profile_trial.EXPECTED_PROFILE_IDS
        ]
    }
    registry_path = tmp_path / "registry.json"
    models_path = tmp_path / "models.json"
    registry_path.write_text(json.dumps(registry))
    models_path.write_text(json.dumps(models))
    return registry_path, models_path


def _envelope(tmp_path, manifest, *, safe=True):
    registry, models = _registries(tmp_path, safe=safe)
    preflight = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        registry_path=registry,
        model_registry_path=models,
        capacity_snapshot=_capacity(),
        workflows_source_sha="3" * 40,
    )
    return preflight, bridge.build_request_envelope(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        preflight_result=preflight,
    )


def _transport_results(tmp_path, manifest, envelope):
    authority = (
        bridge.LOCAL_IDENTITY_AUTHORITY
        if envelope.get("transport") == "local"
        else bridge.REMOTE_IDENTITY_AUTHORITY
    )
    attempts = []
    for request in envelope["requests"]:
        github_run_id = 1_000 + int(request["launch_ordinal"])
        github_run_attempt = 1
        artifact_name = (
            f"model-profile-trial-{request['profile_id']}-{github_run_id}-"
            f"{github_run_attempt}-{request['launch_ordinal']}"
        )
        artifact = {
            "schema": bridge.REMOTE_ARTIFACT_SCHEMA,
            "version": 2,
            "trial_id": manifest["trial_id"],
            "request_id": request["request_id"],
            "request_hash": request["request_hash"],
            "run_id": request["run_id"],
            "profile_id": request["profile_id"],
            "packet_hash": manifest["packet_hash"],
            "acknowledged": True,
            "status": "success",
            "requested_model": request["requested_model"],
            "selected_model": request["requested_model"],
            "reported_model": request["requested_model"],
            "requested_reasoning_effort": "high",
            "reported_reasoning_effort": "high",
            "provider_resolved_provider": None,
            "provider_resolved_model": None,
            "fallback_reason": None,
            "runner_version": request["runner_ref"],
            "cli_version": "codex-cli 0.144.1",
            "thread_id": "thread-" + request["request_id"].split(":", 1)[1],
            "launch_ordinal": request["launch_ordinal"],
            "source_sha_before": request["expected_source_sha"],
            "source_sha_after": request["expected_source_sha"],
            "source_manifest_sha256_before": "sha256:" + ("b" * 64),
            "source_manifest_sha256_after": "sha256:" + ("b" * 64),
            "source_clean": True,
            "exit_code": 0,
            "identity_authority": bridge.REMOTE_ARTIFACT_AUTHORITY,
            "operation_role": "worker",
            "github_repository": bridge.REMOTE_REPOSITORY,
            "github_workflow_ref": bridge.REMOTE_WORKFLOW_REF,
            "github_workflow_sha": request["expected_source_sha"],
            "github_run_id": github_run_id,
            "github_run_attempt": github_run_attempt,
            "artifact_name": artifact_name,
        }
        artifact_path = tmp_path / f"{request['profile_id']}.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        artifact_sha = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        attempts.append(
            {
                "request_id": request["request_id"],
                "request_hash": request["request_hash"],
                "run_id": request["run_id"],
                "profile_id": request["profile_id"],
                "operation_role": "worker",
                "requested_model": request["requested_model"],
                "selected_model": request["requested_model"],
                "reported_model": request["requested_model"],
                "requested_reasoning_effort": "high",
                "reported_reasoning_effort": "high",
                "provider_resolved_provider": None,
                "provider_resolved_model": None,
                "fallback_reason": None,
                "runner_version": request["runner_ref"],
                "cli_version": "codex-cli 0.144.1",
                "status": "success",
                "exit_code": 0,
                "packet_hash": manifest["packet_hash"],
                "acknowledged": True,
                "artifact_ref": str(artifact_path),
                "artifact_sha256": artifact_sha,
                "identity_authority": authority,
                "launch_ordinal": request["launch_ordinal"],
                "source_sha_before": request["expected_source_sha"],
                "source_sha_after": request["expected_source_sha"],
                "source_manifest_sha256_before": "sha256:" + ("b" * 64),
                "source_manifest_sha256_after": "sha256:" + ("b" * 64),
                "source_clean": True,
                "github_repository": bridge.REMOTE_REPOSITORY,
                "github_workflow_ref": bridge.REMOTE_WORKFLOW_REF,
                "github_workflow_sha": request["expected_source_sha"],
                "github_run_id": github_run_id,
                "github_run_attempt": github_run_attempt,
                "github_artifact_id": 2_000 + int(request["launch_ordinal"]),
                "github_artifact_digest": "sha256:" + ("c" * 64),
                "artifact_name": artifact_name,
                "latency_s": 1.0,
                "tokens_in": 2,
                "tokens_out": 1,
            }
        )
    return {
        "schema": bridge.BRIDGE_RESULT_SCHEMA,
        "version": bridge.BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "envelope_hash": envelope["envelope_hash"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "attempts": attempts,
    }


def test_remote_preflight_requires_dedicated_read_only_contract(tmp_path):
    manifest = _manifest(tmp_path)
    preflight, envelope = _envelope(tmp_path, manifest, safe=False)
    assert preflight["ready"] is False
    assert envelope["dispatch_allowed"] is False
    assert any("reasoning_effort" in item for item in preflight["blockers"])
    assert "remote_read_only_trial_mode_missing" in preflight["blockers"]
    assert "remote_trial_artifact_contract_missing" in preflight["blockers"]


def test_safe_remote_preflight_and_envelope_are_replayable(tmp_path):
    manifest = _manifest(tmp_path)
    preflight, envelope = _envelope(tmp_path, manifest)
    assert preflight["ready"] is True
    assert envelope["dispatch_allowed"] is True
    assert preflight["capacity_reservation"]["snapshot_count"] == 1
    assert preflight["capacity_reservation"]["units"] == 3
    bridge.validate_envelope(envelope, manifest)
    assert len({row["request_id"] for row in envelope["requests"]}) == 3
    assert len({row["request_hash"] for row in envelope["requests"]}) == 3

    tampered = json.loads(json.dumps(envelope))
    tampered["requests"][0]["requested_model"] = "gpt-5.5"
    with pytest.raises(ValueError, match="not replayable"):
        bridge.validate_envelope(tampered, manifest)


def test_transport_ingest_requires_exact_identity_and_keeps_quarantine(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _preflight, envelope = _envelope(tmp_path, manifest)
    results = _transport_results(tmp_path, manifest, envelope)
    payload = bridge.ingest_transport_results(manifest, envelope, results)
    assert payload["acknowledged"] is True
    assert payload["schema"] == bridge.QUARANTINE_RESULT_SCHEMA
    assert payload["ready"] is False
    assert payload["stop_reason"] == "provider_resolved_identity_unavailable"
    assert {row["provider_resolved_model"] for row in payload["attempts"]} == {None}
    # Bridge ingestion creates a validated artifact only; it never writes Brain rows.
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    broken = json.loads(json.dumps(results))
    broken["attempts"][0]["reported_model"] = "gpt-5.5"
    broken["attempts"][0]["fallback_reason"] = "requested_model_rejected"
    with pytest.raises(ValueError, match="identity mismatch: reported_model"):
        bridge.ingest_transport_results(manifest, envelope, broken)


def test_failed_or_untrusted_attempt_stops_whole_trial(tmp_path):
    manifest = _manifest(tmp_path)
    _preflight, envelope = _envelope(tmp_path, manifest)
    results = _transport_results(tmp_path, manifest, envelope)
    results["attempts"][1]["status"] = "failed"
    results["attempts"][1]["exit_code"] = 1
    with pytest.raises(ValueError, match="transport attempt failed"):
        bridge.ingest_transport_results(manifest, envelope, results)

    results = _transport_results(tmp_path, manifest, envelope)
    results["attempts"][1]["identity_authority"] = "caller-assertion"
    with pytest.raises(ValueError, match="identity_authority"):
        bridge.ingest_transport_results(manifest, envelope, results)

    results = _transport_results(tmp_path, manifest, envelope)
    results["attempts"][1]["provider_resolved_model"] = "gpt-5.6-terra"
    results["attempts"][1]["provider_resolved_provider"] = "openai"
    with pytest.raises(ValueError, match="unsupported provider resolution"):
        bridge.ingest_transport_results(manifest, envelope, results)


def test_remote_collector_binds_authenticated_actions_metadata(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _preflight, envelope = _envelope(tmp_path, manifest)
    request = sorted(envelope["requests"], key=lambda row: row["launch_ordinal"])[0]
    fixture = _transport_results(tmp_path, manifest, envelope)
    attempt = next(row for row in fixture["attempts"] if row["request_id"] == request["request_id"])
    raw_artifact = Path(attempt["artifact_ref"]).read_bytes()
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("model-profile-trial-attempt.json", raw_artifact)

    def fake_json(endpoint):
        if endpoint.endswith(f"/actions/runs/{attempt['github_run_id']}"):
            return {
                "id": attempt["github_run_id"],
                "run_attempt": attempt["github_run_attempt"],
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": request["expected_source_sha"],
                "path": bridge.REMOTE_WORKFLOW_PATH,
                "status": "completed",
                "conclusion": "success",
            }
        return {
            "artifacts": [
                {
                    "id": attempt["github_artifact_id"],
                    "name": attempt["artifact_name"],
                    "expired": False,
                    "digest": "sha256:" + hashlib.sha256(archive_buffer.getvalue()).hexdigest(),
                    "workflow_run": {"head_sha": request["expected_source_sha"]},
                }
            ]
        }

    monkeypatch.setattr(bridge, "_gh_json", fake_json)
    monkeypatch.setattr(
        bridge, "_gh_download_artifact", lambda _artifact_id: archive_buffer.getvalue()
    )
    collected = bridge.collect_remote_attempt(
        manifest,
        envelope,
        request,
        github_run_id=attempt["github_run_id"],
        artifact_root=tmp_path / "collected",
    )
    assert collected["identity_authority"] == bridge.REMOTE_IDENTITY_AUTHORITY
    assert collected["github_artifact_id"] == attempt["github_artifact_id"]
    assert collected["github_artifact_digest"].startswith("sha256:")
    assert Path(collected["artifact_ref"]).is_file()


def test_instrumentation_completion_events_are_not_miner_input(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    feedback.record_run(
        "instrumentation-run",
        "owner/repo#1",
        "instrumentation:model_profile_trial",
        "codex",
        assignment="instrumentation",
        source="instrumentation",
    )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] > 0
        assert feedback.completion_event_episodes(conn=conn) == []


def test_transport_qualification_releases_only_profile_contract(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _preflight, envelope = _envelope(tmp_path, manifest)
    results = _transport_results(tmp_path, manifest, envelope)
    quarantine = bridge.ingest_transport_results(manifest, envelope, results)

    brain_path = tmp_path / "must-not-be-created" / "feedback.db"
    monkeypatch.setattr(feedback, "DB_PATH", brain_path)
    qualification = bridge.qualify_transport_contract(
        manifest,
        envelope,
        results,
        quarantine,
        evidence_hashes={"quarantine": "sha256:" + ("d" * 64)},
    )

    assert qualification["status"] == "qualified_transport_profile_contract_only"
    assert qualification["transport_contract_qualified"] is True
    assert qualification["cli_reported_profile_contract_qualified"] is True
    assert qualification["future_instrumentation_allowed"] is True
    assert qualification["provider_identity_status"] == "unavailable_unclaimed"
    assert qualification["provider_resolved_provider"] is None
    assert qualification["provider_resolved_model"] is None
    assert qualification["canary_quality_evidence"] is False
    assert qualification["learning_enabled"] is False
    assert qualification["brain_ingest_enabled"] is False
    assert qualification["quality_weight_updates_allowed"] is False
    assert qualification["promotion_allowed"] is False
    assert qualification["provider_attested_finalization_required"] is True
    assert not brain_path.exists()
    assert len(qualification["profile_evidence"]) == 3
    assert {row["fallback_observed"] for row in qualification["profile_evidence"]} == {False}

    replay = dict(qualification)
    supplied_hash = replay.pop("qualification_hash")
    assert supplied_hash == bridge._hash(replay)
    output = tmp_path / "qualification.json"
    output.write_text(json.dumps(qualification), encoding="utf-8")
    report = bridge.build_qualification_report(output)
    assert report["transport_contract_qualified"] is True
    assert report["provider_identity_status"] == "unavailable_unclaimed"


@pytest.mark.parametrize("tamper", ["source_manifest", "quarantine", "identity_artifact"])
def test_transport_qualification_rejects_tampering(tmp_path, tamper):
    manifest = _manifest(tmp_path)
    _preflight, envelope = _envelope(tmp_path, manifest)
    results = _transport_results(tmp_path, manifest, envelope)
    quarantine = bridge.ingest_transport_results(manifest, envelope, results)

    if tamper == "source_manifest":
        manifest["source_before"]["orchestrator"]["entries"][0]["sha256"] = "sha256:" + ("0" * 64)
        match = "aggregate mismatch"
    elif tamper == "quarantine":
        quarantine["attempts"][0]["reported_model"] = "gpt-5.5"
        match = "exact evidence replay"
    else:
        Path(results["attempts"][0]["artifact_ref"]).write_text("{}\n", encoding="utf-8")
        match = "artifact hash mismatch"

    with pytest.raises(ValueError, match=match):
        bridge.qualify_transport_contract(manifest, envelope, results, quarantine)


def test_qualify_cli_defaults_next_to_quarantine_and_report_rejects_tamper(tmp_path):
    manifest = _manifest(tmp_path)
    _preflight, envelope = _envelope(tmp_path, manifest)
    results = _transport_results(tmp_path, manifest, envelope)
    quarantine = bridge.ingest_transport_results(manifest, envelope, results)
    evidence = tmp_path / "accepted"
    evidence.mkdir()
    paths = {
        "manifest": evidence / "manifest.json",
        "envelope": evidence / "envelope.json",
        "results": evidence / "transport-results.json",
        "quarantine": evidence / "quarantine.json",
    }
    for name, payload in (
        ("manifest", manifest),
        ("envelope", envelope),
        ("results", results),
        ("quarantine", quarantine),
    ):
        paths[name].write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    command = [
        "qualify",
        "--manifest",
        str(paths["manifest"]),
        "--envelope",
        str(paths["envelope"]),
        "--results",
        str(paths["results"]),
        "--quarantine",
        str(paths["quarantine"]),
    ]
    assert bridge.main(command) == 0
    output = evidence / bridge.DEFAULT_QUALIFICATION_NAME
    assert output.is_file()
    sealed_bytes = output.read_bytes()
    sealed_mtime = output.stat().st_mtime_ns
    assert bridge.main(command) == 0
    assert output.read_bytes() == sealed_bytes
    assert output.stat().st_mtime_ns == sealed_mtime
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["evidence_hashes"]["quarantine"] == bridge._file_sha256(paths["quarantine"])

    payload["quality_weight_updates_allowed"] = True
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="already exists with different bytes"):
        bridge.main(command)
    report = bridge.build_qualification_report(output)
    assert report["status"] == "invalid_qualification"
    assert report["transport_contract_qualified"] is False
