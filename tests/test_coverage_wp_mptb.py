"""Hermetic regression coverage for model_profile_trial_bridge in work package mptb."""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import execution_profiles
import model_profile_trial
import model_profile_trial_bridge as bridge


def _reroot_home_paths(monkeypatch, new_home, anchor_module):
    """Move every import-time path under the real ~/.codex to `new_home`, wherever it is held.

    The Brain, the capability ledger, the repo-knowledge registry and the capacity ledgers fix their default
    location when their module is imported, both as module constants and as `path: Path = X` defaults, so
    setting ORCH_* variables inside a test moves none of them: run on the owner's machine these tests would
    write to the real state. Each such path is re-pointed at the same relative location under `new_home`.
    """
    live = Path.home() / ".codex"

    def moved(value):
        if isinstance(value, Path) and (value == live or live in value.parents):
            return new_home / value.relative_to(Path.home())
        return value

    src_dir = Path(anchor_module.__file__).resolve().parent
    for module in list(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if not module_file or Path(module_file).resolve().parent != src_dir:
            continue
        for name, value in list(vars(module).items()):
            if isinstance(value, Path) and moved(value) != value:
                monkeypatch.setattr(module, name, moved(value))
            elif inspect.isfunction(value) and value.__module__ == module.__name__:
                defaults = value.__defaults__ or ()
                if any(moved(d) != d for d in defaults):
                    monkeypatch.setattr(value, "__defaults__", tuple(moved(d) for d in defaults))
                kwdefaults = value.__kwdefaults__ or {}
                if any(moved(d) != d for d in kwdefaults.values()):
                    monkeypatch.setattr(
                        value, "__kwdefaults__", {k: moved(d) for k, d in kwdefaults.items()}
                    )


@pytest.fixture(autouse=True, scope="module")
def _live_paths_rerooted(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        new_home = tmp_path_factory.mktemp("home")
        _reroot_home_paths(patch, new_home, bridge)
        # `preflight` probes `<binary> --version` when the app-bundled Codex exists; on a host that has it
        # (or sets ORCH_CODEX_PROFILE_BIN) that would start the real CLI. Point it at a path that is absent.
        patch.setattr(bridge.adapters, "CODEX_PROFILE_BIN", new_home / "no-such-codex")
        yield


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Every target reads and writes only the test's disposable runtime."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    # `preflight` and `prepare` read the live capacity probe when no snapshot is passed, and the probe can
    # start `ccusage` or `npx -y ccusage@latest` (a process and the network) on any host that has them.
    # The seam is stubbed for every test; the ones that care pass an explicit snapshot.
    monkeypatch.setattr(
        bridge.capacity,
        "build",
        lambda *_args, **_kwargs: {"generated_at": 1_000, "agents": {"codex": {"state": "ok"}}},
    )
    # The pool-usage read defaults to the module's LEDGER path, fixed at import: on the owner's machine
    # that is the real capacity ledger. Point it at a file that does not exist.
    monkeypatch.setattr(bridge.capacity, "LEDGER", tmp_path / "capacity-ledger.ndjson")


def _manifest(tmp_path):
    orchestrator = tmp_path / "orchestrator"
    workflows = tmp_path / "workflows"
    orchestrator.mkdir(parents=True, exist_ok=True)
    workflows.mkdir(parents=True, exist_ok=True)
    (orchestrator / "control.py").write_text("CONTROL=True\n", encoding="utf-8")
    (workflows / "worker.yml").write_text("worker: read-only\n", encoding="utf-8")
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
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    models_path.write_text(json.dumps(models), encoding="utf-8")
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
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
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


def test_fleet_roots_with_env(monkeypatch, tmp_path):
    fleet = tmp_path / "fleet_dir"
    monkeypatch.setenv("ORCH_FLEET_ROOT", str(fleet))
    roots = bridge._fleet_roots()
    assert roots[0] == (fleet, "ORCH_FLEET_ROOT")


def test_resolve_workflows_root_existing(monkeypatch, tmp_path):
    fleet = tmp_path / "fleet"
    wf = fleet / "Workflows"
    wf.mkdir(parents=True)
    monkeypatch.setenv("ORCH_FLEET_ROOT", str(fleet))
    resolved_path, origin = bridge.resolve_workflows_root()
    assert resolved_path == wf
    assert origin == "ORCH_FLEET_ROOT"


def test_atomic_json_cleans_up_on_failure(monkeypatch, tmp_path):
    target = tmp_path / "sub" / "target.json"
    created_tmp = []
    real_mkstemp = tempfile.mkstemp

    def track_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created_tmp.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", track_mkstemp)

    def fail_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        bridge._atomic_json(target, {"key": "val"})
    assert len(created_tmp) == 1
    assert not os.path.exists(created_tmp[0])


def test_load_yaml_or_json_missing_file(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match=f"registry file missing: {re.escape(str(missing))}"):
        bridge._load_yaml_or_json(missing)


def test_load_yaml_or_json_yaml_file(tmp_path, monkeypatch):
    # CI's runner installs no PyYAML, so the parser is a stand-in: what is under test is that a non-.json
    # registry file is read as text and handed to ``yaml.safe_load``, and that its mapping is returned.
    seen = []

    class FakeYaml:
        @staticmethod
        def safe_load(text):
            seen.append(text)
            return {"key": "value", "count": 42}

    monkeypatch.setitem(sys.modules, "yaml", FakeYaml)
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text("key: value\ncount: 42\n", encoding="utf-8")
    loaded = bridge._load_yaml_or_json(yaml_file)
    assert loaded == {"key": "value", "count": 42}
    assert seen == ["key: value\ncount: 42\n"]


def test_load_yaml_or_json_non_dict_root(tmp_path):
    list_file = tmp_path / "array.json"
    list_file.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="registry root must be an object"):
        bridge._load_yaml_or_json(list_file)


def test_profiles_by_id_list_and_non_collection():
    rows = [
        {"profile_id": "prof-1", "name": "one"},
        {"id": "prof-2", "name": "two"},
        {"irrelevant": "none"},
    ]
    parsed = bridge._profiles_by_id(rows)
    assert set(parsed.keys()) == {"prof-1", "prof-2"}
    assert parsed["prof-1"]["name"] == "one"
    assert parsed["prof-2"]["name"] == "two"
    assert bridge._profiles_by_id("not-a-collection") == {}
    assert bridge._profiles_by_id(None) == {}


def test_cli_version_success_and_failure(monkeypatch, tmp_path):
    bin_path = tmp_path / "codex"
    bin_path.touch()

    def fake_run(args, capture_output=True, text=True, timeout=15):
        assert "--version" in args
        return SimpleNamespace(returncode=0, stdout="codex-cli 1.0.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert bridge._cli_version(bin_path) == "codex-cli 1.0.0"

    def fake_fail(args, capture_output=True, text=True, timeout=15):
        return SimpleNamespace(returncode=1, stdout="", stderr="error")

    monkeypatch.setattr(subprocess, "run", fake_fail)
    with pytest.raises(ValueError, match="app-bundled Codex CLI version probe failed"):
        bridge._cli_version(bin_path)


def test_git_source_sha_probes(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    expected_sha = "a" * 40

    def fake_run(cmd, capture_output=True, text=True, timeout=15):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=expected_sha + "\n", stderr="")
        if "status" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert bridge._git_source_sha(root) == expected_sha

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="error"),
    )
    with pytest.raises(ValueError, match="Workflows checkout has no immutable git HEAD"):
        bridge._git_source_sha(root)

    def fake_status_err(cmd, **kw):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=expected_sha, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="status fail")

    monkeypatch.setattr(subprocess, "run", fake_status_err)
    with pytest.raises(ValueError, match="Workflows checkout cleanliness probe failed"):
        bridge._git_source_sha(root)

    def fake_status_dirty(cmd, **kw):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=expected_sha, stderr="")
        return SimpleNamespace(returncode=0, stdout=" M file.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_status_dirty)
    with pytest.raises(ValueError, match="Workflows checkout is not clean"):
        bridge._git_source_sha(root)


def test_preflight_source_changed_and_pool_not_ready(tmp_path):
    manifest = _manifest(tmp_path)
    (tmp_path / "orchestrator" / "control.py").write_text("CONTROL=False\n", encoding="utf-8")
    registry, models = _registries(tmp_path, safe=True)
    shed_capacity = {"generated_at": 1000, "agents": {"codex": {"state": "shed"}}}
    res = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        registry_path=registry,
        model_registry_path=models,
        capacity_snapshot=shed_capacity,
        workflows_source_sha="3" * 40,
    )
    assert "source_integrity_changed" in res["blockers"]
    assert "shared_codex_pool_not_ready" in res["blockers"]
    assert res["ready"] is False


def test_preflight_local_transport_blockers(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    missing_bin = tmp_path / "nonexistent" / "codex"
    res1 = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="local",
        capacity_snapshot=_capacity(),
        codex_binary=missing_bin,
    )
    assert "version_capable_app_bundled_codex_missing" in res1["blockers"]

    real_bin = tmp_path / "fake_codex"
    real_bin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    real_bin.chmod(0o755)

    def fake_failing_version(_b):
        raise ValueError("probe failed")

    monkeypatch.setattr(bridge, "_cli_version", fake_failing_version)
    res2 = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="local",
        capacity_snapshot=_capacity(),
        codex_binary=real_bin,
    )
    assert "codex_cli_version_probe_failed" in res2["blockers"]

    monkeypatch.setattr(bridge, "_cli_version", lambda _b: "codex 1.0")
    real_get_profile = execution_profiles.get_profile

    def fake_get_profile(pid):
        p = dict(real_get_profile(pid))
        p["transport_support"] = ["remote"]
        return p

    monkeypatch.setattr(execution_profiles, "get_profile", fake_get_profile)
    res3 = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="local",
        capacity_snapshot=_capacity(),
        codex_binary=real_bin,
    )
    assert any("profile_not_local:" in b for b in res3["blockers"])


def test_preflight_remote_source_sha_and_registry_errors(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    registry, models = _registries(tmp_path, safe=True)
    res1 = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        registry_path=registry,
        model_registry_path=models,
        capacity_snapshot=_capacity(),
        workflows_source_sha="short_sha",
    )
    assert any("remote_source_not_immutable:" in b for b in res1["blockers"])

    bad_registry = tmp_path / "bad_registry.json"
    bad_registry.write_text("{corrupt json", encoding="utf-8")
    res2 = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        registry_path=bad_registry,
        model_registry_path=models,
        capacity_snapshot=_capacity(),
        workflows_source_sha="4" * 40,
    )
    assert any("remote_registry_unreadable:" in b for b in res2["blockers"])


def test_preflight_remote_profile_missing_and_model_registry_mismatch(tmp_path):
    manifest = _manifest(tmp_path)
    pinned_runner = "stranske/Workflows/.github/workflows/reusable-model-profile-trial.yml@" + (
        "1" * 40
    )
    first_profile = manifest["requests"][0]["profile_id"]
    registry = {
        "execution_profiles": {},
        "model_profile_trial_contract": {
            "mode": "read-only",
            "artifact_schema": bridge.REMOTE_ARTIFACT_SCHEMA,
            "identity_authority": bridge.REMOTE_ARTIFACT_AUTHORITY,
            "collector_identity_authority": bridge.REMOTE_IDENTITY_AUTHORITY,
            "runner_ref": pinned_runner,
        },
    }
    models = {
        "models": [
            {
                "model_id": execution_profiles.get_profile(first_profile)["requested_model"],
                "worker_profile": True,
                "lifecycle": "deprecated",
            }
        ]
    }
    reg_path = tmp_path / "reg_missing.json"
    reg_path.write_text(json.dumps(registry), encoding="utf-8")
    models_path = tmp_path / "models_mismatch.json"
    models_path.write_text(json.dumps(models), encoding="utf-8")

    res = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        registry_path=reg_path,
        model_registry_path=models_path,
        capacity_snapshot=_capacity(),
        workflows_source_sha="5" * 40,
    )
    assert any(f"remote_profile_missing:{first_profile}" in b for b in res["blockers"])

    registry_with_profile, _ = _registries(tmp_path, safe=True)
    res2 = bridge.preflight(
        manifest,
        artifact_root=tmp_path / "artifacts",
        transport="remote",
        registry_path=registry_with_profile,
        model_registry_path=models_path,
        capacity_snapshot=_capacity(),
        workflows_source_sha="5" * 40,
    )
    assert any(f"remote_model_registry_mismatch:{first_profile}" in b for b in res2["blockers"])


def test_preflight_invalid_transport(tmp_path):
    manifest = _manifest(tmp_path)
    with pytest.raises(ValueError, match="transport must be local or remote"):
        bridge.preflight(manifest, artifact_root=tmp_path / "artifacts", transport="invalid_mode")


def test_validate_envelope_errors(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)

    bad_schema = dict(envelope, schema="wrong-schema")
    with pytest.raises(ValueError, match="unsupported trial bridge envelope"):
        bridge.validate_envelope(bad_schema, manifest)

    bad_id = dict(envelope, trial_id="wrong-trial-id")
    with pytest.raises(ValueError, match="trial bridge identity mismatch"):
        bridge.validate_envelope(bad_id, manifest)

    bad_prof = json.loads(json.dumps(envelope))
    bad_prof["requests"][0]["profile_id"] = "unknown-prof"
    bad_prof["envelope_hash"] = bridge._hash(
        {k: v for k, v in bad_prof.items() if k != "envelope_hash"}
    )
    with pytest.raises(ValueError, match="trial bridge contains an unknown profile"):
        bridge.validate_envelope(bad_prof, manifest)

    bad_req_hash = json.loads(json.dumps(envelope))
    bad_req_hash["requests"][0]["request_hash"] = "sha256:" + ("0" * 64)
    bad_req_hash["envelope_hash"] = bridge._hash(
        {k: v for k, v in bad_req_hash.items() if k != "envelope_hash"}
    )
    with pytest.raises(ValueError, match="trial bridge request is not replayable"):
        bridge.validate_envelope(bad_req_hash, manifest)

    bad_model = json.loads(json.dumps(envelope))
    req0 = bad_model["requests"][0]
    req0["requested_model"] = "tampered-model"
    temp_row = {k: v for k, v in req0.items() if k not in ("request_id", "request_hash")}
    expected_hash = bridge._hash(temp_row)
    req0["request_hash"] = expected_hash
    req0["request_id"] = "trial-request:" + expected_hash.split(":", 1)[1][:24]
    bad_model["envelope_hash"] = bridge._hash(
        {k: v for k, v in bad_model.items() if k != "envelope_hash"}
    )
    with pytest.raises(ValueError, match="trial bridge request model drifted"):
        bridge.validate_envelope(bad_model, manifest)


def test_identity_artifact_file_and_json_errors(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)
    request = envelope["requests"][0]
    attempt = res["attempts"][0]

    bad_ref_attempt = dict(attempt, artifact_ref=str(tmp_path / "nonexistent.json"))
    with pytest.raises(
        ValueError, match="transport identity artifact is not a local downloaded file"
    ):
        bridge._identity_artifact(manifest, envelope, request, bad_ref_attempt)

    big_file = tmp_path / "big.json"
    big_file.write_bytes(b" " * (65 * 1024))
    big_attempt = dict(attempt, artifact_ref=str(big_file))
    with pytest.raises(ValueError, match="transport identity artifact exceeds 64KiB"):
        bridge._identity_artifact(manifest, envelope, request, big_attempt)

    bad_json_file = tmp_path / "bad.json"
    bad_json_file.write_bytes(b"not json")
    bad_json_attempt = dict(
        attempt,
        artifact_ref=str(bad_json_file),
        artifact_sha256=bridge._file_sha256(bad_json_file),
    )
    with pytest.raises(ValueError, match="transport identity artifact is not strict JSON"):
        bridge._identity_artifact(manifest, envelope, request, bad_json_attempt)

    list_json_file = tmp_path / "list.json"
    list_json_file.write_bytes(b"[1, 2, 3]\n")
    list_attempt = dict(
        attempt,
        artifact_ref=str(list_json_file),
        artifact_sha256=bridge._file_sha256(list_json_file),
    )
    with pytest.raises(ValueError, match="transport identity artifact root is not an object"):
        bridge._identity_artifact(manifest, envelope, request, list_attempt)

    artifact_data = json.loads(Path(attempt["artifact_ref"]).read_text(encoding="utf-8"))
    artifact_data["extra_unsupported_field"] = "bad"
    extra_file = tmp_path / "extra.json"
    extra_file.write_text(json.dumps(artifact_data), encoding="utf-8")
    extra_attempt = dict(
        attempt, artifact_ref=str(extra_file), artifact_sha256=bridge._file_sha256(extra_file)
    )
    with pytest.raises(ValueError, match="transport identity artifact contains unsupported fields"):
        bridge._identity_artifact(manifest, envelope, request, extra_attempt)


def test_identity_artifact_expected_field_mismatch(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)
    request = envelope["requests"][0]
    attempt = res["attempts"][0]

    artifact_data = json.loads(Path(attempt["artifact_ref"]).read_text(encoding="utf-8"))
    artifact_data["status"] = "failed"
    mismatch_file = tmp_path / "mismatch.json"
    mismatch_file.write_text(json.dumps(artifact_data), encoding="utf-8")
    mismatch_attempt = dict(
        attempt,
        artifact_ref=str(mismatch_file),
        artifact_sha256=bridge._file_sha256(mismatch_file),
    )
    with pytest.raises(ValueError, match="transport identity artifact mismatch: status:"):
        bridge._identity_artifact(manifest, envelope, request, mismatch_attempt)


def test_identity_artifact_local_missing_thread_id(tmp_path):
    manifest = _manifest(tmp_path)
    local_envelope = {
        "schema": bridge.BRIDGE_SCHEMA,
        "version": bridge.BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "transport": "local",
        "requests": [
            {
                "run_id": manifest["requests"][0]["run_id"],
                "profile_id": manifest["requests"][0]["profile_id"],
                "launch_ordinal": 1,
                "requested_model": manifest["requests"][0]["requested_model"],
                "reasoning_effort": manifest["requests"][0]["reasoning_effort"],
                "request_id": "trial-request:test1",
                "request_hash": "sha256:test1",
            }
        ],
    }
    request = local_envelope["requests"][0]
    artifact = {
        "schema": bridge.LOCAL_ARTIFACT_SCHEMA,
        "version": 1,
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
        "requested_reasoning_effort": request["reasoning_effort"],
        "reported_reasoning_effort": request["reasoning_effort"],
        "provider_resolved_provider": None,
        "provider_resolved_model": None,
        "fallback_reason": None,
        "runner_version": "v1",
        "cli_version": "v1",
        "thread_id": "",
    }
    art_file = tmp_path / "local_art.json"
    art_file.write_text(json.dumps(artifact), encoding="utf-8")
    attempt = {
        "artifact_ref": str(art_file),
        "artifact_sha256": bridge._file_sha256(art_file),
        "runner_version": "v1",
        "cli_version": "v1",
    }
    with pytest.raises(ValueError, match="local session identity artifact missing thread_id"):
        bridge._identity_artifact(manifest, local_envelope, request, attempt)


def test_identity_artifact_remote_manifest_and_provenance_errors(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)
    request = envelope["requests"][0]
    attempt = res["attempts"][0]

    art1 = json.loads(Path(attempt["artifact_ref"]).read_text(encoding="utf-8"))
    art1["source_manifest_sha256_after"] = "sha256:" + ("0" * 64)
    f1 = tmp_path / "art1.json"
    f1.write_text(json.dumps(art1), encoding="utf-8")
    att1 = dict(attempt, artifact_ref=str(f1), artifact_sha256=bridge._file_sha256(f1))
    with pytest.raises(ValueError, match="transport source manifest mismatch:"):
        bridge._identity_artifact(manifest, envelope, request, att1)

    art2 = json.loads(Path(attempt["artifact_ref"]).read_text(encoding="utf-8"))
    art2["github_run_id"] = 0
    f2 = tmp_path / "art2.json"
    f2.write_text(json.dumps(art2), encoding="utf-8")
    att2 = dict(
        attempt,
        artifact_ref=str(f2),
        artifact_sha256=bridge._file_sha256(f2),
        github_run_id=0,
    )
    with pytest.raises(ValueError, match="transport GitHub run provenance missing:"):
        bridge._identity_artifact(manifest, envelope, request, att2)


def test_gh_json_probes(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"hello": "world"}', stderr=""),
    )
    assert bridge._gh_json("test/endpoint") == {"hello": "world"}

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="gh error"),
    )
    with pytest.raises(ValueError, match="GitHub Actions provenance query failed: gh error"):
        bridge._gh_json("test/endpoint")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    with pytest.raises(ValueError, match="GitHub Actions provenance response was not JSON"):
        bridge._gh_json("test/endpoint")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[1, 2, 3]", stderr=""),
    )
    with pytest.raises(ValueError, match="GitHub Actions provenance response was not an object"):
        bridge._gh_json("test/endpoint")


def test_gh_download_artifact_probes(monkeypatch):
    fake_zip = b"fake zip content"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=fake_zip, stderr=b""),
    )
    assert bridge._gh_download_artifact(123) == fake_zip

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"download 404"),
    )
    with pytest.raises(ValueError, match="GitHub Actions artifact download failed: download 404"):
        bridge._gh_download_artifact(123)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    with pytest.raises(ValueError, match="GitHub Actions artifact archive size is invalid"):
        bridge._gh_download_artifact(123)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"x" * (257 * 1024), stderr=b""),
    )
    with pytest.raises(ValueError, match="GitHub Actions artifact archive size is invalid"):
        bridge._gh_download_artifact(123)


def test_artifact_json_from_zip_probes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("model-profile-trial-attempt.json", b'{"status": "ok"}')
    assert bridge._artifact_json_from_zip(buf.getvalue()) == b'{"status": "ok"}'

    with pytest.raises(ValueError, match="GitHub Actions trial artifact was not a valid ZIP"):
        bridge._artifact_json_from_zip(b"not a zip file")

    buf_wrong = io.BytesIO()
    with zipfile.ZipFile(buf_wrong, "w") as z:
        z.writestr("wrong_name.json", b"{}")
    with pytest.raises(ValueError, match="trial artifact archive must contain one exact JSON file"):
        bridge._artifact_json_from_zip(buf_wrong.getvalue())

    buf_large = io.BytesIO()
    with zipfile.ZipFile(buf_large, "w") as z:
        z.writestr("model-profile-trial-attempt.json", b" " * (65 * 1024))
    with pytest.raises(ValueError, match="trial artifact JSON exceeds 64KiB"):
        bridge._artifact_json_from_zip(buf_large.getvalue())


def test_collect_remote_attempt_errors(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    request = envelope["requests"][0]

    local_env = dict(envelope, transport="local")
    with pytest.raises(ValueError, match="remote collection requires a remote bridge envelope"):
        bridge.collect_remote_attempt(
            manifest, local_env, request, github_run_id=1001, artifact_root=tmp_path / "art"
        )

    run_id = 1001
    valid_run = {
        "id": run_id,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": request["expected_source_sha"],
        "path": bridge.REMOTE_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
    }
    expected_name = (
        f"model-profile-trial-{request['profile_id']}-{run_id}-" f"1-{request['launch_ordinal']}"
    )

    bad_run = dict(valid_run, conclusion="failure")
    monkeypatch.setattr(bridge, "_gh_json", lambda ep: bad_run)
    with pytest.raises(ValueError, match="GitHub Actions run provenance mismatch: conclusion"):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    incomplete_run = dict(valid_run, run_attempt=0)
    monkeypatch.setattr(bridge, "_gh_json", lambda ep: incomplete_run)
    with pytest.raises(ValueError, match="GitHub Actions run identity is incomplete"):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    def gh_no_artifacts(ep):
        if "artifacts" in ep:
            return {"artifacts": []}
        return valid_run

    monkeypatch.setattr(bridge, "_gh_json", gh_no_artifacts)
    with pytest.raises(
        ValueError, match="GitHub Actions run did not publish one exact trial artifact"
    ):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    def gh_bad_artifact_id(ep):
        if "artifacts" in ep:
            return {
                "artifacts": [
                    {
                        "id": 0,
                        "name": expected_name,
                        "expired": False,
                        "workflow_run": {"head_sha": request["expected_source_sha"]},
                    }
                ]
            }
        return valid_run

    monkeypatch.setattr(bridge, "_gh_json", gh_bad_artifact_id)
    with pytest.raises(ValueError, match="GitHub Actions artifact provenance mismatch"):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("model-profile-trial-attempt.json", b'{"key": "val"}')
    zip_bytes = buf.getvalue()

    def gh_art_listing(ep):
        if "artifacts" in ep:
            return {
                "artifacts": [
                    {
                        "id": 50,
                        "name": expected_name,
                        "expired": False,
                        "digest": "sha256:" + ("f" * 64),
                        "workflow_run": {"head_sha": request["expected_source_sha"]},
                    }
                ]
            }
        return valid_run

    monkeypatch.setattr(bridge, "_gh_json", gh_art_listing)
    monkeypatch.setattr(bridge, "_gh_download_artifact", lambda _aid: zip_bytes)
    with pytest.raises(ValueError, match="GitHub Actions artifact archive digest mismatch"):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    buf_bad_json = io.BytesIO()
    with zipfile.ZipFile(buf_bad_json, "w") as z:
        z.writestr("model-profile-trial-attempt.json", b"not json")
    bad_json_zip = buf_bad_json.getvalue()
    bad_json_digest = "sha256:" + hashlib.sha256(bad_json_zip).hexdigest()

    def gh_art_bad_json(ep):
        if "artifacts" in ep:
            return {
                "artifacts": [
                    {
                        "id": 50,
                        "name": expected_name,
                        "expired": False,
                        "digest": bad_json_digest,
                        "workflow_run": {"head_sha": request["expected_source_sha"]},
                    }
                ]
            }
        return valid_run

    monkeypatch.setattr(bridge, "_gh_json", gh_art_bad_json)
    monkeypatch.setattr(bridge, "_gh_download_artifact", lambda _aid: bad_json_zip)
    with pytest.raises(ValueError, match="GitHub Actions trial artifact was not strict JSON"):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    buf_list = io.BytesIO()
    with zipfile.ZipFile(buf_list, "w") as z:
        z.writestr("model-profile-trial-attempt.json", b"[1, 2]")
    list_zip = buf_list.getvalue()
    list_digest = "sha256:" + hashlib.sha256(list_zip).hexdigest()

    def gh_art_list(ep):
        if "artifacts" in ep:
            return {
                "artifacts": [
                    {
                        "id": 50,
                        "name": expected_name,
                        "expired": False,
                        "digest": list_digest,
                        "workflow_run": {"head_sha": request["expected_source_sha"]},
                    }
                ]
            }
        return valid_run

    monkeypatch.setattr(bridge, "_gh_json", gh_art_list)
    monkeypatch.setattr(bridge, "_gh_download_artifact", lambda _aid: list_zip)
    with pytest.raises(ValueError, match="GitHub Actions trial artifact root was not an object"):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )

    art_payload = {
        "github_repository": "wrong/repo",
        "github_workflow_ref": bridge.REMOTE_WORKFLOW_REF,
        "github_workflow_sha": request["expected_source_sha"],
        "github_run_id": run_id,
        "github_run_attempt": 1,
        "artifact_name": expected_name,
    }
    buf_mismatch = io.BytesIO()
    with zipfile.ZipFile(buf_mismatch, "w") as z:
        z.writestr("model-profile-trial-attempt.json", json.dumps(art_payload).encode("utf-8"))
    mismatch_zip = buf_mismatch.getvalue()
    mismatch_digest = "sha256:" + hashlib.sha256(mismatch_zip).hexdigest()

    def gh_art_mismatch(ep):
        if "artifacts" in ep:
            return {
                "artifacts": [
                    {
                        "id": 50,
                        "name": expected_name,
                        "expired": False,
                        "digest": mismatch_digest,
                        "workflow_run": {"head_sha": request["expected_source_sha"]},
                    }
                ]
            }
        return valid_run

    monkeypatch.setattr(bridge, "_gh_json", gh_art_mismatch)
    monkeypatch.setattr(bridge, "_gh_download_artifact", lambda _aid: mismatch_zip)
    with pytest.raises(
        ValueError, match="GitHub Actions artifact identity mismatch: github_repository"
    ):
        bridge.collect_remote_attempt(
            manifest, envelope, request, github_run_id=run_id, artifact_root=tmp_path / "art"
        )


def test_validate_transport_results_field_and_header_errors(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)

    bad_results_field = dict(res, extra_field="bad")
    with pytest.raises(ValueError, match="transport results contain unsupported fields:"):
        bridge._validate_transport_results(manifest, envelope, bad_results_field)

    bad_schema = dict(res, schema="bad-schema")
    with pytest.raises(ValueError, match="unsupported transport result schema"):
        bridge._validate_transport_results(manifest, envelope, bad_schema)

    bad_trial_id = dict(res, trial_id="wrong-trial")
    with pytest.raises(ValueError, match="transport result identity mismatch"):
        bridge._validate_transport_results(manifest, envelope, bad_trial_id)

    bad_ack = dict(res, acknowledged=False)
    with pytest.raises(ValueError, match="transport did not acknowledge the frozen packet"):
        bridge._validate_transport_results(manifest, envelope, bad_ack)

    bad_attempts_count = dict(res, attempts=res["attempts"][:1])
    with pytest.raises(ValueError, match="transport results require one exact attempt per request"):
        bridge._validate_transport_results(manifest, envelope, bad_attempts_count)

    bad_attempt_extra = json.loads(json.dumps(res))
    bad_attempt_extra["attempts"][0]["extra_bogus"] = 123
    with pytest.raises(ValueError, match="transport attempt contains unsupported fields:"):
        bridge._validate_transport_results(manifest, envelope, bad_attempt_extra)


def test_validate_transport_results_attempt_detail_errors(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)

    r1 = json.loads(json.dumps(res))
    r1["attempts"][0]["fallback_reason"] = "rate_limited"
    with pytest.raises(ValueError, match="transport fallback stopped trial:"):
        bridge._validate_transport_results(manifest, envelope, r1)

    r2 = json.loads(json.dumps(res))
    r2["attempts"][0]["acknowledged"] = False
    with pytest.raises(ValueError, match="transport packet acknowledgement missing:"):
        bridge._validate_transport_results(manifest, envelope, r2)

    r3 = json.loads(json.dumps(res))
    r3["attempts"][0]["cli_version"] = ""
    with pytest.raises(ValueError, match="transport version evidence missing:"):
        bridge._validate_transport_results(manifest, envelope, r3)

    r4 = json.loads(json.dumps(res))
    r4["attempts"][0]["artifact_sha256"] = "invalid_hash"
    with pytest.raises(ValueError, match="transport artifact evidence missing:"):
        bridge._validate_transport_results(manifest, envelope, r4)

    r5 = json.loads(json.dumps(res))
    r5["attempts"][0]["source_manifest_sha256_after"] = "sha256:" + ("0" * 64)
    with pytest.raises(ValueError, match="transport source manifest mismatch:"):
        bridge._validate_transport_results(manifest, envelope, r5)

    r6 = json.loads(json.dumps(res))
    r6["attempts"][0]["github_run_attempt"] = 0
    with pytest.raises(ValueError, match="transport GitHub run provenance missing:"):
        bridge._validate_transport_results(manifest, envelope, r6)

    r7 = json.loads(json.dumps(res))
    r7["attempts"][0]["github_artifact_id"] = 0
    with pytest.raises(ValueError, match="transport GitHub artifact provenance missing:"):
        bridge._validate_transport_results(manifest, envelope, r7)


def test_ingest_transport_results_source_tampered_fails(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)
    (tmp_path / "orchestrator" / "control.py").write_text("CONTROL=tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source integrity changed before transport ingest"):
        bridge.ingest_transport_results(manifest, envelope, res)


def test_validate_sealed_source_snapshots_errors(tmp_path):
    manifest = _manifest(tmp_path)

    m1 = json.loads(json.dumps(manifest))
    m1["source_before"]["orchestrator"]["entries"] = []
    with pytest.raises(ValueError, match="sealed source manifest has no entries:orchestrator"):
        bridge._validate_sealed_source_snapshots(m1)

    m2 = json.loads(json.dumps(manifest))
    m2["source_before"]["orchestrator"]["entries"][0]["bogus_key"] = 1
    with pytest.raises(ValueError, match="sealed source manifest entry is invalid:orchestrator"):
        bridge._validate_sealed_source_snapshots(m2)

    m3 = json.loads(json.dumps(manifest))
    m3["source_before"]["orchestrator"]["entries"][0]["path"] = "../secret"
    with pytest.raises(ValueError, match="sealed source manifest path is unsafe:orchestrator"):
        bridge._validate_sealed_source_snapshots(m3)

    m4 = json.loads(json.dumps(manifest))
    m4["source_before"]["orchestrator"]["entries"][0]["sha256"] = "invalid_digest"
    with pytest.raises(ValueError, match="sealed source manifest digest is invalid:orchestrator"):
        bridge._validate_sealed_source_snapshots(m4)

    m5 = json.loads(json.dumps(manifest))
    m5["source_before"]["orchestrator"]["entries"][0]["bytes"] = -1
    with pytest.raises(ValueError, match="sealed source manifest size is invalid:orchestrator"):
        bridge._validate_sealed_source_snapshots(m5)

    m6 = json.loads(json.dumps(manifest))
    entry0 = m6["source_before"]["orchestrator"]["entries"][0]
    m6["source_before"]["orchestrator"]["entries"].append(dict(entry0))
    with pytest.raises(
        ValueError, match="sealed source manifest contains duplicate paths:orchestrator"
    ):
        bridge._validate_sealed_source_snapshots(m6)

    m7 = json.loads(json.dumps(manifest))
    m7["source_before"]["orchestrator"]["file_count"] = 999
    with pytest.raises(ValueError, match="sealed source manifest file count mismatch:orchestrator"):
        bridge._validate_sealed_source_snapshots(m7)


def _rehash_envelope(envelope):
    env = json.loads(json.dumps(envelope))
    for row in env.get("requests", []):
        r = dict(row)
        r.pop("request_id", None)
        r.pop("request_hash", None)
        h = bridge._hash(r)
        row["request_hash"] = h
        row["request_id"] = "trial-request:" + h.split(":", 1)[1][:24]
    e = dict(env)
    e.pop("envelope_hash", None)
    env["envelope_hash"] = bridge._hash(e)
    return env


def test_qualify_transport_contract_errors(tmp_path):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)
    quarantine = bridge.ingest_transport_results(manifest, envelope, res)

    env1 = _rehash_envelope(dict(envelope, transport="local"))
    with pytest.raises(ValueError, match="transport qualification requires remote evidence"):
        bridge.qualify_transport_contract(manifest, env1, res, quarantine)

    env2 = _rehash_envelope(dict(envelope, dispatch_allowed=False))
    with pytest.raises(
        ValueError, match="transport qualification requires a passing sealed preflight"
    ):
        bridge.qualify_transport_contract(manifest, env2, res, quarantine)

    env3 = json.loads(json.dumps(envelope))
    env3["preflight"]["source_integrity"]["unchanged"] = False
    env3 = _rehash_envelope(env3)
    with pytest.raises(ValueError, match="transport qualification source preflight was not clean"):
        bridge.qualify_transport_contract(manifest, env3, res, quarantine)

    env4 = json.loads(json.dumps(envelope))
    env4["preflight"]["remote_runner_ref"] = "unpinned-runner"
    env4 = _rehash_envelope(env4)
    with pytest.raises(ValueError, match="transport qualification runner is not commit-pinned"):
        bridge.qualify_transport_contract(manifest, env4, res, quarantine)

    env5 = json.loads(json.dumps(envelope))
    env5["preflight"]["workflows_source_sha"] = "short-sha"
    env5 = _rehash_envelope(env5)
    with pytest.raises(
        ValueError, match="transport qualification Workflows source is not immutable"
    ):
        bridge.qualify_transport_contract(manifest, env5, res, quarantine)

    env6 = json.loads(json.dumps(envelope))
    env6["requests"][0]["runner_ref"] = "different-runner@" + ("1" * 40)
    env6 = _rehash_envelope(env6)
    with pytest.raises(ValueError, match="transport qualification request runner pin drifted"):
        bridge.qualify_transport_contract(manifest, env6, res, quarantine)

    env7 = json.loads(json.dumps(envelope))
    env7["requests"][0]["expected_source_sha"] = "9" * 40
    env7 = _rehash_envelope(env7)
    with pytest.raises(ValueError, match="transport qualification request source pin drifted"):
        bridge.qualify_transport_contract(manifest, env7, res, quarantine)

    bad_quarantine = dict(quarantine)
    bad_quarantine.pop("status")
    with pytest.raises(ValueError, match="quarantine artifact fields are not exact"):
        bridge.qualify_transport_contract(manifest, envelope, res, bad_quarantine)

    res_dup = json.loads(json.dumps(res))
    res_dup["attempts"][1]["github_run_id"] = res_dup["attempts"][0]["github_run_id"]
    art1 = json.loads(Path(res_dup["attempts"][1]["artifact_ref"]).read_text(encoding="utf-8"))
    art1["github_run_id"] = res_dup["attempts"][0]["github_run_id"]
    f1 = tmp_path / "art_dup.json"
    f1.write_text(json.dumps(art1), encoding="utf-8")
    res_dup["attempts"][1]["artifact_ref"] = str(f1)
    res_dup["attempts"][1]["artifact_sha256"] = bridge._file_sha256(f1)
    quar_dup = bridge.ingest_transport_results(manifest, envelope, res_dup)
    with pytest.raises(
        ValueError,
        match="transport qualification requires one distinct remote attempt per instance",
    ):
        bridge.qualify_transport_contract(manifest, envelope, res_dup, quar_dup)

    res_disagree = json.loads(json.dumps(res))
    res_disagree["attempts"][1]["source_manifest_sha256_before"] = "sha256:" + ("d" * 64)
    res_disagree["attempts"][1]["source_manifest_sha256_after"] = "sha256:" + ("d" * 64)
    art2 = json.loads(Path(res_disagree["attempts"][1]["artifact_ref"]).read_text(encoding="utf-8"))
    art2["source_manifest_sha256_before"] = "sha256:" + ("d" * 64)
    art2["source_manifest_sha256_after"] = "sha256:" + ("d" * 64)
    f2 = tmp_path / "art_disagree.json"
    f2.write_text(json.dumps(art2), encoding="utf-8")
    res_disagree["attempts"][1]["artifact_ref"] = str(f2)
    res_disagree["attempts"][1]["artifact_sha256"] = bridge._file_sha256(f2)
    quar_disagree = bridge.ingest_transport_results(manifest, envelope, res_disagree)
    with pytest.raises(ValueError, match="transport qualification source attestations disagree"):
        bridge.qualify_transport_contract(manifest, envelope, res_disagree, quar_disagree)


def test_default_qualification_report_path(monkeypatch, tmp_path):
    configured = tmp_path / "custom" / "report.json"
    monkeypatch.setenv("ORCH_MODEL_PROFILE_TRANSPORT_QUALIFICATION", str(configured))
    assert bridge._default_qualification_report_path() == configured

    monkeypatch.delenv("ORCH_MODEL_PROFILE_TRANSPORT_QUALIFICATION", raising=False)
    artifact_root = tmp_path / "artifacts"
    sub1 = artifact_root / "trial1"
    sub2 = artifact_root / "trial2"
    sub1.mkdir(parents=True)
    sub2.mkdir(parents=True)
    file1 = sub1 / bridge.DEFAULT_QUALIFICATION_NAME
    file2 = sub2 / bridge.DEFAULT_QUALIFICATION_NAME
    file1.touch()
    file2.touch()
    monkeypatch.setattr(bridge, "DEFAULT_ARTIFACT_ROOT", artifact_root)
    # Explicit mtimes, in both orders: the newest report wins whichever directory holds it, and the answer
    # cannot come from creation order, listing order or a filesystem with coarse timestamps.
    older, newer = 1_000_000_000_000_000_000, 2_000_000_000_000_000_000
    os.utime(file1, ns=(older, older))
    os.utime(file2, ns=(newer, newer))
    assert bridge._default_qualification_report_path() == file2
    os.utime(file1, ns=(newer, newer))
    os.utime(file2, ns=(older, older))
    assert bridge._default_qualification_report_path() == file1


def test_selftest_workflows_root_location_independent(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    home_wf = fake_home / "Library/CloudStorage/Dropbox/Learning/Code/Workflows"
    home_wf.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("ORCH_FLEET_ROOT", str(tmp_path / "prior_fleet"))
    bridge._selftest_workflows_root_location_independent()

    fake_home_empty = tmp_path / "home_empty"
    fake_home_empty.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home_empty)
    monkeypatch.delenv("ORCH_FLEET_ROOT", raising=False)
    bridge._selftest_workflows_root_location_independent()


def test_selftest_direct(capsys):
    bridge._selftest()
    captured = capsys.readouterr()
    assert "model_profile_trial_bridge.py selftest: OK" in captured.out


def test_main_cli_subcommands(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    _pre, envelope = _envelope(tmp_path, manifest)
    res = _transport_results(tmp_path, manifest, envelope)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(res), encoding="utf-8")
    registry, models = _registries(tmp_path, safe=True)

    assert bridge.main(["selftest"]) == 0

    pre_out = bridge.main(
        [
            "preflight",
            "--manifest",
            str(manifest_path),
            "--transport",
            "remote",
            "--registry",
            str(registry),
            "--model-registry",
            str(models),
            "--workflows-source-sha",
            "3" * 40,
        ]
    )
    assert pre_out == 0

    prep_out_path = tmp_path / "prepared_envelope.json"
    assert (
        bridge.main(
            [
                "prepare",
                "--manifest",
                str(manifest_path),
                "--transport",
                "remote",
                "--registry",
                str(registry),
                "--model-registry",
                str(models),
                "--workflows-source-sha",
                "3" * 40,
                "--output",
                str(prep_out_path),
            ]
        )
        == 0
    )
    assert prep_out_path.is_file()

    def fake_collect(_m, _e, run_ids, artifact_root):
        return {"collected": run_ids}

    monkeypatch.setattr(bridge, "collect_remote_results", fake_collect)
    collect_out_path = tmp_path / "collected.json"
    assert (
        bridge.main(
            [
                "collect-remote",
                "--manifest",
                str(manifest_path),
                "--envelope",
                str(envelope_path),
                "--run-id",
                "1001",
                "--run-id",
                "1002",
                "--run-id",
                "1003",
                "--output",
                str(collect_out_path),
            ]
        )
        == 0
    )
    assert collect_out_path.is_file()

    quarantine = bridge.ingest_transport_results(manifest, envelope, res)
    quarantine_path = tmp_path / "quarantine.json"
    quarantine_path.write_text(json.dumps(quarantine), encoding="utf-8")
    with pytest.raises(ValueError, match="qualification output may not overwrite evidence input"):
        bridge.main(
            [
                "qualify",
                "--manifest",
                str(manifest_path),
                "--envelope",
                str(envelope_path),
                "--results",
                str(results_path),
                "--quarantine",
                str(quarantine_path),
                "--output",
                str(quarantine_path),
            ]
        )

    ingest_out_path = tmp_path / "ingest_out.json"
    assert (
        bridge.main(
            [
                "ingest",
                "--manifest",
                str(manifest_path),
                "--envelope",
                str(envelope_path),
                "--results",
                str(results_path),
                "--output",
                str(ingest_out_path),
            ]
        )
        == 0
    )
    assert ingest_out_path.is_file()
