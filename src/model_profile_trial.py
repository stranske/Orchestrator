#!/usr/bin/env python3
"""Guarded, read-only Astra/Terra/Luna worker-profile plumbing trial.

The trial is instrumentation, not a benchmark: one frozen packet, one shared
Codex capacity snapshot, randomized launch order, and three exact profiles. It
never executes a worker itself. ``prepare`` emits launch requests for a guarded
transport bridge; ``finalize`` validates returned telemetry and proves source
integrity. Live Brain recording remains disabled at the CLI boundary until the
multi-row feedback write has a single atomic transaction.
``--selftest`` exercises prepare, validation, and reporting fully offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import capacity
import execution_profiles
import feedback

TRIAL_SCHEMA = "orchestrator.model-profile-trial"
RESULT_SCHEMA = "orchestrator.model-profile-trial-results"
STATE_SCHEMA = "orchestrator.model-profile-trial-state"
SCHEMA_VERSION = 1
CAPABILITY_ID = "local-model-profile-trial"
# The live three-arm tier comparison: cheap / mid / full. GPT-6 Astra replaced Sol as the codex
# full tier on 2026-09-04, so it takes Sol's arm here. Sol keeps its registry profile so outcomes
# already recorded against it stay interpretable, but it is no longer an arm of the running trial.
EXPECTED_PROFILE_IDS = (
    "codex-6-astra-high",
    "codex-5.6-terra-high",
    "codex-5.6-luna-high",
)
# Frozen pre-Astra trials retain their original identity and can still be ingested.
LEGACY_PROFILE_IDS = (
    "codex-5.6-sol-high",
    "codex-5.6-terra-high",
    "codex-5.6-luna-high",
)


def _manifest_profile_ids(manifest: dict[str, Any]) -> tuple[str, ...]:
    requests = manifest.get("requests") or []
    actual = {item.get("profile_id") for item in requests}
    for supported in (EXPECTED_PROFILE_IDS, LEGACY_PROFILE_IDS):
        if actual == set(supported) and len(requests) == len(supported):
            return supported
    raise ValueError("trial requires an exact supported profile set")


DEFAULT_STATE_PATH = Path(
    os.environ.get(
        "ORCH_MODEL_PROFILE_TRIAL_STATE",
        Path.home() / ".codex" / "orchestrator" / "model-profile-trial.json",
    )
)
EXCLUDED_SOURCE_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
}
EXCLUDED_SOURCE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".mp4",
    ".mov",
    ".bin",
}
MAX_MANIFEST_FILES = int(os.environ.get("ORCH_TRIAL_MAX_MANIFEST_FILES", "20000"))
MAX_MANIFEST_BYTES = int(os.environ.get("ORCH_TRIAL_MAX_MANIFEST_BYTES", "200000000"))
FROZEN_PACKET = {
    "schema": "orchestrator.model-profile-trial-packet",
    "version": 1,
    "purpose": "worker identity, fallback, capacity, and lineage plumbing only",
    "instruction": (
        "Read this packet without changing files. Return only the bounded "
        "identity fields requested by the Workflows worker telemetry contract."
    ),
    "expected_output": {
        "acknowledged": True,
        "packet_hash": "supplied-by-manifest",
    },
}
IDENTITY_EVIDENCE_SCHEMA = "orchestrator.model-identity-evidence"
IDENTITY_EVIDENCE_VERSION = 1
IDENTITY_AUTHORITIES = {
    "workflows-read-only-trial-artifact/v1",
    "openai-response-metadata/v1",
}
IDENTITY_EVIDENCE_FIELDS = {
    "schema",
    "version",
    "authority",
    "artifact_ref",
    "artifact_sha256",
}
RESULT_FIELDS = {
    "schema",
    "version",
    "trial_id",
    "packet_hash",
    "acknowledged",
    "attempts",
    "auxiliary_traces",
    "identity_verified",
    "quality_by_profile",
}
ATTEMPT_FIELDS = {
    "run_id",
    "profile_id",
    "operation_role",
    "attempt_ordinal",
    "requested_model",
    "selected_model",
    "reported_model",
    "provider_resolved_provider",
    "provider_resolved_model",
    "fallback_reason",
    "runner_version",
    "cli_version",
    "status",
    "latency_s",
    "tokens_in",
    "tokens_out",
    "artifact_ref",
    "packet_hash",
    "acknowledged",
    "identity_evidence",
}
TRACE_FIELDS = {
    "run_id",
    "trace_id",
    "operation",
    "operation_role",
    "provider",
    "model",
    "status",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_artifact_outside_sources(path: Path, source_roots: Iterable[Path]) -> None:
    if any(_is_relative_to(path, root) for root in source_roots):
        raise ValueError("trial artifacts must be outside source roots")


def source_manifest(root: Path) -> dict[str, Any]:
    """Hash a source tree without reading or writing repository metadata."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    entries: list[Any] = []
    total_bytes = 0
    # Prune derived/vendor directories before walking them.  A plain rglob
    # still descends through .git and virtualenv trees on CloudStorage even
    # when their files are later ignored, making the read-only trial appear
    # hung before it can emit a manifest.
    for directory, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_SOURCE_PARTS)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.is_symlink():
                continue
            if path.suffix.lower() in EXCLUDED_SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if len(entries) >= MAX_MANIFEST_FILES:
                raise ValueError(f"source manifest exceeds file limit {MAX_MANIFEST_FILES}: {root}")
            file_bytes = path.stat().st_size
            if total_bytes + file_bytes > MAX_MANIFEST_BYTES:
                raise ValueError(f"source manifest exceeds byte limit {MAX_MANIFEST_BYTES}: {root}")
            content = path.read_bytes()
            total_bytes += len(content)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "bytes": len(content),
                    "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
            )
    return {
        "root": str(root),
        "file_count": len(entries),
        "aggregate_sha256": _sha256(entries),
        "entries": entries,
    }


def _shared_pool_snapshot(*, state: str, captured_at: int, used: float = 0.0) -> dict[str, Any]:
    snapshot = capacity.profile_capacity_snapshot(
        {"agents": {"codex": {"state": state}}},
        pool_usage={"codex-subscription": float(used)},
        registry=execution_profiles.PROFILE_REGISTRY,
    )
    return {
        "captured_at": captured_at,
        "snapshot_count": 1,
        "pools": snapshot["pools"],
        "profiles": snapshot["profiles"],
    }


def build_trial_manifest(
    orchestrator_root: Path,
    workflows_root: Path,
    *,
    seed: int = 56014,
    now: int | None = None,
    capacity_state: str = "unknown",
    packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a frozen, randomized, source-integrity-bound trial plan."""
    timestamp = int(time.time()) if now is None else int(now)
    roots = (orchestrator_root.resolve(), workflows_root.resolve())
    profiles = [execution_profiles.get_profile(profile_id) for profile_id in EXPECTED_PROFILE_IDS]
    pool_ids = {pool_id for profile in profiles for pool_id in profile["capacity_pool_ids"]}
    stop_reasons = []
    if pool_ids != {"codex-subscription"}:
        stop_reasons.append("profiles_do_not_share_exactly_one_codex_pool")
    if capacity_state not in {capacity.OK, capacity.WARN}:
        stop_reasons.append("shared_codex_pool_not_ready")
    if any(profile["provider"] != "openai" for profile in profiles):
        stop_reasons.append("provider_not_fixed")
    if any(profile["reasoning_effort"] != "high" for profile in profiles):
        stop_reasons.append("reasoning_effort_not_fixed")

    frozen_packet = json.loads(json.dumps(packet or FROZEN_PACKET))
    packet_hash = _sha256(frozen_packet)
    source_before = {
        "orchestrator": source_manifest(roots[0]),
        "workflows": source_manifest(roots[1]),
    }
    order = list(EXPECTED_PROFILE_IDS)
    random.Random(int(seed)).shuffle(order)
    identity = {
        "created_at": timestamp,
        "packet_hash": packet_hash,
        "profile_ids": list(EXPECTED_PROFILE_IDS),
        "seed": int(seed),
        "source_hashes": {key: value["aggregate_sha256"] for key, value in source_before.items()},
    }
    trial_id = "model-profile-trial:" + _sha256(identity).split(":", 1)[1][:24]
    pool_snapshot = _shared_pool_snapshot(state=capacity_state, captured_at=timestamp)
    requests = []
    for launch_ordinal, profile_id in enumerate(order, start=1):
        profile = execution_profiles.get_profile(profile_id)
        requests.append(
            {
                "run_id": f"{trial_id}:{profile_id}",
                "launch_ordinal": launch_ordinal,
                "profile_id": profile_id,
                "provider": profile["provider"],
                "requested_model": profile["requested_model"],
                "reasoning_effort": "high",
                "permission_mode": "read-only",
                "sandbox": "read-only",
                "tools": [],
                "prompt_version": profile["prompt_version"],
                "packet_hash": packet_hash,
                "assignment": "instrumentation",
                "learning_enabled": False,
                "capacity_pool_ids": ["codex-subscription"],
            }
        )
    return {
        "schema": TRIAL_SCHEMA,
        "version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "created_at": timestamp,
        "lifecycle": "shadow",
        "promotion_allowed": False,
        "assignment": "instrumentation",
        "learning_enabled": False,
        "frozen_packet": frozen_packet,
        "packet_hash": packet_hash,
        "seed": int(seed),
        "launch_order": order,
        "requests": requests,
        "capacity_snapshot": pool_snapshot,
        "source_before": source_before,
        "stop_conditions": {
            "triggered": bool(stop_reasons),
            "reasons": stop_reasons,
        },
    }


def validate_trial_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != TRIAL_SCHEMA or manifest.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported model-profile trial manifest")
    if (
        manifest.get("assignment") != "instrumentation"
        or manifest.get("learning_enabled") is not False
    ):
        raise ValueError("model-profile trial must remain instrumentation/no-learning")
    if manifest.get("promotion_allowed") is not False or manifest.get("lifecycle") != "shadow":
        raise ValueError("model-profile trial may not promote profiles")
    if manifest.get("stop_conditions", {}).get("triggered"):
        raise ValueError(
            "trial stop condition: " + ",".join(manifest["stop_conditions"].get("reasons") or [])
        )
    packet = manifest.get("frozen_packet")
    if not isinstance(packet, dict) or _sha256(packet) != manifest.get("packet_hash"):
        raise ValueError("trial packet hash does not match frozen packet")
    source_before = manifest.get("source_before")
    if not isinstance(source_before, dict) or set(source_before) != {"orchestrator", "workflows"}:
        raise ValueError("trial source manifest set is incomplete")
    for source in source_before.values():
        if (
            not isinstance(source, dict)
            or not source.get("root")
            or not source.get("aggregate_sha256")
        ):
            raise ValueError("trial source manifest is incomplete")
    profile_ids = _manifest_profile_ids(manifest)
    expected_count = len(profile_ids)
    identity = {
        "created_at": manifest.get("created_at"),
        "packet_hash": manifest.get("packet_hash"),
        "profile_ids": list(profile_ids),
        "seed": manifest.get("seed"),
        "source_hashes": {
            key: source_before[key]["aggregate_sha256"] for key in ("orchestrator", "workflows")
        },
    }
    expected_trial_id = "model-profile-trial:" + _sha256(identity).split(":", 1)[1][:24]
    if manifest.get("trial_id") != expected_trial_id:
        raise ValueError("trial identity is not reproducible")

    requests = manifest.get("requests") or []
    if {item.get("profile_id") for item in requests} != set(profile_ids):
        raise ValueError("trial requires an exact supported profile set")
    if len({item.get("run_id") for item in requests}) != expected_count:
        raise ValueError("trial requires one distinct run identity per profile")
    if manifest.get("capacity_snapshot", {}).get("snapshot_count") != 1:
        raise ValueError("trial requires one shared-pool snapshot")
    launch_order = manifest.get("launch_order") or []
    if sorted(launch_order) != sorted(profile_ids) or len(set(launch_order)) != len(profile_ids):
        raise ValueError("trial launch order is not an exact profile permutation")
    by_ordinal = sorted(requests, key=lambda item: int(item.get("launch_ordinal") or 0))
    if [item.get("profile_id") for item in by_ordinal] != launch_order:
        raise ValueError("trial request ordinals do not match launch order")
    if [int(item.get("launch_ordinal") or 0) for item in by_ordinal] != list(
        range(1, expected_count + 1)
    ):
        raise ValueError("trial launch ordinals must cover the selected profile set")
    for request in requests:
        profile = execution_profiles.get_profile(request["profile_id"])
        expected_run_id = f"{manifest['trial_id']}:{request['profile_id']}"
        if request.get("run_id") != expected_run_id:
            raise ValueError("trial request run identity is not reproducible")
        if profile.get("lifecycle_status") != "active":
            raise ValueError("trial profile is not active in the authoritative registry")
        if request.get("provider") != profile["provider"]:
            raise ValueError("trial provider contradicts the authoritative profile")
        if request.get("requested_model") != profile["requested_model"]:
            raise ValueError("trial model contradicts the authoritative profile")
        if request.get("reasoning_effort") != profile["reasoning_effort"]:
            raise ValueError("trial reasoning effort contradicts the authoritative profile")
        if request.get("prompt_version") != profile["prompt_version"]:
            raise ValueError("trial prompt version contradicts the authoritative profile")
        if request.get("capacity_pool_ids") != profile["capacity_pool_ids"]:
            raise ValueError("trial capacity pool contradicts the authoritative profile")
        if request.get("packet_hash") != manifest["packet_hash"]:
            raise ValueError("trial request packet hash mismatch")
        if (
            request.get("assignment") != "instrumentation"
            or request.get("learning_enabled") is not False
        ):
            raise ValueError("trial request escaped no-learning assignment")
        if request.get("permission_mode") != "read-only" or request.get("tools") != []:
            raise ValueError("trial request is not read-only")


def _weight_snapshot() -> dict[str, Any]:
    with feedback._conn() as conn:
        result = {}
        for table in ("route_weights", "route_weights_v2"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            result[table] = {
                "row_count": len(rows),
                "content_hash": _sha256(rows),
            }
        return result


def _validate_results(manifest: dict[str, Any], results: dict[str, Any]) -> list[dict[str, Any]]:
    profile_ids = _manifest_profile_ids(manifest)
    unknown_results = sorted(set(results) - RESULT_FIELDS)
    if unknown_results:
        raise ValueError(f"trial results contain unsupported fields: {unknown_results}")
    if results.get("schema") != RESULT_SCHEMA or results.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported model-profile trial results")
    if results.get("trial_id") != manifest["trial_id"]:
        raise ValueError("trial result identity mismatch")
    if (
        results.get("packet_hash") != manifest["packet_hash"]
        or results.get("acknowledged") is not True
    ):
        raise ValueError("trial results did not acknowledge the frozen packet")
    attempts = results.get("attempts") or []
    by_profile = {item.get("profile_id"): item for item in attempts}
    if set(by_profile) != set(profile_ids) or len(attempts) != len(profile_ids):
        raise ValueError("trial results require exactly one attempt per profile")
    request_by_profile = {item["profile_id"]: item for item in manifest["requests"]}
    for profile_id, attempt in by_profile.items():
        unknown_attempt = sorted(set(attempt) - ATTEMPT_FIELDS)
        if unknown_attempt:
            raise ValueError(f"trial attempt contains unsupported fields: {unknown_attempt}")
        request = request_by_profile[profile_id]
        if attempt.get("run_id") != request["run_id"]:
            raise ValueError("trial attempt run/profile join mismatch")
        if attempt.get("operation_role") != "worker":
            raise ValueError("trial profile attempt must be a worker")
        if attempt.get("requested_model") != request["requested_model"]:
            raise ValueError("trial requested model changed after launch")
        if (
            attempt.get("packet_hash") != manifest["packet_hash"]
            or attempt.get("acknowledged") is not True
        ):
            raise ValueError("trial attempt did not acknowledge the frozen packet")
        if str(attempt.get("status") or "").lower() not in {"success", "succeeded"}:
            raise ValueError("trial attempt did not succeed")
        if attempt.get("fallback_reason"):
            raise ValueError("trial stopped because a model fallback was reported")
        if attempt.get("selected_model") != request["requested_model"]:
            raise ValueError("trial stopped because selected model differs from requested model")
        if not str(attempt.get("reported_model") or "").strip():
            raise ValueError("trial attempt missing CLI-reported model")
        if attempt.get("reported_model") != request["requested_model"]:
            raise ValueError("trial stopped because reported model differs from requested model")
        if attempt.get("provider_resolved_provider") != request["provider"]:
            raise ValueError("trial attempt missing exact provider-resolved provider")
        if attempt.get("provider_resolved_model") != request["requested_model"]:
            raise ValueError("trial attempt missing exact provider-resolved model")
        if not str(attempt.get("runner_version") or "").strip():
            raise ValueError("trial attempt missing runner version")
        if not str(attempt.get("cli_version") or "").strip():
            raise ValueError("trial attempt missing CLI version")
        evidence = attempt.get("identity_evidence")
        if not isinstance(evidence, dict):
            raise ValueError("trial attempt missing authoritative identity evidence")
        unknown_evidence = sorted(set(evidence) - IDENTITY_EVIDENCE_FIELDS)
        if unknown_evidence:
            raise ValueError(
                f"trial identity evidence contains unsupported fields: {unknown_evidence}"
            )
        if (
            evidence.get("schema") != IDENTITY_EVIDENCE_SCHEMA
            or evidence.get("version") != IDENTITY_EVIDENCE_VERSION
        ):
            raise ValueError("trial attempt identity evidence schema is unsupported")
        if evidence.get("authority") not in IDENTITY_AUTHORITIES:
            raise ValueError("trial attempt identity authority is not trusted")
        if not str(evidence.get("artifact_ref") or "").strip():
            raise ValueError("trial attempt identity evidence has no artifact reference")
        artifact_hash = str(evidence.get("artifact_sha256") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_hash):
            raise ValueError("trial attempt identity evidence has no valid artifact hash")
    for trace in results.get("auxiliary_traces") or []:
        unknown_trace = sorted(set(trace) - TRACE_FIELDS)
        if unknown_trace:
            raise ValueError(f"trial auxiliary trace contains unsupported fields: {unknown_trace}")
        if trace.get("operation_role") == "worker":
            raise ValueError("evaluator trace contaminated worker attempt")
    # Return only the strict allowlist so state artifacts cannot retain a
    # caller-supplied secret or raw provider response by accident.
    return [
        {key: by_profile[profile_id].get(key) for key in sorted(ATTEMPT_FIELDS)}
        for profile_id in profile_ids
    ]


def _capability_program_summary_path() -> Path:
    state_root = Path(
        os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex" / "orchestrator"))
    )
    return state_root / "capability-program" / "profile-trial.json"


def _load_capability_program_trial_summary() -> dict[str, Any] | None:
    path = _capability_program_summary_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_measured_program_summary(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    if payload.get("brain_ingest_enabled"):
        return True
    if payload.get("identity_verified"):
        return True
    quality = payload.get("quality_by_profile")
    if isinstance(quality, dict) and any(value != "n/a" for value in quality.values()):
        return True
    return False


def _authoritative_identity_verified(attempts: list[dict[str, Any]]) -> bool:
    for attempt in attempts:
        evidence = attempt.get("identity_evidence")
        if not isinstance(evidence, dict):
            return False
        ref = str(evidence.get("artifact_ref") or attempt.get("artifact_ref") or "").strip()
        if not ref:
            return False
        artifact_path = Path(ref).expanduser()
        if not artifact_path.is_file():
            return False
        expected_hash = str(evidence.get("artifact_sha256") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash):
            return False
        actual_hash = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            return False
    return True


def _require_verified_identity_for_ingest(
    results: dict[str, Any], attempts: list[dict[str, Any]]
) -> None:
    if results.get("identity_verified") is not True:
        raise ValueError("trial ingest refused: worker identity is unverified")
    if not _authoritative_identity_verified(attempts):
        raise ValueError("trial ingest refused: identity evidence is not authoritative")


def _validated_quality_by_profile(
    results: dict[str, Any], attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    profile_ids = sorted({item["profile_id"] for item in attempts})
    supplied = results.get("quality_by_profile")
    if supplied is None:
        return {profile_id: "n/a" for profile_id in profile_ids}
    if not isinstance(supplied, dict):
        raise ValueError("trial results quality_by_profile must be a mapping")
    unknown = sorted(set(supplied) - set(profile_ids))
    if unknown:
        raise ValueError(f"trial results quality_by_profile has unknown profiles: {unknown}")
    quality_by_profile: dict[str, Any] = {}
    for profile_id in profile_ids:
        value = supplied.get(profile_id, "n/a")
        if value in (None, "n/a"):
            quality_by_profile[profile_id] = "n/a"
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("trial results quality_by_profile values must be numeric or n/a")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("trial results quality_by_profile values must be finite")
        if numeric < 0.0 or numeric > 1.0:
            raise ValueError("trial results quality_by_profile values must be between 0 and 1")
        quality_by_profile[profile_id] = numeric
    return quality_by_profile


def _write_capability_program_trial_summary(
    manifest: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    identity_verified: bool,
    brain_ingest_enabled: bool,
    quality_by_profile: dict[str, Any],
    timestamp: int,
) -> None:
    """Persist a one-line-friendly trial summary for switch review and program tracking."""
    state_root = Path(
        os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex" / "orchestrator"))
    )
    out_dir = state_root / "capability-program"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_ids = sorted({item["profile_id"] for item in attempts})
    tokens_in = sum(int(item.get("tokens_in") or 0) for item in attempts)
    tokens_out = sum(int(item.get("tokens_out") or 0) for item in attempts)
    payload = {
        "trial_id": manifest["trial_id"],
        "updated_at": timestamp,
        "profile_count": len(profile_ids),
        "instance_count": len(attempts),
        "identity_verified": bool(identity_verified),
        "brain_ingest_enabled": bool(brain_ingest_enabled),
        "quality_by_profile": {pid: quality_by_profile.get(pid, "n/a") for pid in profile_ids},
        "cost_tokens_by_profile": {
            pid: {
                "tokens_in": int(
                    next(a for a in attempts if a["profile_id"] == pid).get("tokens_in") or 0
                ),
                "tokens_out": int(
                    next(a for a in attempts if a["profile_id"] == pid).get("tokens_out") or 0
                ),
            }
            for pid in profile_ids
        },
        "aggregate_tokens_in": tokens_in,
        "aggregate_tokens_out": tokens_out,
    }
    _atomic_json(out_dir / "profile-trial.json", payload)


def finalize_trial(
    manifest: dict[str, Any],
    results: dict[str, Any],
    *,
    state_path: Path | None = None,
    record_feedback: bool = False,
    ingest_brain: bool = False,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate source integrity, record instrumentation, and prove no learning writes."""
    validate_trial_manifest(manifest)
    attempts = _validate_results(manifest, results)
    timestamp = int(time.time()) if now is None else int(now)
    source_after = {
        key: source_manifest(Path(value["root"]))
        for key, value in manifest["source_before"].items()
    }
    source_unchanged = all(
        source_after[key]["aggregate_sha256"] == manifest["source_before"][key]["aggregate_sha256"]
        for key in source_after
    )
    if not source_unchanged:
        raise ValueError("source integrity changed during read-only trial")

    if ingest_brain:
        _require_verified_identity_for_ingest(results, attempts)
    identity_verified = (
        results.get("identity_verified") is True and _authoritative_identity_verified(attempts)
    )
    quality_by_profile = _validated_quality_by_profile(results, attempts)

    write_feedback = record_feedback or ingest_brain
    weights_before = _weight_snapshot() if write_feedback else {}
    recorded_attempt_ids: list[str] = []
    brain_ingest_enabled = False
    if write_feedback:
        ingest_payload = feedback.ingest_profile_trial(
            results,
            manifest=manifest,
            attempts=attempts,
            auxiliary_traces=list(results.get("auxiliary_traces") or []),
            ts=timestamp,
            commit_ingest_marker=ingest_brain,
        )
        recorded_attempt_ids = list(ingest_payload.get("recorded_attempt_ids") or [])
        brain_ingest_enabled = ingest_brain and ingest_payload.get("status") in {
            "ingested",
            "already_ingested",
        }
        ingest_status = ingest_payload.get("status")
        if ingest_status != "already_ingested":
            existing_summary = _load_capability_program_trial_summary()
            same_trial_measured = (
                _is_measured_program_summary(existing_summary)
                and existing_summary is not None
                and existing_summary.get("trial_id") == manifest.get("trial_id")
            )
            if not ingest_brain and same_trial_measured:
                preserved = existing_summary or {}
                preserved_quality = preserved.get("quality_by_profile")
                if not isinstance(preserved_quality, dict):
                    preserved_quality = quality_by_profile
                _write_capability_program_trial_summary(
                    manifest,
                    attempts,
                    identity_verified=bool(preserved.get("identity_verified")),
                    brain_ingest_enabled=bool(preserved.get("brain_ingest_enabled")),
                    quality_by_profile=dict(preserved_quality),
                    timestamp=timestamp,
                )
            else:
                _write_capability_program_trial_summary(
                    manifest,
                    attempts,
                    identity_verified=identity_verified,
                    brain_ingest_enabled=brain_ingest_enabled,
                    quality_by_profile=quality_by_profile,
                    timestamp=timestamp,
                )
    weights_after = _weight_snapshot() if write_feedback else {}
    if weights_after != weights_before:
        raise AssertionError("instrumentation trial altered route weights")

    debit = capacity.debit_profile_pools(
        [
            {
                "event": "start",
                "selected_profile_id": request["profile_id"],
                "units": 1,
            }
            for request in manifest["requests"]
        ]
    )
    if debit != {"codex-subscription": float(len(manifest["requests"]))}:
        raise AssertionError("trial did not debit one shared pool once per profile")
    state = {
        "schema": STATE_SCHEMA,
        "version": SCHEMA_VERSION,
        "trial_id": manifest["trial_id"],
        "updated_at": timestamp,
        "lifecycle": "shadow",
        "promotion_allowed": False,
        "assignment": "instrumentation",
        "learning_enabled": False,
        "status": "complete",
        "ready": True,
        "packet_hash": manifest["packet_hash"],
        "launch_order": manifest["launch_order"],
        "capacity_snapshot": manifest["capacity_snapshot"],
        "shared_pool_debit": debit,
        "source_integrity": {
            "unchanged": source_unchanged,
            "before": manifest["source_before"],
            "after": source_after,
        },
        "attempt_count": len(attempts),
        "recorded_attempt_ids": recorded_attempt_ids,
        "attempts": attempts,
        "auxiliary_trace_count": len(results.get("auxiliary_traces") or []),
        "route_weight_integrity": {
            "unchanged": weights_after == weights_before,
            "before": weights_before,
            "after": weights_after,
        },
        "brain_ingest_enabled": brain_ingest_enabled,
        "next_action": "retain_shadow_and_collect_only_productive_outcomes",
    }
    if state_path:
        roots = [Path(value["root"]) for value in manifest["source_before"].values()]
        ensure_artifact_outside_sources(state_path, roots)
        _atomic_json(state_path, state)
    return state


def build_report(path: Path | None = None) -> dict[str, Any]:
    path = path or DEFAULT_STATE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "path": str(path),
            "status": "not_run",
            "ready": False,
            "lifecycle": "shadow",
            "promotion_allowed": False,
            "assignment": "instrumentation",
            "learning_enabled": False,
            "next_action": "prepare_read_only_trial",
        }
    if payload.get("schema") != STATE_SCHEMA or payload.get("version") != SCHEMA_VERSION:
        return {
            "path": str(path),
            "status": "invalid_state",
            "ready": False,
            "lifecycle": "shadow",
            "promotion_allowed": False,
            "assignment": "instrumentation",
            "learning_enabled": False,
            "next_action": "repair_trial_state_contract",
        }
    return {"path": str(path), **payload}


def _selftest() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        orchestrator_root = root / "orchestrator-source"
        workflows_root = root / "workflows-source"
        orchestrator_root.mkdir()
        workflows_root.mkdir()
        (orchestrator_root / "control.py").write_text(
            "CONTROL = 'selftest-unchanged'\n", encoding="utf-8"
        )
        (workflows_root / "worker.yml").write_text("worker: selftest-read-only\n", encoding="utf-8")

        fixture_packet = {
            "schema": "orchestrator.model-profile-trial-packet",
            "version": 1,
            "purpose": "offline model-profile trial selftest",
            "instruction": "Acknowledge this immutable selftest packet without changing files.",
            "expected_output": {
                "acknowledged": True,
                "packet_hash": "supplied-by-manifest",
            },
        }
        manifest_path = root / "prepared-manifest.json"
        manifest = build_trial_manifest(
            orchestrator_root,
            workflows_root,
            seed=14,
            now=1_000,
            capacity_state=capacity.OK,
            packet=fixture_packet,
        )
        ensure_artifact_outside_sources(
            manifest_path,
            (orchestrator_root, workflows_root),
        )
        _atomic_json(manifest_path, manifest)
        prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert prepared == manifest, "prepare did not persist the exact fixture packet"

        recomputed_source_hashes = {
            "orchestrator": source_manifest(orchestrator_root)["aggregate_sha256"],
            "workflows": source_manifest(workflows_root)["aggregate_sha256"],
        }
        prepared_source_hashes = {
            name: proof["aggregate_sha256"] for name, proof in prepared["source_before"].items()
        }
        assert (
            prepared_source_hashes == recomputed_source_hashes
        ), "prepare source-integrity proof did not validate"
        try:
            validate_trial_manifest(prepared)
        except ValueError as exc:
            raise AssertionError("validation rejected the untampered prepared packet") from exc

        tampered = json.loads(_canonical(prepared))
        tampered["frozen_packet"]["instruction"] = "payload altered after proof computation"
        try:
            validate_trial_manifest(tampered)
        except ValueError as exc:
            assert (
                str(exc) == "trial packet hash does not match frozen packet"
            ), f"tampered packet rejection did not name its integrity failure: {exc}"
        else:
            raise AssertionError("validation accepted a packet altered after proof computation")

        attempts: list[dict[str, Any]] = []
        for request in prepared["requests"]:
            artifact_path = root / "artifacts" / f"{request['profile_id']}.json"
            artifact = {
                "run_id": request["run_id"],
                "packet_hash": prepared["packet_hash"],
                "acknowledged": True,
            }
            _atomic_json(artifact_path, artifact)
            artifact_hash = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            attempts.append(
                {
                    "run_id": request["run_id"],
                    "profile_id": request["profile_id"],
                    "operation_role": "worker",
                    "attempt_ordinal": request["launch_ordinal"],
                    "requested_model": request["requested_model"],
                    "selected_model": request["requested_model"],
                    "reported_model": request["requested_model"],
                    "provider_resolved_provider": request["provider"],
                    "provider_resolved_model": request["requested_model"],
                    "fallback_reason": None,
                    "runner_version": "selftest-runner/v1",
                    "cli_version": "selftest-cli/v1",
                    "status": "success",
                    "latency_s": 0.01,
                    "tokens_in": 1,
                    "tokens_out": 1,
                    "artifact_ref": str(artifact_path),
                    "packet_hash": prepared["packet_hash"],
                    "acknowledged": True,
                    "identity_evidence": {
                        "schema": IDENTITY_EVIDENCE_SCHEMA,
                        "version": IDENTITY_EVIDENCE_VERSION,
                        "authority": "workflows-read-only-trial-artifact/v1",
                        "artifact_ref": str(artifact_path),
                        "artifact_sha256": artifact_hash,
                    },
                }
            )
        results: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "version": SCHEMA_VERSION,
            "trial_id": prepared["trial_id"],
            "packet_hash": prepared["packet_hash"],
            "acknowledged": True,
            "attempts": attempts,
            "auxiliary_traces": [],
        }
        state_path = root / "prepared-state.json"
        state = finalize_trial(
            prepared,
            results,
            state_path=state_path,
            record_feedback=False,
            now=2_000,
        )
        assert (
            state["source_integrity"]["unchanged"] is True
        ), "prepared state did not preserve its source-integrity proof"

        state_before_report = state_path.read_bytes()
        report = build_report(state_path)
        assert report == {"path": str(state_path), **state}, "report shape changed"
        assert state_path.read_bytes() == state_before_report, "report mutated the prepared state"
        assert report["lifecycle"] == "shadow", "report escaped the shadow lifecycle"
        assert report["promotion_allowed"] is False, "report allowed profile promotion"
        assert report["learning_enabled"] is False, "report enabled learning"
        assert report["recorded_attempt_ids"] == [], "offline selftest recorded trial attempts"

    print(
        "model_profile_trial.py selftest: OK "
        "(prepare/source proof, accept/reject validation, shadow report/no promotion)"
    )


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities

        capabilities.production_heartbeat(
            "local-model-profile-trial", event_type, ref="model_profile_trial.main"
        )
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="run the offline selftest")
    sub = parser.add_subparsers(dest="command")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--orchestrator-root", type=Path, required=True)
    prepare.add_argument("--workflows-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--seed", type=int, default=56014)
    prepare.add_argument(
        "--capacity-state", choices=("ok", "warn", "shed", "unknown"), default="unknown"
    )
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--manifest", type=Path, required=True)
    finalize.add_argument("--results", type=Path, required=True)
    finalize.add_argument("--state", type=Path, required=True)
    finalize.add_argument("--confirm-instrumentation", action="store_true")
    finalize.add_argument(
        "--ingest",
        action="store_true",
        help="atomically record verified trial rows in the quarantine Brain",
    )
    report = sub.add_parser("report")
    report.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.command is None:
        parser.error("one of --selftest, prepare, finalize, or report is required")

    _capability_heartbeat()
    if args.command == "prepare":
        ensure_artifact_outside_sources(args.output, (args.orchestrator_root, args.workflows_root))
        payload = build_trial_manifest(
            args.orchestrator_root,
            args.workflows_root,
            seed=args.seed,
            capacity_state=args.capacity_state,
        )
        _atomic_json(args.output, payload)
    elif args.command == "finalize":
        if not args.confirm_instrumentation:
            parser.error("finalize requires --confirm-instrumentation")
        payload = finalize_trial(
            json.loads(args.manifest.read_text(encoding="utf-8")),
            json.loads(args.results.read_text(encoding="utf-8")),
            state_path=args.state,
            record_feedback=bool(args.ingest),
            ingest_brain=bool(args.ingest),
        )
    else:
        payload = build_report(args.state)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
