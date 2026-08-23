#!/usr/bin/env python3
"""Fail-closed transport bridge for the Sol/Terra/Luna profile trial.

The bridge prepares replayable local or Workflows requests, but never dispatches
remote work.  A local canary is quarantined outside every source root and is
guarded by an environment flag plus exact trial-id confirmation.  Transport
results become ``model_profile_trial`` results only after strict identity,
artifact, status, packet, and source-integrity checks.  Brain ingestion is a
separate concern and remains disabled here.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import adapters
import capacity
import execution_profiles
import model_profile_trial

BRIDGE_SCHEMA = "orchestrator.model-profile-trial-bridge"
BRIDGE_RESULT_SCHEMA = "orchestrator.model-profile-trial-bridge-results"
QUARANTINE_RESULT_SCHEMA = "orchestrator.model-profile-trial-quarantine-results"
QUALIFICATION_SCHEMA = "orchestrator.model-profile-transport-qualification"
BRIDGE_VERSION = 1
RUNNER_VERSION = "orchestrator/model-profile-trial-bridge@1"
REMOTE_ARTIFACT_SCHEMA = "workflows.model-profile-trial-result/v2"
LOCAL_ARTIFACT_SCHEMA = "orchestrator.codex-session-identity/v1"
REMOTE_IDENTITY_AUTHORITY = "github-actions-api/workflows-read-only-trial-artifact/v2"
REMOTE_ARTIFACT_AUTHORITY = "workflows-read-only-trial-artifact/v2"
LOCAL_IDENTITY_AUTHORITY = "codex-local-session-turn-context/v1"
REMOTE_REPOSITORY = "stranske/Workflows"
REMOTE_WORKFLOW_PATH = ".github/workflows/agents-model-profile-trial.yml"
REMOTE_WORKFLOW_REF = f"{REMOTE_REPOSITORY}/{REMOTE_WORKFLOW_PATH}@refs/heads/main"
REMOTE_POOL_MAP = {"codex-subscription": "codex-standard"}
REMOTE_FALLBACK = "gpt-5.5"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PINNED_REF_RE = re.compile(r"(?:^|@)[0-9a-f]{40}$")
HERE = Path(__file__).resolve().parent
DEFAULT_ARTIFACT_ROOT = Path.home() / ".codex" / "orchestrator" / "model-profile-trials"
DEFAULT_QUALIFICATION_NAME = "transport-qualification.json"

BRIDGE_RESULT_FIELDS = {
    "schema",
    "version",
    "trial_id",
    "envelope_hash",
    "packet_hash",
    "acknowledged",
    "attempts",
}
TRANSPORT_ATTEMPT_FIELDS = {
    "request_id",
    "request_hash",
    "run_id",
    "profile_id",
    "operation_role",
    "requested_model",
    "selected_model",
    "reported_model",
    "requested_reasoning_effort",
    "reported_reasoning_effort",
    "provider_resolved_provider",
    "provider_resolved_model",
    "fallback_reason",
    "runner_version",
    "cli_version",
    "status",
    "exit_code",
    "packet_hash",
    "acknowledged",
    "artifact_ref",
    "artifact_sha256",
    "identity_authority",
    "latency_s",
    "tokens_in",
    "tokens_out",
    "launch_ordinal",
    "source_sha_before",
    "source_sha_after",
    "source_manifest_sha256_before",
    "source_manifest_sha256_after",
    "github_repository",
    "github_workflow_ref",
    "github_workflow_sha",
    "github_run_id",
    "github_run_attempt",
    "github_artifact_id",
    "github_artifact_digest",
    "artifact_name",
    "source_clean",
}
IDENTITY_ARTIFACT_FIELDS = {
    "schema",
    "version",
    "trial_id",
    "request_id",
    "request_hash",
    "run_id",
    "profile_id",
    "packet_hash",
    "acknowledged",
    "status",
    "requested_model",
    "selected_model",
    "reported_model",
    "requested_reasoning_effort",
    "reported_reasoning_effort",
    "provider_resolved_provider",
    "provider_resolved_model",
    "fallback_reason",
    "runner_version",
    "cli_version",
    "thread_id",
    "launch_ordinal",
    "source_sha_before",
    "source_sha_after",
    "source_manifest_sha256_before",
    "source_manifest_sha256_after",
    "github_repository",
    "github_workflow_ref",
    "github_workflow_sha",
    "github_run_id",
    "github_run_attempt",
    "artifact_name",
    "identity_authority",
    "operation_role",
    "source_clean",
    "exit_code",
}


def _fleet_roots() -> list[tuple[Path, str]]:
    """Candidate directories that hold the sibling fleet repos, best first.

    WHY THIS IS NOT JUST `HERE.parent`. It was, and that is wrong exactly where it matters: launchd
    runs the MIRROR at ~/.codex/orchestrator-mirror, whose parent ~/.codex holds no fleet checkout,
    so `HERE.parent / "Workflows"` resolved to a nonexistent ~/.codex/Workflows there while the
    canonical tree resolved fine. The same one-candidate bug in
    capability_activation_audit.external_caller() made byte-identical code score 37 of 37 capability
    callers reachable in the canonical tree and 36 of 37 in the mirror -- the mirror being where the
    system actually executes. This is the same resolution order that module now uses (its
    `_fleet_roots`); the home-anchored candidate matches the idiom at keepalive_outcomes.py:30.

    Read via the module global so a test can repoint `HERE` and prove the answer does not depend on
    where the module happens to live.
    """
    roots: list[tuple[Path, str]] = []
    env = os.environ.get("ORCH_FLEET_ROOT")
    if env:
        roots.append((Path(env).expanduser(), "ORCH_FLEET_ROOT"))
    roots.append((HERE.parent, "sibling-of-module"))
    roots.append(
        (Path.home() / "Library/CloudStorage/Dropbox/Learning/Code", "home-anchored-workspace")
    )
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for root, origin in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            out.append((root, origin))
    return out


def resolve_workflows_root() -> tuple[Path, str]:
    """First candidate Workflows checkout that exists, plus the origin that resolved it.

    Never raises: with no candidate present it returns the best-guess path tagged
    `unresolved:<origin>` so a caller's error message still names a sensible location, and the
    caller's own existence checks stay the thing that fails.
    """
    candidates = [(root / "Workflows", origin) for root, origin in _fleet_roots()]
    for path, origin in candidates:
        if path.is_dir():
            return path, origin
    path, origin = candidates[0]
    return path, "unresolved:" + origin


def _default_workflows_root() -> Path:
    return resolve_workflows_root()[0]


# Back-compat module-level default. Call sites resolve LAZILY via _default_workflows_root() -- a
# default argument or argparse default would freeze this import-time value, which is precisely the
# location dependence above.
DEFAULT_WORKFLOWS_ROOT = _default_workflows_root()


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical(value)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_sealed_json(path: Path, payload: dict[str, Any]) -> bool:
    """Create a write-once JSON seal; identical replay is a no-op."""
    rendered = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != rendered:
            raise ValueError("sealed qualification output already exists with different bytes")
        return False
    _atomic_json(path, payload)
    return True


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"registry file missing: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise ValueError("PyYAML is required to validate the Workflows registry") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError(f"registry root must be an object: {path}")
    return value


def _profiles_by_id(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        return {
            str(profile_id): {"profile_id": str(profile_id), **(value or {})}
            for profile_id, value in raw.items()
            if isinstance(value, dict)
        }
    if isinstance(raw, list):
        return {
            str(item.get("profile_id") or item.get("id")): dict(item)
            for item in raw
            if isinstance(item, dict) and (item.get("profile_id") or item.get("id"))
        }
    return {}


def _model_rows(raw: Any) -> dict[str, dict[str, Any]]:
    rows = raw if isinstance(raw, list) else []
    return {
        str(item.get("model_id")): dict(item)
        for item in rows
        if isinstance(item, dict) and item.get("model_id")
    }


def _source_integrity(manifest: dict[str, Any]) -> dict[str, Any]:
    after = {
        key: model_profile_trial.source_manifest(Path(value["root"]))
        for key, value in manifest["source_before"].items()
    }
    changed = sorted(
        key
        for key, value in after.items()
        if value["aggregate_sha256"] != manifest["source_before"][key]["aggregate_sha256"]
    )
    return {"unchanged": not changed, "changed_sources": changed}


def _live_capacity_reservation(
    manifest: dict[str, Any], snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    live = snapshot or capacity.build()
    projected = capacity.profile_capacity_snapshot(
        live,
        pool_usage=capacity.profile_pool_usage_from_ledger(),
        registry=execution_profiles.PROFILE_REGISTRY,
    )
    captured_at = int(live.get("generated_at") or time.time())
    body = {
        "trial_id": manifest["trial_id"],
        "captured_at": captured_at,
        "snapshot_count": 1,
        "pool_id": "codex-subscription",
        "units": len(model_profile_trial.EXPECTED_PROFILE_IDS),
        "pool": projected["pools"].get("codex-subscription"),
        "profiles": {
            profile_id: projected["profiles"].get(profile_id)
            for profile_id in model_profile_trial.EXPECTED_PROFILE_IDS
        },
    }
    body["reservation_id"] = "trial-reservation:" + _hash(body).split(":", 1)[1][:24]
    return body


def _cli_version(binary: Path) -> str:
    proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=15)
    value = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 or not value:
        raise ValueError("app-bundled Codex CLI version probe failed")
    return value[:160]


def _git_source_sha(root: Path) -> str:
    root = root.expanduser().resolve()
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    sha = (head.stdout or "").strip().lower()
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Workflows checkout has no immutable git HEAD")
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0:
        raise ValueError("Workflows checkout cleanliness probe failed")
    if (status.stdout or "").strip():
        raise ValueError("Workflows checkout is not clean")
    return sha


def preflight(
    manifest: dict[str, Any],
    *,
    artifact_root: Path,
    transport: str,
    workflows_root: Path | None = None,
    registry_path: Path | None = None,
    model_registry_path: Path | None = None,
    capacity_snapshot: dict[str, Any] | None = None,
    codex_binary: Path | None = None,
    workflows_source_sha: str | None = None,
) -> dict[str, Any]:
    """Validate all launch inputs without executing a provider or writing Brain rows."""
    if workflows_root is None:
        workflows_root = _default_workflows_root()
    model_profile_trial.validate_trial_manifest(manifest)
    source_roots = [Path(row["root"]) for row in manifest["source_before"].values()]
    model_profile_trial.ensure_artifact_outside_sources(artifact_root, source_roots)
    blockers: list[str] = []
    source = _source_integrity(manifest)
    if not source["unchanged"]:
        blockers.append("source_integrity_changed")
    reservation = _live_capacity_reservation(manifest, capacity_snapshot)
    states = {
        str((reservation["profiles"].get(profile_id) or {}).get("state") or capacity.UNKNOWN)
        for profile_id in model_profile_trial.EXPECTED_PROFILE_IDS
    }
    if not states or states - {capacity.OK, capacity.WARN}:
        blockers.append("shared_codex_pool_not_ready")

    version = None
    registry_sha = None
    model_registry_sha = None
    remote_runner_ref = None
    if transport == "local":
        binary = (codex_binary or adapters.CODEX_PROFILE_BIN).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            blockers.append("version_capable_app_bundled_codex_missing")
        else:
            try:
                version = _cli_version(binary)
            except ValueError:
                blockers.append("codex_cli_version_probe_failed")
        for profile_id in model_profile_trial.EXPECTED_PROFILE_IDS:
            if "local" not in execution_profiles.get_profile(profile_id)["transport_support"]:
                blockers.append(f"profile_not_local:{profile_id}")
    elif transport == "remote":
        try:
            workflows_source_sha = (
                str(workflows_source_sha).strip().lower()
                if workflows_source_sha is not None
                else _git_source_sha(workflows_root)
            )
            if not re.fullmatch(r"[0-9a-f]{40}", workflows_source_sha):
                raise ValueError("Workflows source SHA must be a 40-character commit")
        except ValueError as exc:
            blockers.append("remote_source_not_immutable:" + str(exc)[:120])
            workflows_source_sha = None
        registry_path = registry_path or workflows_root / ".github" / "agents" / "registry.yml"
        model_registry_path = (
            model_registry_path or workflows_root / "config" / "model_registry.json"
        )
        try:
            registry = _load_yaml_or_json(registry_path)
            model_registry = _load_yaml_or_json(model_registry_path)
            registry_sha = _hash(registry)
            model_registry_sha = _hash(model_registry)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            blockers.append("remote_registry_unreadable:" + str(exc)[:120])
            registry, model_registry = {}, {}
        remote = _profiles_by_id(registry.get("execution_profiles"))
        models = _model_rows(model_registry.get("models"))
        for profile_id in model_profile_trial.EXPECTED_PROFILE_IDS:
            local = execution_profiles.get_profile(profile_id)
            row = remote.get(profile_id)
            if not row:
                blockers.append(f"remote_profile_missing:{profile_id}")
                continue
            expected_pool = REMOTE_POOL_MAP[local["capacity_pool_ids"][0]]
            checks = {
                "agent": "codex",
                "model": local["requested_model"],
                "fallback_model": REMOTE_FALLBACK,
                "runner": "reusable-model-profile-trial",
                "capacity_pool": expected_pool,
                "safety": "read-only",
                "lifecycle": "trial",
                "reasoning_effort": local["reasoning_effort"],
                "permission_mode": "read-only",
            }
            for field, expected in checks.items():
                if row.get(field) != expected:
                    blockers.append(f"remote_profile_{field}_mismatch:{profile_id}")
            runner_ref = str(row.get("runner_ref") or "")
            if not PINNED_REF_RE.search(runner_ref):
                blockers.append(f"remote_runner_not_immutable:{profile_id}")
            model_row = models.get(local["requested_model"])
            if (
                not model_row
                or model_row.get("worker_profile") is not True
                or model_row.get("lifecycle") != "trial"
            ):
                blockers.append(f"remote_model_registry_mismatch:{profile_id}")
        contract = registry.get("model_profile_trial_contract") or {}
        remote_runner_ref = str(contract.get("runner_ref") or "")
        if contract.get("mode") != "read-only":
            blockers.append("remote_read_only_trial_mode_missing")
        if contract.get("artifact_schema") != REMOTE_ARTIFACT_SCHEMA:
            blockers.append("remote_trial_artifact_contract_missing")
        if contract.get("identity_authority") != REMOTE_ARTIFACT_AUTHORITY:
            blockers.append("remote_artifact_identity_authority_missing")
        if contract.get("collector_identity_authority") != REMOTE_IDENTITY_AUTHORITY:
            blockers.append("remote_collector_identity_authority_missing")
        if not PINNED_REF_RE.search(remote_runner_ref):
            blockers.append("remote_trial_runner_not_immutable")
        for profile_id, row in remote.items():
            if (
                profile_id in model_profile_trial.EXPECTED_PROFILE_IDS
                and row.get("runner_ref") != remote_runner_ref
            ):
                blockers.append(f"remote_profile_runner_ref_contract_mismatch:{profile_id}")
    else:
        raise ValueError("transport must be local or remote")

    return {
        "transport": transport,
        "ready": not blockers,
        "blockers": sorted(set(blockers)),
        "source_integrity": source,
        "capacity_reservation": reservation,
        "cli_version": version,
        "registry_sha256": registry_sha,
        "model_registry_sha256": model_registry_sha,
        "workflows_source_sha": workflows_source_sha,
        "remote_runner_ref": remote_runner_ref,
    }


def build_request_envelope(
    manifest: dict[str, Any],
    *,
    artifact_root: Path,
    transport: str,
    preflight_result: dict[str, Any],
) -> dict[str, Any]:
    model_profile_trial.validate_trial_manifest(manifest)
    trial_root = artifact_root.resolve() / manifest["trial_id"].replace(":", "-")
    model_profile_trial.ensure_artifact_outside_sources(
        trial_root, [Path(row["root"]) for row in manifest["source_before"].values()]
    )
    requests = []
    for request in sorted(manifest["requests"], key=lambda row: row["launch_ordinal"]):
        body = {
            "run_id": request["run_id"],
            "profile_id": request["profile_id"],
            "launch_ordinal": request["launch_ordinal"],
            "provider": request["provider"],
            "requested_model": request["requested_model"],
            "reasoning_effort": request["reasoning_effort"],
            "permission_mode": "read-only",
            "packet_hash": manifest["packet_hash"],
            "assignment": "instrumentation",
            "learning_enabled": False,
            "promotion_allowed": False,
            "capacity_reservation_id": preflight_result["capacity_reservation"]["reservation_id"],
            "expected_source_sha": preflight_result.get("workflows_source_sha"),
            "runner_ref": preflight_result.get("remote_runner_ref") or RUNNER_VERSION,
            "artifact_dir": str(trial_root / request["profile_id"]),
            "expected_result_fields": sorted(TRANSPORT_ATTEMPT_FIELDS),
        }
        body["request_hash"] = _hash(body)
        body["request_id"] = "trial-request:" + body["request_hash"].split(":", 1)[1][:24]
        requests.append(body)
    envelope = {
        "schema": BRIDGE_SCHEMA,
        "version": BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "transport": transport,
        "dispatch_allowed": bool(preflight_result.get("ready")),
        "lifecycle": "shadow",
        "assignment": "instrumentation",
        "learning_enabled": False,
        "promotion_allowed": False,
        "packet_hash": manifest["packet_hash"],
        "artifact_root": str(trial_root),
        "preflight": preflight_result,
        "requests": requests,
        "remote_result_schema": REMOTE_ARTIFACT_SCHEMA,
        "brain_ingest_enabled": False,
    }
    envelope["envelope_hash"] = _hash(envelope)
    return envelope


def validate_envelope(envelope: dict[str, Any], manifest: dict[str, Any]) -> None:
    if envelope.get("schema") != BRIDGE_SCHEMA or envelope.get("version") != BRIDGE_VERSION:
        raise ValueError("unsupported trial bridge envelope")
    if (
        envelope.get("trial_id") != manifest["trial_id"]
        or envelope.get("packet_hash") != manifest["packet_hash"]
    ):
        raise ValueError("trial bridge identity mismatch")
    replay = dict(envelope)
    supplied = replay.pop("envelope_hash", None)
    if supplied != _hash(replay):
        raise ValueError("trial bridge envelope is not replayable")
    request_by_profile = {row["profile_id"]: row for row in manifest["requests"]}
    if len(envelope.get("requests") or []) != 3:
        raise ValueError("trial bridge requires exactly three requests")
    for row in envelope["requests"]:
        request = request_by_profile.get(row.get("profile_id"))
        if not request:
            raise ValueError("trial bridge contains an unknown profile")
        replay_row = dict(row)
        request_id = replay_row.pop("request_id", None)
        request_hash = replay_row.pop("request_hash", None)
        expected_hash = _hash(replay_row)
        if (
            request_hash != expected_hash
            or request_id != "trial-request:" + expected_hash.split(":", 1)[1][:24]
        ):
            raise ValueError("trial bridge request is not replayable")
        if row.get("requested_model") != request["requested_model"]:
            raise ValueError("trial bridge request model drifted")


def _identity_artifact(
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    request: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    ref = str(attempt.get("artifact_ref") or "")
    artifact_path = Path(ref).expanduser()
    if not artifact_path.is_absolute() or not artifact_path.is_file():
        raise ValueError(
            f"transport identity artifact is not a local downloaded file:{request['profile_id']}"
        )
    model_profile_trial.ensure_artifact_outside_sources(
        artifact_path, [Path(row["root"]) for row in manifest["source_before"].values()]
    )
    raw = artifact_path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError(f"transport identity artifact exceeds 64KiB:{request['profile_id']}")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != attempt.get("artifact_sha256"):
        raise ValueError(f"transport identity artifact hash mismatch:{request['profile_id']}")
    try:
        artifact = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"transport identity artifact is not strict JSON:{request['profile_id']}"
        ) from exc
    if not isinstance(artifact, dict):
        raise ValueError(
            f"transport identity artifact root is not an object:{request['profile_id']}"
        )
    unknown = sorted(set(artifact) - IDENTITY_ARTIFACT_FIELDS)
    if unknown:
        raise ValueError(f"transport identity artifact contains unsupported fields: {unknown}")
    expected_schema = (
        LOCAL_ARTIFACT_SCHEMA if envelope.get("transport") == "local" else REMOTE_ARTIFACT_SCHEMA
    )
    exact = request["requested_model"]
    expected = {
        "schema": expected_schema,
        "version": 1 if envelope.get("transport") == "local" else 2,
        "trial_id": manifest["trial_id"],
        "request_id": request["request_id"],
        "request_hash": request["request_hash"],
        "run_id": request["run_id"],
        "profile_id": request["profile_id"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "status": "success",
        "requested_model": exact,
        "selected_model": exact,
        "reported_model": exact,
        "requested_reasoning_effort": request["reasoning_effort"],
        "reported_reasoning_effort": request["reasoning_effort"],
        "provider_resolved_provider": None,
        "provider_resolved_model": None,
        "fallback_reason": None,
        "runner_version": attempt.get("runner_version"),
        "cli_version": attempt.get("cli_version"),
    }
    if envelope.get("transport") == "remote":
        expected.update(
            launch_ordinal=request["launch_ordinal"],
            source_sha_before=request["expected_source_sha"],
            source_sha_after=request["expected_source_sha"],
            github_repository=REMOTE_REPOSITORY,
            github_workflow_ref=REMOTE_WORKFLOW_REF,
            github_workflow_sha=request["expected_source_sha"],
            github_run_id=attempt.get("github_run_id"),
            github_run_attempt=attempt.get("github_run_attempt"),
            artifact_name=attempt.get("artifact_name"),
            identity_authority=REMOTE_ARTIFACT_AUTHORITY,
            operation_role="worker",
            source_clean=True,
            exit_code=0,
        )
    for field, value in expected.items():
        if artifact.get(field) != value:
            raise ValueError(
                f"transport identity artifact mismatch: {field}:{request['profile_id']}"
            )
    if envelope.get("transport") == "local" and not str(artifact.get("thread_id") or ""):
        raise ValueError(
            f"local session identity artifact missing thread_id:{request['profile_id']}"
        )
    if envelope.get("transport") == "remote":
        before = str(artifact.get("source_manifest_sha256_before") or "")
        after = str(artifact.get("source_manifest_sha256_after") or "")
        if not SHA256_RE.fullmatch(before) or after != before:
            raise ValueError(f"transport source manifest mismatch:{request['profile_id']}")
        if (
            int(artifact.get("github_run_id") or 0) <= 0
            or int(artifact.get("github_run_attempt") or 0) <= 0
        ):
            raise ValueError(f"transport GitHub run provenance missing:{request['profile_id']}")
    return artifact


def _gh_json(endpoint: str) -> dict[str, Any]:
    proc = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ValueError("GitHub Actions provenance query failed: " + (proc.stderr or "")[:160])
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub Actions provenance response was not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("GitHub Actions provenance response was not an object")
    return value


def _gh_download_artifact(artifact_id: int) -> bytes:
    proc = subprocess.run(
        ["gh", "api", f"repos/{REMOTE_REPOSITORY}/actions/artifacts/{artifact_id}/zip"],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError(
            "GitHub Actions artifact download failed: " + proc.stderr.decode(errors="replace")[:160]
        )
    if not proc.stdout or len(proc.stdout) > 256 * 1024:
        raise ValueError("GitHub Actions artifact archive size is invalid")
    return proc.stdout


def _artifact_json_from_zip(raw_zip: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) != 1 or files[0].filename != "model-profile-trial-attempt.json":
                raise ValueError("trial artifact archive must contain one exact JSON file")
            if files[0].file_size > 64 * 1024:
                raise ValueError("trial artifact JSON exceeds 64KiB")
            return archive.read(files[0])
    except zipfile.BadZipFile as exc:
        raise ValueError("GitHub Actions trial artifact was not a valid ZIP") from exc


def collect_remote_attempt(
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    request: dict[str, Any],
    *,
    github_run_id: int,
    artifact_root: Path,
) -> dict[str, Any]:
    """Download one Actions artifact and bind it to authenticated run metadata."""
    if envelope.get("transport") != "remote":
        raise ValueError("remote collection requires a remote bridge envelope")
    run_id = int(github_run_id)
    run = _gh_json(f"repos/{REMOTE_REPOSITORY}/actions/runs/{run_id}")
    expected_run = {
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": request["expected_source_sha"],
        "path": REMOTE_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
    }
    for field, expected in expected_run.items():
        if run.get(field) != expected:
            raise ValueError(f"GitHub Actions run provenance mismatch: {field}")
    if int(run.get("id") or 0) != run_id or int(run.get("run_attempt") or 0) <= 0:
        raise ValueError("GitHub Actions run identity is incomplete")

    listing = _gh_json(f"repos/{REMOTE_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")
    expected_name = (
        f"model-profile-trial-{request['profile_id']}-{run_id}-"
        f"{int(run['run_attempt'])}-{request['launch_ordinal']}"
    )
    matches = [
        row
        for row in listing.get("artifacts") or []
        if isinstance(row, dict) and row.get("name") == expected_name and not row.get("expired")
    ]
    if len(matches) != 1:
        raise ValueError("GitHub Actions run did not publish one exact trial artifact")
    metadata = matches[0]
    artifact_id = int(metadata.get("id") or 0)
    workflow_run = metadata.get("workflow_run") or {}
    if artifact_id <= 0 or workflow_run.get("head_sha") != request["expected_source_sha"]:
        raise ValueError("GitHub Actions artifact provenance mismatch")

    raw_zip = _gh_download_artifact(artifact_id)
    archive_digest = "sha256:" + hashlib.sha256(raw_zip).hexdigest()
    if metadata.get("digest") != archive_digest:
        raise ValueError("GitHub Actions artifact archive digest mismatch")
    raw = _artifact_json_from_zip(raw_zip)
    try:
        artifact = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GitHub Actions trial artifact was not strict JSON") from exc
    if not isinstance(artifact, dict):
        raise ValueError("GitHub Actions trial artifact root was not an object")
    provenance = {
        "github_repository": REMOTE_REPOSITORY,
        "github_workflow_ref": REMOTE_WORKFLOW_REF,
        "github_workflow_sha": request["expected_source_sha"],
        "github_run_id": run_id,
        "github_run_attempt": int(run["run_attempt"]),
        "artifact_name": expected_name,
    }
    for field, expected in provenance.items():
        if artifact.get(field) != expected:
            raise ValueError(f"GitHub Actions artifact identity mismatch: {field}")

    trial_root = artifact_root.resolve() / manifest["trial_id"].replace(":", "-")
    model_profile_trial.ensure_artifact_outside_sources(
        trial_root, [Path(row["root"]) for row in manifest["source_before"].values()]
    )
    artifact_path = trial_root / request["profile_id"] / "model-profile-trial-attempt.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(raw)
    attempt = {key: artifact.get(key) for key in TRANSPORT_ATTEMPT_FIELDS if key in artifact}
    attempt.update(
        {
            "operation_role": "worker",
            "exit_code": 0,
            "artifact_ref": str(artifact_path),
            "artifact_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "identity_authority": REMOTE_IDENTITY_AUTHORITY,
            "github_artifact_id": artifact_id,
            "github_artifact_digest": archive_digest,
            "latency_s": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
        }
    )
    return attempt


def collect_remote_results(
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    run_ids: list[int],
    *,
    artifact_root: Path,
) -> dict[str, Any]:
    model_profile_trial.validate_trial_manifest(manifest)
    validate_envelope(envelope, manifest)
    requests = sorted(envelope["requests"], key=lambda row: row["launch_ordinal"])
    if len(run_ids) != 3:
        raise ValueError("remote trial collection requires three serial run IDs")
    attempts = [
        collect_remote_attempt(
            manifest,
            envelope,
            request,
            github_run_id=run_id,
            artifact_root=artifact_root,
        )
        for request, run_id in zip(requests, run_ids)
    ]
    return {
        "schema": BRIDGE_RESULT_SCHEMA,
        "version": BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "envelope_hash": envelope["envelope_hash"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "attempts": attempts,
    }


def _validate_transport_results(
    manifest: dict[str, Any], envelope: dict[str, Any], results: dict[str, Any]
) -> list[dict[str, Any]]:
    """Validate sealed transport evidence without consulting mutable live source."""
    model_profile_trial.validate_trial_manifest(manifest)
    validate_envelope(envelope, manifest)
    unknown = sorted(set(results) - BRIDGE_RESULT_FIELDS)
    if unknown:
        raise ValueError(f"transport results contain unsupported fields: {unknown}")
    if results.get("schema") != BRIDGE_RESULT_SCHEMA or results.get("version") != BRIDGE_VERSION:
        raise ValueError("unsupported transport result schema")
    if (
        results.get("trial_id") != manifest["trial_id"]
        or results.get("envelope_hash") != envelope["envelope_hash"]
    ):
        raise ValueError("transport result identity mismatch")
    if (
        results.get("packet_hash") != manifest["packet_hash"]
        or results.get("acknowledged") is not True
    ):
        raise ValueError("transport did not acknowledge the frozen packet")
    attempts = results.get("attempts") or []
    by_request = {row.get("request_id"): row for row in attempts if isinstance(row, dict)}
    if len(attempts) != 3 or set(by_request) != {row["request_id"] for row in envelope["requests"]}:
        raise ValueError("transport results require one exact attempt per request")
    sanitized = []
    for request in envelope["requests"]:
        attempt = by_request[request["request_id"]]
        extra = sorted(set(attempt) - TRANSPORT_ATTEMPT_FIELDS)
        if extra:
            raise ValueError(f"transport attempt contains unsupported fields: {extra}")
        exact = request["requested_model"]
        expected_authority = (
            LOCAL_IDENTITY_AUTHORITY
            if envelope.get("transport") == "local"
            else REMOTE_IDENTITY_AUTHORITY
        )
        required_equal = {
            "request_hash": request["request_hash"],
            "run_id": request["run_id"],
            "profile_id": request["profile_id"],
            "operation_role": "worker",
            "requested_model": exact,
            "selected_model": exact,
            "reported_model": exact,
            "requested_reasoning_effort": request["reasoning_effort"],
            "reported_reasoning_effort": request["reasoning_effort"],
            "packet_hash": manifest["packet_hash"],
            "identity_authority": expected_authority,
            "runner_version": request["runner_ref"],
        }
        if envelope.get("transport") == "remote":
            required_equal.update(
                launch_ordinal=request["launch_ordinal"],
                source_sha_before=request["expected_source_sha"],
                source_sha_after=request["expected_source_sha"],
                github_repository=REMOTE_REPOSITORY,
                github_workflow_ref=REMOTE_WORKFLOW_REF,
                github_workflow_sha=request["expected_source_sha"],
            )
        for field, expected in required_equal.items():
            if attempt.get(field) != expected:
                raise ValueError(f"transport identity mismatch: {field}:{request['profile_id']}")
        # Codex subscription execution does not expose an independent immutable
        # provider identity. The Workflows/session artifact may prove what the
        # CLI reported, but it must not upgrade that evidence to provider
        # resolution. This keeps the trial useful for plumbing while ineligible
        # for exact-model learning.
        if (
            attempt.get("provider_resolved_provider") is not None
            or attempt.get("provider_resolved_model") is not None
        ):
            raise ValueError(
                f"transport claimed unsupported provider resolution:{request['profile_id']}"
            )
        if attempt.get("fallback_reason") not in (None, ""):
            raise ValueError(f"transport fallback stopped trial:{request['profile_id']}")
        if (
            str(attempt.get("status") or "").lower() not in {"success", "succeeded"}
            or int(attempt.get("exit_code", 1)) != 0
        ):
            raise ValueError(f"transport attempt failed:{request['profile_id']}")
        if attempt.get("acknowledged") is not True:
            raise ValueError(f"transport packet acknowledgement missing:{request['profile_id']}")
        if not str(attempt.get("runner_version") or "") or not str(
            attempt.get("cli_version") or ""
        ):
            raise ValueError(f"transport version evidence missing:{request['profile_id']}")
        if not str(attempt.get("artifact_ref") or "") or not SHA256_RE.fullmatch(
            str(attempt.get("artifact_sha256") or "")
        ):
            raise ValueError(f"transport artifact evidence missing:{request['profile_id']}")
        if envelope.get("transport") == "remote":
            before = str(attempt.get("source_manifest_sha256_before") or "")
            after = str(attempt.get("source_manifest_sha256_after") or "")
            if not SHA256_RE.fullmatch(before) or after != before:
                raise ValueError(f"transport source manifest mismatch:{request['profile_id']}")
            if (
                int(attempt.get("github_run_id") or 0) <= 0
                or int(attempt.get("github_run_attempt") or 0) <= 0
            ):
                raise ValueError(f"transport GitHub run provenance missing:{request['profile_id']}")
            if (
                int(attempt.get("github_artifact_id") or 0) <= 0
                or not SHA256_RE.fullmatch(str(attempt.get("github_artifact_digest") or ""))
                or not str(attempt.get("artifact_name") or "")
            ):
                raise ValueError(
                    f"transport GitHub artifact provenance missing:{request['profile_id']}"
                )
        _identity_artifact(manifest, envelope, request, attempt)
        sanitized_attempt = {
            "run_id": attempt["run_id"],
            "profile_id": attempt["profile_id"],
            "operation_role": "worker",
            "requested_model": exact,
            "selected_model": exact,
            "reported_model": exact,
            "requested_reasoning_effort": request["reasoning_effort"],
            "reported_reasoning_effort": request["reasoning_effort"],
            "provider_resolved_provider": None,
            "provider_resolved_model": None,
            "fallback_reason": None,
            "runner_version": str(attempt["runner_version"])[:160],
            "cli_version": str(attempt["cli_version"])[:160],
            "status": "success",
            "latency_s": float(attempt.get("latency_s") or 0.0),
            "tokens_in": int(attempt.get("tokens_in") or 0),
            "tokens_out": int(attempt.get("tokens_out") or 0),
            "artifact_ref": str(attempt["artifact_ref"])[:512],
            "packet_hash": manifest["packet_hash"],
            "acknowledged": True,
            "identity_authority": expected_authority,
            "artifact_sha256": attempt["artifact_sha256"],
        }
        if envelope.get("transport") == "remote":
            sanitized_attempt.update(
                {
                    key: attempt[key]
                    for key in (
                        "launch_ordinal",
                        "source_sha_before",
                        "source_sha_after",
                        "source_manifest_sha256_before",
                        "source_manifest_sha256_after",
                        "github_repository",
                        "github_workflow_ref",
                        "github_workflow_sha",
                        "github_run_id",
                        "github_run_attempt",
                        "github_artifact_id",
                        "github_artifact_digest",
                        "artifact_name",
                    )
                }
            )
        sanitized.append(sanitized_attempt)
    return sanitized


def ingest_transport_results(
    manifest: dict[str, Any], envelope: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    """Validate strict transport output and emit quarantine-only finalizer input.

    This remains the provider-attested finalization boundary.  In addition to
    the sealed evidence checks shared with offline qualification, it requires
    the live source trees named by the manifest to remain byte-identical.
    """
    sanitized = _validate_transport_results(manifest, envelope, results)
    source = _source_integrity(manifest)
    if not source["unchanged"]:
        raise ValueError("source integrity changed before transport ingest")
    payload = {
        "schema": QUARANTINE_RESULT_SCHEMA,
        "version": BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "status": "quarantined",
        "ready": False,
        "learning_enabled": False,
        "promotion_allowed": False,
        "stop_reason": "provider_resolved_identity_unavailable",
        "source_integrity": source,
        "attempts": sanitized,
    }
    return payload


def _validate_sealed_source_snapshots(manifest: dict[str, Any]) -> dict[str, str]:
    """Recompute the historical source-manifest seals without reading live trees."""
    source_hashes: dict[str, str] = {}
    for name in ("orchestrator", "workflows"):
        snapshot = manifest["source_before"][name]
        entries = snapshot.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"sealed source manifest has no entries:{name}")
        paths: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
                raise ValueError(f"sealed source manifest entry is invalid:{name}")
            path = str(entry.get("path") or "")
            if not path or Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError(f"sealed source manifest path is unsafe:{name}")
            if not SHA256_RE.fullmatch(str(entry.get("sha256") or "")):
                raise ValueError(f"sealed source manifest digest is invalid:{name}")
            size = entry.get("bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError(f"sealed source manifest size is invalid:{name}")
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise ValueError(f"sealed source manifest contains duplicate paths:{name}")
        if snapshot.get("file_count") != len(entries):
            raise ValueError(f"sealed source manifest file count mismatch:{name}")
        if snapshot.get("aggregate_sha256") != _hash(entries):
            raise ValueError(f"sealed source manifest aggregate mismatch:{name}")
        source_hashes[name] = snapshot["aggregate_sha256"]
    return source_hashes


def qualify_transport_contract(
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    results: dict[str, Any],
    quarantine: dict[str, Any],
    *,
    evidence_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Release a quarantine only for transport/profile-contract instrumentation.

    The resulting qualification is intentionally below provider identity and
    quality assurance.  It performs no dispatch and no Brain write, and it does
    not make the original canary eligible for learning or promotion.
    """
    model_profile_trial.validate_trial_manifest(manifest)
    validate_envelope(envelope, manifest)
    source_hashes = _validate_sealed_source_snapshots(manifest)
    if envelope.get("transport") != "remote":
        raise ValueError("transport qualification requires remote evidence")
    preflight_result = envelope.get("preflight") or {}
    if (
        envelope.get("dispatch_allowed") is not True
        or preflight_result.get("ready") is not True
        or preflight_result.get("blockers") != []
    ):
        raise ValueError("transport qualification requires a passing sealed preflight")
    if (preflight_result.get("source_integrity") or {}).get("unchanged") is not True:
        raise ValueError("transport qualification source preflight was not clean")
    runner_ref = str(preflight_result.get("remote_runner_ref") or "")
    workflows_source_sha = str(preflight_result.get("workflows_source_sha") or "")
    if not PINNED_REF_RE.search(runner_ref):
        raise ValueError("transport qualification runner is not commit-pinned")
    if not re.fullmatch(r"[0-9a-f]{40}", workflows_source_sha):
        raise ValueError("transport qualification Workflows source is not immutable")
    if any(request.get("runner_ref") != runner_ref for request in envelope["requests"]):
        raise ValueError("transport qualification request runner pin drifted")
    if any(
        request.get("expected_source_sha") != workflows_source_sha
        for request in envelope["requests"]
    ):
        raise ValueError("transport qualification request source pin drifted")

    sanitized = _validate_transport_results(manifest, envelope, results)
    expected_quarantine_fields = {
        "schema",
        "version",
        "trial_id",
        "packet_hash",
        "acknowledged",
        "status",
        "ready",
        "learning_enabled",
        "promotion_allowed",
        "stop_reason",
        "source_integrity",
        "attempts",
    }
    if set(quarantine) != expected_quarantine_fields:
        raise ValueError("quarantine artifact fields are not exact")
    expected_quarantine = {
        "schema": QUARANTINE_RESULT_SCHEMA,
        "version": BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "packet_hash": manifest["packet_hash"],
        "acknowledged": True,
        "status": "quarantined",
        "ready": False,
        "learning_enabled": False,
        "promotion_allowed": False,
        "stop_reason": "provider_resolved_identity_unavailable",
        "source_integrity": {"unchanged": True, "changed_sources": []},
        "attempts": sanitized,
    }
    if quarantine != expected_quarantine:
        raise ValueError("quarantine artifact is not an exact evidence replay")

    github_runs = [int(row["github_run_id"]) for row in sanitized]
    github_artifacts = [int(row["github_artifact_id"]) for row in sanitized]
    if len(set(github_runs)) != 3 or len(set(github_artifacts)) != 3:
        raise ValueError("transport qualification requires three distinct remote attempts")
    source_manifest_hashes = {str(row["source_manifest_sha256_before"]) for row in sanitized}
    if len(source_manifest_hashes) != 1:
        raise ValueError("transport qualification source attestations disagree")

    profile_evidence = [
        {
            "profile_id": row["profile_id"],
            "requested_model": row["requested_model"],
            "cli_reported_model": row["reported_model"],
            "reasoning_effort": row["reported_reasoning_effort"],
            "cli_version": row["cli_version"],
            "runner_version": row["runner_version"],
            "github_run_id": row["github_run_id"],
            "github_artifact_id": row["github_artifact_id"],
            "identity_artifact_sha256": row["artifact_sha256"],
            "github_archive_sha256": row["github_artifact_digest"],
            "fallback_observed": False,
            "provider_resolved_provider": None,
            "provider_resolved_model": None,
        }
        for row in sanitized
    ]
    payload = {
        "schema": QUALIFICATION_SCHEMA,
        "version": BRIDGE_VERSION,
        "trial_id": manifest["trial_id"],
        "status": "qualified_transport_profile_contract_only",
        "qualification_scope": "future_instrumentation",
        "transport_contract_qualified": True,
        "cli_reported_profile_contract_qualified": True,
        "future_instrumentation_allowed": True,
        "canary_quality_evidence": False,
        "provider_identity_status": "unavailable_unclaimed",
        "provider_resolved_provider": None,
        "provider_resolved_model": None,
        "learning_enabled": False,
        "brain_ingest_enabled": False,
        "quality_weight_updates_allowed": False,
        "promotion_allowed": False,
        "provider_attested_finalization_required": True,
        "assurance_ladder": [
            {"level": 1, "name": "sealed_artifact_integrity", "satisfied": True},
            {"level": 2, "name": "pinned_transport_and_source_attestation", "satisfied": True},
            {"level": 3, "name": "cli_reported_profile_contract", "satisfied": True},
            {
                "level": 4,
                "name": "provider_resolved_identity",
                "satisfied": False,
                "claimed": False,
            },
            {"level": 5, "name": "accepted_task_quality", "satisfied": False, "claimed": False},
        ],
        "replay": {
            "trial_identity_reproduced": True,
            "envelope_hash_reproduced": True,
            "request_hashes_reproduced": True,
            "quarantine_reproduced": True,
        },
        "source_attestation": {
            "sealed_source_aggregates": source_hashes,
            "workflows_source_sha": workflows_source_sha,
            "worker_source_manifest_sha256": next(iter(source_manifest_hashes)),
            "source_clean_for_all_attempts": True,
        },
        "profile_evidence": profile_evidence,
        "evidence_hashes": dict(sorted((evidence_hashes or {}).items())),
        "next_action": "use_only_for_future_no_learning_instrumented_trials",
    }
    payload["qualification_hash"] = _hash(payload)
    return payload


def _default_qualification_report_path() -> Path:
    configured = os.environ.get("ORCH_MODEL_PROFILE_TRANSPORT_QUALIFICATION")
    if configured:
        return Path(configured).expanduser()
    candidates = list(DEFAULT_ARTIFACT_ROOT.glob(f"**/{DEFAULT_QUALIFICATION_NAME}"))
    if candidates:
        return max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    return DEFAULT_ARTIFACT_ROOT / DEFAULT_QUALIFICATION_NAME


def build_qualification_report(path: Path | None = None) -> dict[str, Any]:
    path = path or _default_qualification_report_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "path": str(path),
            "status": "not_qualified",
            "transport_contract_qualified": False,
            "provider_identity_status": "unavailable_unclaimed",
            "learning_enabled": False,
            "quality_weight_updates_allowed": False,
        }
    replay = dict(payload) if isinstance(payload, dict) else {}
    supplied_hash = replay.pop("qualification_hash", None)
    if (
        replay.get("schema") != QUALIFICATION_SCHEMA
        or replay.get("version") != BRIDGE_VERSION
        or supplied_hash != _hash(replay)
    ):
        return {
            "path": str(path),
            "status": "invalid_qualification",
            "transport_contract_qualified": False,
            "provider_identity_status": "unavailable_unclaimed",
            "learning_enabled": False,
            "quality_weight_updates_allowed": False,
        }
    return {"path": str(path), **payload}


def _selftest_workflows_root_location_independent() -> None:
    """The default Workflows root must not depend on where this module is executed from.

    The bug this pins: `HERE.parent / "Workflows"` is right in the canonical tree
    (.../Learning/Code/Orchestrator -> .../Learning/Code/Workflows) and wrong in the launchd mirror
    at ~/.codex/orchestrator-mirror, whose parent ~/.codex has no fleet checkout. So the test
    repoints HERE at an EMPTY directory -- standing in for the mirror -- and demands that a real
    Workflows checkout still resolve, through the home-anchored candidate.
    """
    saved_here = HERE
    saved_env = os.environ.get("ORCH_FLEET_ROOT")
    home_root = Path.home() / "Library/CloudStorage/Dropbox/Learning/Code" / "Workflows"
    try:
        with tempfile.TemporaryDirectory(prefix="trial-bridge-anchor-") as tmp:
            # Stand in for the mirror: an anchor whose parent holds no fleet checkout.
            os.environ.pop("ORCH_FLEET_ROOT", None)
            globals()["HERE"] = Path(tmp) / "orchestrator-mirror"
            sibling_guess = Path(tmp) / "Workflows"
            assert not sibling_guess.exists(), sibling_guess
            path, origin = resolve_workflows_root()
            if home_root.is_dir():
                assert (
                    path == home_root
                ), f"real Workflows root must resolve from any anchor: {path}"
                assert origin == "home-anchored-workspace", origin
                assert _default_workflows_root() == home_root, _default_workflows_root()
                # preflight() must not have frozen a def-time default (the original defect).
                import inspect

                assert inspect.signature(preflight).parameters["workflows_root"].default is None
            else:
                # No fleet checkout on this machine: no candidate can exist, and the resolver must
                # say so rather than hand back a path that only looks resolved.
                assert origin.startswith("unresolved:"), (path, origin)

            # An explicit override wins, so a relocated workspace stays usable.
            override = Path(tmp) / "elsewhere"
            (override / "Workflows").mkdir(parents=True)
            os.environ["ORCH_FLEET_ROOT"] = str(override)
            path2, origin2 = resolve_workflows_root()
            assert path2 == override / "Workflows" and origin2 == "ORCH_FLEET_ROOT", (
                path2,
                origin2,
            )

            # A sibling checkout beside the module still wins over the home anchor.
            os.environ.pop("ORCH_FLEET_ROOT", None)
            sibling_guess.mkdir()
            path3, origin3 = resolve_workflows_root()
            assert path3 == sibling_guess and origin3 == "sibling-of-module", (path3, origin3)
    finally:
        globals()["HERE"] = saved_here
        if saved_env is None:
            os.environ.pop("ORCH_FLEET_ROOT", None)
        else:
            os.environ["ORCH_FLEET_ROOT"] = saved_env


def _selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="trial-bridge-") as tmp:
        root = Path(tmp)
        orch, workflows = root / "orch", root / "workflows"
        orch.mkdir()
        workflows.mkdir()
        (orch / "a.py").write_text("x=1\n", encoding="utf-8")
        (workflows / "a.yml").write_text("x: 1\n", encoding="utf-8")
        manifest = model_profile_trial.build_trial_manifest(
            orch, workflows, now=1000, seed=14, capacity_state="ok"
        )
        snapshot = {"generated_at": 1000, "agents": {"codex": {"state": "ok"}}}
        pre = preflight(
            manifest,
            artifact_root=root / "artifacts",
            transport="local",
            capacity_snapshot=snapshot,
            codex_binary=adapters.CODEX_PROFILE_BIN,
        )
        envelope = build_request_envelope(
            manifest,
            artifact_root=root / "artifacts",
            transport="local",
            preflight_result=pre,
        )
        validate_envelope(envelope, manifest)
        broken = json.loads(json.dumps(envelope))
        broken["requests"][0]["requested_model"] = "fallback"
        try:
            validate_envelope(broken, manifest)
            raise AssertionError("tampered request passed replay validation")
        except ValueError:
            pass
    _selftest_workflows_root_location_independent()
    print("model_profile_trial_bridge.py selftest: OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "prepare"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--manifest", type=Path, required=True)
        cmd.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
        cmd.add_argument("--transport", choices=("local", "remote"), required=True)
        # default=None so preflight() resolves the fleet root at call time, not at
        # parser-build time (see _fleet_roots: the mirror and the canonical tree differ).
        cmd.add_argument("--workflows-root", type=Path, default=None)
        cmd.add_argument("--registry", type=Path)
        cmd.add_argument("--model-registry", type=Path)
        cmd.add_argument("--workflows-source-sha")
        if name == "prepare":
            cmd.add_argument("--output", type=Path, required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--manifest", type=Path, required=True)
    ingest.add_argument("--envelope", type=Path, required=True)
    ingest.add_argument("--results", type=Path, required=True)
    ingest.add_argument("--output", type=Path, required=True)
    collect = sub.add_parser("collect-remote")
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--envelope", type=Path, required=True)
    collect.add_argument("--run-id", type=int, action="append", required=True)
    collect.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    collect.add_argument("--output", type=Path, required=True)
    qualify = sub.add_parser("qualify")
    qualify.add_argument("--manifest", type=Path, required=True)
    qualify.add_argument("--envelope", type=Path, required=True)
    qualify.add_argument("--results", type=Path, required=True)
    qualify.add_argument("--quarantine", type=Path, required=True)
    qualify.add_argument("--output", type=Path)
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        _selftest()
        return 0
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.command in {"preflight", "prepare"}:
        result = preflight(
            manifest,
            artifact_root=args.artifact_root,
            transport=args.transport,
            workflows_root=args.workflows_root,
            registry_path=args.registry,
            model_registry_path=args.model_registry,
            workflows_source_sha=args.workflows_source_sha,
        )
        payload = (
            result
            if args.command == "preflight"
            else build_request_envelope(
                manifest,
                artifact_root=args.artifact_root,
                transport=args.transport,
                preflight_result=result,
            )
        )
        if args.command == "prepare":
            model_profile_trial.ensure_artifact_outside_sources(
                args.output, [Path(row["root"]) for row in manifest["source_before"].values()]
            )
            _atomic_json(args.output, payload)
    elif args.command == "collect-remote":
        envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
        payload = collect_remote_results(
            manifest, envelope, args.run_id, artifact_root=args.artifact_root
        )
        _atomic_json(args.output, payload)
    elif args.command == "qualify":
        envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
        results = json.loads(args.results.read_text(encoding="utf-8"))
        quarantine = json.loads(args.quarantine.read_text(encoding="utf-8"))
        inputs = {
            "manifest": args.manifest,
            "envelope": args.envelope,
            "transport_results": args.results,
            "quarantine": args.quarantine,
        }
        output = args.output or args.quarantine.parent / DEFAULT_QUALIFICATION_NAME
        if output.resolve() in {path.resolve() for path in inputs.values()}:
            raise ValueError("qualification output may not overwrite evidence input")
        model_profile_trial.ensure_artifact_outside_sources(
            output, [Path(row["root"]) for row in manifest["source_before"].values()]
        )
        payload = qualify_transport_contract(
            manifest,
            envelope,
            results,
            quarantine,
            evidence_hashes={name: _file_sha256(path) for name, path in inputs.items()},
        )
        _write_sealed_json(output, payload)
    else:
        envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
        results = json.loads(args.results.read_text(encoding="utf-8"))
        payload = ingest_transport_results(manifest, envelope, results)
        _atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
